# Receives only Claude Code's documented status-line JSON; keeps the numeric
# fields of every session in one rolling file, claude-status.json, next to
# this script: {"schema":3,"updated_at":…,"sessions":{"<session_id>":{"updated_at":…,"rate_limits":{…}}}}.
# Sessions not heard from for KeepSeconds are dropped, and at most KeepSessions
# newest are kept, so the file never needs cleaning up.
$ErrorActionPreference = 'Stop'
[Console]::InputEncoding = New-Object System.Text.UTF8Encoding($false)
$KeepSeconds = 7 * 86400
$KeepSessions = 500
try {
    $payload = [Console]::In.ReadToEnd() | ConvertFrom-Json
    $session = [string]$payload.session_id
    if ($session -notmatch '^[a-zA-Z0-9_-]{1,100}$') { exit 0 }
    $destination = Join-Path $PSScriptRoot 'claude-status.json'
    $now = [DateTimeOffset]::UtcNow.ToUnixTimeMilliseconds() / 1000.0
    # Several sessions redraw their status lines at once; one writer at a time.
    $mutex = New-Object System.Threading.Mutex($false, 'Local\CirclowClaudeStatus')
    $held = $false
    try { $held = $mutex.WaitOne(3000) } catch [System.Threading.AbandonedMutexException] { $held = $true }
    try {
        $existing = $null
        if (Test-Path -LiteralPath $destination) {
            try { $existing = [System.IO.File]::ReadAllText($destination) | ConvertFrom-Json } catch { $existing = $null }
        }
        $previous = $null
        $sessions = [ordered]@{}
        if ($null -ne $existing -and $null -ne $existing.sessions) {
            foreach ($property in $existing.sessions.PSObject.Properties) {
                $entry = $property.Value
                if ($null -eq $entry -or $null -eq $entry.updated_at) { continue }
                if ($property.Name -eq $session) { $previous = $entry; continue }
                if (([double]$entry.updated_at) -ge ($now - $KeepSeconds)) { $sessions[$property.Name] = $entry }
            }
        }
        $limits = [ordered]@{}
        $live = @{}
        foreach ($name in @('five_hour', 'seven_day')) {
            $window = $payload.rate_limits.$name
            if ($null -ne $window -and $null -ne $window.used_percentage) {
                $used = [double]$window.used_percentage
                if ($used -ge 0 -and $used -le 100) {
                    $limits[$name] = [ordered]@{used_percentage=$used; resets_at=$window.resets_at; observed_at=$now}
                    $live[$name] = $true
                    continue
                }
            }
            # Claude Code omits a window once its reading passes the reset; keep
            # the last reading so the tray sees the rollover instead of a gap.
            $old = if ($null -ne $previous) { $previous.rate_limits.$name } else { $null }
            if ($null -ne $old -and $null -ne $old.used_percentage) {
                $seen = if ($null -ne $old.observed_at) { [double]$old.observed_at } else { [double]$previous.updated_at }
                $limits[$name] = [ordered]@{used_percentage=[double]$old.used_percentage; resets_at=$old.resets_at; observed_at=$seen}
            }
        }
        $sessions[$session] = [ordered]@{updated_at=$now; rate_limits=$limits}
        if ($sessions.Count -gt $KeepSessions) {
            $trimmed = [ordered]@{}
            $newest = $sessions.Keys | Sort-Object { [double]$sessions[$_].updated_at } -Descending | Select-Object -First $KeepSessions
            foreach ($key in $newest) { $trimmed[$key] = $sessions[$key] }
            $sessions = $trimmed
        }
        $record = [ordered]@{ schema=3; updated_at=$now; sessions=$sessions }
        $temporary = $destination + '.' + $PID + '.tmp'
        $utf8 = New-Object System.Text.UTF8Encoding($false)
        [System.IO.File]::WriteAllText($temporary, ($record | ConvertTo-Json -Depth 6 -Compress), $utf8)
        # The tray may be reading the file at this instant; a replace then fails once.
        $moved = $false
        for ($attempt = 0; $attempt -lt 5 -and -not $moved; $attempt++) {
            try { Move-Item -LiteralPath $temporary -Destination $destination -Force; $moved = $true } catch { Start-Sleep -Milliseconds 40 }
        }
        if (-not $moved) { Remove-Item -LiteralPath $temporary -Force -ErrorAction SilentlyContinue }
    } finally {
        if ($held) { $mutex.ReleaseMutex() }
        $mutex.Dispose()
    }
    $parts = @('Claude Code')
    if ($live.ContainsKey('five_hour')) { $parts += ('5h left {0:0}%' -f (100 - $limits.five_hour.used_percentage)) }
    if ($live.ContainsKey('seven_day')) { $parts += ('7d left {0:0}%' -f (100 - $limits.seven_day.used_percentage)) }
    [Console]::WriteLine(($parts -join ' | '))
} catch {
    # Do not spill stdin, terminal content or credentials to stdout/stderr.
    exit 0
}
