"""Decide which English words from a subtitle line are worth learning."""
import os
import re
import unicodedata

import config

_WORD_RE = re.compile(r"[A-Za-z][A-Za-z'\-]+")

# Contractions whose apostrophe OCR commonly drops ("they're"->"theyre"). These
# look like rare words but are just punctuation loss — never vocabulary.
# Number words — captured as "rare" by frequency but not worth studying.
_NUMBER_WORDS = {
    "zero", "one", "two", "three", "four", "five", "six", "seven", "eight",
    "nine", "ten", "eleven", "twelve", "thirteen", "fourteen", "fifteen",
    "sixteen", "seventeen", "eighteen", "nineteen", "twenty", "thirty", "forty",
    "fifty", "sixty", "seventy", "eighty", "ninety", "hundred", "thousand",
    "million", "billion", "dozen",
}

_TOO_COMMON = {"cannot", "into", "onto", "upon", "okay", "yeah", "gonna", "wanna"}

# OCR-garbage tokens that the macOS /usr/share/dict/words file wrongly accepts as
# real words (obscure/archaic dialect entries) or that match via the naive suffix
# stemmer. These keep slipping through is_real_word() and getting captured / pushed
# into the learner profile, so reject them explicitly. Confirmed from logs/critique.jsonl
# verdicts (ocr_garbage). Add new false-positives here as the self-critic finds them.
_OCR_GARBAGE = {
    "partans", "partan", "fice", "cany", "dusters", "dodds",
}

_DEAPOSTROPHED = {
    "theyre", "youre", "weve", "theyve", "youve", "wont", "cant", "dont",
    "didnt", "doesnt", "isnt", "arent", "wasnt", "werent", "hasnt", "havent",
    "shouldnt", "wouldnt", "couldnt", "im", "ive", "ill", "id", "hes", "shes",
    "thats", "whats", "lets", "gonna", "wanna", "gotta", "thave", "couldive", "wouldive", "shouldive", "mustive", "couldve", "wouldve", "shouldve",
}

# Tiny suffix-stripper so "running"/"runs" map toward "run" for frequency lookup.
# Not a real lemmatizer, just enough to avoid missing common inflections.
_SUFFIXES = ("ing", "ed", "es", "s", "'s", "ly", "ment", "ness")


def _load_ranks():
    ranks = {}
    try:
        with open(config.COMMON_WORDS, "r", encoding="utf-8") as handle:
            for index, line in enumerate(handle):
                word = line.strip().lower()
                if word:
                    ranks[word] = index + 1
    except OSError:
        pass
    return ranks


_RANKS = _load_ranks()

# English dictionary — used to reject OCR garbage, clipped fragments, and proper
# nouns. A captured word (or a naive base form) must appear here. We try the macOS
# system dictionary first, then a bundled copy in data/ (so Windows works too).
_DICT_CANDIDATES = ["/usr/share/dict/words", os.path.join(config.DATA, "words.txt")]


def _load_dictionary():
    """Return (all_words_lower, common_words_lower).

    `common_words_lower` holds only entries that appear LOWERCASE in the
    dictionary — i.e. ordinary words. Proper nouns (Rachel, Kevin, Hollywood) are
    stored capitalized, so they land in `all` but NOT in `common`. This lets us
    accept a capitalized on-screen word like "Scrabble" (lowercase 'scrabble' is a
    common entry) while rejecting names like "Burton"/"Rachel"."""
    all_words = set()
    common = set()
    for path in _DICT_CANDIDATES:
        try:
            with open(path, "r", encoding="utf-8", errors="ignore") as handle:
                for line in handle:
                    token = line.strip()
                    if not token:
                        continue
                    all_words.add(token.lower())
                    if token[0].islower():
                        common.add(token)
            break  # first readable dictionary wins
        except OSError:
            continue
    return all_words, common


_DICT, _COMMON_DICT = _load_dictionary()


def has_chinese(text):
    return any("CJK" in unicodedata.name(ch, "") for ch in text)


