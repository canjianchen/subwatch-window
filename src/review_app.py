"""SubWatch Review — a flashcard app for the captured vocabulary.

Shows due words (spaced repetition). Front = the word + the sentence you heard it
in (with the word blanked). Reveal shows definition / Chinese line / full sentence.
Rate Again / Hard / Good / Easy to schedule the next review.
"""
import tkinter as tk
from tkinter import font as tkfont

import store


class ReviewApp:
    def __init__(self, root):
        self.root = root
        self.root.title("SubWatch Review")
        self.root.geometry("760x520")
        self.root.configure(bg="#1e1e2e")

        self.queue = store.due_terms(limit=200)
        self.current = None
        self.revealed = False

        self.title_font = tkfont.Font(family="Helvetica", size=34, weight="bold")
        self.body_font = tkfont.Font(family="Helvetica", size=16)
        self.small_font = tkfont.Font(family="Helvetica", size=12)

        self.word_label = tk.Label(root, text="", font=self.title_font,
                                   fg="#cdd6f4", bg="#1e1e2e", wraplength=700)
        self.word_label.pack(pady=(40, 10))

        self.context_label = tk.Label(root, text="", font=self.body_font,
                                      fg="#a6adc8", bg="#1e1e2e", wraplength=700, justify="center")
        self.context_label.pack(pady=10)

        self.answer_label = tk.Label(root, text="", font=self.body_font,
                                     fg="#a6e3a1", bg="#1e1e2e", wraplength=700, justify="center")
        self.answer_label.pack(pady=10)

        self.status_label = tk.Label(root, text="", font=self.small_font,
                                     fg="#6c7086", bg="#1e1e2e")
        self.status_label.pack(side="bottom", pady=10)

        self.button_frame = tk.Frame(root, bg="#1e1e2e")
        self.button_frame.pack(side="bottom", pady=20)

        self.reveal_button = tk.Button(self.button_frame, text="Reveal  (space)",
                                       font=self.body_font, command=self.reveal, width=30)
        self.reveal_button.pack()

        self.rate_buttons = tk.Frame(root, bg="#1e1e2e")
        specs = [("Again (1)", 0, "#f38ba8"), ("Hard (2)", 1, "#fab387"),
                 ("Good (3)", 2, "#a6e3a1"), ("Easy (4)", 3, "#89b4fa")]
        for text, quality, color in specs:
            tk.Button(self.rate_buttons, text=text, font=self.body_font,
                      bg=color, fg="#1e1e2e", width=10,
                      command=lambda q=quality: self.rate(q)).pack(side="left", padx=6)

        root.bind("<space>", lambda e: self.reveal())
        root.bind("1", lambda e: self.rate(0))
        root.bind("2", lambda e: self.rate(1))
        root.bind("3", lambda e: self.rate(2))
        root.bind("4", lambda e: self.rate(3))

        self.next_card()

    def _blank_word(self, context, word):
        if not context:
            return ""
        import re
        return re.sub(rf"\b{re.escape(word)}\b", "______", context, flags=re.IGNORECASE)

    def next_card(self):
        self.revealed = False
        self.answer_label.config(text="")
        self.rate_buttons.pack_forget()
        self.button_frame.pack(side="bottom", pady=20)

        if not self.queue:
            self.current = None
            self.word_label.config(text="🎉 All caught up!")
            self.context_label.config(text="No words are due for review right now.")
            self.reveal_button.config(state="disabled")
            self._update_status()
            return

        self.current = self.queue.pop(0)
        self.word_label.config(text=self.current["word"])
        blanked = self._blank_word(self.current.get("context"), self.current["word"])
        self.context_label.config(text=f"\"{blanked}\"" if blanked else "")
        self.reveal_button.config(state="normal")
        self._update_status()

    def reveal(self):
        if not self.current or self.revealed:
            return
        self.revealed = True
        parts = []
        if self.current.get("phrase"):
            parts.append(f"📌 phrase: {self.current['phrase']}")
        if self.current.get("definition"):
            parts.append(self.current["definition"])
        if self.current.get("chinese"):
            parts.append(f"中文字幕: {self.current['chinese']}")
        if self.current.get("context"):
            parts.append(f"Full line: \"{self.current['context']}\"")
        rank = self.current.get("rarity_rank")
        parts.append("(not in top-10k common words)" if rank is None else f"(frequency rank {rank})")
        self.answer_label.config(text="\n\n".join(parts))
        self.button_frame.pack_forget()
        self.rate_buttons.pack(side="bottom", pady=20)

    def rate(self, quality):
        if not self.current or not self.revealed:
            return
        store.review(self.current["word"], quality)
        self.next_card()

    def _update_status(self):
        s = store.stats()
        remaining = len(self.queue) + (1 if self.current else 0)
        self.status_label.config(
            text=f"{remaining} left this session  ·  {s['total']} total  ·  {s['mastered']} mastered")


def main():
    store.init_db()
    root = tk.Tk()
    ReviewApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
