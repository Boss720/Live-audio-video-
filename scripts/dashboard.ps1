param(
    [int]$Port = 7777,
    [switch]$Once,
    [string]$Chat = '',
    [string[]]$Repos = @()
)

$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Web.Extensions
$script:JsonSerializer = New-Object System.Web.Script.Serialization.JavaScriptSerializer
$script:JsonSerializer.MaxJsonLength = [int]::MaxValue
$script:TranscriptCache = @{}
$script:TranscriptSummaryCache = @{}
$script:CodexRolloutCache = @{}
$script:CodexSubChatCache = @{}
$script:AgyTaskCache = @{}
$script:AgyModelCache = @{}
$script:ClaudeModelCache = @{}
$script:AvailabilityCache = $null
$script:AvailabilityCachedAt = [datetime]::MinValue
$script:JsonLinesDroppedLong = $false
$script:CodexHome = if ($env:CODEX_HOME) { $env:CODEX_HOME } else { Join-Path $env:USERPROFILE '.codex' }
$script:AgyHome = if ($env:TRINITY_AGY_HOME) { $env:TRINITY_AGY_HOME } else { Join-Path (Join-Path $env:USERPROFILE '.gemini') 'antigravity-cli' }
$script:ClaudeHome = if ($env:CLAUDE_CONFIG_DIR) { $env:CLAUDE_CONFIG_DIR } else { Join-Path $env:USERPROFILE '.claude' }

function Get-JsonValue($Object, [string]$Name, $Default = $null) {
    if ($null -eq $Object) { return $Default }
    if ($Object -is [System.Collections.IDictionary]) { if ($Object.Keys -contains $Name) { return $Object[$Name] }; return $Default }
    $property = $Object.PSObject.Properties[$Name]
    if ($property) { return $property.Value }
    return $Default
}

function Convert-ToIso($Value) {
    if ($null -eq $Value -or [string]::IsNullOrWhiteSpace([string]$Value)) { return '' }
    try { $stamp = [datetime]::Parse([string]$Value); if ($stamp.Kind -ne [DateTimeKind]::Unspecified) { $stamp = $stamp.ToLocalTime() }; return $stamp.ToString("yyyy-MM-dd'T'HH:mm:ss") } catch { return '' }
}

function Convert-UnixToIso($Value) {
    try { return ([datetime]::new(1970, 1, 1).AddSeconds([double]$Value)).ToLocalTime().ToString("yyyy-MM-dd'T'HH:mm:ss") } catch { return '' }
}

function Read-JsonLinesIncremental([string]$Path) {
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) { return @() }
    try {
        $stream = New-Object IO.FileStream($Path, [IO.FileMode]::Open, [IO.FileAccess]::Read, [IO.FileShare]::ReadWrite)
        try {
            $key = [IO.Path]::GetFullPath($Path).ToLowerInvariant()
            $cache = $script:TranscriptCache[$key]
            if (-not $cache -or $stream.Length -lt $cache.offset) { $cache = @{ offset = 0L; pending = '' } }
            [void]$stream.Seek([long]$cache.offset, [IO.SeekOrigin]::Begin)
            $remaining = [int][Math]::Min([int]::MaxValue, $stream.Length - $cache.offset)
            $buffer = New-Object byte[] $remaining
            $read = 0
            while ($read -lt $remaining) { $n = $stream.Read($buffer, $read, $remaining - $read); if ($n -le 0) { break }; $read += $n }
            $cache.offset = $stream.Position
            $text = $cache.pending + [Text.Encoding]::UTF8.GetString($buffer, 0, $read)
            $lines = $text -split "`n"
            $cache.pending = ''
            if (-not $text.EndsWith("`n")) { $cache.pending = $lines[$lines.Count - 1]; if ($lines.Count -gt 1) { $lines = $lines[0..($lines.Count - 2)] } else { $lines = @() } }
            $records = New-Object System.Collections.Generic.List[object]
            foreach ($line in $lines) {
                $line = $line.TrimEnd("`r").TrimStart([char]0xFEFF)
                if (-not $line -or $line.Length -gt 1048576) { continue }
                try { $records.Add($script:JsonSerializer.DeserializeObject($line)) } catch { }
            }
            $script:TranscriptCache[$key] = $cache
            return @($records.ToArray())
        }
        finally { $stream.Dispose() }
    } catch { return @() }
}

function Read-JsonLinesBounded([string]$Path, [int]$MaxBytes = 4194304, [switch]$HeadAndTail) {
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) { return @() }
    try {
        $stream = New-Object IO.FileStream($Path, [IO.FileMode]::Open, [IO.FileAccess]::Read, [IO.FileShare]::ReadWrite)
        try {
            if ($HeadAndTail -and $stream.Length -gt 131072) {
                $half = 65536; $buffers = @()
                foreach ($offset in @(0L, [long]($stream.Length - $half))) { [void]$stream.Seek($offset, [IO.SeekOrigin]::Begin); $buffer = New-Object byte[] $half; $read = $stream.Read($buffer, 0, $half); $buffers += [Text.Encoding]::UTF8.GetString($buffer, 0, $read) }
                $text = $buffers -join "`n"
            } else {
                $count = [int][Math]::Min($stream.Length, $MaxBytes)
                if ($stream.Length -gt $count) { [void]$stream.Seek(-$count, [IO.SeekOrigin]::End) }
                $buffer = New-Object byte[] $count; $read = 0
                while ($read -lt $count) { $n = $stream.Read($buffer, $read, $count - $read); if ($n -le 0) { break }; $read += $n }
                $text = [Text.Encoding]::UTF8.GetString($buffer, 0, $read)
            }
            $records = New-Object System.Collections.Generic.List[object]
            foreach ($line in ($text.TrimStart([char]0xFEFF) -split "`r?`n")) { if ($line.Length -gt 1048576) { $script:JsonLinesDroppedLong = $true; continue }; if ($line.Trim()) { try { $records.Add($script:JsonSerializer.DeserializeObject($line)) } catch { } } }
            return @($records.ToArray())
        } finally { $stream.Dispose() }
    } catch { return @() }
}

function Get-HeadText([string]$Path, [int]$MaxBytes = 8192) {
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) { return '' }
    try {
        $stream = New-Object IO.FileStream($Path, [IO.FileMode]::Open, [IO.FileAccess]::Read, [IO.FileShare]::ReadWrite)
        try { $count=[int][Math]::Min($stream.Length,$MaxBytes); $buffer=New-Object byte[] $count; $read=0; while($read -lt $count){$n=$stream.Read($buffer,$read,$count-$read);if($n -le 0){break};$read+=$n}; return [Text.Encoding]::UTF8.GetString($buffer,0,$read) }
        finally { $stream.Dispose() }
    } catch { return '' }
}

function Get-Redacted([string]$Text) {
    if ($null -eq $Text) { return '' }
    foreach ($pattern in @('sk-[A-Za-z0-9_-]{16,}', 'gh[pousr]_[A-Za-z0-9]{20,}', 'xox[abprs]-[A-Za-z0-9-]{10,}', 'AKIA[0-9A-Z]{16}', 'AIza[0-9A-Za-z_-]{35}', '(?i)bearer\s+[A-Za-z0-9._~+/=-]{20,}')) { $Text = [regex]::Replace($Text, $pattern, '[redacted]') }
    return $Text
}

function Get-ChatKeyKind([string]$Key) {
    if ($Key -match '^codex:[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$') { return 'codex' }
    if ($Key -match '^agy:[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$') { return 'agy' }
    if ($Key -match '^claude:[A-Za-z0-9-]{1,200}:[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}(:[A-Za-z0-9_-]{1,64})?$') { return 'claude' }
    return ''
}

function Get-CodexRollout([string]$SessionId, [string]$Started) {
    if (-not $SessionId) { return '' }
    if ($script:CodexRolloutCache.ContainsKey($SessionId) -and $script:CodexRolloutCache[$SessionId]) { return $script:CodexRolloutCache[$SessionId] }
    $sessions = Join-Path $script:CodexHome 'sessions'
    $folders = New-Object System.Collections.Generic.List[string]
    try { $date = [datetime]::Parse($Started); for ($i = -1; $i -le 1; $i++) { $folders.Add((Join-Path $sessions $date.AddDays($i).ToString('yyyy\\MM\\dd'))) } } catch { }
    foreach ($folder in $folders) {
        if (Test-Path -LiteralPath $folder -PathType Container) {
            $file = Get-ChildItem -LiteralPath $folder -Filter "rollout-*-$SessionId.jsonl" -File -ErrorAction SilentlyContinue | Select-Object -First 1
            if ($file) { $script:CodexRolloutCache[$SessionId] = $file.FullName; return $file.FullName }
        }
    }
    $searched = 0; $file = $null
    if (Test-Path -LiteralPath $sessions -PathType Container) {
        foreach ($year in (Get-ChildItem -LiteralPath $sessions -Directory -ErrorAction SilentlyContinue | Sort-Object Name -Descending)) {
            foreach ($month in (Get-ChildItem -LiteralPath $year.FullName -Directory -ErrorAction SilentlyContinue | Sort-Object Name -Descending)) {
                foreach ($day in (Get-ChildItem -LiteralPath $month.FullName -Directory -ErrorAction SilentlyContinue | Sort-Object Name -Descending)) {
                    $searched++; $file = Get-ChildItem -LiteralPath $day.FullName -Filter "rollout-*-$SessionId.jsonl" -File -ErrorAction SilentlyContinue | Select-Object -First 1
                    if ($file -or $searched -ge 365) { break }
                }
                if ($file -or $searched -ge 365) { break }
            }
            if ($file -or $searched -ge 365) { break }
        }
    }
    $path = if ($file) { $file.FullName } else { '' }; $script:CodexRolloutCache[$SessionId] = $path; return $path
}

