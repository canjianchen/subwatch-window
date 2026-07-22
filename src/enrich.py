"""Optional Codex enrichment: fill definitions + Chinese glosses for captured words.

Best-effort: if Codex login or network is missing,
it skips gracefully so the core capture/review flow never depends on it.

Run:  python3 enrich.py            # enrich all terms missing a definition
"""
import json
import store
import config as _cfg
import codex_ai
import translate


def _use_translation_api():
    """True when translation should come from the cheap API instead of Codex.
    Delegates to hard_phrases_llm.translation_by_api() so the reachability check
    (don't drop the LLM's Chinese when the network endpoints are blocked) is shared."""
    try:
        import hard_phrases_llm
        return hard_phrases_llm.translation_by_api()
    except Exception:  # noqa: BLE001
        return False


def enrich_word(word, context, display_term=None):
    """Return (definition, chinese, phrase, translation) for a word given its context.
    `display_term` is the human term to define (for phrase keys like 'phrase:get a pass'
    pass the matched idiom, not the raw key).

    Codex is asked only for what needs reasoning (idiom detection + English definition +
    the contextual sense). The direct term translation comes from the cheap translation
    API when enabled, so we don't spend LLM tokens on plain translation."""
    target = display_term or word
    api_translate = _use_translation_api()
    translation_line = (
        ""
        if api_translate
        else '  "translation": the DIRECT Chinese translation (简体中文) of the word/phrase '
             'itself, just the term (e.g. for "cops" -> "警察")\n'
    )
    prompt = (
        f"You are helping a Chinese native speaker learn English from movie subtitles.\n"
        f'Target word: "{target}"\n'
        f'Sentence it appeared in: "{context or "(no context)"}"\n\n'
        "First decide: is the target word part of an idiom, phrasal verb, or fixed "
        "expression in this sentence (e.g. \"hammer time\", \"for ... sake\", \"give up\")?\n"
        "If YES, define the WHOLE phrase, not the single word — that is what the learner "
        "should study.\n"
        "If NO, just define the single word.\n\n"
        "Reply with ONLY a compact JSON object, no markdown, with keys:\n"
        '  "phrase": the idiom/expression if one applies, else null\n'
        '  "definition": a short plain-English meaning of the phrase (if any) or word (<=18 words)\n'
        + translation_line +
        '  "chinese": the Chinese meaning of the word/phrase AS USED IN THIS SENTENCE '
        "(简体中文, captures the contextual sense)\n"
    )
    try:
        text = codex_ai.ask(prompt, effort="medium", timeout=180)
    except Exception:  # noqa: BLE001 — best-effort; degrade to no enrichment
        return None, None, None, None
    # tolerate stray fencing
    if text.startswith("```"):
        text = text.strip("`")
        text = text[text.find("{"):]
    raw = text[text.find("{"): text.rfind("}") + 1]
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        try:
            # tolerate trailing commas before } or ] which the model sometimes emits
            import re as _re
            data = json.loads(_re.sub(r",\s*([}\]])", r"\1", raw))
        except json.JSONDecodeError:
            return None, None, None, None
    phrase = data.get("phrase")
    if isinstance(phrase, str) and not phrase.strip():
        phrase = None
    translation = data.get("translation")
    if api_translate:
        # translate the phrase if one applies, else the plain term — matching how the
        # card is studied. Falls back to the LLM's contextual gloss if the API fails.
        translation = translate.translate_to_zh(phrase or target) or data.get("chinese")
    return data.get("definition"), data.get("chinese"), phrase, translation


def enrich_all(limit=None):
    store.init_db()
    # backfill anything missing a definition OR a direct translation, so every
    # entry ends up with full detail regardless of how it was captured.
    terms = [t for t in store.all_terms()
             if not t.get("definition") or not t.get("translation")]
    if limit:
        terms = terms[:limit]
    if not terms:
        print("Nothing to enrich — all terms already have definitions.")
        return

    if not _cfg.codex_available(refresh=True):
        print("Codex CLI is unavailable or not logged in. Skipping enrichment.")
        return

    done = 0
    for term in terms:
        try:
            # for phrase rows, define the matched idiom, not the 'phrase:...' key
            display = term.get("matched") if term.get("kind") == "phrase" else None
            definition, chinese, phrase, translation = enrich_word(
                term["word"], term.get("context"), display_term=display)
            # NEVER overwrite the captured Chinese SUBTITLE line; only fill the
            # contextual `chinese` gloss and the direct `translation`.
            store.set_enrichment(term["word"], definition=definition,
                                 chinese=chinese, phrase=phrase, translation=translation)
            label = display or term["word"]
            print(f"  ✓ {label}: {definition}")
            done += 1
        except Exception as exc:  # noqa: BLE001 — skip a failing word, keep going
            print(f"  ✗ {term['word']}: {exc}")
    print(f"\nEnriched {done}/{len(terms)} terms.")


if __name__ == "__main__":
    enrich_all()
