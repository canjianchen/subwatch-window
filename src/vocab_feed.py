"""Live vocabulary feed — the bridge between the capture loop and the on-screen
vocab overlay.

The watch loop (watch.py) pushes the hard words it finds for each subtitle line
here; the vocab overlay (vocab_overlay.py) reads them and shows them on screen so
you can read the word + its Chinese even after the subtitle has scrolled away.

Stored as a small rolling JSON list at db/live_vocab.json — one entry per hard word
with the sentence it came from and a timestamp, newest last. Written atomically
(tmp + replace) so the overlay never reads a half-written file."""
import json
import os
import threading
import time

import config

PATH = os.path.join(config.DB_DIR, "live_vocab.json")
_MAX = 24  # keep the most recent N words on disk; the overlay shows a shorter tail
_LOCK = threading.Lock()


def _write(words):
    os.makedirs(config.DB_DIR, exist_ok=True)
    tmp = PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump({"words": words[-_MAX:]}, handle, ensure_ascii=False)
    os.replace(tmp, PATH)


def _entries(items, sentence, sentence_cn):
    now = time.time()
    out = []
    for item in items:
        term = (item.get("term") or "").strip()
        if term:
            out.append({"term": term, "cn": (item.get("cn") or "").strip(),
                        "kind": item.get("kind", "word"), "sentence": sentence or "",
                        "sentence_cn": sentence_cn or "", "t": now})
    return out


def push(items, sentence, sentence_cn=None):
    """Append the hard words found for one subtitle line. `items` is a list of dicts
    with at least 'term'; optional 'cn' (Chinese of the term) and 'kind'. Best-effort —
    never raises into the capture loop."""
    if not items:
        return
    try:
        with _LOCK:
            existing = _read().get("words", [])
            existing.extend(_entries(items, sentence, sentence_cn))
            _write(existing)
    except OSError:
        pass


def replace(items, sentence, sentence_cn=None):
    """Atomically replace provisional/final words for one subtitle sentence."""
    try:
        with _LOCK:
            existing = [entry for entry in _read().get("words", [])
                        if entry.get("sentence", "") != (sentence or "")]
            existing.extend(_entries(items, sentence, sentence_cn))
            _write(existing)
    except OSError:
        pass


def _read():
    try:
        with open(PATH, encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, json.JSONDecodeError):
        return {}


def recent(max_sentences=2, max_words=10, max_age=120.0):
    """Return the hard words to show on the overlay, scoped to the most recent
    `max_sentences` DISTINCT subtitle lines — so the panel shows the words for the
    sentence currently on screen (plus optionally the line just before it), not a long
    rolling history. Newest first, de-duplicated by term. `max_age` only clears words
    after a long pause so the panel doesn't hold a stale frame forever."""
    words = _read().get("words", [])
    now = time.time()
    fresh = [w for w in words if now - w.get("t", 0) <= max_age]
    # identify the last N distinct sentences, in the order they arrived
    distinct = []
    for entry in fresh:
        sentence = entry.get("sentence", "")
        if sentence and sentence not in distinct:
            distinct.append(sentence)
    keep = set(distinct[-max_sentences:]) if max_sentences > 0 else set(distinct)
    out, seen_terms = [], set()
    for entry in reversed(fresh):  # newest first
        if entry.get("sentence", "") not in keep:
            continue
        key = entry["term"].lower()
        if key in seen_terms:
            continue
        seen_terms.add(key)
        out.append(entry)
        if len(out) >= max_words:
            break
    return out
