"""On-screen mask overlay — a single opaque bar you drag, resize, and zoom yourself.

Built on PyObjC (native NSWindow) because plain Tk borderless windows launched
from a background process don't render on macOS. One generic black bar:
  • drag anywhere on it          → move
  • drag the bottom-right grip ◢  → resize (free width/height)
  • scroll / pinch over it       → zoom bigger / smaller (keeps centre)
  • +  /  -  keys                → zoom in / out
Its position and size are saved to config and restored next launch.

Limitation: macOS native-fullscreen video lives in its own Space — the overlay
can only sit on top in a *maximized window*, not true fullscreen.

Run:  python3 overlay.py   (or toggle from the web panel)
"""
import signal

import config

import Cocoa
import objc

LAYOUT_KEY = "overlay_bar"  # single bar geometry [x, y, w, h] in Cocoa (bottom-left) coords
MIN_W, MIN_H = 120, 26
ZOOM_STEP = 0.06


def _zoom_window(window, factor, on_move):
    """Scale a window about its centre, clamped to a minimum size."""
    frame = window.frame()
    cx = frame.origin.x + frame.size.width / 2
    cy = frame.origin.y + frame.size.height / 2
    new_w = max(MIN_W, frame.size.width * factor)
    new_h = max(MIN_H, frame.size.height * factor)
    window.setFrame_display_(
        Cocoa.NSMakeRect(cx - new_w / 2, cy - new_h / 2, new_w, new_h), True
    )
    if on_move:
        on_move()


class BarContentView(Cocoa.NSView):
    """Transparent overlay layer (over the frosted-glass blur) that shows a faint
    hint and lets you drag the window. No opaque fill — the blur shows through."""

    def initWithFrame_hint_(self, frame, hint):
        self = objc.super(BarContentView, self).initWithFrame_(frame)
        if self is None:
            return None
        self._hint = hint
        return self

    def isOpaque(self):
        return False  # let the frosted blur underneath show through

    def mouseDownCanMoveWindow(self):
        return True  # drag anywhere on the bar to move the window

    def drawRect_(self, rect):
        # Clean glass — no hint text, no fill. Tips live in the control panel now.
        # (The transparent view still captures drag so you can move the bar.)
        pass


class CloseButton(Cocoa.NSView):
    """A small ⨯ in the top-right corner — click to quit the overlay."""

    def initWithFrame_(self, frame):
        self = objc.super(CloseButton, self).initWithFrame_(frame)
        if self is not None:
            self._hover = False
        return self

    def mouseDownCanMoveWindow(self):
        return False

    def updateTrackingAreas(self):
        for area in list(self.trackingAreas()):
            self.removeTrackingArea_(area)
        opts = (Cocoa.NSTrackingMouseEnteredAndExited | Cocoa.NSTrackingActiveAlways
                | Cocoa.NSTrackingInVisibleRect)
        area = Cocoa.NSTrackingArea.alloc().initWithRect_options_owner_userInfo_(
            self.bounds(), opts, self, None)
        self.addTrackingArea_(area)

    def mouseEntered_(self, event):
        self._hover = True
        self.setNeedsDisplay_(True)

    def mouseExited_(self, event):
        self._hover = False
        self.setNeedsDisplay_(True)

    def drawRect_(self, rect):
        bounds = self.bounds()
        # Subtle by default — a faint grey ✕ that blends into the glass and doesn't
        # block the view; it only brightens (and gets a soft circle) on hover.
        if self._hover:
            Cocoa.NSColor.colorWithWhite_alpha_(0.0, 0.35).set()
            Cocoa.NSBezierPath.bezierPathWithOvalInRect_(bounds).fill()
            glyph_alpha = 0.9
        else:
            glyph_alpha = 0.28
        attrs = {
            Cocoa.NSFontAttributeName: Cocoa.NSFont.systemFontOfSize_(12),
            Cocoa.NSForegroundColorAttributeName: Cocoa.NSColor.colorWithWhite_alpha_(1.0, glyph_alpha),
        }
        Cocoa.NSString.stringWithString_("×").drawAtPoint_withAttributes_(
            Cocoa.NSMakePoint(bounds.size.width / 2 - 4, bounds.size.height / 2 - 8), attrs
        )

    def mouseDown_(self, event):
        Cocoa.NSApplication.sharedApplication().terminate_(None)


