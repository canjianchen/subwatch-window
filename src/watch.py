"""SubWatch capture loop.

Periodically OCRs the screen (or a configured subtitle region), separates the
English and Chinese subtitle lines, applies the chosen display filter to what it
prints, and captures hard English words into the vocabulary database with the
surrounding sentence as context.

This is headless capture-to-notes. The overlay/hide of on-screen subtitles is a
display concern handled by the (optional) overlay; here "hide" governs what the
running log shows so you can read the loop output as a study transcript.
"""
import argparse
import os
import queue
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import config
import detector
import hard_phrases_llm
import ocr
import phrases
import store
import translate
import vocab_feed


import re

# Player chrome that OCR picks up when the control bar is visible (e.g. paused or
# on mouse-move). These are not subtitles and must not pollute capture or dedup.
_NOISE_PATTERNS = [
    re.compile(r"^\d{1,2}:\d{2}(:\d{2})?\s*/\s*\d{1,2}:\d{2}(:\d{2})?$"),  # 00:28:27 / 01:45:19
    re.compile(r"^\d{2,4}[pP]$"),                                            # 576P, 1080p
    re.compile(r"^\d+(\.\d+)?[xX]$"),                                        # 1.0X speed
    re.compile(r"^[©®™\W]+$"),                                               # stray glyphs / icons
]
# Short non-prose tokens commonly emitted by player UI.
_NOISE_TOKENS = {"清晰度", "MP", "HD", "KY", "K刀", "576P", "WP", "倍速", "弹幕", "全屏"}

# Player-UI Chinese phrases (danmaku/controls) that are NOT subtitles. Matched as
# substrings because OCR often merges them with neighbouring glyphs.
_NOISE_SUBSTRINGS = (
    "点击此处输入弹幕", "输入弹幕", "发送弹幕", "点击此处", "发弹幕",
    "登录", "注册", "会员", "缓存", "下载", "选集", "倍速播放",
)

# Desktop/browser chrome can overlap the lower subtitle band (notably Windows taskbar
# previews). These phrases are strong UI signatures, not natural subtitle dialogue.
# Include common OCR variants such as a clipped leading "S" in "SubWatch".
_DESKTOP_UI_SUBSTRINGS = (
    "google chrome", "new tab", "ask google", "type a url", "http://", "https://",
    "netflix.com/", "ubwatch", "live captures", "my dictionary", "design inspector",
    "agentspaces", "amazon optics", "photo by nasa", "add shortcut",
)


# TV channel logos / watermarks that sit in the corner of the capture band.
_NOISE_LOGOS = {"CTV", "BBC", "HBO", "NBC", "CBS", "ABC", "FOX", "CNN", "TNT",
                "AMC", "ITV", "CW", "ESPN", "Netflix", "iQIYI", "Youku"}


def _is_noise(text):
    stripped = text.strip()
    folded = stripped.casefold()
    if any(marker in folded for marker in _DESKTOP_UI_SUBSTRINGS):
        return True
    if stripped in _NOISE_TOKENS or stripped in _NOISE_LOGOS:
        return True
    if any(token in stripped for token in _NOISE_SUBSTRINGS):
        return True
    if any(p.match(stripped) for p in _NOISE_PATTERNS):
        return True
    # a "line" with no run of >=3 latin letters and no CJK is almost certainly UI chrome
    if not detector.has_chinese(stripped) and not re.search(r"[A-Za-z]{3,}", stripped):
        return True
    return False


# Music-note / quote glyphs OCR emits for on-screen song lyrics and stray marks.
_STRIP_GLYPHS = "♪♩♫♬「」『』JS©®™"


