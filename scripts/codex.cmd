@echo off
rem Run the Codex CLI bundled with the Codex desktop app (not on PATH).
rem Override with set CODEX_EXE=C:\path\to\codex.exe
setlocal
if defined CODEX_EXE goto run
set "BIN=%LOCALAPPDATA%\OpenAI\Codex\bin"
for /f "delims=" %%D in ('dir /b /ad /o-d "%BIN%" 2^>nul') do (
  if not defined CODEX_EXE if exist "%BIN%\%%D\codex.exe" set "CODEX_EXE=%BIN%\%%D\codex.exe"
)
if defined CODEX_EXE goto run
rem .exe only, so this wrapper (codex.cmd) never finds itself and recurses
for /f "delims=" %%P in ('where codex.exe 2^>nul') do if not defined CODEX_EXE set "CODEX_EXE=%%P"
if not defined CODEX_EXE (
  echo codex.exe not found. Install the Codex desktop app or set CODEX_EXE. 1>&2
  exit /b 1
)
:run
pushd "%~dp0.."
"%CODEX_EXE%" %*
set "RC=%ERRORLEVEL%"
popd
exit /b %RC%
