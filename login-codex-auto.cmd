@echo off
set "LOG=C:\Users\q2853\Documents\Codex\subwatch\logs\codex-device-login-2.log"
set "CODEX_CA_CERTIFICATE=C:\Users\q2853\Documents\Codex\subwatch\.venv\Lib\site-packages\certifi\cacert.pem"
if not exist "C:\Users\q2853\Documents\Codex\subwatch\logs" mkdir "C:\Users\q2853\Documents\Codex\subwatch\logs"
call "%APPDATA%\npm\codex.cmd" login --device-auth > "%LOG%" 2>&1
call "%APPDATA%\npm\codex.cmd" login status >> "%LOG%" 2>&1
