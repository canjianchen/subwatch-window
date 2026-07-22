"""SubWatch cross-platform CLI — works on macOS and Windows.

Run as:  python src/cli.py <command> [args]
or via the platform wrappers:  ./subwatch <cmd>   (macOS)   subwatch.bat <cmd> (Windows)
"""
import subprocess
import sys

import config

USAGE = """SubWatch — auto-capture hard English words from on-screen subtitles while you watch.

Usage: subwatch <command>

  watch              Start the capture loop (Ctrl-C to stop).
  once               Capture a single frame (good for testing).
  region             Drag-select the subtitle area of the screen and save it.
  autodetect [N]     Auto-find the subtitle band on display N (needs a video playing).
  panel              Open the web control panel (toggle hide modes, watch, notes — live).
  overlay            Show a draggable/resizable mask bar to hide subtitles on screen.
  meeting            Meeting Mode: capture live Zoom captions → transcript + AI notes + Q&A.
  meetings           List captured meetings.
  review             Open the flashcard review app (spaced repetition).
  notes              Export vocabulary to notes/vocabulary.md + anki_import.csv.
  enrich             Use Codex to add definitions + translations.
  audio              Generate clickable pronunciation clips for captured terms.
  listen [opts]      Live audio→English→capture (for videos with NO subtitle): local
                     Whisper transcribes/translates, Codex polishes the text, then
                     hard words are captured. Opts: --device BlackHole, --accurate
                     (small model), --no-translate, --no-polish, --list.
  mode <m>           Display filter: show_both | hide_chinese | hide_english | hide_both
  level <n>          Difficulty: capture only words rarer than rank <n> (3000 easy,
                     7000 advanced, 9000 expert).
  smart <on|off>     LLM judges which words/phrases are truly hard (catches words
                     frequency misses, e.g. "sudsy"). Default on; needs Codex login.
  phrases <on|off>   Capture idiom/slang phrases (e.g. "help yourself"). Default on.
  llm-phrases <on|off>  Use Codex to flag hard expressions the list misses
                     (e.g. "devoid of humor"). Default off (one Codex run per line).
  config             Print the current configuration.
  stats              Print vocabulary statistics.
  rebuild-ocr        (macOS only) Recompile the Swift Vision OCR helper.
"""


def _run(module, *args):
    """Run a sibling module as a subprocess so GUI apps get their own event loop."""
    return subprocess.call([sys.executable, "-m", module.replace(".py", ""), *args])


def main(argv):
    cmd = argv[0] if argv else "help"
    rest = argv[1:]

    if cmd == "watch":
        return _run("watch")
    if cmd == "once":
        return _run("watch", "--once")
    if cmd == "listen":
        return _run("audio_listen", *rest)
    if cmd == "region":
        return _run("pick_region")
    if cmd == "autodetect":
        return _run("autodetect", *rest)
    if cmd == "review":
        return _run("review_app")
    if cmd == "panel":
        return _run("server")
    if cmd == "meeting":
        return _run("meeting", *rest)
    if cmd == "meetings":
        import meeting_store
        meeting_store.init_db()
        rows = meeting_store.list_meetings()
        if not rows:
            print("No meetings captured yet. Start one from the panel or `subwatch meeting`.")
            return 0
        for m in rows:
            status = "🔴 live" if m["status"] == "live" else "✓"
            print(f"  #{m['id']:<4} {status:<6} {m['title']}  ({m['segment_count']} lines)")
        return 0
    if cmd == "overlay":
        return _run("overlay")
    if cmd in ("vocab", "words"):
        return _run("vocab_overlay")
    if cmd == "notes":
        return _run("export_notes")
    if cmd == "enrich":
        return _run("enrich")
    if cmd == "audio":
        return _run("audio")

    if cmd == "mode":
        if not rest:
            print("usage: subwatch mode <show_both|hide_chinese|hide_english|hide_both>")
            return 1
        cfg = config.load_config()
        cfg["display_mode"] = rest[0]
        config.save_config(cfg)
        print("display_mode =", cfg["display_mode"])
        return 0

    if cmd == "level":
        if not rest:
            print("usage: subwatch level <rank>  (e.g. 7000; higher = stricter)")
            return 1
        cfg = config.load_config()
        cfg["rarity_threshold"] = int(rest[0])
        config.save_config(cfg)
        print("rarity_threshold =", cfg["rarity_threshold"], "(only words rarer than this get captured)")
        return 0

    if cmd in ("phrases", "llm-phrases", "smart", "local"):
        key = {"phrases": "capture_phrases", "llm-phrases": "use_llm_phrases",
               "smart": "smart_capture", "local": "local_only"}[cmd]
        if not rest or rest[0] not in ("on", "off"):
            print(f"usage: subwatch {cmd} <on|off>")
            return 1
        cfg = config.load_config()
        cfg[key] = (rest[0] == "on")
        config.save_config(cfg)
        print(f"{key} = {cfg[key]}")
        return 0

    if cmd == "smart-level":
        if not rest or rest[0] not in ("intermediate", "advanced", "expert"):
            print("usage: subwatch smart-level <intermediate|advanced|expert>")
            return 1
        cfg = config.load_config()
        cfg["smart_level"] = rest[0]
        config.save_config(cfg)
        print(f"smart_level = {cfg['smart_level']}")
        return 0

    if cmd == "config":
        import json
        print(json.dumps(config.load_config(), indent=2, ensure_ascii=False))
        return 0

    if cmd == "stats":
        import store
        store.init_db()
        print(store.stats())
        return 0

    if cmd == "rebuild-ocr":
        if sys.platform != "darwin":
            print("rebuild-ocr is macOS-only (the Swift Vision helper). "
                  "On Windows the OCR uses Windows.Media.Ocr — nothing to build.")
            return 0
        import os
        src = os.path.join(config.ROOT, "src", "ocr_helper.swift")
        out = os.path.join(config.BIN, "ocr_helper")
        rc = subprocess.call(["swiftc", "-O", src, "-o", out])
        if rc == 0:
            print("OCR helper rebuilt.")
        return rc

    if cmd in ("help", "--help", "-h"):
        print(USAGE)
        return 0

    print(f"Unknown command: {cmd}\n")
    print(USAGE)
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
