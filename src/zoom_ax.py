"""Zoom live-caption reader via the macOS Accessibility (AX) API.

The primary capture source for Meeting Mode. Zoom 7.x renders its UI with an
AX-friendly toolkit, and exposes the live caption surfaces as readable text:
  • the rolling floating "Subtitle" window, and
  • the "View Full Transcript" side panel (whole-meeting scrollback, WITH speaker
    names) — the richest target.

Reading AX text returns the EXACT source strings (no OCR error on names/numbers),
includes speaker labels for free, costs ~8 ms/poll, and needs no screenshots — only
the Accessibility permission (which the user grants once to the terminal/app).

This module is read-only and best-effort: every function degrades to None/[] rather
than raising, so the capture loop never dies on an AX hiccup. If AX yields nothing
(rare), the caller falls back to OCR of the caption window (see meeting_capture).

Empirically validated on Zoom 7.0.5 (build 81138). Bridging note: the system python
has pyobjc Cocoa/Quartz but NOT pyobjc-framework-ApplicationServices, so we load the
AX functions directly from the framework bundle and register the AXUIElementRef
signature — without that registration AXUIElementCopyAttributeValue returns None.
"""
import sys

# ── AX bridge ────────────────────────────────────────────────────────────────
_AX_OK = False
try:
    import objc
    import Quartz

    # The opaque AXUIElementRef must be registered before any AX call, or the
    # bridge can't marshal it and every AXUIElementCopyAttributeValue returns None.
    objc.registerCFSignature("AXUIElementRef", b"^{__AXUIElement=}",
                             id(objc.lookUpClass("NSObject")))
    _AS = objc.loadBundle(
        "ApplicationServices", globals(),
        bundle_path="/System/Library/Frameworks/ApplicationServices.framework")
    objc.loadBundleFunctions(_AS, globals(), [
        ("AXIsProcessTrusted", b"Z"),
        ("AXUIElementCreateApplication", b"^{__AXUIElement=}i"),
        ("AXUIElementCopyAttributeValue", b"i^{__AXUIElement=}@o^@"),
    ])
    _AX_OK = True
except Exception:  # noqa: BLE001 — non-macOS or missing pyobjc; callers handle _AX_OK
    _AX_OK = False


# Roles whose AXValue carries displayed text.
_TEXT_ROLES = ("AXStaticText", "AXTextArea", "AXTextField")
# Window titles / element labels that signal the caption surfaces (matched loosely,
# case-insensitive substring) so we can prefer them when harvesting.
_CAPTION_HINTS = ("caption", "subtitle", "transcript", "closed caption", "live transcript")


def available():
    """True if the AX bridge loaded (macOS + pyobjc present)."""
    return _AX_OK


def trusted():
    """True if this process has Accessibility permission (else AX returns nothing)."""
    if not _AX_OK:
        return False
    try:
        return bool(AXIsProcessTrusted())  # noqa: F821 — loaded into globals above
    except Exception:  # noqa: BLE001
        return False


def _get(el, attr):
    """Read one AX attribute, returning the value or None on any error."""
    if el is None:
        return None
    try:
        err, val = AXUIElementCopyAttributeValue(el, attr, None)  # noqa: F821
        return val if err == 0 else None
    except Exception:  # noqa: BLE001
        return None


def zoom_pid():
    """PID of the running zoom.us app, or None. Uses the on-screen+offscreen window
    list so it works even before any window is frontmost."""
    if not _AX_OK:
        return None
    try:
        info = Quartz.CGWindowListCopyWindowInfo(
            Quartz.kCGWindowListOptionAll, Quartz.kCGNullWindowID) or []
    except Exception:  # noqa: BLE001
        return None
    for w in info:
        if (w.get("kCGWindowOwnerName") or "").lower() == "zoom.us":
            return w.get("kCGWindowOwnerPID")
    return None


def _app_element(pid):
    try:
        return AXUIElementCreateApplication(pid)  # noqa: F821
    except Exception:  # noqa: BLE001
        return None


