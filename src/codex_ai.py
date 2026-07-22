"""Small, read-only Codex CLI adapter used by every SubWatch LLM feature.

The adapter reuses the user's saved Codex CLI login. Prompts are passed on stdin,
sessions are ephemeral, and the agent gets a read-only sandbox because SubWatch only
needs text generation here -- never repository edits or shell commands.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import time

import config

_AVAILABLE = None
_AVAILABLE_AT = 0.0


def _codex_env():
    """Use certifi on Windows when the system TLS certificate store is unavailable."""
    env = os.environ.copy()
    if not env.get("CODEX_CA_CERTIFICATE"):
        try:
            import certifi
            env["CODEX_CA_CERTIFICATE"] = certifi.where()
        except (ImportError, OSError):
            pass
    return env


def _native_codex():
    explicit = os.environ.get("SUBWATCH_CODEX_CMD")
    if explicit and os.path.isfile(explicit):
        return explicit

    appdata = os.environ.get("APPDATA", "")
    bundled = os.path.join(
        appdata, "npm", "node_modules", "@openai", "codex", "node_modules",
        "@openai", "codex-win32-x64", "vendor", "x86_64-pc-windows-msvc",
        "bin", "codex.exe")
    if os.path.isfile(bundled):
        return bundled

    found = shutil.which("codex.exe") or shutil.which("codex")
    return found


def available(refresh=False):
    """Return whether a runnable, authenticated Codex CLI is available."""
    global _AVAILABLE, _AVAILABLE_AT
    if not refresh and _AVAILABLE is not None and time.time() - _AVAILABLE_AT < 30:
        return _AVAILABLE
    exe = _native_codex()
    if not exe:
        _AVAILABLE = False
    else:
        try:
            proc = subprocess.run(
                [exe, "login", "status"], capture_output=True, text=True,
                encoding="utf-8", errors="replace", timeout=12,
                env=_codex_env(),
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            _AVAILABLE = proc.returncode == 0
        except (OSError, subprocess.SubprocessError):
            _AVAILABLE = False
    _AVAILABLE_AT = time.time()
    return _AVAILABLE


def ask(prompt, *, system=None, model=None, effort="low", timeout=180):
    """Return the final Codex response as plain text.

    Raises RuntimeError when Codex is missing, unauthenticated, or the run fails.
    """
    exe = _native_codex()
    if not exe:
        raise RuntimeError("Codex CLI is not installed")
    cfg = config.load_config()
    model = model or cfg.get("codex_model", "gpt-5.6-terra")
    instruction = (
        "Do not inspect files, call tools, run commands, or modify anything. "
        "Answer the task directly and return only the requested content."
    )
    if system:
        instruction += f"\n\nSYSTEM INSTRUCTIONS:\n{system.strip()}"
    full_prompt = f"{instruction}\n\nTASK:\n{prompt.strip()}"
    cmd = [
        exe, "exec", "--ephemeral", "--sandbox", "read-only",
        "--ignore-rules", "--ignore-user-config", "--model", model,
        "--config", f'model_reasoning_effort="{effort}"', "-",
    ]
    env = _codex_env()
    env["PYTHONUTF8"] = "1"
    try:
        proc = subprocess.run(
            cmd, input=full_prompt, capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=timeout, env=env,
            cwd=config.ROOT,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"Codex timed out after {timeout}s") from exc
    except OSError as exc:
        raise RuntimeError(f"Could not start Codex: {exc}") from exc
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "unknown Codex error").strip()
        raise RuntimeError(detail[-500:])
    text = (proc.stdout or "").strip()
    if not text:
        raise RuntimeError("Codex returned an empty response")
    return text


def clear_availability_cache():
    global _AVAILABLE, _AVAILABLE_AT
    _AVAILABLE = None
    _AVAILABLE_AT = 0.0
