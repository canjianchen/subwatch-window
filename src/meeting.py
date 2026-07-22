"""Meeting Mode driver — the process the panel spawns/stops like watch.py.

Pipeline:
  Zoom captions (AX primary → OCR fallback → audio/Whisper fallback)
    → meeting_assemble.Assembler (clean, dedup'd, timestamped segments)
    → meeting_store (persistent SQLite transcript)
    → meeting_live.json (rolling feed the panel polls for the live view)
  plus background threads that, on a slow cadence, build RAG chunks and refresh the
  running AI notes (Codex). On a clean stop (SIGTERM/SIGINT) it flips the meeting to
  'ended' and generates the deep post-meeting summary.

Run:
  python3 meeting.py                 # auto source (AX→OCR→audio), new meeting
  python3 meeting.py --source ax     # force a capture source
  python3 meeting.py --meeting-id 5  # attach to / resume an existing meeting row
  python3 meeting.py --once          # one capture pass then exit (test)
"""
import argparse
import json
import os
import signal
import sys
import threading
import time

import config
import meeting_store as store
import meeting_assemble
import meeting_rag

LIVE_PATH = os.path.join(config.DB_DIR, "meeting_live.json")
_STOP = threading.Event()    # user pressed Stop / SIGTERM — exit entirely
_ENDED = None                # per-meeting "this meeting ended, roll to the next" flag
                             # (set to a threading.Event in auto-split mode, else None)


def _meeting_cfg():
    return config.load_config().get("meeting", {}) or {}


def _stopping():
    """True when the CURRENT meeting's capture should stop — either the user pressed Stop
    (_STOP, exits everything) or this meeting ended for auto-split rollover (_ENDED)."""
    return _STOP.is_set() or (_ENDED is not None and _ENDED.is_set())


# ── live JSON feed (panel polls this for the live transcript view) ────────────
def _publish_live(meeting_id):
    """Write the current transcript (finalized + the in-progress partial) + latest notes
    to meeting_live.json atomically, so the panel renders the live view with no dups."""
    try:
        meeting = store.get_meeting(meeting_id)
        started_at = meeting["started_at"] if meeting else time.time()
        segs = store.all_segments(meeting_id, include_partial=True)
        # FULL transcript start→end (no truncation), then the in-progress partial if newer
        finals = [s for s in segs if not s["is_partial"]]
        partials = [s for s in segs if s["is_partial"]]
        lines = [{"seq": s["seq"], "speaker": s["speaker"], "text": s["text"],
                  "t": s["t_start_ms"], "partial": False} for s in finals]
        if partials:
            p = partials[-1]
            if not finals or p["seq"] >= finals[-1]["seq"]:
                lines.append({"seq": p["seq"], "speaker": p["speaker"], "text": p["text"],
                              "t": p["t_start_ms"], "partial": True})
        notes = store.latest_notes(meeting_id)
        payload = {
            "meeting_id": meeting_id,
            "started_at": started_at,   # epoch seconds → UI renders wall-clock (local TZ)
            "title": (meeting["title"] if meeting else ""),
            "lines": lines,
            "notes": (notes["bullets"] if notes else ""),
            "action_items": (json.loads(notes["action_items"]) if notes and notes["action_items"] else []),
            "decisions": (json.loads(notes["decisions"]) if notes and notes["decisions"] else []),
            "ts": time.time(),
        }
        os.makedirs(config.DB_DIR, exist_ok=True)
        tmp = LIVE_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False)
        os.replace(tmp, LIVE_PATH)
    except (OSError, ValueError):
        pass


# ── capture sources ──────────────────────────────────────────────────────────
def _capture_ax(asm, meeting_id, poll_hz, once):
    """Primary: poll Zoom's Accessibility tree for caption lines."""
    import zoom_ax
    if not zoom_ax.available() or not zoom_ax.trusted():
        return False
    pid = zoom_ax.zoom_pid()
    if not pid:
        print("  zoom.us not running — cannot use AX capture.", flush=True)
        return False
    interval = 1.0 / max(1.0, poll_hz)
    print(f"  📋 AX capture: zoom.us pid={pid}, {poll_hz}Hz", flush=True)
    last_speaker = None
    while not _stopping():
        # The moment a caption (CC) window is open, OCR-by-window-id is the reliable
        # source (Zoom renders captions in a surface AX can't read on 7.0.5). The user
        # often turns CC on a few minutes INTO the call, so re-check every poll and hand
        # off — otherwise we'd stay stuck in AX and miss the whole conversation.
        if not once and zoom_ax.caption_window_id():
            print("  caption window appeared → switching to OCR capture.", flush=True)
            return False
        lines = zoom_ax.read_caption_lines(pid=pid)
        # keep only plausible caption lines (drop one-word UI labels)
        lines = [l for l in lines if len(l) > 3]
        if lines:
            # attribute to Zoom's current active speaker, same as the OCR path — without
            # this every AX-captured line lands as Unknown ('?').
            speaker = zoom_ax.active_speaker() or last_speaker
            if speaker:
                last_speaker = speaker
            asm.push_frame(lines, speaker_hint=speaker)
            _publish_live(meeting_id)
        if once:
            break
        _STOP.wait(interval)
    return True


