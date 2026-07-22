@echo off
REM SubWatch control script for Windows. Usage: subwatch <command> [args]
setlocal
set "ROOT=%~dp0"
set "PYTHONUTF8=1"
set "CODEX_CA_CERTIFICATE=%ROOT%.venv\Lib\site-packages\certifi\cacert.pem"
pushd "%ROOT%src"
"%ROOT%.venv\Scripts\python.exe" cli.py %*
popd
endlocal