def _clean_line(text):
    """Strip stray glyphs and merged channel-logo fragments from a subtitle line."""
    # remove a trailing/leading channel logo even when merged (©TV, CTV, CHV, CTY…)
    text = re.sub(r"[©®]?\s*C[HT][VY]\b", " ", text)
    text = re.sub(r"\b(CTV|TNV|CHV|CTY|TNT)\b", " ", text)
    # drop leading/trailing music & quote glyphs used around lyrics
    text = text.strip().strip(_STRIP_GLYPHS).strip()
    # OCR commonly merges the pronoun "I" into the next word ("Iam"→"I am",
    # "Ijust"→"I just") and reads standalone "I" as lowercase "l". Repair both so
    # the following word isn't lost to the detector.
    text = re.sub(r"\bI([a-z]{2,})", lambda m: "I " + m.group(1)
                  if m.group(1) in _I_MERGES else m.group(0), text)
    text = re.sub(r"\bl\b", "I", text)
    # stray vertical-bar / broken-glyph artifacts OCR inserts mid-line
    text = text.replace("|", " ")
    # OCR sometimes inserts a stray slash before an apostrophe ("Missy/'s" -> "Missy's")
    text = text.replace("/'", "'").replace("/’", "’")
    # collapse whitespace
    return re.sub(r"\s{2,}", " ", text).strip()


# Words that frequently get merged onto a leading "I" by OCR.
_I_MERGES = {
    "am", "was", "will", "would", "can", "could", "should", "just", "think",
    "know", "want", "need", "got", "had", "have", "did", "do", "dont", "cant",
    "studied", "wanted", "guess", "mean", "love", "like", "said", "saw", "see", "nailed",
}


def classify_lines(lines):
    """Split OCR lines into (english_lines, chinese_lines), dropping player chrome."""
    english, chinese = [], []
    for line in lines:
        text = _clean_line(line["text"].strip())
        if not text or _is_noise(text):
            continue
        if detector.has_chinese(text):
            chinese.append(text)
        elif looks_like_dialogue(text):
            english.append(text)
        # else: drop non-dialogue English (credits, title cards)
    return english, chinese


def apply_display(english, chinese, mode):
    """Return the subtitle text to display given the hide mode."""
    if mode == "hide_chinese":
        return english
    if mode == "hide_english":
        return chinese
    if mode == "hide_both":
        return []
    return english + chinese  # show_both


def _ocr_quality_ok(text):
    """Reject lines that are mostly OCR garbage (jitter like "invisgihle strings").
    If fewer than ~60% of the alphabetic words are real dictionary words, the line
    is too corrupted to feed the LLM reliably."""
    words = [w.lower().strip("'-") for w in re.findall(r"[A-Za-z][A-Za-z'\-]+", text)]
    words = [w for w in words if len(w) >= 3]
    if not words:
        return False
    if len(words) < 3:
        return True  # too short to judge corruption; let the LLM decide in context
    real = sum(1 for w in words if detector.is_real_word(w))
    return real / len(words) >= 0.6


def looks_like_dialogue(text):
    """Reject end-credits / title cards / dense non-subtitle text.

    Real subtitle lines are short and sentence-like. Credit rolls produce long
    strings packed with Capitalized Names and fragments. Heuristics:
      • too long (a subtitle line rarely exceeds ~90 chars)
      • too many Capitalized-word runs (names list)
      • very low ratio of lowercase letters (ALL-CAPS title cards)
    """
    stripped = text.strip()
    if len(stripped) > 90:
        return False
    words = re.findall(r"[A-Za-z]+", stripped)
    if len(words) > 14:
        return False
    if words:
        caps = sum(1 for w in words if w[0].isupper())
        if len(words) >= 5 and caps / len(words) > 0.6:
            return False  # mostly capitalized → names/title card
    letters = [c for c in stripped if c.isalpha()]
    if len(letters) >= 8:
        lower_ratio = sum(1 for c in letters if c.islower()) / len(letters)
        if lower_ratio < 0.45:
            return False  # mostly uppercase → title card
    return True


def _norm(text):
    """Lowercase, strip punctuation/spaces — for comparing two OCR'd lines."""
    return re.sub(r"[^a-z0-9]+", "", text.lower())


