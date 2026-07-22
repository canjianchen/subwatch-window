"""Meeting Mode AI: running notes, grounded Q&A chat, and post-meeting summaries.

All language-model calls run through the authenticated local Codex CLI. The mechanical
layer (transcript dedup/assembly, chunking) remains deterministic and local.

Design borrows directly from Amazon's internal AiMS (AI Meeting Summaries) production
service and its Livestream captioning service:
  • the summary SCHEMA (conciseOverview / narrativeSummary / decisions / actionItems /
    announcements / keyPoints) and structured action items {action, owner, deadline}.
  • action-verb-first phrasing; owner = first name or TBD, only when explicitly stated;
    deadline preserved verbatim or TBD.
  • a Chain-of-Verification (CoVe) EVALUATION pass: a second Codex call fact-checks every
    name/date/number/owner against the transcript and scores the summary — the single
    highest-leverage anti-hallucination technique.
  • strict anti-hallucination clauses ("only what is explicitly stated; never invent").
  • chat retrieves transcript context on demand (RAG) instead of stuffing everything.

Unlike AiMS (which by policy keeps NO transcript), we persist everything locally — so we
can regenerate, re-summarize, change verbosity, and let chat cite exact passages.

Everything degrades gracefully: if Codex is unavailable or local_only is set, notes
fall back to a simple extractive list and chat returns a clear "AI unavailable" notice,
while the transcript itself (the core value) keeps working.
"""
import json
import re
import struct

import config
import codex_ai
import meeting_store as store
import hard_phrases_llm as L  # reuse tolerant JSON helpers


# Every task uses the same configured Codex model; reasoning effort varies by task.
def _summary_model():
    return config.load_config().get("codex_model", "gpt-5.6-terra")


def _notes_model():
    cfg = config.load_config()
    return cfg.get("meeting", {}).get("notes_model") or cfg.get("codex_model",
                                                                 "gpt-5.6-terra")


def _ai_ok():
    return not config.effective_local_only() and config.codex_available()


# ── low-level Codex adapter ──────────────────────────────────────────────────
def _effort(kind="summary"):
    """Reasoning-effort level. The post-meeting summary uses high effort. Live chat
    uses medium effort so replies return sooner. Valid: low|medium|high|xhigh|max.
    Both overridable via meeting.effort / meeting.chat_effort."""
    mc = config.load_config().get("meeting", {})
    if kind == "chat":
        return mc.get("chat_effort", "medium")
    return mc.get("effort", "high")


_last_converse_error = None  # surfaced to the chat UI when a call truly fails


def _converse(prompt, model, max_tokens=1500, system=None, retries=2, kind="summary"):
    """One non-streaming Codex call. Retries on transient errors. Returns
    text, or '' after exhausting retries (and records the error in _last_converse_error)."""
    global _last_converse_error
    last = None
    for attempt in range(retries + 1):
        try:
            effort = _effort(kind)
            r = codex_ai.ask(prompt, system=system, model=model, effort=effort,
                             timeout=360 if kind == "summary" else 240)
            _last_converse_error = None
            return r.strip()
        except Exception as exc:  # noqa: BLE001 — transient → brief backoff
            last = exc
            if attempt < retries:
                import time as _t
                _t.sleep(0.8 * (attempt + 1))
    _last_converse_error = str(last)[:200] if last else "unknown error"
    return ""


def _converse_stream(prompt, model, max_tokens=1500, system=None, kind="summary"):
    """Compatibility generator for callers that expect streamed answer chunks."""
    global _last_converse_error
    text = _converse(prompt, model, max_tokens=max_tokens, system=system,
                     retries=1, kind=kind)
    if text:
        yield text


def _json_obj(text):
    """Parse the first JSON object out of model text, tolerant of fences/prose."""
    if not text:
        return {}
    try:
        return L._extract_json_object(text)
    except Exception:  # noqa: BLE001
        return {}


def _seg_line(seg):
    """Render a segment for prompt context: '[mm:ss] (#seq) Speaker: text'."""
    ms = seg.get("t_start_ms", 0)
    ts = f"{ms // 60000:02d}:{(ms // 1000) % 60:02d}"
    spk = (seg.get("speaker") or "").strip()
    prefix = f"{spk}: " if spk else ""
    return f"[{ts}] (#{seg['seq']}) {prefix}{seg['text']}"


