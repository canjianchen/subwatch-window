"""Re-score the existing vocabulary against the current difficulty bar and prune
anything too easy. Use after tightening the difficulty settings so the deck
reflects the new standard (instead of hand-deleting words).

Run:  python3 rescore.py            # prune below the configured level
      python3 rescore.py --dry      # show what would be removed, change nothing
"""
import sys

import config
import store
import hard_phrases_llm as L

MIN_SCORE = {"intermediate": 6, "advanced": 8, "expert": 9}


def main():
    dry = "--dry" in sys.argv
    store.init_db()
    cfg = config.load_config()
    level = cfg.get("smart_level", "advanced")
    floor = MIN_SCORE.get(level, 8)

    terms = store.all_terms()
    if not terms:
        print("Deck is empty.")
        return 0

    import sqlite3
    conn = sqlite3.connect(config.DB_PATH)
    removed, kept = [], []
    for term in terms:
        display = term.get("matched") or term["word"].replace("phrase:", "")
        context = term.get("context") or display
        result = L.extract_hard(context, level=level)
        # find this term among the scored items; keep only if it scores >= floor
        score = 0
        for item in result.get("items", []):
            it = (item.get("term") or "").lower()
            if it == display.lower() or display.lower() in it or it in display.lower():
                score = max(score, int(item.get("score", 0) or 0))
        if score >= floor:
            kept.append((display, score))
        else:
            removed.append((display, score))
            if not dry:
                conn.execute("DELETE FROM terms WHERE word = ?", (term["word"],))
    if not dry:
        conn.commit()
    conn.close()

    print("KEPT (%d):" % len(kept))
    for d, s in sorted(kept, key=lambda x: -x[1]):
        print("  %2d  %s" % (s, d))
    print("\n%s (%d):" % ("WOULD REMOVE" if dry else "REMOVED", len(removed)))
    for d, s in removed:
        print("  %2d  %s" % (s, d))
    return 0


if __name__ == "__main__":
    sys.exit(main())
