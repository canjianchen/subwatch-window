"""Measure how well the live overlay matches the reference on-screen subtitle.

Samples, over N seconds: (a) the reference caption via OCR of the bottom strip,
(b) our live transcript line. Scores word-overlap (meaning proxy) and reports the
gap so we can tune the streaming params toward "same meaning, tight timing".

Run:  python3 sync_eval.py [seconds] [--region x,y,w,h]
"""
import json
import os
import subprocess
import sys
import tempfile
import time

import config

OCR = config.OCR_HELPER
TRANSCRIPT = os.path.join(config.DB_DIR, "live_transcript.json")


def _ocr_strip(region):
    fd, path = tempfile.mkstemp(suffix=".png", prefix="synceval_")
    os.close(fd)
    subprocess.run(["screencapture", "-x", "-D", "1", path], capture_output=True)
    if region:
        x, y, w, h = region
        subprocess.run(["sips", "--cropToHeightWidth", str(h), str(w),
                        "--cropOffset", str(y), str(x), path], capture_output=True)
    out = subprocess.run([OCR, path], capture_output=True, text=True).stdout
    try:
        os.remove(path)
    except OSError:
        pass
    try:
        items = json.loads(out or "[]")
    except json.JSONDecodeError:
        return ""
    # keep latin (the reference English caption); drop CJK/UI noise
    import re
    text = " ".join(o["text"] for o in items)
    return re.sub(r"\s+", " ", text).strip()


def _my_line():
    try:
        with open(TRANSCRIPT, encoding="utf-8") as h:
            lines = json.load(h).get("lines", [])
        return lines[-1]["text"] if lines else ""
    except (OSError, json.JSONDecodeError, KeyError):
        return ""


def _words(s):
    import re
    return set(re.findall(r"[a-z']+", s.lower()))


def _overlap(a, b):
    wa, wb = _words(a), _words(b)
    if not wa or not wb:
        return 0.0
    return len(wa & wb) / len(wa | wb)   # Jaccard


def main():
    secs = int(sys.argv[1]) if len(sys.argv) > 1 and sys.argv[1].isdigit() else 30
    region = None
    for a in sys.argv:
        if a.startswith("--region"):
            region = [int(x) for x in a.split("=")[1].split(",")]
    # default reference region = bottom strip of display 1 (3840x2160)
    if region is None:
        region = [576, 1840, 2688, 230]

    print(f"Sampling {secs}s — reference(YouTube OCR) vs our overlay…\n")
    samples = []
    seen_ref = set()
    t_end = time.time() + secs
    while time.time() < t_end:
        ref = _ocr_strip(region)
        mine = _my_line()
        if ref and ref not in seen_ref:
            seen_ref.add(ref)
            ov = _overlap(ref, mine)
            samples.append(ov)
            print(f"  ref : {ref[:70]}")
            print(f"  mine: {mine[:70]}")
            print(f"  overlap: {ov:.0%}\n")
        time.sleep(1.5)

    if samples:
        avg = sum(samples) / len(samples)
        print(f"=== avg word-overlap over {len(samples)} ref lines: {avg:.0%} ===")
    else:
        print("No reference captions captured — check the --region or that captions are on.")


if __name__ == "__main__":
    main()
