"""SubWatch web control panel — stdlib HTTP server, no dependencies.

Run:  python3 server.py   (or `subwatch panel`)
Opens a local dashboard at http://127.0.0.1:8765 to:
  - toggle display mode (show_both / hide_chinese / hide_english / hide_both)
  - set difficulty level, pick display, autodetect/region
  - start & stop the capture loop (watch)
  - start & stop the on-screen mask overlay
  - watch a live feed of captured words + browse the vocabulary note
"""
import json
import os
import signal
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

import config
import store
import export_notes

SRC = os.path.dirname(os.path.abspath(__file__))
PY = sys.executable
# Preferred port, but we scan upward for a free one so we never collide with
# another local server already bound to it.
PORT = 8770

# Track child processes started from the panel.
_procs = {"watch": None, "overlay": None, "listen": None, "subtitle": None,
          "vocab": None, "meeting": None}
_lock = threading.Lock()


def _overlay_window_present():
    """Detect a running MASK overlay by its on-screen window (works even if the overlay
    was launched outside the panel) rather than only panel-launched children. Matches the
    SubWatchMask window SPECIFICALLY — not the word panel / live-subtitle overlays, which
    would otherwise make the mask toggle falsely read as 'on'."""
    try:
        import ocr
        return ocr._mac_mask_window_present()
    except Exception:  # noqa: BLE001
        return False


def _active_meeting_id():
    """The id of the meeting capture is currently writing to, or None. A meeting is only
    'active' if its row is live AND the driver process is actually running — otherwise a
    meeting orphaned by a crash/kill would look active forever."""
    try:
        import meeting_store
        if not _alive("meeting"):
            return None
        m = meeting_store.latest_live_meeting()
        return m["id"] if m else None
    except Exception:  # noqa: BLE001
        return None


def _int_arg(url, name):
    """Parse an int query arg from a URL, or None."""
    val = parse_qs(urlparse(url).query).get(name, [None])[0]
    try:
        return int(val) if val is not None else None
    except (TypeError, ValueError):
        return None


def _export_meeting_md(mid, meeting, ms):
    """Render a full meeting (summary + Q&A chat + transcript) to Markdown for download."""
    import time as _t
    lines = [f"# {meeting['title']}", ""]
    started = _t.strftime("%Y-%m-%d %H:%M", _t.localtime(meeting["started_at"]))
    lines.append(f"_Captured {started} · source: {meeting['source']}_\n")
    if meeting.get("summary"):
        lines.append(meeting["summary"])
        lines.append("\n---\n")
    # Q&A chat as part of the meeting record — the questions you asked + answers are
    # meeting metadata worth keeping alongside the summary.
    chat = ms.chat_history(mid)
    if chat:
        lines.append("## Questions & Answers\n")
        for c in chat:
            who = "**Q:**" if c["role"] == "user" else "**A:**"
            lines.append(f"{who} {c['content']}\n")
        lines.append("---\n")
    lines.append("## Full Transcript\n")
    start = meeting["started_at"]
    for s in ms.all_segments(mid):
        # wall-clock time of the line, in local (Pacific) time
        ts = _t.strftime("%-I:%M %p", _t.localtime(start + s["t_start_ms"] / 1000.0))
        spk = f"**{s['speaker']}:** " if s.get("speaker") else ""
        lines.append(f"`{ts}` {spk}{s['text']}")
    return "\n".join(lines)


def _list_displays():
    """Return [{index, name, w, h}] in screencapture -D order (1-based) so the user
    can pick a monitor by NAME instead of guessing a number."""
    if os.name == "nt":
        try:
            import mss
            with mss.mss() as sct:
                return [{"index": i, "name": f"Display {i}",
                         "w": int(m["width"]), "h": int(m["height"])}
                        for i, m in enumerate(sct.monitors[1:], start=1)]
        except Exception:  # noqa: BLE001
            return []
    try:
        from AppKit import NSScreen
        out = []
        for i, s in enumerate(NSScreen.screens()):
            try:
                name = s.localizedName()
            except Exception:  # noqa: BLE001
                name = f"Display {i + 1}"
            fr = s.frame()
            out.append({"index": i + 1, "name": str(name),
                        "w": int(fr.size.width), "h": int(fr.size.height)})
        return out
    except Exception:  # noqa: BLE001
        return []


# Cache the python-process list briefly so a single /api/state call (which checks 5
# components) shells out to `ps` ONCE instead of five times. Without this, each state
# poll spawned `ps` per component (~0.6s total), making every panel click feel slow.
_ps_cache = {"entries": None, "at": 0.0}
_PS_TTL = 1.0  # seconds; well under the panel's 2.5s state-poll cadence


def _python_process_entries():
    """Return cached ``(pid, command_line)`` pairs for Python processes."""
    import time as _t
    now = _t.time()
    if _ps_cache["entries"] is not None and now - _ps_cache["at"] < _PS_TTL:
        return _ps_cache["entries"]
    if os.name == "nt":
        # Windows restricts cross-process command-line inspection in some desktop
        # sandboxes. Panel-launched children are tracked directly in `_procs`, so no
        # global process scan is needed for the supported Windows workflow.
        entries = []
    else:
        r = subprocess.run(["ps", "-axo", "pid=,command="], capture_output=True,
                           text=True)
        entries = []
        for line in r.stdout.splitlines():
            pid, sep, cmd = line.strip().partition(" ")
            if sep and pid.isdigit() and "python" in cmd.lower():
                entries.append((int(pid), cmd.strip()))
    _ps_cache["entries"] = entries
    _ps_cache["at"] = now
    return entries


def _python_proc_lines():
    return [line for _pid, line in _python_process_entries()]


def _proc_running(scripts):
    """True if a PYTHON process is running one of the given script files. Matches both
    launch forms — a path like '…/watch.py' (panel-spawned) AND the module form
    'python -m watch' (CLI-spawned via `subwatch watch`) — so the panel reports the
    right state no matter how the process was started. Anchored so it won't false-
    positive on a shell command that merely mentions the script name. Reads a cached
    process snapshot so checking many components costs a single `ps` call."""
    import re
    for line in _python_proc_lines():
        for s in scripts:
            module = s[:-3] if s.endswith(".py") else s
            # path form: '…/watch.py' ending the arg; module form: '-m watch'
            if re.search(r"(^|[\\/])" + re.escape(s) + r"(\s|$)", line):
                return True
            if re.search(r"-m\s+" + re.escape(module) + r"(\s|$)", line):
                return True
    return False


def _alive(name):
    if os.name == "nt":
        proc = _procs.get(name)
        return proc is not None and proc.poll() is None
    # pgrep on the actual script is AUTHORITATIVE — a stale/zombie tracked Popen
    # handle was making state report True after the real process was killed.
    patterns = {
        "overlay": ["overlay.py"],
        "subtitle": ["subtitle_overlay.py"],
        "vocab": ["vocab_overlay.py"],
        "listen": ["audio_stream.py", "audio_listen.py"],
        "watch": ["watch.py"],
        "meeting": ["meeting.py"],
    }.get(name)
    if patterns:
        if name == "overlay":
            return _overlay_window_present() or _proc_running(["overlay.py"])
        return _proc_running(patterns)
    proc = _procs.get(name)
    return proc is not None and proc.poll() is None


_prev_output = None  # audio output to restore when listening stops


def _switch_audio(target):
    """Set the macOS default output device by name (needs SwitchAudioSource).
    Returns True on success."""
    from shutil import which
    if not which("SwitchAudioSource"):
        return False
    return subprocess.run(["SwitchAudioSource", "-s", target],
                          capture_output=True).returncode == 0


def _current_output():
    from shutil import which
    if not which("SwitchAudioSource"):
        return None
    r = subprocess.run(["SwitchAudioSource", "-c"], capture_output=True, text=True)
    return r.stdout.strip() or None


def _route_audio_for_listen():
    """Before listening, route output to the Multi-Output device (so the user still
    HEARS audio while BlackHole captures it). Remembers the prior output to restore."""
    global _prev_output
    cur = _current_output()
    if cur and "multi-output" not in cur.lower():
        _prev_output = cur
    # prefer a Multi-Output device; fall back to BlackHole only if none exists
    for target in ("Multi-Output Device", "BlackHole 2ch"):
        if _switch_audio(target):
            return target
    return None


def _restore_audio():
    """Restore the prior output. If we never recorded one (output was already the
    Multi-Output device), LEAVE it on Multi-Output — it's fully audible, so the user
    keeps hearing everything and can start/stop listening freely."""
    global _prev_output
    if _prev_output and "multi-output" not in _prev_output.lower():
        _switch_audio(_prev_output)
        _prev_output = None


def _start(name, script):
    with _lock:
        if _alive(name):
            return False
        if name == "listen":
            _route_audio_for_listen()
        os.makedirs(config.LOGS_DIR, exist_ok=True)
        log = open(os.path.join(config.LOGS_DIR, f"{name}.log"),
                   "a", encoding="utf-8")
        try:
            _procs[name] = subprocess.Popen(
                [PY, os.path.join(SRC, script)],
                cwd=SRC,
                stdout=log,
                stderr=subprocess.STDOUT,
            )
        finally:
            log.close()
        _ps_cache["entries"] = None  # invalidate so state reflects the new process at once
        return True


