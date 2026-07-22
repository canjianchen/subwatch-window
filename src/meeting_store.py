"""SQLite storage for Meeting Mode — transcripts, notes, RAG chunks, and chat.

Lives in the SAME database file as the vocabulary deck (config.DB_PATH) but in its own
additive tables; it never touches the `terms` table. Mirrors store.py's connection
discipline (WAL + busy_timeout + a fresh connection per call) so the capture loop, the
web panel, and the notes/RAG workers can read/write concurrently without "database is
locked" errors.

Design notes informed by Amazon's internal Livestream captioning service:
  • each segment carries is_partial + a source result_id, so a growing caption line is
    UPSERTED in place (keyed by result_id) and only "locked" when finalized — this is
    what keeps the live transcript clean instead of duplicating every partial frame.
  • the finalized transcript is durable on disk (unlike AiMS, which stores summary only)
    — persisting it locally is the whole point: the user can't get it from Zoom.
"""
import json
import os
import sqlite3
import time

import config


def _connect():
    os.makedirs(config.DB_DIR, exist_ok=True)
    conn = sqlite3.connect(config.DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=30000")
    except sqlite3.DatabaseError:
        pass
    return conn


def init_db():
    """Create the Meeting Mode tables if absent. Idempotent; safe to call every start."""
    conn = _connect()
    try:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS meetings (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                title        TEXT,
                source       TEXT NOT NULL DEFAULT 'zoom_ax',  -- zoom_ax | ocr | audio_whisper
                started_at   REAL NOT NULL,
                ended_at     REAL,
                status       TEXT NOT NULL DEFAULT 'live',      -- live | ended
                summary      TEXT,                              -- markdown rollup
                summary_json TEXT                               -- {tldr, decisions[], action_items[], qa[], topics[]}
            )
        """)
        # One finalized utterance = one row. `seq` is monotonic per meeting and is the
        # idempotent key (UNIQUE) so re-finalizing a grown line REPLACES in place.
        # `result_id` carries the upstream id (Zoom AX line identity / OCR active-line
        # key) so partials can be matched+upserted before they finalize.
        conn.execute("""
            CREATE TABLE IF NOT EXISTS segments (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                meeting_id  INTEGER NOT NULL,
                seq         INTEGER NOT NULL,
                speaker     TEXT,
                text        TEXT NOT NULL,
                norm        TEXT NOT NULL,
                t_start_ms  INTEGER NOT NULL,
                t_end_ms    INTEGER NOT NULL,
                is_partial  INTEGER NOT NULL DEFAULT 0,
                result_id   TEXT,
                created_at  REAL NOT NULL,
                UNIQUE(meeting_id, seq)
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_seg_meeting ON segments(meeting_id, t_start_ms)")
        # Running AI notes — appended on a slow cadence; the latest row is the current notes.
        conn.execute("""
            CREATE TABLE IF NOT EXISTS notes (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                meeting_id   INTEGER NOT NULL,
                created_at   REAL NOT NULL,
                upto_seq     INTEGER NOT NULL,
                kind         TEXT NOT NULL DEFAULT 'running',   -- running | final
                bullets      TEXT,
                action_items TEXT,                              -- json array
                decisions    TEXT                               -- json array
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_notes_meeting ON notes(meeting_id, created_at)")
        # RAG chunks: rolling windows of finalized segments for retrieval.
        conn.execute("""
            CREATE TABLE IF NOT EXISTS chunks (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                meeting_id  INTEGER NOT NULL,
                seq_start   INTEGER NOT NULL,
                seq_end     INTEGER NOT NULL,
                t_start_ms  INTEGER NOT NULL,
                t_end_ms    INTEGER NOT NULL,
                text        TEXT NOT NULL,
                token_est   INTEGER NOT NULL,
                embedding   BLOB,                               -- float32 bytes; null = lexical-only
                created_at  REAL NOT NULL
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_chunks_meeting ON chunks(meeting_id, t_start_ms)")
        # Lexical retrieval (always available; hybridized with vectors when present).
        conn.execute("""
            CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts
            USING fts5(text, content='chunks', content_rowid='id')
        """)
        # Chat history so "ask after the meeting" keeps continuity.
        conn.execute("""
            CREATE TABLE IF NOT EXISTS chat (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                meeting_id  INTEGER NOT NULL,
                role        TEXT NOT NULL,                      -- user | assistant
                content     TEXT NOT NULL,
                cited_seqs  TEXT,                               -- json array of segment seqs
                created_at  REAL NOT NULL
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_chat_meeting ON chat(meeting_id, created_at)")
        conn.commit()
    finally:
        conn.close()


# ── meetings ─────────────────────────────────────────────────────────────────
def create_meeting(title=None, source="zoom_ax"):
    now = time.time()
    if not title:
        title = "Meeting " + time.strftime("%Y-%m-%d %H:%M", time.localtime(now))
    conn = _connect()
    try:
        cur = conn.execute(
            "INSERT INTO meetings (title, source, started_at, status) VALUES (?,?,?, 'live')",
            (title, source, now))
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def set_title(meeting_id, title):
    """Rename a meeting (used to backfill calendar titles, or for user edits)."""
    conn = _connect()
    try:
        conn.execute("UPDATE meetings SET title=? WHERE id=?", (title, meeting_id))
        conn.commit()
    finally:
        conn.close()


def add_screen_capture(meeting_id, text, t_ms):
    """Store OCR'd shared-screen content as a context segment, marked with the speaker
    '[shared screen]' so it flows through the existing transcript/RAG (summary & chat can
    reference what was on screen) without a schema change. De-dups against the last few
    screen captures so a static slide isn't stored repeatedly."""
    conn = _connect()
    try:
        # skip if near-identical to a recent screen capture
        recent = conn.execute(
            "SELECT norm FROM segments WHERE meeting_id=? AND speaker='[shared screen]' "
            "ORDER BY seq DESC LIMIT 4", (meeting_id,)).fetchall()
        norm = " ".join(text.lower().split())
        for r in recent:
            a, b = set(norm.split()), set((r["norm"] or "").split())
            if a and b and len(a & b) / max(1, len(a | b)) > 0.85:
                return False  # essentially the same slide already captured
        row = conn.execute("SELECT MAX(seq) m FROM segments WHERE meeting_id=?",
                           (meeting_id,)).fetchone()
        nxt = (row["m"] if row and row["m"] is not None else -1) + 1
        # tolerate a race with the caption assembler writing the same seq: bump until free
        conn.execute("""
            INSERT OR IGNORE INTO segments (meeting_id, seq, speaker, text, norm, t_start_ms,
                                  t_end_ms, is_partial, result_id, created_at)
            VALUES (?,?,?,?,?,?,?,0,?,?)
        """, (meeting_id, nxt, "[shared screen]", text, norm, t_ms, t_ms,
              f"screen{nxt}", time.time()))
        conn.commit()
        return True
    finally:
        conn.close()


def end_meeting(meeting_id):
    conn = _connect()
    try:
        conn.execute("UPDATE meetings SET ended_at=?, status='ended' WHERE id=?",
                     (time.time(), meeting_id))
        conn.commit()
    finally:
        conn.close()


def get_meeting(meeting_id):
    conn = _connect()
    try:
        row = conn.execute("SELECT * FROM meetings WHERE id=?", (meeting_id,)).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def list_meetings(limit=100):
    conn = _connect()
    try:
        rows = conn.execute("""
            SELECT m.*,
                   (SELECT COUNT(*) FROM segments s WHERE s.meeting_id=m.id AND s.is_partial=0) AS segment_count
            FROM meetings m ORDER BY m.started_at DESC LIMIT ?
        """, (limit,)).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def latest_live_meeting():
    """The most recent meeting still marked live (used to resume/attach), or None."""
    conn = _connect()
    try:
        row = conn.execute(
            "SELECT * FROM meetings WHERE status='live' ORDER BY started_at DESC LIMIT 1").fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def set_summary(meeting_id, summary_md, summary_obj):
    conn = _connect()
    try:
        conn.execute("UPDATE meetings SET summary=?, summary_json=? WHERE id=?",
                     (summary_md, json.dumps(summary_obj, ensure_ascii=False), meeting_id))
        conn.commit()
    finally:
        conn.close()


def delete_meeting(meeting_id):
    """Remove a meeting and all its child rows."""
    conn = _connect()
    try:
        for tbl in ("segments", "notes", "chunks", "chat"):
            conn.execute(f"DELETE FROM {tbl} WHERE meeting_id=?", (meeting_id,))
        conn.execute("DELETE FROM meetings WHERE id=?", (meeting_id,))
        conn.commit()
    finally:
        conn.close()


# ── segments ─────────────────────────────────────────────────────────────────
def upsert_segment(meeting_id, seq, text, norm, t_start_ms, t_end_ms,
                   speaker=None, is_partial=0, result_id=None):
    """Insert or replace a segment by (meeting_id, seq). Used both for live partials
    (is_partial=1, upserted as the line grows) and for the final locked line."""
    conn = _connect()
    try:
        conn.execute("""
            INSERT INTO segments (meeting_id, seq, speaker, text, norm, t_start_ms,
                                  t_end_ms, is_partial, result_id, created_at)
            VALUES (?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(meeting_id, seq) DO UPDATE SET
                speaker=excluded.speaker, text=excluded.text, norm=excluded.norm,
                t_end_ms=excluded.t_end_ms, is_partial=excluded.is_partial,
                result_id=excluded.result_id
        """, (meeting_id, seq, speaker, text, norm, t_start_ms, t_end_ms,
              int(is_partial), result_id, time.time()))
        conn.commit()
    finally:
        conn.close()


def finalized_segments(meeting_id, after_seq=-1):
    """All finalized (non-partial) segments with seq > after_seq, in order."""
    conn = _connect()
    try:
        rows = conn.execute("""
            SELECT * FROM segments
            WHERE meeting_id=? AND is_partial=0 AND seq>?
            ORDER BY seq ASC
        """, (meeting_id, after_seq)).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def all_segments(meeting_id, include_partial=False):
    """Every segment for a meeting (for the live view / export)."""
    conn = _connect()
    try:
        q = "SELECT * FROM segments WHERE meeting_id=?"
        if not include_partial:
            q += " AND is_partial=0"
        q += " ORDER BY seq ASC"
        rows = conn.execute(q, (meeting_id,)).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def max_seq(meeting_id):
    conn = _connect()
    try:
        row = conn.execute("SELECT MAX(seq) AS m FROM segments WHERE meeting_id=?",
                           (meeting_id,)).fetchone()
        return (row["m"] if row and row["m"] is not None else -1)
    finally:
        conn.close()


# ── notes ────────────────────────────────────────────────────────────────────
def add_notes(meeting_id, upto_seq, bullets, action_items, decisions, kind="running"):
    conn = _connect()
    try:
        conn.execute("""
            INSERT INTO notes (meeting_id, created_at, upto_seq, kind, bullets,
                               action_items, decisions)
            VALUES (?,?,?,?,?,?,?)
        """, (meeting_id, time.time(), upto_seq, kind, bullets,
              json.dumps(action_items, ensure_ascii=False),
              json.dumps(decisions, ensure_ascii=False)))
        conn.commit()
    finally:
        conn.close()


def latest_notes(meeting_id):
    conn = _connect()
    try:
        row = conn.execute("""
            SELECT * FROM notes WHERE meeting_id=? ORDER BY created_at DESC LIMIT 1
        """, (meeting_id,)).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


# ── chunks (RAG) ─────────────────────────────────────────────────────────────
def add_chunk(meeting_id, seq_start, seq_end, t_start_ms, t_end_ms, text,
              token_est, embedding=None):
    conn = _connect()
    try:
        cur = conn.execute("""
            INSERT INTO chunks (meeting_id, seq_start, seq_end, t_start_ms, t_end_ms,
                                text, token_est, embedding, created_at)
            VALUES (?,?,?,?,?,?,?,?,?)
        """, (meeting_id, seq_start, seq_end, t_start_ms, t_end_ms, text,
              token_est, embedding, time.time()))
        rowid = cur.lastrowid
        conn.execute("INSERT INTO chunks_fts (rowid, text) VALUES (?,?)", (rowid, text))
        conn.commit()
        return rowid
    finally:
        conn.close()


def max_chunk_seq(meeting_id):
    """Highest seq_end already chunked, so the chunker only processes new segments."""
    conn = _connect()
    try:
        row = conn.execute("SELECT MAX(seq_end) AS m FROM chunks WHERE meeting_id=?",
                           (meeting_id,)).fetchone()
        return (row["m"] if row and row["m"] is not None else -1)
    finally:
        conn.close()


def all_chunks(meeting_id):
    conn = _connect()
    try:
        rows = conn.execute(
            "SELECT * FROM chunks WHERE meeting_id=? ORDER BY t_start_ms ASC",
            (meeting_id,)).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def search_chunks_fts(meeting_id, query, limit=8):
    """BM25 lexical retrieval over chunks. Returns chunk dicts with a `score` (lower
    bm25 = better; we negate so higher=better). Tolerates FTS query-syntax errors."""
    conn = _connect()
    try:
        safe = _fts_sanitize(query)
        if not safe:
            return []
        try:
            rows = conn.execute("""
                SELECT c.*, bm25(chunks_fts) AS bm25
                FROM chunks_fts JOIN chunks c ON c.id = chunks_fts.rowid
                WHERE chunks_fts MATCH ? AND c.meeting_id=?
                ORDER BY bm25 ASC LIMIT ?
            """, (safe, meeting_id, limit)).fetchall()
        except sqlite3.OperationalError:
            return []
        out = []
        for r in rows:
            d = dict(r)
            d["score"] = -float(d.pop("bm25", 0.0))
            out.append(d)
        return out
    finally:
        conn.close()


def _fts_sanitize(query):
    """Turn a free-text question into a safe FTS5 OR-query of its content words."""
    import re
    words = re.findall(r"[A-Za-z0-9']+", query or "")
    words = [w for w in words if len(w) > 2]
    return " OR ".join(words[:24])


# ── chat ─────────────────────────────────────────────────────────────────────
def add_chat(meeting_id, role, content, cited_seqs=None):
    conn = _connect()
    try:
        conn.execute("""
            INSERT INTO chat (meeting_id, role, content, cited_seqs, created_at)
            VALUES (?,?,?,?,?)
        """, (meeting_id, role, content,
              json.dumps(cited_seqs or [], ensure_ascii=False), time.time()))
        conn.commit()
    finally:
        conn.close()


def chat_history(meeting_id, limit=50):
    conn = _connect()
    try:
        rows = conn.execute("""
            SELECT * FROM chat WHERE meeting_id=? ORDER BY created_at ASC LIMIT ?
        """, (meeting_id, limit)).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


if __name__ == "__main__":
    init_db()
    print("meeting_store initialized at", config.DB_PATH)
    print("meetings:", len(list_meetings()))
