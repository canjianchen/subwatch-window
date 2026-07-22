"""Self-evaluation harness — runs in the background, grades recent captures, and
logs problems so the system can be improved without manual inspection.

Every INTERVAL seconds it:
  1. pulls captures added since the last pass
  2. asks the LLM to grade each: is it OCR garbage? a proper noun/name? right
     difficulty for the learner's profile? is the translation correct?
  3. auto-removes clear garbage/duplicates from the deck
  4. appends a structured critique to logs/critique.jsonl and a human summary to
     logs/critique_summary.log

The summary is what to read between cycles to decide code-level improvements.
Run:  python3 self_critic.py        (loops)
      python3 self_critic.py --once (single pass)
"""
import json
import os
import sys
import time

import config
import codex_ai
import store
import profile as prof

LOG_DIR = config.LOGS_DIR
CRITIQUE_JSONL = os.path.join(LOG_DIR, "critique.jsonl")
SUMMARY_LOG = os.path.join(LOG_DIR, "critique_summary.log")
STATE_PATH = os.path.join(LOG_DIR, "critic_state.json")
INTERVAL = 90


def _load_state():
    try:
        with open(STATE_PATH) as handle:
            return json.load(handle)
    except (OSError, json.JSONDecodeError):
        return {"last_ts": 0.0, "passes": 0}


def _save_state(state):
    os.makedirs(LOG_DIR, exist_ok=True)
    with open(STATE_PATH, "w") as handle:
        json.dump(state, handle)


def _grade_batch(terms, level, known, unknown):
    """Ask the LLM to grade a batch of captured terms. Returns list of verdicts."""
    import json as _json
    items = [{"term": t.get("matched") or t["word"].replace("phrase:", ""),
              "context": t.get("context", ""),
              "translation": t.get("translation", "")} for t in terms]
    prompt = (
        f"You audit an English-learning capture tool for a {level}-level Chinese learner.\n"
        f"They ALREADY KNOW (don't want captured): {', '.join(known[-30:]) or '(none)'}\n"
        f"They DON'T know (good to capture): {', '.join(unknown[-30:]) or '(none)'}\n\n"
        "For each captured item below, judge it. Output ONLY a JSON array, one object per item:\n"
        '  {"term","verdict":"good"|"too_easy"|"ocr_garbage"|"proper_name"|"bad_translation",'
        '"fix":"<short note>"}\n'
        "  good = genuinely worth studying at this level\n"
        "  too_easy = the learner almost certainly knows it\n"
        "  ocr_garbage = not a real word/expression (OCR error)\n"
        "  proper_name = a person/brand name, not vocabulary\n"
        "  bad_translation = the Chinese translation is wrong/garbled\n\n"
        "Items:\n" + _json.dumps(items, ensure_ascii=False)
    )
    text = codex_ai.ask(prompt, effort="medium", timeout=240)
    if text.startswith("```"):
        text = text.strip("`"); text = text[text.find("["):]
    start, end = text.find("["), text.rfind("]")
    return json.loads(text[start:end + 1]) if start >= 0 else []


def run_pass():
    store.init_db()
    state = _load_state()
    cfg = config.load_config()
    p = prof.load_profile()
    level = cfg.get("smart_level", "advanced")

    terms = store.all_terms(order="last_seen DESC")
    fresh = [t for t in terms if (t.get("last_seen") or 0) > state["last_ts"]][:40]
    if not fresh:
        return {"graded": 0, "removed": 0, "note": "no new captures"}

    try:
        verdicts = _grade_batch(fresh, level, p.get("known", []), p.get("unknown", []))
    except Exception as exc:  # noqa: BLE001
        return {"graded": 0, "removed": 0, "note": f"LLM unavailable: {exc}"}

    counts = {"good": 0, "too_easy": 0, "ocr_garbage": 0, "proper_name": 0, "bad_translation": 0}
    removed = []
    import sqlite3
    conn = sqlite3.connect(config.DB_PATH)
    for v in verdicts:
        verdict = v.get("verdict", "good")
        counts[verdict] = counts.get(verdict, 0) + 1
        term = v.get("term", "")
        # auto-remove clear junk; keep too_easy (informs level) and bad_translation
        # (can be re-enriched) — only purge garbage and names.
        if verdict in ("ocr_garbage", "proper_name"):
            conn.execute("DELETE FROM terms WHERE word=? OR word=?", (term, f"phrase:{term.lower()}"))
            removed.append(f"{term}:{verdict}")
    conn.commit()
    conn.close()

    # newest last_seen we processed
    state["last_ts"] = max((t.get("last_seen") or 0) for t in fresh)
    state["passes"] = state.get("passes", 0) + 1
    _save_state(state)

    os.makedirs(LOG_DIR, exist_ok=True)
    with open(CRITIQUE_JSONL, "a", encoding="utf-8") as handle:
        for v in verdicts:
            handle.write(json.dumps(v, ensure_ascii=False) + "\n")
    summary = (f"pass#{state['passes']} graded={len(verdicts)} "
               f"good={counts['good']} too_easy={counts['too_easy']} "
               f"garbage={counts['ocr_garbage']} names={counts['proper_name']} "
               f"bad_tr={counts['bad_translation']} removed={len(removed)} "
               f"| {', '.join(removed[:8])}")
    with open(SUMMARY_LOG, "a", encoding="utf-8") as handle:
        handle.write(summary + "\n")
    print(summary, flush=True)
    return {"graded": len(verdicts), "removed": len(removed), "counts": counts}


def main():
    if "--once" in sys.argv:
        print(run_pass())
        return
    print(f"self-critic running every {INTERVAL}s (Ctrl-C to stop)", flush=True)
    while True:
        try:
            run_pass()
        except Exception as exc:  # noqa: BLE001 — never die
            print(f"critic error: {exc}", flush=True)
        time.sleep(INTERVAL)


if __name__ == "__main__":
    main()
