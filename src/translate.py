"""Cheap EN->ZH translation, so we stop spending LLM tokens on plain translation.

SubWatch still uses the LLM to *judge* which words are hard (that needs reasoning),
but the actual English->Simplified-Chinese translation is a mechanical task a free
translation endpoint does well. This module provides one function, translate_to_zh(),
with a graceful fallback chain and a persistent on-disk cache so any given string is
translated over the network at most once.

Fallback order (config `translate_provider="auto"`):
  1. Google's single-call web endpoint  — keyless, free, no account, high ZH quality.
  2. MyMemory public API                 — keyless free tier, different infrastructure.
  3. The local Codex CLI                 — last resort, matches the old behavior.
Any failure falls through to the next backend; if all fail, returns None so callers
degrade gracefully (a card simply keeps whatever translation it already had).

Pure stdlib (urllib + sqlite3) — adds NO new dependency and behaves identically on
macOS and Windows. Honors `local_only`: in that mode only the local Codex CLI is used
(no network), consistent with the rest of the app.
"""
from __future__ import annotations

import json
import os
import sqlite3
import time
import urllib.parse
import urllib.request

import config

_TIMEOUT = 8
_HEADERS = {"User-Agent": "Mozilla/5.0 (SubWatch translation)"}
_CACHE_PATH = os.path.join(config.DB_DIR, "translation_cache.db")

# in-process memo on top of the SQLite cache, to avoid a disk hit for repeats within a run
_MEMO: dict[str, str] = {}


def _http_json(url):
    request = urllib.request.Request(url, headers=_HEADERS)
    with urllib.request.urlopen(request, timeout=_TIMEOUT) as response:
        return json.loads(response.read().decode("utf-8", "replace"))


def _google(text):
    """Unofficial single-call endpoint. Returns the concatenated ZH segments, or None."""
    params = urllib.parse.urlencode(
        {"client": "gtx", "sl": "en", "tl": "zh-CN", "dt": "t", "q": text})
    data = _http_json(f"https://translate.googleapis.com/translate_a/single?{params}")
    # shape: [[["译文", "source", ...], ...], ...]
    segments = data[0] if data and data[0] else []
    out = "".join(seg[0] for seg in segments if seg and seg[0]).strip()
    return out or None


def _mymemory(text):
    """Keyless free-tier API. Returns the translated text, or None."""
    params = urllib.parse.urlencode({"q": text, "langpair": "en|zh-CN"})
    data = _http_json(f"https://api.mymemory.translated.net/get?{params}")
    if data.get("responseStatus") == 200:
        return (data.get("responseData", {}).get("translatedText") or "").strip() or None
    return None


def _codex(text):
    """Last resort — ask the local Codex CLI for just the translation, low effort."""
    try:
        import codex_ai
        prompt = (
            "Translate this English word or phrase to Simplified Chinese (简体中文). "
            "Reply with ONLY the translation — no pinyin, no explanation, no quotes:\n"
            f'"{text}"'
        )
        return (codex_ai.ask(prompt, effort="low", timeout=60) or "").strip() or None
    except Exception:  # noqa: BLE001 — best-effort; never raise from a translation
        return None


def _backends():
    """The ordered list of backends to try, per config."""
    provider = config.load_config().get("translate_provider", "auto")
    named = {"google": _google, "mymemory": _mymemory, "codex": _codex}
    if provider in named:
        return [named[provider]]
    return [_google, _mymemory, _codex]


# ── persistent cache ────────────────────────────────────────────────────────
def _cache_conn():
    os.makedirs(config.DB_DIR, exist_ok=True)
    conn = sqlite3.connect(_CACHE_PATH, timeout=30)
    conn.execute("PRAGMA busy_timeout=30000")
    conn.execute(
        "CREATE TABLE IF NOT EXISTS translations "
        "(source TEXT PRIMARY KEY, target TEXT NOT NULL, created REAL NOT NULL)"
    )
    return conn


def _cache_get(source):
    if source in _MEMO:
        return _MEMO[source]
    try:
        conn = _cache_conn()
        row = conn.execute(
            "SELECT target FROM translations WHERE source = ?", (source,)).fetchone()
        conn.close()
    except sqlite3.DatabaseError:
        return None
    if row and row[0]:
        _MEMO[source] = row[0]
        return row[0]
    return None


def _cache_put(source, target):
    _MEMO[source] = target
    try:
        conn = _cache_conn()
        conn.execute(
            "INSERT OR REPLACE INTO translations (source, target, created) VALUES (?, ?, ?)",
            (source, target, time.time()))
        conn.commit()
        conn.close()
    except sqlite3.DatabaseError:
        pass  # a cache write failure must never break translation


def translate_to_zh(text):
    """Return the Simplified-Chinese translation of `text`, or None if all backends fail.

    Never raises — translation is best-effort so enrichment/capture keep working even
    when offline. Results are cached (per exact source string) so a repeated term costs
    nothing after the first lookup."""
    if not text or not text.strip():
        return None
    text = text.strip()

    cached = _cache_get(text)
    if cached is not None:
        return cached

    # local_only forbids network calls; only the local Codex CLI is allowed.
    if config.load_config().get("local_only"):
        result = _codex(text)
        if result:
            _cache_put(text, result)
        return result

    for backend in _backends():
        try:
            result = backend(text)
        except Exception:  # noqa: BLE001 — try the next backend on any failure
            continue
        if result:
            _cache_put(text, result)
            return result
    return None


if __name__ == "__main__":
    import sys
    query = " ".join(sys.argv[1:]) or "flabbergasted"
    print(f"{query!r} -> {translate_to_zh(query)!r}")