# Reused anti-hallucination clause (paraphrased from the AiMS generation prompt).
_GROUNDING = (
    "Use ONLY information explicitly present in the transcript. Do NOT invent participants, "
    "decisions, numbers, dates, or events that were not stated. Attribute statements to a "
    "speaker only when the transcript explicitly identifies them. If something was not "
    "discussed, say so rather than guessing. Avoid weasel words (very, really, just, simply)."
)


def _persona():
    """Personalization context (the user + pronoun roster) injected into notes/summary/
    chat prompts, so the AI flags the USER's own action items and uses correct names and
    pronouns (the AiMS personalization + pronoun-correction requests)."""
    mc = config.load_config().get("meeting", {})
    me = mc.get("me", {}) or {}
    bits = []
    name = (me.get("name") or "").strip()
    if name:
        aliases = ", ".join(me.get("aliases", []) or [])
        pron = me.get("pronouns", "")
        first = name.split()[0]
        who = (f"IMPORTANT — IDENTITY: The user asking is {name}"
               + (f" ({pron})" if pron else "") + ". ")
        if aliases:
            who += (f"In the transcript they may appear as any of these spellings "
                    f"(often OCR-garbled): {aliases}. Treat ALL of these as the user. ")
        who += (f"When the user says 'I', 'me', 'my', or 'mine', they mean {first}. "
                f"So 'what are my action items' = action items owned by {first}/{name} "
                f"(or any of those spellings). If the user asks what THEY should do, look "
                f"for commitments {first} made or tasks assigned to {first}, and answer "
                f"directly (say 'You …'); also include any action item assigned to everyone. "
                f"Mark the user's own items clearly.")
        bits.append(who)
    roster = mc.get("pronouns", {}) or {}
    if roster:
        pairs = "; ".join(f"{n}: {p}" for n, p in roster.items())
        bits.append(f"Use these people's correct pronouns: {pairs}.")
    return (" " + " ".join(bits)) if bits else ""