def _capture_ocr(asm, meeting_id, poll_hz, once):
    """OCR the Zoom caption window BY WINDOW ID. This is the reliable caption source:
    Zoom renders captions in a surface the Accessibility API can't read, but the window
    can be captured by id via Quartz regardless of display/Retina/occlusion. Re-resolves
    the window id each poll so it survives the caption window being reopened/moved."""
    import zoom_ax
    import ocr
    interval = 1.0 / max(1.0, poll_hz)
    print("  🖼  OCR capture of the Zoom caption window (by window id)", flush=True)
    warned = False
    saw_meeting = zoom_ax.in_meeting()
    gone_polls = 0
    last_wid = None
    last_speaker = None
    while not _stopping():
        # Meeting-end detection for auto-split: once we've seen a meeting, if Zoom's meeting
        # window stays gone for a sustained window, this meeting ended → signal rollover.
        if _ENDED is not None:
            if zoom_ax.in_meeting():
                saw_meeting = True
                gone_polls = 0
            elif saw_meeting:
                gone_polls += 1
                if gone_polls > poll_hz * 6:  # ~6s with no Zoom meeting window
                    print("  meeting ended (Zoom meeting window gone) → finalizing.", flush=True)
                    _ENDED.set()
                    return True
        wid = zoom_ax.caption_window_id()
        # multi-screen / moved-window awareness: log when the caption window id changes
        # (e.g. you dragged it to another monitor). Capture-by-id follows it automatically.
        if wid and wid != last_wid:
            if last_wid is not None:
                print(f"  ↪ caption window moved/reopened (id {last_wid}→{wid}) — still tracking.",
                      flush=True)
            last_wid = wid
        texts = []
        if wid:
            lines = ocr.grab_window_text(wid)
            texts = [l.get("text", "") for l in (lines or []) if l.get("text")]
        else:
            if not warned:
                print("  (no Zoom caption window found — open Zoom's Live Caption (CC) "
                      "window to capture.)", flush=True)
                warned = True
        if texts:
            warned = False
            # attribute lines to whoever Zoom currently shows as the active speaker
            # (the caption panel only shows avatars, so this AX signal is the name source).
            # Remember the last known speaker so brief gaps between utterances (when Zoom
            # momentarily reports nobody talking) don't drop attribution to Unknown.
            speaker = zoom_ax.active_speaker() or last_speaker
            if speaker:
                last_speaker = speaker
            asm.push_frame(texts, speaker_hint=speaker)
            _publish_live(meeting_id)
        if once:
            break
        _STOP.wait(interval)
    return True


def _capture_audio(asm, meeting_id, once):
    """Last resort: system audio → Whisper → final chunks into the assembler."""
    try:
        import audio_listen
        import numpy as np
        import sounddevice as sd
    except Exception as exc:  # noqa: BLE001
        print(f"  audio capture unavailable: {exc}", flush=True)
        return False
    dev_idx, dev_name = audio_listen._pick_device(None)
    if dev_idx is None:
        print("  no system-audio device (BlackHole) — audio fallback unavailable.", flush=True)
        return False
    print(f"  🎧 audio capture via {dev_name} (Whisper)", flush=True)
    model = audio_listen._load_model("base")
    block = int(audio_listen.CHUNK_SECONDS * audio_listen.SAMPLE_RATE)
    while not _stopping():
        rec = sd.rec(block, samplerate=audio_listen.SAMPLE_RATE, channels=1,
                     device=dev_idx, dtype="float32")
        sd.wait()
        mono = rec.reshape(-1)
        if float(np.sqrt(np.mean(mono ** 2))) < audio_listen.SILENCE_RMS:
            if once:
                break
            continue
        text, _lang = audio_listen._transcribe(model, mono, translate=False)
        if text:
            asm.push_final(text)
            _publish_live(meeting_id)
        if once:
            break
    return True


# ── background AI workers ─────────────────────────────────────────────────────
def _notes_worker(meeting_id):
    """Refresh RAG chunks + running notes on a slow, debounced cadence so the AI never
    blocks capture and cost stays bounded."""
    mc = _meeting_cfg()
    every = float(mc.get("notes_interval_seconds", 45))
    while not _stopping():
        if _STOP.wait(every):
            break
        if _stopping():
            break
        try:
            meeting_rag.flush_chunks(meeting_id)
            updated = meeting_rag.update_notes(meeting_id)
            if updated:
                _publish_live(meeting_id)
                print(f"  📝 notes refreshed (through seq {updated['upto_seq']})", flush=True)
        except Exception as exc:  # noqa: BLE001 — never kill the worker
            print(f"  (notes worker error: {exc})", flush=True)


