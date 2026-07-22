@echo off
title SubWatch - Codex sign in
set "CODEX_CA_CERTIFICATE=C:\Users\q2853\Documents\Codex\subwatch\.venv\Lib\site-packages\certifi\cacert.pem"
echo Signing in to Codex for SubWatch...
echo.
call "%APPDATA%\npm\codex.cmd" login --device-auth
echo.
if errorlevel 1 (
  echo Sign-in failed. Keep this window open and send the error to Codex.
) else (
  echo Sign-in completed. You can close this window and refresh SubWatch.
  call "%APPDATA%\npm\codex.cmd" login status
)
echo.
pause