# ── chunking (for RAG) ───────────────────────────────────────────────────────
def flush_chunks(meeting_id, window_segments=14, overlap=2):
    """Build RAG chunks from any new finalized segments not yet chunked. A chunk is a
    rolling window of ~window_segments segments with a small overlap so an answer that
    straddles a boundary isn't split. Embeds when an embedder is available; always
    indexed in FTS. Returns the number of new chunks created."""
    already = store.max_chunk_seq(meeting_id)
    segs = store.finalized_segments(meeting_id, after_seq=already)
    if len(segs) < 1:
        return 0
    made = 0
    i = 0
    while i < len(segs):
        window = segs[i:i + window_segments]
        if not window:
            break
        text = "\n".join(_seg_line(s) for s in window)
        emb = _embed(text)
        store.add_chunk(
            meeting_id,
            seq_start=window[0]["seq"], seq_end=window[-1]["seq"],
            t_start_ms=window[0]["t_start_ms"], t_end_ms=window[-1]["t_end_ms"],
            text=text, token_est=max(1, len(text) // 4),
            embedding=emb)
        made += 1
        if i + window_segments >= len(segs):
            break
        i += max(1, window_segments - overlap)
    return made


# ── embeddings (optional; graceful 3-tier) ───────────────────────────────────
_EMBEDDER = None
_EMBED_TRIED = False


def _embed(text):
    """Return a float32 embedding BLOB, or None if no embedder is available (then we
    fall back to pure lexical FTS retrieval). Uses only a local sentence-transformer;
    no cloud embedding model is allowed in the Codex-only configuration."""
    global _EMBEDDER, _EMBED_TRIED
    if not _EMBED_TRIED:
        _EMBED_TRIED = True
        try:
            from sentence_transformers import SentenceTransformer
            _EMBEDDER = ("local", SentenceTransformer("all-MiniLM-L6-v2"))
        except Exception:  # noqa: BLE001
            _EMBEDDER = (None, None)
    kind = _EMBEDDER[0] if _EMBEDDER else None
    try:
        if kind == "local":
            vec = _EMBEDDER[1].encode([text])[0]
            return struct.pack(f"{len(vec)}f", *[float(x) for x in vec])
    except Exception:  # noqa: BLE001
        return None
    return None


def _unpack(blob):
    if not blob:
        return None
    n = len(blob) // 4
    return list(struct.unpack(f"{n}f", blob))


def _cosine(a, b):
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = sum(x * x for x in a) ** 0.5
    nb = sum(y * y for y in b) ** 0.5
    return dot / (na * nb) if na and nb else 0.0


# ── retrieval ────────────────────────────────────────────────────────────────
def retrieve(meeting_id, question, k=6):
    """Hybrid retrieval: union of FTS (BM25) and, when embeddings exist, vector cosine
    over the meeting's chunks. Returns up to k chunk dicts, best-first."""
    lexical = store.search_chunks_fts(meeting_id, question, limit=k * 2)
    scored = {c["id"]: dict(c, _hybrid=c.get("score", 0.0)) for c in lexical}

    qemb = _unpack(_embed(question)) if question else None
    if qemb:
        for c in store.all_chunks(meeting_id):
            v = _unpack(c.get("embedding"))
            if v:
                sim = _cosine(qemb, v)
                if c["id"] in scored:
                    scored[c["id"]]["_hybrid"] += sim
                else:
                    scored[c["id"]] = dict(c, _hybrid=sim)
    ranked = sorted(scored.values(), key=lambda c: c["_hybrid"], reverse=True)
    return ranked[:k]


# ── running notes (incremental, append-only) ─────────────────────────────────
def update_notes(meeting_id):
    """Merge-update the running notes with segments added since the last notes row.
    Cheap and bounded: never re-feeds the whole transcript. Returns the new notes dict
    or None if there was nothing new / AI unavailable."""
    prev = store.latest_notes(meeting_id)
    upto = prev["upto_seq"] if prev else -1
    new_segs = store.finalized_segments(meeting_id, after_seq=upto)
    if not new_segs:
        return None
    newest_seq = new_segs[-1]["seq"]

    if not _ai_ok():
        return _extractive_notes(meeting_id, new_segs, prev, newest_seq)

    prev_bullets = prev["bullets"] if prev else ""
    prev_actions = json.loads(prev["action_items"]) if prev and prev["action_items"] else []
    prev_decisions = json.loads(prev["decisions"]) if prev and prev["decisions"] else []
    transcript_new = "\n".join(_seg_line(s) for s in new_segs)

    prompt = (
        "You are taking live notes during a meeting. You are given the notes SO FAR and the "
        "NEW transcript lines since then. UPDATE the notes by MERGING in the new content — "
        "keep existing points, add new ones, refine if needed. Do NOT rewrite from scratch and "
        "do NOT drop earlier points. Keep it tight and scannable.\n\n"
        f"{_GROUNDING}{_persona()}\n\n"
        f"NOTES SO FAR (markdown bullets):\n{prev_bullets or '(none yet)'}\n\n"
        f"EXISTING ACTION ITEMS (json):\n{json.dumps(prev_actions, ensure_ascii=False)}\n\n"
        f"EXISTING DECISIONS (json):\n{json.dumps(prev_decisions, ensure_ascii=False)}\n\n"
        f"NEW TRANSCRIPT LINES:\n{transcript_new}\n\n"
        "Reply with ONLY a JSON object:\n"
        '{"bullets": "the FULL updated markdown bullet list (running summary of the whole '
        'meeting so far)", "action_items": [{"action":"[VERB] task","owner":"FirstName or TBD",'
        '"deadline":"verbatim or TBD"}], "decisions": ["decision", ...]}\n'
        "Action items: verb-first; owner only if explicitly stated else TBD; deadline verbatim "
        "or TBD. Only include action items/decisions actually stated."
    )
    data = _json_obj(_converse(prompt, _notes_model(), max_tokens=4000))
    if not data or "bullets" not in data:
        return None
    bullets = (data.get("bullets") or prev_bullets or "").strip()
    actions = data.get("action_items") or prev_actions
    decisions = data.get("decisions") or prev_decisions
    store.add_notes(meeting_id, newest_seq, bullets, actions, decisions, kind="running")
    return {"bullets": bullets, "action_items": actions, "decisions": decisions,
            "upto_seq": newest_seq}


def _extractive_notes(meeting_id, new_segs, prev, newest_seq):
    """Local-only fallback notes: simple extractive bullets (longest/keyword lines).
    No model call. Keeps the feature usable offline."""
    prev_bullets = prev["bullets"] if prev else ""
    picks = []
    for s in new_segs:
        t = s["text"].strip()
        if len(t) > 40 or re.search(r"\b(decide|action|will|should|need to|todo|by )\b", t, re.I):
            spk = f"{s['speaker']}: " if s.get("speaker") else ""
            picks.append(f"- {spk}{t}")
    bullets = (prev_bullets + "\n" + "\n".join(picks[:8])).strip()
    store.add_notes(meeting_id, newest_seq, bullets, [], [], kind="running")
    return {"bullets": bullets, "action_items": [], "decisions": [], "upto_seq": newest_seq}


# ── chat (grounded Q&A; live + post) ─────────────────────────────────────────
def answer_question(meeting_id, question, stream=False):
    """Answer a question about the meeting, grounded in retrieved transcript context +
    the running notes. Returns (answer_text, cited_seqs). If stream=True, returns a
    generator of text deltas instead (and the caller persists the final text)."""
    if not _ai_ok():
        msg = ("AI chat needs an authenticated Codex CLI, which is currently unavailable. "
               "The full transcript is still saved and searchable.")
        return (iter([msg]) if stream else (msg, []))

    chunks = retrieve(meeting_id, question, k=6)
    notes = store.latest_notes(meeting_id)
    notes_block = (notes["bullets"] if notes else "") or "(no notes yet)"
    # Always include the last few segments so "what did he just say" works live.
    recent = store.finalized_segments(meeting_id, after_seq=store.max_seq(meeting_id) - 8)
    cited_seqs = sorted({s for c in chunks for s in range(c["seq_start"], c["seq_end"] + 1)})

    context = "\n\n".join(c["text"] for c in chunks) or "(no transcript chunks yet)"
    recent_block = "\n".join(_seg_line(s) for s in recent) or "(none)"
    history = store.chat_history(meeting_id, limit=8)
    hist_block = "\n".join(f"{h['role'].upper()}: {h['content']}" for h in history[-6:])

    system = (
        "You are a meeting assistant answering the user's questions about a meeting they are "
        "in or have attended. " + _GROUNDING + _persona() + " Cite the segment numbers "
        "(e.g. #12) you used. If the answer isn't in the transcript, say it wasn't discussed. "
        "Be concise and direct."
    )
    prompt = (
        f"MEETING NOTES SO FAR:\n{notes_block}\n\n"
        f"RELEVANT TRANSCRIPT EXCERPTS:\n{context}\n\n"
        f"MOST RECENT LINES:\n{recent_block}\n\n"
        + (f"EARLIER Q&A:\n{hist_block}\n\n" if hist_block else "")
        + f"QUESTION: {question}\n\nAnswer:"
    )
    model = _summary_model()
    if stream:
        return _converse_stream(prompt, model, max_tokens=4000, system=system, kind="chat")
    answer = _converse(prompt, model, max_tokens=4000, system=system, kind="chat")
    if not answer:
        # genuine failure after retries — tell the user what happened (and that nothing
        # was lost) instead of a flat "couldn't generate an answer".
        why = _last_converse_error or "the AI service was momentarily unavailable"
        answer = (f"Couldn't reach the AI just now ({why}). Your transcript is saved — "
                  "please ask again in a moment.")
    return answer, cited_seqs


# ── meeting-type awareness (tailor the summary to the kind of meeting) ───────
# Per-type guidance appended to the summary system prompt. Inspired by the AiMS
# "adjust summary prompt based on meeting" request — a standup wants per-person status,
# a 1:1 wants growth/feedback themes, an interview wants signal, etc.
_TYPE_GUIDANCE = {
    "standup": "This is a STANDUP. Organize around each person's update (done / doing / blockers) "
               "and surface blockers and cross-dependencies prominently.",
    "one_on_one": "This is a 1:1. Focus on themes: priorities, feedback, growth/career, concerns, "
                  "and any commitments made by either person. Keep it discreet and factual.",
    "planning": "This is a PLANNING/roadmap meeting. Emphasize scope decisions, priorities, "
                "owners, sequencing, dates/milestones, and trade-offs considered.",
    "review": "This is a REVIEW (design/doc/code/operational). Capture the proposal, feedback "
               "raised, concerns/risks, decisions, and required follow-up changes.",
    "interview": "This is an INTERVIEW debrief/loop. Capture competency signals and evidence; "
                 "do NOT fabricate a hire/no-hire recommendation if not explicitly stated.",
    "incident": "This is an INCIDENT/operational bridge. Capture timeline, impact, current status, "
                "mitigations, action items with owners, and open questions.",
    "general": "",
}


def detect_meeting_type(meeting_id, title="", sample=""):
    """Classify the meeting (standup/one_on_one/planning/review/interview/incident/general)
    from its title + a transcript sample. Cheap heuristic first; LLM only if ambiguous."""
    text = f"{title}\n{sample}".lower()
    rules = [
        ("standup", ("standup", "stand-up", "daily scrum", "daily sync")),
        ("interview", ("interview", "loop", "debrief", "candidate", "phone screen")),
        ("incident", ("incident", "sev2", "sev1", "outage", "bridge", "coe", "ops review")),
        ("one_on_one", ("1:1", "1-1", "one on one", "one-on-one", "skip level", "skip-level")),
        ("review", ("design review", "doc review", "code review", "cr review", "operational review",
                    "ops review", "sprint review", "retro")),
        ("planning", ("planning", "roadmap", "okr", "sprint planning", "kickoff", "kick-off",
                      "prioritization", "backlog")),
    ]
    for kind, keys in rules:
        if any(k in text for k in keys):
            return kind
    return "general"


def _summary_system(meeting_type="general"):
    """Build the summary system prompt: leader persona + grounding + personalization +
    meeting-type-specific guidance."""
    base = ("You are an experienced business and technology leader writing a meeting summary in "
            "clear, professional English. " + _GROUNDING + _persona())
    guidance = _TYPE_GUIDANCE.get(meeting_type, "")
    return base + ((" " + guidance) if guidance else "")


def generate_title(meeting_id, max_words=7):
    """Generate a short topic title from the TRANSCRIPT itself (ground truth), e.g.
    'Search latency & Q3 roadmap' or 'Personal doctor call'. This is the reliable signal
    — unlike the calendar, which can mis-match a HOLD/placeholder block. Returns '' if no
    transcript or AI unavailable. Uses a transcript sample (head+tail) to keep it cheap."""
    segs = store.finalized_segments(meeting_id)
    if not segs or not _ai_ok():
        return ""
    # sample: first ~25 + last ~15 lines is plenty to name the meeting
    sample = segs[:25] + (segs[-15:] if len(segs) > 40 else [])
    text = "\n".join(s["text"] for s in sample)[:6000]
    prompt = (
        f"Below is a sample of a meeting transcript. In at most {max_words} words, give a "
        "specific TITLE describing what this meeting was actually about (the real topic, like "
        "a person would name it). No quotes, no trailing punctuation, Title Case. If it's "
        "clearly a personal/non-work call, say so.\n\n"
        f"TRANSCRIPT SAMPLE:\n{text}\n\nTitle:")
    t = _converse(prompt, _summary_model(), max_tokens=2000)
    # take the first non-empty line, strip quotes/punctuation
    t = (t or "").strip().splitlines()[0].strip().strip('"\'.').strip() if t else ""
    return t[:80]


def generate_summary(meeting_id):
    """One-shot post-meeting summary over the full transcript. Map-reduce for long
    meetings so it fits the context window. Adopts the AiMS schema and runs a CoVe
    fact-check pass. Saves to the meeting row. Returns the summary dict."""
    segs = store.finalized_segments(meeting_id)
    if not segs:
        obj = {"conciseOverview": "No transcript was captured.", "narrativeSummary": "",
               "decisions": [], "actionItems": [], "announcements": [], "keyPoints": ""}
        store.set_summary(meeting_id, _render_summary_md(obj), obj)
        return obj

    if not _ai_ok():
        obj = _extractive_summary(segs)
        store.set_summary(meeting_id, _render_summary_md(obj), obj)
        return obj

    full = "\n".join(_seg_line(s) for s in segs)
    # tailor the summary to the kind of meeting (standup/1:1/planning/...)
    meeting = store.get_meeting(meeting_id) or {}
    mtype = detect_meeting_type(meeting_id, meeting.get("title", ""), full[:2000])
    system = _summary_system(mtype)
    # ~4 chars/token; map-reduce above ~12k tokens of transcript.
    if len(full) > 48000:
        obj = _summary_map_reduce(segs, system)
    else:
        obj = _summary_one_shot(full, system)
    obj["meetingType"] = mtype

    # CoVe fact-check pass (best-effort; attaches a quality score, never blocks).
    obj["evaluation"] = _evaluate_summary(full, obj, system)
    store.set_summary(meeting_id, _render_summary_md(obj), obj)
    return obj


_SCHEMA_INSTRUCTIONS = (
    "Reply with ONLY a JSON object with these fields:\n"
    '{"conciseOverview": "one scannable sentence",\n'
    ' "narrativeSummary": "2-5 sentence big-picture paragraph",\n'
    ' "decisions": ["each discrete decision agreed in the meeting"],\n'
    ' "actionItems": [{"action":"[VERB] the task","owner":"FirstName or TBD",'
    '"deadline":"verbatim natural language or TBD"}],\n'
    ' "announcements": ["org/policy/process announcements only; omit if none"],\n'
    ' "keyPoints": "markdown with #### topic headers carrying discussion context, metrics, '
    'alternatives, concerns — MUST NOT repeat decisions/actionItems; escape newlines as \\\\n"}\n'
    "Rules: action items are verb-first; owner is a first name only when explicitly stated, else "
    "TBD; deadline preserved verbatim or TBD. No direct quotes. No speaker attribution except in "
    "action-item owners. Sentence case. Scale depth to transcript length."
)


def _summary_one_shot(full_transcript, system):
    prompt = (f"Summarize the following meeting transcript.\n\n{_SCHEMA_INSTRUCTIONS}\n\n"
              f"TRANSCRIPT:\n{full_transcript}")
    obj = _json_obj(_converse(prompt, _summary_model(), max_tokens=8000, system=system))
    return _coerce_summary(obj)


def _summary_map_reduce(segs, system):
    """For long meetings: summarize windows, then reduce the window-summaries into the
    final schema. Keeps every call within the context window."""
    window, windows, cur = 60, [], []
    for s in segs:
        cur.append(s)
        if len(cur) >= window:
            windows.append(cur)
            cur = []
    if cur:
        windows.append(cur)
    partials = []
    for i, w in enumerate(windows):
        text = "\n".join(_seg_line(s) for s in w)
        p = _converse(
            f"Summarize this portion ({i + 1}/{len(windows)}) of a meeting transcript into "
            f"concise bullet points capturing decisions, action items (with owner/deadline if "
            f"stated), and key discussion. {_GROUNDING}\n\nTRANSCRIPT:\n{text}",
            _summary_model(), max_tokens=1200, system=system)
        if p:
            partials.append(f"[Part {i + 1}]\n{p}")
    combined = "\n\n".join(partials)
    prompt = (f"Combine these sequential partial summaries of ONE meeting into a single final "
              f"summary.\n\n{_SCHEMA_INSTRUCTIONS}\n\nPARTIAL SUMMARIES:\n{combined}")
    obj = _json_obj(_converse(prompt, _summary_model(), max_tokens=8000, system=system))
    return _coerce_summary(obj)


def _evaluate_summary(full_transcript, summary_obj, system=None):
    """Chain-of-Verification: a second Codex pass that fact-checks the summary's
    names/dates/numbers/owners against the transcript and scores it 0-5. Returns a
    dict {accuracy, completeness, notes} — surfaced to the user, never blocks."""
    prompt = (
        "You are a senior auditor fact-checking a meeting summary against the source transcript. "
        "For every name, date, number, decision, and action-item owner in the SUMMARY, find the "
        "transcript segment that confirms or refutes it. Then score:\n"
        "- accuracy (0-5): are all facts/owners supported by the transcript? A mis-attributed "
        "action-item owner caps accuracy at 2.\n"
        "- completeness (0-5): are the meeting's real decisions/action items captured?\n"
        "Reply with ONLY JSON: {\"accuracy\": int, \"completeness\": int, \"notes\": \"one line on "
        "any unsupported or missing items\"}\n\n"
        f"SUMMARY:\n{json.dumps(summary_obj, ensure_ascii=False)}\n\n"
        f"TRANSCRIPT:\n{full_transcript[:60000]}")
    data = _json_obj(_converse(prompt, _summary_model(), max_tokens=2500,
                               system=system or _summary_system()))
    if not data:
        return {}
    return {"accuracy": data.get("accuracy"), "completeness": data.get("completeness"),
            "notes": data.get("notes", "")}


def _coerce_summary(obj):
    """Ensure all schema fields exist with the right types."""
    return {
        "conciseOverview": str(obj.get("conciseOverview", "") or ""),
        "narrativeSummary": str(obj.get("narrativeSummary", "") or ""),
        "decisions": obj.get("decisions") or [],
        "actionItems": obj.get("actionItems") or [],
        "announcements": obj.get("announcements") or [],
        "keyPoints": str(obj.get("keyPoints", "") or ""),
    }


def _extractive_summary(segs):
    """Local-only fallback: a crude but useful structured summary with no model call."""
    lines = [s["text"] for s in segs]
    overview = f"Meeting with {len(segs)} captured lines."
    decisions = [t for t in lines if re.search(r"\b(decide|agreed|conclusion|will go with)\b", t, re.I)][:8]
    actions = []
    for t in lines:
        if re.search(r"\b(will|should|need to|action|todo|take care of|follow up)\b", t, re.I):
            actions.append({"action": t.strip()[:160], "owner": "TBD", "deadline": "TBD"})
    return {"conciseOverview": overview,
            "narrativeSummary": " ".join(lines[:3])[:400],
            "decisions": decisions,
            "actionItems": actions[:12],
            "announcements": [],
            "keyPoints": "#### Transcript captured locally\n" +
                         "\n".join(f"- {t}" for t in lines[:20])}


def _render_summary_md(obj):
    """Render the summary dict to Markdown for display/export."""
    md = []
    if obj.get("conciseOverview"):
        md.append(f"**{obj['conciseOverview']}**\n")
    if obj.get("narrativeSummary"):
        md.append(obj["narrativeSummary"] + "\n")
    if obj.get("decisions"):
        md.append("## Decisions")
        md += [f"- {d}" for d in obj["decisions"]]
        md.append("")
    if obj.get("actionItems"):
        md.append("## Action Items")
        for a in obj["actionItems"]:
            owner = a.get("owner", "TBD")
            deadline = a.get("deadline", "TBD")
            md.append(f"- **{a.get('action', '')}** — {owner} ({deadline})")
        md.append("")
    if obj.get("announcements"):
        md.append("## Announcements")
        md += [f"- {a}" for a in obj["announcements"]]
        md.append("")
    if obj.get("keyPoints"):
        md.append("## Key Points")
        md.append(obj["keyPoints"].replace("\\n", "\n"))
    ev = obj.get("evaluation") or {}
    if ev.get("accuracy") is not None:
        md.append(f"\n_Quality check — accuracy {ev.get('accuracy')}/5, "
                  f"completeness {ev.get('completeness')}/5. {ev.get('notes', '')}_")
    return "\n".join(md).strip()


if __name__ == "__main__":
    import sys
    mid = int(sys.argv[1]) if len(sys.argv) > 1 else None
    if mid:
        print(json.dumps(generate_summary(mid), indent=2, ensure_ascii=False))