def _screen_worker(meeting_id, started_at):
    """When someone screen-shares, OCR the shared content on a slow cadence and store
    distinct text blocks as context segments. This lets the summary/chat reference what
    was ON SCREEN (slides, docs, CRs) — something audio-only tools can't do. Best-effort
    and de-duplicated so a static slide isn't stored repeatedly."""
    import zoom_ax
    import ocr
    if not _meeting_cfg().get("capture_shared_screen", True):
        return
    every = 18.0
    while not _stopping():
        if _STOP.wait(every):
            break
        if _stopping():
            break
        try:
            if not zoom_ax.is_screen_sharing():
                continue
            wid = zoom_ax.shared_screen_window_id()
            if not wid:
                continue
            lines = ocr.grab_window_text(wid)
            # keep only document-like text: several real words, drop UI chrome
            texts = [l.get("text", "") for l in (lines or []) if l.get("text")]
            blob = " ".join(t.strip() for t in texts if len(t.split()) >= 2)
            blob = " ".join(blob.split())
            if len(blob.split()) < 8:
                continue  # not enough content to be a meaningful slide/doc
            t_ms = int((time.time() - started_at) * 1000)
            if store.add_screen_capture(meeting_id, blob[:2000], t_ms):
                _publish_live(meeting_id)
                print(f"  🖥  captured shared-screen content ({len(blob.split())} words)",
                      flush=True)
        except Exception as exc:  # noqa: BLE001
            print(f"  (screen worker error: {exc})", flush=True)


# ── driver ─────────────────────────────────────────────────────────────────--
def run(meeting_id=None, source="auto", once=False, autosplit=True):
    """Supervisor: capture one meeting; if auto-split is on, when that meeting ends we
    summarize it and AUTOMATICALLY start a fresh meeting record for the next Zoom call —
    so switching meetings gives each its own trackable section without any clicks. The
    user pressing Stop (SIGTERM → _STOP) exits the whole supervisor."""
    global _ENDED
    store.init_db()
    first = True
    while not _STOP.is_set():
        mid = meeting_id if first else None
        ended_naturally = _capture_one_meeting(mid, source, once)
        first = False
        if once or _STOP.is_set():
            break
        # this meeting wrapped up — finalize + summarize it
        if not autosplit:
            break
        if not ended_naturally:
            break  # capture returned for a non-meeting-end reason; don't spin
        # auto-split: wait until a NEW meeting starts, then loop to capture it fresh
        print("⏸  Waiting for the next meeting…", flush=True)
        import zoom_ax
        while not _STOP.is_set():
            if zoom_ax.in_meeting():
                print("▶️  New meeting detected — starting a fresh section.", flush=True)
                break
            _STOP.wait(2.0)
    return