def _very_similar(a, b):
    """True if two subtitle lines are essentially the same (OCR jitter tolerant)."""
    if not b:
        return False
    na, nb = _norm(a), _norm(b)
    if not na or not nb:
        return na == nb
    if na == nb:
        return True
    # one is a prefix/substring of the other (partial line growing in) → treat same
    if na in nb or nb in na:
        return True
    # quick char-overlap ratio via difflib
    import difflib
    return difflib.SequenceMatcher(None, na, nb).ratio() >= 0.90


# Short-term memory of recently-processed subtitle lines, so a lingering/flickering
# subtitle (or OCR jitter) doesn't trigger repeated work — especially the LLM call.
_RECENT = []
_RECENT_MAX = 12


def _recently_processed(text):
    return any(_very_similar(text, prev) for prev in _RECENT)


def _remember(text):
    _RECENT.append(text)
    if len(_RECENT) > _RECENT_MAX:
        _RECENT.pop(0)


def process_frame(cfg, last_line):
    lines = ocr.capture_text(
        region=cfg.get("capture_region"),
        min_confidence=cfg["min_confidence"],
        display=cfg.get("display"),
    )
    if not lines:
        return last_line, []

    english, chinese = classify_lines(lines)
    english_text = " ".join(english).strip()
    # A subtitle is at most a few short lines. If full-screen OCR accidentally
    # includes tabs, address bars and page controls, joining those fragments creates
    # a long UI dump; reject it instead of storing that dump as every card's context.
    english_words = re.findall(r"[A-Za-z]+", english_text)
    if len(english) > 3 or len(english_text) > 180 or len(english_words) > 28:
        english_text = ""

    # Skip if this is essentially the same as the immediately previous line (and not
    # a longer/more-complete version), OR if we've processed a near-identical line in
    # the recent window. The recent-window check stops a flickering subtitle from
    # re-invoking the (costly) LLM on every frame.
    if english_text and _very_similar(english_text, last_line) \
            and len(english_text) <= len(last_line):
        return last_line, []
    if english_text and _recently_processed(english_text):
        return english_text, []
    # Always remember the longest version we've seen of the current line.
    if english_text and _very_similar(english_text, last_line) and len(english_text) > len(last_line):
        last_line = english_text

    captured = []
    if english_text:
        chinese_ctx = " ".join(chinese[:3]).strip() or None
        if chinese_ctx and len(chinese_ctx) > 180:
            chinese_ctx = None

        # SMART MODE (default): let the LLM judge which words/phrases are actually
        # hard for a learner. A Codex call is too slow to run
        # inline, as it would freeze OCR + the on-screen subtitle. So we ENQUEUE the
        # line for a background worker (see _score_worker) and return immediately;
        # captures land in the deck a few seconds later, asynchronously.
        if cfg.get("smart_capture", True) and not cfg.get("local_only", False):
            # The user selected Codex mode. If Codex is temporarily unavailable, skip
            # this frame instead of silently creating untranslated local-only cards.
            # Do not remember it, so a lingering subtitle can be retried after login or
            # connectivity recovers.
            if not _ocr_quality_ok(english_text) or not config.codex_available():
                return english_text or last_line, []
            # mark processed up front so a flickering/lingering subtitle doesn't enqueue
            # the same line many times while the worker is still scoring it.
            _remember(english_text)
            _enqueue_for_scoring(english_text, chinese_ctx, cfg)
            return english_text or last_line, []

        # FREQUENCY MODE (only when smart_capture is explicitly OFF): rank-based words ...
        _remember(english_text)
        overlay_items = []
        for word, rank in detector.hard_words(english_text, cfg):
            overlay_items.append({"term": word, "kind": "word", "cn": ""})
            if store.upsert_term(word, context=english_text, chinese=chinese_ctx, rarity_rank=rank):
                captured.append((word, rank))
        # ... plus curated idiom list
        if cfg.get("capture_phrases", True):
            for matched in phrases.find_phrases(english_text):
                overlay_items.append({"term": matched, "kind": "phrase", "cn": ""})
                if store.upsert_phrase(matched, english_text, chinese=chinese_ctx):
                    captured.append((f"“{matched}”", "phrase"))
        # mirror the line's words to the on-screen vocab overlay
        vocab_feed.push(overlay_items, english_text, chinese_ctx)

    return english_text or last_line, captured


