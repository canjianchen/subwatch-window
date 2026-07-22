@echo off
REM Resolve paths relative to this script so it works on any machine / install path.
set "ROOT=%~dp0"
set "LOG=%ROOT%logs\codex-device-login.log"
set "CODEX_CA_CERTIFICATE=%ROOT%.venv\Lib\site-packages\certifi\cacert.pem"
if not exist "%ROOT%logs" mkdir "%ROOT%logs"
call "%APPDATA%\npm\codex.cmd" login --device-auth > "%LOG%" 2>&1
call "%APPDATA%\npm\codex.cmd" login status >> "%LOG%" 2>&1
