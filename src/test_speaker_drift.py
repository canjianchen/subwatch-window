"""Offline reproduction of the speaker-drift + duplicate-commit bug (no Zoom needed).

Real mechanism (confirmed from live DB meeting #24): Zoom's caption box is a tall
scrollback, so a finalized SHORT line stays VISIBLE for 60s+. Once finalized it leaves
_active; the next OCR poll still sees the text, spawns a FRESH line, and re-commits it.
Cross-utterance dedup only blocks this within a 20s window, so a short fragment lingering
past 20s recommits — and picks up whoever is the CURRENT active speaker, producing BOTH
duplicate commits and speaker drift.

Repro: one short line ("Yeah, good, good.") stays visible across ~45s of polls while a
newer growing line scrolls beneath it and the active speaker flips Nick -> Vincent.
Expect: exactly ONE commit of the short line, locked to Nick (who said it).
"""
import meeting_assemble as ma


def run():
    commits = []

    def on_segment(seg):
        if not seg["is_partial"]:
            commits.append((seg["norm"], seg["speaker"], seg["text"]))

    asm = ma.Assembler(on_segment, started_at=0.0,
                       cfg={"stable_frames": 2},
                       roster=["Nick Kilian", "Vincent Ni"])

    SHORT = "Yeah, good, good."
    # 18 polls, 2.5s apart = ~45s. SHORT stays visible the whole time (top of box) while
    # a second, ever-changing line grows beneath it. Speaker = Nick for first 5 polls,
    # then Vincent for the rest (he starts talking while Nick's short line still lingers).
    tail_lines = [
        "so on our side we call their", "model it could be like we",
        "didnt receive any response", "and timeout so we call again",
        "right but on their side they", "already received those requests",
        "is processing so on webhook", "it will see two responses",
        "lets just assume webhook is", "working and the last one wins",
        "yeah the last one wins okay", "yeah thats okay but then",
        "from the front end it will see", "a different image right okay",
    ]
    frames = []
    for i in range(18):
        spk = "Nick Kilian" if i < 5 else "Vincent Ni"
        bottom = tail_lines[min(i, len(tail_lines) - 1)] + f" {i}"  # always changing
        frames.append(([SHORT, bottom], spk))
    frames += [([], None), ([], None)]  # box clears -> flush

    for i, (box, spk) in enumerate(frames):
        ma.time.time = (lambda t: (lambda: t))(float(i) * 2.5)
        asm.push_frame(box, speaker_hint=spk)
    asm.flush()

    target = ma._norm_meeting(SHORT)
    hits = [(sp, txt) for (norm, sp, txt) in commits if norm == target]
    print(f"total commits: {len(commits)}")
    print(f"{SHORT!r} committed {len(hits)} time(s); speakers = {[h[0] for h in hits]}")

    problems = 0
    if len(hits) != 1:
        print(f"  BUG: expected exactly 1 commit, got {len(hits)} (duplicate-commit)")
        problems += 1
    if any(sp != "Nick Kilian" for sp, _ in hits):
        print(f"  BUG: misattributed — should all be 'Nick Kilian' (drift)")
        problems += 1
    if not problems:
        print("  OK: single commit, correctly attributed to Nick Kilian")
    return problems


if __name__ == "__main__":
    import sys
    sys.exit(1 if run() else 0)
