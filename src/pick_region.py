"""Interactively pick the subtitle capture region and save it to config.

Uses macOS `screencapture -i` which lets you drag a rectangle. We capture to a
throwaway file just to read back the selected region from its dimensions — but
the cleanest path is to ask the user for the bottom-strip via a drag and store
the rectangle. Here we use a transparent Tk overlay to drag-select instead, so
no file is needed and we get exact pixel coordinates.
"""
import tkinter as tk

import config


class RegionPicker:
    def __init__(self):
        self.root = tk.Tk()
        self.root.attributes("-fullscreen", True)
        self.root.attributes("-alpha", 0.3)
        self.root.configure(bg="black")
        self.root.attributes("-topmost", True)

        self.canvas = tk.Canvas(self.root, cursor="cross", bg="black", highlightthickness=0)
        self.canvas.pack(fill="both", expand=True)
        self.canvas.create_text(
            self.root.winfo_screenwidth() // 2, 60,
            text="Drag a box over where subtitles appear (usually the bottom strip).  Esc to cancel.",
            fill="white", font=("Helvetica", 22),
        )

        self.start = None
        self.rect = None
        self.region = None

        self.canvas.bind("<ButtonPress-1>", self.on_press)
        self.canvas.bind("<B1-Motion>", self.on_drag)
        self.canvas.bind("<ButtonRelease-1>", self.on_release)
        self.root.bind("<Escape>", lambda e: self.root.destroy())

    def on_press(self, event):
        self.start = (event.x, event.y)
        self.rect = self.canvas.create_rectangle(event.x, event.y, event.x, event.y,
                                                  outline="#89b4fa", width=3)

    def on_drag(self, event):
        if self.rect:
            self.canvas.coords(self.rect, self.start[0], self.start[1], event.x, event.y)

    def on_release(self, event):
        x1, y1 = self.start
        x2, y2 = event.x, event.y
        x, y = min(x1, x2), min(y1, y2)
        w, h = abs(x2 - x1), abs(y2 - y1)
        if w > 10 and h > 10:
            self.region = [x, y, w, h]
        self.root.destroy()

    def run(self):
        self.root.mainloop()
        return self.region


def main():
    region = RegionPicker().run()
    cfg = config.load_config()
    if region:
        cfg["capture_region"] = region
        config.save_config(cfg)
        print(f"Saved capture region: x={region[0]} y={region[1]} w={region[2]} h={region[3]}")
    else:
        print("No region selected (cancelled). Capture region unchanged.")


if __name__ == "__main__":
    main()
