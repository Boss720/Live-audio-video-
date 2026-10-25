<#
.SYNOPSIS
  Create an isolated worktree + branch agent/<agent>/<task> and register it on the shared agent board.
  For create-and-start-the-agent in one step, use: trinity dispatch

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File scripts\agent-task.ps1 -Agent codex -Task login-form -Scope "src/auth/*"
  powershell -ExecutionPolicy Bypass -File scripts\agent-task.ps1 -Agent antigravity -Task ui-pass -Scope "src/ui/*" -Open
  powershell -ExecutionPolicy Bypass -File scripts\agent-task.ps1 -List
#>
param(
    [ValidateSet('claude', 'codex', 'antigravity')]
    [string]$Agent,
    [string]$Task,
    [string]$Scope = 'unspecified',
    [string]$Base = 'main',
    [switch]$Open,
    [switch]$List
)

. (Join-Path $PSScriptRoot 'lib\board.ps1')
$paths = Get-RepoPaths $PSScriptRoot

if ($List) {
    git -C $paths.Main worktree list
    Write-Host ''
    Write-Host "Board: $($paths.Board)"
    Invoke-BoardLocked $paths { Get-Content $paths.Board }
    exit 0
}

if (-not $Agent) { Fail '-Agent is required (claude, codex, antigravity)' }
$wt = New-AgentWorktree $paths $Agent $Task $Scope $Base

Write-Host ''
Write-Host "Worktree: $($wt.Path)"
Write-Host "Branch:   $($wt.Branch)"
Write-Host "Board:    $($paths.Board)"
Write-Host ''
switch ($Agent) {
    'codex'       { Write-Host "Start Codex there:   `"$($wt.Path)\scripts\codex.cmd`"   (or open the folder in the Codex app)" }
    'antigravity' { Write-Host "Open in Antigravity: antigravity-ide `"$($wt.Path)`"" }
    'claude'      { Write-Host "Open a new Claude Code session on: $($wt.Path)" }
}
Write-Host "Merge later: trinity merge $Task"

if ($Open -and $Agent -eq 'antigravity') {
    Start-Process -FilePath 'cmd.exe' -ArgumentList '/c', 'start', '""', 'antigravity-ide', "`"$($wt.Path)`""
}
