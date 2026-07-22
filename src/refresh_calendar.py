"""Refresh db/calendar_cache.json from the local Microsoft Outlook app (macOS) via
AppleScript, so Meeting Mode can auto-title sessions and pull the attendee roster
without any cloud call.

Outlook on this Mac is scriptable; we query a bounded window (a few days around now)
to keep it fast on a large mailbox. Writes the same shape meeting_calendar.py reads.
Falls back silently (leaving any existing cache in place) if Outlook isn't available.

Run:  python3 refresh_calendar.py          # refresh now
The meeting driver calls maybe_refresh() on a freshness interval.
"""
import json
import os
import subprocess
import time

import config

CACHE_PATH = os.path.join(config.DB_DIR, "calendar_cache.json")
_MAX_AGE = 30 * 60  # refresh if the cache is older than 30 minutes

# AppleScript: emit events in a window as TSV (subject \t start \t end \t organizer \t
# attendees(';'-joined) \t location). Uses a record-separator so Python can split safely.
_SCRIPT = r'''
on isoOf(d)
    set {y, m, dd, h, mm} to {year of d, month of d as integer, day of d, hours of d, minutes of d}
    return (y as string) & "-" & text -2 thru -1 of ("0" & (m as string)) & "-" & text -2 thru -1 of ("0" & (dd as string)) & "T" & text -2 thru -1 of ("0" & (h as string)) & ":" & text -2 thru -1 of ("0" & (mm as string)) & ":00"
end isoOf

tell application "Microsoft Outlook"
    set lo to (current date) - (1 * days)
    set hi to (current date) + (3 * days)
    set evs to (every calendar event whose start time ≥ lo and start time ≤ hi)
    set output to ""
    repeat with e in evs
        try
            set subj to subject of e
        on error
            set subj to "(no subject)"
        end try
        try
            set loc to location of e
        on error
            set loc to ""
        end try
        try
            set org to name of organizer of e
        on error
            set org to ""
        end try
        set attNames to ""
        try
            repeat with a in (attendees of e)
                set attNames to attNames & (name of a) & ";"
            end repeat
        end try
        set output to output & subj & tab & my isoOf(start time of e) & tab & my isoOf(end time of e) & tab & org & tab & attNames & tab & loc & linefeed
    end repeat
    return output
end tell
'''


def _local_iso(s):
    """Tag a naive 'YYYY-MM-DDThh:mm:ss' (local time from Outlook) with the local UTC
    offset so meeting_calendar parses it at the right absolute time."""
    try:
        import datetime
        dt = datetime.datetime.fromisoformat(s)
        local = datetime.datetime.now().astimezone().tzinfo
        return dt.replace(tzinfo=local).isoformat()
    except (ValueError, TypeError):
        return s


def refresh():
    """Query Outlook and rewrite the cache. Returns event count, or -1 on failure."""
    try:
        r = subprocess.run(["osascript", "-e", _SCRIPT],
                           capture_output=True, text=True, timeout=40)
    except (subprocess.TimeoutExpired, OSError):
        return -1
    if r.returncode != 0 or not r.stdout.strip():
        return -1
    import re
    events = []
    for line in r.stdout.splitlines():
        parts = line.split("\t")
        if len(parts) < 6:
            continue
        subj, start, end, org, atts, loc = parts[:6]
        m = re.search(r"/j/(\d+)", loc or "")
        events.append({
            "subject": subj.strip(),
            "start": _local_iso(start.strip()),
            "end": _local_iso(end.strip()),
            "organizer": org.strip(),
            "attendees": [a.strip() for a in atts.split(";") if a.strip()],
            "zoom_id": m.group(1) if m else None,
        })
    os.makedirs(config.DB_DIR, exist_ok=True)
    tmp = CACHE_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump({"events": events, "fetched_at": _local_iso(
            time.strftime("%Y-%m-%dT%H:%M:%S")), "source": "outlook_local"}, fh,
            ensure_ascii=False, indent=1)
    os.replace(tmp, CACHE_PATH)
    return len(events)


def maybe_refresh():
    """Refresh only if the cache is missing or stale (> _MAX_AGE). Best-effort: never
    raises, so the meeting driver can call it freely."""
    try:
        age = time.time() - os.path.getmtime(CACHE_PATH)
        if age < _MAX_AGE:
            return None
    except OSError:
        pass  # no cache yet → refresh
    try:
        return refresh()
    except Exception:  # noqa: BLE001
        return -1


if __name__ == "__main__":
    n = refresh()
    print(f"refreshed {n} events to {CACHE_PATH}" if n >= 0
          else "Outlook refresh failed (is Microsoft Outlook running?)")