def _start_with_args(name, script, args, detached=False):
    """Like _start but passes extra CLI args (used for `meeting.py --meeting-id N`).
    detached=True puts the child in its OWN session/process group (start_new_session) so
    it KEEPS RUNNING even if the panel is restarted/killed — meeting capture must not stop
    just because the web server bounced. Output goes to a log file for debugging."""
    with _lock:
        if _alive(name):
            return False
        log = open(os.path.join(config.LOGS_DIR, f"{name}.log"), "a") \
            if os.path.isdir(config.LOGS_DIR) else subprocess.DEVNULL
        _procs[name] = subprocess.Popen(
            [PY, os.path.join(SRC, script), *args],
            cwd=SRC, stdout=log, stderr=log,
            start_new_session=detached,
        )
        _ps_cache["entries"] = None
        return True


# The actual script file(s) behind each toggle name — used to stop a process even when
# it was launched outside the panel (e.g. from the `subwatch` CLI as `-m vocab_overlay`).
# The panel's short name ("vocab") differs from the script ("vocab_overlay.py"), so we
# can't just pkill "<name>.py".
_SCRIPTS = {
    "watch": ["watch.py", "watch"],
    "overlay": ["overlay.py", "overlay_mac.py", "overlay"],
    "subtitle": ["subtitle_overlay.py", "subtitle_overlay"],
    "vocab": ["vocab_overlay.py", "vocab_overlay_win.py",
              "vocab_overlay", "vocab_overlay_win"],
    "listen": ["audio_stream.py", "audio_listen.py", "audio_stream", "audio_listen"],
    "meeting": ["meeting.py", "meeting"],
}


def _stop(name):
    with _lock:
        if name == "listen":
            _restore_audio()  # put the sound output back where it was
        killed = False
        # 1) terminate a panel-tracked child if we have one
        proc = _procs.get(name)
        if proc and proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                proc.kill()
            killed = True
        _procs[name] = None
        if os.name == "nt":
            _ps_cache["entries"] = None
            return killed
        # 2) ALSO sweep for any copy running independently of the panel (started from the
        # CLI). Find its PID(s) precisely and kill by PID — avoids `pkill -f "-m …"` (the
        # leading dash is parsed as a flag by pkill) and substring collisions (e.g.
        # "overlay" matching "vocab_overlay"). Matches BOTH launch forms: a path
        # '…/vocab_overlay.py' and the module form 'python -m vocab_overlay'.
        import re
        scripts = _SCRIPTS.get(name, [f"{name}.py"])
        pats = []
        for script in scripts:
            module = script[:-3] if script.endswith(".py") else script
            pats.append(re.compile(r"(^|[\\/])" + re.escape(script) + r"(\s|$)"))
            pats.append(re.compile(r"-m\s+" + re.escape(module) + r"(\s|$)"))
        for pid, line in _python_process_entries():
            if any(p.search(line) for p in pats):
                if os.name == "nt":
                    subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                                   capture_output=True,
                                   creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
                else:
                    subprocess.run(["kill", str(pid)], capture_output=True)
                killed = True
        _ps_cache["entries"] = None  # invalidate so state reflects the kill immediately
        return killed


