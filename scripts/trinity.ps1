# Trinity: one entry point for the Claude + Codex + Antigravity shared workspace.
# Run "trinity help" for the command list.
param(
    [Parameter(Position = 0)][string]$Command = 'status',
    [string]$Scope = 'unspecified',
    [string]$Base = 'main',
    [switch]$Yes,
    [switch]$Force,
    [switch]$Brief,
    [string]$PromptFile,
    [int]$Timeout = 1800,
    [int]$Port = 7777,
    [switch]$Json,
    [switch]$Stop,
    [Parameter(ValueFromRemainingArguments = $true)][string[]]$Rest
)

. (Join-Path $PSScriptRoot 'lib\board.ps1')
$paths = Get-RepoPaths $PSScriptRoot
if (-not (Test-Path $paths.Out)) { New-Item -ItemType Directory -Path $paths.Out | Out-Null }

function Show-Usage {
    Write-Host 'trinity <command> [options]'
    Write-Host ''
    Write-Host '  status [-Brief]                       branch, agents available, worktrees, board, HANDOFF status'
    Write-Host '  review [--base <b> | --commit <sha>]  read-only review of uncommitted changes (default), a branch, or a commit'
    Write-Host '                                          trinity review --base main'
    Write-Host '  ask <prompt...>                       one-shot read-only question (prompt can also come on stdin)'
    Write-Host '                                          review/ask/merge use Codex, else Claude Code, else Antigravity'
    Write-Host '                                          (TRINITY_REVIEWER=codex|claude|antigravity|none picks one)'
    Write-Host '                                          trinity ask "why does the login test fail?"'
    Write-Host '  dispatch <agent> <task> <prompt...>   worktree + task file + start the agent'
    Write-Host '                                          trinity dispatch codex login-form -Scope "src/auth/*" Build the login form'
    Write-Host '                                          (agent: codex | antigravity = headless, background; claude = task file)'
    Write-Host '                                          if that agent cannot run here, the other headless one takes it'
    Write-Host '                                          (TRINITY_DISABLE=codex turns Codex off, e.g. without a ChatGPT plan)'
    Write-Host '  run <agent> <task> <prompt...>        dispatch, then wait and print result + commits'
    Write-Host '                                          trinity run codex login-form -Timeout 900 Build the login form'
    Write-Host '  wait <task> [-Timeout 1800]           block until done; exit 0 done, 1 failed, 2 timeout, 3 other'
    Write-Host '                                          trinity wait login-form -Timeout 600'
    Write-Host '  done <task> [summary...]              agent marks its own task done on the board'
    Write-Host '                                          trinity done login-form added the login form'
    Write-Host '  (dispatch/run) -PromptFile <file>     read the prompt from a file instead of <prompt...>'
    Write-Host '                                          trinity dispatch codex login-form -PromptFile prompt.txt'
    Write-Host '  merge <task> [-Yes] [-Force]          review, merge --no-ff, run tests, clean up'
    Write-Host '                                          trinity merge login-form'
    Write-Host '  cancel <task> [-Force]                remove the worktree and branch, mark cancelled'
    Write-Host '  init <path> [-Force]                  install or update trinity in another git repo'
    Write-Host '                                          trinity init D:\code\myapp   (keeps its docs unless -Force)'
    Write-Host '  track <task> <note...>                put your own (Claude) work on the board; finish with done'
    Write-Host '  dashboard [-Port 7777] [-Json] [-Stop] live page of all three agents across every trinity repo'
    Write-Host '  ui                                    open the main checkout in Antigravity'
    Write-Host '  setup                                 enable .githooks and create .codex-out'
    Write-Host '  help                                  this text'
}

# Codex review findings: a "- [Pn] title" line plus the indented lines under it.
function Split-Findings([string[]]$lines) {
    $blocks = @()
    $cur = $null
    foreach ($l in $lines) {
        if ($l -match '^\s*-\s*\[P[0-3]\]') {
            if ($cur) { $blocks += $cur }
            $cur = $l
        }
        elseif ($cur -and $l.Trim()) { $cur += "`n" + $l }
        elseif ($cur) { $blocks += $cur; $cur = $null }
    }
    if ($cur) { $blocks += $cur }
    $blocks
}

# Backticked paths/globs on the "- Shared files: ..." line of AGENTS.md.
function Get-SharedFiles([string]$root) {
    $md = Join-Path $root 'AGENTS.md'
    if (-not (Test-Path $md)) { return @() }
    foreach ($l in [IO.File]::ReadAllLines($md)) {
        if ($l -match '^- Shared files: (.+)$') {
            return @([regex]::Matches($Matches[1], '`([^`]+)`') | ForEach-Object { $_.Groups[1].Value } | Where-Object { $_ -ne 'TBD' })
        }
    }
    @()
}

# Native stderr noise must not throw, so every git call here ignores stderr via cmd-free capture.
function Get-Git([string[]]$GitArgs) {
    $out = & git @GitArgs 2>$null
    if ($LASTEXITCODE -ne 0) { return $null }
    $out
}

function Format-Availability($avail) {
    (@($avail.Keys | ForEach-Object { if ($avail[$_].available) { "$_ ok" } else { "$_ unavailable ($($avail[$_].detail))" } }) -join ', ')
}

# Headless worker for a dispatch: the requested one if it can run here, else the other headless agent.
function Select-Worker([string]$agent, [string]$task) {
    if ($agent -notin @('codex', 'antigravity')) { return $agent }
    $avail = Get-AgentAvailability @('codex', 'antigravity')
    if ($avail[$agent].available) { return $agent }
    $other = if ($agent -eq 'codex') { 'antigravity' } else { 'codex' }
    if (-not $avail[$other].available) {
        Fail "no headless worker can run here ($(Format-Availability $avail)); do $task yourself or use: trinity dispatch claude $task ..."
    }
    Write-Host "$agent unavailable ($($avail[$agent].detail)); routing $task to $other." -ForegroundColor Yellow
    $other
}

# Reviewer: TRINITY_REVIEWER (codex|claude|antigravity|none) if usable, else Codex, else Claude or Antigravity,
# preferring one that did not write the change. $null = nobody can review.
function Select-Reviewer([string]$author) {
    $choice = "$env:TRINITY_REVIEWER".Trim().ToLowerInvariant()
    if ($choice -eq 'none') { return $null }
    $avail = Get-AgentAvailability
    if ($choice) {
        if ($avail.Contains($choice) -and $avail[$choice].available) { return $choice }
        Write-Host "TRINITY_REVIEWER=$choice cannot run here; picking another reviewer." -ForegroundColor Yellow
    }
    if ($avail.codex.available) { return 'codex' }
    $usable = @('claude', 'antigravity' | Where-Object { $avail[$_].available })
    $pick = @(@($usable | Where-Object { $_ -ne $author }) + $usable) | Select-Object -First 1
    if (-not $pick) { Write-Host "No reviewer can run here ($(Format-Availability $avail))." -ForegroundColor Yellow }
    $pick
}

