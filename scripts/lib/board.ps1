# Shared helpers for agent-task.ps1 and trinity.ps1. Dot-source this file.

function Fail([string]$msg) {
    Write-Host "trinity: $msg" -ForegroundColor Red
    exit 1
}

$script:Utf8 = New-Object Text.UTF8Encoding($false)

# Repo paths seen from any checkout (main or worktree).
function Get-RepoPaths([string]$From) {
    $root = (git -C $From rev-parse --show-toplevel)
    if ($LASTEXITCODE -ne 0 -or -not $root) { Fail 'not inside a git repo' }
    $root = $root -replace '/', '\'
    $common = (git -C $root rev-parse --path-format=absolute --git-common-dir) -replace '/', '\'
    $main = Split-Path $common -Parent
    [pscustomobject]@{
        Root     = $root
        Common   = $common
        Main     = $main
        Board    = Join-Path $common 'agent-board.md'
        Worktrees = Join-Path (Split-Path $main -Parent) ((Split-Path $main -Leaf) + '.worktrees')
        Out      = Join-Path $main '.codex-out'
    }
}

# Every board write goes through a repo-wide exclusive lock so concurrent agents cannot clobber each other.
function Invoke-BoardLocked($Paths, [scriptblock]$Body) {
    $lockPath = Join-Path $Paths.Common 'agent-board.lock'
    $lock = $null
    for ($i = 0; $i -lt 50 -and -not $lock; $i++) {
        try { $lock = [IO.File]::Open($lockPath, 'OpenOrCreate', 'ReadWrite', 'None') }
        catch { Start-Sleep -Milliseconds 200 }
    }
    if (-not $lock) { Fail "board lock busy for 10s: $lockPath" }
    try {
        if (-not (Test-Path $Paths.Board)) {
            $header = "# Agent board`r`n`r`nShared by every worktree of this repo. One section per task. Not committed.`r`n"
            [IO.File]::WriteAllText($Paths.Board, $header, $script:Utf8)
        }
        & $Body
    }
    finally { $lock.Dispose() }
}

function Assert-TaskName([string]$Task) {
    if ($Task -notmatch '^[a-z0-9][a-z0-9-]*$') { Fail "task name '$Task' must be lowercase letters, digits, dashes" }
}

# A merged or cancelled task frees its name: its old section is replaced, so Get-BoardEntry never sees the stale one.
function Add-BoardEntry($Paths, [string]$Task, [string]$Agent, [string]$Branch, [string]$WtPath, [string]$Scope) {
    $stamp = Get-Date -Format 'yyyy-MM-dd HH:mm'
    $entry = "## $Task`r`n- agent: $Agent`r`n- branch: $Branch`r`n- worktree: $WtPath`r`n- scope: $Scope`r`n- status: started $stamp`r`n"
    Invoke-BoardLocked $Paths {
        $kept = New-Object System.Collections.Generic.List[string]
        $inTask = $false
        foreach ($line in [IO.File]::ReadAllLines($Paths.Board)) {
            if ($line -match '^## ') { $inTask = ($line -eq "## $Task") }
            if (-not $inTask) { $kept.Add($line); continue }
            if ($line -match '^- status: (.*)$' -and $Matches[1] -notmatch '^(merged|cancelled)') {
                throw "task $Task is already on the board (status: $($Matches[1]))"
            }
        }
        while ($kept.Count -gt 0 -and $kept[$kept.Count - 1] -eq '') { $kept.RemoveAt($kept.Count - 1) }
        [IO.File]::WriteAllText($Paths.Board, (($kept -join "`r`n") + "`r`n`r`n" + $entry), $script:Utf8)
    }
}

# Replace the status line of one task section.
function Set-BoardStatus($Paths, [string]$Task, [string]$Status) {
    $stamp = Get-Date -Format 'yyyy-MM-dd HH:mm'
    $state = @{ found = $false }   # the locked block runs in a child scope
    Invoke-BoardLocked $Paths {
        $lines = [IO.File]::ReadAllLines($Paths.Board)
        $inTask = $false
        for ($i = 0; $i -lt $lines.Length; $i++) {
            if ($lines[$i] -match '^## ') { $inTask = ($lines[$i] -eq "## $Task") }
            elseif ($inTask -and $lines[$i] -match '^- status:') { $lines[$i] = "- status: $Status $stamp"; $state.found = $true }
        }
        [IO.File]::WriteAllText($Paths.Board, (($lines -join "`r`n") + "`r`n"), $script:Utf8)
    }
    if (-not $state.found) { Write-Host "trinity: task $Task is not on the board; status '$Status' not recorded" -ForegroundColor Yellow }
}

