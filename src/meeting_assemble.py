"""Transcript assembler — turns a noisy live caption stream into a clean, timestamped,
de-duplicated transcript.

This is the genuinely new piece of Meeting Mode. Zoom's caption surface is a rolling
text box: the bottom line GROWS word-by-word, then SCROLLS up as the next utterance
begins; the same sentence is therefore re-read across many polls (as a growing prefix,
and again verbatim with jitter before it scrolls off). The job is to emit each utterance
EXACTLY ONCE, finalized, when it stops changing.

watch.py's dedup is the WRONG tool here — it's tuned for replace-style subtitles (one
slot replaced by the next) and has no notion of a line growing then scrolling, nor of
"final vs still-growing". So this is a NEW stateful assembler. It DOES reuse watch's
OCR-repair (_clean_line) and quality gate (_ocr_quality_ok), and a meeting-specific norm
that keeps word boundaries (unlike watch._norm).

Source-agnostic: feed it either rolling AX/OCR frames (a list of visible lines) via
push_frame(), or already-final non-overlapping chunks (Whisper) via push_final(). Both
land in the same finalize + cross-utterance-dedup path and call the same on_segment sink.

Borrowed from Amazon's internal Livestream captioning service: treat each line as
partial-then-final, upsert partials in place keyed by a stable id, and lock on finalize.
"""
import difflib
import re
import time

import watch  # reuse _ocr_quality_ok (garbage gate) + _I_MERGES


# Zoom UI chrome strings that are valid English (so they pass the letter-ratio gate) but
# are NOT speech — AX harvesting and stray OCR pick these up at the window edges. Matched
# case-insensitively: exact-equal, or a known control phrase appearing as a substring.
_CHROME_EXACT = {
    "settings", "editor", "apps", "loading", "loading…", "more", "chat", "participants",
    "mute", "unmute", "start video", "stop video", "share", "share screen", "reactions",
    "view", "leave", "record", "recording", "speaker view", "gallery view", "captions",
    "live transcript", "show captions", "hide captions", "full screen",
}
_CHROME_CONTAINS = (
    "has started screen sharing", "has stopped screen sharing",
    "the shared content is fit to your screen", "started screen sharing",
    "is sharing", "you are screen sharing", "view options", "to see the original",
)


def _is_chrome(text):
    """True if the line is Zoom UI chrome (button labels, share-banner, etc.) rather than
    spoken caption text. These slip past the noise filter because they're real words."""
    t = (text or "").strip().lower().rstrip(".…")
    if t in _CHROME_EXACT:
        return True
    return any(phrase in t for phrase in _CHROME_CONTAINS)


def _is_caption_noise(text):
    """Reject OCR noise that Zoom's caption panel produces at its edges: speaker-icon
    fragments ('-H Oh'), stray CJK/garbage with no real English ('IIapHEI！！'), and lines
    that are mostly non-letters. Captions are English prose, so require a couple of real
    multi-letter words and a healthy letter ratio."""
    t = (text or "").strip()
    if len(t) < 3:
        return True
    if _is_chrome(t):
        return True
    letters = sum(c.isalpha() and c.isascii() for c in t)
    if letters < max(3, len(t) * 0.45):
        return True  # mostly punctuation / non-latin → noise
    words = re.findall(r"[A-Za-z]{2,}", t)
    if len(words) < 1:
        return True
    # a leading icon-fragment like "-H " or "-HOh" — strip handled in _clean_caption,
    # but a line that is ONLY such a fragment is noise
    if len(words) == 1 and len(words[0]) <= 2:
        return True
    return False


def _clean_caption(text):
    """OCR-repair a caption line. Like watch._clean_line but WITHOUT its lyric-glyph
    stripping — watch._STRIP_GLYPHS contains 'S' and 'J', which would clip a leading
    letter from words like 'Speaker'/'John' (fine for song-lyric subtitles, wrong for
    meeting captions). Keeps the genuinely useful repairs: I-merges, l→I, stray bars,
    slash-before-apostrophe."""
    text = (text or "").strip().strip("♪♩♫♬「」『』").strip()
    # strip a leading speaker-icon OCR artifact Zoom's caption panel emits, e.g.
    # "-H Oh," / "-HOh" / "» " before the actual words
    text = re.sub(r"^[-=»>•·\s]*H?\s*(?=[A-Z])", "", text) if re.match(r"^[-=»>•·]", text) else text
    text = re.sub(r"\bI([a-z]{2,})", lambda m: "I " + m.group(1)
                  if m.group(1) in watch._I_MERGES else m.group(0), text)
    text = re.sub(r"\bl\b", "I", text)
    text = text.replace("|", " ").replace("/'", "'").replace("/’", "’")
    return re.sub(r"\s{2,}", " ", text).strip()


