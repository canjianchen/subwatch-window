"""Regression tests for the AX-capture bugs that wrecked meeting #25 (2026-06-30 10:00):

1. read_caption_lines must NOT harvest Zoom UI chrome when no caption window is open.
2. The assembler must drop UI-chrome strings ('Settings', 'editor', share banners) that
   are valid English and so slip past the OCR-noise filter.
3. AX-captured lines must be attributed to the active speaker, not committed as Unknown.

These don't need a live Zoom — they exercise the pure logic that was wrong.
"""
import meeting_assemble as ma
import zoom_ax


def test_chrome_filter():
    chrome = ["Settings", "editor", "Apps", "Loading", "Vincent Ni has started screen sharing",
              "The shared content is fit to your screen. To see the original",
              "Mute", "Gallery View"]
    speech = ["And there'll be two versions of it. The one is the digital",
              "Good morning, everyone, I think we have everyone here",
              "Yeah, right now we haven't discussed how to put those two pieces"]
    bad = [c for c in chrome if not ma._is_chrome(c)]
    good_blocked = [s for s in speech if ma._is_chrome(s)]
    ok = not bad and not good_blocked
    print(f"[chrome filter] {'OK' if ok else 'FAIL'}: "
          f"{len(chrome)-len(bad)}/{len(chrome)} chrome blocked, "
          f"{len(speech)-len(good_blocked)}/{len(speech)} speech kept")
    if bad: print("   leaked chrome:", bad)
    if good_blocked: print("   wrongly blocked speech:", good_blocked)
    return ok


def test_assembler_drops_chrome_keeps_speech_with_speaker():
    commits = []
    def on_seg(s):
        if not s["is_partial"]:
            commits.append((s["speaker"], s["text"]))
    asm = ma.Assembler(on_seg, started_at=0.0, cfg={"stable_frames": 2},
                       roster=["Vincent Ni", "Runyou Wang"])
    # Frames mixing chrome with a real spoken line; speaker_hint supplied (AX path now does).
    frames = [
        (["Settings", "editor", "And there'll be two versions of it"], "Vincent Ni"),
        (["editor", "And there'll be two versions of it"], "Vincent Ni"),
        (["Vincent Ni has started screen sharing"], "Vincent Ni"),
        ([], None), ([], None),
    ]
    for i, (box, spk) in enumerate(frames):
        ma.time.time = (lambda t: (lambda: t))(float(i) * 2.5)
        asm.push_frame(box, speaker_hint=spk)
    asm.flush()
    texts = [t for _sp, t in commits]
    has_speech = any("two versions" in t for t in texts)
    no_chrome = not any(ma._is_chrome(t) for t in texts)
    attributed = all(sp == "Vincent Ni" for sp, _t in commits) and commits
    ok = has_speech and no_chrome and attributed
    print(f"[assembler] {'OK' if ok else 'FAIL'}: committed {commits}")
    return ok


def test_read_caption_lines_no_chrome_fallback():
    # Without a live Zoom we can't exercise the AX tree, but we CAN assert the code no
    # longer references an all-windows fallback path (the bug). Static guard.
    import inspect
    src = inspect.getsource(zoom_ax.read_caption_lines)
    ok = "targets = windows" not in src and "if not caption_windows:" in src
    print(f"[read_caption_lines] {'OK' if ok else 'FAIL'}: no all-windows chrome fallback")
    return ok


if __name__ == "__main__":
    import sys
    results = [test_chrome_filter(),
               test_assembler_drops_chrome_keeps_speech_with_speaker(),
               test_read_caption_lines_no_chrome_fallback()]
    sys.exit(0 if all(results) else 1)
