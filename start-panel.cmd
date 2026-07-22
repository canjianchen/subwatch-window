@echo off
REM Resolve paths relative to this script so it works on any machine / install path.
set "ROOT=%~dp0"
set "PYTHONUTF8=1"
set "CODEX_CA_CERTIFICATE=%ROOT%.venv\Lib\site-packages\certifi\cacert.pem"
if not exist "%ROOT%logs" mkdir "%ROOT%logs"
cd /d "%ROOT%src"
"%ROOT%.venv\Scripts\python.exe" -u "%ROOT%src\server.py" >> "%ROOT%logs\panel.log" 2>&1
