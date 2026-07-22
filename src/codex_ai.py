"""Small, read-only Codex CLI adapter used by every SubWatch LLM feature.

The adapter reuses the user's saved Codex CLI login. Prompts are passed on stdin,
sessions are ephemeral, and the agent gets a read-only sandbox because SubWatch only
needs text generation here -- never repository edits or shell commands.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import threading
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


def ask_lines(prompt, *, system=None, model=None, effort="low", timeout=180):
    """Yield Codex's final response one line at a time, AS the CLI writes each line.

    Same invocation as ask(), but streams stdout line-by-line instead of buffering the
    whole response — so a JSONL consumer can act on the first object in ~3s instead of
    waiting for the full ~6.5s answer. Raises RuntimeError on missing/failed Codex.
    On a non-zero exit, whatever complete lines already streamed are still yielded; the
    error is raised only if nothing was produced (so a caller never silently loses work).
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
        proc = subprocess.Popen(
            cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace",
            env=env, cwd=config.ROOT, bufsize=1,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except OSError as exc:
        raise RuntimeError(f"Could not start Codex: {exc}") from exc

    # A watchdog enforces the timeout since we're not using subprocess.run's timeout.
    timed_out = {"value": False}

    def _kill_on_timeout():
        timed_out["value"] = True
        try:
            proc.kill()
        except OSError:
            pass

    watchdog = threading.Timer(timeout, _kill_on_timeout)
    watchdog.start()

    # Drain stderr on a side thread so a chatty CLI can't fill the pipe buffer and
    # deadlock the stdout reader below.
    stderr_chunks = []

    def _drain_stderr():
        try:
            for line in proc.stderr:
                stderr_chunks.append(line)
        except (OSError, ValueError):
            pass

    stderr_thread = threading.Thread(target=_drain_stderr, daemon=True)
    stderr_thread.start()

    produced = False
    try:
        proc.stdin.write(full_prompt)
        proc.stdin.close()
        for line in proc.stdout:
            produced = True
            yield line.rstrip("\n")
    finally:
        watchdog.cancel()
        try:
            proc.stdout.close()
        except OSError:
            pass
        proc.wait()
        stderr_thread.join(timeout=1)
    if timed_out["value"]:
        raise RuntimeError(f"Codex timed out after {timeout}s")
    if proc.returncode not in (0, None) and not produced:
        detail = "".join(stderr_chunks).strip()
        raise RuntimeError((detail or "Codex exited non-zero")[-500:])


def clear_availability_cache():
    global _AVAILABLE, _AVAILABLE_AT
    _AVAILABLE = None
    _AVAILABLE_AT = 0.0


def _selftest():
    """Diagnostic: `python src/codex_ai.py` — is Codex reachable, and how slow is a call?
    Prints login status, a tiny timed grading call, and any error, so we can tell whether
    the scoring stall is 'Codex slow' vs 'Codex unreachable'."""
    import time as _t
    exe = _native_codex()
    print(f"codex exe: {exe or 'NOT FOUND'}")
    print(f"available (login ok): {available(refresh=True)}")
    start = _t.time()
    try:
        result = ask('Reply only with the JSON {"ok":1}', effort="low", timeout=60)
        print(f"RESULT: {result!r}")
    except Exception as exc:  # noqa: BLE001
        print(f"ERROR: {type(exc).__name__}: {exc}")
    print(f"took {round(_t.time() - start, 1)}s")


if __name__ == "__main__":
    _selftest()
