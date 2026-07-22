"""Screen capture + OCR bridge.

Dispatches to a platform backend:
  - macOS  -> screencapture + the compiled Swift Vision helper (bin/ocr_helper)
  - Windows -> mss screen grab + Windows.Media.Ocr (via winrt), Chinese+English

Both backends return the same shape from capture_text(): a list of
{text, confidence, x, y, w, h} dicts ordered top-to-bottom, so the rest of the
app (detector, watch loop, store) is platform-agnostic.
"""
import json
import os
import subprocess
import sys
import tempfile

import config

IS_MAC = sys.platform == "darwin"
IS_WINDOWS = os.name == "nt"


# ---------------------------------------------------------------------------
# macOS backend (Apple Vision)
# ---------------------------------------------------------------------------
def _mac_crop(path, region):
    """Crop a PNG in-place to [x, y, w, h] using sips (no extra dependency)."""
    x, y, w, h = region
    subprocess.run(
        ["sips", "--cropToHeightWidth", str(h), str(w), "--cropOffset", str(y), str(x), path],
        capture_output=True,
    )


def _mac_display_bounds(display):
    """Return the CGRect bounds of a 1-based display index (matching screencapture
    -D numbering = NSScreen order), or None for the whole desktop."""
    if not display:
        return None
    try:
        from AppKit import NSScreen
        import Quartz
    except ImportError:
        return None
    screens = NSScreen.screens()
    index = display - 1
    if index < 0 or index >= len(screens):
        return None
    frame = screens[index].frame()
    # NSScreen is bottom-left origin; Quartz CGRect for capture is top-left. Flip Y
    # using the primary screen height (screens[0] is the menu-bar/primary screen).
    primary_h = screens[0].frame().size.height
    top = primary_h - (frame.origin.y + frame.size.height)
    return Quartz.CGRectMake(frame.origin.x, top, frame.size.width, frame.size.height)


def _mac_grab_below_window(window_id, path, display=None):
    """Capture a display EXCLUDING a given window (the mask overlay), using Quartz.
    Lets us keep hiding subtitles on screen while still OCR'ing the text underneath
    the bar. Returns True on success."""
    try:
        import Quartz
        from Cocoa import NSBitmapImageRep, NSPNGFileType
    except ImportError:
        return False
    rect = _mac_display_bounds(display) or Quartz.CGRectInfinite
    image = Quartz.CGWindowListCreateImage(
        rect,
        Quartz.kCGWindowListOptionOnScreenBelowWindow,
        int(window_id),
        Quartz.kCGWindowImageDefault,
    )
    if image is None:
        return False
    rep = NSBitmapImageRep.alloc().initWithCGImage_(image)
    png = rep.representationUsingType_properties_(NSPNGFileType, None)
    return bool(png.writeToFile_atomically_(path, True))


def _mac_find_overlay_window():
    """Find SubWatch's own overlay windows by NAME (robust to restarts / stale ids) so
    the capture loop can screenshot BELOW them — otherwise it would OCR our own mask bar
    or the on-screen word panel and feed them back into capture. Returns the lowest such
    window id (capturing below it excludes every SubWatch overlay above it), or None."""
    try:
        import Quartz
    except ImportError:
        return None
    info = Quartz.CGWindowListCopyWindowInfo(
        Quartz.kCGWindowListOptionOnScreenOnly | Quartz.kCGWindowListExcludeDesktopElements,
        Quartz.kCGNullWindowID,
    ) or []
    ours = {"SubWatchMask", "SubWatchVocab", "SubWatchLiveSubtitle"}
    ids = [w.get("kCGWindowNumber") for w in info
           if w.get("kCGWindowName") in ours]
    return min(ids) if ids else None


def _mac_mask_window_present():
    """True only if the MASK overlay (SubWatchMask) is on screen — used by the panel to
    report the mask toggle state. Distinct from _mac_find_overlay_window(), which matches
    ALL SubWatch windows (so OCR can exclude the word panel / live-subtitle too)."""
    try:
        import Quartz
    except ImportError:
        return False
    info = Quartz.CGWindowListCopyWindowInfo(
        Quartz.kCGWindowListOptionOnScreenOnly | Quartz.kCGWindowListExcludeDesktopElements,
        Quartz.kCGNullWindowID,
    ) or []
    return any(w.get("kCGWindowName") == "SubWatchMask" for w in info)


