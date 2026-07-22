@echo off
set "PYTHONUTF8=1"
set "CODEX_CA_CERTIFICATE=C:\Users\q2853\Documents\Codex\subwatch\.venv\Lib\site-packages\certifi\cacert.pem"
if not exist "C:\Users\q2853\Documents\Codex\subwatch\logs" mkdir "C:\Users\q2853\Documents\Codex\subwatch\logs"
cd /d "C:\Users\q2853\Documents\Codex\subwatch\src"
"C:\Users\q2853\Documents\Codex\subwatch\.venv\Scripts\python.exe" -u "C:\Users\q2853\Documents\Codex\subwatch\src\server.py" >> "C:\Users\q2853\Documents\Codex\subwatch\logs\panel.log" 2>&1
