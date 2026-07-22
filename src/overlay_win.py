"""On-screen mask overlay for Windows — one opaque bar you drag, resize, zoom, close.

Uses Tkinter (bundled with Python). On Windows a borderless always-on-top Tk
window renders fine in the foreground (the macOS background-process limitation
does not apply), so no extra dependency is needed.

Controls (mirrors the macOS overlay):
  • drag anywhere            -> move
  • drag bottom-right grip ◢ -> resize
  • mouse wheel              -> zoom bigger / smaller
  • + / -                    -> zoom
  • red ⨯ (top-right)        -> close

Position and size persist in config under `overlay_bar` = [x, y, w, h]
(top-left origin, screen pixels — Tk's native coordinate space on Windows).
"""
import tkinter as tk

import config

LAYOUT_KEY = "overlay_bar"
MIN_W, MIN_H = 120, 26
ZOOM_STEP = 0.06


class WinOverlay:
    def __init__(self):
        self.cfg = config.load_config()
        self.root = tk.Tk()
        self.root.overrideredirect(True)          # borderless
        self.root.attributes("-topmost", True)    # always on top
        self.root.configure(bg="black")

        saved = self.cfg.get(LAYOUT_KEY)
        if saved and len(saved) == 4:
            x, y, w, h = [int(v) for v in saved]
        else:
            sw = self.root.winfo_screenwidth()
            sh = self.root.winfo_screenheight()
            w, h = int(sw * 0.5), 56
            x, y = int((sw - w) / 2), int(sh * 0.78)
        self.root.geometry(f"{w}x{h}+{x}+{y}")

        self.hint = tk.Label(
            self.root, bg="black", fg="#555", anchor="w", padx=10,
            font=("Segoe UI", 11),
            text="⠿ drag · wheel/corner to resize · +/− zoom · ⨯ to close",
        )
        self.hint.place(x=0, y=0, relwidth=1, relheight=1)

        # close button (red ⨯) top-right
        self.close = tk.Label(self.root, text="✕", bg="#d94f57", fg="black",
                              font=("Segoe UI", 10, "bold"))
        self.close.place(relx=1.0, x=-20, y=2, width=16, height=16)
        self.close.bind("<Button-1>", lambda e: self._quit())

        # resize grip (bottom-right)
        self.grip = tk.Label(self.root, text="◢", bg="black", fg="#888",
                             font=("Segoe UI", 11))
        self.grip.place(relx=1.0, rely=1.0, x=-16, y=-16, width=16, height=16)

        # drag-to-move on the bar/hint
        for widget in (self.root, self.hint):
            widget.bind("<ButtonPress-1>", self._press)
            widget.bind("<B1-Motion>", self._move)
            widget.bind("<ButtonRelease-1>", lambda e: self._save())

        # resize on the grip
        self.grip.bind("<ButtonPress-1>", self._press)
        self.grip.bind("<B1-Motion>", self._resize)
        self.grip.bind("<ButtonRelease-1>", lambda e: self._save())

        # zoom: wheel + keys
        self.root.bind("<MouseWheel>", self._wheel)        # Windows wheel
        self.root.bind("<plus>", lambda e: self._zoom(1 + ZOOM_STEP))
        self.root.bind("<equal>", lambda e: self._zoom(1 + ZOOM_STEP))
        self.root.bind("<minus>", lambda e: self._zoom(1 - ZOOM_STEP))
        self.root.bind("<Escape>", lambda e: self._quit())

        self._off = (0, 0)

    def _press(self, event):
        self._off = (event.x, event.y)

    def _move(self, event):
        nx = self.root.winfo_x() + event.x - self._off[0]
        ny = self.root.winfo_y() + event.y - self._off[1]
        self.root.geometry(f"+{nx}+{ny}")

    def _resize(self, event):
        nw = max(MIN_W, self.root.winfo_width() + event.x - self._off[0])
        nh = max(MIN_H, self.root.winfo_height() + event.y - self._off[1])
        self._off = (event.x, event.y)
        self.root.geometry(f"{nw}x{nh}")

    def _zoom(self, factor):
        x, y = self.root.winfo_x(), self.root.winfo_y()
        w, h = self.root.winfo_width(), self.root.winfo_height()
        cx, cy = x + w / 2, y + h / 2
        nw, nh = max(MIN_W, int(w * factor)), max(MIN_H, int(h * factor))
        self.root.geometry(f"{nw}x{nh}+{int(cx - nw / 2)}+{int(cy - nh / 2)}")
        self._save()

    def _wheel(self, event):
        self._zoom(1 + ZOOM_STEP if event.delta > 0 else 1 - ZOOM_STEP)

    def _save(self):
        self.cfg[LAYOUT_KEY] = [self.root.winfo_x(), self.root.winfo_y(),
                                self.root.winfo_width(), self.root.winfo_height()]
        config.save_config(self.cfg)

    def _quit(self):
        self._save()
        self.root.destroy()

    def run(self):
        self.root.mainloop()


if __name__ == "__main__":
    WinOverlay().run()
