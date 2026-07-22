"""Codex-based hard-expression extractor.

A fixed idiom list (phrases.py) can't anticipate every hard expression — e.g.
"devoid of humor", "a far cry from", "par for the course". This module asks an
LLM to read a subtitle sentence and pull out the expressions/slang/collocations
that would challenge an intermediate–advanced learner, returning the whole
sentence as context plus the flagged expression.

Best-effort: if Codex is unavailable it returns [], so the capture loop still
works offline with the curated list. Used by the watch loop only when
`use_llm_phrases` is enabled in config (off by default to avoid per-frame cost).
"""
import json

import codex_ai
import config


def reset_client():
    """Refresh the cached Codex login status."""
    codex_ai.clear_availability_cache()


def extract(sentence):
    """Return a list of {"expression", "definition", "chinese"} for hard expressions
    in the sentence (idioms, slang, phrasal verbs, tricky collocations). [] on any error."""
    if not sentence or not sentence.strip():
        return []
    prompt = (
        "You help a Chinese learner study English from movie subtitles.\n"
        f'Subtitle line: "{sentence}"\n\n'
        "Identify ONLY multi-word expressions in this line that an intermediate–advanced "
        "learner would find hard: idioms, slang, phrasal verbs, or non-literal "
        "collocations (e.g. \"devoid of humor\", \"help yourself\", \"a far cry from\"). "
        "Ignore ordinary literal phrases and single common words.\n"
        "Reply with ONLY a compact JSON array (no markdown). Each item:\n"
        '  {"expression": "...", "definition": "<=15 word plain-English meaning", '
        '"chinese": "简体中文"}\n'
        "If there are none, reply []."
    )
    try:
        text = _invoke_raw(prompt, max_tokens=350)
        if text.startswith("```"):
            text = text.strip("`")
            text = text[text.find("["):]
        start, end = text.find("["), text.rfind("]")
        if start == -1 or end == -1:
            return []
        items = json.loads(text[start:end + 1])
        return [it for it in items if isinstance(it, dict) and it.get("expression")]
    except Exception:  # noqa: BLE001 — best-effort; never break the capture loop
        return []


def _invoke_raw(prompt, max_tokens=600, retries=2):
    last = None
    for attempt in range(retries + 1):
        try:
            # Live subtitle grading favors latency, so use low reasoning effort.
            return codex_ai.ask(prompt, effort="low", timeout=180)
        except Exception as exc:  # noqa: BLE001 — transient; brief backoff
            last = exc
            if attempt < retries:
                import time
                time.sleep(0.8 * (attempt + 1))
    raise last


def _invoke(prompt, max_tokens=500):
    text = _invoke_raw(prompt, max_tokens)
    if text.startswith("```"):
        text = text.strip("`")
        text = text[text.find("["):]
    start, end = text.find("["), text.rfind("]")
    if start == -1 or end == -1:
        return []
    return json.loads(text[start:end + 1])


def _profile_hint():
    """Calibration text from the learner's placement test + marks (empty if untested)."""
    try:
        import profile
        return profile.calibration_hint()
    except Exception:  # noqa: BLE001 — profile is optional
        return ""


def _extract_json_object(text):
    if text.startswith("```"):
        text = text.strip("`")
        text = text[text.find("{"):]
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end == -1:
        return {}
    return json.loads(text[start:end + 1])


def extract_hard(sentence, level="advanced"):
    """Read a subtitle line and return the genuinely hard/worth-learning items for a
    Chinese learner — BOTH single words and multi-word expressions — judged by an LLM
    rather than raw word frequency (frequency can't tell "difficulty").

    `level` tunes strictness: "intermediate" keeps more; "advanced" keeps only
    challenging vocab + non-literal idioms (skips everyday words an advanced speaker
    already knows). Returns a list of dicts:
      {"term": "...", "kind": "word"|"phrase", "definition": "...",
       "translation": "简体中文 of the term", "chinese": "contextual meaning"}
    [] on any error so the capture loop never breaks.
    """
    if not sentence or not sentence.strip():
        return {"items": [], "sentence_cn": ""}
    body, min_score = _build_grade_prompt(sentence, level)
    prompt = body + (
        "Give a clean Chinese translation of the whole line regardless.\n"
        "Reply with ONLY a compact JSON object, no markdown:\n"
        '{"sentence_cn": "整句的简体中文翻译",\n'
        ' "items": [{"term": "...", "kind": "word" or "phrase", "score": <int 1-10>, '
        '"definition": "<=15 word plain English meaning", '
        '"translation": "简体中文 of the term itself", '
        '"chinese": "its meaning in this sentence (简体中文)"}]}'
    )
    try:
        data = _extract_json_object(_invoke_raw(prompt))
        out = []
        for it in data.get("items", []):
            if not (isinstance(it, dict) and it.get("term")):
                continue
            try:
                score = int(it.get("score", 0))
            except (TypeError, ValueError):
                score = 0
            if score < min_score:
                continue
            it.setdefault("kind", "word")
            out.append(it)
        return {"items": out, "sentence_cn": (data.get("sentence_cn") or "").strip()}
    except Exception:  # noqa: BLE001 — best-effort; signal failure so caller falls back
        return {"items": [], "sentence_cn": None, "_failed": True}


