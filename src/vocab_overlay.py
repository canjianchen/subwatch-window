"""On-screen vocabulary overlay (macOS).

Shows the hard words SubWatch captured from the subtitle line(s) CURRENTLY on screen —
the word plus its Chinese — as a floating panel. It stays scoped to the current
sentence (plus the one just before it), so it reads like a per-line glossary rather
than a long scrolling history. Because the words persist on the panel, the ~0.4s
grading delay no longer matters: the subtitle line passes, the words stay.

Resizable: drag any edge/corner to resize the box; the text scales with it. Draggable
anywhere; remembers position + size. Reads db/live_vocab.json (written by the watch
loop via vocab_feed.py). Run:
  python3 vocab_overlay.py        (or the panel's "Word overlay" toggle)
"""
import sys

# Keep this historical entry point working on Windows too. Older panel processes
# may still launch vocab_overlay.py until the panel itself is restarted.
if sys.platform == "win32" and __name__ == "__main__":
    from vocab_overlay_win import main as windows_main

    windows_main()
    raise SystemExit

import signal

import config
import vocab_feed
from overlay_mac import ResizeGrip   # reuse the mask overlay's free-resize handles

import Cocoa
import objc

LAYOUT_KEY = "vocab_overlay_rect"
MAX_SENTENCES = 4   # retain several recent subtitle lines
MAX_WORDS = 16      # show a broader rolling set of hard words
MAX_AGE = 180.0     # clear the panel only after a long pause (seconds)
DEFAULT_W = 360.0   # reference width; text scales relative to this as you resize
WINDOW_NAME = "SubWatchVocab"   # named so the OCR capture loop can exclude it


def _scale_for_width(width):
    """Text scale factor for a given panel width — bigger box, bigger words. A plain
    module function (NOT a method) so PyObjC doesn't try to bridge it as a selector."""
    return max(0.6, min(2.6, width / DEFAULT_W))


class VocabView(Cocoa.NSView):
    def initWithFrame_(self, frame):
        self = objc.super(VocabView, self).initWithFrame_(frame)
        if self is None:
            return None
        self._entries = []   # list of dicts: {term, cn, kind}
        return self

    def isOpaque(self):
        return False

    def mouseDownCanMoveWindow(self):
        return True  # drag the panel anywhere (resize grips sit on top and opt out)

    def setEntries_(self, entries):
        self._entries = entries or []
        self.setNeedsDisplay_(True)

    def drawRect_(self, rect):
        bounds = self.bounds()
        # Nearly see-through card so the video + original subtitle stay visible BEHIND the
        # words (the panel no longer blocks the picture). Readability comes from a dark
        # outline + shadow on the text itself, not from an opaque background. The faint
        # backing alpha is configurable via `vocab_overlay_bg` (0 = fully transparent).
        try:
            bg_alpha = float(config.load_config().get("vocab_overlay_bg", 0.22))
        except Exception:  # noqa: BLE001
            bg_alpha = 0.22
        if bg_alpha > 0.001:
            Cocoa.NSColor.colorWithWhite_alpha_(0.0, max(0.0, min(1.0, bg_alpha))).set()
            Cocoa.NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(
                bounds, 14, 14).fill()
        if not self._entries:
            self._draw_hint(bounds)
            return

        s = _scale_for_width(bounds.size.width)
        pad_x, pad_top = 18 * s, 16 * s
        row_h = 42 * s
        # Dark shadow behind glyphs so they read over bright video without a solid card.
        shadow = Cocoa.NSShadow.alloc().init()
        shadow.setShadowColor_(Cocoa.NSColor.colorWithWhite_alpha_(0.0, 0.85))
        shadow.setShadowBlurRadius_(2.5)
        shadow.setShadowOffset_(Cocoa.NSMakeSize(0, -1))
        # Clean white text with a soft shadow (classic subtitle look) — no color tint, no
        # chunky outline. Readable over video without looking gaudy.
        term_attrs = {
            Cocoa.NSFontAttributeName: Cocoa.NSFont.systemFontOfSize_(24 * s),
            Cocoa.NSForegroundColorAttributeName: Cocoa.NSColor.whiteColor(),
            Cocoa.NSShadowAttributeName: shadow,
        }
        cn_attrs = {
            Cocoa.NSFontAttributeName: Cocoa.NSFont.systemFontOfSize_(20 * s),
            Cocoa.NSForegroundColorAttributeName: Cocoa.NSColor.colorWithWhite_alpha_(0.92, 1.0),
            Cocoa.NSShadowAttributeName: shadow,
        }
        # newest at the top, packed downward with a scaled row height
        for i, entry in enumerate(self._entries):
            y = bounds.size.height - pad_top - (i + 1) * row_h
            if y < -row_h * 0.5:
                break  # ran past the bottom of the panel
            term = entry.get("term", "")
            cn = entry.get("cn", "")
            ts = Cocoa.NSString.stringWithString_(term)
            ts.drawAtPoint_withAttributes_(Cocoa.NSMakePoint(pad_x, y + 8 * s), term_attrs)
            if cn:
                tw = ts.sizeWithAttributes_(term_attrs).width
                cs = Cocoa.NSString.stringWithString_("  " + cn)
                cs.drawAtPoint_withAttributes_(
                    Cocoa.NSMakePoint(pad_x + tw, y + 10 * s), cn_attrs)

    def _draw_hint(self, bounds):
        style = Cocoa.NSMutableParagraphStyle.alloc().init()
        style.setAlignment_(Cocoa.NSTextAlignmentCenter)
        attrs = {
            Cocoa.NSFontAttributeName: Cocoa.NSFont.systemFontOfSize_(15),
            Cocoa.NSForegroundColorAttributeName: Cocoa.NSColor.colorWithWhite_alpha_(0.8, 0.5),
            Cocoa.NSParagraphStyleAttributeName: style,
        }
        s = Cocoa.NSString.stringWithString_("hard words appear here as you watch")
        s.drawInRect_withAttributes_(Cocoa.NSInsetRect(bounds, 16, bounds.size.height / 2 - 12), attrs)


