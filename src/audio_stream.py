"""Compatibility entry point for the old cloud audio command.

SubWatch's Codex-only configuration keeps speech recognition local with Whisper.
This module intentionally delegates to audio_listen so no AWS model can be selected.
"""
from audio_listen import run


if __name__ == "__main__":
    run()