# Run Claude or Antigravity headless and read-only on a prompt file; returns @{ ExitCode; Lines }.
function Invoke-ReadOnlyAgent([string]$agent, [string]$dir, [string]$promptFile, [string]$tag) {
    $out = Join-Path $paths.Out "$agent-out-$tag.txt"
    $err = Join-Path $paths.Out "$agent-err-$tag.txt"
    $runFile = Join-Path $paths.Out "run-$agent-$tag.cmd"
    if ($agent -eq 'claude') {
        $exe = Find-ClaudeExe
        $cmdLine = 'cd /d "' + $dir + '" && "' + $exe + '" -p --tools Read,Grep,Glob --add-dir "' + $paths.Out + '" < "' + $promptFile + '" > "' + $out + '" 2> "' + $err + '"'
    }
    else {
        $exe = Find-AgyExe
        # Plan mode cannot edit; headless it also cannot run commands, so the prompt asks for the file viewer.
        # A read outside the workspace is auto-denied headless, and one denial drops agy's whole answer.
        $cmdLine = 'cd /d "' + $dir + '" && "' + $exe + '" -p "Use your file viewing tool (not the terminal) to read ' + $promptFile + ' and follow it. Do not run any commands. Read only files inside ' + $dir + ' or ' + $paths.Out + ': anything outside them is blocked and ends your turn without an answer." --mode plan --output-format json --add-dir "' + $paths.Out + '" --print-timeout 1200s > "' + $out + '" 2> "' + $err + '"'
    }
    [IO.File]::WriteAllText($runFile, "@echo off`r`n$cmdLine`r`nexit /b %ERRORLEVEL%`r`n", $script:Utf8)
    # A Claude child refuses to start while it looks nested in this Claude session.
    $nested = $env:CLAUDECODE
    $env:CLAUDECODE = $null
    try {
        & $runFile; $rc = $LASTEXITCODE
        if ($agent -eq 'antigravity' -and (Test-Path $err) -and ([IO.File]::ReadAllText($err) -match 'auto-denied')) {
            Write-Host 'antigravity strayed outside its workspace; retrying once.' -ForegroundColor Yellow
            & $runFile; $rc = $LASTEXITCODE
        }
    }
    finally { $env:CLAUDECODE = $nested }
    $text = if (Test-Path $out) { [IO.File]::ReadAllText($out) } else { '' }
    if ($agent -eq 'antigravity') {
        try {
            $j = $text | ConvertFrom-Json
            $text = "$($j.response)"
            if ($j.status -ne 'SUCCESS' -or -not $text.Trim()) { $rc = [Math]::Max($rc, 1) }
        }
        catch { $rc = [Math]::Max($rc, 1) }
        $text = $text -replace '(?s)The following is a <SYSTEM_MESSAGE>.*?</SYSTEM_MESSAGE>\s*', ''
    }
    if ($rc -ne 0 -and (Test-Path $err)) { $text += "`n" + [IO.File]::ReadAllText($err) }
    [pscustomobject]@{ ExitCode = $rc; Lines = @($text -split '\r?\n') }
}

# agy -p joins the text of every turn into "response": an agent that starts tests in the background ends a turn
# with "Running tests..." and is resumed later. Only the last turn is its summary, so take that turn's text from
# the session transcript; keep the whole response when the transcript is missing or does not end with it.
function Get-AgyFinalMessage([string]$session, [string]$response) {
    if ($session -notmatch '^[0-9a-fA-F-]{36}$') { return $response }
    $agyHome = if ($env:TRINITY_AGY_HOME) { $env:TRINITY_AGY_HOME } else { Join-Path $env:USERPROFILE '.gemini\antigravity-cli' }
    $transcript = Join-Path $agyHome "brain\$session\.system_generated\logs\transcript.jsonl"
    if (-not (Test-Path -LiteralPath $transcript)) { return $response }
    $last = ''
    try {
        foreach ($line in [IO.File]::ReadAllLines($transcript)) {
            if (-not $line.Contains('"PLANNER_RESPONSE"') -or -not $line.Contains('"content"')) { continue }
            try { $record = $line.TrimStart([char]0xFEFF) | ConvertFrom-Json } catch { continue }
            if ($record.type -eq 'PLANNER_RESPONSE' -and "$($record.content)".Trim()) { $last = "$($record.content)".Trim() }
        }
    }
    catch { return $response }
    if ($last -and $response.TrimEnd().EndsWith($last, [StringComparison]::Ordinal)) { return $last + "`n" }
    $response
}

# Review uncommitted changes ('uncommitted'), the current branch against $ref ('base'), or one commit ('commit').
function Invoke-Review([string]$reviewer, [string]$dir, [string]$mode, [string]$ref, [string]$tag) {
    Write-Host "reviewer: $reviewer"
    if ($reviewer -eq 'codex') {
        $exe = Resolve-CodexExe
        # @() keeps a one-element result an array; splatting a bare string would pass it char by char.
        $codexArgs = @(switch ($mode) { 'base' { '--base', $ref } 'commit' { '--commit', $ref } default { '--uncommitted' } })
        Push-Location $dir
        try { $lines = @($null | & $exe review -c sandbox_mode=read-only @codexArgs 2>&1 | ForEach-Object { "$_" }); $rc = $LASTEXITCODE }
        finally { Pop-Location }
        return [pscustomobject]@{ ExitCode = $rc; Lines = $lines }
    }
    switch ($mode) {
        'base' { $diff = Get-Git @('-C', $dir, 'diff', "$ref...HEAD"); $what = "the current branch against $ref" }
        'commit' { $diff = Get-Git @('-C', $dir, 'show', $ref); $what = "commit $ref" }
        default { $diff = Get-Git @('-C', $dir, 'diff', 'HEAD'); $what = 'the uncommitted changes' }
    }
    $diffText = ($diff -join "`n")
    if ($diffText.Length -gt 400000) { $diffText = $diffText.Substring(0, 400000) + "`n[patch truncated at 400 KB]" }
    $patch = Join-Path $paths.Out "review-diff-$tag.patch"
    [IO.File]::WriteAllText($patch, $diffText + "`n", $script:Utf8)
    $untracked = @(if ($mode -eq 'uncommitted') { Get-Git @('-C', $dir, 'ls-files', '--others', '--exclude-standard') })
    $prompt = @(
        '# Code review', '',
        "Review $what. The patch is in: $patch",
        "Repository: $dir. Read any file there you need for context; do not open files outside it.",
        $(if ($untracked) { 'New untracked files, not in the patch (read them directly): ' + ($untracked -join ', ') }),
        'Do not edit, create or delete files, and do not run commands.', '',
        'Report only real problems: bugs, security holes, data loss, broken behaviour, missing error handling that matters. Skip style and naming.',
        'Answer with findings only, one block per finding, in exactly this format:', '',
        '- [P1] <one-line title> - <path>:<line>',
        '  <one or two lines: what breaks and when>', '',
        'P0 = security hole, data loss or crash on the main path; P1 = wrong behaviour users will hit; P2 = edge-case bug; P3 = minor.',
        'If there is nothing to report, answer exactly: No findings.'
    ) -join "`r`n"
    $promptFile = Join-Path $paths.Out "review-prompt-$tag.md"
    [IO.File]::WriteAllText($promptFile, $prompt + "`r`n", $script:Utf8)
    Invoke-ReadOnlyAgent $reviewer $dir $promptFile "review-$tag"
}

