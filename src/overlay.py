"""On-screen mask overlay — platform dispatcher.

A single opaque bar you drag, resize, zoom, and close to cover subtitle line(s)
on screen. The implementation differs per OS:
  - macOS   -> overlay_mac.py  (native Cocoa NSWindow via PyObjC)
  - Windows -> overlay_win.py  (Tkinter borderless always-on-top window)

Both expose the same controls and persist geometry in config under `overlay_bar`.
"""
import os
import sys


def main():
    if sys.platform == "darwin":
        from overlay_mac import OverlayController
        OverlayController().run()
    elif os.name == "nt":
        from overlay_win import WinOverlay
        WinOverlay().run()
    else:
        raise RuntimeError(f"Overlay is not supported on this platform: {sys.platform}")


if __name__ == "__main__":
    main()
