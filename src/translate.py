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
import threading
import time
import urllib.parse
import urllib.request

import config

# Keep the per-request timeout SHORT: subtitles scroll fast, and a blocked/slow endpoint
# must not make a word sit on "翻译中…". If the network is bad we want to fail over to the
# LLM's own Chinese quickly, not wait 8s per word.
_TIMEOUT = 3.5
_HEADERS = {"User-Agent": "Mozilla/5.0 (SubWatch translation)"}
_CACHE_PATH = os.path.join(config.DB_DIR, "translation_cache.db")

# in-process memo on top of the SQLite cache, to avoid a disk hit for repeats within a run
_MEMO: dict[str, str] = {}

# ── circuit breaker ─────────────────────────────────────────────────────────
# On a network where the free endpoints are blocked (e.g. a corporate proxy / TLS
# interception), retrying every word wastes seconds each and freezes the overlay on
# "翻译中…". After a run of consecutive failures we OPEN the breaker: translate_to_zh()
# returns None immediately (no network) for a cool-off period, so callers fall back to
# the LLM's Chinese instantly. A single success closes it again.
_BREAKER_LOCK = threading.Lock()
_FAILS = 0
_FAIL_THRESHOLD = 3
_OPEN_UNTIL = 0.0
_COOLOFF_SECONDS = 60.0
# Cached result of the one-time reachability probe used by reachable(): (checked_at, ok).
_PROBE = {"at": 0.0, "ok": None}
_PROBE_TTL = 120.0


def _breaker_open():
    with _BREAKER_LOCK:
        return time.time() < _OPEN_UNTIL


def _record_success():
    global _FAILS, _OPEN_UNTIL
    with _BREAKER_LOCK:
        _FAILS = 0
        _OPEN_UNTIL = 0.0


def _record_failure():
    global _FAILS, _OPEN_UNTIL
    with _BREAKER_LOCK:
        _FAILS += 1
        if _FAILS >= _FAIL_THRESHOLD:
            _OPEN_UNTIL = time.time() + _COOLOFF_SECONDS


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
    nothing after the first lookup. When the network endpoints keep failing (blocked
    proxy, offline), a circuit breaker makes this return None instantly so the caller
    falls back to the LLM's own Chinese rather than waiting on every word."""
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

    # breaker open → the network is known-bad right now; don't stall, let the caller
    # use the LLM Chinese. (Codex-only provider is exempt: it's local, not network.)
    provider = config.load_config().get("translate_provider", "auto")
    if provider != "codex" and _breaker_open():
        return None

    for backend in _backends():
        try:
            result = backend(text)
        except Exception:  # noqa: BLE001 — try the next backend on any failure
            continue
        if result:
            _cache_put(text, result)
            if backend is not _codex:
                _record_success()
            return result
    # every network backend failed (codex fallback also empty) → count toward the breaker
    if provider != "codex":
        _record_failure()
    return None


def reachable():
    """Best-effort: is a free network translation endpoint actually usable right now?

    Result is cached for a couple of minutes. Used by callers to decide the split — if the
    API can't be reached (blocked corporate proxy, offline), they keep asking the LLM for
    Chinese inline instead of leaving cards untranslated. Codex-only provider and
    local_only are treated as 'reachable' since they don't need the network."""
    cfg = config.load_config()
    if cfg.get("local_only") or cfg.get("translate_provider") == "codex":
        return True
    now = time.time()
    if _PROBE["ok"] is not None and now - _PROBE["at"] < _PROBE_TTL:
        return _PROBE["ok"]
    if _breaker_open():
        _PROBE.update(at=now, ok=False)
        return False
    # a tiny real translation is the most honest probe (and warms the cache).
    ok = bool(translate_to_zh("hello"))
    _PROBE.update(at=now, ok=ok)
    return ok


if __name__ == "__main__":
    import sys
    query = " ".join(sys.argv[1:]) or "flabbergasted"
    print(f"{query!r} -> {translate_to_zh(query)!r}")
