@echo off
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\trinity.ps1" %*
exit /b %ERRORLEVEL%
