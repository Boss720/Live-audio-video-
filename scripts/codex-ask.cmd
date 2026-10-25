@echo off
rem One-shot read-only question: Codex when available, else Claude Code, else Antigravity (see trinity ask).
rem Final answer also saved to .codex-out\codex-last.md
rem   cmd:        scripts\codex-ask.cmd "Why does test X fail? Point to file:line."
rem   Git Bash:   echo "Why does test X fail?" | ./scripts/codex-ask.cmd
rem   any shell:  scripts\codex-ask.cmd < prompt.txt
rem No argument = prompt read from stdin (avoids cmd quote-stripping on paths with spaces).
setlocal
if [%1]==[] (
  powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0trinity.ps1" ask
) else (
  powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0trinity.ps1" ask %* <nul
)
exit /b %ERRORLEVEL%