PAGE = """<!doctype html><html lang="en" class="dark"><head><meta charset="utf-8">
<title>SubWatch</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&display=swap" rel="stylesheet">
<script src="/static/tailwind.js"></script>
<style type="text/tailwindcss">
  @theme {
    --font-sans: "Inter", ui-sans-serif, system-ui, sans-serif;
    /* eatnaked-inspired: warm near-black, charcoal surfaces, ember-orange accent */
    --color-bg: #0a0a0b;
    --color-surface: #121214;
    --color-surface2: #1b1b1e;
    --color-accent: #e8602e;
    --color-accent2: #f08a3c;
  }
</style>
<style>
  /* warm near-black base with a soft ember glow rising from the bottom (eatnaked vibe) */
  body{background:#0a0a0b;color:#ececee;font-family:"Inter",system-ui,sans-serif;
    background-image:radial-gradient(120% 80% at 50% 120%, rgba(232,96,46,0.10) 0%, rgba(232,96,46,0.03) 35%, transparent 60%);
    background-attachment:fixed}
  .cjk{font-family:"PingFang SC","Microsoft YaHei",sans-serif}
  ::-webkit-scrollbar{width:8px}::-webkit-scrollbar-thumb{background:#2a2a2e;border-radius:6px}
  [x-cloak]{display:none!important}
  /* READABILITY: lift the dim greys on the near-black bg to accessible contrast,
     and keep the ember accent for emphasis only (not body text). */
  .text-slate-600{color:#8a8a92!important}   /* faint → still legible */
  .text-slate-500{color:#9a9aa3!important}   /* hints/labels */
  .text-slate-400{color:#b4b4bd!important}   /* secondary text */
  .text-slate-300{color:#d0d0d6!important}
  .text-slate-200{color:#e4e4e8!important}
  /* word translations: a soft warm cream reads far better than orange, esp. CJK */
  .text-accent2{color:#f3c9a3!important}
  /* cards: a hair lighter + clearer hairline so they separate from the bg */
  .bg-surface{background:#161618!important}
  .bg-surface2{background:#202023!important}
  .border-white\/5{border-color:rgba(255,255,255,0.08)!important}
  /* ember accent helpers */
  .glow-accent{box-shadow:0 8px 30px -8px rgba(232,96,46,0.5)}
  .ring-dashed{border:1px dashed rgba(255,255,255,0.08);border-radius:9999px}
  /* branded loading orbit (eatnaked-style dashed rings + ember) */
  .orbit{position:fixed;inset:0;display:flex;align-items:center;justify-content:center;background:#0a0a0b;z-index:50;transition:opacity .5s}
  .orbit .ring{position:absolute;border-radius:9999px;border:1px dashed rgba(255,255,255,0.07);animation:spin 18s linear infinite}
  @keyframes spin{to{transform:rotate(360deg)}}
  @keyframes pulse-soft{0%,100%{opacity:.55}50%{opacity:1}}
  .line-clamp-2{display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden}
  /* 3D flip card */
  .flip{perspective:1000px;height:150px}
  .flip-inner{position:relative;width:100%;height:100%;transition:transform .5s cubic-bezier(.4,.2,.2,1);transform-style:preserve-3d}
  .flip.flipped .flip-inner{transform:rotateY(180deg)}
  .flip-face{position:absolute;inset:0;backface-visibility:hidden;-webkit-backface-visibility:hidden;border-radius:.75rem;display:flex;flex-direction:column;overflow:hidden}
  .flip-back{transform:rotateY(180deg)}
</style>
<script src="/static/lucide.js"></script>
<script>
  // Fail-safe: if Alpine hasn't removed [x-cloak] within 3s (slow/blocked CDN),
  // reveal the UI anyway so the page never stays blank.
  document.addEventListener('DOMContentLoaded',function(){
    setTimeout(function(){document.querySelectorAll('[x-cloak]').forEach(function(e){e.removeAttribute('x-cloak')});},3000);
  });
</script>
</head>
<body class="min-h-screen">

<!-- branded loading orbit (eatnaked-style): dashed rings + ember mark, fades out on load -->
<div id="orbit" class="orbit">
  <div class="ring" style="width:380px;height:380px"></div>
  <div class="ring" style="width:260px;height:260px;animation-direction:reverse;animation-duration:24s"></div>
  <div class="flex flex-col items-center gap-3" style="animation:pulse-soft 2s ease-in-out infinite">
    <div class="text-5xl">🎬</div>
    <div class="tracking-[0.3em] text-xs text-slate-500 uppercase">SubWatch</div>
  </div>
</div>
<script>
  // fade the orbit once the app is up (or after a max wait)
  function hideOrbit(){var o=document.getElementById('orbit'); if(o){o.style.opacity='0'; setTimeout(()=>o.remove(),550);}}
  window.addEventListener('load',()=>setTimeout(hideOrbit,400));
  setTimeout(hideOrbit,3500);
</script>

<div x-data="app" x-cloak>

<!-- glass header -->
<header class="sticky top-0 z-20 backdrop-blur-xl bg-black/40 border-b border-white/5 px-6 py-3.5 flex items-center gap-4">
  <div class="flex items-center gap-2.5">
    <span class="text-xl">🎬</span>
    <h1 class="text-[17px] font-700 tracking-[-0.01em]">Sub<span class="text-accent">Watch</span></h1>
  </div>
  <div class="flex items-center gap-2 ml-3">
    <span class="flex items-center gap-1.5 text-[11px] px-2.5 py-1 rounded-full border"
          :class="(state.watch||state.listen)?'bg-accent/15 text-accent border-accent/30':'bg-white/5 text-slate-500 border-white/5'">
      <span class="w-1.5 h-1.5 rounded-full" :class="(state.watch||state.listen)?'bg-accent':'bg-slate-600'" :style="(state.watch||state.listen)?'animation:pulse-soft 1.6s ease-in-out infinite':''"></span>
      <span x-text="state.listen?'listening':(state.watch?'capturing':'idle')"></span>
    </span>
    <span class="flex items-center gap-1.5 text-[11px] px-2.5 py-1 rounded-full border"
          :class="state.overlay?'bg-white/10 text-slate-200 border-white/10':'bg-white/5 text-slate-500 border-white/5'"
          x-show="state.overlay"><span x-text="'mask on'"></span></span>
  </div>
  <div class="flex-1"></div>
  <div class="text-xs text-slate-400 tracking-wide">
    <span class="text-white font-700" x-text="state.stats?.total||0"></span> terms
    <span class="text-slate-600 mx-1">·</span>
    <span class="text-accent font-600" x-text="favCount"></span> saved
  </div>
</header>

<main class="grid grid-cols-1 lg:grid-cols-[360px_1fr] gap-5 p-5 max-w-[1500px] mx-auto">

  <!-- LEFT: controls -->
  <div class="space-y-5">
    <!-- capture -->
    <section class="rounded-2xl bg-surface border border-white/5 p-5">
      <h2 class="text-[11px] font-600 tracking-[0.12em] uppercase text-slate-400 mb-3">Capture</h2>
      <div class="text-[11px] text-slate-500 mb-1">📺 From subtitles on screen</div>
      <button @click="toggle('watch')"
        class="w-full py-2.5 rounded-xl font-600 transition flex items-center justify-center gap-2"
        :class="state.watch?'bg-rose-500 text-white hover:bg-rose-400':'bg-gradient-to-r from-accent to-accent2 text-white hover:brightness-110'">
        <i :data-lucide="state.watch?'square':'play'" class="w-4 h-4"></i>
        <span x-text="state.watch?'Stop watching':'Start watching'"></span>
      </button>
      <button @click="autodetect()"
        class="w-full mt-2 py-2 rounded-xl bg-surface2 hover:bg-white/10 transition text-sm flex items-center justify-center gap-2">
        <i data-lucide="scan-line" class="w-4 h-4"></i> Auto-detect subtitle region
      </button>
      <div class="flex items-center gap-2 mt-3 text-sm">
        <span class="text-slate-400">monitor</span>
        <select x-model.number="state.display" @change="pickDisplay()" @mousedown="loadDisplays()"
          class="flex-1 bg-bg border border-white/10 rounded-lg px-2 py-1.5 text-slate-200">
          <template x-for="d in displays" :key="d.index">
            <option :value="d.index" x-text="d.name+' ('+d.w+'×'+d.h+')'"></option>
          </template>
        </select>
      </div>
      <div class="text-[11px] text-slate-500 mt-4 mb-1">🎧 From audio (no subtitle needed)</div>
      <div class="flex items-center gap-2 mb-2 text-sm">
        <span class="text-slate-400">spoken language</span>
        <select x-model="state.audio_lang" @change="setCfg({audio_lang:state.audio_lang})"
          class="bg-bg border border-white/10 rounded-lg px-2 py-1.5 text-slate-200">
          <option value="ja">Japanese</option>
          <option value="zh">Chinese</option>
          <option value="ko">Korean</option>
          <option value="es">Spanish</option>
          <option value="fr">French</option>
          <option value="de">German</option>
          <option value="en">English</option>
        </select>
      </div>
      <button @click="toggle('listen')"
        class="w-full py-2.5 rounded-xl font-600 transition flex items-center justify-center gap-2"
        :class="state.listen?'bg-rose-500 text-white hover:bg-rose-400':'bg-gradient-to-r from-teal-500 to-emerald-500 text-white hover:brightness-110'">
        <i :data-lucide="state.listen?'square':'ear'" class="w-4 h-4"></i>
        <span x-text="state.listen?'Stop listening':'Start listening (audio)'"></span>
      </button>
      <p class="text-[11px] text-slate-500 mt-1.5 leading-snug">Transcribes audio locally with Whisper; Codex cleans and grades the English text. Select a system-audio input such as Stereo Mix or BlackHole.</p>
      <button @click="toggle('subtitle')"
        class="w-full mt-2 py-2 rounded-xl transition text-sm flex items-center justify-center gap-2"
        :class="state.subtitle?'bg-rose-500 text-white hover:bg-rose-400':'bg-surface2 hover:bg-white/10'">
        <i data-lucide="captions" class="w-4 h-4"></i>
        <span x-text="state.subtitle?'Hide subtitle overlay':'Show live subtitle on screen'"></span>
      </button>
      <label class="flex items-center justify-between gap-2 mt-4 pt-3 border-t border-white/5 text-sm cursor-pointer">
        <span class="flex items-center gap-1.5"><i data-lucide="shield-check" class="w-3.5 h-3.5 text-slate-400"></i> Local-only (disable Codex)</span>
        <input type="checkbox" x-model="state.local_only" @change="setCfg({local_only:state.local_only})" class="accent-accent w-4 h-4">
      </label>
      <p class="text-[11px] text-slate-500 mt-1 leading-snug" x-show="state.local_only">On-device Whisper + word lists only. Log in with Codex CLI, then turn this off for Codex grading and summaries.</p>
    </section>

    <!-- mask overlay -->
    <section class="rounded-2xl bg-surface border border-white/5 p-5">
      <h2 class="text-[11px] font-600 tracking-[0.12em] uppercase text-slate-400 mb-3">Mask overlay</h2>
      <p class="text-xs text-slate-500 mb-3 leading-snug">A frosted bar that blurs the original subtitle on screen so you read English instead. Drag to move, corner/scroll to resize.</p>
      <button @click="toggle('overlay')"
        class="w-full py-2.5 rounded-xl font-600 transition flex items-center justify-center gap-2"
        :class="state.overlay?'bg-rose-500 text-white hover:bg-rose-400':'bg-gradient-to-r from-fuchsia-500 to-pink-500 text-white hover:brightness-110'">
        <i :data-lucide="state.overlay?'eye-off':'square-dashed'" class="w-4 h-4"></i>
        <span x-text="state.overlay?'Hide mask':'Show mask over subtitle'"></span>
      </button>
      <div class="mt-4" :class="state.overlay?'opacity-100':'opacity-40 pointer-events-none'">
        <div class="flex items-center justify-between text-xs text-slate-400 mb-1.5">
          <span>Transparency</span>
          <span class="text-slate-300" x-text="Math.round((1-tint)*100)+'% see-through'"></span>
        </div>
        <input type="range" min="0" max="100" step="2" x-model.number="tintPct" @input="setTint()"
          class="w-full accent-fuchsia-500">
        <div class="flex justify-between text-[10px] text-slate-600"><span>clear glass</span><span>solid cover</span></div>
      </div>
    </section>

    <!-- word overlay -->
    <section class="rounded-2xl bg-surface border border-white/5 p-5">
      <h2 class="text-[11px] font-600 tracking-[0.12em] uppercase text-slate-400 mb-3">Word overlay</h2>
      <p class="text-xs text-slate-500 mb-3 leading-snug">A floating panel that shows the hard words from each line — word + 中文 — and keeps them on screen for a few seconds, so you can read them even after the subtitle passes. Drag to move.</p>
      <button @click="toggle('vocab')"
        class="w-full py-2.5 rounded-xl font-600 transition flex items-center justify-center gap-2"
        :class="state.vocab?'bg-rose-500 text-white hover:bg-rose-400':'bg-gradient-to-r from-amber-500 to-orange-500 text-white hover:brightness-110'">
        <i :data-lucide="state.vocab?'eye-off':'book-open'" class="w-4 h-4"></i>
        <span x-text="state.vocab?'Hide word overlay':'Show hard words on screen'"></span>
      </button>
    </section>

    <!-- level -->
    <section class="rounded-2xl bg-surface border border-white/5 p-5">
      <h2 class="text-[11px] font-600 tracking-[0.12em] uppercase text-slate-400 mb-3 flex items-center justify-between">
        Your level
        <button @click="openTest()" class="text-accent hover:text-accent normal-case tracking-normal font-500 text-xs flex items-center gap-1">
          <i data-lucide="clipboard-check" class="w-3.5 h-3.5"></i> take test</button>
      </h2>
      <div class="text-sm" x-show="!profile.level">Not tested yet — take the test so captures match your level.</div>
      <div class="text-sm" x-show="profile.level">
        <span class="inline-block px-2.5 py-1 rounded-lg bg-accent/20 text-accent font-600 capitalize" x-text="profile.level"></span>
        <span class="text-slate-400 ml-2" x-show="profile.min_band">band <span x-text="profile.min_band"></span>+</span>
        <div class="text-xs text-slate-500 mt-2">knew <span x-text="(profile.known||[]).length"></span> · new <span x-text="(profile.unknown||[]).length"></span></div>
        <div class="text-xs text-amber-400/80 mt-1" x-show="lastAdjust" x-text="lastAdjust"></div>
      </div>

      <!-- test modal-ish inline -->
      <div x-show="testOpen" x-transition class="mt-4 border-t border-white/10 pt-3">
        <p class="text-xs text-slate-400 mb-2">Tap every word/phrase you do NOT know, then submit.</p>
        <div class="max-h-52 overflow-auto">
          <template x-for="it in testItems" :key="it">
            <span @click="toggleChip(it)"
              class="inline-block px-2.5 py-1 m-0.5 rounded-full text-[13px] cursor-pointer select-none transition"
              :class="testMiss.includes(it)?'bg-rose-500 text-white font-600':'bg-surface2 hover:bg-white/10'"
              x-text="it"></span>
          </template>
        </div>
        <button @click="submitTest()" class="w-full mt-3 py-2 rounded-xl bg-gradient-to-r from-accent to-accent2 text-white font-600 text-sm">Submit test</button>
      </div>
    </section>
  </div>

  <!-- RIGHT: tabbed feed -->
  <section class="rounded-2xl bg-surface border border-white/5 p-5 flex flex-col" style="height:calc(100vh - 40px);min-height:520px;overflow:hidden">
    <div class="flex items-center gap-2 mb-3">
      <button @click="tab='live'"
        class="px-4 py-2 rounded-xl text-sm font-600 transition flex items-center gap-2"
        :class="tab==='live'?'bg-gradient-to-r from-accent to-accent2 text-white':'bg-surface2 text-slate-400 hover:bg-white/10'">
        <i data-lucide="radio" class="w-4 h-4"></i> Live captures</button>
      <button @click="tab='dict';loadFavs()"
        class="px-4 py-2 rounded-xl text-sm font-600 transition flex items-center gap-2"
        :class="tab==='dict'?'bg-gradient-to-r from-accent to-accent2 text-white':'bg-surface2 text-slate-400 hover:bg-white/10'">
        <i data-lucide="star" class="w-4 h-4"></i> My Dictionary
        <span class="text-xs px-1.5 rounded-full bg-white/10" x-text="favCount"></span></button>
      <button @click="tab='sub'"
        class="px-4 py-2 rounded-xl text-sm font-600 transition flex items-center gap-2"
        :class="tab==='sub'?'bg-gradient-to-r from-teal-500 to-emerald-500 text-white':'bg-surface2 text-slate-400 hover:bg-white/10'">
        <i data-lucide="captions" class="w-4 h-4"></i> Live Subtitle</button>
      <button @click="tab='meeting';loadMeetings()"
        class="px-4 py-2 rounded-xl text-sm font-600 transition flex items-center gap-2"
        :class="tab==='meeting'?'bg-gradient-to-r from-indigo-500 to-violet-500 text-white':'bg-surface2 text-slate-400 hover:bg-white/10'">
        <i data-lucide="mic" class="w-4 h-4"></i> Meeting
        <span x-show="state.meeting" class="w-2 h-2 rounded-full bg-rose-500 animate-pulse"></span></button>
      <div class="flex-1"></div>
      <!-- grid / list view toggle (hidden on the subtitle tab) -->
      <div x-show="tab!=='sub'" class="flex bg-surface2 rounded-lg p-0.5 mr-1">
        <button @click="view='grid'" title="Grid view"
          class="px-2 py-1.5 rounded-md transition"
          :class="view==='grid'?'bg-accent text-white':'text-slate-400 hover:text-slate-200'">
          <i data-lucide="layout-grid" class="w-4 h-4"></i></button>
        <button @click="view='list'" title="List view"
          class="px-2 py-1.5 rounded-md transition"
          :class="view==='list'?'bg-accent text-white':'text-slate-400 hover:text-slate-200'">
          <i data-lucide="list" class="w-4 h-4"></i></button>
      </div>
      <button @click="tab==='live'?loadFeed():loadFavs()" class="text-slate-400 hover:text-slate-200 p-1.5"><i data-lucide="refresh-cw" class="w-4 h-4"></i></button>
    </div>

    <!-- LIVE -->
    <div x-show="tab==='live'" class="flex-1 overflow-auto">
      <p class="text-xs text-slate-500 mb-2" x-text="view==='grid'?'Tap a card to flip for detail · ✓ knew it · ✗ new · ★ save.':'✓ knew it · ✗ new · ★ save.'"></p>
      <template x-if="!feed.length">
        <p class="text-sm text-slate-500 py-8 text-center">No captures yet. Start watching a subtitled video.</p>
      </template>

      <!-- GRID VIEW: flip cards -->
      <div x-show="view==='grid'" class="grid gap-3" style="grid-template-columns:repeat(auto-fill,minmax(190px,1fr))">
        <template x-for="t in feed" :key="t.word">
          <div class="flip" :class="flipped===t.word?'flipped':''">
            <div class="flip-inner">
              <div class="flip-face bg-bg/70 border border-white/8 p-3 hover:border-accent/40 transition" @click="flip(t.word)">
                <div class="flex items-start justify-between gap-1">
                  <span class="font-700 text-[16px] leading-tight" x-text="label(t)"></span>
                  <i x-show="t.audio_path" @click.stop="playAudio(t.word)" data-lucide="volume-2" class="w-4 h-4 shrink-0 text-slate-500 cursor-pointer hover:text-accent"></i>
                </div>
                <span class="text-accent2 font-600 text-[14px] cjk mt-1 leading-snug" x-text="t.translation||''"></span>
                <span class="flex-1"></span>
                <div class="flex items-center gap-3 pt-2 border-t border-white/5">
                  <i @click.stop="fav(t)" data-lucide="star" class="w-4 h-4 cursor-pointer" :class="t.favorite?'text-amber-400 fill-amber-400':'text-slate-500 hover:text-amber-400'"></i>
                  <i @click.stop="mark(t,true)" data-lucide="check" class="w-4 h-4 cursor-pointer text-slate-500 hover:text-emerald-400"></i>
                  <i @click.stop="mark(t,false)" data-lucide="x" class="w-4 h-4 cursor-pointer text-slate-500 hover:text-rose-400"></i>
                  <span class="flex-1"></span>
                  <i data-lucide="rotate-cw" class="w-3.5 h-3.5 text-slate-600"></i>
                </div>
              </div>
              <div class="flip-face flip-back bg-surface2 border border-accent/40 p-3 overflow-auto" @click="flip(t.word)">
                <div class="text-[12px] text-slate-200 leading-snug" x-text="t.definition||''"></div>
                <div class="text-[11px] text-slate-400 italic mt-1.5" x-show="t.context" x-text="'“'+(t.context||'')+'”'"></div>
                <div class="text-[12px] text-emerald-300/90 mt-1.5 pl-2 border-l-2 border-accent/40 cjk" x-show="t.subtitle_cn" x-text="'中文: '+(t.subtitle_cn||'')"></div>
              </div>
            </div>
          </div>
        </template>
      </div>

      <!-- LIST VIEW: full-detail rows -->
      <div x-show="view==='list'" class="space-y-2.5">
        <template x-for="t in feed" :key="t.word">
          <div class="rounded-xl bg-bg/60 border border-white/5 p-3.5 hover:bg-surface2 transition group">
            <div class="flex items-start gap-2">
              <div class="flex-1">
                <span class="font-700 text-[15px]" x-text="label(t)"></span>
                <span class="text-accent2 font-600 ml-2 cjk" x-text="t.translation||''"></span>
                <span class="text-xs text-slate-500 ml-1" x-text="'·'+t.times_seen+'×'"></span>
              </div>
              <div class="flex items-center gap-2.5 opacity-70 group-hover:opacity-100 transition">
                <i x-show="t.audio_path" @click="playAudio(t.word)" data-lucide="volume-2" class="w-4 h-4 cursor-pointer hover:text-accent"></i>
                <i @click="fav(t)" data-lucide="star" class="w-4 h-4 cursor-pointer" :class="t.favorite?'text-amber-400 fill-amber-400':'text-slate-500 hover:text-amber-400'"></i>
                <i @click="mark(t,true)" data-lucide="check" class="w-4 h-4 cursor-pointer text-slate-500 hover:text-emerald-400"></i>
                <i @click="mark(t,false)" data-lucide="x" class="w-4 h-4 cursor-pointer text-slate-500 hover:text-rose-400"></i>
              </div>
            </div>
            <div class="text-[13px] text-slate-300 mt-1" x-show="t.definition" x-text="t.definition"></div>
            <div class="text-xs text-slate-500 italic mt-1" x-show="t.context" x-text="'“'+(t.context||'')+'”'"></div>
            <div class="text-[13px] text-emerald-300/90 mt-1 pl-2 border-l-2 border-accent/40 cjk" x-show="t.subtitle_cn" x-text="'中文: '+(t.subtitle_cn||'')"></div>
          </div>
        </template>
      </div>
    </div>

    <!-- DICTIONARY -->
    <div x-show="tab==='dict'" class="flex-1 overflow-auto">
      <template x-if="!favs.length">
        <p class="text-sm text-slate-500 py-8 text-center">No saved words yet. On Live, tap ★ to keep a word here.</p>
      </template>
      <!-- GRID VIEW -->
      <div x-show="view==='grid'" class="grid gap-3" style="grid-template-columns:repeat(auto-fill,minmax(190px,1fr))">
        <template x-for="t in favs" :key="t.word">
          <div class="flip" :class="flipped===t.word?'flipped':''">
            <div class="flip-inner">
              <div class="flip-face bg-bg/70 border border-amber-400/20 p-3" @click="flip(t.word)">
                <div class="flex items-start justify-between gap-1">
                  <span class="font-700 text-[16px] leading-tight" x-text="label(t)"></span>
                  <i x-show="t.audio_path" @click.stop="playAudio(t.word)" data-lucide="volume-2" class="w-4 h-4 shrink-0 text-slate-500 cursor-pointer hover:text-accent"></i>
                </div>
                <span class="text-accent2 font-600 text-[14px] cjk mt-1 leading-snug" x-text="t.translation||''"></span>
                <span class="flex-1"></span>
                <div class="flex items-center gap-3 pt-2 border-t border-white/5">
                  <i @click.stop="fav(t)" data-lucide="star" class="w-4 h-4 cursor-pointer text-amber-400 fill-amber-400"></i>
                  <span class="flex-1"></span>
                  <i data-lucide="rotate-cw" class="w-3.5 h-3.5 text-slate-600"></i>
                </div>
              </div>
              <div class="flip-face flip-back bg-surface2 border border-amber-400/40 p-3 overflow-auto" @click="flip(t.word)">
                <div class="text-[12px] text-slate-200 leading-snug" x-text="t.definition||''"></div>
                <div class="text-[11px] text-slate-400 italic mt-1.5" x-show="t.context" x-text="'“'+(t.context||'')+'”'"></div>
                <div class="text-[12px] text-emerald-300/90 mt-1.5 pl-2 border-l-2 border-amber-400/40 cjk" x-show="t.subtitle_cn" x-text="'中文: '+(t.subtitle_cn||'')"></div>
              </div>
            </div>
          </div>
        </template>
      </div>

      <!-- LIST VIEW -->
      <div x-show="view==='list'" class="space-y-2.5">
        <template x-for="t in favs" :key="t.word">
          <div class="rounded-xl bg-bg/60 border border-amber-400/15 p-3.5">
            <div class="flex items-start gap-2">
              <div class="flex-1">
                <span class="font-700 text-[15px]" x-text="label(t)"></span>
                <span class="text-accent2 font-600 ml-2 cjk" x-text="t.translation||''"></span>
              </div>
              <div class="flex items-center gap-2.5">
                <i x-show="t.audio_path" @click="playAudio(t.word)" data-lucide="volume-2" class="w-4 h-4 cursor-pointer hover:text-accent"></i>
                <i @click="fav(t)" data-lucide="star" class="w-4 h-4 cursor-pointer text-amber-400 fill-amber-400"></i>
              </div>
            </div>
            <div class="text-[13px] text-slate-300 mt-1" x-show="t.definition" x-text="t.definition"></div>
            <div class="text-xs text-slate-500 italic mt-1" x-show="t.context" x-text="'“'+(t.context||'')+'”'"></div>
            <div class="text-[13px] text-emerald-300/90 mt-1 pl-2 border-l-2 border-amber-400/40 cjk" x-show="t.subtitle_cn" x-text="'中文: '+(t.subtitle_cn||'')"></div>
          </div>
        </template>
      </div>
    </div>

    <!-- LIVE SUBTITLE -->
    <div x-show="tab==='sub'" class="flex-1 overflow-auto flex flex-col">
      <template x-if="!state.listen">
        <p class="text-sm text-slate-500 py-8 text-center">Start <b class="text-teal-300">🎧 listening</b> (audio mode) to see the live subtitle here.</p>
      </template>
      <div x-show="state.listen" class="flex-1 flex flex-col justify-end gap-2 pb-2">
        <template x-for="(l,i) in transcript" :key="i">
          <div class="transition" :class="i===transcript.length-1?'opacity-100':'opacity-45'">
            <p class="leading-snug" :class="i===transcript.length-1?'text-2xl font-600 text-white':'text-base text-slate-400'"
               x-text="i===transcript.length-1?subLive:l.text"></p>
            <span class="text-[10px] uppercase tracking-wide text-teal-400/70" x-show="l.lang && l.lang!=='en'" x-text="l.lang+' → english'"></span>
          </div>
        </template>
        <p x-show="!transcript.length" class="text-sm text-slate-500 text-center py-8">Listening… subtitle will appear as people speak.</p>
      </div>
    </div>

    <!-- MEETING MODE -->
    <div x-show="tab==='meeting'" class="flex-1 overflow-hidden flex flex-col">
      <!-- controls -->
      <div class="flex items-center gap-2 mb-3">
        <button @click="toggleMeeting()"
          class="px-4 py-2 rounded-xl text-sm font-600 transition flex items-center gap-2"
          :class="state.meeting?'bg-rose-500 text-white hover:bg-rose-400':'bg-gradient-to-r from-indigo-500 to-violet-500 text-white hover:brightness-110'">
          <i :data-lucide="state.meeting?'square':'mic'" class="w-4 h-4"></i>
          <span x-text="state.meeting?'Stop & summarize':'Start meeting capture'"></span></button>
        <span x-show="state.meeting" class="text-xs text-rose-400 flex items-center gap-1">
          <span class="w-2 h-2 rounded-full bg-rose-500 animate-pulse"></span> recording captions</span>
        <div class="flex-1"></div>
        <button @click="loadMeetings()" class="text-slate-400 hover:text-slate-200 p-1.5"><i data-lucide="refresh-cw" class="w-4 h-4"></i></button>
      </div>
      <p class="text-[11px] text-slate-500 mb-2 leading-snug">Open Zoom's <b>Live Caption (CC)</b> window, then Start. SubWatch reads the captions into a saved, searchable transcript with AI notes — and you can ask questions about the meeting. Notes, chat, and summaries use Codex.</p>
      <!-- calendar sync status -->
      <div class="flex items-center gap-2 text-[11px] text-slate-500 mb-3">
        <i data-lucide="calendar-check" class="w-3.5 h-3.5"></i>
        <span x-text="cal.synced_at ? ('Calendar synced '+calAgo) : 'Calendar not synced'"></span>
        <span x-show="cal.upcoming && cal.upcoming.length" class="text-slate-600">·</span>
        <span x-show="cal.upcoming && cal.upcoming.length" class="text-slate-400 truncate"
          x-text="cal.upcoming&&cal.upcoming.length ? ('next: '+cal.upcoming[0].subject) : ''"></span>
      </div>

      <!-- LIVE view: transcript (left) + notes/chat (right) -->
      <div x-show="state.meeting || curMeeting" class="flex-1 min-h-0 grid grid-cols-2 gap-3 overflow-hidden">
        <!-- transcript -->
        <div class="flex flex-col min-h-0 overflow-hidden bg-surface2/40 rounded-xl p-3">
          <div class="text-[11px] uppercase tracking-wide text-slate-400 mb-2 flex items-center justify-between">
            <span>Transcript</span>
            <a :href="curMeeting?('/api/meeting/export?id='+curMeeting):'#'" x-show="curMeeting"
               class="text-indigo-300 hover:text-indigo-200 normal-case tracking-normal flex items-center gap-1"><i data-lucide="download" class="w-3 h-3"></i>export</a>
          </div>
          <div class="flex-1 min-h-0 overflow-auto space-y-2 pr-1" id="mtgTranscript">
            <template x-for="(l,i) in mLines" :key="i">
              <div class="flex gap-2" :class="l.partial?'opacity-60':''">
                <!-- avatar: colored initials circle, only on a speaker change (chat-style grouping) -->
                <div class="w-7 shrink-0">
                  <div x-show="showAvatar(i)" class="w-7 h-7 rounded-full flex items-center justify-center text-[10px] font-700 text-white"
                       :style="'background:'+avatarColor(l.speaker)" x-text="initials(l.speaker)"></div>
                </div>
                <div class="flex-1 min-w-0">
                  <div x-show="showAvatar(i)" class="flex items-center gap-2 mb-0.5">
                    <span class="text-xs font-600" :style="'color:'+avatarColor(l.speaker)" x-text="l.speaker||'Unknown speaker'"></span>
                    <span class="text-[10px] text-slate-600" x-text="fmtTime(l.t)"></span>
                  </div>
                  <div class="text-sm leading-snug text-slate-200" :class="l.partial?'italic':''" x-text="l.text"></div>
                </div>
              </div>
            </template>
            <p x-show="!mLines.length" class="text-sm text-slate-500 text-center py-8">Waiting for captions… open Zoom's CC window.</p>
          </div>
        </div>
        <!-- notes + chat -->
        <div class="flex flex-col min-h-0 overflow-hidden gap-3">
          <div class="overflow-auto bg-surface2/40 rounded-xl p-3" style="max-height:40%">
            <div class="text-[11px] uppercase tracking-wide text-slate-400 mb-2">Live notes</div>
            <div class="prose-mtg text-sm text-slate-200 leading-snug" x-html="mNotes?md(mNotes):'Notes will appear as the meeting progresses…'"></div>
            <template x-if="mActions.length">
              <div class="mt-2">
                <div class="text-[11px] uppercase tracking-wide text-amber-400/80 mb-1">Action items</div>
                <template x-for="(a,i) in mActions" :key="i">
                  <div class="text-xs text-slate-300">• <b x-text="a.action"></b> <span class="text-slate-500" x-text="'— '+(a.owner||'TBD')+' ('+(a.deadline||'TBD')+')'"></span></div>
                </template>
              </div>
            </template>
          </div>
          <!-- chat -->
          <div class="flex-1 min-h-0 flex flex-col overflow-hidden bg-surface2/40 rounded-xl p-3">
            <div class="text-[11px] uppercase tracking-wide text-slate-400 mb-2">Ask about the meeting</div>
            <div class="flex-1 min-h-0 overflow-auto space-y-2 mb-2" id="mtgChat">
              <template x-for="(c,i) in mChat" :key="i">
                <div :class="c.role==='user'?'flex justify-end':'flex justify-start'">
                  <span class="inline-block rounded-xl px-3 py-2 text-sm max-w-[90%] leading-snug text-left"
                    :class="c.role==='user'?'bg-indigo-500/30 text-indigo-100':'bg-white/5 text-slate-200'"
                    x-html="c.role==='user'?(c.content):md(c.content)"></span>
                </div>
              </template>
              <p x-show="mChatBusy" class="text-xs text-slate-500">thinking…</p>
            </div>
            <form @submit.prevent="askMeeting()" class="flex gap-2">
              <input x-model="mQuestion" :disabled="!curMeeting" placeholder="e.g. what did I commit to?"
                class="flex-1 bg-surface2 rounded-xl px-3 py-2 text-sm outline-none border border-white/5 focus:border-indigo-500/50">
              <button type="submit" :disabled="!curMeeting||mChatBusy" class="px-3 py-2 rounded-xl bg-indigo-500 text-white text-sm disabled:opacity-40"><i data-lucide="send" class="w-4 h-4"></i></button>
            </form>
          </div>
        </div>
      </div>

      <!-- meetings list (when not actively viewing one) -->
      <div x-show="!state.meeting && !curMeeting" class="flex-1 overflow-auto">
        <div class="text-[11px] uppercase tracking-wide text-slate-400 mb-2">Past meetings</div>
        <p x-show="!meetings.length" class="text-sm text-slate-500 text-center py-8">No meetings yet. Open Zoom CC and hit Start.</p>
        <template x-for="m in meetings" :key="m.id">
          <div class="group rounded-xl bg-surface2/40 hover:bg-white/5 p-3 mb-2 flex items-center justify-between gap-2">
            <div @click="openMeeting(m.id)" class="cursor-pointer flex-1 min-w-0">
              <div class="text-sm text-slate-200 font-600 truncate" x-text="m.title"></div>
              <div class="text-xs text-slate-500"><span x-text="m.segment_count"></span> lines · <span x-text="m.status"></span></div>
            </div>
            <div class="flex items-center gap-1 shrink-0">
              <i @click.stop="renameMeeting(m)" data-lucide="pencil" title="Rename"
                 class="w-4 h-4 text-slate-500 hover:text-indigo-300 cursor-pointer opacity-0 group-hover:opacity-100"></i>
              <i @click.stop="deleteMeeting(m)" data-lucide="trash-2" title="Delete"
                 class="w-4 h-4 text-slate-500 hover:text-rose-400 cursor-pointer opacity-0 group-hover:opacity-100"></i>
              <i @click="openMeeting(m.id)" data-lucide="chevron-right" class="w-4 h-4 text-slate-500 cursor-pointer"></i>
            </div>
          </div>
        </template>
      </div>
      <button x-show="curMeeting && !state.meeting" @click="curMeeting=null;loadMeetings()" class="mt-2 text-xs text-slate-400 hover:text-slate-200 flex items-center gap-1"><i data-lucide="arrow-left" class="w-3 h-3"></i> back to meetings</button>
    </div>
  </section>
</main>

<!-- toast -->
<div x-show="toastMsg" x-transition x-cloak class="fixed bottom-5 right-5 bg-surface2 border border-white/10 rounded-xl px-4 py-3 text-sm shadow-xl" x-text="toastMsg"></div>
</div>

<script>
document.addEventListener('alpine:init',()=>{ window.Alpine.data('app', app); });
function app(){return {
  state:{watch:false,overlay:false,display:1,display_mode:'hide_chinese',stats:{}},
  profile:{}, feed:[], favs:[], favCount:0, tab:'live', flipped:null, view:'grid', transcript:[], subLive:'',
  tint:0.55, tintPct:45, displays:[],
  testOpen:false, testItems:[], testMiss:[], lastAdjust:'', toastMsg:'',
  meetings:[], curMeeting:null, mLines:[], mNotes:'', mActions:[], mDecisions:[], mChat:[], mQuestion:'', mChatBusy:false, mStartedAt:0, cal:{}, calAgo:'',
  async api(p,b){const r=await fetch(p,{method:b?'POST':'GET',headers:{'Content-Type':'application/json'},body:b?JSON.stringify(b):null});return r.json()},
  icons(){this.$nextTick(()=>window.lucide&&lucide.createIcons())},
  toast(m){this.toastMsg=m;clearTimeout(this._t);this._t=setTimeout(()=>this.toastMsg='',1800)},
  label(t){return t.kind==='phrase'?(t.matched||t.word.replace('phrase:','')):t.word},
  flip(w){this.flipped=this.flipped===w?null:w},
  async setTint(){this._dragging=true;this.tint=this.tintPct/100;
    await this.api('/api/config',{overlay_tint:this.tint});
    clearTimeout(this._dragT);this._dragT=setTimeout(()=>this._dragging=false,1200)},
  termKey(t){return (t.matched||t.word.replace('phrase:',''))},
  async refresh(){this.state=await this.api('/api/state');
    // keep the slider synced with config (so the bar's [ ] keys reflect here too),
    // but don't fight the user while they're actively dragging the slider.
    if(this.state.overlay_tint!=null && !this._dragging){
      this.tint=this.state.overlay_tint; this.tintPct=Math.round(this.tint*100);}
    this.icons()},
  async loadFeed(){const d=await this.api('/api/terms');this.feed=d.terms;this.icons()},
  async loadFavs(){const d=await this.api('/api/favorites');this.favs=d.favorites;this.favCount=d.favorites.length;this.icons()},
  async loadTranscript(){const d=await this.api('/api/transcript');this.transcript=d.lines||[];},
  // ── Meeting Mode ──
  fmtTime(ms){ // wall-clock time of a line = meeting start + offset, shown in LOCAL (Pacific) time
    ms=ms||0;
    if(this.mStartedAt){
      const d=new Date(this.mStartedAt*1000 + ms);
      return d.toLocaleTimeString([], {hour:'numeric', minute:'2-digit'});
    }
    const s=Math.floor(ms/1000);return String(Math.floor(s/60)).padStart(2,'0')+':'+String(s%60).padStart(2,'0');
  },
  md(t){ // tiny, safe markdown→HTML for chat/notes (bold, italic, headers, bullets, code)
    if(!t)return '';
    let s=String(t).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
    s=s.replace(/`([^`]+)`/g,'<code class="bg-white/10 px-1 rounded text-[12px]">$1</code>');
    s=s.replace(/\*\*([^*]+)\*\*/g,'<b>$1</b>').replace(/(^|[^*])\*([^*]+)\*/g,'$1<i>$2</i>');
    const lines=s.split(String.fromCharCode(10)); let out=[], inList=false;
    for(let ln of lines){
      let m;
      if(m=ln.match(/^\s*#{1,6}\s+(.*)$/)){ if(inList){out.push('</ul>');inList=false;} out.push('<div class="font-700 text-slate-100 mt-2 mb-0.5">'+m[1]+'</div>'); continue; }
      if(m=ln.match(/^\s*[-•*]\s+(.*)$/)){ if(!inList){out.push('<ul class="list-disc ml-4 space-y-0.5">');inList=true;} out.push('<li>'+m[1]+'</li>'); continue; }
      if(inList){out.push('</ul>');inList=false;}
      if(ln.trim()==='') out.push('<div class="h-1.5"></div>'); else out.push('<div>'+ln+'</div>');
    }
    if(inList)out.push('</ul>');
    return out.join('');
  },
  initials(name){if(!name)return '?';const p=name.trim().split(/\s+/);return ((p[0]||'')[0]||'')+((p[1]||'')[0]||'')||'?';},
  avatarColor(name){ // deterministic HSL from the name (like Zoom/Asana tiles)
    let h=0; for(const c of (name||'?'))h=(h*31+c.charCodeAt(0))%360; return 'hsl('+h+',55%,45%)';},
  showAvatar(i){ // show the avatar+name only when the speaker changes (chat-style grouping)
    if(i===0)return true; return (this.mLines[i].speaker||'')!==(this.mLines[i-1].speaker||'');},
  async loadCalStatus(){
    const c=await this.api('/api/calendar/status'); this.cal=c||{};
    if(c && c.mtime){
      const mins=Math.round((Date.now()/1000 - c.mtime)/60);
      this.calAgo = mins<1?'just now':(mins<60?mins+'m ago':(Math.round(mins/60)+'h ago'));
    } else this.calAgo='';
  },
  async loadMeetings(){const d=await this.api('/api/meetings');this.meetings=d.meetings||[];this.loadCalStatus();
    // if a capture is live, auto-attach to it
    if(this.state.meeting && this.state.meeting_active){this.curMeeting=this.state.meeting_active;}
    this.icons();},
  async toggleMeeting(){
    if(this.state.meeting){ this.toast('Stopping & summarizing…'); await this.api('/api/meeting/stop',{}); }
    else { const d=await this.api('/api/meeting/start',{source:'auto'}); this.curMeeting=d.meeting_id; this.toast('Meeting capture started'); }
    setTimeout(()=>{this.refresh();this.loadMeetings();},800);
  },
  async pollMeetingLive(){
    if(this.tab!=='meeting'||!this.state.meeting)return;
    const d=await this.api('/api/meeting/live');
    this.mLines=d.lines||[]; this.mNotes=d.notes||''; this.mActions=d.action_items||[]; this.mDecisions=d.decisions||[];
    this.mStartedAt=d.started_at||0;
    if(d.meeting_id)this.curMeeting=d.meeting_id;
    this.$nextTick(()=>{const e=document.getElementById('mtgTranscript');if(e)e.scrollTop=e.scrollHeight;});
  },
  async renameMeeting(m){
    const t=prompt('Rename meeting:', m.title); if(t===null)return;
    const title=t.trim(); if(!title)return;
    await this.api('/api/meeting/rename',{meeting_id:m.id,title}); this.loadMeetings();
  },
  async deleteMeeting(m){
    if(!confirm('Delete "'+m.title+'"? This removes its transcript permanently.'))return;
    await this.api('/api/meeting/delete',{meeting_id:m.id}); this.loadMeetings();
  },
  async openMeeting(id){
    this.curMeeting=id;
    const d=await this.api('/api/meeting?id='+id);
    this.mStartedAt=(d.meeting&&d.meeting.started_at)||0;
    this.mLines=(d.segments||[]).map(s=>({speaker:s.speaker,text:s.text,t:s.t_start_ms,partial:false}));
    this.mNotes=d.notes||''; this.mActions=d.action_items||[]; this.mDecisions=d.decisions||[];
    this.mChat=(d.chat||[]).map(c=>({role:c.role,content:c.content}));
    if(d.summary_md && !this.mNotes)this.mNotes=d.summary_md;
    this.icons();
  },
  async askMeeting(){
    const q=this.mQuestion.trim(); if(!q||!this.curMeeting)return;
    this.mChat.push({role:'user',content:q}); this.mQuestion=''; this.mChatBusy=true;
    this.$nextTick(()=>{const e=document.getElementById('mtgChat');if(e)e.scrollTop=e.scrollHeight;});
    const d=await this.api('/api/meeting/chat',{meeting_id:this.curMeeting,question:q});
    this.mChat.push({role:'assistant',content:d.answer||'(no answer)'}); this.mChatBusy=false;
    this.$nextTick(()=>{const e=document.getElementById('mtgChat');if(e)e.scrollTop=e.scrollHeight;});
  },
  async loadDisplays(){
    const d=await this.api('/api/displays'); this.displays=d.displays||[];
    // if the saved monitor index no longer exists (unplugged/rearranged), fall back
    // to the first available one so the dropdown never shows an invalid selection.
    if(this.displays.length && !this.displays.some(x=>x.index===this.state.display)){
      this.state.display=this.displays[0].index;
      this.setCfg({display:this.state.display});
    }},
  revealTick(){
    // newest NON-empty line, capped to ~14 words; hold last text on empties so the
    // caption doesn't blink/disappear between segments
    const lines=this.transcript||[];
    let t='';
    for(let i=lines.length-1;i>=0;i--){ if((lines[i].text||'').trim()){t=lines[i].text;break;} }
    if(!t){ if(this.subLive)return; }   // keep showing last if nothing new
    const w=t.split(' ');
    if(w.length>14) t='… '+w.slice(-14).join(' ');
    this.subLive=t;
  },
  async loadLevel(){const d=await this.api('/api/leveltest');this.profile=d.profile||{};
    const a=(this.profile.history||[]).filter(h=>h.type==='auto-adjust').slice(-1)[0];
    this.lastAdjust=a?('↻ auto-adjusted '+a.from+'→'+a.to):'';},
  async toggle(n){await this.api('/api/toggle',{name:n});this.refresh()},
  async autodetect(){this.toast('Detecting subtitle region…');await this.api('/api/autodetect',{display:this.state.display});this.refresh();this.toast('Region updated')},
  async pickDisplay(){
    // switching monitors: save the choice, then auto-redetect the subtitle region on
    // the new screen so capture follows you (covers the same-resolution case too).
    await this.setCfg({display:this.state.display});
    this.toast('Monitor changed — detecting subtitle region…');
    await this.api('/api/autodetect',{display:this.state.display});
    this.toast('Region updated for this monitor');
  },
  async setCfg(o){await this.api('/api/config',o);this.refresh()},
  playAudio(w){new Audio('/api/audio?word='+encodeURIComponent(w)).play()},
  async fav(t){const on=!t.favorite;await this.api('/api/favorite',{word:t.word,value:on});t.favorite=on;
    this.toast(on?'Saved to dictionary':'Removed');this.loadFavs();this.icons()},
  async mark(t,known){await this.api('/api/mark',{term:this.termKey(t),known});
    this.toast(known?'Marked known ✓':'Marked new ✗');this.feed=this.feed.filter(x=>x.word!==t.word);this.loadLevel();this.icons()},
  async openTest(){const d=await this.api('/api/leveltest');const items=[];
    d.bank.bands.forEach(b=>b.items.forEach(w=>items.push(w)));d.bank.idiom_check.forEach(w=>items.push(w));
    this.testItems=items;this.testMiss=[];this.testOpen=true},
  toggleChip(it){this.testMiss.includes(it)?this.testMiss=this.testMiss.filter(x=>x!==it):this.testMiss.push(it)},
  async submitTest(){const r=await this.api('/api/leveltest',{unknown:this.testMiss});this.testOpen=false;
    this.loadLevel();this.toast('Level set: '+r.level+' (band '+r.min_band+'+)')},
  init(){
    this.view=localStorage.getItem('sw_view')||'grid';
    this.$watch('view',v=>{localStorage.setItem('sw_view',v);this.icons()});
    this.refresh();this.loadFeed();this.loadFavs();this.loadLevel();this.loadDisplays();this.icons();
    setInterval(()=>this.refresh(),2500);
    setInterval(()=>this.loadDisplays(),5000);   // keep monitor list in sync with the system
    setInterval(()=>{if(this.tab==='live')this.loadFeed()},1000);
    setInterval(()=>{if(this.tab==='sub'&&this.state.listen)this.loadTranscript()},1000);
    setInterval(()=>{if(this.tab==='sub')this.revealTick()},150);
    setInterval(()=>this.pollMeetingLive(),1200);}
}}
</script>
<!-- Alpine loaded LAST (self-hosted) so app() is already defined when it initializes -->
<script src="/static/alpine.min.js"></script>
</body></html>"""


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass  # quiet

    def _send(self, code, body, ctype="application/json"):
        data = body.encode("utf-8") if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _json_body(self):
        length = int(self.headers.get("Content-Length", 0))
        if not length:
            return {}
        try:
            return json.loads(self.rfile.read(length).decode("utf-8"))
        except json.JSONDecodeError:
            return {}

    def do_GET(self):
        try:
            return self._do_GET()
        except BaseException as exc:  # noqa: BLE001 — keep the panel responsive
            import traceback
            if sys.stderr is not None:
                traceback.print_exc()
            return self._send(500, json.dumps({"error": str(exc)}))

    def _do_GET(self):
        path = urlparse(self.path).path
        if path == "/":
            return self._send(200, PAGE, "text/html; charset=utf-8")
        if path.startswith("/static/"):
            # serve self-hosted JS libs (Alpine/Tailwind/Lucide) — no CDN dependency
            name = os.path.basename(path)
            fpath = os.path.join(SRC, "static", name)
            if os.path.isfile(fpath):
                with open(fpath, "rb") as handle:
                    return self._send(200, handle.read(), "application/javascript")
            return self._send(404, json.dumps({"error": "not found"}))
        if path == "/api/state":
            cfg = config.load_config()
            statuses = {name: _alive(name) for name in
                        ("watch", "overlay", "listen", "subtitle", "vocab", "meeting")}
            local_only = config.effective_local_only()
            codex_ready = config.codex_available()
            return self._send(200, json.dumps({
                **statuses, "meeting_active": _active_meeting_id(),
                "display_mode": cfg["display_mode"], "rarity_threshold": cfg["rarity_threshold"],
                "display": cfg.get("display"), "stats": store.stats(),
                "overlay_tint": cfg.get("overlay_tint", 0.45),
                "smart_level": cfg.get("smart_level", "advanced"),
                "audio_lang": cfg.get("audio_lang", "ja"),
                "local_only": local_only,
                "codex_available": codex_ready,
            }))
        if path == "/api/terms":
            terms = store.all_terms(order="last_seen DESC")[:60]
            return self._send(200, json.dumps({"terms": terms}))
        if path == "/api/audio":
            from urllib.parse import unquote
            word = unquote(parse_qs(urlparse(self.path).query).get("word", [""])[0])
            term = next((t for t in store.all_terms() if t["word"] == word), None)
            audio_path = term.get("audio_path") if term else None
            if not audio_path or not os.path.exists(audio_path):
                return self._send(404, json.dumps({"error": "no audio"}))
            with open(audio_path, "rb") as handle:
                data = handle.read()
            ctype = "audio/aiff" if audio_path.endswith(".aiff") else "audio/wav"
            return self._send(200, data, ctype)
        if path == "/api/leveltest":
            import profile
            return self._send(200, json.dumps({
                "bank": profile.load_bank(), "profile": profile.load_profile()}))
        if path == "/api/favorites":
            return self._send(200, json.dumps({"favorites": store.favorites()}))
        if path == "/api/transcript":
            tpath = os.path.join(config.DB_DIR, "live_transcript.json")
            try:
                with open(tpath, encoding="utf-8") as handle:
                    return self._send(200, handle.read())
            except OSError:
                return self._send(200, json.dumps({"lines": []}))
        if path == "/api/displays":
            return self._send(200, json.dumps({"displays": _list_displays()}))
        # ── Meeting Mode ──
        if path == "/api/meetings":
            import meeting_store
            meeting_store.init_db()
            return self._send(200, json.dumps({"meetings": meeting_store.list_meetings()}))
        if path == "/api/calendar/status":
            # report when the calendar cache was last synced + a couple upcoming events
            import meeting_calendar
            cache_path = os.path.join(config.DB_DIR, "calendar_cache.json")
            info = {"synced_at": None, "event_count": 0, "upcoming": []}
            try:
                with open(cache_path, encoding="utf-8") as fh:
                    data = json.load(fh)
                info["synced_at"] = data.get("fetched_at")
                info["mtime"] = os.path.getmtime(cache_path)
                evs = data.get("events", [])
                info["event_count"] = len(evs)
                import time as _t
                now = _t.time()
                up = []
                for e in evs:
                    st = meeting_calendar._parse_iso(e.get("start"))
                    if st and st > now:
                        up.append({"subject": e.get("subject"), "start": e.get("start")})
                up.sort(key=lambda x: x["start"])
                info["upcoming"] = up[:3]
            except (OSError, json.JSONDecodeError):
                pass
            return self._send(200, json.dumps(info))
        if path == "/api/meeting":
            import meeting_store
            mid = _int_arg(self.path, "id")
            if not mid:
                return self._send(400, json.dumps({"error": "id required"}))
            m = meeting_store.get_meeting(mid)
            if not m:
                return self._send(404, json.dumps({"error": "no such meeting"}))
            segs = meeting_store.all_segments(mid)
            notes = meeting_store.latest_notes(mid)
            return self._send(200, json.dumps({
                "meeting": m,
                "segments": segs,
                "notes": (notes["bullets"] if notes else ""),
                "action_items": (json.loads(notes["action_items"]) if notes and notes["action_items"] else []),
                "decisions": (json.loads(notes["decisions"]) if notes and notes["decisions"] else []),
                "summary_json": (json.loads(m["summary_json"]) if m.get("summary_json") else None),
                "summary_md": m.get("summary"),
                "chat": meeting_store.chat_history(mid),
            }))
        if path == "/api/meeting/live":
            tpath = os.path.join(config.DB_DIR, "meeting_live.json")
            try:
                with open(tpath, encoding="utf-8") as handle:
                    return self._send(200, handle.read())
            except OSError:
                return self._send(200, json.dumps({"lines": [], "notes": ""}))
        if path == "/api/meeting/export":
            import meeting_store
            mid = _int_arg(self.path, "id")
            m = meeting_store.get_meeting(mid) if mid else None
            if not m:
                return self._send(404, json.dumps({"error": "no such meeting"}))
            md = _export_meeting_md(mid, m, meeting_store)
            fname = f"meeting_{mid}.md"
            self.send_response(200)
            self.send_header("Content-Type", "text/markdown; charset=utf-8")
            self.send_header("Content-Disposition", f'attachment; filename="{fname}"')
            data = md.encode("utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return
        return self._send(404, json.dumps({"error": "not found"}))

    def do_POST(self):
        try:
            return self._do_POST()
        except BaseException as exc:  # noqa: BLE001 — surface action failures to the UI
            import traceback
            if sys.stderr is not None:
                traceback.print_exc()
            return self._send(500, json.dumps({"error": str(exc)}))

    def _do_POST(self):
        path = urlparse(self.path).path
        body = self._json_body()
        if path == "/api/config":
            cfg = config.load_config()
            for key in ("display_mode", "rarity_threshold", "display", "overlay_tint",
                        "smart_level", "capture_breadth", "audio_lang", "local_only"):
                if key in body:
                    cfg[key] = body[key]
            config.save_config(cfg)
            return self._send(200, json.dumps({"ok": True}))
        if path == "/api/toggle":
            name = body.get("name")
            if name == "watch":
                _stop("watch") if _alive("watch") else _start("watch", "watch.py")
            elif name == "overlay":
                _stop("overlay") if _alive("overlay") else _start("overlay", "overlay.py")
            elif name == "listen":
                # Audio recognition stays local in every mode; Codex handles only the
                # optional transcript cleanup and language-learning judgments.
                script = "audio_listen.py"
                _stop("listen") if _alive("listen") else _start("listen", script)
            elif name == "subtitle":
                _stop("subtitle") if _alive("subtitle") else _start("subtitle", "subtitle_overlay.py")
            elif name == "vocab":
                script = "vocab_overlay_win.py" if os.name == "nt" else "vocab_overlay.py"
                _stop("vocab") if _alive("vocab") else _start("vocab", script)
            return self._send(200, json.dumps({"ok": True}))
        if path == "/api/autodetect":
            display = body.get("display")
            args = [PY, os.path.join(SRC, "autodetect.py")]
            if display:
                args.append(str(display))
            subprocess.run(args, cwd=SRC, capture_output=True)
            return self._send(200, json.dumps({"ok": True}))
        if path == "/api/leveltest":
            import profile, datetime
            unknown = body.get("unknown", [])
            result = profile.score_test(unknown, datetime.datetime.now().isoformat())
            return self._send(200, json.dumps(result))
        if path == "/api/mark":
            import profile, datetime
            profile.mark(body.get("term", ""), bool(body.get("known")),
                         datetime.datetime.now().isoformat())
            return self._send(200, json.dumps({"ok": True}))
        if path == "/api/favorite":
            store.set_favorite(body.get("word", ""), bool(body.get("value")))
            return self._send(200, json.dumps({"ok": True}))
        # ── Meeting Mode ──
        if path == "/api/meeting/start":
            if _alive("meeting"):
                return self._send(200, json.dumps({"ok": True, "already": True,
                                                   "meeting_id": _active_meeting_id()}))
            import meeting_store
            meeting_store.init_db()
            # close out any meetings orphaned by a previous crash/kill (driver isn't
            # running, so they can't still be live) before starting a fresh one.
            for stale in meeting_store.list_meetings():
                if stale["status"] == "live":
                    meeting_store.end_meeting(stale["id"])
            source = body.get("source", "auto")
            title = (body.get("title") or "").strip() or None
            mid = meeting_store.create_meeting(title=title, source=source)
            args = ["--meeting-id", str(mid), "--source", source]
            _start_with_args("meeting", "meeting.py", args, detached=True)
            return self._send(200, json.dumps({"ok": True, "meeting_id": mid}))
        if path == "/api/meeting/stop":
            # SIGTERM the driver; it flips status=ended and generates the summary on exit.
            _stop("meeting")
            return self._send(200, json.dumps({"ok": True}))
        if path == "/api/meeting/chat":
            import meeting_store, meeting_rag
            mid = body.get("meeting_id") or _active_meeting_id()
            question = (body.get("question") or "").strip()
            if not mid or not question:
                return self._send(400, json.dumps({"error": "meeting_id and question required"}))
            meeting_store.add_chat(mid, "user", question)
            answer, cited = meeting_rag.answer_question(mid, question, stream=False)
            meeting_store.add_chat(mid, "assistant", answer, cited_seqs=cited)
            return self._send(200, json.dumps({"answer": answer, "cited_seqs": cited}))
        if path == "/api/meeting/summarize":
            # (re)generate the summary on demand — e.g. for a meeting orphaned by a crash.
            import meeting_store, meeting_rag
            mid = body.get("meeting_id")
            if not mid:
                return self._send(400, json.dumps({"error": "meeting_id required"}))
            if meeting_store.get_meeting(mid) and meeting_store.get_meeting(mid)["status"] == "live":
                meeting_store.end_meeting(mid)
            summary = meeting_rag.generate_summary(mid)
            return self._send(200, json.dumps({"ok": True, "summary": summary}))
        if path == "/api/meeting/delete":
            import meeting_store
            mid = body.get("meeting_id")
            if mid:
                meeting_store.delete_meeting(mid)
            return self._send(200, json.dumps({"ok": True}))
        if path == "/api/meeting/rename":
            import meeting_store
            mid = body.get("meeting_id")
            title = (body.get("title") or "").strip()
            if mid and title:
                meeting_store.set_title(mid, title)
            return self._send(200, json.dumps({"ok": True}))
        return self._send(404, json.dumps({"error": "not found"}))


def _bind_free_server():
    """Bind the server to the first free port at/above PORT."""
    last_error = None
    for candidate in range(PORT, PORT + 25):
        try:
            return ThreadingHTTPServer(("127.0.0.1", candidate), Handler), candidate
        except OSError as exc:
            last_error = exc
    raise last_error


def main():
    store.init_db()
    server, port = _bind_free_server()
    url = f"http://127.0.0.1:{port}"
    if sys.stdout is not None:
        print(f"SubWatch control panel -> {url}  (Ctrl-C to stop)")
    try:
        subprocess.Popen(["open", url])
    except Exception:
        pass

    def _shutdown(*_):
        _stop("watch")
        _stop("overlay")
        server.shutdown()
    signal.signal(signal.SIGINT, _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)
    server.serve_forever()


if __name__ == "__main__":
    main()
