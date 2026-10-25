#!/bin/sh
# Git Bash entry point (Claude Code's Bash tool is Git Bash): use it there instead of trinity.cmd.
# Windows starts a .cmd through "cmd /c", which drops the first and last quote of the command line when it
# holds more than two. Any quoted argument then splits a repo path with a space, before the script runs.
# This calls PowerShell directly, so arguments with spaces arrive intact.
script=$(cygpath -aw "$(dirname "$0")/scripts/trinity.ps1")
# Git Bash also rewrites every argument that starts with / ("/army ..." became "C:/Program Files/Git/army ...").
# Convert only drive paths (/c/...) here and turn that rewriting off.
for a do
    shift
    case $a in /[A-Za-z]/*) a=$(cygpath -w "$a") ;; esac
    set -- "$@" "$a"
done
MSYS_NO_PATHCONV=1 exec powershell.exe -NoProfile -ExecutionPolicy Bypass -File "$script" "$@"