def _harvest_text(el, out, depth=0, budget=None):
    """Recursively collect non-empty text-role AXValues under `el`, in tree order.
    Bounded in depth and node count so a pathological tree can't hang the poll."""
    if el is None or depth > 24:
        return
    if budget is None:
        budget = [0]
    budget[0] += 1
    if budget[0] > 8000:
        return
    role = _get(el, "AXRole")
    if str(role) in _TEXT_ROLES:
        val = _get(el, "AXValue")
        if val:
            text = str(val).strip()
            # skip empty / single-glyph noise and AX placeholder reprs like "<...>"
            if len(text) > 2 and not text.startswith("<"):
                out.append(text)
    for child in (_get(el, "AXChildren") or []):
        _harvest_text(child, out, depth + 1, budget)


def _window_caption_score(window):
    """Heuristic: how likely this window is a caption/transcript surface (higher =
    more likely), from its title/AXDescription. Used to prefer caption windows."""
    title = (str(_get(window, "AXTitle") or "") + " "
             + str(_get(window, "AXDescription") or "")).lower()
    return sum(1 for hint in _CAPTION_HINTS if hint in title)


def read_caption_lines(app=None, pid=None):
    """Return the current visible caption/transcript lines from Zoom, in display
    order (top→bottom). Best-effort: [] if Zoom isn't running, AX isn't trusted, or
    no caption surface is open.

    Strategy: harvest text ONLY from windows that score as a caption/transcript surface.
    We do NOT fall back to harvesting all Zoom windows — that pulls in pure UI chrome
    ('Settings', 'editor', meeting titles, participant names, email addresses) which the
    assembler then commits as if it were speech. If no caption surface is open we return
    [] so the driver falls through to the OCR/audio path instead of capturing garbage."""
    if not _AX_OK:
        return []
    if app is None:
        pid = pid or zoom_pid()
        if not pid:
            return []
        app = _app_element(pid)
        if app is None:
            return []
    windows = _get(app, "AXWindows") or []
    if not windows:
        return []

    scored = [(w, _window_caption_score(w)) for w in windows]
    caption_windows = [w for w, s in scored if s > 0]
    if not caption_windows:
        return []  # no caption surface → no AX captions (don't harvest chrome)
    targets = caption_windows

    lines = []
    for w in targets:
        _harvest_text(w, lines)
    # collapse exact consecutive duplicates the tree sometimes yields
    out = []
    for line in lines:
        if not out or out[-1] != line:
            out.append(line)
    return out


def active_speaker(app=None, pid=None):
    """Return the name of the CURRENT active speaker from Zoom's participant tiles, or
    None. Zoom exposes each tile's state in its AX description, e.g.
    'Runyou Wang, Computer audio unmuted, Video on, active speaker'. This is how we
    attribute caption lines to a speaker — the caption window itself only shows avatars,
    not names (same approach AiMS uses: map active-speaker events, not voice/diarization)."""
    if not _AX_OK:
        return None
    if app is None:
        pid = pid or zoom_pid()
        if not pid:
            return None
        app = _app_element(pid)
        if app is None:
            return None

    found = []
    budget = [0]  # fresh per call (NOT a mutable default arg, which would persist + exhaust)

    def _walk(el, depth=0):
        if el is None or depth > 28 or budget[0] > 8000:
            return
        budget[0] += 1
        role = str(_get(el, "AXRole"))
        # Source 1 (gallery view): a participant tile flagged 'active speaker', e.g.
        # 'Nick Kilian, Computer audio unmuted, Video on, active speaker'
        if role == "AXTabGroup":
            desc = _get(el, "AXDescription")
            if desc and "active speaker" in str(desc).lower():
                found.append(str(desc).split(",")[0].strip())
        # Source 2 (minimized/floating video window): a static text 'Talking: <name>'.
        # Zoom exposes the speaker here even when the gallery tiles aren't in the tree.
        elif role == "AXStaticText":
            val = _get(el, "AXValue")
            if val:
                s = str(val).strip()
                if s.lower().startswith("talking:"):
                    name = s.split(":", 1)[1].strip()
                    if name:
                        found.append(name)
        for child in (_get(el, "AXChildren") or []):
            _walk(child, depth + 1)
            if found:
                return

    for w in (_get(app, "AXWindows") or []):
        _walk(w)
        if found:
            break
    return found[0] if found else None