# ── Background LLM scoring ──────────────────────────────────────────────────
# The difficulty Codex call is asynchronous. Running it inline froze the
# capture loop. Instead the loop enqueues each new line here, and a small pool of
# worker threads scores + stores captures asynchronously. store.py opens a fresh
# SQLite connection per call, so writing from worker threads is safe.
_SCORE_Q = queue.Queue(maxsize=12)
_WORKERS_STARTED = False
_SCORE_WORKERS = 6  # enough parallel calls to keep pace with fast dialogue
_SCORE_SEQUENCE = 0
_LATEST_OVERLAY_SEQUENCE = -1
_SCORE_LOCK = threading.Lock()

# Translation runs on a cheap API, so fire the line + per-term translations CONCURRENTLY
# (with each other AND with the LLM difficulty grader) instead of one blocking call at a
# time. Fast dialogue scrolls quickly; overlapping these network calls is what keeps the
# on-screen 中文 landing before the line leaves the screen. Shared pool, lazily created.
_TRANSLATE_POOL = None
_TRANSLATE_POOL_LOCK = threading.Lock()


def _translate_pool():
    global _TRANSLATE_POOL
    if _TRANSLATE_POOL is None:
        with _TRANSLATE_POOL_LOCK:
            if _TRANSLATE_POOL is None:
                _TRANSLATE_POOL = ThreadPoolExecutor(max_workers=8,
                                                     thread_name_prefix="subwatch-tr")
    return _TRANSLATE_POOL


def _enqueue_for_scoring(english_text, chinese_ctx, cfg):
    """Hand a subtitle line to the background scorers. Drops the line (rather than
    blocking the loop) if the queue is full — better to miss a line than stall."""
    global _SCORE_SEQUENCE, _LATEST_OVERLAY_SEQUENCE
    _ensure_workers()
    with _SCORE_LOCK:
        _SCORE_SEQUENCE += 1
        sequence = _SCORE_SEQUENCE
    try:
        _SCORE_Q.put_nowait((sequence, english_text, chinese_ctx,
                             cfg.get("smart_level", "intermediate")))
    except queue.Full:
        return None  # scorers are backed up; skip this line, the loop keeps running
    # Put safe local candidates on screen immediately. Codex replaces this provisional
    # set with verified terms and Chinese translations when its answer arrives.
    pending = [{"term": word, "kind": "word", "cn": "翻译中…"}
               for word, _rank in detector.hard_words(english_text, cfg)]
    if cfg.get("capture_phrases", True):
        pending.extend({"term": phrase, "kind": "phrase", "cn": "翻译中…"}
                       for phrase in phrases.find_phrases(english_text))
    with _SCORE_LOCK:
        _LATEST_OVERLAY_SEQUENCE = sequence
    if pending:
        vocab_feed.replace(pending, english_text, chinese_ctx)
    return sequence


def _ensure_workers():
    global _WORKERS_STARTED
    if _WORKERS_STARTED:
        return
    _WORKERS_STARTED = True
    for _ in range(_SCORE_WORKERS):
        thread = threading.Thread(target=_score_worker, daemon=True)
        thread.start()


def _score_worker():
    while True:
        sequence, english_text, chinese_ctx, level = _SCORE_Q.get()
        try:
            _stream_and_store(english_text, chinese_ctx, level, sequence=sequence)
        except Exception as exc:  # noqa: BLE001 — never let a worker die
            print(f"   (scoring error: {exc})", flush=True)
        finally:
            _SCORE_Q.task_done()