def _capture_one_meeting(meeting_id, source, once):
    """Capture a single meeting end-to-end. Returns True if it ended because the Zoom
    meeting closed (auto-split should roll to the next), False otherwise. On a clean end
    it marks the meeting 'ended' and generates the summary."""
    global _ENDED
    _ENDED = threading.Event() if (not once) else None
    # Calendar awareness: match the current Outlook event to auto-name the session and
    # get the real attendee roster (used to correct OCR'd speaker names).
    roster = []
    try:
        import meeting_calendar
        cal = meeting_calendar.enrich_for_now()
        if cal.get("matched"):
            roster = cal.get("attendees", [])
            print(f"  📅 calendar: '{cal['title']}' ({len(roster)} attendees)", flush=True)
    except Exception:  # noqa: BLE001 — calendar is best-effort
        cal = {"matched": False}
    if meeting_id is None:
        # Title from the calendar event + the time, so the Past Meetings list reads
        # "XCM Tech Daily Standup — Jun 26 10:00 AM". Falls back to a timestamp title.
        title = None
        if cal.get("matched") and cal.get("title"):
            title = f"{cal['title']} — {time.strftime('%b %d %-I:%M %p')}"
        meeting_id = store.create_meeting(title=title, source=source)
        print(f"▶️  Meeting #{meeting_id} started"
              + (f" — {title}" if title else f" (source={source})") + ".", flush=True)
    else:
        print(f"▶️  Attaching to meeting #{meeting_id}.", flush=True)

    m = store.get_meeting(meeting_id)
    started_at = m["started_at"] if m else time.time()
    mc = _meeting_cfg()
    poll_hz = float(mc.get("poll_hz", 3))

    # The assembler resumes seq numbering after whatever's already stored.
    base_seq = store.max_seq(meeting_id) + 1

    def on_segment(seg):
        store.upsert_segment(
            meeting_id, seq=base_seq + seg["seq"], text=seg["text"], norm=seg["norm"],
            t_start_ms=seg["t_start_ms"], t_end_ms=seg["t_end_ms"],
            speaker=seg.get("speaker"), is_partial=seg["is_partial"],
            result_id=seg.get("result_id"))

    asm = meeting_assemble.Assembler(on_segment=on_segment, started_at=started_at,
                                     cfg=mc, roster=roster)

    # background workers (skip in --once): running notes, and shared-screen OCR capture
    if not once:
        threading.Thread(target=_notes_worker, args=(meeting_id,), daemon=True).start()
        threading.Thread(target=_screen_worker, args=(meeting_id, started_at),
                         daemon=True).start()

    _publish_live(meeting_id)

    # capture: try sources in order unless one is forced.
    # AUTO: if Zoom's caption window is already on screen, go straight to OCR-by-window-id
    # (Zoom renders captions in a surface the Accessibility API can't read, confirmed on
    # 7.0.5). Otherwise try AX first (cheaper if a future Zoom exposes captions), then OCR,
    # then audio.
    if source == "auto":
        try:
            import zoom_ax
            if zoom_ax.caption_window_id():
                order = [_capture_ocr, _capture_ax, _capture_audio]
                print("  caption window detected → OCR capture.", flush=True)
            else:
                order = [_capture_ax, _capture_ocr, _capture_audio]
        except Exception:  # noqa: BLE001
            order = [_capture_ax, _capture_ocr, _capture_audio]
    else:
        order = {"ax": [_capture_ax], "ocr": [_capture_ocr],
                 "audio": [_capture_audio]}.get(source, [_capture_ax, _capture_ocr, _capture_audio])
    try:
        for fn in order:
            if _STOP.is_set():
                break
            if fn is _capture_audio:
                ok = fn(asm, meeting_id, once)
            else:
                ok = fn(asm, meeting_id, poll_hz, once)
            if ok:
                break
    except KeyboardInterrupt:
        pass
    finally:
        asm.flush()
        _publish_live(meeting_id)

    if once:
        # in --once we still want any captured line persisted; no summary.
        print(f"  (once) finalized {store.max_seq(meeting_id) + 1} segment slot(s).", flush=True)
        return False

    # whether capture returned because the Zoom meeting ended (auto-split rollover) vs the
    # user pressing Stop.
    meeting_ended = _ENDED is not None and _ENDED.is_set()

    # end + deep summary
    print("⏹  Finalizing transcript and generating summary…", flush=True)
    store.end_meeting(meeting_id)
    try:
        meeting_rag.flush_chunks(meeting_id)
        # Title = scheduled (calendar) name + an AI gist FROM THE TRANSCRIPT. The gist is
        # ground truth — if the calendar mis-matched (e.g. a HOLD block while you were
        # actually on a personal call), the transcript-derived part reveals the real topic.
        gist = meeting_rag.generate_title(meeting_id)
        sched = cal.get("title") if cal.get("matched") else None
        when = time.strftime('%b %d %-I:%M %p', time.localtime(started_at))
        if sched and gist:
            new_title = f"{sched}: {gist} — {when}"
        elif gist:
            new_title = f"{gist} — {when}"
        elif sched:
            new_title = f"{sched} — {when}"
        else:
            new_title = None
        if new_title:
            store.set_title(meeting_id, new_title)
            print(f"  🏷  {new_title}", flush=True)
        summary = meeting_rag.generate_summary(meeting_id)
        ev = summary.get("evaluation") or {}
        print(f"✅ Summary saved (accuracy {ev.get('accuracy', '?')}/5). "
              f"{len(summary.get('actionItems', []))} action item(s).", flush=True)
    except Exception as exc:  # noqa: BLE001
        print(f"  (summary error: {exc})", flush=True)
    _publish_live(meeting_id)
    return meeting_ended


def _install_signal_handlers():
    def _stop(_signum, _frame):
        _STOP.set()
    signal.signal(signal.SIGINT, _stop)
    signal.signal(signal.SIGTERM, _stop)


def main():
    parser = argparse.ArgumentParser(description="SubWatch Meeting Mode driver")
    parser.add_argument("--source", default="auto", choices=["auto", "ax", "ocr", "audio"])
    parser.add_argument("--meeting-id", type=int, default=None)
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()
    _install_signal_handlers()
    run(meeting_id=args.meeting_id, source=args.source, once=args.once)


if __name__ == "__main__":
    sys.exit(main())
