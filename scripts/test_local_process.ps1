#Requires -Version 7.3
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$script:Repo = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..')).TrimEnd([IO.Path]::DirectorySeparatorChar)
$script:TestRoot = Join-Path $script:Repo ('runs/W02-test-' + [Guid]::NewGuid().ToString('N'))
$script:CaseRepo = Join-Path $script:TestRoot 'repo 空格'
$script:Local = Join-Path $script:CaseRepo 'scripts/local.ps1'
$script:Checks = 0

function Check([bool]$Okay, [string]$Message) {
    if (-not $Okay) { throw "CHECK FAILED: $Message" }
    $script:Checks++
}
function Invoke-Case([string[]]$ArgsList, [bool]$ShouldPass = $true) {
    $named = @{ Action = $ArgsList[0] }
    for ($i = 1; $i -lt $ArgsList.Count; $i += 2) { $named[$ArgsList[$i].TrimStart('-')] = $ArgsList[$i + 1] }
    try {
        $output = & $script:Local @named 2>&1
        $code = $LASTEXITCODE
    } catch {
        if ($ShouldPass) { throw }
        return $_.Exception.Message
    }
    if ($ShouldPass -and $code -ne 0) { throw "Case failed ($code): $($ArgsList -join ' '): $($output | Out-String)" }
    if (-not $ShouldPass -and $code -eq 0) { throw "Case unexpectedly passed: $($ArgsList -join ' ')" }
    return ($output | Out-String)
}
function Start-Manager([string]$Scenario, [int]$Seconds = 30, [int]$Timeout = 60) {
    $start = [Diagnostics.ProcessStartInfo]::new((Get-Process -Id $PID).Path)
    $start.UseShellExecute = $false
    $start.CreateNoWindow = $true
    foreach ($arg in @('-NoProfile', '-NonInteractive', '-File', $script:Local, 'Run', '-Scenario', $Scenario,
            '-Seconds', "$Seconds", '-TimeoutSeconds', "$Timeout")) { $null = $start.ArgumentList.Add($arg) }
    $start.RedirectStandardOutput = $true; $start.RedirectStandardError = $true
    return [Diagnostics.Process]::Start($start)
}
function Wait-Marker([int]$TimeoutSeconds = 15) {
    $path = Join-Path $script:CaseRepo '.dfx-local/state/active.json'
    $watch = [Diagnostics.Stopwatch]::StartNew()
    while ($watch.Elapsed.TotalSeconds -lt $TimeoutSeconds) {
        if ([IO.File]::Exists($path)) {
            try { return (Get-Content -LiteralPath $path -Raw | ConvertFrom-Json) } catch {}
        }
        Start-Sleep -Milliseconds 100
    }
    throw 'Timed out waiting for active marker'
}
function Wait-Dead([int]$Id) {
    $watch = [Diagnostics.Stopwatch]::StartNew()
    while ($watch.Elapsed.TotalSeconds -lt 8) {
        if ($null -eq (Get-Process -Id $Id -ErrorAction SilentlyContinue)) { return $true }
        Start-Sleep -Milliseconds 100
    }
    return $false
}
function Wait-SpawnedChild([string]$LogPath) {
    $watch = [Diagnostics.Stopwatch]::StartNew()
    while ($watch.Elapsed.TotalSeconds -lt 8) {
        if ([IO.File]::Exists($LogPath)) {
            try {
                $stream = [IO.File]::Open($LogPath, [IO.FileMode]::Open, [IO.FileAccess]::Read, [IO.FileShare]::ReadWrite)
                try {
                    $reader = [IO.StreamReader]::new($stream)
                    $match = [regex]::Match($reader.ReadToEnd(), 'child_pid=(\d+)')
                    if ($match.Success) { return [int]$match.Groups[1].Value }
                } finally { $stream.Dispose() }
            } catch [IO.IOException] {}
        }
        Start-Sleep -Milliseconds 100
    }
    throw 'Timed out waiting for synthetic grandchild'
}
function Close-Manager($Process) {
    if ($null -ne $Process -and -not $Process.HasExited) { $Process.Kill(); $null = $Process.WaitForExit(5000) }
    if ($null -ne $Process) { $Process.Dispose() }
}