def _stream_and_store(english_text, chinese_ctx, level, sequence=None):
    """Stream the LLM difficulty scorer over one line and save + print each capture the
    MOMENT it arrives, so detailed words land in the deck progressively (first ~3s)
    instead of all at once after the whole response (~6.5s). Pure work — no loop state —
    safe on a background thread."""
    sentence_cn = chinese_ctx  # updated from the first streamed line, which is sentence_cn
    # When the split is on, the LLM only judges difficulty; the translation API supplies
    # the whole-line Chinese and the per-term Chinese. To keep up with fast subtitles we
    # OVERLAP all of it: the line translation starts immediately (concurrent with the LLM
    # grader), and each term's translation is dispatched the moment it streams in — so
    # the network round-trips run in parallel instead of one-after-another.
    api_translate = hard_phrases_llm.translation_by_api()
    pool = _translate_pool() if api_translate else None
    line_future = None
    if api_translate and not chinese_ctx:
        # only translate the whole line when we don't already have the on-screen Chinese
        line_future = pool.submit(translate.translate_to_zh, english_text)

    overlay_items = []  # all VALID hard words of this line, for the on-screen overlay
    pending = []        # (item, term_cn_future_or_none) awaiting translation, in stream order
    for obj in hard_phrases_llm.extract_hard_stream(english_text, level=level):
        if obj.get("_failed"):
            break  # true LLM error — skip the line (don't fall back to the frequency flood)
        if "sentence_cn" in obj and "term" not in obj:
            sentence_cn = obj.get("sentence_cn") or sentence_cn
            vocab_feed.push([], english_text, sentence_cn)
            continue
        # With the split on, the LLM omits Chinese — kick off the term translation NOW
        # (don't block the stream loop) and collect the result after the stream ends.
        term_future = None
        if api_translate and not (obj.get("translation") or obj.get("chinese")):
            term_text = (obj.get("term") or "").strip()
            if term_text:
                term_future = pool.submit(translate.translate_to_zh, term_text)
        pending.append((obj, term_future))

    if line_future is not None:
        try:
            line_cn = line_future.result(timeout=8)
        except Exception:  # noqa: BLE001
            line_cn = None
        if line_cn:
            sentence_cn = line_cn
            vocab_feed.push([], english_text, sentence_cn)

    for obj, term_future in pending:
        if term_future is not None and not (obj.get("translation") or obj.get("chinese")):
            try:
                api_cn = term_future.result(timeout=8)
            except Exception:  # noqa: BLE001
                api_cn = None
            if api_cn:
                obj["translation"] = api_cn
        term_cn = (obj.get("translation") or obj.get("chinese") or "").strip()
        # A word card without its Chinese meaning is incomplete. Ignore a malformed model
        # item (or a term the translation API couldn't render) rather than showing a blank card.
        if not term_cn:
            continue
        stored = _store_item(obj, english_text, sentence_cn, level)
        if not stored:
            continue
        term, kind, is_new = stored
        # show EVERY valid hard word of this line on the overlay (new or already-known),
        # since the user wants to read them on screen as the line plays.
        overlay_items.append({"term": term, "kind": kind, "cn": term_cn})
        if is_new:
            print(f"   ➕ captured: {term}  ({kind})", flush=True)
    # Push in capture order. With multiple fast Codex workers an older request can
    # finish after a newer one; never let that stale result replace the current overlay.
    global _LATEST_OVERLAY_SEQUENCE
    if sequence is None:
        vocab_feed.replace(overlay_items, english_text, sentence_cn)
    else:
        with _SCORE_LOCK:
            stale = sequence < _LATEST_OVERLAY_SEQUENCE
            if not stale:
                _LATEST_OVERLAY_SEQUENCE = sequence
        if stale:
            vocab_feed.replace([], english_text, sentence_cn)
            return
        vocab_feed.replace(overlay_items, english_text, sentence_cn)