class ResizeGrip(Cocoa.NSView):
    """A resize handle that drags one or more EDGES of the window, so the bar can be
    freely shaped to any width/height (not aspect-locked). `edges` is a set drawn
    from {'l','r','t','b'} — e.g. {'r','b'} is the bottom-right corner, {'r'} the
    right edge, {'t'} the top edge. All 8 handles together = free-form rectangle."""

    def initWithFrame_window_onChange_edges_(self, frame, window, on_change, edges):
        self = objc.super(ResizeGrip, self).initWithFrame_(frame)
        if self is None:
            return None
        self._window = window
        self._on_change = on_change
        self._edges = set(edges)
        self._start_frame = None
        return self

    def mouseDownCanMoveWindow(self):
        return False  # this view resizes instead of moving

    def drawRect_(self, rect):
        # faint handle hint — only the corner triangle is drawn for the BR corner;
        # edge handles are invisible hit-zones (kept clean per the no-clutter ask).
        if self._edges == {"r", "b"}:
            bounds = self.bounds()
            Cocoa.NSColor.colorWithWhite_alpha_(0.5, 0.6).set()
            path = Cocoa.NSBezierPath.bezierPath()
            path.moveToPoint_(Cocoa.NSMakePoint(bounds.size.width, 0))
            path.lineToPoint_(Cocoa.NSMakePoint(bounds.size.width, bounds.size.height))
            path.lineToPoint_(Cocoa.NSMakePoint(0, 0))
            path.closePath()
            path.fill()

    def mouseDown_(self, event):
        self._start_frame = self._window.frame()

    def mouseDragged_(self, event):
        if self._start_frame is None:
            return
        loc = Cocoa.NSEvent.mouseLocation()  # screen coords, bottom-left origin
        f = self._start_frame
        left, bottom = f.origin.x, f.origin.y
        right, top = f.origin.x + f.size.width, f.origin.y + f.size.height
        if "r" in self._edges:
            right = max(left + MIN_W, loc.x)
        if "l" in self._edges:
            left = min(right - MIN_W, loc.x)
        if "t" in self._edges:
            top = max(bottom + MIN_H, loc.y)
        if "b" in self._edges:
            bottom = min(top - MIN_H, loc.y)
        new_w, new_h = right - left, top - bottom
        self._window.setFrame_display_(
            Cocoa.NSMakeRect(left, bottom, new_w, new_h), True
        )

    def mouseUp_(self, event):
        self._start_frame = None
        if self._on_change:
            self._on_change()


