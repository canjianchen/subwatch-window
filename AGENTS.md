# AGENTS.md — SubWatch

Guidance for AI coding agents (Codex, Claude Code, etc.) working in this repo.
Read this before making changes. It exists so an agent can onboard the app on a
**fresh Windows machine** without breaking the working macOS build.

## What this is

SubWatch is a **local desktop app** that turns on-screen video into an English
learning session: it OCRs subtitles off the screen (or transcribes audio),
translates non-English to English, and mines genuinely-hard words into a
spaced-repetition dictionary. There is also a Meeting Mode (live Zoom caption →
transcript + AI notes). It is **not** a web service — it must run on the machine
whose screen/audio you want to read. There is nothing to deploy to a server.

Pure Python (stdlib HTTP server for the panel, SQLite for storage) plus a few
per-OS native pieces. No framework, no build step for the Python.

## Platform matrix — READ THIS FIRST

The app is cross-platform by design; imports of native libs are **lazy and
guarded by `sys.platform` / `os.name`**, so a module imports cleanly on the
"wrong" OS and only fails if you actually call an unsupported path. Do not
"fix" this by adding top-level native imports — that breaks the other OS.

| Feature | Command | macOS | Windows |
|---|---|---|---|
| Subtitle OCR capture | `watch` / `once` | Apple Vision (`bin/ocr_helper`, Swift) | `Windows.Media.Ocr` via `winrt` + `mss` screen grab |
| Web control panel | `panel` | ✅ | ✅ (identical; pure Python) |
| Review flashcards | `review` | ✅ Tkinter | ✅ Tkinter |
| Notes / Anki export | `notes` | ✅ | ✅ |
| Vocabulary store (SQLite) | — | ✅ | ✅ |
| Mask overlay | `overlay` | `overlay_mac.py` (Cocoa) | `overlay_win.py` (Tkinter) |
| Pronunciation audio | `audio` | `say` | SAPI via PowerShell |
| Audio mode (transcribe) | `listen` | Whisper + BlackHole | Whisper + a virtual audio device (e.g. VB-CABLE) |
| Menu-bar app | `menubar` | ✅ Cocoa | ❌ (macOS-only) |
| Meeting Mode **capture** | `meeting` | ✅ AX + Quartz | ❌ **not ported** (capture uses macOS AX/Quartz) |
| Meeting transcript/notes/Q&A/export | `meetings`, panel | ✅ | ✅ (viewing/AI works; only live capture is Mac-only) |

**macOS-only source files** (don't import these on Windows): `overlay_mac.py`,
`menubar.py`, `ocr_helper.swift`, `zoom_ax.py`, and the Quartz paths in `ocr.py`
and `meeting.py`. **Windows-specific:** `overlay_win.py`, the `_win_*` functions
in `ocr.py`, the SAPI path in `audio.py`.

## Setup

**macOS:** `./setup.sh` (installs deps, fetches UI libs, builds the Swift OCR helper).

**Windows:**
```powershell
powershell -ExecutionPolicy Bypass -File .\setup.ps1
```
This installs the Windows + cross-platform Python deps and fetches the panel's
UI libraries. `requirements.txt` uses `sys_platform` markers, so pip installs the
right subset per OS (it will NOT try to install `pyobjc` on Windows).

If the panel loads unstyled, the UI libs in `src/static/*.js` are missing
(they are `.gitignore`d and fetched by setup) — re-run setup while online, or
manually download the three URLs listed in `setup.ps1` into `src/static/`.

## Run

macOS: `./subwatch <cmd>`   Windows: `.\subwatch.bat <cmd>`
Both delegate to `python src/cli.py <cmd>`. `subwatch panel` is the main entry —
it opens `http://127.0.0.1:8770` and drives every other command from the browser.

Run `subwatch help` (or read `src/cli.py`'s `USAGE`) for the full command list.

## Windows onboarding — likely gotchas an agent should expect

1. **OCR language pack.** `Windows.Media.Ocr` needs the language installed:
   Settings → Time & Language → Language → add the language, then add the
   optional **"Optical character recognition"** feature. English is usually
   present; **Chinese (Simplified)** must be added for the Chinese subtitle line.
   Without it, `_win_ocr_image` raises a clear "No OCR language available" error.
2. **`winrt` packaging drift.** The OCR path imports
   `winrt.windows.media.ocr`, `.graphics.imaging`, `.storage`, `.globalization`.
   Package layouts changed across versions. If a bare `pip install winrt` does
   not expose those namespaces on the installed Python, the maintained fork is
   **`winsdk`** (same API, `from winsdk.windows...`). If you switch, add a
   try/except import shim in `ocr.py` (`try: from winrt... except ImportError:
   from winsdk...`) rather than replacing outright — keep both working.
3. **`py` vs `python`.** `setup.ps1` prefers the `py` launcher, falls back to
   `python`. The CLI subprocesses use `sys.executable`, so they're consistent
   once launched.
4. **Whisper is heavy** (pulls `torch`). Only `subwatch listen` needs it; don't
   let a whisper/torch install failure block subtitle mode, review, or the panel.
5. **Tkinter** ships with the python.org installer but can be missing on some
   distributions — `review` and `overlay` need it.

## Constraints for agents

- **Do not regress macOS.** Keep native imports lazy and platform-guarded. Test
  that `python src/cli.py help` and `python -c "import server, ocr, config"`
  import without error on the current OS before committing.
- **No new heavy dependencies** without a strong reason; the base install is
  intentionally light and Whisper imports are lazy so the core runs offline.
- **Personal data is gitignored** (`db/`, `data/config.json`, `data/profile.json`,
  `notes/`, `logs/`). Never commit a real vocabulary DB, profile, or credentials.
- **No hardcoded secrets.** Codex reuses its own saved CLI login; the app auto-runs
  in `local_only` mode when Codex is unavailable.
- Match the surrounding code style: small functions, descriptive names, docstrings
  that explain *why*. This repo favors clarity over cleverness.

## Layout

```
subwatch / subwatch.bat   CLI wrappers (macOS / Windows) → src/cli.py
setup.sh / setup.ps1      per-OS installers
requirements.txt          deps with sys_platform markers
src/
  cli.py                  command dispatch
  server.py               web panel (stdlib HTTP; serves src/static/*)
  ocr.py                  screen capture + OCR bridge (mac Vision / win Windows.Media.Ocr)
  ocr_helper.swift        macOS Vision helper (compiled to bin/ocr_helper)
  watch.py                subtitle capture loop
  detector.py             hard-word detection (frequency list)
  hard_phrases_llm.py     optional Codex word/phrase grading
  store.py                SQLite vocab + spaced repetition
  review_app.py           Tkinter flashcards
  overlay.py              platform dispatcher → overlay_mac.py / overlay_win.py
  audio*.py               pronunciation + live audio transcribe/translate
  meeting*.py, zoom_ax.py Meeting Mode (capture is macOS-only; store/notes/Q&A portable)
  enrich.py, export_notes.py, config.py, ...
data/  common_words.txt (10k frequency list), idioms, level bank  (committed)
db/    subwatch.db  (your data — gitignored)
```