def _store_item(item, english_text, sentence_cn, level):
    """Filter and store ONE scored item. Returns (term, kind, is_new) if the item is a
    VALID hard word — is_new True if it was just added to the deck, False if already
    known (still valid, so the overlay can show it). Returns None if the item fails the
    proper-noun, OCR-garbage or frequency-floor guards (not worth showing at all)."""
    term = (item.get("term") or "").strip()
    if not term:
        return None
    kind = item.get("kind", "word")
    # Drop person-name proper nouns (credits like "Chuck Lorre"); keep cultural references
    # the LLM marks as worth a note (MTV, etc) only if single token / acronym.
    if "proper" in kind.lower():
        if " " in term and not term.isupper():
            return None
        kind = "word"
    # Validate single-word terms: reject OCR garbage the LLM echoed back, AND apply a
    # frequency FLOOR — a word among the N most common English words cannot be "hard"
    # no matter how the LLM scored it. Multi-word phrases are exempt (idioms are made
    # of common words). (rank is None = absent from the 10k list = let it through.)
    if kind == "word" and " " not in term:
        clean = term.strip(".,!?;:'\"").lower()
        if not detector.is_real_word(clean):
            # The bundled frequency/dictionary list is intentionally small and misses
            # valid derived words such as "incentivize". Trust a clean alphabetic term
            # only when Codex gave it a high score plus both a definition and Chinese;
            # weak/malformed OCR guesses still fail closed.
            try:
                score = int(item.get("score", 0))
            except (TypeError, ValueError):
                score = 0
            model_verified = bool(
                re.fullmatch(r"[A-Za-z][A-Za-z'-]{2,}", clean)
                and score >= 7
                and (item.get("definition") or "").strip()
                and (item.get("translation") or item.get("chinese") or "").strip()
            )
            if not model_verified:
                return None
        rank = detector.rarity_rank(clean)
        # floor = rank below which a word is "too common to be hard". A LOWER floor lets
        # MORE common words through — beginners want that; experts want it high so only
        # rare words survive.
        # Higher learner levels must reject a larger common-vocabulary prefix. The
        # previous intermediate/expert values were reversed, causing intermediate
        # mode to discard more useful words than advanced mode.
        _floor = {"beginner": 1500, "intermediate": 3000, "advanced": 5000,
                  "expert": 8000}.get(level, 3000)
        if rank is not None and rank <= _floor:
            return None
    # Prefer the LLM's clean whole-line translation for the 中文字幕 — it corrects OCR
    # character errors in the on-screen Chinese. Fall back to OCR'd Chinese otherwise.
    sub_cn = sentence_cn
    if kind == "phrase":
        is_new = store.upsert_phrase(term, english_text, chinese=sub_cn)
        key = f"phrase:{term.lower()}"
    else:
        is_new = store.upsert_term(term.lower(), context=english_text,
                                   chinese=sub_cn, rarity_rank=None)
        key = term.lower()
    # Always refresh enrichment, not only on first insert. This repairs an existing
    # card that was originally captured during a transient local-only fallback.
    store.set_enrichment(key, definition=item.get("definition"),
                         chinese=item.get("chinese"),
                         translation=item.get("translation"))
    return (term, kind, is_new)


_last_fit_check = [0.0, None]  # (timestamp, (display,w,h) signature) to throttle the check


def _display_pixel_size(display):
    """Actual captured pixel size of a display (backing pixels), via a quick grab."""
    try:
        if os.name == "nt":
            import mss
            with mss.mss() as sct:
                monitors = sct.monitors[1:]
                index = int(display or 1) - 1
                if index < 0 or index >= len(monitors):
                    index = 0
                monitor = monitors[index]
                return int(monitor["width"]), int(monitor["height"])
        import ocr
        path = ocr._mac_grab_screen(region=None, display=display)
        out = subprocess.run(["sips", "-g", "pixelWidth", "-g", "pixelHeight", path],
                             capture_output=True, text=True).stdout
        os.remove(path)
        w = h = 0
        for line in out.splitlines():
            if "pixelWidth" in line:
                w = int(line.split(":")[1])
            if "pixelHeight" in line:
                h = int(line.split(":")[1])
        return (w, h) if w and h else None
    except Exception:  # noqa: BLE001
        return None