class MaskWindow(Cocoa.NSWindow):
    def initWithRect_onMove_(self, rect, on_move):
        style = Cocoa.NSWindowStyleMaskBorderless
        self = objc.super(MaskWindow, self).initWithContentRect_styleMask_backing_defer_(
            rect, style, Cocoa.NSBackingStoreBuffered, False
        )
        if self is None:
            return None
        self._on_move = on_move
        # initial tint darkness (0=clear .. 0.95=fully hidden), remembered in config
        try:
            self._tint = float(config.load_config().get("overlay_tint", 0.45))
        except Exception:  # noqa: BLE001
            self._tint = 0.45
        # name the window so the capture loop can find & exclude it dynamically
        # (more robust than a saved window-id that goes stale across restarts).
        self.setTitle_("SubWatchMask")
        self.setLevel_(Cocoa.NSStatusWindowLevel)
        # Translucent frosted-glass blur instead of an opaque black bar: the window
        # itself is non-opaque/clear, and an NSVisualEffectView blurs whatever is
        # behind it — the subtitle becomes an unreadable frosted smear while the bar
        # reads as glass, not a black block. (Capture still OCRs the clean text under
        # it via Quartz, so hiding it visually doesn't affect word capture.)
        self.setOpaque_(False)
        self.setBackgroundColor_(Cocoa.NSColor.clearColor())
        self.setMovableByWindowBackground_(True)
        self.setHasShadow_(False)
        self.setCollectionBehavior_(
            Cocoa.NSWindowCollectionBehaviorCanJoinAllSpaces
            | Cocoa.NSWindowCollectionBehaviorStationary
        )

        full = Cocoa.NSMakeRect(0, 0, rect.size.width, rect.size.height)
        # STACK several frosted-blur views to compound the smear so subtitle text is
        # destroyed WITHOUT darkening — the video's color/brightness shows through,
        # only sharp edges (text) are blurred away. More effective than one blur.
        self._blurs = []
        container = Cocoa.NSView.alloc().initWithFrame_(full)
        container.setAutoresizingMask_(Cocoa.NSViewWidthSizable | Cocoa.NSViewHeightSizable)
        container.setWantsLayer_(True)
        if container.layer() is not None:
            container.layer().setCornerRadius_(8.0)
            container.layer().setMasksToBounds_(True)
        for _ in range(3):
            b = Cocoa.NSVisualEffectView.alloc().initWithFrame_(full)
            b.setAutoresizingMask_(Cocoa.NSViewWidthSizable | Cocoa.NSViewHeightSizable)
            b.setBlendingMode_(Cocoa.NSVisualEffectBlendingModeBehindWindow)
            b.setMaterial_(Cocoa.NSVisualEffectMaterialFullScreenUI)
            b.setState_(Cocoa.NSVisualEffectStateActive)
            container.addSubview_(b)
            self._blurs.append(b)
        self.setContentView_(container)
        self._blur = container

        # Light frosted veil (white, not black) — softens/whitens slightly instead of
        # darkening, so color is preserved. Slider/keys scale only a gentle amount.
        tint = Cocoa.NSView.alloc().initWithFrame_(full)
        tint.setAutoresizingMask_(Cocoa.NSViewWidthSizable | Cocoa.NSViewHeightSizable)
        tint.setWantsLayer_(True)
        tint.layer().setCornerRadius_(8.0)
        self._tintview = tint
        container.addSubview_(tint)
        self.applyTint_(self._tint)
        return self

    def applyTint_(self, value):
        # Slider 0..1 drives the bar from clear frosted glass → a SOLID opaque cover, so
        # it can fully HIDE a stubborn subtitle (large/high-contrast text, especially
        # Chinese, used to read straight through the old 35%-cap glass). Low end keeps the
        # color-preserving frosted look; high end becomes an opaque dark block.
        self._tint = max(0.0, min(1.0, value))
        # Number of active blur passes scales with the slider — more passes = stronger
        # text smear (the color-preserving hiding mechanism at the low/mid range).
        passes = 1 + int(round(self._tint * 2))   # 1..3 stacked blurs
        for i, b in enumerate(getattr(self, "_blurs", [])):
            b.setHidden_(i >= passes)
        # The veil now scales all the way to a SOLID opaque cover. It also shifts from a
        # light frosted tint toward dark as it strengthens, so at the top of the slider
        # the subtitle is completely covered, not just softened.
        if getattr(self, "_tintview", None) is not None and self._tintview.layer() is not None:
            # alpha 0 → 1 across the slider (squared so the first half stays gentle glass,
            # the top quarter goes fully opaque); colour fades light→dark as it strengthens.
            alpha = min(1.0, self._tint ** 1.3 * 1.25)
            white = max(0.0, 0.85 - self._tint * 0.78)   # 0.85 (light) → ~0.07 (near-black)
            self._tintview.layer().setBackgroundColor_(
                Cocoa.NSColor.colorWithWhite_alpha_(white, alpha).CGColor())
        # Fade the frosted blur itself: barely-there at the clear end, full by mid-slider.
        if getattr(self, "_blur", None) is not None:
            self._blur.setAlphaValue_(min(1.0, 0.15 + self._tint * 1.4))

    def canBecomeKeyWindow(self):
        return True

    def scrollWheel_(self, event):
        dy = event.deltaY()
        if dy == 0:
            return
        _zoom_window(self, 1.0 + (ZOOM_STEP if dy > 0 else -ZOOM_STEP), self._on_move)

    def keyDown_(self, event):
        ch = event.charactersIgnoringModifiers()
        if ch in ("+", "="):
            _zoom_window(self, 1.0 + ZOOM_STEP, self._on_move)
        elif ch in ("-", "_"):
            _zoom_window(self, 1.0 - ZOOM_STEP, self._on_move)
        elif ch == "]":                       # more opaque (hide more)
            self.applyTint_(self._tint + 0.04)
            if self._on_move:
                self._on_move()
        elif ch == "[":                       # more transparent (see more)
            self.applyTint_(self._tint - 0.04)
            if self._on_move:
                self._on_move()
        else:
            objc.super(MaskWindow, self).keyDown_(event)

    def windowDidMove_(self, _notification):
        if self._on_move:
            self._on_move()