def is_real_word(word):
    """True if the word (or a naive de-inflected form) is in the English dictionary.
    If the dictionary is unavailable, fall back to accepting everything."""
    if word.lower() in _OCR_GARBAGE:  # known OCR junk the dictionary wrongly accepts
        return False
    if not _DICT:
        return True
    if "-" in word:  # hyphenated compounds: accept if every part is real
        return all(is_real_word(part) for part in word.split("-") if part)
    return any(form in _DICT for form in _base_forms(word))


def is_common_word(word):
    """True if the (lowercase) word is an ordinary dictionary word, not a proper noun.
    Used to decide whether a Capitalized on-screen token is real ('Scrabble') or a
    name ('Burton'). If the dictionary is unavailable, fall back to accepting."""
    if not _COMMON_DICT:
        return True
    lower = word.lower()
    return any(form in _COMMON_DICT for form in _base_forms(lower))


def _base_forms(word):
    """Yield the word plus a few naive de-inflected forms for rank lookup."""
    yield word
    for suffix in _SUFFIXES:
        if word.endswith(suffix) and len(word) - len(suffix) >= 3:
            stem = word[: -len(suffix)]
            yield stem
            # handle doubled consonant ("running" -> "run") and dropped 'e'
            if len(stem) >= 2 and stem[-1] == stem[-2]:
                yield stem[:-1]
            yield stem + "e"


def rarity_rank(word):
    """Best (lowest) frequency rank across the word and its naive base forms.
    Returns None if the word never appears in the common list (= very rare)."""
    best = None
    for form in _base_forms(word):
        rank = _RANKS.get(form)
        if rank is not None and (best is None or rank < best):
            best = rank
    return best


def hard_words(text, cfg=None):
    """Return a list of (word, rarity_rank) for words deemed hard in this line.
    rarity_rank is None for words absent from the common list (treated as hardest).

    A word is captured only if it survives every filter: not a contraction, long
    enough, a REAL English word (rejects OCR garbage, clipped fragments like
    'troph'/'gener', and proper nouns), and not clipped at the line's edge."""
    cfg = cfg or config.load_config()
    threshold = cfg["rarity_threshold"]
    min_len = cfg["min_word_length"]

    matches = list(_WORD_RE.finditer(text))
    seen = set()
    results = []
    for index, match in enumerate(matches):
        raw = match.group(0)
        # Skip contractions / possessives ("it's", "fuck's") — not vocabulary to learn,
        # and they aren't in the frequency list so they'd look falsely "rare".
        if "'" in raw:
            continue
        # Proper nouns: a capitalized token mid-line is a name/brand UNLESS its
        # lowercase form is an ordinary dictionary word. So "Scrabble" survives
        # (scrabble is common) but "Burton"/"Rachel"/"Kevin" are dropped.
        if index > 0 and raw[0].isupper() and not is_common_word(raw):
            continue
        word = raw.lower().strip("'-")
        if len(word) < min_len or word in seen:
            continue
        if word in _DEAPOSTROPHED:  # OCR-dropped apostrophe ("theyre" = "they're")
            continue
        if word in _NUMBER_WORDS:   # "fifty", "hundred" — rare by frequency but not vocab
            continue
        if word in _TOO_COMMON:     # function words that slip through as rank-None
            continue
        seen.add(word)
        rank = rarity_rank(word)
        # Edge tokens (first/last in the OCR'd line) are frequently clipped by the
        # capture region ("...troph", "ljust...", "...mone"). A clipped fragment has
        # no frequency rank, so at the edges we only trust KNOWN common words.
        at_edge = (index == 0 and match.start() == 0) or \
                  (index == len(matches) - 1 and match.end() == len(text))
        if at_edge and rank is None:
            continue
        # Must be a genuine English word (rejects OCR garbage like "heylhey").
        if not is_real_word(word):
            continue
        # Hard = not in the common list at all, OR ranked rarer than the threshold.
        if rank is None or rank > threshold:
            results.append((word, rank))
    return results