function Show-Status {
    $branch = Get-Git @('-C', $paths.Main, 'branch', '--show-current')
    $dirty = @(Get-Git @('-C', $paths.Main, 'status', '--porcelain') | Where-Object { $_ }).Count
    Write-Host "main checkout: branch=$branch uncommitted=$dirty"
    Write-Host "agents: $(Format-Availability (Get-AgentAvailability))"
    Write-Host ''
    Write-Host 'Worktrees:'
    Get-Git @('-C', $paths.Main, 'worktree', 'list') | ForEach-Object { Write-Host "  $_" }

    Write-Host ''
    Write-Host 'Board:'
    if (Test-Path $paths.Board) {
        $lines = [IO.File]::ReadAllLines($paths.Board)
        if ($Brief) {
            # Brief view skips merged tasks so session-start context stays small.
            $section = $null
            foreach ($l in $lines) {
                if ($l -match '^## ') { $section = $l }
                elseif ($l -match '^- status:' -and $l -notmatch '^- status: merged') { Write-Host "  $section  ($($l.Substring(2)))" }
            }
        }
        else {
            $lines | Where-Object { $_ -notmatch '^# ' -and $_.Trim() } | ForEach-Object { Write-Host "  $_" }
        }
    }
    else { Write-Host '  (no board yet)' }

    # HANDOFF.md "## Status" section, up to the next "## ".
    $handoff = Join-Path $paths.Main 'HANDOFF.md'
    if (Test-Path $handoff) {
        Write-Host ''
        Write-Host 'HANDOFF status:'
        $in = $false
        foreach ($l in [IO.File]::ReadAllLines($handoff)) {
            if ($l -match '^## ') { $in = ($l -match '^## Status'); continue }
            if ($in -and $l.Trim()) { Write-Host "  $l" }
        }
    }
}

# Poll the board until the task leaves started/running/waiting, then print status, result and commits.
# Returns 0 done, 1 failed, 2 timeout (still pending), 3 anything else (e.g. merged).
function Wait-Task([string]$task, [int]$timeoutSec) {
    $entry = Get-BoardEntry $paths $task
    if (-not $entry) { Fail "no board entry for $task" }
    $start = Get-Date
    $lastNote = $start
    while ($true) {
        $entry = Get-BoardEntry $paths $task
        $status = $entry.status
        # Headless tasks finish only when the runner writes its own status (after committing), never on a bare "done".
        $headless = $entry.agent -in @('codex', 'antigravity')
        # The runner deletes its pid file once it has committed and written the status, so a live runner
        # means pending whatever the board says (an agent could write a lookalike status itself).
        $pidFile = Join-Path $paths.Out "pid-$task.txt"
        $runnerAlive = $false
        if ($headless -and (Test-Path $pidFile)) {
            $runnerPid = [IO.File]::ReadAllText($pidFile).Trim()
            $runnerAlive = [bool]($runnerPid -and (Get-Process -Id $runnerPid -ErrorAction SilentlyContinue))
        }
        $runnerDone = -not $runnerAlive -and ((-not $headless) -or ($status -match '^(done \(|done, no commits|failed|merged|cancelled)'))
        if ($runnerDone -and $status -notmatch '^(started|running|waiting)') { break }
        if (((Get-Date) - $start).TotalSeconds -ge $timeoutSec) { break }
        if (((Get-Date) - $lastNote).TotalSeconds -ge 60) {
            Write-Host "waiting on $($task): $status"
            $lastNote = Get-Date
        }
        Start-Sleep -Seconds 5
    }
    $pending = -not $runnerDone -or ($status -match '^(started|running|waiting)')
    if ($pending) { Write-Host "timeout after $($timeoutSec)s, still: $status" }
    Write-Host "status: $status"
    $result = Join-Path $paths.Out "result-$task.md"
    if (Test-Path $result) {
        Write-Host '--- result ---'
        Write-Host ([IO.File]::ReadAllText($result))
    }
    $branch = $entry.branch
    if ($branch -and (Get-Git @('-C', $paths.Main, 'rev-parse', '--verify', '--quiet', "refs/heads/$branch"))) {
        Write-Host '--- commits ---'
        Get-Git @('-C', $paths.Main, 'log', '--oneline', "main..$branch") | ForEach-Object { Write-Host $_ }
        Write-Host '--- diffstat ---'
        Get-Git @('-C', $paths.Main, 'diff', '--stat', "main...$branch") | ForEach-Object { Write-Host $_ }
    }
    if ($pending) { return 2 }
    if ($status -match '^done') { return 0 }
    if ($status -match '^(failed|cancelled)') { return 1 }
    3
}