def all_participants(app=None, pid=None):
    """Return the list of participant names currently in the meeting (from the tiles)."""
    if not _AX_OK:
        return []
    if app is None:
        pid = pid or zoom_pid()
        app = _app_element(pid) if pid else None
        if app is None:
            return []
    names = []
    budget = [0]  # fresh per call (NOT a mutable default arg)

    def _walk(el, depth=0):
        if el is None or depth > 28 or budget[0] > 8000:
            return
        budget[0] += 1
        if str(_get(el, "AXRole")) == "AXTabGroup":
            desc = _get(el, "AXDescription")
            if desc:
                name = str(desc).split(",")[0].strip()
                if name and name not in names:
                    names.append(name)
        for child in (_get(el, "AXChildren") or []):
            _walk(child, depth + 1)

    for w in (_get(app, "AXWindows") or []):
        _walk(w)
    return names


def shared_screen_window_id():
    """Return the Quartz window id of Zoom's shared-content area when someone is screen
    sharing, else None. Heuristic: the large 'Zoom Meeting' / 'Zoom Share' window (the
    big content surface, not the small floating tiles). Used to OCR shared slides/docs/
    code as meeting context — something audio-only tools (AiMS) cannot capture."""
    if not _AX_OK:
        return None
    try:
        info = Quartz.CGWindowListCopyWindowInfo(
            Quartz.kCGWindowListOptionOnScreenOnly, Quartz.kCGNullWindowID) or []
    except Exception:  # noqa: BLE001
        return None
    best = None
    best_area = 0
    for w in info:
        if (w.get("kCGWindowOwnerName") or "").lower() != "zoom.us":
            continue
        name = (w.get("kCGWindowName") or "")
        # the content/share surface; skip caption/subtitle + tiny chrome windows
        if name in ("Zoom Meeting", "Zoom Share", "Zoom Shared Screen") or "share" in name.lower():
            b = w.get("kCGWindowBounds") or {}
            area = b.get("Width", 0) * b.get("Height", 0)
            # must be reasonably large (a real content surface, not a toolbar)
            if area > 300_000 and area > best_area:
                best, best_area = w.get("kCGWindowNumber"), area
    return best


def is_screen_sharing():
    """True if Zoom currently shows a 'sharing'/'view options' AX indicator."""
    if not _AX_OK:
        return False
    pid = zoom_pid()
    if not pid:
        return False
    app = _app_element(pid)
    found = [False]
    budget = [0]

    def _walk(el, depth=0):
        if el is None or depth > 24 or budget[0] > 5000 or found[0]:
            return
        budget[0] += 1
        for attr in ("AXDescription", "AXTitle", "AXValue"):
            v = _get(el, attr)
            if v and any(k in str(v).lower() for k in
                         ("view options", "stop share", "you are screen sharing",
                          "sharing screen", "viewing", "shared screen")):
                found[0] = True
                return
        for child in (_get(el, "AXChildren") or []):
            _walk(child, depth + 1)

    for w in (_get(app, "AXWindows") or []):
        _walk(w)
        if found[0]:
            break
    return found[0]


def in_meeting():
    """True if a Zoom MEETING is currently active (a 'Zoom Meeting' window exists). This
    is the boundary signal for auto-split: when it goes False the current meeting ended;
    when it goes True again a new meeting started. ('Zoom Workplace' is the idle home
    window and does NOT count.)"""
    if not _AX_OK:
        return False
    try:
        info = Quartz.CGWindowListCopyWindowInfo(
            Quartz.kCGWindowListOptionOnScreenOnly, Quartz.kCGNullWindowID) or []
    except Exception:  # noqa: BLE001
        return False
    for w in info:
        if (w.get("kCGWindowOwnerName") or "").lower() != "zoom.us":
            continue
        name = w.get("kCGWindowName") or ""
        if name == "Zoom Meeting" or "caption" in name.lower() or "subtitle" in name.lower():
            return True
    return False