class VocabWindow(Cocoa.NSWindow):
    def initWithRect_(self, rect):
        style = Cocoa.NSWindowStyleMaskBorderless
        self = objc.super(VocabWindow, self).initWithContentRect_styleMask_backing_defer_(
            rect, style, Cocoa.NSBackingStoreBuffered, False)
        if self is None:
            return None
        self.setTitle_(WINDOW_NAME)
        self.setLevel_(Cocoa.NSStatusWindowLevel)
        self.setOpaque_(False)
        self.setBackgroundColor_(Cocoa.NSColor.clearColor())
        self.setMovableByWindowBackground_(True)
        self.setHasShadow_(True)
        self.setCollectionBehavior_(
            Cocoa.NSWindowCollectionBehaviorCanJoinAllSpaces
            | Cocoa.NSWindowCollectionBehaviorStationary)
        return self

    def canBecomeKeyWindow(self):
        return True


class VocabController:
    def __init__(self):
        self.cfg = config.load_config()
        self.app = Cocoa.NSApplication.sharedApplication()
        self.app.setActivationPolicy_(Cocoa.NSApplicationActivationPolicyAccessory)
        self._shown_key = None   # signature of last-rendered entries (skip redundant redraws)

    def _default_rect(self):
        f = Cocoa.NSScreen.mainScreen().frame()
        w = DEFAULT_W
        h = 4 * 42 + 32                               # room for ~4 words by default
        x = f.origin.x + f.size.width - w - 40        # top-right corner
        y = f.origin.y + f.size.height - h - 80
        return Cocoa.NSMakeRect(x, y, w, h)

    def _tick(self):
        entries = vocab_feed.recent(max_sentences=MAX_SENTENCES,
                                    max_words=MAX_WORDS, max_age=MAX_AGE)
        key = tuple((e["term"], e.get("cn", "")) for e in entries)
        if key != self._shown_key:
            self._shown_key = key
            self.view.setEntries_(entries)

    def _save(self):
        if not self.window:
            return
        f = self.window.frame()
        self.cfg[LAYOUT_KEY] = [f.origin.x, f.origin.y, f.size.width, f.size.height]
        config.save_config(self.cfg)

    def _add_resize_handles(self, rect):
        """Add 8 free-resize handles (edges + corners) on top of the view, so the panel
        can be dragged to any width/height — same control model as the mask overlay."""
        W, H = rect.size.width, rect.size.height
        T = 12  # handle thickness / hit-zone
        MnX = Cocoa.NSViewMinXMargin; MxX = Cocoa.NSViewMaxXMargin
        MnY = Cocoa.NSViewMinYMargin; MxY = Cocoa.NSViewMaxYMargin
        FW = Cocoa.NSViewWidthSizable; FH = Cocoa.NSViewHeightSizable
        handles = [
            ({"l", "b"}, 0,     0,     T, T, MxX | MxY),   # bottom-left
            ({"r", "b"}, W - T, 0,     T, T, MnX | MxY),   # bottom-right (drawn triangle)
            ({"l", "t"}, 0,     H - T, T, T, MxX | MnY),   # top-left
            ({"r", "t"}, W - T, H - T, T, T, MnX | MnY),   # top-right
            ({"l"}, 0,     T, T, H - 2 * T, MxX | FH),     # left edge
            ({"r"}, W - T, T, T, H - 2 * T, MnX | FH),     # right edge
            ({"b"}, T, 0,     W - 2 * T, T, FW | MxY),     # bottom edge
            ({"t"}, T, H - T, W - 2 * T, T, FW | MnY),     # top edge
        ]
        for edges, hx, hy, hw, hh, mask in handles:
            g = ResizeGrip.alloc().initWithFrame_window_onChange_edges_(
                Cocoa.NSMakeRect(hx, hy, hw, hh), self.window, self._on_resized, edges)
            g.setAutoresizingMask_(mask)
            self.view.addSubview_(g)

    def _on_resized(self):
        self.view.setNeedsDisplay_(True)   # rescale text to the new size
        self._save()

    def run(self):
        saved = self.cfg.get(LAYOUT_KEY)
        rect = Cocoa.NSMakeRect(*saved) if (saved and len(saved) == 4) else self._default_rect()
        self.window = VocabWindow.alloc().initWithRect_(rect)
        self.view = VocabView.alloc().initWithFrame_(
            Cocoa.NSMakeRect(0, 0, rect.size.width, rect.size.height))
        self.view.setAutoresizingMask_(Cocoa.NSViewWidthSizable | Cocoa.NSViewHeightSizable)
        self.window.setContentView_(self.view)
        self._add_resize_handles(rect)
        self.window.makeKeyAndOrderFront_(None)
        self.app.activateIgnoringOtherApps_(True)

        # poll the feed a few times a second; words appear/expire on this timer
        self._timer = Cocoa.NSTimer.scheduledTimerWithTimeInterval_repeats_block_(
            0.3, True, lambda _t: self._tick())

        def _bye(*_):
            self._save()
            self.app.terminate_(None)
        signal.signal(signal.SIGINT, _bye)
        signal.signal(signal.SIGTERM, _bye)
        self.app.run()


if __name__ == "__main__":
    VocabController().run()
