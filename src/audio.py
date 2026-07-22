"""Generate cached pronunciation audio for captured words/phrases.

macOS: uses the built-in `say` command to render an AIFF clip, no install needed.
Windows: uses SAPI via PowerShell to render a WAV (also built-in).

Clips are cached under data/audio/ and the path stored on the term, so the review
app and web panel can offer a play button.

Run:  python3 audio.py        # generate audio for all terms missing it
"""
import hashlib
import os
import subprocess
import sys

import config
import store

AUDIO_DIR = os.path.join(config.DATA, "audio")
IS_MAC = sys.platform == "darwin"
IS_WINDOWS = os.name == "nt"


def _spoken_form(term):
    """The text to pronounce: the idiom for phrases, else the plain word."""
    if term.get("kind") == "phrase":
        return term.get("matched") or term["word"].replace("phrase:", "")
    return term["word"]


def _clip_path(text, ext):
    digest = hashlib.md5(text.lower().encode("utf-8")).hexdigest()[:16]
    return os.path.join(AUDIO_DIR, f"{digest}.{ext}")


def _say_mac(text, path):
    # AIFF is what `say` writes natively; widely playable on macOS.
    result = subprocess.run(["say", "-o", path, text], capture_output=True)
    return result.returncode == 0 and os.path.exists(path)


def _say_windows(text, path):
    safe = text.replace("'", "''")
    script = (
        "Add-Type -AssemblyName System.Speech; "
        "$s = New-Object System.Speech.Synthesis.SpeechSynthesizer; "
        f"$s.SetOutputToWaveFile('{path}'); $s.Speak('{safe}'); $s.Dispose()"
    )
    result = subprocess.run(["powershell", "-NoProfile", "-Command", script],
                            capture_output=True)
    return result.returncode == 0 and os.path.exists(path)


def generate_for(text):
    """Render (if needed) a pronunciation clip for `text`. Returns the path or None."""
    os.makedirs(AUDIO_DIR, exist_ok=True)
    if IS_MAC:
        path = _clip_path(text, "aiff")
        if os.path.exists(path):
            return path
        return path if _say_mac(text, path) else None
    if IS_WINDOWS:
        path = _clip_path(text, "wav")
        if os.path.exists(path):
            return path
        return path if _say_windows(text, path) else None
    return None


def play(path):
    """Play a cached clip (used by the CLI/review app)."""
    if not path or not os.path.exists(path):
        return False
    if IS_MAC:
        subprocess.Popen(["afplay", path])
        return True
    if IS_WINDOWS:
        # default player via PowerShell SoundPlayer (works for WAV)
        script = f"(New-Object Media.SoundPlayer '{path}').PlaySync()"
        subprocess.Popen(["powershell", "-NoProfile", "-Command", script])
        return True
    return False


def generate_all():
    store.init_db()
    pending = store.terms_needing_audio()
    if not pending:
        print("All terms already have audio.")
        return
    done = 0
    for term in pending:
        text = _spoken_form(term)
        path = generate_for(text)
        if path:
            store.set_audio(term["word"], path)
            done += 1
            print(f"  🔊 {text}")
        else:
            print(f"  ✗ {text} (audio generation failed)")
    print(f"\nGenerated audio for {done}/{len(pending)} terms.")


if __name__ == "__main__":
    generate_all()