def _build_grade_prompt(sentence, level):
    """Build the shared difficulty-grading prompt body (everything up to the output-format
    instruction) plus the per-level min_score. Used by both the batch (extract_hard) and the
    streaming (extract_hard_stream) paths so their judgment stays identical."""
    # minimum difficulty score (1-10) to keep an item, per level. `breadth` (config
    # capture_breadth, default 0) lowers the bar to catch MORE words when the user
    # wants broader coverage — each point drops the threshold by 1 (floor 3).
    min_score = _minimum_score(level)
    learner = {
        "beginner": "a BEGINNER English learner (around CEFR A2-B1, a Chinese speaker). "
                    "They know only the most basic everyday words; capture generously — "
                    "common words, simple idioms, phrasal verbs, and everyday concrete "
                    "nouns/verbs are all worth learning for them",
        "intermediate": "an INTERMEDIATE English learner (around CEFR B1-B2, a Chinese "
                        "speaker). They know everyday vocabulary but NOT advanced words, "
                        "most idioms, slang, or figurative phrases",
        "advanced": "an ADVANCED English learner (CEFR C1, ~10k words). They know common "
                    "vocabulary and many idioms but not rare/literary words",
        "expert": "a near-native C2 learner who only needs rare, literary, or specialized terms",
    }.get(level, "an intermediate English learner")
    prompt_body = (
        f"You help {learner}, studying from a movie subtitle.\n\n"
        f'Subtitle line (may contain minor OCR errors): "{sentence}"\n\n'
        "Score every candidate word/expression 1-10 for how hard it is FOR THIS LEARNER "
        "(higher = harder for them specifically):\n"
        "  1-3 = truly basic words everyone knows (the, go, happy, eat, friend, water).\n"
        "  4-5 = common everyday words/phrases a B1 learner already has. These are NOT worth "
        "capturing — single-word examples that were wrongly captured before: worried, decide, "
        "borrow, convinced, predictable, intersect, crane, eliminate, tolerate, ruin, mess, "
        "messy, odd, behave, emotion, emotions, hurts, reopened, neural, syllable. And "
        "literal multi-word examples wrongly captured before: cost me, true potential, "
        "true purpose, serve a purpose, get back to, way of knowing, mixed with, "
        "act of rebellion, regardless of, completely honest, out to get you, take over, "
        "getting a sense, low low price, limited edition, brain scans, mental health, "
        "mental illness, subatomic particles, snow globes.\n"
        "  HARD RULE — literal/compositional phrases: a multi-word phrase made ONLY of words "
        "the learner already knows, whose meaning is the obvious sum of its parts (verb + "
        "common object, adjective + common noun, common preposition phrase), scores 4-5 and "
        "MUST be EXCLUDED — even if the topic sounds technical or the phrase is long. "
        "'serve a purpose', 'get back to', 'true purpose', 'mixed with', 'mental health', "
        "'brain scans' are all literal and EXCLUDED. ONLY capture a multi-word phrase if its "
        "meaning is NON-literal — i.e. it is a genuine idiom, slang, or phrasal verb whose "
        "meaning you could NOT guess from the individual words (e.g. 'cut to the chase', "
        "'out of the blue', 'bring around', 'call dibs'). Score the WHOLE phrase by whether "
        "its MEANING is non-obvious, NOT by its length, topic, or its rarest single word.\n"
        "  6-7 = mid/upper vocabulary, idioms, phrasal verbs, slang an intermediate "
        "learner likely does NOT know yet (reluctant, candid, hypocrite, cut to the chase, "
        "call dibs on, on your game, bring you around, a big get). ALSO score here any "
        "concrete everyday NOUN/verb that a Chinese intermediate learner often hasn't "
        "learned even though natives find it basic (lid, jar, sink, leash, curb, bucket, "
        "shrug, squint, smirk) — judge by 'would a B1-B2 Chinese learner know this word', "
        "not by how basic it feels to a native.\n"
        "  8-10 = advanced/rare/literary words & figurative idioms (ragtag, gander, "
        "flabbergasted, devoid of, on tenterhooks, snake oil salesman).\n\n"
        f"Return ALL items scoring >= {min_score}. Capture generously at this threshold — it's "
        "fine to return several items per line if they're genuinely above the bar. But still "
        "EXCLUDE words clearly below it and OCR garbage.\n"
        "IMPORTANT: the line is OCR'd and may contain MISREADINGS — if a 'word' is actually a "
        "garbled/misspelled version of a common word in context (e.g. 'howe'→how, 'doina'→doing, "
        "'tum'→turn, 'dusters'→clusters, 'top it'→stop it, 'partans'→spartans, 'fice/cany'→noise), "
        "EXCLUDE it; never treat an OCR error as rare vocabulary. A short token that is NOT a "
        "standard dictionary word (e.g. partans, fice, cany, dodds, beside-as-fragment) is OCR "
        "garbage — exclude it.\n"
        "If the line is a single isolated word or tiny fragment (e.g. 'moke?', 'tum') with "
        "no real context, assume it's an OCR error and return NO items.\n"
        "PROPER NAMES: never capture names of people, places, companies, TV shows, ranks, or "
        "titles — they are not vocabulary. This includes MULTI-WORD names (e.g. 'Ivan the "
        "Terrible', 'People's Court', 'Petty Officer', 'HUD data', 'Madrigal'). If unsure "
        "whether a capitalized term is a name, exclude it.\n"
        + _profile_hint()
    )
    return prompt_body, min_score


