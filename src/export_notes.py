"""Export captured vocabulary to a Markdown study note and Anki-style CSV."""
import csv
import datetime
import os

import config
import store


def export_markdown():
    terms = store.all_terms(order="times_seen DESC, last_seen DESC")
    os.makedirs(config.NOTES_DIR, exist_ok=True)
    path = os.path.join(config.NOTES_DIR, "vocabulary.md")

    s = store.stats()
    stamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
    lines = [
        "# SubWatch Vocabulary",
        "",
        f"_Updated {stamp} — {s['total']} terms, {s['due']} due, {s['mastered']} mastered._",
        "",
    ]
    # Personal dictionary first — the words the user explicitly starred to keep.
    favs = [t for t in terms if t.get("favorite")]
    if favs:
        lines.append("## ⭐ My Dictionary (saved)\n")
        for term in favs:
            label = term.get("matched") or term["word"].replace("phrase:", "")
            tr = f" — {term['translation']}" if term.get("translation") else ""
            lines.append(f"### {label}{tr}")
            if term.get("definition"):
                lines.append(f"- **Definition:** {term['definition']}")
            if term.get("context"):
                lines.append(f"- **Heard in:** \"{term['context']}\"")
            if term.get("subtitle_cn"):
                lines.append(f"- **中文字幕:** {term['subtitle_cn']}")
            lines.append("")

    words = [t for t in terms if t.get("kind", "word") != "phrase"]
    phrase_terms = [t for t in terms if t.get("kind", "word") == "phrase"]

    if words:
        lines.append("## Words\n")
    for term in words:
        word = term["word"]
        seen = term["times_seen"]
        rank = term["rarity_rank"]
        rarity = "not in top-10k" if rank is None else f"rank {rank}"
        heading = word
        if term.get("phrase"):
            heading = f"{word}  →  *{term['phrase']}*"
        title = heading
        if term.get("translation"):
            title = f"{heading} — {term['translation']}"
        lines.append(f"### {title}  *(seen {seen}×, {rarity})*")
        if term.get("phrase"):
            lines.append(f"- **Phrase:** {term['phrase']}")
        if term.get("translation"):
            lines.append(f"- **翻译:** {term['translation']}")
        if term.get("definition"):
            lines.append(f"- **Definition:** {term['definition']}")
        if term.get("chinese") and term.get("chinese") != term.get("translation"):
            lines.append(f"- **释义:** {term['chinese']}")
        if term.get("context"):
            lines.append(f"- **Heard in:** \"{term['context']}\"")
        if term.get("subtitle_cn"):
            lines.append(f"- **中文字幕:** {term['subtitle_cn']}")
        if term.get("audio_path"):
            lines.append(f"- **🔊 Pronunciation:** `{term['audio_path']}`")
        lines.append(f"- **Status:** {term['status']}")
        lines.append("")

    if phrase_terms:
        lines.append("## Slang & Idioms\n")
    for term in phrase_terms:
        expr = term.get("matched") or term["word"].replace("phrase:", "")
        seen = term["times_seen"]
        title = expr
        if term.get("translation"):
            title = f"{expr} — {term['translation']}"
        lines.append(f"### {title}  *(seen {seen}×)*")
        if term.get("translation"):
            lines.append(f"- **翻译:** {term['translation']}")
        if term.get("definition"):
            lines.append(f"- **Meaning:** {term['definition']}")
        if term.get("context"):
            lines.append(f"- **Whole sentence:** \"{term['context']}\"")
        if term.get("subtitle_cn"):
            lines.append(f"- **中文字幕:** {term['subtitle_cn']}")
        if term.get("audio_path"):
            lines.append(f"- **🔊 Pronunciation:** `{term['audio_path']}`")
        lines.append(f"- **Status:** {term['status']}")
        lines.append("")

    with open(path, "w", encoding="utf-8") as handle:
        handle.write("\n".join(lines))
    return path


def export_anki_csv():
    terms = store.all_terms(order="last_seen DESC")
    os.makedirs(config.NOTES_DIR, exist_ok=True)
    path = os.path.join(config.NOTES_DIR, "anki_import.csv")
    with open(path, "w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["Front", "Back", "Context"])
        for term in terms:
            back = term.get("definition") or term.get("chinese") or ""
            writer.writerow([term["word"], back, term.get("context") or ""])
    return path


if __name__ == "__main__":
    md = export_markdown()
    csv_path = export_anki_csv()
    print(f"Wrote {md}")
    print(f"Wrote {csv_path}")
