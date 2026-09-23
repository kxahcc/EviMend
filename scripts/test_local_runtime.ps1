param([ValidateSet('Paths', 'Processes', 'Bootstrap')][string]$Suite = 'Paths')

if ($Suite -eq 'Bootstrap') {
    & (Join-Path $PSScriptRoot 'test_local_bootstrap.ps1')
    exit $LASTEXITCODE
}

if ($Suite -eq 'Processes') {
    & (Join-Path $PSScriptRoot 'test_local_process.ps1')
    exit $LASTEXITCODE
}

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$script:Repo = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..')).TrimEnd([IO.Path]::DirectorySeparatorChar)
$script:Runs = Join-Path $script:Repo 'runs'
$script:TestRoot = Join-Path $script:Runs ("W01-test-" + [Guid]::NewGuid().ToString('N'))
$script:CaseRepo = Join-Path $script:TestRoot 'repo 空格 示例'
$script:Sentinel = Join-Path $script:TestRoot 'outside-sentinel.txt'
$script:LocalScript = Join-Path $script:CaseRepo 'scripts/local.ps1'
$script:Checks = 0

function Check([bool]$Condition, [string]$Message) {
    if (-not $Condition) { throw "CHECK FAILED: $Message" }
    $script:Checks++
}

function Invoke-Local([string[]]$Arguments, [bool]$ShouldPass = $true) {
    $named = @{ Action = $Arguments[0] }
    for ($index = 1; $index -lt $Arguments.Count; $index++) {
        $key = $Arguments[$index].TrimStart('-')
        if ($key -in @('Apply', 'DiscardEvidence')) { $named[$key] = $true }
        else { $index++; $named[$key] = $Arguments[$index] }
    }
    try {
        $result = & $script:LocalScript @named 2>&1
        if (-not $ShouldPass) { throw "Expected failure: $($Arguments -join ' ')" }
        return (($result | Out-String) | ConvertFrom-Json)
    } catch {
        if ($ShouldPass) { throw }
        return $_.Exception.Message
    }
}

function Write-File([string]$Path, [string]$Text) {
    $parent = [IO.Path]::GetDirectoryName($Path)
    [IO.Directory]::CreateDirectory($parent) | Out-Null
    [IO.File]::WriteAllText($Path, $Text)
}