function Get-RelativeTouchedPath([string]$Path, [string]$Worktree) {
    if (-not $Path) { return '' }
    $pathText = $Path.Replace('\', '/')
    # A patch quoted inside another file's content leaves fragments such as "tests/"; a file path never ends in a slash.
    if ($pathText.EndsWith('/')) { return '' }
    if ($Worktree -and $Worktree -ne '-') {
        try {
            $root = [IO.Path]::GetFullPath($Worktree).TrimEnd('\', '/').Replace('\', '/')
            if ($pathText.StartsWith($root + '/', [StringComparison]::OrdinalIgnoreCase)) { return $pathText.Substring($root.Length + 1) }
        } catch { }
    }
    return $pathText
}

function Get-AgyTaskSession([string]$OutDir, [string]$Task) {
    $taskPath = [IO.Path]::GetFullPath((Join-Path $OutDir ("task-{0}.md" -f $Task))).Replace('\', '/').ToLowerInvariant()
    $cached = $script:AgyTaskCache[$taskPath]
    if ($cached -and $cached.until -gt (Get-Date)) { return $cached }
    $found = @{ session=''; model=''; until=(Get-Date).AddSeconds(60) }
    $brain = Join-Path $script:AgyHome 'brain'
    if (Test-Path -LiteralPath $brain -PathType Container) {
        foreach ($folder in (Get-ChildItem -LiteralPath $brain -Directory -ErrorAction SilentlyContinue | Sort-Object LastWriteTime -Descending | Select-Object -First 200)) {
            if ($folder.Name -notmatch '^[0-9a-fA-F]{8}(-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}$') { continue }
            $file = Join-Path $folder.FullName '.system_generated\logs\transcript.jsonl'
            $head = Get-HeadText $file 16384
            foreach ($line in ($head -split "`n")) {
                if ($line -notmatch '"type"\s*:\s*"USER_INPUT"') { continue }
                try { $record = $script:JsonSerializer.DeserializeObject($line.TrimEnd("`r").TrimStart([char]0xFEFF)); $content = [string](Get-JsonValue $record 'content' '') } catch { continue }
                try { $content = [Uri]::UnescapeDataString($content) } catch { break }
                if ($content -notmatch '(?i)Read the task file\s+[`"'']?(?:file:///)?(.+?task-[^\s`"'']+\.md)') { break }
                $mentioned = $Matches[1].Replace('\', '/').ToLowerInvariant()
                if ($mentioned -eq $taskPath) {
                    $found.session = $folder.Name
                    if ($content -match '(?i)setting `Model Selection` from .+? to (.+?)\.(?:\s|$)') { $found.model = $Matches[1].Trim() }
                }
                break
            }
            if ($found.session) { break }
        }
    }
    # A found conversation never changes; only misses expire, so a task started later is still picked up.
    if ($found.session) { $found.until = [datetime]::MaxValue }
    $script:AgyTaskCache[$taskPath] = $found
    return $found
}

function Get-AgyModelBySession([string]$Session) {
    if ($script:AgyModelCache.ContainsKey($Session)) { return $script:AgyModelCache[$Session] }
    $model = ''; $logDir = Join-Path $script:AgyHome 'log'
    if (Test-Path -LiteralPath $logDir -PathType Container) {
        foreach ($file in (Get-ChildItem -LiteralPath $logDir -Filter 'cli-*.log' -File -ErrorAction SilentlyContinue | Sort-Object LastWriteTime -Descending | Select-Object -First 50)) {
            $head = Get-HeadText $file.FullName 262144
            if ($head -match [regex]::Escape($Session) -and $head -match 'label="([^"]+)"') { $model = $Matches[1]; break }
        }
    }
    $script:AgyModelCache[$Session] = $model
    return $model
}

function Get-CodexData([string]$OutDir, [string]$Task, $Meta, [bool]$Running, [string]$Worktree) {
    $logPath = Join-Path $OutDir ("log-{0}.txt" -f $Task)
    $head = Get-HeadText $logPath 8192
    $model = ''; $effort = ''; $sessionId = ''
    if ($head -match '(?im)^model:\s*(.+)$') { $model = $Matches[1].Trim() }
    if ($head -match '(?im)^reasoning effort:\s*(.*)$') { $effort = $Matches[1].Trim() }
    if ($head -match '(?im)^session id:\s*([0-9a-f-]{36})\s*$') { $sessionId = $Matches[1] }
    if (-not $sessionId) { $sessionId = [string](Get-JsonValue $Meta 'sessionId' '') }
    $started = [string](Get-JsonValue $Meta 'started' '')
    $rollout = Get-CodexRollout $sessionId $started
    $cacheKey = if ($rollout) { [IO.Path]::GetFullPath($rollout).ToLowerInvariant() } else { '' }
    $records = if ($rollout) { Read-JsonLinesIncremental $rollout } else { @() }
    $summary = if ($cacheKey -and $script:TranscriptSummaryCache.ContainsKey($cacheKey)) { $script:TranscriptSummaryCache[$cacheKey] } else { @{ tokens=$null; context=$null; limits=@(); toolCalls=0; lastActivity=''; contextUsed=0L; steps=0; thinkingSteps=0; toolCounts=@{}; lastTool=''; filesTouched=@() } }
    $tokens = $summary.tokens; $context = $summary.context; $limits = @($summary.limits); $toolCalls = [int]$summary.toolCalls; $lastActivity = [string]$summary.lastActivity
    $contextUsed = [long]$summary.contextUsed; $steps = [int]$summary.steps; $thinkingSteps = [int]$summary.thinkingSteps; $toolCounts = $summary.toolCounts; $lastTool = [string]$summary.lastTool; $filesTouched = New-Object System.Collections.Generic.List[string]; foreach ($path in $summary.filesTouched) { $filesTouched.Add($path) }
    foreach ($record in $records) {
        $stamp = Convert-ToIso (Get-JsonValue $record 'timestamp' '')
        if ($stamp) { $lastActivity = $stamp }
        $type = [string](Get-JsonValue $record 'type' '')
        $payload = Get-JsonValue $record 'payload'
        $payloadType = [string](Get-JsonValue $payload 'type' '')
        if ($type -eq 'response_item' -and $payloadType -eq 'reasoning') { $thinkingSteps++ }
        if ($type -eq 'response_item' -and $payloadType -in @('custom_tool_call', 'function_call')) {
            $toolCalls++; $name = [string](Get-JsonValue $payload 'name' ''); $lastTool = $name
            if ($name) { if (-not $toolCounts.ContainsKey($name)) { $toolCounts[$name] = 0 }; $toolCounts[$name]++ }
            # apply_patch runs directly or inside an exec script, where the patch is a JS string literal (newlines as \n, backslashes doubled).
            $inputText = [string](Get-JsonValue $payload 'input' (Get-JsonValue $payload 'arguments' ''))
            # Headers start a line; quoted headers in added lines start with "+". A direct patch has real newlines and plain paths,
            # an exec-embedded one is a JS string: escaped newlines (not doubly escaped ones) and doubled backslashes, or a template literal with real newlines.
            $embedded = $name -ne 'apply_patch'
            if ($inputText.Contains('*** Begin Patch') -and (-not $embedded -or $inputText.Contains('apply_patch('))) {
                $headerPattern = if ($embedded) { '(?:(?<!\\)\\n|\n)\*\*\* (?:Add|Update|Delete) File: (.+?)\s*(?=(?<!\\)\\[nr]|["`]|\r?\n|$)' } else { '(?m)^\*\*\* (?:Add|Update|Delete) File: (.+?)\s*$' }
                foreach ($match in [regex]::Matches($inputText, $headerPattern)) {
                    $path = $match.Groups[1].Value.Trim(); if ($embedded) { $path = $path.Replace('\\', '\') }
                    $path = Get-RelativeTouchedPath $path $Worktree
                    if ($path -and $filesTouched.Count -lt 20 -and -not $filesTouched.Contains($path)) { $filesTouched.Add($path) }
                }
            }
        }
        if ($type -eq 'event_msg' -and $payloadType -eq 'token_count') {
            $info = Get-JsonValue $payload 'info'; $usage = Get-JsonValue $info 'total_token_usage'
            if ($info) { $steps++; $contextUsed = [long](Get-JsonValue (Get-JsonValue $info 'last_token_usage') 'input_tokens' 0) }
            if ($usage) { $tokens = [ordered]@{ input = [long](Get-JsonValue $usage 'input_tokens' 0); cached = [long](Get-JsonValue $usage 'cached_input_tokens' 0); output = [long](Get-JsonValue $usage 'output_tokens' 0); reasoning = [long](Get-JsonValue $usage 'reasoning_output_tokens' 0); total = [long](Get-JsonValue $usage 'total_tokens' 0) } }
            $context = Get-JsonValue $info 'model_context_window' $context
            $rate = Get-JsonValue $payload 'rate_limits'
            $limits = @()
            foreach ($name in @('primary', 'secondary')) { $lim = Get-JsonValue $rate $name; if ($lim) { $limits += [pscustomobject]@{ name = $name; usedPercent = Get-JsonValue $lim 'used_percent' 0; windowMinutes = Get-JsonValue $lim 'window_minutes' 0; resetsAt = Convert-UnixToIso (Get-JsonValue $lim 'resets_at' 0) } } }
        }
    }
    if ($cacheKey) { $script:TranscriptSummaryCache[$cacheKey] = @{ tokens=$tokens; context=$context; limits=@($limits); toolCalls=$toolCalls; lastActivity=$lastActivity; contextUsed=$contextUsed; steps=$steps; thinkingSteps=$thinkingSteps; toolCounts=$toolCounts; lastTool=$lastTool; filesTouched=@($filesTouched.ToArray()) } }
    if ($Meta) { $model = [string](Get-JsonValue $Meta 'model' $model); $effort = [string](Get-JsonValue $Meta 'effort' $effort); $sessionId = [string](Get-JsonValue $Meta 'sessionId' $sessionId) }
    if (-not $Running -and (Get-JsonValue $Meta 'tokens')) { $tokens = Get-JsonValue $Meta 'tokens' }
    if (-not $lastActivity -and $rollout) { try { $lastActivity = (Get-Item -LiteralPath $rollout).LastWriteTime.ToString("yyyy-MM-dd'T'HH:mm:ss") } catch { } }
    $chat = if ($rollout) { "codex:$sessionId" } else { '' }
    $subs = New-Object System.Collections.Generic.List[object]
    # A running task can still spawn subagents, so only a finished task's list is served from the cache.
    if ($sessionId -and $rollout -and -not $Running -and $script:CodexSubChatCache.ContainsKey($sessionId)) { foreach ($sub in $script:CodexSubChatCache[$sessionId]) { $subs.Add($sub) } }
    elseif ($sessionId -and $rollout) {
        # Children land in the parent's day folder or a later one (a run can cross midnight).
        $folder = Split-Path $rollout -Parent; $folders = @($folder)
        try { $day = [datetime]::ParseExact(($folder.Substring($folder.Length - 10) -replace '[\\/]', '-'), 'yyyy-MM-dd', $null); $root = Split-Path (Split-Path (Split-Path $folder -Parent) -Parent) -Parent; $folders += @(1, 2 | ForEach-Object { Join-Path $root $day.AddDays($_).ToString('yyyy\\MM\\dd') }) } catch { }
        $pattern = '"parent_thread_id"\s*:\s*"' + [regex]::Escape($sessionId) + '"'
        foreach ($dir in $folders) {
            if (-not (Test-Path -LiteralPath $dir -PathType Container)) { continue }
            foreach ($file in (Get-ChildItem -LiteralPath $dir -Filter "rollout-*.jsonl" -File -ErrorAction SilentlyContinue)) {
                if ($file.FullName -eq $rollout -or $file.BaseName -notmatch '([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})$') { continue }
                $childId = $Matches[1]
                # session_meta is the first line; in a child rollout payload.session_id is the PARENT's id, the file name holds the child's own.
                $head = Get-TextLines $file.FullName 8
                if ($head -notmatch $pattern) { continue }
                $title = 'subagent'; if ($head -match '"subagent"\s*:\s*(?:\{\s*"[^"]+"\s*:\s*)?"([^"]+)"') { $title = $Matches[1] }
                $subModel = ''; if ($head -match '"model"\s*:\s*"([^"]+)"') { $subModel = $Matches[1] }
                $subs.Add([pscustomobject]@{ chat = "codex:$childId"; title = $title; model = $subModel })
            }
        }
        $script:CodexSubChatCache[$sessionId] = @($subs.ToArray())
    }
    $breakdown = @($toolCounts.GetEnumerator() | Sort-Object @{ Expression = { $_.Value }; Descending = $true }, @{ Expression = { $_.Key } } | Select-Object -First 8 | ForEach-Object { [pscustomobject]@{ name=$_.Key; count=$_.Value } })
    return @{ model = $model; effort = $effort; sessionId = $sessionId; tokens = $tokens; contextWindow = $context; toolCalls = $toolCalls; rateLimits = @($limits); lastActivity = $lastActivity; chat = $chat; subChats = @($subs.ToArray()); rollout = $rollout; records = $records; contextUsed=$contextUsed; steps=$steps; thinkingSteps=$thinkingSteps; toolBreakdown=$breakdown; lastTool=$lastTool; filesTouched=@($filesTouched.ToArray()); backgroundTasks=$subs.Count }
}

function Get-AgyData([string]$OutDir, [string]$Task, $Meta, [bool]$Running, [string]$Worktree) {
    $log = Join-Path $OutDir ("agy-{0}.log" -f $Task); $text = Get-TextLines $log 100
    $model = ''; $session = ''
    if ($text -match 'label="([^"]+)"') { $model = $Matches[1] }
    if ($text -match 'cascade_id:\s*([0-9a-f-]{36})') { $session = $Matches[1] }
    # The runner records the session in meta too; it covers a missing or rotated log.
    if (-not $session) { $metaSession = [string](Get-JsonValue $Meta 'sessionId' ''); if ($metaSession -match '^[0-9a-f-]{36}$') { $session = $metaSession } }
    if (-not $session) { $found = Get-AgyTaskSession $OutDir $Task; $session = $found.session; if (-not $model) { $model = $found.model } }
    if ($session -and -not $model) { $model = Get-AgyModelBySession $session }
    $transcript = if ($session) { Join-Path $script:AgyHome ("brain\{0}\.system_generated\logs\transcript.jsonl" -f $session) } else { '' }
    $cacheKey = if ($transcript) { [IO.Path]::GetFullPath($transcript).ToLowerInvariant() } else { '' }
    $records = if ($transcript) { Read-JsonLinesIncremental $transcript } else { @() }
    $summary = if ($cacheKey -and $script:TranscriptSummaryCache.ContainsKey($cacheKey)) { $script:TranscriptSummaryCache[$cacheKey] } else { @{ tokens=[ordered]@{ input=0L; cached=0L; output=0L; reasoning=0L; total=0L }; toolCalls=0; last=''; first=''; contextUsed=0L; steps=0; thinkingSteps=0; toolCounts=@{}; lastTool=''; filesTouched=@(); backgroundTasks=0; backgroundIds=@{} } }
    $tokens = $summary.tokens; $toolCalls = [int]$summary.toolCalls; $last = [string]$summary.last
    $first = [string]$summary.first; $contextUsed = [long]$summary.contextUsed; $steps = [int]$summary.steps; $thinkingSteps = [int]$summary.thinkingSteps; $toolCounts = $summary.toolCounts; $lastTool = [string]$summary.lastTool; $backgroundIds = $summary.backgroundIds; if ($null -eq $backgroundIds) { $backgroundIds = @{} }; $filesTouched = New-Object System.Collections.Generic.List[string]; foreach ($path in $summary.filesTouched) { $filesTouched.Add($path) }
    foreach ($r in $records) {
        $stamp = Convert-ToIso (Get-JsonValue $r 'created_at' ''); if ($stamp) { if (-not $first) { $first = $stamp }; $last = $stamp }
        if ((Get-JsonValue $r 'type') -eq 'PLANNER_RESPONSE') {
            $steps++; $contextUsed = [long](Get-JsonValue $r 'input_tokens' 0) + [long](Get-JsonValue $r 'cache_read_tokens' 0)
            if ([string](Get-JsonValue $r 'thinking' '')) { $thinkingSteps++ }
            $tokens.input += [long](Get-JsonValue $r 'input_tokens' 0); $tokens.cached += [long](Get-JsonValue $r 'cache_read_tokens' 0); $tokens.output += [long](Get-JsonValue $r 'output_tokens' 0)
            foreach ($call in @(Get-JsonValue $r 'tool_calls' @())) {
                $toolCalls++; $name = [string](Get-JsonValue $call 'name' ''); $lastTool = $name
                if ($name) { if (-not $toolCounts.ContainsKey($name)) { $toolCounts[$name] = 0 }; $toolCounts[$name]++ }
                # manage_task is also called to poll or kill a task, so count distinct task ids.
                if ($name -eq 'manage_task') { $taskArgs = Get-JsonValue $call 'args'; if ($taskArgs -is [string]) { try { $taskArgs = $script:JsonSerializer.DeserializeObject($taskArgs) } catch { $taskArgs = $null } }; $taskId = [string](Get-JsonValue $taskArgs 'TaskId' ''); if ($taskId.StartsWith('"')) { try { $taskId = [string]$script:JsonSerializer.DeserializeObject($taskId) } catch { } }; if (-not $taskId) { $taskId = 'call-' + $toolCalls }; $backgroundIds[$taskId] = $true }
                if ($name -in @('write_file', 'write_to_file', 'edit_file', 'replace_file_content', 'multi_replace_file_content')) {
                    $callArgs = Get-JsonValue $call 'args'; if ($callArgs -is [string]) { try { $callArgs = $script:JsonSerializer.DeserializeObject($callArgs) } catch { $callArgs = $null } }
                    $path = [string](Get-JsonValue $callArgs 'Path' (Get-JsonValue $callArgs 'TargetFile' ''))
                    # agy stores each argument value JSON-encoded, quotes included.
                    if ($path.StartsWith('"')) { try { $path = [string]$script:JsonSerializer.DeserializeObject($path) } catch { } }
                    $path = Get-RelativeTouchedPath $path $Worktree
                    if ($path -and $filesTouched.Count -lt 20 -and -not $filesTouched.Contains($path)) { $filesTouched.Add($path) }
                }
            }
        }
    }
    $tokens.total = $tokens.input + $tokens.output
    if ($cacheKey) { $script:TranscriptSummaryCache[$cacheKey] = @{ tokens=$tokens; toolCalls=$toolCalls; last=$last; first=$first; contextUsed=$contextUsed; steps=$steps; thinkingSteps=$thinkingSteps; toolCounts=$toolCounts; lastTool=$lastTool; filesTouched=@($filesTouched.ToArray()); backgroundIds=$backgroundIds } }
    if (-not $last -and $transcript -and (Test-Path -LiteralPath $transcript)) { try { $last = (Get-Item -LiteralPath $transcript).LastWriteTime.ToString("yyyy-MM-dd'T'HH:mm:ss") } catch { } }
    if ($Meta) { $model = [string](Get-JsonValue $Meta 'model' $model); if (-not $Running -and (Get-JsonValue $Meta 'tokens')) { $tokens = Get-JsonValue $Meta 'tokens' } }
    $agyHasTokens = ($transcript -and (Test-Path -LiteralPath $transcript)) -or (Get-JsonValue $Meta 'tokens')
    $breakdown = @($toolCounts.GetEnumerator() | Sort-Object @{ Expression = { $_.Value }; Descending = $true }, @{ Expression = { $_.Key } } | Select-Object -First 8 | ForEach-Object { [pscustomobject]@{ name=$_.Key; count=$_.Value } })
    return @{ model = $model; effort = [string](Get-JsonValue $Meta 'effort' ''); sessionId = [string](Get-JsonValue $Meta 'sessionId' $session); tokens = $(if ($agyHasTokens) { $tokens } else { $null }); contextWindow = $null; toolCalls = $toolCalls; rateLimits = @(); lastActivity = $last; chat = $(if ($transcript -and (Test-Path -LiteralPath $transcript)) { 'agy:' + $session } else { '' }); subChats = @(); rollout = $transcript; records = $records; started=$first; finished=$(if ($Running) { '' } else { $last }); contextUsed=$contextUsed; steps=$steps; thinkingSteps=$thinkingSteps; toolBreakdown=$breakdown; lastTool=$lastTool; filesTouched=@($filesTouched.ToArray()); backgroundTasks=$backgroundIds.Count }
}

function Get-ClaudeProjectSlug([string]$Path) { return [regex]::Replace([IO.Path]::GetFullPath($Path), '[^A-Za-z0-9]', '-') }

function Get-ClaudeSummary([string]$Path, [string]$Slug, [string]$SessionId, [string]$TaskName = '', [switch]$NoSubagents) {
    $records = Read-JsonLinesBounded $Path 131072 -HeadAndTail
    $title = ''; $model = ''; $effort = ''; $branch = ''; $cwd = ''; $started = ''; $last = ''; $context = 0L; $seenMessage = @{}
    foreach ($r in $records) {
        $type = [string](Get-JsonValue $r 'type' '')
        $stamp = Convert-ToIso (Get-JsonValue $r 'timestamp' '')
        if ($stamp) { if (-not $started) { $started = $stamp }; $last = $stamp }
        $branch = [string](Get-JsonValue $r 'gitBranch' $branch); $cwd = [string](Get-JsonValue $r 'cwd' $cwd)
        if ($type -eq 'custom-title') { $title = [string](Get-JsonValue $r 'customTitle' $title) }
        elseif ($type -eq 'user' -and -not (Get-JsonValue $r 'isMeta' $false) -and -not $title) {
            $content = Get-JsonValue (Get-JsonValue $r 'message') 'content'
            if ($content -is [string]) { $title = [regex]::Replace($content, '<system-reminder>.*?</system-reminder>', '', 'Singleline') }
            elseif ($content) { $title = (@($content | ForEach-Object { if ((Get-JsonValue $_ 'type') -eq 'text') { Get-JsonValue $_ 'text' } }) -join ' ') }
        }
        elseif ($type -eq 'assistant') {
            $message = Get-JsonValue $r 'message'; $msgModel = [string](Get-JsonValue $message 'model' '')
            if ($msgModel -and $msgModel -ne '<synthetic>') { $model = $msgModel }
            $effort = [string](Get-JsonValue $r 'effort' $effort)
            $msgId = [string](Get-JsonValue $message 'id' '')
            if ($msgId -and -not $seenMessage.ContainsKey($msgId)) { $seenMessage[$msgId] = $true; $usage = Get-JsonValue $message 'usage'; $context = [long](Get-JsonValue $usage 'input_tokens' 0) + [long](Get-JsonValue $usage 'cache_creation_input_tokens' 0) + [long](Get-JsonValue $usage 'cache_read_input_tokens' 0) }
        }
    }
    # One big tool result can fill the whole tail window; keep the last model and context seen instead of blanking them.
    $modelKey = $Path.ToLowerInvariant()
    if ($model) { $script:ClaudeModelCache[$modelKey] = @{ model = $model; effort = $effort; context = $context } }
    elseif ($script:ClaudeModelCache.ContainsKey($modelKey)) { $known = $script:ClaudeModelCache[$modelKey]; $model = $known.model; if (-not $effort) { $effort = $known.effort }; if (-not $context) { $context = $known.context } }
    $title = (($title -split "`r?`n", 2)[0]).Trim()
    if ($title.Length -gt 100) { $title = $title.Substring(0, 100) }
    if (-not $last) { try { $last = (Get-Item -LiteralPath $Path).LastWriteTime.ToString("yyyy-MM-dd'T'HH:mm:ss") } catch { } }
    $subagents = New-Object System.Collections.Generic.List[object]
    $subDir = Join-Path (Join-Path (Join-Path $script:ClaudeHome 'projects') $Slug) (Join-Path $SessionId 'subagents')
    if (-not $NoSubagents -and (Test-Path -LiteralPath $subDir -PathType Container)) {
        foreach ($file in (Get-ChildItem -LiteralPath $subDir -Filter 'agent-*.jsonl' -File -ErrorAction SilentlyContinue | Sort-Object LastWriteTime -Descending | Select-Object -First 20)) {
            $id = [IO.Path]::GetFileNameWithoutExtension($file.Name) -replace '^agent-', ''
            $metaPath = Join-Path $subDir ("agent-{0}.meta.json" -f $id); $meta = $null
            if (Test-Path -LiteralPath $metaPath) { try { $meta = $script:JsonSerializer.DeserializeObject([IO.File]::ReadAllText($metaPath)) } catch { } }
            $sub = Get-ClaudeSummary $file.FullName $Slug $SessionId '' -NoSubagents
            $finished = [bool](Get-JsonValue $meta 'finished' $false)
            $subagents.Add([pscustomobject]@{ id = $id; description = [string](Get-JsonValue $meta 'description' ''); type = [string](Get-JsonValue $meta 'agentType' ''); model = [string](Get-JsonValue $meta 'model' $sub.model); finished = $finished; active = (-not $finished -and $file.LastWriteTime -gt (Get-Date).AddSeconds(-120)); lastActivity = $sub.lastActivity; chat = "claude:${Slug}:${SessionId}:$id" })
        }
    }
    return @{ id = $SessionId; title = $title; model = $model; effort = $effort; branch = $branch; cwd = $cwd; started = $started; lastActivity = $last; active = ((Get-Item -LiteralPath $Path).LastWriteTime -gt (Get-Date).AddSeconds(-120)); contextTokens = $context; task = $TaskName; chat = "claude:${Slug}:${SessionId}"; subagents = @($subagents.ToArray()) }
}

function Get-ClaudeSessions([string]$Main, $Tasks) {
    $sessions = New-Object System.Collections.Generic.List[object]
    $slugs = New-Object System.Collections.Generic.List[string]
    $slugs.Add((Get-ClaudeProjectSlug $Main))
    foreach ($task in $Tasks) { if ($task.agent -eq 'claude' -and $task.worktree -and $task.worktree -ne '-') { $slugs.Add((Get-ClaudeProjectSlug $task.worktree)) } }
    foreach ($slug in ($slugs | Select-Object -Unique)) {
        $folder = Join-Path (Join-Path $script:ClaudeHome 'projects') $slug
        if (-not (Test-Path -LiteralPath $folder -PathType Container)) { continue }
        foreach ($file in (Get-ChildItem -LiteralPath $folder -Filter '*.jsonl' -File -ErrorAction SilentlyContinue | Where-Object { $_.LastWriteTime -gt (Get-Date).AddDays(-7) } | Sort-Object LastWriteTime -Descending | Select-Object -First 15)) {
            $id = [IO.Path]::GetFileNameWithoutExtension($file.Name)
            $linked = @($Tasks | Where-Object { $_.sessionId -eq $id } | Select-Object -First 1)
            $taskName = if ($linked.Count) { $linked[0].task } else { '' }
            $summary = Get-ClaudeSummary $file.FullName $slug $id $taskName
            $sessions.Add([pscustomobject]$summary)
        }
    }
    return @($sessions | Sort-Object { $_.lastActivity } -Descending | Select-Object -First 5)
}

function Resolve-Chat([string]$Key) {
    $kind = Get-ChatKeyKind $Key
    if (-not $kind) { return @{ status = 400; value = $null } }
    $file = ''; $title = ''; $model = ''; $id = $Key
    if ($kind -eq 'codex') { $session = $Key.Substring(6); $file = Get-CodexRollout $session ''; $title = $session }
    elseif ($kind -eq 'agy') {
        $session = $Key.Substring(4); $file = Join-Path $script:AgyHome ("brain\{0}\.system_generated\logs\transcript.jsonl" -f $session); $title = $session
    }
    else {
        $parts = $Key -split ':'; $slug = $parts[1]; $session = $parts[2]; $agentId = if ($parts.Count -eq 4) { $parts[3] } else { '' }
        $base = Join-Path (Join-Path $script:ClaudeHome 'projects') $slug
        if ($agentId) { $file = Join-Path $base (Join-Path $session (Join-Path 'subagents' ("agent-{0}.jsonl" -f $agentId))) }
        else { $file = Join-Path $base ("{0}.jsonl" -f $session) }
        if ($agentId) { $metaPath = Join-Path $base (Join-Path $session (Join-Path 'subagents' ("agent-{0}.meta.json" -f $agentId))); if (Test-Path -LiteralPath $metaPath) { try { $meta = $script:JsonSerializer.DeserializeObject([IO.File]::ReadAllText($metaPath)); $title = [string](Get-JsonValue $meta 'description' ''); $model = [string](Get-JsonValue $meta 'model' '') } catch { } } }
        if (-not $agentId -and (Test-Path -LiteralPath $file -PathType Leaf)) { $summary = Get-ClaudeSummary $file $slug $session '' -NoSubagents; $title = $summary.title; $model = $summary.model }
    }
    if (-not $file -or -not (Test-Path -LiteralPath $file -PathType Leaf)) { return @{ status = 404; value = $null } }
    if ($kind -in @('codex','agy')) { $taskInfo = Get-ChatTaskInfo $kind $session; $title = $taskInfo.title; $model = $taskInfo.model; if (-not $title) { $title = $session } }
    $script:JsonLinesDroppedLong = $false
    $records = Read-JsonLinesBounded $file 4194304
    $messages = New-Object System.Collections.Generic.List[object]; $truncated = ((Get-Item -LiteralPath $file).Length -gt 4194304) -or $script:JsonLinesDroppedLong; $updated = ''
    foreach ($r in $records) {
        $stamp = Convert-ToIso (Get-JsonValue $r 'timestamp' (Get-JsonValue $r 'created_at' '')); $type = [string](Get-JsonValue $r 'type' '')
        if ($kind -eq 'codex') {
            $p = Get-JsonValue $r 'payload'; $pt = [string](Get-JsonValue $p 'type' '')
            if ($type -eq 'response_item' -and $pt -eq 'message') { $role = [string](Get-JsonValue $p 'role' ''); if ($role -ne 'developer') { foreach ($c in (Get-JsonValue $p 'content' @())) { if ((Get-JsonValue $c 'type') -in @('input_text','output_text')) { $messages.Add([pscustomobject]@{ at=$stamp; role=$(if ($role -eq 'user') {'user'} else {'assistant'}); name=''; text=(Get-JsonValue $c 'text' '') }) } } } }
            elseif ($type -eq 'response_item' -and $pt -in @('custom_tool_call','function_call')) { $messages.Add([pscustomobject]@{ at=$stamp; role='tool'; name=(Get-JsonValue $p 'name' ''); text=(Get-JsonValue $p 'input' (Get-JsonValue $p 'arguments' '')) | ConvertTo-Json -Compress -Depth 8 }) }
            elseif ($type -eq 'response_item' -and $pt -in @('custom_tool_call_output','function_call_output')) { $out = Get-JsonValue $p 'output' ''; if ($out -isnot [string]) { $out = $out | ConvertTo-Json -Compress -Depth 8 }; $messages.Add([pscustomobject]@{ at=$stamp; role='result'; name=''; text=$out }) }
            elseif ($type -eq 'response_item' -and $pt -eq 'reasoning') { $sum = @((Get-JsonValue $p 'summary' @()) | ForEach-Object { Get-JsonValue $_ 'text' }) -join "`n"; if ($sum) { $messages.Add([pscustomobject]@{ at=$stamp; role='thinking'; name=''; text=$sum }) } }
        }
        elseif ($kind -eq 'agy') {
            switch ($type) {
                USER_INPUT { $messages.Add([pscustomobject]@{ at=$stamp; role='user'; name=''; text=(Get-JsonValue $r 'content' '') }) }
                PLANNER_RESPONSE { $thinking = Get-JsonValue $r 'thinking' ''; if ($thinking) { $messages.Add([pscustomobject]@{ at=$stamp; role='thinking'; name=''; text=$thinking }) }; $content = Get-JsonValue $r 'content' ''; if ($content) { $messages.Add([pscustomobject]@{ at=$stamp; role='assistant'; name=''; text=$content }) }; foreach ($tool in (Get-JsonValue $r 'tool_calls' @())) { $messages.Add([pscustomobject]@{ at=$stamp; role='tool'; name=(Get-JsonValue $tool 'name' ''); text=(Get-JsonValue $tool 'args' @{}) | ConvertTo-Json -Compress -Depth 8 }) } }
                GENERIC { $messages.Add([pscustomobject]@{ at=$stamp; role='result'; name=''; text=(Get-JsonValue $r 'content' '') }) }
                SYSTEM_MESSAGE { $messages.Add([pscustomobject]@{ at=$stamp; role='system'; name=''; text=(Get-JsonValue $r 'content' '') }) }
            }
        }
        else {
            if ($type -eq 'user' -and -not (Get-JsonValue $r 'isMeta' $false)) {
                $content = Get-JsonValue (Get-JsonValue $r 'message') 'content'
                if ($content -is [string]) { $content = [regex]::Replace($content, '<system-reminder>.*?</system-reminder>', '', 'Singleline'); if ($content.Trim()) { $messages.Add([pscustomobject]@{ at=$stamp; role='user'; name=''; text=$content }) } }
                elseif ($content) { foreach ($part in $content) { if ((Get-JsonValue $part 'type') -eq 'text') { $body=[regex]::Replace([string](Get-JsonValue $part 'text' ''), '<system-reminder>.*?</system-reminder>', '', 'Singleline'); if ($body.Trim()) { $messages.Add([pscustomobject]@{ at=$stamp; role='user'; name=''; text=$body }) } } elseif ((Get-JsonValue $part 'type') -eq 'tool_result') { $messages.Add([pscustomobject]@{ at=$stamp; role='result'; name=''; text=(Get-JsonValue $part 'content' '') }) } } }
            }
            elseif ($type -eq 'assistant') { foreach ($part in (Get-JsonValue (Get-JsonValue $r 'message') 'content' @())) { $role='assistant'; $name=''; $body=''; if (Get-JsonValue $part 'text') { $body=Get-JsonValue $part 'text' } elseif (Get-JsonValue $part 'thinking') { $role='thinking'; $body=Get-JsonValue $part 'thinking' } elseif ((Get-JsonValue $part 'type') -eq 'tool_use') { $role='tool'; $name=Get-JsonValue $part 'name' ''; $body=(Get-JsonValue $part 'input' @{}) | ConvertTo-Json -Compress -Depth 8 } elseif (Get-JsonValue $part 'tool_use') { $tool=Get-JsonValue $part 'tool_use'; $role='tool'; $name=Get-JsonValue $tool 'name' ''; $body=(Get-JsonValue $tool 'input' @{}) | ConvertTo-Json -Compress -Depth 8 }; if ($body) { $messages.Add([pscustomobject]@{ at=$stamp; role=$role; name=$name; text=$body }) } } }
            if (-not $title -and $type -eq 'custom-title') { $title = [string](Get-JsonValue $r 'customTitle' '') }
        }
        if ($stamp) { $updated = $stamp }
    }
    if (-not $updated) { try { $updated = (Get-Item -LiteralPath $file).LastWriteTime.ToString("yyyy-MM-dd'T'HH:mm:ss") } catch { } }
    if ($messages.Count -gt 300) { $messages = New-Object System.Collections.Generic.List[object] (,$messages.ToArray()[($messages.Count - 300)..($messages.Count - 1)]); $truncated = $true }
    foreach ($m in $messages) { $max = if ($m.role -in @('tool','result')) { 1500 } else { 4000 }; $rawText = if ($m.text -is [string]) { $m.text } else { $m.text | ConvertTo-Json -Compress -Depth 8 }; $clean = Get-Redacted ([string]$rawText); if ($clean.Length -gt $max) { $clean = $clean.Substring(0, $max - 3) + '...'; $truncated = $true }; $m.text = $clean }
    if (-not $title) { $title = $session }
    return @{ status = 200; value = [pscustomobject]@{ id=$id; agent=$(if ($kind -eq 'agy') {'antigravity'} else {$kind}); title=$title; model=$model; updated=$updated; truncated=$truncated; messages=@($messages.ToArray()) } }
}

function Get-ChatTaskInfo([string]$Agent, [string]$SessionId) {
    $paths = New-Object System.Collections.Generic.List[string]
    foreach ($candidate in $Repos) { if ($candidate -and (Test-Path -LiteralPath $candidate -PathType Container)) { $paths.Add($candidate) } }
    $trinityHome = if ($env:TRINITY_HOME) { $env:TRINITY_HOME } else { Join-Path $env:USERPROFILE '.trinity' }
    $registry = Join-Path $trinityHome 'repos.txt'
    if (Test-Path -LiteralPath $registry -PathType Leaf) { foreach ($line in [IO.File]::ReadAllLines($registry)) { $p=$line.Trim(); if ($p -and (Test-Path -LiteralPath $p -PathType Container)) { $paths.Add($p) } } }
    $paths.Add((Split-Path $PSScriptRoot -Parent))
    foreach ($repo in $paths) {
        $outDir = Join-Path $repo '.codex-out'
        if (-not (Test-Path -LiteralPath $outDir -PathType Container)) { continue }
        foreach ($metaFile in (Get-ChildItem -LiteralPath $outDir -Filter 'meta-*.json' -File -ErrorAction SilentlyContinue)) {
            try { $meta = $script:JsonSerializer.DeserializeObject([IO.File]::ReadAllText($metaFile.FullName)); if ((Get-JsonValue $meta 'sessionId') -eq $SessionId -and ((Get-JsonValue $meta 'agent') -eq $Agent -or ($Agent -eq 'agy' -and (Get-JsonValue $meta 'agent') -eq 'antigravity'))) { return @{ title=($metaFile.BaseName -replace '^meta-', ''); model=[string](Get-JsonValue $meta 'model' ''); effort=[string](Get-JsonValue $meta 'effort' '') } } } catch { }
        }
        $pattern = if ($Agent -eq 'codex') { "session id:\s*$([regex]::Escape($SessionId))" } else { "cascade_id:\s*$([regex]::Escape($SessionId))" }
        foreach ($logFile in (Get-ChildItem -LiteralPath $outDir -Filter $(if ($Agent -eq 'codex') { 'log-*.txt' } else { 'agy-*.log' }) -File -ErrorAction SilentlyContinue)) {
            $text = Get-TextLines $logFile.FullName 100
            if ($text -match $pattern) { $model=''; $effort=''; if ($text -match '(?im)^model:\s*(.+)$') { $model=$Matches[1].Trim() }; if ($text -match '(?im)^reasoning effort:\s*(.+)$') { $effort=$Matches[1].Trim() }; if ($Agent -eq 'agy' -and $text -match 'label="([^"]+)"') { $model=$Matches[1] }; return @{ title=($logFile.BaseName -replace '^(log|agy)-', ''); model=$model; effort=$effort } }
        }
    }
    return @{ title=''; model=''; effort='' }
}

function Invoke-GitRead([string]$Directory, [string[]]$Arguments) {
    $oldPreference = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try { $output = & git -C $Directory @Arguments 2>$null }
    finally { $ErrorActionPreference = $oldPreference }
    if ($LASTEXITCODE -ne 0) { return $null }
    return ($output -join "`n").Trim()
}

function Get-TextLines([string]$Path, [int]$Limit, [switch]$Tail) {
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) { return '' }
    # Agent output is UTF-8 (Get-Content in PS 5.1 would read it as ANSI). Read at most 64 KB from the head or
    # the tail, since logs grow to megabytes, and share ReadWrite so a running agent's log is readable mid-write.
    $text = ''
    $partial = $false
    try {
        $stream = New-Object IO.FileStream($Path, [IO.FileMode]::Open, [IO.FileAccess]::Read, [IO.FileShare]::ReadWrite)
        try {
            $count = [int][Math]::Min($stream.Length, 65536)
            if ($Tail -and $stream.Length -gt $count) { [void]$stream.Seek(-$count, [IO.SeekOrigin]::End); $partial = $true }
            $buffer = New-Object byte[] $count
            $read = 0
            while ($read -lt $count) {
                $n = $stream.Read($buffer, $read, $count - $read)
                if ($n -le 0) { break }
                $read += $n
            }
            $text = [Text.Encoding]::UTF8.GetString($buffer, 0, $read)
        }
        finally { $stream.Dispose() }
    }
    catch { return '' }
    $lines = @($text.TrimStart([char]0xFEFF).TrimEnd("`r", "`n") -split "`r?`n")
    # A tail read starts mid-line (maybe mid-character); drop that fragment.
    if ($partial -and $lines.Count -gt 1) { $lines = $lines[1..($lines.Count - 1)] }
    if ($Tail -and $lines.Count -gt $Limit) { $lines = $lines[($lines.Count - $Limit)..($lines.Count - 1)] }
    elseif (-not $Tail -and $lines.Count -gt $Limit) { $lines = $lines[0..($Limit - 1)] }
    return ($lines -join "`n")
}

function Get-TaskState([string]$Status, [bool]$RunnerAlive) {
    if ($RunnerAlive -or $Status -match '^(started|running|working)(\s|:|$)') { return 'running' }
    if ($Status -match '^failed') { return 'failed' }
    if ($Status -match '^merged') { return 'merged' }
    if ($Status -match '^cancelled') { return 'cancelled' }
    if ($Status -match '^done') { return 'done' }
    if ($Status -match '^waiting') { return 'waiting' }
    return 'other'
}

function Get-RepoTasks([string]$Main, [string]$Common) {
    $boardPath = Join-Path $Common 'agent-board.md'
    if (-not (Test-Path -LiteralPath $boardPath -PathType Leaf)) { return @() }
    $sections = New-Object System.Collections.Generic.List[object]
    $entry = $null
    foreach ($line in [IO.File]::ReadAllLines($boardPath)) {
        if ($line -match '^##\s+(.+)$') {
            if ($entry) { $sections.Add($entry) }
            $entry = [ordered]@{ task = $Matches[1].Trim(); agent = ''; branch = ''; worktree = ''; scope = ''; status = ''; session = '' }
        }
        elseif ($entry -and $line -match '^-\s+(agent|branch|worktree|scope|status|session):\s*(.*)$') {
            $entry[$Matches[1]] = $Matches[2].Trim()
        }
    }
    if ($entry) { $sections.Add($entry) }

    $outDir = Join-Path $Main '.codex-out'
    # Commits ahead of main for every branch in one git call (git 2.41+); older git falls back to rev-list per task.
    $aheadOf = @{}
    $refs = Invoke-GitRead $Main @('for-each-ref', '--format=%(refname:short) %(ahead-behind:main)', 'refs/heads')
    $batched = [bool]$refs
    if (-not $batched) { $refs = Invoke-GitRead $Main @('for-each-ref', '--format=%(refname:short)', 'refs/heads') }
    if ($refs) {
        foreach ($line in ($refs -split "`n")) {
            $parts = @($line.Trim() -split '\s+')
            if (-not $parts[0]) { continue }
            $aheadOf[$parts[0]] = $null
            if ($batched -and $parts.Count -ge 2 -and $parts[1] -match '^\d+$') { $aheadOf[$parts[0]] = [int]$parts[1] }
        }
    }
    $tasks = New-Object System.Collections.Generic.List[object]
    foreach ($item in $sections) {
        if ($item.agent -notin @('claude', 'codex', 'antigravity')) { continue }
        $pidPath = Join-Path $outDir ("pid-{0}.txt" -f $item.task)
        $runnerAlive = $false
        if (Test-Path -LiteralPath $pidPath -PathType Leaf) {
            $pidText = (Get-Content -LiteralPath $pidPath -Raw -ErrorAction SilentlyContinue).Trim()
            $runnerPid = 0
            if ([int]::TryParse($pidText, [ref]$runnerPid) -and $runnerPid -gt 0) {
                $runnerAlive = [bool](Get-Process -Id $runnerPid -ErrorAction SilentlyContinue)
            }
        }
        $status = [string]$item.status
        $state = Get-TaskState $status $runnerAlive
        $updated = ''
        if ($status -match '(\d{4}-\d{2}-\d{2} \d{2}:\d{2})\s*$') { $updated = $Matches[1] }
        $commits = 0
        if ($item.branch -and $item.branch -ne '-' -and $aheadOf.ContainsKey($item.branch)) {
            if ($null -ne $aheadOf[$item.branch]) { $commits = $aheadOf[$item.branch] }
            else {
                $count = Invoke-GitRead $Main @('rev-list', '--count', "main..$($item.branch)")
                if ($count -match '^\d+$') { $commits = [int]$count }
            }
        }
        $result = Get-Redacted (Get-TextLines (Join-Path $outDir ("result-{0}.md" -f $item.task)) 15)
        $logTail = Get-Redacted (Get-TextLines (Join-Path $outDir ("log-{0}.txt" -f $item.task)) 15 -Tail)
        $meta = $null
        $metaPath = Join-Path $outDir ("meta-{0}.json" -f $item.task)
        if (Test-Path -LiteralPath $metaPath -PathType Leaf) { try { $meta = $script:JsonSerializer.DeserializeObject([IO.File]::ReadAllText($metaPath)) } catch { } }
        $detail = @{ model=''; effort=''; sessionId=''; started=''; finished=''; durationSec=$null; lastActivity=''; tokens=$null; contextWindow=$null; toolCalls=0; rateLimits=@(); chat=''; subChats=@(); contextUsed=0L; steps=0; thinkingSteps=0; toolBreakdown=@(); lastTool=''; filesTouched=@(); backgroundTasks=0 }
        if ($item.agent -eq 'codex') { $detail = Get-CodexData $outDir $item.task $meta ($state -eq 'running') $item.worktree }
        elseif ($item.agent -eq 'antigravity') { $detail = Get-AgyData $outDir $item.task $meta ($state -eq 'running') $item.worktree }
        else {
            $detail.sessionId = [string]$item.session
            if ($item.session -match '^[0-9a-fA-F-]{36}$') {
                foreach ($slug in @((Get-ClaudeProjectSlug $Main), $(if ($item.worktree -and $item.worktree -ne '-') { Get-ClaudeProjectSlug $item.worktree }))) {
                    $claudeFile = Join-Path (Join-Path (Join-Path $script:ClaudeHome 'projects') $slug) ("{0}.jsonl" -f $item.session)
                    if (Test-Path -LiteralPath $claudeFile -PathType Leaf) {
                        $summary = Get-ClaudeSummary $claudeFile $slug $item.session '' -NoSubagents
                        $detail.chat = "claude:${slug}:$($item.session)"; $detail.model = $summary.model; $detail.effort = $summary.effort; $detail.started = $summary.started; $detail.lastActivity = $summary.lastActivity
                        break
                    }
                }
            }
        }
        $started = Convert-ToIso (Get-JsonValue $meta 'started' $detail.started)
        if (-not $started) { $started = $detail.started }
        $finished = Convert-ToIso (Get-JsonValue $meta 'finished' $detail.finished)
        if (-not $detail.sessionId) { $detail.sessionId = [string](Get-JsonValue $meta 'sessionId' '') }
        $duration = Get-JsonValue $meta 'durationSec'
        if ($null -eq $duration -and $started) { try { $begin = [datetime]::Parse($started); $end = if ($finished) { [datetime]::Parse($finished) } else { Get-Date }; $duration = [Math]::Round(($end - $begin).TotalSeconds, 1) } catch { } }
        $detail.model = [string](Get-JsonValue $meta 'model' $detail.model)
        $detail.effort = [string](Get-JsonValue $meta 'effort' $detail.effort)
        if ($meta -and $state -ne 'running' -and (Get-JsonValue $meta 'lastActivity')) { $detail.lastActivity = Convert-ToIso (Get-JsonValue $meta 'lastActivity') }
        if ($meta -and (Get-JsonValue $meta 'tokens') -and $state -ne 'running') { $detail.tokens = Get-JsonValue $meta 'tokens' }
        if (-not $detail.lastActivity) {
            $activityPath = if ($detail.rollout) { $detail.rollout } elseif ($item.agent -eq 'antigravity') { Join-Path $outDir ("agy-{0}.log" -f $item.task) } else { Join-Path $outDir ("log-{0}.txt" -f $item.task) }
            if ($activityPath -and (Test-Path -LiteralPath $activityPath -PathType Leaf)) { try { $detail.lastActivity = (Get-Item -LiteralPath $activityPath).LastWriteTime.ToString("yyyy-MM-dd'T'HH:mm:ss") } catch { } }
        }
        $lastActivity = $detail.lastActivity
        $tasks.Add([pscustomobject]@{
            task = [string]$item.task
            agent = [string]$item.agent
            state = $state
            status = $status
            updated = $updated
            branch = [string]$item.branch
            scope = [string]$item.scope
            runnerAlive = $runnerAlive
            commits = $commits
            result = $result
            logTail = $logTail
            model = [string]$detail.model
            effort = [string]$detail.effort
            sessionId = [string]$detail.sessionId
            started = $started
            finished = $finished
            durationSec = $duration
            lastActivity = $lastActivity
            tokens = $detail.tokens
            contextWindow = $detail.contextWindow
            toolCalls = [int]$detail.toolCalls
            contextUsed = [long]$detail.contextUsed
            steps = [int]$detail.steps
            thinkingSteps = [int]$detail.thinkingSteps
            toolBreakdown = @($detail.toolBreakdown)
            lastTool = [string]$detail.lastTool
            filesTouched = @($detail.filesTouched)
            backgroundTasks = [int]$detail.backgroundTasks
            rateLimits = @($detail.rateLimits)
            chat = [string]$detail.chat
            subChats = @($detail.subChats)
            worktree = [string]$item.worktree
        })
    }
    return @($tasks | Sort-Object @{ Expression = { if ($_.state -eq 'running') { 0 } else { 1 } } }, @{ Expression = { $_.updated }; Descending = $true })
}

function Get-RepoState([string]$Path) {
    # Every poll rebuilds state, and each git spawn costs ~50 ms on Windows: three calls per repo in total.
    $revParse = Invoke-GitRead $Path @('rev-parse', '--show-toplevel', '--path-format=absolute', '--git-common-dir')
    $revLines = @(if ($revParse) { $revParse -split "`n" })
    if ($revLines.Count -lt 2) { return $null }
    $root = [IO.Path]::GetFullPath($revLines[0].Trim())
    $common = [IO.Path]::GetFullPath($revLines[1].Trim())
    # A registered path may be a worktree; board, .codex-out and PLAN live with the main checkout.
    $main = Split-Path $common -Parent
    if (Test-Path -LiteralPath (Join-Path $main '.git')) { $root = $main }
    # Branch, commits ahead of upstream and changed files in one call.
    $branch = ''
    $ahead = 0
    $uncommitted = 0
    $status = Invoke-GitRead $root @('status', '--porcelain=v2', '--branch')
    if ($status) {
        foreach ($line in ($status -split "`n")) {
            if ($line -match '^# branch\.head (.+)$') { if ($Matches[1] -ne '(detached)') { $branch = $Matches[1].Trim() } }
            elseif ($line -match '^# branch\.ab \+(\d+) ') { $ahead = [int]$Matches[1] }
            elseif ($line -and $line -notmatch '^#') { $uncommitted++ }
        }
    }
    $plan = [ordered]@{ done = 0; total = 0; open = @() }
    $planPath = Join-Path $root 'PLAN.md'
    if (Test-Path -LiteralPath $planPath -PathType Leaf) {
        foreach ($line in [IO.File]::ReadAllLines($planPath)) {
            if ($line -match '^\s*-\s+\[([ xX])\]\s*(.*)$') {
                $plan.total++
                if ($Matches[1] -match '[xX]') { $plan.done++ } else { $plan.open += $Matches[2] }
            }
        }
    }
    $handoff = @()
    $handoffPath = Join-Path $root 'HANDOFF.md'
    if (Test-Path -LiteralPath $handoffPath -PathType Leaf) {
        $inStatus = $false
        foreach ($line in [IO.File]::ReadAllLines($handoffPath)) {
            if ($line -match '^##\s+') { $inStatus = ($line -match '^##\s+Status\s*$'); continue }
            if ($inStatus -and $line -match '^\s*-\s+(.+?)\s*$') { $handoff += $Matches[1] }
        }
    }
    $tasks = @(Get-RepoTasks $root $common)
    $claudeSessions = @(Get-ClaudeSessions $root $tasks)
    return [pscustomobject]@{
        name = (Split-Path $root -Leaf)
        path = $root
        branch = $branch
        uncommitted = $uncommitted
        ahead = $ahead
        plan = [pscustomobject]$plan
        handoff = @($handoff)
        tasks = $tasks
        claudeSessions = $claudeSessions
    }
}

function Get-AgentDefaults($RepoStates) {
    $defaults = [ordered]@{ claude = @{ model=''; effort='' }; codex = @{ model=''; effort='' }; antigravity = @{ model=''; effort='' } }
    $config = Join-Path $script:CodexHome 'config.toml'
    if (Test-Path -LiteralPath $config -PathType Leaf) {
        $text = Get-TextLines $config 100
        foreach ($line in ($text -split "`r?`n")) {
            if ($line -match '^\s*\[') { break }
            if (-not $defaults.codex.model -and $line -match '^\s*model\s*=\s*"([^"]+)"') { $defaults.codex.model = $Matches[1] }
            if (-not $defaults.codex.effort -and $line -match '^\s*model_reasoning_effort\s*=\s*"([^"]+)"') { $defaults.codex.effort = $Matches[1] }
        }
    }
    $agyLogs = Join-Path $script:AgyHome 'log'
    if (Test-Path -LiteralPath $agyLogs -PathType Container) {
        foreach ($file in (Get-ChildItem -LiteralPath $agyLogs -Filter 'cli-*.log' -File -ErrorAction SilentlyContinue | Sort-Object LastWriteTime -Descending | Select-Object -First 50)) {
            $log = Get-TextLines $file.FullName 65536
            if ($log -match 'label="([^"]+)"') { $defaults.antigravity.model = $Matches[1]; break }
        }
    }
    $sessions = @($RepoStates | ForEach-Object { $_.claudeSessions } | Sort-Object { $_.lastActivity } -Descending)
    if ($sessions.Count) { $defaults.claude.model = $sessions[0].model; $defaults.claude.effort = $sessions[0].effort }
    return $defaults
}

function Get-Availability($Defaults) {
    if (-not $script:AvailabilityCache -or ((Get-Date) - $script:AvailabilityCachedAt).TotalSeconds -ge 60) {
        . (Join-Path $PSScriptRoot 'lib\board.ps1')
        $script:AvailabilityCache = Get-AgentAvailability
        $script:AvailabilityCachedAt = Get-Date
    }
    $out = [ordered]@{}
    foreach ($agent in @('claude','codex','antigravity')) {
        $raw = $script:AvailabilityCache[$agent]
        $out[$agent] = [pscustomobject]@{ available=[bool]$raw.available; detail=[string]$raw.detail; model=[string]$Defaults[$agent].model; effort=[string]$Defaults[$agent].effort }
    }
    return [pscustomobject]$out
}

function Get-DashboardState {
    $candidatePaths = New-Object System.Collections.Generic.List[string]
    $trinityHome = if ($env:TRINITY_HOME) { $env:TRINITY_HOME } else { Join-Path $env:USERPROFILE '.trinity' }
    $registry = Join-Path $trinityHome 'repos.txt'
    if (Test-Path -LiteralPath $registry -PathType Leaf) {
        foreach ($line in [IO.File]::ReadAllLines($registry)) {
            $value = $line.Trim()
            if ($value -and -not $value.StartsWith('#')) { $candidatePaths.Add($value) }
        }
    }
    $scriptRoot = Split-Path $PSScriptRoot -Parent
    $candidatePaths.Add($scriptRoot)
    foreach ($extra in $Repos) { if ($extra) { $candidatePaths.Add($extra) } }
    $seen = @{}
    $repoStates = New-Object System.Collections.Generic.List[object]
    foreach ($candidate in $candidatePaths) {
        if (-not (Test-Path -LiteralPath $candidate -PathType Container)) { continue }
        $state = Get-RepoState $candidate
        if (-not $state) { continue }
        $key = $state.path.ToLowerInvariant()
        if ($seen.ContainsKey($key)) { continue }
        $seen[$key] = $true
        $repoStates.Add($state)
    }
    $allTasks = @($repoStates | ForEach-Object { $_.tasks })
    $defaults = Get-AgentDefaults $repoStates
    $agents = [ordered]@{}
    foreach ($agent in @('claude', 'codex', 'antigravity')) {
        $agentTasks = @($allTasks | Where-Object { $_.agent -eq $agent })
        $agents[$agent] = [pscustomobject]@{
            running = @($agentTasks | Where-Object { $_.state -eq 'running' }).Count
            done = @($agentTasks | Where-Object { $_.state -in @('done', 'merged') }).Count
            failed = @($agentTasks | Where-Object { $_.state -eq 'failed' }).Count
            total = $agentTasks.Count
            models = @($agentTasks | Where-Object { $_.state -eq 'running' -and $_.model } | ForEach-Object { $_.model } | Select-Object -Unique)
        }
    }
    $agents.claude.models = @(@($agents.claude.models) + @($repoStates | ForEach-Object { $_.claudeSessions } | Where-Object { $_.active -and $_.model } | ForEach-Object { $_.model }) | Select-Object -Unique)
    return [pscustomobject]@{
        generated = (Get-Date -Format "yyyy-MM-dd'T'HH:mm:ss")
        availability = Get-Availability $defaults
        agents = [pscustomobject]$agents
        repos = @($repoStates | Sort-Object name)
    }
}

# ASCII-only JSON (non-ASCII as \uXXXX) survives any console code page when -Once output is captured.
function Convert-StateToJson($State) {
    $json = $State | ConvertTo-Json -Depth 12 -Compress
    return [regex]::Replace($json, '[^\x00-\x7F]', { param($m) '\u{0:x4}' -f [int][char]$m.Value })
}

if ($Chat) {
    $kind = Get-ChatKeyKind $Chat
    if (-not $kind) { [Console]::Error.WriteLine('Malformed chat id.'); exit 2 }
    $chatResult = Resolve-Chat $Chat
    if ($chatResult.status -ne 200) { [Console]::Error.WriteLine('Chat not found.'); exit 3 }
    Convert-StateToJson $chatResult.value
    exit 0
}

if ($Once) {
    Convert-StateToJson (Get-DashboardState)
    exit 0
}

# Request failures go to <trinity home>\dashboard-errors.log (route + exception only, never chat content), kept under 512 KB.
function Write-DashboardError([string]$Route, $ErrorRecord) {
    try {
        $dir = if ($env:TRINITY_HOME) { $env:TRINITY_HOME } else { Join-Path $env:USERPROFILE '.trinity' }
        if (-not (Test-Path $dir)) { New-Item -ItemType Directory -Path $dir | Out-Null }
        $log = Join-Path $dir 'dashboard-errors.log'
        if ((Test-Path $log) -and (Get-Item $log).Length -gt 512KB) { Move-Item $log "$log.1" -Force }
        $where = if ($ErrorRecord.InvocationInfo) { $ErrorRecord.InvocationInfo.PositionMessage -replace '\s+', ' ' } else { '' }
        $line = '{0} {1} {2}: {3} {4}' -f (Get-Date -Format 's'), $Route, $ErrorRecord.Exception.GetType().Name, $ErrorRecord.Exception.Message, $where
        [IO.File]::AppendAllText($log, ($line -replace '[\r\n]+', ' ') + "`r`n", (New-Object Text.UTF8Encoding($false)))
    }
    catch { }
}

$listener = $null
try {
    $listener = New-Object System.Net.HttpListener
    $listener.Prefixes.Add("http://localhost:$Port/")
    $listener.Start()
}
catch {
    [Console]::Error.WriteLine("Dashboard could not listen on port ${Port}: $($_.Exception.Message)")
    exit 2
}
try {
    while ($listener.IsListening) {
        $context = $null
        try { $context = $listener.GetContext() }
        catch [System.Net.HttpListenerException] { break }
        catch [System.ObjectDisposedException] { break }
        if (-not $context) { continue }
        $response = $context.Response
        try {
            $response.Headers['X-Content-Type-Options'] = 'nosniff'
            $hostName = [string]$context.Request.Headers['Host']
            if ($hostName -notin @("localhost:$Port", "127.0.0.1:$Port")) {
                $response.StatusCode = 403
            }
            elseif ($context.Request.HttpMethod -ne 'GET') {
                $response.StatusCode = 404
            }
            elseif ($context.Request.Url.AbsolutePath -eq '/api/state') {
                $bytes = [Text.Encoding]::UTF8.GetBytes((Convert-StateToJson (Get-DashboardState)))
                $response.StatusCode = 200
                $response.ContentType = 'application/json; charset=utf-8'
                $response.Headers['Cache-Control'] = 'no-store'
                $response.ContentLength64 = $bytes.Length
                $response.OutputStream.Write($bytes, 0, $bytes.Length)
            }
            elseif ($context.Request.Url.AbsolutePath -eq '/api/chat') {
                $key = [string]$context.Request.QueryString['id']
                $chatResult = Resolve-Chat $key
                $response.StatusCode = $chatResult.status
                if ($chatResult.status -eq 200) {
                    $bytes = [Text.Encoding]::UTF8.GetBytes((Convert-StateToJson $chatResult.value))
                    $response.ContentType = 'application/json; charset=utf-8'
                    $response.Headers['Cache-Control'] = 'no-store'
                    $response.ContentLength64 = $bytes.Length
                    $response.OutputStream.Write($bytes, 0, $bytes.Length)
                }
            }
            elseif ($context.Request.Url.AbsolutePath -in @('/', '/dashboard-sample.json')) {
                # The page's ?sample=1 preview fetches the sample file next to it.
                $isSample = $context.Request.Url.AbsolutePath -ne '/'
                $htmlPath = Join-Path $PSScriptRoot $(if ($isSample) { 'dashboard-sample.json' } else { 'dashboard.html' })
                if (Test-Path -LiteralPath $htmlPath -PathType Leaf) {
                    $bytes = [IO.File]::ReadAllBytes($htmlPath)
                    $response.StatusCode = 200
                    $response.ContentType = $(if ($isSample) { 'application/json; charset=utf-8' } else { 'text/html; charset=utf-8' })
                    $response.ContentLength64 = $bytes.Length
                    $response.OutputStream.Write($bytes, 0, $bytes.Length)
                }
                else { $response.StatusCode = 404 }
            }
            else { $response.StatusCode = 404 }
        }
        catch {
            Write-DashboardError $context.Request.Url.AbsolutePath $_
            $response.StatusCode = 500
        }
        finally {
            try { $response.Close() } catch { }
        }
    }
}
finally { $listener.Close() }