# Speaker prefix: a leading Title-Case name run (1-4 words), optional (Role), then ": ".
_SPEAKER_RE = re.compile(
    r"^(?P<name>[A-Z][\w.'’-]+(?:\s+[A-Z][\w.'’-]+){0,3})(?:\s*\([^)]+\))?\s*:\s+(?P<rest>\S.*)$")
# Labels that look like "Name:" but aren't speakers.
_NOT_SPEAKER = {"note", "warning", "http", "https", "error", "tip", "ps", "re", "fwd"}


def _norm_meeting(text):
    """Comparison-only normalization that KEEPS word boundaries (unlike watch._norm,
    which strips all non-alnum). Lowercase, collapse whitespace, drop terminal
    punctuation, fold a few common OCR confusions. Never used for stored text."""
    t = (text or "").lower().strip()
    t = re.sub(r"[\s]+", " ", t)
    t = re.sub(r"[.?!,;:]+$", "", t)
    # fold OCR-ambiguous glyphs for comparison robustness
    t = t.replace("|", "i").replace("1", "i").replace("0", "o")
    return t.strip()


def _similar(a, b):
    """Fuzzy similarity 0..1 on normalized strings (difflib — no extra dependency)."""
    if not a or not b:
        return 0.0
    return difflib.SequenceMatcher(None, a, b).ratio()


def _split_speaker(text):
    """Return (speaker_or_None, rest). Strips a leading 'Name: ' prefix when it looks
    like a real speaker label, guarding against false positives."""
    m = _SPEAKER_RE.match(text)
    if not m:
        return None, text
    name = m.group("name").strip()
    rest = m.group("rest").strip()
    if name.lower() in _NOT_SPEAKER or any(ch.isdigit() for ch in name):
        return None, text
    # require the remainder to contain a letter (rules out URL tails / pure punctuation).
    # A single letter is allowed so a still-growing turn like "Raj: I" splits the same way
    # as its grown form "Raj: I finished…" — otherwise the two wouldn't prefix-match and a
    # stray "Raj: I" fragment would be committed.
    if not re.search(r"[A-Za-z]", rest):
        return None, text
    return name, rest


class _ActiveLine:
    """A caption line currently visible in the box (growing or stable)."""
    __slots__ = ("key", "text", "norm", "speaker", "first_ts", "last_change_ts",
                 "frames_stable", "seen_below", "finalized", "seq")

    def __init__(self, key, text, norm, speaker, now):
        self.key = key
        self.text = text
        self.norm = norm
        self.speaker = speaker
        self.first_ts = now
        self.last_change_ts = now
        self.frames_stable = 0
        self.seen_below = False     # a newer line appeared below → this one is done growing
        self.finalized = False
        self.seq = None             # assigned at finalize


