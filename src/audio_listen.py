"""Live audio → English subtitle → hard-word capture.

For videos with NO English subtitle: capture the system audio (via a virtual audio
device like BlackHole), transcribe it with local Whisper, and — when the speech is
not English — use Whisper's built-in `translate` task to render it directly as
English. The resulting English text flows through the SAME hard-word capture path
as the OCR subtitle pipeline (detector + smart LLM + store), so captures, the
dictionary, level calibration, etc. all work identically.

Run:  python3 audio_listen.py            (loops, prints transcript + captures)
      python3 audio_listen.py --device "BlackHole"   (pick input device by name)
      python3 audio_listen.py --list     (list audio input devices)

Requires: sounddevice, numpy, openai-whisper. Audio capture needs an input device
that carries the system/speaker output (BlackHole 2ch, or an Aggregate/Multi-Output
device). Use `--list` to find it.
"""
import argparse
import sys
import time

import numpy as np
import sounddevice as sd

import config
import store
import watch  # reuse classify/dedup/capture logic

SAMPLE_RATE = 16000          # Whisper wants 16 kHz mono
CHUNK_SECONDS = 5.0          # transcribe this much audio per pass
OVERLAP_SECONDS = 1.0        # carry-over so words at the boundary aren't lost
SILENCE_RMS = 0.004          # below this RMS the chunk is treated as silence

import json as _json
import os as _os
TRANSCRIPT_PATH = _os.path.join(config.DB_DIR, "live_transcript.json")
_recent_lines = []


def _publish_transcript(text, lang, replace=False):
    """Publish a line to the rolling live-subtitle feed the web panel polls.
    replace=True updates the last line in place (used when the LLM polish refines
    the raw transcript) instead of appending a new one."""
    import time as _t
    if replace and _recent_lines:
        _recent_lines[-1] = {"text": text, "lang": lang, "t": _t.time()}
    else:
        _recent_lines.append({"text": text, "lang": lang, "t": _t.time()})
    del _recent_lines[:-12]  # keep the last 12 lines
    try:
        _os.makedirs(config.DB_DIR, exist_ok=True)
        tmp = TRANSCRIPT_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as handle:
            _json.dump({"lines": _recent_lines}, handle, ensure_ascii=False)
        _os.replace(tmp, TRANSCRIPT_PATH)
    except OSError:
        pass


def list_devices():
    print(sd.query_devices())


def _pick_device(name_hint):
    """Find an input device whose name contains name_hint (case-insensitive).
    Defaults to BlackHole, then any device with 'aggregate'/'loopback', else the
    system default input."""
    devices = sd.query_devices()
    hints = [name_hint] if name_hint else ["blackhole", "aggregate", "loopback"]
    for hint in hints:
        if not hint:
            continue
        for idx, dev in enumerate(devices):
            if dev["max_input_channels"] > 0 and hint.lower() in dev["name"].lower():
                return idx, dev["name"]
    return None, None


def _load_model(size):
    import whisper
    return whisper.load_model(size)


def _transcribe(model, audio, translate):
    """Whisper on a mono float32 array. translate=True -> any language to English."""
    result = model.transcribe(
        audio,
        task="translate" if translate else "transcribe",
        fp16=False,
        condition_on_previous_text=False,
        no_speech_threshold=0.6,
    )
    return (result.get("text") or "").strip(), result.get("language")


def _polish_with_llm(raw_text, lang):
    """Use Codex to clean up Whisper's rough output into a correct, fluent
    English subtitle (fixes mistranscriptions/awkward phrasing). Best-effort: returns
    the raw text unchanged if Codex is unavailable. This is the LLM quality
    pass on top of local Whisper transcription."""
    if not raw_text:
        return raw_text
    try:
        import json
        import hard_phrases_llm as L
        src = f" (the speaker's language was {lang})" if lang and lang != "en" else ""
        prompt = (
            "You are cleaning up a live speech-to-text transcript of a movie/show for an "
            "English learner. The text is from Whisper and may have mistranscriptions or "
            f"awkward phrasing{src}. Return the corrected, natural ENGLISH version of what "
            "was most likely said — fix obvious errors, keep it faithful, do not add "
            "commentary. If it's already fine, return it unchanged.\n\n"
            f'Transcript: "{raw_text}"\n\n'
            'Reply with ONLY a JSON object: {"text": "the clean English line"}'
        )
        data = L._extract_json_object(L._invoke_raw(prompt, max_tokens=200))
        cleaned = (data.get("text") or "").strip()
        return cleaned or raw_text
    except Exception:  # noqa: BLE001 — best-effort
        return raw_text