# Create worktree + task file for one agent and start it (codex/antigravity) or park it (claude).
function Start-Dispatch([string]$agent, [string]$task, [string]$prompt) {
    # Pick a worker that can run here now, in the foreground, rather than failing in the hidden runner.
    $agent = Select-Worker $agent $task
    $wt = New-AgentWorktree $paths $agent $task $Scope $Base

    $taskFile = Join-Path $paths.Out "task-$task.md"
    $resultPath = Join-Path $paths.Out "result-$task.md"
    $body = @(
        "# Task: $task",
        '',
        "- agent: $agent",
        "- task: $task",
        "- branch: $($wt.Branch)",
        "- worktree: $($wt.Path)",
        "- scope: $Scope",
        '',
        '## Request',
        '',
        $prompt,
        '',
        '## Rules',
        '',
        '- Read AGENTS.md first.',
        '- Edit only files within the scope.',
        $(if ($agent -ne 'claude') { '- Work only inside your current directory (your worktree). Do not run git commit or push; the runner commits your changes when you finish, using your final summary as the message.',
             '- Do not start GUI apps or long-running servers; you run headless.' }
          else { '- Commit your work on this branch with clear messages (no Co-Authored-By trailers).',
             "- When finished: write your summary to $resultPath, then run in your worktree: trinity done $task <one-line summary>" }),
        '- Do not push.',
        '- Do not edit HANDOFF.md or PLAN.md.',
        '- Finish with a short summary of what you did and anything left.'
    ) -join "`r`n"
    [IO.File]::WriteAllText($taskFile, $body + "`r`n", $script:Utf8)

    Write-Host "Worktree: $($wt.Path)"
    Write-Host "Branch:   $($wt.Branch)"
    Write-Host "Task:     $taskFile"
    switch ($agent) {
        { $_ -in @('codex', 'antigravity') } {
            Set-BoardStatus $paths $task 'running'
            $runner = Start-Process powershell.exe -WindowStyle Hidden -PassThru -ArgumentList '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', "`"$PSCommandPath`"", '_run', $task
            [IO.File]::WriteAllText((Join-Path $paths.Out "pid-$task.txt"), "$($runner.Id)", $script:Utf8)
            Write-Host "$agent started headless in background."
            Write-Host "Log:    $(Join-Path $paths.Out "log-$task.txt")"
            Write-Host "Result: $(Join-Path $paths.Out "result-$task.md")"
            Write-Host 'Check progress with: trinity status'
        }
        'claude' {
            Set-BoardStatus $paths $task 'waiting for claude session'
            Write-Host "Open a new Claude Code session on $($wt.Path) and say: do the task in $taskFile"
        }
    }
}

switch ($Command) {
    'status' { Show-Status }

    'review' {
        # PowerShell binds "--base x" to -Base, so a bound Base means a branch review.
        $reviewArgs = @($Rest | Where-Object { $_ })
        $mode = 'uncommitted'; $ref = ''
        # A target flag without its value must not silently fall back to reviewing uncommitted changes.
        foreach ($flag in @('--commit', '--base')) {
            $i = [array]::IndexOf($reviewArgs, $flag)
            if ($i -ge 0 -and ($i + 1 -ge $reviewArgs.Count -or $reviewArgs[$i + 1] -like '-*')) { Fail "usage: trinity review [--base <branch> | --commit <sha>] ($flag needs a value)" }
        }
        $i = [array]::IndexOf($reviewArgs, '--commit')
        if ($i -ge 0 -and $i + 1 -lt $reviewArgs.Count) { $mode = 'commit'; $ref = $reviewArgs[$i + 1] }
        elseif ($PSBoundParameters.ContainsKey('Base')) { $mode = 'base'; $ref = $Base }
        else {
            $i = [array]::IndexOf($reviewArgs, '--base')
            if ($i -ge 0 -and $i + 1 -lt $reviewArgs.Count) { $mode = 'base'; $ref = $reviewArgs[$i + 1] }
        }
        $reviewer = Select-Reviewer ''
        if (-not $reviewer) { Fail 'no reviewer available; install or log in to Codex, Claude Code or Antigravity' }
        $rv = Invoke-Review $reviewer $paths.Root $mode $ref 'last'
        $rv.Lines | ForEach-Object { Write-Host $_ }
        [IO.File]::WriteAllText((Join-Path $paths.Out 'review-last.txt'), (($rv.Lines -join "`r`n") + "`r`n"), $script:Utf8)
        exit $rv.ExitCode
    }

    'ask' {
        $prompt = $Rest -join ' '
        if (-not $prompt.Trim() -and [Console]::IsInputRedirected) { $prompt = [Console]::In.ReadToEnd() }
        if (-not $prompt.Trim()) { Fail 'usage: trinity ask <prompt...>   (or pipe the prompt on stdin)' }
        $answer = Join-Path $paths.Out 'codex-last.md'
        $asker = Select-Reviewer ''
        if (-not $asker) { Fail 'nobody to ask; install or log in to Codex, Claude Code or Antigravity' }
        Write-Host "asking: $asker"
        if ($asker -eq 'codex') {
            $exe = Resolve-CodexExe
            $OutputEncoding = $script:Utf8
            $prompt | & $exe exec --sandbox read-only --ephemeral --skip-git-repo-check -C $paths.Root -o $answer -
            exit $LASTEXITCODE
        }
        $promptFile = Join-Path $paths.Out 'ask-prompt.md'
        [IO.File]::WriteAllText($promptFile, "Answer this question about the repository at $($paths.Root). Read files as needed; do not edit anything or run commands.`r`n`r`n$prompt`r`n", $script:Utf8)
        $rv = Invoke-ReadOnlyAgent $asker $paths.Root $promptFile 'ask'
        [IO.File]::WriteAllText($answer, (($rv.Lines -join "`r`n") + "`r`n"), $script:Utf8)
        $rv.Lines | ForEach-Object { Write-Host $_ }
        exit $rv.ExitCode
    }

    'dispatch' {
        if ($PromptFile) {
            if (@($Rest).Count -lt 2) { Fail 'usage: trinity dispatch <codex|antigravity|claude> <task> -PromptFile <file>' }
            if (-not (Test-Path $PromptFile)) { Fail "prompt file not found: $PromptFile" }
            $prompt = [IO.File]::ReadAllText($PromptFile)
        }
        else {
            if (@($Rest).Count -lt 3) { Fail 'usage: trinity dispatch <codex|antigravity|claude> <task> <prompt...> (or -PromptFile <file>)' }
            $prompt = ($Rest[2..($Rest.Count - 1)]) -join ' '
        }
        Start-Dispatch $Rest[0] $Rest[1] $prompt
    }

    'run' {
        if ($PromptFile) {
            if (@($Rest).Count -lt 2) { Fail 'usage: trinity run <codex|antigravity|claude> <task> -PromptFile <file>' }
            if (-not (Test-Path $PromptFile)) { Fail "prompt file not found: $PromptFile" }
            $prompt = [IO.File]::ReadAllText($PromptFile)
        }
        else {
            if (@($Rest).Count -lt 3) { Fail 'usage: trinity run <codex|antigravity|claude> <task> <prompt...> (or -PromptFile <file>)' }
            $prompt = ($Rest[2..($Rest.Count - 1)]) -join ' '
        }
        Start-Dispatch $Rest[0] $Rest[1] $prompt
        if ($Rest[0] -eq 'claude') {
            Write-Host 'claude tasks need a Claude Code session; not waiting.'
            exit 0
        }
        exit (Wait-Task $Rest[1] $Timeout)
    }

    'wait' {
        $task = $Rest[0]
        if (-not $task) { Fail 'usage: trinity wait <task> [-Timeout 1800]' }
        exit (Wait-Task $task $Timeout)
    }

    'done' {
        $task = $Rest[0]
        if (-not $task) { Fail 'usage: trinity done <task> [summary...]' }
        $entry = Get-BoardEntry $paths $task
        if (-not $entry) { Fail "no board entry for $task" }
        if ($entry.agent -in @('codex', 'antigravity')) { Fail "$task is a headless $($entry.agent) task; its runner marks it done after committing" }
        $summary = ''
        if (@($Rest).Count -gt 1) { $summary = (($Rest[1..($Rest.Count - 1)]) -join ' ') -replace '[\r\n]+', ' ' }
        $status = if ($summary.Trim()) { "done: $($summary.Trim())" } else { 'done' }
        Set-BoardStatus $paths $task $status
        Write-Host "status: $status"
    }

    { $_ -in @('_run', '_run-codex') } {
        $task = $Rest[0]
        $entry = Get-BoardEntry $paths $task
        if (-not $entry) { Fail "no board entry for $task" }
        $taskFile = Join-Path $paths.Out "task-$task.md"
        $log = Join-Path $paths.Out "log-$task.txt"
        $result = Join-Path $paths.Out "result-$task.md"

        # Run through a generated .cmd file: PS 5.1 mangles embedded quotes passed to cmd /c,
        # and cmd gives stdin/stdout redirection that works with spaces in paths.
        # Metadata for the dashboard (docs/dashboard.md): start now, model/session/tokens when the agent ends.
        $metaFile = Join-Path $paths.Out "meta-$task.json"
        $meta = [ordered]@{ agent = $entry.agent; started = (Get-Date -Format 'yyyy-MM-ddTHH:mm:ss') }
        [IO.File]::WriteAllText($metaFile, ($meta | ConvertTo-Json -Compress), $script:Utf8)
        $agyJson = Join-Path $paths.Out "agyout-$task.json"
        $agyLog = Join-Path $paths.Out "agy-$task.log"

        $runFile = Join-Path $paths.Out "run-$task.cmd"
        if ($entry.agent -eq 'antigravity') {
            # agy -p --output-format json returns the final answer plus conversation id and token use;
            # permissions are skipped, so the task rules confine it to its worktree.
            $exe = Resolve-AgyExe
            Remove-Item $agyJson, $agyLog -ErrorAction SilentlyContinue
            $cmdLine = 'cd /d "' + $entry.worktree + '" && "' + $exe + '" -p "Read the task file ' + $taskFile + ' and do it. Your final message is your summary." --dangerously-skip-permissions --add-dir "' + $paths.Out + '" --output-format json --log-file "' + $agyLog + '" > "' + $agyJson + '" 2> "' + $log + '"'
        }
        else {
            $exe = Resolve-CodexExe
            $cmdLine = '"' + $exe + '" exec --sandbox workspace-write --add-dir "' + $paths.Common + '" --skip-git-repo-check -C "' + $entry.worktree + '" -o "' + $result + '" - < "' + $taskFile + '" > "' + $log + '" 2>&1'
        }
        [IO.File]::WriteAllText($runFile, "@echo off`r`n$cmdLine`r`nexit /b %ERRORLEVEL%`r`n", $script:Utf8)
        & $runFile
        $rc = $LASTEXITCODE
        if ((Get-BoardEntry $paths $task).status -match '^cancelled') { exit 0 }

        $meta.finished = Get-Date -Format 'yyyy-MM-ddTHH:mm:ss'
        $meta.exitCode = $rc
        if ($entry.agent -eq 'antigravity') {
            # The JSON answer becomes the result file; output that is not JSON is kept as the summary as-is.
            $raw = if (Test-Path $agyJson) { [IO.File]::ReadAllText($agyJson) } else { '' }
            $text = $raw
            try {
                $j = $raw | ConvertFrom-Json
                if ($j -and $null -ne $j.response) {
                    $meta.sessionId = "$($j.conversation_id)"
                    $text = Get-AgyFinalMessage $meta.sessionId "$($j.response)"
                    if ($j.duration_seconds) { $meta.durationSec = [double]$j.duration_seconds }
                    if ($j.usage) {
                        $meta.tokens = [ordered]@{ input = [long]$j.usage.input_tokens; cached = [long]$j.usage.cache_read_tokens
                            output = [long]$j.usage.output_tokens; reasoning = [long]$j.usage.thinking_tokens; total = [long]$j.usage.total_tokens }
                    }
                }
            }
            catch { }
            # agy -p echoes its internal background-task notices; keep only the agent's own summary.
            $text = $text -replace '(?s)The following is a <SYSTEM_MESSAGE>.*?</SYSTEM_MESSAGE>\s*', ''
            [IO.File]::WriteAllText($result, $text, $script:Utf8)
            if (Test-Path $agyLog) {
                $m = [regex]::Match([IO.File]::ReadAllText($agyLog), 'label="([^"]+)"')
                if ($m.Success) { $meta.model = $m.Groups[1].Value }
            }
        }
        elseif (Test-Path $log) {
            # Codex prints its settings in the log header.
            $head = [IO.File]::ReadAllText($log)
            if ($head.Length -gt 8192) { $head = $head.Substring(0, 8192) }
            foreach ($pair in @(@('model', '(?m)^model:\s*(\S+)'), @('effort', '(?m)^reasoning effort:\s*(\S+)'), @('sessionId', '(?m)^session id:\s*(\S+)'))) {
                $m = [regex]::Match($head, $pair[1])
                if ($m.Success) { $meta[$pair[0]] = $m.Groups[1].Value }
            }
        }
        if (-not $meta.Contains('durationSec')) { $meta.durationSec = [Math]::Round(((Get-Date $meta.finished) - (Get-Date $meta.started)).TotalSeconds, 1) }
        [IO.File]::WriteAllText($metaFile, ($meta | ConvertTo-Json -Compress -Depth 4), $script:Utf8)

        # Codex's sandbox keeps .git read-only, so the runner commits for every headless agent; the final summary is the message.
        if ($rc -eq 0 -and (Get-Git @('-C', $entry.worktree, 'status', '--porcelain'))) {
            $summary = @(if (Test-Path $result) { [IO.File]::ReadAllLines($result) | Where-Object { $_.Trim() } })
            $subject = "$($task): " + $(if ($summary) { ($summary[0] -replace '\[([^\]]*)\]\([^)]*\)', '$1' -replace '[\[\]`*]', '') } else { 'Codex changes' })
            if ($subject.Length -gt 72) { $subject = $subject.Substring(0, 69) + '...' }
            $msgFile = Join-Path $paths.Out "commitmsg-$task.txt"
            [IO.File]::WriteAllText($msgFile, $subject + "`r`n`r`n" + ($summary -join "`r`n") + "`r`n", $script:Utf8)
            git -C $entry.worktree add -A
            git -C $entry.worktree commit -q -F $msgFile
            if ($LASTEXITCODE -ne 0) { $rc = 2 }
        }

        $n = [int](Get-Git @('-C', $paths.Main, 'rev-list', '--count', "main..$($entry.branch)"))
        if ($rc -eq 0 -and $n -gt 0) { $status = "done ($n commits)" }
        elseif ($rc -eq 0) { $status = 'done, no commits' }
        else { $status = "failed (exit $rc)" }
        Set-BoardStatus $paths $task $status
        Remove-Item (Join-Path $paths.Out "pid-$task.txt") -ErrorAction SilentlyContinue

        try {
            Add-Type -AssemblyName System.Windows.Forms
            Add-Type -AssemblyName System.Drawing
            $ni = New-Object System.Windows.Forms.NotifyIcon
            $ni.Icon = [System.Drawing.SystemIcons]::Information
            $ni.Visible = $true
            $ni.BalloonTipTitle = "Trinity: $task"
            $ni.BalloonTipText = $status
            $ni.ShowBalloonTip(8000)
            Start-Sleep 9
            $ni.Dispose()
        }
        catch { }
    }

    'cancel' {
        $task = $Rest[0]
        if (-not $task) { Fail 'usage: trinity cancel <task>' }
        $entry = Get-BoardEntry $paths $task
        if (-not $entry) { Fail "no board entry for $task" }
        $n = [int](Get-Git @('-C', $paths.Main, 'rev-list', '--count', "main..$($entry.branch)"))
        if ($n -gt 0 -and -not $Force) { Fail "$($entry.branch) has $n commits; rerun with -Force to drop them" }
        # Mark first so a runner that is still finishing never overwrites it, then stop the runner and its agent.
        Set-BoardStatus $paths $task 'cancelled'
        $pidFile = Join-Path $paths.Out "pid-$task.txt"
        if (Test-Path $pidFile) {
            $runnerPid = [IO.File]::ReadAllText($pidFile).Trim()
            if ($runnerPid -and (Get-Process -Id $runnerPid -ErrorAction SilentlyContinue)) {
                taskkill.exe /PID $runnerPid /T /F | Out-Null
                Write-Host "Stopped runner process tree $runnerPid."
            }
            Remove-Item $pidFile
        }
        $registered = @(Get-Git @('-C', $paths.Main, 'worktree', 'list', '--porcelain') | Where-Object { $_ -like 'worktree *' } |
            ForEach-Object { ($_.Substring(9)) -replace '/', '\' }) -contains $entry.worktree
        if ($registered) {
            git -C $paths.Main worktree remove --force $entry.worktree
            if ($LASTEXITCODE -ne 0) { Fail "could not remove worktree $($entry.worktree); close programs using it and retry" }
        }
        git -C $paths.Main worktree prune
        if ($entry.worktree -and (Test-Path $entry.worktree)) {
            Write-Host "Leftover folder (no longer a worktree), delete it when nothing holds it: $($entry.worktree)" -ForegroundColor Yellow
        }
        git -C $paths.Main branch -D $entry.branch 2>$null | Out-Null
        Write-Host "Cancelled $task. Worktree and branch removed."
    }

    'merge' {
        $task = $Rest[0]
        if (-not $task) { Fail 'usage: trinity merge <task>' }
        $entry = Get-BoardEntry $paths $task
        if (-not $entry) { Fail "no board entry for $task" }
        $branch = $entry.branch

        $cur = Get-Git @('-C', $paths.Main, 'branch', '--show-current')
        if ($cur -ne 'main') { Fail "main checkout is on '$cur', switch it to main first" }
        $dirty = @(Get-Git @('-C', $paths.Main, 'status', '--porcelain') | Where-Object { $_ })
        if ($dirty.Count -gt 0) { Fail 'main checkout has uncommitted changes; commit or stash them first' }
        $n = [int](Get-Git @('-C', $paths.Main, 'rev-list', '--count', "main..$branch"))
        if ($n -eq 0) { Fail "nothing to merge: $branch has no commits ahead of main" }

        # Codex reviews when it can; without it Claude Code or Antigravity (read-only) does, else tests alone gate.
        $reviewer = Select-Reviewer $entry.agent
        if ($reviewer) {
            Write-Host "Reviewing $branch ($n commits) with $reviewer..."
            $rv = Invoke-Review $reviewer $entry.worktree 'base' 'main' $task
            $review = $rv.Lines
            # A failed review has no findings to parse, so it must not count as a clean one.
            if ($rv.ExitCode -ne 0 -and -not $Force) {
                $review | Select-Object -Last 20 | ForEach-Object { Write-Host $_ }
                Fail "$reviewer review failed (exit $($rv.ExitCode)); rerun, set TRINITY_REVIEWER to another agent, or use -Force"
            }
        }
        else {
            $review = @('No review: no reviewer can run here (TRINITY_REVIEWER=none, or Codex, Claude Code and Antigravity all unavailable). Tests alone gate this merge.')
            Write-Host $review[0] -ForegroundColor Yellow
        }
        [IO.File]::WriteAllText((Join-Path $paths.Out "review-$task.txt"), (($review -join "`r`n") + "`r`n"), $script:Utf8)
        $review | ForEach-Object { Write-Host $_ }

        $blocks = @(Split-Findings $review | Select-Object -Unique)
        $high = @($blocks | Where-Object { $_ -match '\[P[01]\]' })
        $any = $blocks.Count -gt 0

        # Workers never edit shared files, so a finding about them on a branch that leaves them untouched
        # is wiring the orchestrator does after the merge, not a reason to block it.
        $shared = @(Get-SharedFiles $paths.Main)
        if ($high -and $shared) {
            $touched = @(Get-Git @('-C', $paths.Main, 'diff', '--name-only', "main...$branch"))
            $touchesShared = @($touched | Where-Object { $f = $_; $shared | Where-Object { $f -like $_ } })
            if (-not $touchesShared) {
                $deferred = @($high | Where-Object { $b = $_ -replace '\\', '/'; $shared | Where-Object { $b -like "*$_*" } })
                if ($deferred) {
                    $high = @($high | Where-Object { $deferred -notcontains $_ })
                    [IO.File]::WriteAllText((Join-Path $paths.Out "deferred-$task.txt"), (($deferred -join "`r`n`r`n") + "`r`n"), $script:Utf8)
                    foreach ($d in $deferred) { Write-Host "Deferred to orchestrator (shared file): $(($d -split "`n")[0].Trim())" -ForegroundColor Yellow }
                }
            }
        }
        if ($high -and -not $Force) { Fail 'P0/P1 findings; fix on the branch or rerun with -Force' }
        if ($any -and -not $Yes) {
            $ans = Read-Host 'Merge anyway? [y/N]'
            if ($ans -ne 'y') { Fail 'merge cancelled' }
        }

        git -C $paths.Main merge --no-ff --no-commit $branch | Out-Host
        if ($LASTEXITCODE -ne 0) {
            git -C $paths.Main merge --abort
            Fail 'merge conflicts; aborted. Rebase the branch on main and retry.'
        }

        # Test command comes from the "- Test: `...`" line in AGENTS.md.
        $testCmd = $null
        $agentsMd = Join-Path $paths.Main 'AGENTS.md'
        if (Test-Path $agentsMd) {
            foreach ($l in [IO.File]::ReadAllLines($agentsMd)) {
                if ($l -match '^- Test: `(.+)`$') { $testCmd = $Matches[1]; break }
            }
        }
        if ($testCmd -and $testCmd -ne 'TBD') {
            Write-Host "Running tests: $testCmd"
            Push-Location $paths.Main
            cmd.exe /d /c $testCmd
            $rc = $LASTEXITCODE
            Pop-Location
            if ($rc -ne 0) {
                git -C $paths.Main merge --abort
                Fail "tests failed (exit $rc); merge aborted"
            }
        }

        git -C $paths.Main commit -m "Merge $branch" | Out-Host
        if ($LASTEXITCODE -ne 0) { Fail 'merge commit failed' }
        Set-BoardStatus $paths $task 'merged'
        git -C $paths.Main worktree remove $entry.worktree
        if ($LASTEXITCODE -ne 0) {
            Write-Host "Merged $branch into main, but the worktree has leftover files. Inspect, then: git worktree remove --force `"$($entry.worktree)`" and git branch -d $branch" -ForegroundColor Yellow
            exit 1
        }
        git -C $paths.Main branch -d $branch
        Write-Host ''
        Write-Host "Merged $branch into main ($n commits). Worktree and branch removed."
        if ($deferred) { Write-Host "Shared-file wiring left for you: .codex-out\deferred-$task.txt" -ForegroundColor Yellow }
    }

    'init' {
        if (-not $Rest -or -not $Rest[0]) { Fail 'usage: trinity init <path-to-git-repo> [-Force]' }
        $resolved = Resolve-Path $Rest[0] -ErrorAction SilentlyContinue
        if (-not $resolved) { Fail "path not found: $($Rest[0])" }
        $top = Get-Git @('-C', $resolved.Path, 'rev-parse', '--show-toplevel')
        if (-not $top) { Fail "$($resolved.Path) is not a git repo; run git init there first" }
        $target = $top -replace '/', '\'
        $kit = Split-Path -Parent $PSScriptRoot
        if ((Resolve-Path $kit).Path -eq $target) { Fail 'target is the toolkit repo itself' }

        $copy = {
            param([string]$rel)
            $dst = Join-Path $target $rel
            $dir = Split-Path -Parent $dst
            if (-not (Test-Path $dir)) { New-Item -ItemType Directory -Path $dir -Force | Out-Null }
            Copy-Item (Join-Path $kit $rel) $dst -Force
        }

        # Toolkit files are always refreshed, so rerunning init updates an install.
        $toolkit = @('trinity.cmd', 'trinity.sh', 'scripts\trinity.ps1', 'scripts\agent-task.ps1', 'scripts\lib\board.ps1',
            'scripts\codex.cmd', 'scripts\codex-review.cmd', 'scripts\codex-ask.cmd', '.githooks\pre-commit',
            'scripts\dashboard.ps1', 'scripts\dashboard.html',
            '.claude\agents\codex-worker.md', '.claude\agents\codex-reviewer.md', '.claude\agents\antigravity-worker.md',
            '.claude\skills\army\SKILL.md')
        foreach ($rel in $toolkit) {
            if (-not (Test-Path (Join-Path $kit $rel))) { Write-Host "  missing  $rel (not in this toolkit checkout; skipped)"; continue }
            & $copy $rel; Write-Host "  updated  $rel"
        }
        foreach ($rel in @('.claude\agents\antigravity-ui.md')) {
            $old = Join-Path $target $rel
            if (Test-Path $old) { Remove-Item $old; Write-Host "  removed  $rel (retired toolkit file)" }
        }

        # Rule and memory docs belong to the project once written: created when missing, replaced only with -Force.
        $agentsText = [IO.File]::ReadAllText((Join-Path $kit 'AGENTS.md'))
        $blank = "## Project commands`r`n`r`nFill these in when code lands. Every agent runs them before claiming done.`r`n`r`n" +
            "- Install: ``TBD```r`n- Typecheck/build: ``TBD```r`n- Test: ``TBD```r`n- Run: ``TBD```r`n" +
            "- Shared files: ``TBD`` (files only the orchestrator edits, e.g. registries, package.json)`r`n`r`n"
        $agentsText = [regex]::Replace($agentsText, '(?s)## Project commands\r?\n.*?(?=## )', { param($m) $blank })
        $docs = [ordered]@{
            'AGENTS.md'                    = $agentsText
            'CLAUDE.md'                    = $null
            'GEMINI.md'                    = $null
            '.agent\rules\holy-trinity.md' = $null
            '.claude\settings.json'        = $null
            'PLAN.md'                      = "# PLAN.md`r`n`r`nPer-module checklist. Owner = claude | codex | antigravity.`r`n`r`n## Module 1`r`n- [ ] First task - unassigned`r`n"
            'HANDOFF.md'                   = "# HANDOFF.md`r`n`r`nUpdate before you stop. Newest entry on top.`r`n`r`n## Status`r`n- Completed: none`r`n- In flight: none`r`n- Next command: fill in Project commands in AGENTS.md`r`n- Blockers: none`r`n`r`n## From Claude`r`n`r`n## From Codex`r`n`r`n## From Antigravity`r`n"
        }
        foreach ($rel in $docs.Keys) {
            $dst = Join-Path $target $rel
            if ((Test-Path $dst) -and -not $Force) { Write-Host "  kept     $rel (exists; -Force replaces it)"; continue }
            if ($null -eq $docs[$rel]) { & $copy $rel }
            else { [IO.File]::WriteAllText($dst, $docs[$rel], $script:Utf8) }
            Write-Host "  wrote    $rel"
        }
        # A kept settings.json still gets the SessionStart hook: the old cwd-relative command is swapped in place,
        # a missing hook is merged in (original saved to .codex-out\settings.json.bak, since JSON is rewritten).
        $settings = Join-Path $target '.claude\settings.json'
        $kitHook = (ConvertFrom-Json ([IO.File]::ReadAllText((Join-Path $kit '.claude\settings.json')))).hooks.SessionStart[0]
        $hookCmd = $kitHook.hooks[0].command
        $raw = [IO.File]::ReadAllText($settings)
        $oldCmd = 'powershell -NoProfile -ExecutionPolicy Bypass -File scripts/trinity.ps1 status -Brief'
        if ($raw.Contains($oldCmd)) {
            $raw = $raw.Replace($oldCmd, $hookCmd.Replace('\', '\\').Replace('"', '\"'))
            [IO.File]::WriteAllText($settings, $raw, $script:Utf8)
            Write-Host '  updated  .claude\settings.json (SessionStart hook now works from any folder)'
        }
        elseif (-not $raw.Contains('trinity.ps1')) {
            try { $settingsObj = ConvertFrom-Json $raw } catch { $settingsObj = $null }
            if ($null -eq $settingsObj) {
                Write-Host '  NOTE     .claude\settings.json is not valid JSON; add the SessionStart hook from the toolkit by hand' -ForegroundColor Yellow
            }
            else {
                $outDir = Join-Path $target '.codex-out'
                if (-not (Test-Path $outDir)) { New-Item -ItemType Directory -Path $outDir | Out-Null }
                [IO.File]::WriteAllText((Join-Path $outDir 'settings.json.bak'), $raw, $script:Utf8)
                if (-not $settingsObj.PSObject.Properties['hooks']) { $settingsObj | Add-Member -NotePropertyName hooks -NotePropertyValue ([pscustomobject]@{}) }
                if (-not $settingsObj.hooks.PSObject.Properties['SessionStart']) { $settingsObj.hooks | Add-Member -NotePropertyName SessionStart -NotePropertyValue @() }
                $settingsObj.hooks.SessionStart = @($settingsObj.hooks.SessionStart) + $kitHook
                [IO.File]::WriteAllText($settings, ($settingsObj | ConvertTo-Json -Depth 32), $script:Utf8)
                Write-Host '  merged   .claude\settings.json (added trinity SessionStart hook; original in .codex-out\settings.json.bak)'
            }
        }

        # Line-based config files: append only the lines that are missing.
        $append = @{
            '.gitignore'     = @('.codex-out/')
            '.gitattributes' = @('*.cmd text eol=crlf', '*.ps1 text eol=crlf', '.githooks/* text eol=lf', '*.sh text eol=lf')
        }
        foreach ($rel in $append.Keys) {
            $dst = Join-Path $target $rel
            $have = @(if (Test-Path $dst) { [IO.File]::ReadAllLines($dst) })
            $missing = @($append[$rel] | Where-Object { $have -notcontains $_ })
            if ($missing) {
                $prefix = if ($have.Count -and (Get-Content $dst -Raw) -notmatch '\n$') { "`r`n" } else { '' }
                [IO.File]::AppendAllText($dst, $prefix + (($missing -join "`r`n") + "`r`n"), $script:Utf8)
                Write-Host "  appended $rel ($($missing -join ', '))"
            }
        }

        git -C $target config core.hooksPath .githooks
        Register-Repo $target
        $outDir = Join-Path $target '.codex-out'
        if (-not (Test-Path $outDir)) { New-Item -ItemType Directory -Path $outDir | Out-Null }
        Write-Host ''
        Write-Host "Installed trinity into $target. Next: fill in Project commands in AGENTS.md, commit, then open a new Claude session there."
    }

    'ui' {
        Start-Process cmd.exe -ArgumentList '/c', 'start', '""', 'antigravity-ide', "`"$($paths.Main)`"" -WindowStyle Hidden
    }

    'setup' {
        git -C $paths.Main config core.hooksPath .githooks
        if (-not (Test-Path $paths.Out)) { New-Item -ItemType Directory -Path $paths.Out | Out-Null }
        Register-Repo $paths.Main
        Write-Host 'Setup done: core.hooksPath=.githooks, .codex-out ready, repo listed for the dashboard.'
    }

    'track' {
        # Claude's own work on the board, so the dashboard shows the orchestrator next to its workers.
        $task = $Rest[0]
        if (-not $task) { Fail 'usage: trinity track <task> <what you are doing...>   (finish with: trinity done <task> <summary>)' }
        Assert-TaskName $task
        $note = if (@($Rest).Count -gt 1) { (($Rest[1..($Rest.Count - 1)]) -join ' ') -replace '[\r\n]+', ' ' } else { 'in progress' }
        $entry = Get-BoardEntry $paths $task
        if ($entry -and $entry.agent -ne 'claude') { Fail "$task belongs to $($entry.agent); pick another task name" }
        if (-not $entry) { Add-BoardEntry $paths $task 'claude' '-' '-' $Scope }
        # Inside a Claude Code session, link the task to it so the dashboard shows its model and chat.
        if ($env:CLAUDE_CODE_SESSION_ID -match '^[0-9a-fA-F-]{36}$') { Set-BoardField $paths $task 'session' $env:CLAUDE_CODE_SESSION_ID }
        Set-BoardStatus $paths $task "working: $note"
        Write-Host "tracking $($task): $note"
    }

    'dashboard' {
        $script = Join-Path $PSScriptRoot 'dashboard.ps1'
        if (-not (Test-Path $script)) { Fail 'scripts\dashboard.ps1 is missing' }
        if ($Json) { & powershell.exe -NoProfile -ExecutionPolicy Bypass -File $script -Once; exit $LASTEXITCODE }
        $pidFile = Join-Path (Get-TrinityHome) 'dashboard.pid'
        $running = $null
        if (Test-Path $pidFile) {
            $saved = [IO.File]::ReadAllText($pidFile).Trim() -split ' '
            # A saved pid only counts if it is still our server (Windows reuses pids).
            $proc = Get-CimInstance Win32_Process -Filter "ProcessId = $([int]$saved[0])" -ErrorAction SilentlyContinue
            if ($proc -and $proc.CommandLine -like '*dashboard.ps1*') { $running = Get-Process -Id $proc.ProcessId -ErrorAction SilentlyContinue }
            if ($running) { $Port = [int]$saved[1] }
        }
        if ($Stop) {
            if ($running) { Stop-Process -Id $running.Id -Force; Write-Host "Dashboard stopped (pid $($running.Id))." }
            else { Write-Host 'Dashboard is not running.' }
            Remove-Item $pidFile -ErrorAction SilentlyContinue
            exit 0
        }
        if (-not $running) {
            Register-Repo $paths.Main
            $server = Start-Process powershell.exe -WindowStyle Hidden -PassThru -ArgumentList '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', "`"$script`"", '-Port', $Port
            Start-Sleep -Seconds 2
            if ($server.HasExited) { Fail "dashboard server exited (code $($server.ExitCode)); is port $Port busy? Try -Port 7788" }
            [IO.File]::WriteAllText($pidFile, "$($server.Id) $Port", $script:Utf8)
        }
        $url = "http://localhost:$Port/"
        Start-Process cmd.exe -ArgumentList '/c', 'start', '""', $url -WindowStyle Hidden
        Write-Host "Dashboard: $url   (stop with: trinity dashboard -Stop)"
    }

    default { Show-Usage }
}