# Set an extra "- <field>: <value>" line of one task section (replaced in place, else added before its status).
function Set-BoardField($Paths, [string]$Task, [string]$Field, [string]$Value) {
    Invoke-BoardLocked $Paths {
        $lines = New-Object System.Collections.Generic.List[string]
        $lines.AddRange([string[]][IO.File]::ReadAllLines($Paths.Board))
        $inTask = $false
        $done = $false
        for ($i = 0; $i -lt $lines.Count -and -not $done; $i++) {
            if ($lines[$i] -match '^## ') { $inTask = ($lines[$i] -eq "## $Task") }
            elseif ($inTask -and $lines[$i] -match "^- $($Field):") { $lines[$i] = "- $($Field): $Value"; $done = $true }
            elseif ($inTask -and $lines[$i] -match '^- status:') { $lines.Insert($i, "- $($Field): $Value"); $done = $true }
        }
        [IO.File]::WriteAllText($Paths.Board, (($lines -join "`r`n") + "`r`n"), $script:Utf8)
    }
}

# Board section for one task as a hashtable (agent, branch, worktree, scope, status).
function Get-BoardEntry($Paths, [string]$Task) {
    if (-not (Test-Path $Paths.Board)) { return $null }
    $entry = $null
    foreach ($line in [IO.File]::ReadAllLines($Paths.Board)) {
        if ($line -match '^## (.+)$') {
            if ($entry) { break }
            if ($Matches[1] -eq $Task) { $entry = @{ task = $Task } }
        }
        elseif ($entry -and $line -match '^- (\w+): (.*)$') { $entry[$Matches[1]] = $Matches[2] }
    }
    $entry
}

# Create worktree + branch agent/<agent>/<task> from $Base and register it on the board.
function New-AgentWorktree($Paths, [string]$Agent, [string]$Task, [string]$Scope, [string]$Base) {
    if ($Agent -notin @('claude', 'codex', 'antigravity')) { Fail '-Agent must be claude, codex, or antigravity' }
    Assert-TaskName $Task
    $old = Get-BoardEntry $Paths $Task
    if ($old -and $old.status -notmatch '^(merged|cancelled)') { Fail "task $Task is already on the board (status: $($old.status)); pick another name" }
    $branch = "agent/$Agent/$Task"
    $wtPath = Join-Path $Paths.Worktrees "$Agent-$Task"
    git -C $Paths.Main rev-parse --verify --quiet "refs/heads/$branch" | Out-Null
    if ($LASTEXITCODE -eq 0) { Fail "branch $branch already exists" }
    if (Test-Path $wtPath) { Fail "path already exists: $wtPath" }
    if (-not (Test-Path $Paths.Worktrees)) { New-Item -ItemType Directory -Path $Paths.Worktrees | Out-Null }
    git -C $Paths.Main worktree add -b $branch $wtPath $Base | Out-Host
    if ($LASTEXITCODE -ne 0) { Fail 'git worktree add failed' }
    try { Add-BoardEntry $Paths $Task $Agent $branch $wtPath $Scope }
    catch {
        # Undo the fresh worktree and branch (no commits yet) so the task name stays usable.
        $why = $_.Exception.Message
        git -C $Paths.Main worktree remove --force $wtPath 2>&1 | Out-Null
        git -C $Paths.Main branch -D $branch 2>&1 | Out-Null
        if (Test-Path $wtPath) { Fail "board registration failed ($why); remove $wtPath and branch $branch by hand" }
        Fail "board registration failed: $why (worktree and branch removed)"
    }
    [pscustomobject]@{ Branch = $branch; Path = $wtPath }
}

# Per-user trinity state (repo registry, dashboard pid). TRINITY_HOME overrides it; the self-test uses that.
function Get-TrinityHome {
    if ($env:TRINITY_HOME) { return $env:TRINITY_HOME }
    Join-Path $env:USERPROFILE '.trinity'
}

# Every repo that uses trinity, one root per line; the dashboard reads it.
function Register-Repo([string]$Root) {
    $dir = Get-TrinityHome
    if (-not (Test-Path $dir)) { New-Item -ItemType Directory -Path $dir | Out-Null }
    $file = Join-Path $dir 'repos.txt'
    $have = @(if (Test-Path $file) { [IO.File]::ReadAllLines($file) })
    if ($have -notcontains $Root) { [IO.File]::AppendAllText($file, "$Root`r`n", $script:Utf8) }
}

# Find-*Exe return $null when the CLI is missing; an AGY_EXE/CODEX_EXE/CLAUDE_EXE override is authoritative.
function Find-AgyExe {
    if ($env:AGY_EXE) { if (Test-Path $env:AGY_EXE) { return $env:AGY_EXE } return $null }
    $exe = Join-Path $env:LOCALAPPDATA 'agy\bin\agy.exe'
    if (Test-Path $exe) { return $exe }
    foreach ($name in 'agy.exe', 'agy.cmd', 'agy.bat') {
        $cmd = Get-Command $name -ErrorAction SilentlyContinue
        if ($cmd) { return $cmd.Source }
    }
    $null
}