def _mac_grab_screen(region=None, display=None):
    fd, path = tempfile.mkstemp(suffix=".png", prefix="subwatch_")
    os.close(fd)

    # If a mask overlay is on screen, capture BELOW it so OCR still sees the text.
    # Find it live by window name rather than a saved id (avoids stale-id bugs).
    overlay_id = _mac_find_overlay_window() or config.load_config().get("overlay_window_id")
    if overlay_id and _mac_grab_below_window(overlay_id, path, display=display):
        if region:
            _mac_crop(path, region)
        return path

    cmd = ["screencapture", "-x"]
    if display:
        cmd += ["-D", str(display)]
    cmd.append(path)
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0 or not os.path.exists(path) or os.path.getsize(path) == 0:
        stderr = (result.stderr or "").strip()
        if "could not create image" in stderr or not os.path.exists(path):
            raise PermissionError(
                "screencapture could not read the display. Grant Screen Recording "
                "permission to your terminal app:\n"
                "  System Settings > Privacy & Security > Screen Recording > enable your terminal,\n"
                "then fully quit and reopen the terminal and try again."
            )
        raise RuntimeError(f"screencapture failed: {stderr}")
    if region:
        _mac_crop(path, region)
    return path


def _mac_ocr_image(image_path):
    result = subprocess.run(
        [config.OCR_HELPER, image_path], capture_output=True, text=True, timeout=30
    )
    try:
        return json.loads(result.stdout or "[]")
    except json.JSONDecodeError:
        return []


def grab_window_text(window_id):
    """OCR a single window captured BY ID (Quartz CGWindowListCreateImage). This is the
    reliable way to read Zoom's caption window: it works regardless of which display the
    window is on (incl. negative/secondary-monitor coords and Retina scaling) and even if
    the window is partially occluded. Returns a list of OCR line dicts ({text,...}), or []
    on any failure. macOS only."""
    try:
        import Quartz
        from Cocoa import NSBitmapImageRep, NSPNGFileType
    except ImportError:
        return []
    try:
        img = Quartz.CGWindowListCreateImage(
            Quartz.CGRectNull, Quartz.kCGWindowListOptionIncludingWindow,
            int(window_id), Quartz.kCGWindowImageBoundsIgnoreFraming)
        if img is None:
            return []
        rep = NSBitmapImageRep.alloc().initWithCGImage_(img)
        png = rep.representationUsingType_properties_(NSPNGFileType, None)
        fd, path = tempfile.mkstemp(suffix=".png", prefix="subwatch_cap_")
        os.close(fd)
        if not png.writeToFile_atomically_(path, True):
            return []
        try:
            return _mac_ocr_image(path)
        finally:
            try:
                os.remove(path)
            except OSError:
                pass
    except Exception:  # noqa: BLE001
        return []


# ---------------------------------------------------------------------------
# Windows backend (mss + Windows.Media.Ocr). All imports are lazy so this file
# imports cleanly on macOS where these packages are absent.
# ---------------------------------------------------------------------------
def _win_grab_screen(region=None, display=None):
    """Capture a monitor with mss to a temp PNG, optionally cropped to region.

    `display` is 1-based (mss monitor index; 1 = primary). `region` is
    [x, y, w, h] in that monitor's pixels, origin top-left."""
    import mss          # pip install mss
    import mss.tools

    fd, path = tempfile.mkstemp(suffix=".png", prefix="subwatch_")
    os.close(fd)
    with mss.mss() as sct:
        monitors = sct.monitors  # [0]=all, [1..]=individual
        index = display if (display and display < len(monitors)) else 1
        mon = monitors[index]
        if region:
            x, y, w, h = region
            box = {"left": mon["left"] + x, "top": mon["top"] + y, "width": w, "height": h}
        else:
            box = mon
        shot = sct.grab(box)
        mss.tools.to_png(shot.rgb, shot.size, output=path)
    return path


