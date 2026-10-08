#Requires -Version 7.3
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$script:Repo = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..')).TrimEnd([IO.Path]::DirectorySeparatorChar)
$script:TestBase = Join-Path $script:Repo '.dfx-local/tmp'
$script:TestRoot = Join-Path $script:TestBase ('W02-test-' + [Guid]::NewGuid().ToString('N'))
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

    # A worker that immediately requires every identity/limit field would fail
    # if it ran during the deliberate delay before lease publication.
    $managerText = Get-Content -LiteralPath $script:Local -Raw
    $syntheticPath = Join-Path $script:CaseRepo 'scripts/local_synthetic.ps1'
    $syntheticText = Get-Content -LiteralPath $syntheticPath -Raw
    $proofPath = Join-Path $script:CaseRepo '.dfx-local/tmp/startup-proof.json'
    $leaseWorker = @'
param([string]$Scenario, [int]$Seconds)
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$repo = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
$lease = Get-Content -LiteralPath (Join-Path $repo '.dfx-local/state/active.json') -Raw | ConvertFrom-Json
$self = Get-Process -Id $PID
$owner = Get-Content -LiteralPath (Join-Path $repo '.dfx-local/owner.json') -Raw | ConvertFrom-Json
if ($lease.schema_version -ne 1 -or $lease.child_pid -ne $PID -or
    $lease.child_start_utc_ticks -ne $self.StartTime.ToUniversalTime().Ticks -or
    $lease.project_instance_id -cne $owner.project_instance_id -or $lease.manager_pid -le 0 -or
    $lease.manager_start_utc_ticks -le 0 -or -not $lease.manager_executable -or
    $lease.job_name -cne ('Local\DFX-Guard-' + $lease.run_id) -or $lease.task -cne 'idle' -or
    $lease.effective_limits.cpu_percent -ne 20 -or $lease.effective_limits.memory_bytes -ne 268435456 -or
    $lease.effective_limits.max_processes -le 0 -or -not $lease.started_at) {
    throw 'Worker started before complete lease identity and limits were published'
}
$lease | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath (Join-Path $repo '.dfx-local/tmp/startup-proof.json')
[Console]::Out.WriteLine('complete_lease_observed_before_work')
'@
    $publicationAnchor = '$child = Get-Process -Id $childPid -ErrorAction Stop'
    Check ($managerText.Contains($publicationAnchor)) 'publication-delay fixture identifies the actual manager boundary'
    $delayedText = $managerText.Replace($publicationAnchor,
        ('Start-Sleep -Milliseconds 1200' + [Environment]::NewLine + '            ' + $publicationAnchor))
    try {
        Set-Content -LiteralPath $syntheticPath -Value $leaseWorker -Encoding UTF8
        Set-Content -LiteralPath $script:Local -Value $delayedText -Encoding UTF8
        $published = Invoke-Case @('Run', '-Scenario', 'idle', '-Seconds', '1', '-TimeoutSeconds', '8') | ConvertFrom-Json
        Check ($published.status -eq 'finished') 'delayed lease publication precedes any worker execution'
        $proof = Get-Content -LiteralPath $proofPath -Raw | ConvertFrom-Json
        Check ($proof.run_id -ceq $published.run_id -and $proof.child_pid -eq $published.child_pid) 'worker observed its exact published lease'
        Check (-not [IO.File]::Exists((Join-Path $script:CaseRepo '.dfx-local/state/active.json'))) 'delayed startup clears the active marker after success'
        Remove-Item -LiteralPath $proofPath -Force

        $moveAnchor = '[IO.File]::Move($activePendingPath, $script:ActivePath)'
        Check ($managerText.Contains($moveAnchor)) 'publication-failure fixture reaches the pending lease before publication'
        $failedText = $managerText.Replace($moveAnchor, 'throw ''Synthetic lease publication failure''')
        Set-Content -LiteralPath $script:Local -Value $failedText -Encoding UTF8
        $failedStart = Invoke-Case @('Run', '-Scenario', 'idle', '-Seconds', '1', '-TimeoutSeconds', '8') $false | ConvertFrom-Json
        Check ($failedStart.status -eq 'failed' -and $failedStart.reason -ceq 'Synthetic lease publication failure') 'lease publication failure fails closed'
        Check (Wait-Dead $failedStart.child_pid) 'publication failure terminates the suspended worker'
        Check (-not [IO.File]::Exists($proofPath)) 'failed publication never runs worker code'
        Check (-not [IO.File]::Exists((Join-Path $script:CaseRepo '.dfx-local/state/active.json'))) 'failed publication leaves no active marker'
        Check (@(Get-ChildItem -LiteralPath (Join-Path $script:CaseRepo '.dfx-local/state') -Filter 'active-*.new').Count -eq 0) 'failed publication removes its partial pending lease'
        $released = [DfxLocal.BoundedJob]::new(20, 268435456, 2, ('Local\DFX-Guard-' + $failedStart.run_id))
        $released.Dispose()
        Check $true 'publication failure closes the named Job so its identity can be reused'

        $resumeAnchor = '$job.Resume()'
        Check ($managerText.Contains($resumeAnchor)) 'resume-failure fixture reaches the published-lease boundary'
        $resumeFailureText = $managerText.Replace($resumeAnchor, 'throw ''Synthetic worker resume failure''')
        Set-Content -LiteralPath $script:Local -Value $resumeFailureText -Encoding UTF8
        $failedResume = Invoke-Case @('Run', '-Scenario', 'idle', '-Seconds', '1', '-TimeoutSeconds', '8') $false | ConvertFrom-Json
        Check ($failedResume.status -eq 'failed' -and $failedResume.reason -ceq 'Synthetic worker resume failure') 'post-publication resume exception fails closed'
        Check (Wait-Dead $failedResume.child_pid) 'resume exception terminates the suspended worker'
        Check (-not [IO.File]::Exists($proofPath)) 'resume exception never runs worker code'
        Check (-not [IO.File]::Exists((Join-Path $script:CaseRepo '.dfx-local/state/active.json'))) 'resume exception clears its published active marker'

        $stopAnchor = '$markerCreated = $true'
        Check ($managerText.Contains($stopAnchor)) 'startup-stop fixture identifies the published marker boundary'
        $stopText = $managerText.Replace($stopAnchor, ($stopAnchor + [Environment]::NewLine +
            '            @{ schema_version = 1; run_id = $runId } | ConvertTo-Json | Set-Content -LiteralPath $script:StopPath -Encoding UTF8'))
        Set-Content -LiteralPath $script:Local -Value $stopText -Encoding UTF8
        $stoppedStart = Invoke-Case @('Run', '-Scenario', 'idle', '-Seconds', '1', '-TimeoutSeconds', '8') $false | ConvertFrom-Json
        Check ($stoppedStart.status -eq 'canceled' -and $stoppedStart.reason -ceq 'stop_requested') 'Stop arriving before resume prevents worker execution'
        Check (Wait-Dead $stoppedStart.child_pid) 'pre-resume Stop terminates the suspended worker'
        Check (-not [IO.File]::Exists($proofPath)) 'pre-resume Stop never runs worker code'
        Check (-not [IO.File]::Exists((Join-Path $script:CaseRepo '.dfx-local/state/active.json'))) 'pre-resume Stop clears the active marker'
    } finally {
        Set-Content -LiteralPath $script:Local -Value $managerText -Encoding UTF8
        Set-Content -LiteralPath $syntheticPath -Value $syntheticText -Encoding UTF8
    }

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
    $managedTmp = [IO.Path]::GetFullPath($script:TestBase).TrimEnd([IO.Path]::DirectorySeparatorChar)
    if (-not $base.StartsWith(($managedTmp + [IO.Path]::DirectorySeparatorChar), [StringComparison]::OrdinalIgnoreCase) -or
        [IO.Path]::GetFileName($base) -notlike 'W02-test-*') { throw 'Test cleanup path failed containment check' }
    if ([IO.Directory]::Exists($base)) { Remove-Item -LiteralPath $base -Recurse -Force }
}