class Assembler:
    """Stateful transcript assembler. Call push_frame(lines) for rolling caption frames
    or push_final(text) for already-complete chunks. Finalized utterances are delivered
    to on_segment(seg) where seg = {seq, speaker, text, norm, t_start_ms, t_end_ms,
    is_partial, result_id}. Also emits live partials (is_partial=1) so the panel can show
    the in-progress line immediately."""

    def __init__(self, on_segment, started_at=None, cfg=None, roster=None):
        self.on_segment = on_segment
        self.started_at = started_at or time.time()
        cfg = cfg or {}
        self.stable_frames = int(cfg.get("stable_frames", 4))
        self.grow_sim = float(cfg.get("grow_similarity", 0.85))
        self.dup_sim = float(cfg.get("dup_similarity", 0.92))
        self.dup_window_s = float(cfg.get("dup_window_seconds", 20.0))
        self.silence_reset_s = float(cfg.get("speaker_reset_seconds", 8.0))
        # the real attendee roster from the calendar — used to snap an active-speaker /
        # OCR'd name to the correct spelling (e.g. 'Nick D' / 'Kenjian' → 'Nick Deng').
        self.roster = list(roster or [])

        self._active = []           # ordered list of _ActiveLine (display order, top→bottom)
        self._next_key = 0
        self._next_seq = 0
        self._tail = []             # recent finalized {norm, ts, seq, text} for cross-utterance dedup
        self._seen_norms = set()    # session-wide set of committed substantial-line norms
        self._committed_visible = {}  # norm -> end_ts for committed lines STILL on screen.
                                      # Zoom keeps a finalized line visible 60s+; once it
                                      # leaves _active it can re-spawn and re-commit. We
                                      # suppress that until the line actually scrolls off
                                      # (drops out of the visible frame), so a genuine later
                                      # repeat of the same short ack still counts.
        self._current_speaker = None
        self._last_seg_ts = self.started_at
        self._speakers = {}         # canonical-name registry for fuzzy-merge

    # ── public API ───────────────────────────────────────────────────────────
    def push_frame(self, raw_lines, speaker_hint=None):
        """Process one rolling caption frame (list of visible line strings, top→bottom).
        speaker_hint (e.g. Zoom's current active speaker from AX) attributes lines that
        carry no inline 'Name:' prefix — Zoom's caption panel shows avatars, not names, so
        this is the real source of speaker attribution."""
        now = time.time()
        if speaker_hint:
            self._current_speaker = self._canonical_speaker(speaker_hint)
        cleaned = []
        for raw in raw_lines:
            speaker, body = self._prepare(raw)
            if body is None:
                continue
            norm = _norm_meeting(body)
            if not norm:
                continue
            cleaned.append((speaker or self._current_speaker, body, norm))

        # Prune the committed-and-still-visible set: any norm we committed earlier that is
        # NO LONGER on screen has scrolled off, so a future reappearance is a genuine repeat.
        visible_norms = {norm for (_sp, _b, norm) in cleaned}
        self._committed_visible = {n: ts for n, ts in self._committed_visible.items()
                                   if n in visible_norms}

        if not cleaned:
            # nothing visible this frame → everything that WAS active scrolled off
            self._finalize_absent(set(), now)
            self._tick_timeouts(now)
            return

        present_keys = set()
        for idx, (speaker, body, norm) in enumerate(cleaned):
            # Already committed and still lingering in the scrollback → ignore it (this is
            # the duplicate-commit guard: don't re-spawn/re-commit a finalized line that
            # Zoom simply keeps showing). It will leave _committed_visible once it scrolls off.
            if norm in self._committed_visible:
                continue
            line = self._match(body, norm, speaker, now)
            present_keys.add(line.key)
            # anything earlier in display order that is ABOVE a newer line is done growing
            for other in self._active:
                if other.key != line.key and not other.seen_below:
                    # a line that appears before this one in the box and isn't this one
                    pass
        # mark lines that now have something below them (newer key) as done-growing
        self._mark_seen_below()
        # finalize lines that vanished from the visible box (scrolled off)
        self._finalize_absent(present_keys, now)
        # publish the current bottom (growing) line as a live partial
        self._emit_partial()
        # timeout / sentence-complete finalization
        self._tick_timeouts(now)

    def push_final(self, text, speaker=None):
        """Feed an already-final, non-overlapping utterance (e.g. a Whisper chunk).
        Skips positional tracking and goes straight to dedup + finalize."""
        sp, body = self._prepare(text if speaker is None else f"{speaker}: {text}")
        if body is None:
            return
        norm = _norm_meeting(body)
        if not norm:
            return
        now = time.time()
        self._commit(sp or self._current_speaker, body, norm, now, now)

    def flush(self):
        """Finalize everything still active (call on stop)."""
        now = time.time()
        for line in list(self._active):
            self._finalize(line, now)
        self._active.clear()

    # ── internals ──────────────────────────────────────────────────────────--
    def _prepare(self, raw):
        """Clean + speaker-split + quality-gate one raw line. Returns (speaker, body)
        or (None, None) to drop."""
        text = _clean_caption((raw or "").strip())
        if not text:
            return None, None
        speaker, body = _split_speaker(text)
        if speaker:
            speaker = self._canonical_speaker(speaker)
            self._current_speaker = speaker
        else:
            speaker = None
        body = body.strip()
        if len(body) < 2:
            return None, None
        if _is_caption_noise(body) or not watch._ocr_quality_ok(body):
            return None, None
        return speaker, body

    def _canonical_speaker(self, name):
        """Resolve a detected speaker name to a canonical spelling. First snap it to the
        real calendar ROSTER (so 'Nick D'/'Kenjian'/OCR noise → the true attendee name),
        then fuzzy-merge against names already seen this session."""
        # 1) snap to the calendar roster — try first-name and fuzzy full-name match
        if self.roster:
            nl = name.lower().strip()
            best, best_score = None, 0.0
            for real in self.roster:
                rl = real.lower()
                # exact first-name match (Zoom often shows just the first name)
                if nl == rl.split()[0] or rl.startswith(nl + " ") or nl == rl:
                    best, best_score = real, 1.0
                    break
                s = _similar(nl, rl)
                if s > best_score:
                    best, best_score = real, s
            if best is not None and best_score >= 0.6:
                self._speakers[best] = self._speakers.get(best, 0) + 1
                return best
        for canon in self._speakers:
            if _similar(name.lower(), canon.lower()) >= 0.9:
                # keep the longer/more-complete spelling
                if len(name) > len(canon):
                    self._speakers[name] = self._speakers.pop(canon, 0) + 1
                    return name
                self._speakers[canon] += 1
                return canon
        self._speakers[name] = 1
        return name

    def _match(self, body, norm, speaker, now):
        """Match a frame line to an existing ActiveLine (growth / jitter) or create one."""
        best = None
        best_score = 0.0
        for line in self._active:
            if line.finalized:
                continue
            # (a) growth: one norm is a prefix/extension of the other
            if norm == line.norm or norm.startswith(line.norm) or line.norm.startswith(norm):
                best, best_score = line, 1.0
                break
            # (b) jitter: high fuzzy similarity
            s = _similar(norm, line.norm)
            if s > best_score:
                best, best_score = line, s
        if best is not None and best_score >= self.grow_sim:
            # update to the LONGER variant; growth resets stability, jitter doesn't
            if len(body) > len(best.text):
                if norm != best.norm:
                    best.last_change_ts = now
                    best.frames_stable = 0
                best.text = body
                best.norm = norm
            else:
                best.frames_stable += 1
            # Speaker is LOCKED at first sighting: only backfill if we never knew it.
            # Never overwrite a known speaker with the current active speaker — Zoom's
            # scrollback keeps a line visible long after it was said, so the active
            # speaker has usually moved on by the time we re-match it (the drift bug).
            if speaker and not best.speaker:
                best.speaker = speaker
            return best
        # new line
        line = _ActiveLine(self._next_key, body, norm,
                           speaker or self._current_speaker, now)
        self._next_key += 1
        self._active.append(line)
        return line

    def _mark_seen_below(self):
        """A line that has any higher-key (newer) line after it in _active is no longer
        the bottom growing line → mark done-growing."""
        if len(self._active) < 2:
            return
        max_key = max(l.key for l in self._active)
        for line in self._active:
            if line.key < max_key:
                line.seen_below = True

    def _finalize_absent(self, present_keys, now):
        """Finalize active lines that are no longer visible (scrolled off the box)."""
        for line in list(self._active):
            if line.key not in present_keys:
                self._finalize(line, now)

    def _tick_timeouts(self, now):
        """Finalize lines that are done-growing/stable or end a sentence. A very short
        fragment (1-2 words) is almost always still growing, so it is NOT finalized by
        the timeout/done-growing triggers — only by scroll-off (it left the box) or by
        ending in sentence punctuation. This avoids committing partials like 'Raj: I'
        before they become the full utterance."""
        for line in list(self._active):
            if line.finalized:
                continue
            word_count = len(line.text.split())
            done_growing = line.seen_below and line.frames_stable >= 1
            stable = line.frames_stable >= self.stable_frames
            sentence = (line.text.rstrip().endswith((".", "?", "!"))
                        and line.frames_stable >= 2)
            if word_count < 3 and not sentence:
                continue  # too short to be a finished utterance; wait for it to grow
            if done_growing or stable or sentence:
                self._finalize(line, now)

    def _emit_partial(self):
        """Publish the current bottom (growing) line as a live partial segment."""
        if not self._active:
            return
        line = max(self._active, key=lambda l: l.key)
        if line.finalized:
            return
        self.on_segment({
            "seq": self._next_seq,         # tentative seq (the slot the final will take)
            "speaker": line.speaker,
            "text": line.text,
            "norm": line.norm,
            "t_start_ms": int((line.first_ts - self.started_at) * 1000),
            "t_end_ms": int((time.time() - self.started_at) * 1000),
            "is_partial": 1,
            "result_id": f"k{line.key}",
        })

    def _finalize(self, line, now):
        if line.finalized:
            return
        line.finalized = True
        if line in self._active:
            self._active.remove(line)
        self._commit(line.speaker, line.text, line.norm, line.first_ts, now,
                     result_id=f"k{line.key}")

    def _commit(self, speaker, text, norm, start_ts, end_ts, result_id=None):
        """Cross-utterance dedup, then deliver as a finalized segment (or replace a
        previously-finalized superset in place).

        Zoom's caption panel is a tall full-scrollback: a finalized line stays VISIBLE for
        a long time (often 60s+) and, once it scrolls within the panel, can reappear as a
        fresh 'active line' after the time-windowed tail forgot it — producing duplicates.
        So substantial lines (4+ words) are also deduped against a SESSION-WIDE seen-set,
        not just the sliding window. Short acks ('Okay, yeah') keep only the windowed check
        so a genuine later repeat by another speaker still counts."""
        words = norm.split()
        substantial = len(words) >= 4
        if substantial and norm in self._seen_norms:
            return  # already committed this line earlier in the session
        # exact / fuzzy / containment dedup against recent tail
        for entry in reversed(self._tail):
            if now_gap(end_ts, entry["ts"]) > self.dup_window_s:
                break
            if norm == entry["norm"]:
                return  # exact repeat
            if norm in entry["norm"]:
                return  # we already stored a superset
            if entry["norm"] in norm:
                # this is a SUPERSET of a stored line (it grew after we finalized early)
                # → replace that segment in place (idempotent, same seq)
                self._deliver(entry["seq"], speaker, text, norm, start_ts, end_ts,
                              result_id, replace_entry=entry)
                if substantial:
                    self._seen_norms.add(norm)
                return
            if _similar(norm, entry["norm"]) >= self.dup_sim:
                return
        # genuinely new utterance
        seq = self._next_seq
        self._next_seq += 1
        self._deliver(seq, speaker, text, norm, start_ts, end_ts, result_id)
        if substantial:
            self._seen_norms.add(norm)

    def _deliver(self, seq, speaker, text, norm, start_ts, end_ts, result_id,
                 replace_entry=None):
        seg = {
            "seq": seq,
            "speaker": speaker,
            "text": text,
            "norm": norm,
            "t_start_ms": int((start_ts - self.started_at) * 1000),
            "t_end_ms": int((end_ts - self.started_at) * 1000),
            "is_partial": 0,
            "result_id": result_id,
        }
        self.on_segment(seg)
        self._last_seg_ts = end_ts
        # remember this committed line so that, while it lingers visibly in Zoom's
        # scrollback, the next polls don't re-spawn and re-commit it (duplicate guard).
        self._committed_visible[norm] = end_ts
        if replace_entry is not None:
            replace_entry["norm"] = norm
            replace_entry["text"] = text
            replace_entry["ts"] = end_ts
        else:
            self._tail.append({"norm": norm, "text": text, "ts": end_ts, "seq": seq})
            del self._tail[:-40]
        # speaker stickiness reset on long silence
        if end_ts - self._last_seg_ts > self.silence_reset_s:
            self._current_speaker = None


def now_gap(end_ts, entry_ts):
    """Seconds between a finalized line's end and a tail entry (both epoch)."""
    return abs(end_ts - entry_ts)
