"""Detect slang / idiom phrases in a subtitle line.

Single-word capture (detector.py) misses multi-word expressions like
"help yourself" or "knock it off" because they're made of common words. This
module scans the line against a curated idiom list and returns any matches.

Per the user's preference, a phrase hit is recorded as the WHOLE English
sentence (the full subtitle line), with the matched idiom flagged separately —
so the saved item keeps full context, not just the substring.
"""
import os
import re

import config

_IDIOMS_PATH = os.path.join(config.DATA, "idioms.txt")


def _load_idioms():
    """Return a list of (phrase, compiled_regex) sorted longest-first.

    Longest-first so 'hang in there' matches before 'hang on' when both could.
    Each regex matches the phrase as whole words, case-insensitively, allowing
    flexible whitespace between tokens."""
    phrases = []
    try:
        with open(_IDIOMS_PATH, "r", encoding="utf-8") as handle:
            for line in handle:
                phrase = line.strip()
                if not phrase or phrase.startswith("#"):
                    continue
                phrases.append(phrase.lower())
    except OSError:
        return []

    phrases = sorted(set(phrases), key=len, reverse=True)
    compiled = []
    for phrase in phrases:
        # build a whole-phrase regex: word boundaries at ends, flexible spaces inside
        tokens = [re.escape(tok) for tok in phrase.split()]
        pattern = r"\b" + r"\s+".join(tokens) + r"\b"
        compiled.append((phrase, re.compile(pattern, re.IGNORECASE)))
    return compiled


_IDIOMS = _load_idioms()


def find_phrases(text):
    """Return a list of idiom phrases found in `text` (canonical lowercase forms).

    Overlapping matches are de-duplicated: once a span is consumed by a longer
    phrase, shorter phrases inside it are skipped. The caller stores the full
    sentence as context — these are just the flags for what made it interesting."""
    if not text or not _IDIOMS:
        return []
    consumed = []  # list of (start, end) spans already claimed
    found = []
    for phrase, regex in _IDIOMS:  # already longest-first
        match = regex.search(text)
        if not match:
            continue
        # Guard short ambiguous idioms ("i'm out"/"in"/"all in") against literal
        # uses: skip if the next word turns it into a literal phrase ("out OF the
        # house", "in THE car"). Only applies to these few risky entries.
        if phrase in _CONTEXT_GUARDED:
            tail = text[match.end():].lstrip().lower()
            if tail.startswith(("of ", "in ", "the ", "on ", "at ", "to ", "for ")):
                continue
        span = match.span()
        if any(span[0] < e and s < span[1] for s, e in consumed):
            continue  # overlaps a longer phrase already taken
        consumed.append(span)
        found.append(phrase)
    return found


# Idioms whose words also occur in literal phrases — only count them when not
# immediately followed by a preposition/article that makes them literal.
_CONTEXT_GUARDED = {"i'm out", "i'm in", "all in", "all out", "on me", "for real"}