class OverlayController:
    def __init__(self):
        self.cfg = config.load_config()
        self.app = Cocoa.NSApplication.sharedApplication()
        self.app.setActivationPolicy_(Cocoa.NSApplicationActivationPolicyAccessory)
        self.window = None

    def _default_rect(self):
        frame = Cocoa.NSScreen.mainScreen().frame()
        width = frame.size.width * 0.62      # wide enough for a subtitle line
        height = 40                          # short — just the subtitle line
        x = frame.origin.x + (frame.size.width - width) / 2
        y = frame.origin.y + frame.size.height * 0.12
        return Cocoa.NSMakeRect(x, y, width, height)

    def _save(self):
        if not self.window:
            return
        frame = self.window.frame()
        self.cfg[LAYOUT_KEY] = [frame.origin.x, frame.origin.y,
                                frame.size.width, frame.size.height]
        self.cfg["overlay_tint"] = round(getattr(self.window, "_tint", 0.55), 2)
        config.save_config(self.cfg)

    def build(self):
        saved = self.cfg.get(LAYOUT_KEY)
        if saved and len(saved) == 4:
            rect = Cocoa.NSMakeRect(*saved)
        else:
            rect = self._default_rect()

        self.window = MaskWindow.alloc().initWithRect_onMove_(rect, self._save)

        hint = "⠿ drag · resize · +/− zoom · [ ] transparency · ⨯ close"
        content = BarContentView.alloc().initWithFrame_hint_(
            Cocoa.NSMakeRect(0, 0, rect.size.width, rect.size.height), hint
        )
        content.setAutoresizingMask_(Cocoa.NSViewWidthSizable | Cocoa.NSViewHeightSizable)
        # add the controls ON TOP of the frosted-glass blur (the window's content
        # view), rather than replacing it — so the blur stays the background.
        self.window.contentView().addSubview_(content)

        # Free-form resize handles on every edge + corner, so the bar can be dragged
        # to ANY width/height. Each: (edges, x, y, w, h, autoresize-mask). Corners are
        # small squares; edges are thin strips. w/h of the window = rect.size.* here.
        W, H = rect.size.width, rect.size.height
        T = 10  # handle thickness
        MnX = Cocoa.NSViewMinXMargin; MxX = Cocoa.NSViewMaxXMargin
        MnY = Cocoa.NSViewMinYMargin; MxY = Cocoa.NSViewMaxYMargin
        FW = Cocoa.NSViewWidthSizable; FH = Cocoa.NSViewHeightSizable
        handles = [
            # corners (Cocoa origin = bottom-left)
            ({"l", "b"}, 0,     0,     T, T, MxX | MxY),   # bottom-left
            ({"r", "b"}, W - T, 0,     T, T, MnX | MxY),   # bottom-right (drawn triangle)
            ({"l", "t"}, 0,     H - T, T, T, MxX | MnY),   # top-left
            ({"r", "t"}, W - T, H - T, T, T, MnX | MnY),   # top-right
            # edges
            ({"l"}, 0,     T, T, H - 2 * T, MxX | FH),     # left edge
            ({"r"}, W - T, T, T, H - 2 * T, MnX | FH),     # right edge
            ({"b"}, T, 0,     W - 2 * T, T, FW | MxY),     # bottom edge
            ({"t"}, T, H - T, W - 2 * T, T, FW | MnY),     # top edge
        ]
        for edges, hx, hy, hw, hh, mask in handles:
            g = ResizeGrip.alloc().initWithFrame_window_onChange_edges_(
                Cocoa.NSMakeRect(hx, hy, hw, hh), self.window, self._save, edges)
            g.setAutoresizingMask_(mask)
            content.addSubview_(g)

        # close button (⨯) pinned to the top-right corner
        cb = 16
        close = CloseButton.alloc().initWithFrame_(
            Cocoa.NSMakeRect(rect.size.width - cb - 4, rect.size.height - cb - 4, cb, cb)
        )
        close.setAutoresizingMask_(Cocoa.NSViewMinXMargin | Cocoa.NSViewMinYMargin)
        content.addSubview_(close)

        self.window.makeKeyAndOrderFront_(None)
        self.app.activateIgnoringOtherApps_(True)

        # Publish our window number so the capture loop can exclude the bar from
        # screenshots — otherwise it would OCR the black mask instead of the text.
        self.cfg["overlay_window_id"] = int(self.window.windowNumber())
        config.save_config(self.cfg)

        # Poll config so the web panel's transparency slider applies LIVE (no restart).
        self._poll = Cocoa.NSTimer.scheduledTimerWithTimeInterval_repeats_block_(
            0.5, True, lambda _t: self._sync_tint())
        return True

    def _sync_tint(self):
        try:
            want = float(config.load_config().get("overlay_tint", 0.55))
        except Exception:  # noqa: BLE001
            return
        if self.window and abs(getattr(self.window, "_tint", -1) - want) > 0.001:
            self.window.applyTint_(want)

    def _clear_window_id(self):
        cfg = config.load_config()
        if "overlay_window_id" in cfg:
            cfg.pop("overlay_window_id", None)
            config.save_config(cfg)

    def run(self):
        if not self.build():
            return

        def _bye(*_):
            self._clear_window_id()
            self.app.terminate_(None)

        signal.signal(signal.SIGINT, _bye)
        signal.signal(signal.SIGTERM, _bye)
        try:
            self.app.run()
        finally:
            self._clear_window_id()


if __name__ == "__main__":
    OverlayController().run()