def _ensure_region_fits(cfg):
    """If the saved capture_region no longer fits the current display (you switched or
    rearranged monitors), re-autodetect so capture keeps working automatically.
    Throttled to once every ~6s and skipped when nothing changed."""
    import time as _t
    region = cfg.get("capture_region")
    display = cfg.get("display")
    now = _t.time()
    if now - _last_fit_check[0] < 6:
        return cfg
    _last_fit_check[0] = now
    size = _display_pixel_size(display)
    if not size:
        return cfg
    w, h = size
    sig = (display, w, h)
    if not region:
        # Safe cross-player fallback: a wide band covering the lower-middle subtitle
        # area while excluding browser tabs/address bars and the bottom control strip.
        region = [int(w * 0.08), int(h * 0.55), int(w * 0.84), int(h * 0.37)]
        cfg["capture_region"] = region
        config.save_config(cfg)
        _last_fit_check[1] = sig
        print(f"  ✓ using subtitle-safe region {region}", flush=True)
        return cfg
    fits = region and region[0] >= 0 and region[1] >= 0 \
        and region[0] + region[2] <= w + 4 and region[1] + region[3] <= h + 4
    if fits and _last_fit_check[1] == sig:
        return cfg
    _last_fit_check[1] = sig
    if not fits:
        # region is off-screen for this monitor → re-detect
        print(f"  ↻ display changed ({w}×{h}) — re-detecting subtitle region…", flush=True)
        try:
            import autodetect
            new_region, _texts = autodetect.detect_region_retry(display=display, attempts=4, delay=0.8)
            if new_region:
                cfg["capture_region"] = new_region
                config.save_config(cfg)
                print(f"  ✓ new region {new_region}", flush=True)
        except Exception as exc:  # noqa: BLE001
            print(f"  (re-detect failed: {exc})", flush=True)
    return cfg


def run(once=False):
    store.init_db()
    cfg = config.load_config()
    mode = cfg["display_mode"]
    interval = cfg["capture_interval"]

    print(f"SubWatch watching | display={mode} | interval={interval}s | "
          f"rarity>{cfg['rarity_threshold']} | Ctrl-C to stop\n", flush=True)

    last_line = ""
    try:
        while True:
            # reload config each cycle so live changes (e.g. the level auto-adjusting
            # from your ✓/✗ marks, or panel toggles) apply without a restart.
            cfg = config.load_config()
            # AUTO-FIT the capture region to the current monitor: if the saved region
            # no longer fits the display's pixel size (you switched/rearranged screens),
            # re-autodetect so capture keeps working without manual `autodetect`.
            cfg = _ensure_region_fits(cfg)
            mode = cfg["display_mode"]
            interval = cfg["capture_interval"]
            try:
                last_line, captured = process_frame(cfg, last_line)
            except PermissionError as exc:
                print(f"\n⚠️  {exc}\n", flush=True)
                return
            except Exception as exc:  # noqa: BLE001 — a transient OCR/screencapture
                # hiccup must not kill the whole watch loop; log and keep going.
                print(f"   (capture error: {exc})", flush=True)
                time.sleep(interval)
                continue

            english_now, chinese_now = ([], [])
            # Re-derive shown subtitle for the live transcript from last_line.
            if last_line:
                if detector.has_chinese(last_line):
                    chinese_now = [last_line]
                else:
                    english_now = [last_line]
            shown = apply_display(english_now, chinese_now, mode)
            if shown:
                print(f"  📺 {' / '.join(shown)}", flush=True)
            for word, rank in captured:
                tag = "NEW" if rank is None else f"rank {rank}"
                print(f"   ➕ captured: {word}  ({tag})", flush=True)

            if once:
                # wait for the background scorer to finish this line before exiting,
                # otherwise the daemon thread is killed mid-LLM-call and nothing is saved.
                if _WORKERS_STARTED:
                    _SCORE_Q.join()
                break
            time.sleep(interval)
    except KeyboardInterrupt:
        pass

    s = store.stats()
    print(f"\nStopped. Vocabulary: {s['total']} terms, {s['due']} due for review, "
          f"{s['mastered']} mastered.", flush=True)


def main():
    parser = argparse.ArgumentParser(description="SubWatch — watch & learn English from subtitles")
    parser.add_argument("--once", action="store_true", help="capture a single frame and exit")
    args = parser.parse_args()
    run(once=args.once)


if __name__ == "__main__":
    sys.exit(main())