def _minimum_score(level):
    try:
        breadth = int(config.load_config().get("capture_breadth", 0))
    except Exception:  # noqa: BLE001
        breadth = 0
    base = {"beginner": 4, "intermediate": 6, "advanced": 8, "expert": 9}.get(level, 8)
    return max(3, base - breadth)


def extract_hard_stream(sentence, level="advanced"):
    """Streaming variant of extract_hard: yields each hard item the MOMENT the model
    finishes writing it, instead of waiting for the whole response. Same Codex judgment
    and same full detail (definition + translations) — just delivered progressively so
    the first word lands in ~3s instead of ~6.5s.

    Output format is JSONL — one self-contained JSON object per line — so each line is a
    complete, usable item as soon as its newline arrives. The first line is the whole-
    sentence translation: {"sentence_cn": "..."}; every later line is one term.

    Yields dicts. A term dict has a "term" key; the sentence dict has "sentence_cn".
    On a hard failure it yields a single {"_failed": True}. The caller applies the same
    score-threshold + frequency-floor filtering it would for extract_hard."""
    if not sentence or not sentence.strip():
        return
    min_score = _minimum_score(level)
    learner = {"beginner": "A2-B1", "intermediate": "B1-B2",
               "advanced": "C1", "expert": "C2"}.get(level, "B1-B2")
    # Live subtitles favor latency. Preserve the essential quality guards while
    # avoiding the much longer calibration prompt used for offline/batch grading.
    prompt = (
        f'English movie subtitle (OCR may have minor errors): "{sentence}"\n'
        f"For a Chinese {learner} learner, return every genuinely useful word, idiom, slang, "
        f"or non-literal phrasal verb scoring at least {min_score}/10. Include upper-intermediate "
        "and advanced vocabulary generously. Exclude basic words, obvious literal phrases, "
        "proper names/titles/brands, and misspelled OCR fragments.\n"
        "Reply as JSONL only. First translate the whole sentence, then output each term with "
        "a short English definition and Simplified Chinese:\n"
        '{"sentence_cn": "整句的简体中文翻译"}\n'
        '{"term": "...", "kind": "word"|"phrase", "score": <int 1-10>, '
        '"definition": "<=15 word plain English meaning", '
        '"translation": "简体中文 of the term itself", '
        '"chinese": "its meaning in this sentence (简体中文)"}\n'
        "If nothing qualifies, output only sentence_cn. No markdown or commentary."
    )
    try:
        for obj in _stream_jsonl(prompt):
            if "sentence_cn" in obj and "term" not in obj:
                yield {"sentence_cn": (obj.get("sentence_cn") or "").strip()}
                continue
            if not obj.get("term"):
                continue
            try:
                score = int(obj.get("score", 0))
            except (TypeError, ValueError):
                score = 0
            if score < min_score:
                continue
            obj.setdefault("kind", "word")
            yield obj
    except Exception:  # noqa: BLE001 — best-effort; signal failure so caller can skip the line
        yield {"_failed": True}


def _stream_jsonl(prompt, max_tokens=600):
    """Yield JSONL objects from Codex's final response.

    `codex exec` deliberately prints only the final agent message to stdout, so this
    compatibility generator yields completed lines after that message arrives.
    """
    text = _invoke_raw(prompt, max_tokens=max_tokens)
    for line in text.splitlines():
        obj = _parse_jsonl_line(line)
        if obj is not None:
            yield obj


def _parse_jsonl_line(line):
    """Parse one JSONL line into a dict, tolerating markdown fences / stray prose. None if
    the line isn't a usable JSON object."""
    line = line.strip().strip("`").strip()
    start, end = line.find("{"), line.rfind("}")
    if start == -1 or end == -1 or end < start:
        return None
    try:
        obj = json.loads(line[start:end + 1])
        return obj if isinstance(obj, dict) else None
    except (ValueError, json.JSONDecodeError):
        return None
