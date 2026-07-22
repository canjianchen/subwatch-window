"""SQLite-backed storage for captured vocabulary and a spaced-repetition schedule."""
import os
import sqlite3
import time

import config


def _connect():
    os.makedirs(config.DB_DIR, exist_ok=True)
    # timeout makes concurrent writers (watch loop + rescore/enrich/panel) WAIT for
    # a lock instead of raising "database is locked"; WAL improves reader/writer
    # concurrency. This keeps the long-running watch loop from crashing.
    conn = sqlite3.connect(config.DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=30000")
    except sqlite3.DatabaseError:
        pass
    return conn


def init_db():
    conn = _connect()
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS terms (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            word          TEXT UNIQUE NOT NULL,
            lemma         TEXT,
            context       TEXT,
            chinese       TEXT,
            definition    TEXT,
            rarity_rank   INTEGER,
            times_seen    INTEGER NOT NULL DEFAULT 1,
            first_seen    REAL NOT NULL,
            last_seen     REAL NOT NULL,
            -- spaced repetition (SM-2-lite)
            ease          REAL NOT NULL DEFAULT 2.5,
            interval_days REAL NOT NULL DEFAULT 0,
            due           REAL NOT NULL,
            reps          INTEGER NOT NULL DEFAULT 0,
            status        TEXT NOT NULL DEFAULT 'new'
        )
        """
    )
    # Migration: add `phrase` column for idioms/phrasal expressions the word is part of.
    cols = {row["name"] for row in conn.execute("PRAGMA table_info(terms)")}
    if "phrase" not in cols:
        conn.execute("ALTER TABLE terms ADD COLUMN phrase TEXT")
    # 'word' = single vocabulary word; 'phrase' = idiom/slang (stored as the full
    # sentence with the matched idiom flagged in `matched`).
    if "kind" not in cols:
        conn.execute("ALTER TABLE terms ADD COLUMN kind TEXT NOT NULL DEFAULT 'word'")
    if "matched" not in cols:
        conn.execute("ALTER TABLE terms ADD COLUMN matched TEXT")
    # Direct translation of the word/phrase ITSELF (distinct from `chinese`, which
    # holds the surrounding subtitle line). And the path to a cached pronunciation
    # audio clip for the word/phrase.
    if "translation" not in cols:
        conn.execute("ALTER TABLE terms ADD COLUMN translation TEXT")
    if "audio_path" not in cols:
        conn.execute("ALTER TABLE terms ADD COLUMN audio_path TEXT")
    # The actual Chinese SUBTITLE line that was on screen when captured. Preserved
    # verbatim and never overwritten by enrichment (unlike `chinese`, which the LLM
    # fills with a contextual gloss).
    if "subtitle_cn" not in cols:
        conn.execute("ALTER TABLE terms ADD COLUMN subtitle_cn TEXT")
    # user-curated personal dictionary: 1 = starred/kept for future reference.
    if "favorite" not in cols:
        conn.execute("ALTER TABLE terms ADD COLUMN favorite INTEGER NOT NULL DEFAULT 0")
    conn.commit()
    conn.close()


def set_favorite(word, value):
    """Star/unstar a term for the user's personal dictionary."""
    conn = _connect()
    conn.execute("UPDATE terms SET favorite = ? WHERE word = ?", (1 if value else 0, word))
    conn.commit()
    conn.close()


def favorites():
    conn = _connect()
    rows = conn.execute("SELECT * FROM terms WHERE favorite = 1 ORDER BY last_seen DESC").fetchall()
    conn.close()
    return [dict(r) for r in rows]


def upsert_term(word, context=None, chinese=None, rarity_rank=None, lemma=None):
    """Record a sighting of a word. Increments times_seen if already known.
    `chinese` here is the on-screen Chinese SUBTITLE line; it is stored verbatim in
    subtitle_cn and preserved (never overwritten by enrichment)."""
    now = time.time()
    conn = _connect()
    row = conn.execute("SELECT id, times_seen, context, subtitle_cn FROM terms WHERE word = ?", (word,)).fetchone()
    if row:
        # keep the longest context we have seen (usually the most informative)
        keep_context = row["context"]
        if context and (not keep_context or len(context) > len(keep_context)):
            keep_context = context
        keep_cn = row["subtitle_cn"] or chinese
        conn.execute(
            "UPDATE terms SET times_seen = times_seen + 1, last_seen = ?, context = ?, subtitle_cn = ? WHERE id = ?",
            (now, keep_context, keep_cn, row["id"]),
        )
        new = False
    else:
        conn.execute(
            """INSERT INTO terms
               (word, lemma, context, subtitle_cn, rarity_rank, first_seen, last_seen, due)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (word, lemma or word, context, chinese, rarity_rank, now, now, now),
        )
        new = True
    conn.commit()
    conn.close()
    return new


def upsert_phrase(matched, sentence, chinese=None):
    """Record a phrase/slang sighting. Keyed on the matched expression; stores the
    FULL English sentence as context (per the user's preference to keep the whole
    sentence, not a substring)."""
    now = time.time()
    key = f"phrase:{matched.lower()}"
    conn = _connect()
    row = conn.execute("SELECT id, times_seen, context, subtitle_cn FROM terms WHERE word = ?", (key,)).fetchone()
    if row:
        keep = row["context"]
        if sentence and (not keep or len(sentence) > len(keep)):
            keep = sentence
        keep_cn = row["subtitle_cn"] or chinese
        conn.execute(
            "UPDATE terms SET times_seen = times_seen + 1, last_seen = ?, context = ?, subtitle_cn = ? WHERE id = ?",
            (now, keep, keep_cn, row["id"]),
        )
        new = False
    else:
        conn.execute(
            """INSERT INTO terms
               (word, lemma, context, subtitle_cn, rarity_rank, first_seen, last_seen, due, kind, matched)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'phrase', ?)""",
            (key, matched, sentence, chinese, None, now, now, now, matched),
        )
        new = True
    conn.commit()
    conn.close()
    return new


def set_enrichment(word, definition=None, chinese=None, phrase=None, translation=None):
    """Attach a definition / translation / idiom phrase produced later (LLM pass).
    `translation` = direct word/phrase translation; `chinese` = subtitle-line context."""
    conn = _connect()
    fields, values = [], []
    if definition is not None:
        fields.append("definition = ?")
        values.append(definition)
    if chinese is not None:
        fields.append("chinese = ?")
        values.append(chinese)
    if phrase is not None:
        fields.append("phrase = ?")
        values.append(phrase)
    if translation is not None:
        fields.append("translation = ?")
        values.append(translation)
    if fields:
        values.append(word)
        conn.execute(f"UPDATE terms SET {', '.join(fields)} WHERE word = ?", values)
        conn.commit()
    conn.close()


def set_audio(word, audio_path):
    """Record the cached pronunciation audio path for a word/phrase."""
    conn = _connect()
    conn.execute("UPDATE terms SET audio_path = ? WHERE word = ?", (audio_path, word))
    conn.commit()
    conn.close()


def terms_needing_audio():
    """Words/phrases that don't yet have a cached audio clip."""
    conn = _connect()
    rows = conn.execute(
        "SELECT word, matched, kind FROM terms WHERE audio_path IS NULL OR audio_path = ''"
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def due_terms(limit=50):
    now = time.time()
    conn = _connect()
    rows = conn.execute(
        "SELECT * FROM terms WHERE due <= ? ORDER BY due ASC LIMIT ?", (now, limit)
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def all_terms(order="last_seen DESC"):
    conn = _connect()
    rows = conn.execute(f"SELECT * FROM terms ORDER BY {order}").fetchall()
    conn.close()
    return [dict(r) for r in rows]


def review(word, quality):
    """Apply an SM-2-lite update. quality: 0=again, 1=hard, 2=good, 3=easy."""
    now = time.time()
    conn = _connect()
    row = conn.execute("SELECT * FROM terms WHERE word = ?", (word,)).fetchone()
    if not row:
        conn.close()
        return
    ease = row["ease"]
    interval = row["interval_days"]
    reps = row["reps"]

    if quality == 0:  # again
        reps = 0
        interval = 0
        ease = max(1.3, ease - 0.2)
        status = "learning"
    else:
        ease = max(1.3, ease + (0.1 if quality == 3 else (0 if quality == 2 else -0.15)))
        if reps == 0:
            interval = 1 if quality == 1 else (2 if quality == 2 else 4)
        elif reps == 1:
            interval = 3 if quality == 1 else (6 if quality == 2 else 10)
        else:
            interval = round(interval * ease, 2)
        reps += 1
        status = "review" if reps < 5 else "mastered"

    due = now + interval * 86400
    conn.execute(
        "UPDATE terms SET ease = ?, interval_days = ?, reps = ?, due = ?, status = ? WHERE word = ?",
        (ease, interval, reps, due, status, word),
    )
    conn.commit()
    conn.close()


def stats():
    conn = _connect()
    total = conn.execute("SELECT COUNT(*) c FROM terms").fetchone()["c"]
    now = time.time()
    due = conn.execute("SELECT COUNT(*) c FROM terms WHERE due <= ?", (now,)).fetchone()["c"]
    mastered = conn.execute("SELECT COUNT(*) c FROM terms WHERE status = 'mastered'").fetchone()["c"]
    conn.close()
    return {"total": total, "due": due, "mastered": mastered}