$manager = $null
try {
    [IO.Directory]::CreateDirectory((Join-Path $script:CaseRepo 'scripts')) | Out-Null
    [IO.Directory]::CreateDirectory((Join-Path $script:CaseRepo 'configs')) | Out-Null
    foreach ($file in @('local.ps1', 'local_job.cs', 'local_synthetic.ps1')) {
        Copy-Item -LiteralPath (Join-Path $script:Repo "scripts/$file") -Destination (Join-Path $script:CaseRepo "scripts/$file")
    }
    $policyPath = Join-Path $script:CaseRepo 'configs/local-runtime.json'
    $policy = Get-Content -LiteralPath (Join-Path $script:Repo 'configs/local-runtime.json') -Raw | ConvertFrom-Json
    $policy.limits.job_memory_cap_bytes = 268435456
    $policy.limits.max_log_file_bytes = 32768
    $policy.limits.max_run_log_bytes = 65536
    $policy.limits.monitor_interval_seconds = 1
    $policy | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath $policyPath -Encoding UTF8
    $prepared = Invoke-Case @('Prepare') | ConvertFrom-Json
    Check $prepared.prepared 'isolated owner prepared'

    $src = Get-Content -LiteralPath (Join-Path $script:CaseRepo 'scripts/local_job.cs') -Raw
    Add-Type -TypeDefinition $src
    $job = [DfxLocal.BoundedJob]::new(20, 268435456, 2)
    Check ($job.MemoryLimitBytes -eq 268435456) 'queried CPU, process and memory Job limits'
    $job.Dispose()
    $memory = [DfxLocal.BoundedJob]::PhysicalMemory()
    Check ($memory[0] -gt 0 -and $memory[1] -gt 0) 'physical memory query'

    $idle = Invoke-Case @('Run', '-Scenario', 'idle', '-Seconds', '1', '-TimeoutSeconds', '4') | ConvertFrom-Json
    Check ($idle.status -eq 'finished' -and $idle.reason -eq 'exited_zero') 'bounded idle task finished'
    Check (-not [IO.File]::Exists((Join-Path $script:CaseRepo '.dfx-local/state/active.json'))) 'normal exit clears marker'
    $timeout = Invoke-Case @('Run', '-Scenario', 'idle', '-Seconds', '5', '-TimeoutSeconds', '1') $false | ConvertFrom-Json
    Check ($timeout.reason -eq 'timeout' -and $timeout.status -eq 'canceled') 'wall timeout terminates Job'
    Check (Wait-Dead $timeout.child_pid) 'timed-out child exited'
    $noiseText = Invoke-Case @('Run', '-Scenario', 'noisy', '-Seconds', '5', '-TimeoutSeconds', '8') $false
    if (-not $noiseText.TrimStart().StartsWith('{')) { throw "Unexpected noisy response: $noiseText" }
    $noise = $noiseText | ConvertFrom-Json
    Check ($noise.reason -eq 'log_limit' -and $noise.log_truncated) 'log growth stops task and truncates retained log'
    Check ([IO.FileInfo]::new($noise.log_path).Length -le 32768) 'retained log within cap'
    $shortNoise = Invoke-Case @('Run', '-Scenario', 'noisy', '-Seconds', '1', '-TimeoutSeconds', '4') $false | ConvertFrom-Json
    Check ($shortNoise.reason -eq 'log_limit') 'late output still fails before accepting exit'
    $memoryText = Invoke-Case @('Run', '-Scenario', 'memory', '-Seconds', '3', '-TimeoutSeconds', '6') $false
    $memoryRun = $memoryText | ConvertFrom-Json
    Check ($memoryRun.status -eq 'failed' -and $memoryRun.reason -eq 'child_exit_nonzero') 'small Job cap rejects synthetic allocations'
    Check (Wait-Dead $memoryRun.child_pid) 'memory-failed child exited'

    $outside = Join-Path $script:TestRoot 'outside'
    [IO.Directory]::CreateDirectory($outside) | Out-Null
    [IO.File]::WriteAllText((Join-Path $outside 'sentinel.txt'), 'keep')
    $linked = Join-Path $script:CaseRepo '.dfx-local/tmp/link'
    New-Item -ItemType Junction -Path $linked -Target $outside | Out-Null
    $refusedPath = Invoke-Case @('Run', '-Scenario', 'idle', '-Seconds', '1') $false
    Check ($refusedPath.Contains('Reparse point')) 'Run refuses linked managed subtree'
    Check ([IO.File]::ReadAllText((Join-Path $outside 'sentinel.txt')) -ceq 'keep') 'outside sentinel intact'
    Remove-Item -LiteralPath $linked -Force

    $invalid = Get-Content -LiteralPath $policyPath -Raw | ConvertFrom-Json
    $invalid.limits.cpu_rate_percent = 0
    $invalid | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath $policyPath -Encoding UTF8
    $denied = Invoke-Case @('Run', '-Scenario', 'idle', '-Seconds', '1') $false | ConvertFrom-Json
    Check ($denied.status -eq 'failed' -and $denied.reason -eq 'Invalid local resource policy') 'invalid limits fail closed'
    $policy | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath $policyPath -Encoding UTF8

    $manager = Start-Manager child
    $active = Wait-Marker
    Check ($active.child_pid -gt 0) 'managed parent started'
    $grandchild = Wait-SpawnedChild (Join-Path $script:CaseRepo ".dfx-local/artifacts/$($active.run_id).log")
    $busy = Invoke-Case @('Run', '-Scenario', 'idle', '-Seconds', '1') $false
    Check ($busy.Contains('Managed root is busy')) 'concurrent Run rejected'
    $tampered = Get-Content -LiteralPath (Join-Path $script:CaseRepo '.dfx-local/state/active.json') -Raw | ConvertFrom-Json
    $tampered.manager_start_utc_ticks = [long]$tampered.manager_start_utc_ticks + 1
    $tampered | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath (Join-Path $script:CaseRepo '.dfx-local/state/active.json')
    $refused = Invoke-Case @('Stop') $false
    Check ($refused.Contains('identity mismatch')) 'PID identity mismatch refuses Stop'
    $active | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath (Join-Path $script:CaseRepo '.dfx-local/state/active.json')
    $stopped = Invoke-Case @('Stop') | ConvertFrom-Json
    Check $stopped.stopped 'Stop completed'
    Check (Wait-Dead $active.child_pid) 'Stop terminated managed parent'
    Check (Wait-Dead $grandchild) 'Stop terminated grandchild in same Job'
    Check (-not [IO.File]::Exists((Join-Path $script:CaseRepo '.dfx-local/state/active.json'))) 'Stop cleared marker'
    Close-Manager $manager; $manager = $null

    $manager = Start-Manager child
    $active = Wait-Marker
    $grandchild = Wait-SpawnedChild (Join-Path $script:CaseRepo ".dfx-local/artifacts/$($active.run_id).log")
    $manager.Kill(); $null = $manager.WaitForExit(5000)
    Check (Wait-Dead $active.child_pid) 'manager crash closes Job and kills child'
    Check (Wait-Dead $grandchild) 'manager crash kills grandchild'
    $recovered = Invoke-Case @('Stop') | ConvertFrom-Json
    Check $recovered.stopped 'stale marker recoverable after crash'
    Close-Manager $manager; $manager = $null
    "PASS: $script:Checks W02 bounded-process assertions in isolated synthetic tree"
} finally {
    Close-Manager $manager
    $base = [IO.Path]::GetFullPath($script:TestRoot)
    $runsBase = [IO.Path]::GetFullPath((Join-Path $script:Repo 'runs')).TrimEnd([IO.Path]::DirectorySeparatorChar)
    if (-not $base.StartsWith(($runsBase + [IO.Path]::DirectorySeparatorChar), [StringComparison]::OrdinalIgnoreCase) -or
        [IO.Path]::GetFileName($base) -notlike 'W02-test-*') { throw 'Test cleanup path failed containment check' }
    if ([IO.Directory]::Exists($base)) { Remove-Item -LiteralPath $base -Recurse -Force }
}
