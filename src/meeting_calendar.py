"""Calendar awareness for Meeting Mode.

Matches the meeting you're capturing to your Outlook calendar event, so SubWatch can:
  • auto-NAME the session with the real meeting title (not "Meeting 2026-06-26 10:42"),
  • know the real meeting TYPE from the title (standup / design review / 1:1 / …),
  • know the ATTENDEE ROSTER (to validate/correct OCR'd speaker names against real names),
  • know the organizer.

The calendar itself is fetched out-of-band (the SubWatch agent pulls it via the Outlook
/ Microsoft Graph MCP and writes db/calendar_cache.json); this module just reads that
cache and does the time-based matching. If there's no cache or no match, everything
degrades gracefully to the old behavior.
"""
import json
import os
import time

import config

CACHE_PATH = os.path.join(config.DB_DIR, "calendar_cache.json")


def _parse_iso(s):
    """Parse an ISO-8601 timestamp to epoch seconds. Handles 'Z' and offsets. Returns
    None on failure. Treats naive timestamps as UTC (Graph returns UTC 'Z')."""
    if not s:
        return None
    try:
        import datetime
        s = s.strip()
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        dt = datetime.datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=datetime.timezone.utc)
        return dt.timestamp()
    except (ValueError, TypeError):
        return None


def load_events():
    try:
        with open(CACHE_PATH, encoding="utf-8") as fh:
            return json.load(fh).get("events", [])
    except (OSError, json.JSONDecodeError):
        return []


def current_event(now=None):
    """Return the calendar event happening NOW (started, not yet ended, with a small
    grace window), preferring the one that best brackets the current time. None if no
    cache or no match. `now` is epoch seconds (defaults to wall clock)."""
    now = now if now is not None else time.time()
    grace = 5 * 60  # allow matching a few minutes before start / after end
    best = None
    best_span = None
    for e in load_events():
        start = _parse_iso(e.get("start"))
        end = _parse_iso(e.get("end"))
        if start is None or end is None:
            continue
        if start - grace <= now <= end + grace:
            span = end - start
            # prefer the SHORTEST matching event (the most specific; avoids an all-day
            # or long block swallowing a 30-min meeting)
            if best is None or span < best_span:
                best, best_span = e, span
    return best


def enrich_for_now(now=None):
    """Return {title, meeting_type, attendees, organizer, matched} for the meeting
    happening now, or a minimal dict with matched=False if the calendar can't help."""
    e = current_event(now)
    if not e:
        return {"matched": False, "title": None, "attendees": [], "organizer": None}
    return {
        "matched": True,
        "title": e.get("subject"),
        "attendees": _normalize_names(e.get("attendees", [])),
        "organizer": _flip_name(e.get("organizer", "")),
        "is_organizer": e.get("is_organizer", False),
    }


def _flip_name(name):
    """Outlook gives 'Last, First' — flip to 'First Last' to match Zoom caption names."""
    name = (name or "").strip()
    if "," in name:
        last, first = [p.strip() for p in name.split(",", 1)]
        return f"{first} {last}".strip()
    return name


def _normalize_names(names):
    return [_flip_name(n) for n in (names or []) if n]


if __name__ == "__main__":
    import sys
    e = current_event()
    if e:
        print("NOW:", e["subject"])
        print("  attendees:", _normalize_names(e.get("attendees", [])))
        print("  organizer:", _flip_name(e.get("organizer", "")))
    else:
        print("no calendar event matches now (cache:", CACHE_PATH, ")")
