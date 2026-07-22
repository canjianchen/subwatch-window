"""Auto-detect the subtitle region on a display by finding prose lines near the bottom.

Captures the whole display, OCRs it, keeps lines that look like real subtitles
(Chinese text or runs of latin words) located in the lower portion of the screen,
and unions their boxes into an [x, y, w, h] region with padding. Player chrome
(timecodes, quality labels, speed) is excluded via the same noise filter as watch.
"""
import sys
import struct

import config
import detector
import ocr
import watch  # for _is_noise / has_chinese reuse


def _png_dimensions(path):
    """Read PNG dimensions without relying on macOS `sips` or Pillow."""
    try:
        with open(path, "rb") as handle:
            header = handle.read(24)
        if header[:8] == b"\x89PNG\r\n\x1a\n" and len(header) >= 24:
            return struct.unpack(">II", header[16:24])
    except OSError:
        pass
    return 0, 0


def detect_region(display=None, lower_fraction=0.45, pad_frac=0.02):
    """Return ([x, y, w, h], lines) for the subtitle band on `display`, or (None, [])."""
    path = ocr.grab_screen(display=display)
    width, height = _png_dimensions(path)
    raw = ocr.ocr_image(path)
    import os
    try:
        os.remove(path)
    except OSError:
        pass
    if not width or not height:
        return None, []

    boxes, texts = [], []
    for line in raw:
        text = line.get("text", "").strip()
        if not text or watch._is_noise(text):
            continue
        # Vision boxes are normalized, origin bottom-left. Convert to top-left pixels.
        x = line["x"] * width
        y = (1 - line["y"] - line["h"]) * height
        w = line["w"] * width
        h = line["h"] * height
        # only keep lines in the lower portion of the screen (subtitles live there)
        if (y + h) < height * (1 - lower_fraction):
            continue
        # EXCLUDE the bottom ~6% of the screen — that's where the player control bar
        # (timecode, quality, danmaku box, seek bar) lives, not subtitles.
        if y > height * 0.94:
            continue
        # Subtitles are dialogue: real Chinese text, or an English line that looks
        # like a sentence — not UI fragments / numbers / single glyphs.
        is_dialogue = detector.has_chinese(text) or watch.looks_like_dialogue(text)
        if not is_dialogue:
            continue
        boxes.append((x, y, w, h))
        texts.append(text)

    if not boxes:
        return None, []

    # Vertical extent: tight around the detected subtitle lines (plus padding).
    y0 = min(b[1] for b in boxes)
    y1 = max(b[1] + b[3] for b in boxes)
    pad_y = height * pad_frac
    y0 = max(0, int(y0 - pad_y))
    y1 = min(height, int(y1 + pad_y))

    # Horizontal extent: subtitles are centred and vary in length frame-to-frame,
    # so a box fitted to ONE frame clips longer lines. Use a wide centred band
    # around the detected text's centre instead — capture ~90% of the width,
    # symmetric about the subtitle centre, so long lines are never cut off.
    text_cx = sum(b[0] + b[2] / 2 for b in boxes) / len(boxes)
    half_band = width * 0.46  # ~92% of screen width total
    x0 = max(0, int(text_cx - half_band))
    x1 = min(width, int(text_cx + half_band))
    return [x0, y0, x1 - x0, y1 - y0], texts


def detect_region_retry(display=None, attempts=8, delay=1.0):
    """Retry detection across several frames — subtitles come and go, so a single
    empty frame (a gap between lines) shouldn't fail the whole detection."""
    import time
    last = (None, [])
    for attempt in range(attempts):
        region, texts = detect_region(display=display)
        if region:
            return region, texts
        last = (region, texts)
        if attempt < attempts - 1:
            time.sleep(delay)
    return last


def main():
    display = None
    if len(sys.argv) > 1:
        display = int(sys.argv[1])
    print("Detecting subtitle region (watching a few seconds for subtitles)...")
    region, texts = detect_region_retry(display=display)
    if not region:
        print("No subtitle-like text found in the lower screen. Is a subtitled video playing?")
        return 1
    cfg = config.load_config()
    cfg["capture_region"] = region
    if display:
        cfg["display"] = display
    config.save_config(cfg)
    print(f"Detected subtitle region on display {cfg.get('display')}: "
          f"x={region[0]} y={region[1]} w={region[2]} h={region[3]}")
    print("Based on these lines:")
    for text in texts:
        print(f"   • {text}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
