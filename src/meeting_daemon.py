"""Meeting Mode auto-daemon — watches for a Zoom meeting with captions and starts/stops
capture automatically, so you never have to open the portal to trigger it.

Designed to be MEMORY-LIGHT while idle: it does NOT load Quartz/Cocoa. It only runs
cheap `pgrep` checks (~a few MB resident) on a slow interval, and ONLY launches the real
caption-capture process (meeting.py, which loads Quartz and uses ~280MB) once a meeting
with captions is actually detected. When the meeting ends, meeting.py exits on its own
(auto-split logic) and the memory is returned — so at idle the footprint is negligible.

Detection signal: Zoom spawns a 'CptHost' helper process when the Live Caption window is
open. Presence of CptHost (plus the zoom.us app) = "in a meeting with captions on" = the
moment to capture. This is far cheaper than polling the Accessibility tree.

Run:  python3 meeting_daemon.py        (or via launchd — see install_service.sh)
"""
import os
import subprocess
import sys
import time

import config

SRC = os.path.dirname(os.path.abspath(__file__))
PY = sys.executable
CHECK_INTERVAL = 10.0   # seconds between idle checks (cheap pgrep)
_proc = None            # the running meeting.py capture process, if any


def _pgrep(pattern):
    """True if any process matches (cheap; no Quartz). Uses pgrep -f."""
    return subprocess.run(["pgrep", "-f", pattern],
                          capture_output=True).returncode == 0


def _zoom_running():
    return _pgrep("zoom.us") or _pgrep("ZoomCefHelper")


def _captions_open():
    """Zoom's caption host process exists only while the Live Caption window is open."""
    return _pgrep("CptHost") or _pgrep("caption")


def _capture_alive():
    global _proc
    return _proc is not None and _proc.poll() is None


def _start_capture():
    global _proc
    if _capture_alive():
        return
    print(f"[{time.strftime('%H:%M:%S')}] caption window detected → starting capture",
          flush=True)
    log = open(os.path.join(config.LOGS_DIR, "meeting.log"), "a") \
        if os.path.isdir(config.LOGS_DIR) else subprocess.DEVNULL
    # detached so it survives even if this daemon is restarted; meeting.py's supervisor
    # loop handles its own meeting-end + summary, and exits when captions stop.
    _proc = subprocess.Popen([PY, os.path.join(SRC, "meeting.py"), "--source", "auto"],
                             cwd=SRC, stdout=log, stderr=log, start_new_session=True)


def run():
    if not config.load_config().get("meeting", {}).get("auto_capture", True):
        print("auto_capture is disabled in config; daemon idle.", flush=True)
    print(f"[{time.strftime('%H:%M:%S')}] SubWatch meeting daemon up — watching for "
          f"Zoom captions every {CHECK_INTERVAL:.0f}s (idle footprint ~a few MB).",
          flush=True)
    grace = 0  # count consecutive checks with captions gone, to debounce brief drops
    while True:
        try:
            cfg = config.load_config().get("meeting", {})
            if cfg.get("auto_capture", True) and _zoom_running() and _captions_open():
                grace = 0
                if not _capture_alive():
                    _start_capture()
            else:
                # captions/meeting gone — meeting.py auto-splits & exits on its own when
                # the meeting window disappears, so we just let it wind down. We don't
                # kill it (it needs to finish its summary).
                grace += 1
        except Exception as exc:  # noqa: BLE001 — never let the daemon die
            print(f"[daemon error] {exc}", flush=True)
        time.sleep(CHECK_INTERVAL)


if __name__ == "__main__":
    try:
        run()
    except KeyboardInterrupt:
        print("\ndaemon stopped.")