def run(device_hint=None, model_size="base", translate=True, once=False, polish=True):
    store.init_db()
    cfg = config.load_config()

    dev_idx, dev_name = _pick_device(device_hint)
    if dev_idx is None:
        print("⚠️  No system-audio input device found.\n"
              "    Install BlackHole (brew install blackhole-2ch) and route the video's\n"
              "    audio through it (a Multi-Output device lets you still hear it), then\n"
              "    run:  subwatch listen --device BlackHole\n"
              "    See available devices with:  subwatch listen --list")
        return

    print(f"Loading Whisper '{model_size}'…", flush=True)
    model = _load_model(model_size)
    print(f"🎧 Listening on: {dev_name} | mode={'translate→EN' if translate else 'transcribe'} | "
          f"Ctrl-C to stop\n", flush=True)

    block = int(CHUNK_SECONDS * SAMPLE_RATE)
    overlap = int(OVERLAP_SECONDS * SAMPLE_RATE)
    last_line = ""
    carry = np.zeros(0, dtype=np.float32)

    try:
        while True:
            # record one chunk (sd.rec is reliable across devices; the InputStream
            # read path captured nothing on some CoreAudio devices).
            rec = sd.rec(block, samplerate=SAMPLE_RATE, channels=1,
                         device=dev_idx, dtype="float32")
            sd.wait()
            mono = rec.reshape(-1)
            chunk = np.concatenate([carry, mono]) if carry.size else mono
            carry = chunk[-overlap:].copy()  # keep tail for next pass

            if True:
                # skip near-silence to save compute and avoid hallucinated text
                if float(np.sqrt(np.mean(chunk ** 2))) < SILENCE_RMS:
                    if once:
                        break
                    continue

                text, lang = _transcribe(model, chunk, translate)
                if not text or not watch._ocr_quality_ok(text):
                    if once:
                        break
                    continue

                # Publish the RAW transcript instantly so the live subtitle is as
                # fast as possible (no waiting on the LLM polish / capture).
                _publish_transcript(text, lang)

                # Codex quality pass cleans Whisper's rough output for
                # the captured words (and refreshes the subtitle with the clean text).
                # Skipped in local_only mode.
                if polish and not config.effective_local_only():
                    polished = _polish_with_llm(text, lang)
                    if polished and polished != text:
                        text = polished
                        _publish_transcript(text, lang, replace=True)

                # route the recognized English through the shared capture pipeline
                last_line, captured = _capture_from_text(cfg, text, last_line, lang)
                tag = f"[{lang}→en]" if (translate and lang and lang != "en") else ""
                print(f"  🎙️ {tag} {text}", flush=True)
                for term, kind in captured:
                    print(f"   ➕ captured: {term}  ({kind})", flush=True)

                if once:
                    break
    except KeyboardInterrupt:
        pass

    s = store.stats()
    print(f"\nStopped. Vocabulary: {s['total']} terms, {s['due']} due.", flush=True)


def _capture_from_text(cfg, english_text, last_line, lang):
    """Mirror watch.process_frame's capture logic for a line of English text from audio.
    Uses the same dedup + smart-LLM/frequency capture so audio captures behave exactly
    like subtitle captures."""
    english_text = watch._clean_line(english_text)
    if not english_text:
        return last_line, []
    if watch._very_similar(english_text, last_line) and len(english_text) <= len(last_line):
        return last_line, []
    if watch._recently_processed(english_text):
        return english_text, []

    captured = []
    # smart LLM path (preferred), falling back exactly like the watch loop
    if cfg.get("smart_capture", True) and not config.effective_local_only() and watch._ocr_quality_ok(english_text):
        import hard_phrases_llm
        result = hard_phrases_llm.extract_hard(english_text, level=cfg.get("smart_level", "advanced"))
        if result.get("sentence_cn") is not None and not result.get("_failed"):
            watch._remember(english_text)
            for item in result.get("items", []):
                term = (item.get("term") or "").strip()
                if not term:
                    continue
                kind = item.get("kind", "word")
                if "proper" in kind.lower() and " " in term and not term.isupper():
                    continue
                if kind == "word" and " " not in term:
                    clean = term.strip(".,!?;:'\"").lower()
                    import detector
                    if not clean.isupper() and not detector.is_real_word(clean):
                        continue
                if kind == "phrase":
                    is_new = store.upsert_phrase(term, english_text, chinese=None)
                    key = f"phrase:{term.lower()}"
                else:
                    is_new = store.upsert_term(term.lower(), context=english_text, rarity_rank=None)
                    key = term.lower()
                if is_new:
                    store.set_enrichment(key, definition=item.get("definition"),
                                         chinese=item.get("chinese"), translation=item.get("translation"))
                    captured.append((term, kind))
            return english_text, captured
        return english_text, []

    # frequency fallback
    watch._remember(english_text)
    import detector
    for word, rank in detector.hard_words(english_text, cfg):
        if store.upsert_term(word, context=english_text, rarity_rank=rank):
            captured.append((word, rank))
    return english_text, captured


def main():
    parser = argparse.ArgumentParser(description="SubWatch live audio → English → capture")
    parser.add_argument("--list", action="store_true", help="list audio input devices")
    parser.add_argument("--device", default=None, help="input device name (e.g. BlackHole)")
    parser.add_argument("--model", default="base", help="whisper model: tiny/base/small/medium (default base; Codex polish cleans it up)")
    parser.add_argument("--accurate", action="store_true", help="use the small model (more accurate, slower on CPU)")
    parser.add_argument("--no-translate", action="store_true", help="transcribe only, don't translate")
    parser.add_argument("--no-polish", action="store_true", help="skip the Codex clean-up pass")
    parser.add_argument("--once", action="store_true", help="one chunk then exit (test)")
    args = parser.parse_args()
    if args.list:
        list_devices()
        return
    run(device_hint=args.device, model_size=("small" if args.accurate else args.model),
        translate=not args.no_translate, once=args.once, polish=not args.no_polish)


if __name__ == "__main__":
    main()