# Newest codex.exe shipped with the Codex desktop app, else codex.exe on PATH.
function Find-CodexExe {
    if ($env:CODEX_EXE) { if (Test-Path $env:CODEX_EXE) { return $env:CODEX_EXE } return $null }
    $bin = Join-Path $env:LOCALAPPDATA 'OpenAI\Codex\bin'
    $exe = Get-ChildItem $bin -Directory -ErrorAction SilentlyContinue | Sort-Object LastWriteTime -Descending |
        ForEach-Object { Join-Path $_.FullName 'codex.exe' } | Where-Object { Test-Path $_ } | Select-Object -First 1
    if ($exe) { return $exe }
    $cmd = Get-Command codex.exe -ErrorAction SilentlyContinue
    if ($cmd) { return $cmd.Source }
    $null
}

# Claude Code CLI: PATH, the native installer, the current Claude session, then the desktop app's bundled copy.
function Find-ClaudeExe {
    if ($env:CLAUDE_EXE) { if (Test-Path $env:CLAUDE_EXE) { return $env:CLAUDE_EXE } return $null }
    foreach ($name in 'claude.exe', 'claude.cmd') {
        $cmd = Get-Command $name -ErrorAction SilentlyContinue
        if ($cmd) { return $cmd.Source }
    }
    $local = Join-Path $env:USERPROFILE '.local\bin\claude.exe'
    if (Test-Path $local) { return $local }
    if ($env:CLAUDE_CODE_EXECPATH -and (Test-Path $env:CLAUDE_CODE_EXECPATH)) { return $env:CLAUDE_CODE_EXECPATH }
    $roots = @(Get-ChildItem (Join-Path $env:LOCALAPPDATA 'Packages') -Directory -Filter 'Claude_*' -ErrorAction SilentlyContinue |
            ForEach-Object { Join-Path $_.FullName 'LocalCache\Roaming\Claude\claude-code' }) + @(Join-Path $env:APPDATA 'Claude\claude-code')
    $roots | Where-Object { Test-Path $_ } | ForEach-Object { Get-ChildItem $_ -Recurse -Depth 2 -Filter claude.exe -ErrorAction SilentlyContinue } |
        Sort-Object LastWriteTime -Descending | Select-Object -First 1 -ExpandProperty FullName
}

function Resolve-AgyExe {
    $exe = Find-AgyExe
    if ($exe) { return $exe }
    Fail 'agy.exe (Antigravity CLI) not found. Install it or set AGY_EXE.'
}

function Resolve-CodexExe {
    $exe = Find-CodexExe
    if ($exe) { return $exe }
    Fail 'codex.exe not found. Install the Codex desktop app or set CODEX_EXE.'
}

# Run a CLI's login check; exit code 0 = usable. The child must not think it is nested in this Claude session.
function Test-CliLogin([string]$Exe, [string[]]$CliArgs) {
    $savedPref = $ErrorActionPreference
    $savedNested = $env:CLAUDECODE
    $ErrorActionPreference = 'Continue'
    $env:CLAUDECODE = $null
    try {
        $out = @($null | & $Exe @CliArgs 2>&1 | ForEach-Object { "$_" })
        $rc = $LASTEXITCODE
    }
    catch { $out = @($_.Exception.Message); $rc = 1 }
    finally { $ErrorActionPreference = $savedPref; $env:CLAUDECODE = $savedNested }
    [pscustomobject]@{ Ok = ($rc -eq 0); First = "$(@($out | Where-Object { $_.Trim() })[0])".Trim() }
}

# Which agents trinity can run headless here, as agent -> @{ available; detail; exe }.
# TRINITY_DISABLE (comma list, e.g. "codex") turns an agent off even when it is installed.
function Get-AgentAvailability([string[]]$Agents = @('claude', 'codex', 'antigravity')) {
    $disabled = @("$env:TRINITY_DISABLE" -split '[,; ]+' | Where-Object { $_ } | ForEach-Object { $_.Trim().ToLowerInvariant() })
    $result = [ordered]@{}
    foreach ($agent in $Agents) {
        $exe = switch ($agent) { 'codex' { Find-CodexExe } 'antigravity' { Find-AgyExe } 'claude' { Find-ClaudeExe } }
        if ($disabled -contains $agent) { $state = @{ available = $false; detail = 'disabled by TRINITY_DISABLE' } }
        elseif (-not $exe) { $state = @{ available = $false; detail = "$agent CLI not found" } }
        elseif ($agent -eq 'codex') {
            $login = Test-CliLogin $exe @('login', 'status')
            $state = if ($login.Ok) { @{ available = $true; detail = $(if ($login.First) { $login.First } else { 'logged in' }) } }
                else { @{ available = $false; detail = 'not logged in (run: codex login)' } }
        }
        elseif ($agent -eq 'claude') {
            $login = Test-CliLogin $exe @('auth', 'status')
            $state = if ($login.Ok) { @{ available = $true; detail = 'logged in' } }
                else { @{ available = $false; detail = 'not logged in (run: claude auth login)' } }
        }
        else { $state = @{ available = $true; detail = 'ok' } }
        $state.exe = "$exe"
        $result[$agent] = $state
    }
    $result
}