try {
    [IO.Directory]::CreateDirectory((Join-Path $script:CaseRepo 'scripts')) | Out-Null
    [IO.Directory]::CreateDirectory((Join-Path $script:CaseRepo 'configs')) | Out-Null
    Copy-Item -LiteralPath (Join-Path $script:Repo 'scripts/local.ps1') -Destination $script:LocalScript
    Copy-Item -LiteralPath (Join-Path $script:Repo 'configs/local-runtime.json') -Destination (Join-Path $script:CaseRepo 'configs/local-runtime.json')
    Write-File $script:Sentinel 'keep-me'
    Write-File (Join-Path $script:CaseRepo 'data_B/source.txt') 'original'

    $before = Invoke-Local -Arguments @('Status')
    Check (-not $before.owner_valid) 'fresh root must not claim ownership'
    $prepared = Invoke-Local -Arguments @('Prepare')
    Check ($prepared.prepared -and -not $prepared.runtime_installed) 'Prepare must not install runtime'
    $again = Invoke-Local -Arguments @('Prepare')
    Check ($again.project_instance_id -eq $prepared.project_instance_id) 'Prepare must be idempotent'

    $root = Join-Path $script:CaseRepo '.dfx-local'
    $cacheFile = Join-Path $root 'cache/one.txt'
    Write-File $cacheFile 'disposable'
    $plan = Invoke-Local -Arguments @('Clean', '-Scope', 'cache')
    Check ($plan.dry_run -and [IO.File]::Exists($cacheFile)) 'Clean without Apply is a preview'
    $wrong = Invoke-Local -Arguments @('Clean', '-Scope', 'cache', '-Apply', '-PlanId', ([Guid]::NewGuid().ToString())) -ShouldPass $false
    Check ($wrong.Contains('no longer matches')) 'wrong plan ID must fail'
    $withoutId = Invoke-Local -Arguments @('Clean', '-Scope', 'cache', '-Apply') -ShouldPass $false
    Check ($withoutId.Contains('PlanId')) 'Apply without plan ID must fail'
    Check ([IO.File]::Exists($cacheFile)) 'wrong plan must preserve cache'
    $applied = Invoke-Local -Arguments @('Clean', '-Scope', 'cache', '-Apply', '-PlanId', $plan.plan_id)
    Check ($applied.applied -and -not [IO.File]::Exists($cacheFile)) 'matching plan removes only cache'
    Check ([IO.File]::ReadAllText($script:Sentinel) -ceq 'keep-me') 'outside sentinel unchanged'
    Check ([IO.File]::ReadAllText((Join-Path $script:CaseRepo 'data_B/source.txt')) -ceq 'original') 'data unchanged'

    $uvCache = Join-Path $root 'cache/uv/keep.txt'
    Write-File $uvCache 'uv-cache'
    Write-File (Join-Path $root 'cache/pytest/old.txt') 'old-cache'
    $testCachePlan = Invoke-Local -Arguments @('Clean', '-Scope', 'testcache')
    Check ($testCachePlan.target_count -eq 1 -and [IO.File]::Exists($uvCache)) 'testcache plan selects only test cache'
    $null = Invoke-Local -Arguments @('Clean', '-Scope', 'testcache', '-Apply', '-PlanId', $testCachePlan.plan_id)
    Check (-not [IO.Directory]::Exists((Join-Path $root 'cache/pytest')) -and [IO.File]::Exists($uvCache)) 'testcache preserves uv cache'
    Write-File (Join-Path $root 'tmp/old.txt') 'temporary'
    $tmpPlan = Invoke-Local -Arguments @('Clean', '-Scope', 'tmp')
    Check ($tmpPlan.target_count -eq 1 -and [IO.File]::Exists($uvCache)) 'tmp plan selects only temporary directory'
    $null = Invoke-Local -Arguments @('Clean', '-Scope', 'tmp', '-Apply', '-PlanId', $tmpPlan.plan_id)
    Check (-not [IO.Directory]::Exists((Join-Path $root 'tmp')) -and [IO.File]::Exists($uvCache)) 'tmp cleanup preserves cache'

    Write-File $cacheFile 'new'
    $stale = Invoke-Local -Arguments @('Clean', '-Scope', 'cache')
    Write-File $cacheFile 'changed length'
    $changed = Invoke-Local -Arguments @('Clean', '-Scope', 'cache', '-Apply', '-PlanId', $stale.plan_id) -ShouldPass $false
    Check ($changed.Contains('targets changed')) 'modified target must invalidate plan'
    Check ([IO.File]::Exists($cacheFile)) 'invalidated plan must not delete'

    $plan = Invoke-Local -Arguments @('Clean', '-Scope', 'cache')
    $planPath = Join-Path $root 'state/cleanup-plan.json'
    $savedPlan = Get-Content -LiteralPath $planPath -Raw | ConvertFrom-Json
    $savedPlan.created_at = [DateTimeOffset]::UtcNow.AddHours(-1).ToString('o')
    $savedPlan | ConvertTo-Json | Set-Content -LiteralPath $planPath
    $expired = Invoke-Local -Arguments @('Clean', '-Scope', 'cache', '-Apply', '-PlanId', $plan.plan_id) -ShouldPass $false
    Check ($expired.Contains('expired')) 'expired plan must fail'

    $ownerPath = Join-Path $root 'owner.json'
    $savedOwner = Get-Content -LiteralPath $ownerPath -Raw
    Remove-Item -LiteralPath $ownerPath
    $missing = Invoke-Local -Arguments @('Clean', '-Scope', 'cache') -ShouldPass $false
    Check ($missing.Contains('no owner')) 'missing owner must fail'
    Write-File $ownerPath $savedOwner
    $badOwner = $savedOwner | ConvertFrom-Json
    $badOwner.repo_real_path = $script:TestRoot
    $badOwner | ConvertTo-Json | Set-Content -LiteralPath $ownerPath
    $mismatch = Invoke-Local -Arguments @('Clean', '-Scope', 'cache') -ShouldPass $false
    Check ($mismatch.Contains('does not match')) 'foreign owner must fail'
    Write-File $ownerPath $savedOwner

    Write-File (Join-Path $root 'state/active.json') '{}'
    $active = Invoke-Local -Arguments @('Clean', '-Scope', 'cache') -ShouldPass $false
    Check ($active.Contains('activity marker')) 'active task must block cleanup'
    Remove-Item -LiteralPath (Join-Path $root 'state/active.json')
    Write-File (Join-Path $root 'state/remote-jobs.json') '{}'
    $remote = Invoke-Local -Arguments @('Clean', '-Scope', 'cache') -ShouldPass $false
    Check ($remote.Contains('activity marker')) 'unknown remote job must block cleanup'
    Remove-Item -LiteralPath (Join-Path $root 'state/remote-jobs.json')

    Write-File (Join-Path $root 'work/r1/file.txt') 'working'
    $unfinished = Invoke-Local -Arguments @('Clean', '-Scope', 'work') -ShouldPass $false
    Check ($unfinished.Contains('no terminal state')) 'unfinished work must be retained'
    Write-File (Join-Path $root 'work/r1/run-state.json') '{"status":"finished"}'
    $workPlan = Invoke-Local -Arguments @('Clean', '-Scope', 'work')
    $null = Invoke-Local -Arguments @('Clean', '-Scope', 'work', '-Apply', '-PlanId', $workPlan.plan_id)
    Check (-not [IO.Directory]::Exists((Join-Path $root 'work'))) 'finished work can be cleaned'

    $internalDir = Join-Path $root 'cache/internal'
    Write-File (Join-Path $internalDir 'inside.txt') 'internal'
    New-Item -ItemType Junction -Path (Join-Path $root 'cache/link') -Target $internalDir | Out-Null
    $internalPlan = Invoke-Local -Arguments @('Clean', '-Scope', 'cache')
    $null = Invoke-Local -Arguments @('Clean', '-Scope', 'cache', '-Apply', '-PlanId', $internalPlan.plan_id)
    Check (-not [IO.Directory]::Exists((Join-Path $root 'cache'))) 'verified internal junction is removed without traversal'
    $shared = Join-Path $root 'cache/shared.txt'
    Write-File $shared 'shared'
    [IO.Directory]::CreateDirectory((Join-Path $root 'env')) | Out-Null
    New-Item -ItemType HardLink -Path (Join-Path $root 'env/shared.txt') -Target $shared | Out-Null
    $hardlinkPlan = Invoke-Local -Arguments @('Clean', '-Scope', 'env')
    $null = Invoke-Local -Arguments @('Clean', '-Scope', 'env', '-Apply', '-PlanId', $hardlinkPlan.plan_id)
    Check ([IO.File]::ReadAllText($shared) -ceq 'shared') 'cleaning hardlink preserves other name'

    Write-File (Join-Path $root 'artifacts/patch.diff') 'evidence'
    $blocked = Invoke-Local -Arguments @('Clean', '-Scope', 'all') -ShouldPass $false
    Check ($blocked.Contains('Evidence remains')) 'all must protect evidence by default'
    Check ([IO.File]::Exists((Join-Path $root 'artifacts/patch.diff'))) 'blocked clean keeps evidence'

    $outsideDir = Join-Path $script:TestRoot 'external directory'
    [IO.Directory]::CreateDirectory($outsideDir) | Out-Null
    Write-File (Join-Path $outsideDir 'keep.txt') 'outside'
    $link = Join-Path $root 'cache/link'
    [IO.Directory]::CreateDirectory((Join-Path $root 'cache')) | Out-Null
    New-Item -ItemType Junction -Path $link -Target $outsideDir | Out-Null
    $linked = Invoke-Local -Arguments @('Clean', '-Scope', 'cache') -ShouldPass $false
    Check ($linked.Contains('Reparse point')) 'junction must block planning'
    Check ([IO.File]::Exists((Join-Path $outsideDir 'keep.txt'))) 'junction target unchanged'
    Remove-Item -LiteralPath $link -Force

    $beforeSwap = Invoke-Local -Arguments @('Clean', '-Scope', 'cache')
    Remove-Item -LiteralPath (Join-Path $root 'cache') -Recurse -Force
    New-Item -ItemType Junction -Path (Join-Path $root 'cache') -Target $outsideDir | Out-Null
    $swapped = Invoke-Local -Arguments @('Clean', '-Scope', 'cache', '-Apply', '-PlanId', $beforeSwap.plan_id) -ShouldPass $false
    Check ($swapped.Contains('Reparse point')) 'link swapped after planning must block Apply'
    Check ([IO.File]::Exists((Join-Path $outsideDir 'keep.txt'))) 'swapped link target unchanged'
    Remove-Item -LiteralPath (Join-Path $root 'cache') -Force

    $complete = Invoke-Local -Arguments @('Clean', '-Scope', 'all', '-DiscardEvidence')
    Check ([IO.Directory]::Exists($root)) 'all plan is still a preview'
    $allDone = Invoke-Local -Arguments @('Clean', '-Scope', 'all', '-DiscardEvidence', '-Apply', '-PlanId', $complete.plan_id)
    Check ($allDone.applied -and -not [IO.Directory]::Exists($root)) 'all removes only managed root'
    Check ([IO.File]::Exists((Join-Path $script:CaseRepo 'data_B/source.txt'))) 'all leaves data'
    Check ([IO.File]::Exists((Join-Path $outsideDir 'keep.txt'))) 'all leaves external directory'
    Check ([IO.File]::Exists((Join-Path $script:CaseRepo ('runs/exports/cleanup-' + $complete.plan_id + '.json')))) 'all writes receipt'
    $second = Invoke-Local -Arguments @('Clean', '-Scope', 'all', '-DiscardEvidence') -ShouldPass $false
    Check ($second.Contains('no owner')) 'repeat cleanup is safely rejected'

    "PASS: $script:Checks W01 path and cleanup assertions in isolated synthetic tree"
} finally {
    $base = [IO.Path]::GetFullPath($script:TestRoot)
    $runsBase = [IO.Path]::GetFullPath($script:Runs).TrimEnd([IO.Path]::DirectorySeparatorChar)
    if (-not $base.StartsWith(($runsBase + [IO.Path]::DirectorySeparatorChar), [StringComparison]::OrdinalIgnoreCase) -or
        [IO.Path]::GetFileName($base) -notlike 'W01-test-*') {
        throw 'Test cleanup path failed containment check'
    }
    if ([IO.Directory]::Exists($base)) { Remove-Item -LiteralPath $base -Recurse -Force }
}
#Requires -Version 7.3