def _win_ocr_image(image_path):
    """OCR via Windows.Media.Ocr. Returns the same dict shape as the Mac helper:
    text/confidence/x/y/w/h with normalized coords and BOTTOM-LEFT origin (to match
    Vision), so capture_text()'s top-down sort works identically on both platforms."""
    import asyncio

    # winrt namespaces (package: winrt / winsdk). Import lazily.
    try:
        from winrt.windows.media.ocr import OcrEngine
        from winrt.windows.globalization import Language
        from winrt.windows.graphics.imaging import BitmapDecoder
        from winrt.windows.storage import StorageFile, FileAccessMode
    except ImportError as exc:  # pragma: no cover - windows only
        raise RuntimeError(
            "Windows OCR needs the PyWinRT namespace packages from requirements.txt.\n"
            f"(import error: {exc})"
        )

    async def _run():
        sfile = await StorageFile.get_file_from_path_async(os.path.abspath(image_path))
        stream = await sfile.open_async(FileAccessMode.READ)
        decoder = await BitmapDecoder.create_async(stream)
        bitmap = await decoder.get_software_bitmap_async()
        width, height = bitmap.pixel_width, bitmap.pixel_height

        # Prefer a Chinese engine (also recognizes Latin); fall back to user profile.
        engine = None
        for tag in ("zh-Hans-CN", "zh-Hans", "zh-Hant"):
            if OcrEngine.is_language_supported(Language(tag)):
                engine = OcrEngine.try_create_from_language(Language(tag))
                break
        if engine is None:
            engine = OcrEngine.try_create_from_user_profile_languages()
        if engine is None:
            raise RuntimeError(
                "No OCR language available. Install a language pack:\n"
                "  Settings > Time & Language > Language > add Chinese (Simplified),\n"
                "  and ensure the optional 'Optical character recognition' feature is on."
            )

        result = await engine.recognize_async(bitmap)
        lines = []
        for line in result.lines:
            text = line.text
            if not text.strip():
                continue
            # union the word rects to get the line box (pixels, top-left origin)
            xs0, ys0, xs1, ys1 = [], [], [], []
            for word in line.words:
                rect = word.bounding_rect
                xs0.append(rect.x); ys0.append(rect.y)
                xs1.append(rect.x + rect.width); ys1.append(rect.y + rect.height)
            if not xs0:
                continue
            x0, y0, x1, y1 = min(xs0), min(ys0), max(xs1), max(ys1)
            lines.append({
                "text": text,
                "confidence": 1.0,  # Windows OCR doesn't expose per-line confidence
                "x": x0 / width,
                # convert to bottom-left origin to match the Vision convention
                "y": 1.0 - (y1 / height),
                "w": (x1 - x0) / width,
                "h": (y1 - y0) / height,
            })
        return lines

    return asyncio.run(_run())


# ---------------------------------------------------------------------------
# Public, platform-agnostic API
# ---------------------------------------------------------------------------
def grab_screen(region=None, display=None):
    """Capture a display to a temp PNG, optionally cropped to [x,y,w,h]. Returns path."""
    if IS_MAC:
        return _mac_grab_screen(region, display=display)
    if IS_WINDOWS:
        return _win_grab_screen(region, display=display)
    raise RuntimeError(f"Unsupported platform: {sys.platform}")


def ocr_image(image_path):
    """Run OCR on an image. Returns list of dicts with text/confidence/box."""
    if IS_MAC:
        return _mac_ocr_image(image_path)
    if IS_WINDOWS:
        return _win_ocr_image(image_path)
    raise RuntimeError(f"Unsupported platform: {sys.platform}")


def capture_text(region=None, min_confidence=0.3, display=None):
    """Capture the screen region and return recognized lines above a confidence floor.
    Each line: {text, confidence, x, y, w, h}. Lines are ordered top-to-bottom."""
    path = grab_screen(region, display=display)
    try:
        lines = ocr_image(path)
    finally:
        try:
            os.remove(path)
        except OSError:
            pass
    kept = [ln for ln in lines if ln.get("confidence", 0) >= min_confidence and ln.get("text", "").strip()]
    # Both backends use bottom-left origin: larger y = higher on screen. Sort top-down.
    kept.sort(key=lambda ln: -ln.get("y", 0))
    return kept
