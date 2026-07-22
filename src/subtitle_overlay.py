"""On-screen live subtitle overlay (macOS).

Shows the live audio-transcript (from db/live_transcript.json, written by
audio_listen.py) as a caption bar pinned near the bottom of the screen — like a
normal subtitle track, but generated live from the audio. Words appear with a
quick left-to-right reveal for a "live" feel.

Borderless, always-on-top, click-through is NOT enabled (so you can drag it). Run:
  python3 subtitle_overlay.py        (or the panel's "Subtitle on screen" toggle)
"""
import json
import os
import signal

import config
import Cocoa
import objc

TRANSCRIPT_PATH = os.path.join(config.DB_DIR, "live_transcript.json")
LAYOUT_KEY = "subtitle_overlay_rect"


MAX_WORDS = 14   # cap the on-screen caption to ~1-2 lines (show the most recent tail)


def _tail(text):
    """Keep the caption short: when a line runs long, show only the most recent
    ~MAX_WORDS words (a leading … signals there was earlier text)."""
    words = (text or "").split()
    if len(words) <= MAX_WORDS:
        return text or ""
    return "… " + " ".join(words[-MAX_WORDS:])


def _read_latest():
    try:
        with open(TRANSCRIPT_PATH, encoding="utf-8") as handle:
            lines = json.load(handle).get("lines", [])
        return lines[-1] if lines else None
    except (OSError, json.JSONDecodeError):
        return None


class CaptionView(Cocoa.NSView):
    def initWithFrame_(self, frame):
        self = objc.super(CaptionView, self).initWithFrame_(frame)
        if self is None:
            return None
        self._text = ""
        return self

    def isOpaque(self):
        return False

    def mouseDownCanMoveWindow(self):
        return True  # drag the caption anywhere

    def setText_(self, text):
        self._text = text or ""
        self.setNeedsDisplay_(True)

    def drawRect_(self, rect):
        bounds = self.bounds()
        # rounded translucent dark pill behind the text (classic subtitle look)
        Cocoa.NSColor.colorWithWhite_alpha_(0.0, 0.55).set()
        Cocoa.NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(
            bounds, 12, 12).fill()
        if not self._text:
            return
        style = Cocoa.NSMutableParagraphStyle.alloc().init()
        style.setAlignment_(Cocoa.NSTextAlignmentCenter)
        # white text with a dark stroke for readability over any background
        attrs = {
            Cocoa.NSFontAttributeName: Cocoa.NSFont.boldSystemFontOfSize_(28),
            Cocoa.NSForegroundColorAttributeName: Cocoa.NSColor.whiteColor(),
            Cocoa.NSParagraphStyleAttributeName: style,
            Cocoa.NSStrokeColorAttributeName: Cocoa.NSColor.blackColor(),
            Cocoa.NSStrokeWidthAttributeName: -3.0,
        }
        s = Cocoa.NSString.stringWithString_(self._text)
        text_rect = Cocoa.NSInsetRect(bounds, 24, 12)
        s.drawInRect_withAttributes_(text_rect, attrs)


class SubtitleWindow(Cocoa.NSWindow):
    def initWithRect_(self, rect):
        style = Cocoa.NSWindowStyleMaskBorderless
        self = objc.super(SubtitleWindow, self).initWithContentRect_styleMask_backing_defer_(
            rect, style, Cocoa.NSBackingStoreBuffered, False)
        if self is None:
            return None
        self.setTitle_("SubWatchLiveSubtitle")
        self.setLevel_(Cocoa.NSStatusWindowLevel)
        self.setOpaque_(False)
        self.setBackgroundColor_(Cocoa.NSColor.clearColor())
        self.setMovableByWindowBackground_(True)
        self.setHasShadow_(False)
        self.setCollectionBehavior_(
            Cocoa.NSWindowCollectionBehaviorCanJoinAllSpaces
            | Cocoa.NSWindowCollectionBehaviorStationary)
        return self

    def canBecomeKeyWindow(self):
        return True


class SubtitleController:
    def __init__(self):
        self.cfg = config.load_config()
        self.app = Cocoa.NSApplication.sharedApplication()
        self.app.setActivationPolicy_(Cocoa.NSApplicationActivationPolicyAccessory)
        self._shown_text = ""     # last text actually rendered (avoid redundant redraws)
        self._last_ts = 0

    def _default_rect(self):
        f = Cocoa.NSScreen.mainScreen().frame()
        w = f.size.width * 0.7
        h = 56
        x = f.origin.x + (f.size.width - w) / 2
        y = f.origin.y + f.size.height * 0.06   # near the bottom
        return Cocoa.NSMakeRect(x, y, w, h)

    def _tick(self):
        latest = _read_latest()
        if not latest:
            return
        text = (latest.get("text") or "").strip()
        ts = latest.get("t", 0)
        # Hold the last non-empty caption on screen. Ignore empty updates (between
        # segments) and only redraw when the text actually CHANGED — this stops the
        # subtitle blinking/disappearing as partials and finalize events flow through.
        if not text:
            return
        capped = _tail(text)
        if capped != self._shown_text:
            self._shown_text = capped
            self.view.setText_(capped)

    def run(self):
        saved = self.cfg.get(LAYOUT_KEY)
        rect = Cocoa.NSMakeRect(*saved) if (saved and len(saved) == 4) else self._default_rect()
        self.window = SubtitleWindow.alloc().initWithRect_(rect)
        self.view = CaptionView.alloc().initWithFrame_(
            Cocoa.NSMakeRect(0, 0, rect.size.width, rect.size.height))
        self.view.setAutoresizingMask_(Cocoa.NSViewWidthSizable | Cocoa.NSViewHeightSizable)
        self.window.setContentView_(self.view)
        self.window.makeKeyAndOrderFront_(None)
        self.app.activateIgnoringOtherApps_(True)

        # reveal a word every ~120ms; check for new lines on the same timer
        self._timer = Cocoa.NSTimer.scheduledTimerWithTimeInterval_repeats_block_(
            0.12, True, lambda _t: self._tick())

        def _bye(*_):
            f = self.window.frame()
            self.cfg[LAYOUT_KEY] = [f.origin.x, f.origin.y, f.size.width, f.size.height]
            config.save_config(self.cfg)
            self.app.terminate_(None)
        signal.signal(signal.SIGINT, _bye)
        signal.signal(signal.SIGTERM, _bye)
        self.app.run()


if __name__ == "__main__":
    SubtitleController().run()