def caption_window_id():
    """Return the Quartz window id of Zoom's caption window (owner zoom.us, name hinting
    at captions/subtitles), or None. Used to OCR that window by id — reliable across
    displays/Retina, unlike a region crop. Picks the caption-named window."""
    if not _AX_OK:
        return None
    try:
        info = Quartz.CGWindowListCopyWindowInfo(
            Quartz.kCGWindowListOptionOnScreenOnly, Quartz.kCGNullWindowID) or []
    except Exception:  # noqa: BLE001
        return None
    for w in info:
        if (w.get("kCGWindowOwnerName") or "").lower() != "zoom.us":
            continue
        if any(hint in (w.get("kCGWindowName") or "").lower() for hint in _CAPTION_HINTS):
            return w.get("kCGWindowNumber")
    return None


def caption_window_bounds(pid=None):
    """Return (x, y, w, h) of Zoom's caption/subtitle window for the OCR fallback, or
    None. Uses the Quartz window list (owner zoom.us) and picks the smallest on-screen
    window whose name hints at captions; falls back to None so the caller uses a
    configured region."""
    if not _AX_OK:
        return None
    try:
        info = Quartz.CGWindowListCopyWindowInfo(
            Quartz.kCGWindowListOptionOnScreenOnly
            | Quartz.kCGWindowListExcludeDesktopElements,
            Quartz.kCGNullWindowID) or []
    except Exception:  # noqa: BLE001
        return None
    candidates = []
    for w in info:
        if (w.get("kCGWindowOwnerName") or "").lower() != "zoom.us":
            continue
        name = (w.get("kCGWindowName") or "").lower()
        if any(hint in name for hint in _CAPTION_HINTS):
            b = w.get("kCGWindowBounds") or {}
            if b.get("Width") and b.get("Height"):
                candidates.append((b["Width"] * b["Height"],
                                   (int(b["X"]), int(b["Y"]),
                                    int(b["Width"]), int(b["Height"]))))
    if not candidates:
        return None
    candidates.sort(key=lambda c: c[0])  # smallest = the caption bar, not the main window
    return candidates[0][1]


def _diagnostic_dump():
    """Print the full role tree of every Zoom window — run during a meeting with CC +
    'View Full Transcript' open to confirm/lock onto the caption subtree."""
    if not trusted():
        print("Accessibility NOT granted to this process. Grant it in "
              "System Settings > Privacy & Security > Accessibility, then retry.")
        return
    pid = zoom_pid()
    if not pid:
        print("zoom.us is not running.")
        return
    app = _app_element(pid)
    windows = _get(app, "AXWindows") or []
    print(f"zoom.us pid={pid}, {len(windows)} window(s).")

    def walk(el, depth=0, lim=[0]):
        if el is None or depth > 24 or lim[0] > 4000:
            return
        lim[0] += 1
        role = _get(el, "AXRole")
        bits = []
        for nm, attr in (("val", "AXValue"), ("title", "AXTitle"), ("desc", "AXDescription")):
            v = _get(el, attr)
            if v is not None:
                s = str(v).strip()
                if s and not s.startswith("<"):
                    bits.append(f"{nm}={s[:70]!r}")
        print("  " * depth + f"{role} " + " ".join(bits))
        for child in (_get(el, "AXChildren") or []):
            walk(child, depth + 1, lim)

    for i, w in enumerate(windows):
        print(f"\n===== WINDOW {i}: title={_get(w, 'AXTitle')!r} =====")
        walk(w)


if __name__ == "__main__":
    # `python3 zoom_ax.py --dump` → diagnostic tree; otherwise poll-print caption lines.
    import time
    if "--dump" in sys.argv:
        _diagnostic_dump()
    else:
        if not trusted():
            print("Accessibility not granted; grant it and retry.")
            sys.exit(1)
        print("Polling Zoom captions (open CC / 'View Full Transcript'). Ctrl-C to stop.")
        seen = set()
        try:
            while True:
                for ln in read_caption_lines():
                    if ln not in seen:
                        seen.add(ln)
                        print(f"[{time.strftime('%H:%M:%S')}] {ln}")
                time.sleep(0.3)
        except KeyboardInterrupt:
            print("\nstopped.")
