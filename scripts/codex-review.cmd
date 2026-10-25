@echo off
rem Read-only code review: Codex when available, else Claude Code, else Antigravity (see trinity review).
rem   codex-review.cmd                 review uncommitted changes
rem   codex-review.cmd --base main     review this branch against main
rem   codex-review.cmd --commit SHA    review one commit
setlocal
set "ARGS=%*"
if "%~1"=="" set "ARGS=--uncommitted"
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0trinity.ps1" review %ARGS% <nul
exit /b %ERRORLEVEL%
