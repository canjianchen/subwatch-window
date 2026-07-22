"""SubWatch menu-bar app (macOS).

A lightweight NSStatusItem that lives in the menu bar for quick control without
opening the browser: start/stop watching, listening, the mask & subtitle overlays,
toggle local-only, and open the dashboard. It talks to the running panel server
over HTTP (same /api endpoints), so the panel is the single source of truth.

If the panel server isn't running, the menu items that need it will start it.

Run:  python3 menubar.py   (or `subwatch menubar`)
"""
import json
import os
import signal
import subprocess
import sys
import urllib.request

import config
import Cocoa
import objc

SRC = os.path.dirname(os.path.abspath(__file__))
PY = sys.executable
PANEL_PORTS = list(range(8770, 8795))   # the panel auto-picks the first free one


def _panel_base():
    """Find the running panel's base URL by probing the candidate ports."""
    for port in PANEL_PORTS:
        url = f"http://127.0.0.1:{port}"
        try:
            with urllib.request.urlopen(url + "/api/state", timeout=0.4) as r:
                if r.status == 200:
                    return url
        except Exception:  # noqa: BLE001
            continue
    return None


def _api(base, path, body=None):
    try:
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(base + path, data=data,
                                     headers={"Content-Type": "application/json"},
                                     method="POST" if body is not None else "GET")
        with urllib.request.urlopen(req, timeout=1.5) as r:
            return json.loads(r.read().decode())
    except Exception:  # noqa: BLE001
        return {}


def _do_toggle(app, name):
    """Module-level so PyObjC doesn't try to bridge it as a selector."""
    if not app._ensure_panel():
        return
    _api(app.base, "/api/toggle", {"name": name})
    app._refresh()


class MenuApp(Cocoa.NSObject):
    def init(self):
        self = objc.super(MenuApp, self).init()
        if self is None:
            return None
        self.base = None
        self.state = {}
        bar = Cocoa.NSStatusBar.systemStatusBar()
        self.item = bar.statusItemWithLength_(Cocoa.NSVariableStatusItemLength)
        self.item.button().setTitle_("🎬")
        self.menu = Cocoa.NSMenu.alloc().init()
        self.menu.setAutoenablesItems_(False)
        self.item.setMenu_(self.menu)
        self._build_menu()
        # refresh state every 2s so the checkmarks/labels track reality
        self.timer = Cocoa.NSTimer.scheduledTimerWithTimeInterval_repeats_block_(
            2.0, True, lambda _t: self._refresh())
        self._refresh()
        return self

    # ---- menu construction ----
    def _build_menu(self):
        def add(title, action, key=""):
            it = Cocoa.NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(title, action, key)
            it.setTarget_(self)
            self.menu.addItem_(it)
            return it
        sep = lambda: self.menu.addItem_(Cocoa.NSMenuItem.separatorItem())
        self.mi_panel = add("Open dashboard", "openPanel:")
        sep()
        self.mi_watch = add("Start watching (subtitles)", "toggleWatch:")
        self.mi_listen = add("Start listening (audio)", "toggleListen:")
        self.mi_overlay = add("Show mask overlay", "toggleOverlay:")
        self.mi_subtitle = add("Show live subtitle", "toggleSubtitle:")
        sep()
        self.mi_local = add("Local-only (disable Codex)", "toggleLocal:")
        self.mi_status = add("", "noop:")
        self.mi_status.setEnabled_(False)
        sep()
        add("Quit SubWatch menu", "quit:")

    # ---- state sync ----
    def _refresh(self):
        if not self.base:
            self.base = _panel_base()
        self.state = _api(self.base, "/api/state") if self.base else {}
        s = self.state
        running = bool(s)
        self.mi_watch.setTitle_("◼︎ Stop watching" if s.get("watch") else "▶ Start watching (subtitles)")
        self.mi_listen.setTitle_("◼︎ Stop listening" if s.get("listen") else "🎧 Start listening (audio)")
        self.mi_overlay.setTitle_("Hide mask overlay" if s.get("overlay") else "Show mask overlay")
        self.mi_subtitle.setTitle_("Hide live subtitle" if s.get("subtitle") else "Show live subtitle")
        self.mi_local.setState_(1 if s.get("local_only") else 0)
        # menu-bar glyph reflects activity
        if s.get("listen"):
            self.item.button().setTitle_("🎧")
        elif s.get("watch"):
            self.item.button().setTitle_("🔴")
        else:
            self.item.button().setTitle_("🎬")
        if not running:
            self.mi_status.setTitle_("dashboard not running — click Open")
        else:
            aws = "Codex ✓" if s.get("codex_available") else "local-only (Codex offline)"
            total = (s.get("stats") or {}).get("total", 0)
            self.mi_status.setTitle_(f"{total} terms · {aws}")
        for it in (self.mi_watch, self.mi_listen, self.mi_overlay, self.mi_subtitle, self.mi_local):
            it.setEnabled_(running)

    def _ensure_panel(self):
        if self.base:
            return True
        # start the panel server, then locate it
        subprocess.Popen([PY, os.path.join(SRC, "server.py")], cwd=SRC,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        import time
        for _ in range(20):
            time.sleep(0.4)
            self.base = _panel_base()
            if self.base:
                return True
        return False

    # ---- actions ----
    def openPanel_(self, _s):
        if self._ensure_panel():
            subprocess.Popen(["open", self.base])

    def toggleWatch_(self, _s): _do_toggle(self, "watch")
    def toggleListen_(self, _s): _do_toggle(self, "listen")
    def toggleOverlay_(self, _s): _do_toggle(self, "overlay")
    def toggleSubtitle_(self, _s): _do_toggle(self, "subtitle")

    def toggleLocal_(self, _s):
        if not self._ensure_panel():
            return
        new = not self.state.get("local_only")
        _api(self.base, "/api/config", {"local_only": new})
        # If turning off local-only without a Codex login, explain what is missing.
        if not new and not self.state.get("codex_available"):
            self._alert_no_codex()
        self._refresh()

    def _alert_no_codex(self):
        a = Cocoa.NSAlert.alloc().init()
        a.setMessageText_("Codex CLI is not logged in")
        a.setInformativeText_(
            "Run `codex login`, then toggle Local-only off again.\n\n"
            "SubWatch will stay in local-only mode until Codex is available.")
        a.addButtonWithTitle_("OK")
        a.runModal_()

    def noop_(self, _s): pass

    def quit_(self, _s):
        Cocoa.NSApplication.sharedApplication().terminate_(None)


_KEEP_ALIVE = []   # module-level ref so the menu controller isn't GC'd


def main():
    app = Cocoa.NSApplication.sharedApplication()
    app.setActivationPolicy_(Cocoa.NSApplicationActivationPolicyAccessory)  # menu-bar only, no Dock
    delegate = MenuApp.alloc().init()
    _KEEP_ALIVE.append(delegate)
    signal.signal(signal.SIGINT, lambda *_: app.terminate_(None))
    app.run()


if __name__ == "__main__":
    main()
