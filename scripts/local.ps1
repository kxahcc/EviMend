param(
    [Parameter(Position = 0, Mandatory = $true)]
    [ValidateSet('Status', 'Prepare', 'Clean')][string]$Action,
    [ValidateSet('cache', 'work', 'env', 'all')][string]$Scope = 'cache',
    [switch]$Apply,
    [string]$PlanId,
    [switch]$DiscardEvidence
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$script:Repo = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..')).TrimEnd([IO.Path]::DirectorySeparatorChar)
$script:PolicyPath = Join-Path $script:Repo 'configs/local-runtime.json'
$script:Policy = Get-Content -LiteralPath $script:PolicyPath -Raw -Encoding UTF8 | ConvertFrom-Json
if ($script:Policy.schema_version -ne 1 -or $script:Policy.paths.managed_root -cne '.dfx-local' -or $script:Policy.paths.export_root -cne 'runs/exports') {
    throw 'Unsupported local runtime policy or managed path'
}
if ($script:Policy.execution.require_windows_job_limits -ne $true -or $script:Policy.cleanup.default_dry_run -ne $true) {
    throw 'Local runtime safety settings cannot be disabled'
}
$script:Root = [IO.Path]::GetFullPath((Join-Path $script:Repo '.dfx-local'))
$script:OwnerPath = Join-Path $script:Root 'owner.json'
$script:StatePath = Join-Path $script:Root 'state'
$script:PlanPath = Join-Path $script:StatePath 'cleanup-plan.json'
$script:ExportPath = Join-Path $script:Repo 'runs/exports'
$script:Comparison = [StringComparison]::OrdinalIgnoreCase

function Test-Within([string]$Path, [string]$Parent) {
    $full = [IO.Path]::GetFullPath($Path)
    $base = [IO.Path]::GetFullPath($Parent).TrimEnd([IO.Path]::DirectorySeparatorChar)
    return $full.StartsWith($base + [IO.Path]::DirectorySeparatorChar, $script:Comparison)
}

function Assert-PathChain([string]$Path) {
    $full = [IO.Path]::GetFullPath($Path)
    if (-not (Test-Within $full $script:Repo)) { throw "Path outside repository: $full" }
    $item = Get-Item -LiteralPath $script:Repo -Force
    if (($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) { throw 'Repository is a reparse point' }
    $relative = $full.Substring($script:Repo.Length).TrimStart([IO.Path]::DirectorySeparatorChar)
    $cursor = $script:Repo
    foreach ($segment in ($relative -split '[\\/]')) {
        if ($segment.Length -eq 0) { continue }
        $cursor = Join-Path $cursor $segment
        if ([IO.File]::Exists($cursor) -or [IO.Directory]::Exists($cursor) -or (Test-Path -LiteralPath $cursor)) {
            $part = Get-Item -LiteralPath $cursor -Force
            if (($part.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0 -or $null -ne $part.LinkType) {
                throw "Reparse point in managed path: $cursor"
            }
        }
    }
}

function Get-Mutex() {
    $bytes = [Security.Cryptography.SHA256]::HashData([Text.Encoding]::UTF8.GetBytes($script:Repo.ToUpperInvariant()))
    $name = 'Local\DFX-' + [Convert]::ToHexString($bytes).Substring(0, 24)
    return [Threading.Mutex]::new($false, $name)
}

function Read-Owner() {
    Assert-PathChain $script:OwnerPath
    if (-not [IO.File]::Exists($script:OwnerPath)) { throw 'Managed root has no owner; refusing cleanup' }
    $owner = Get-Content -LiteralPath $script:OwnerPath -Raw -Encoding UTF8 | ConvertFrom-Json
    if ($owner.schema_version -ne 1 -or $owner.repo_real_path -cne $script:Repo -or
        $owner.managed_real_path -cne $script:Root -or
        -not [Guid]::TryParse([string]$owner.project_instance_id, [ref][Guid]::Empty)) {
        throw 'Managed root owner does not match this repository'
    }
    return $owner
}

function Get-Members([string]$Target) {
    $entries = [Collections.Generic.List[object]]::new()
    if (-not [IO.Directory]::Exists($Target)) { return @() }
    Assert-PathChain $Target
    $pending = [Collections.Generic.Stack[string]]::new()
    $pending.Push($Target)
    while ($pending.Count -gt 0) {
        $folder = $pending.Pop()
        foreach ($entry in [IO.DirectoryInfo]::new($folder).EnumerateFileSystemInfos()) {
            Assert-PathChain $entry.FullName
            $isDir = ($entry.Attributes -band [IO.FileAttributes]::Directory) -ne 0
            $relative = $entry.FullName.Substring($script:Root.Length).TrimStart([IO.Path]::DirectorySeparatorChar)
            if ($relative -in @('state\cleanup-plan.json', 'state/cleanup-plan.json')) { continue }
            $length = if ($isDir) { 0L } else { [long]([IO.FileInfo]$entry).Length }
            $entries.Add([pscustomobject]@{
                path = $relative; directory = $isDir; bytes = $length
                modified_utc_ticks = if ($isDir) { 0L } else { $entry.LastWriteTimeUtc.Ticks }
            })
            if ($isDir) { $pending.Push($entry.FullName) }
        }
    }
    return @($entries | Sort-Object -Property path -CaseSensitive)
}

function Get-Fingerprint([string[]]$Targets) {
    $records = [Collections.Generic.List[string]]::new()
    [long]$bytes = 0
    foreach ($target in $Targets) {
        Assert-PathChain $target
        foreach ($member in @(Get-Members $target)) {
            $records.Add("$($member.path)|$($member.directory)|$($member.bytes)|$($member.modified_utc_ticks)")
            $bytes += $member.bytes
        }
    }
    $sha = [Security.Cryptography.SHA256]::HashData([Text.Encoding]::UTF8.GetBytes(($records -join "`n")))
    return [pscustomobject]@{ hash = [Convert]::ToHexString($sha); bytes = $bytes; entries = $records.Count }
}

function Get-Targets([string]$SelectedScope) {
    $children = switch ($SelectedScope) {
        'cache' { @('cache', 'tmp') }
        'work' { @('work') }
        'env' { @('tools', 'python', 'env') }
        'all' { @('.') }
        default { throw 'Unsupported cleanup scope' }
    }
    $targets = foreach ($child in $children) {
        $resolved = [IO.Path]::GetFullPath((Join-Path $script:Root $child))
        if ($SelectedScope -ne 'all' -and -not (Test-Within $resolved $script:Root)) { throw 'Invalid cleanup target' }
        if ($SelectedScope -eq 'all' -and $resolved -cne $script:Root) { throw 'Invalid all target' }
        Assert-PathChain $resolved
        if ([IO.Directory]::Exists($resolved)) { $resolved }
    }
    return @($targets)
}

function Assert-Quiescent([string]$SelectedScope, [bool]$AllowDiscard) {
    foreach ($marker in @('active.json', 'remote-jobs.json')) {
        if ([IO.File]::Exists((Join-Path $script:StatePath $marker))) {
            throw "Unresolved activity marker blocks cleanup: $marker"
        }
    }
    if ($SelectedScope -in @('work', 'all') -and [IO.Directory]::Exists((Join-Path $script:Root 'work'))) {
        foreach ($run in [IO.DirectoryInfo]::new((Join-Path $script:Root 'work')).EnumerateDirectories()) {
            Assert-PathChain $run.FullName
            $marker = Join-Path $run.FullName 'run-state.json'
            if (-not [IO.File]::Exists($marker)) { throw "Work item has no terminal state: $($run.Name)" }
            $state = Get-Content -LiteralPath $marker -Raw -Encoding UTF8 | ConvertFrom-Json
            if ($state.status -notin @('finished', 'failed', 'canceled')) {
                throw "Work item is not terminal: $($run.Name)"
            }
        }
    }
    if ($SelectedScope -eq 'all' -and -not $AllowDiscard) {
        $artifacts = Join-Path $script:Root 'artifacts'
        $hasArtifacts = [IO.Directory]::Exists($artifacts) -and @([IO.Directory]::EnumerateFileSystemEntries($artifacts)).Count -gt 0
        $hasState = [IO.Directory]::Exists($script:StatePath) -and @([IO.Directory]::EnumerateFileSystemEntries($script:StatePath) | Where-Object { [IO.Path]::GetFileName($_) -ne 'cleanup-plan.json' }).Count -gt 0
        if ($hasArtifacts -or $hasState) { throw 'Evidence remains; export it or explicitly use -DiscardEvidence' }
    }
}

function Remove-VerifiedTree([string]$Target) {
    Assert-PathChain $Target
    if (-not [IO.Directory]::Exists($Target)) { return }
    foreach ($entry in [IO.DirectoryInfo]::new($Target).EnumerateFileSystemInfos()) {
        Assert-PathChain $entry.FullName
        if (($entry.Attributes -band [IO.FileAttributes]::Directory) -ne 0) {
            Remove-VerifiedTree $entry.FullName
        } else {
            [IO.File]::Delete($entry.FullName)
        }
    }
    [IO.Directory]::Delete($Target, $false)
}

if ($Action -eq 'Status') {
    Assert-PathChain $script:Root
    $ownerValid = $false
    if ([IO.Directory]::Exists($script:Root)) {
        try { $null = Read-Owner; $ownerValid = $true } catch { $ownerValid = $false }
    }
    $sizes = @{}
    foreach ($name in @('tools', 'python', 'env', 'cache', 'tmp', 'work', 'artifacts', 'state')) {
        $folder = Join-Path $script:Root $name
        $sizes[$name] = if ([IO.Directory]::Exists($folder)) { (Get-Fingerprint @($folder)).bytes } else { 0L }
    }
    $exportBytes = if ([IO.Directory]::Exists($script:ExportPath)) {
        [long](@([IO.DirectoryInfo]::new($script:ExportPath).EnumerateFiles('*', [IO.SearchOption]::AllDirectories) | Measure-Object -Property Length -Sum)[0].Sum)
    } else { 0L }
    [pscustomobject]@{
        managed_root = $script:Root; owner_valid = $ownerValid
        managed_bytes_by_category = $sizes; export_bytes = $exportBytes
        active_marker = [IO.File]::Exists((Join-Path $script:StatePath 'active.json'))
        remote_jobs_unknown = [IO.File]::Exists((Join-Path $script:StatePath 'remote-jobs.json'))
        resource_enforcement = 'not_implemented'; cleanup_available = $true
    } | ConvertTo-Json -Depth 5
    exit 0
}

$mutex = Get-Mutex
$locked = $false
try {
    try { $locked = $mutex.WaitOne(0) } catch [Threading.AbandonedMutexException] { $locked = $true }
    if (-not $locked) { throw 'Managed root is busy' }
    Assert-PathChain $script:Root

    if ($Action -eq 'Prepare') {
        if ([IO.Directory]::Exists($script:Root)) {
            $owner = Read-Owner
        } else {
            [IO.Directory]::CreateDirectory($script:Root) | Out-Null
            [IO.Directory]::CreateDirectory($script:StatePath) | Out-Null
            $owner = [pscustomobject]@{
                schema_version = 1; project_instance_id = [Guid]::NewGuid().ToString()
                repo_real_path = $script:Repo; managed_real_path = $script:Root
                created_at = [DateTimeOffset]::UtcNow.ToString('o')
            }
            $owner | ConvertTo-Json -Depth 5 | Set-Content -LiteralPath $script:OwnerPath -Encoding UTF8
        }
        [pscustomobject]@{ project_instance_id = $owner.project_instance_id; prepared = $true; runtime_installed = $false } | ConvertTo-Json
        exit 0
    }

    $owner = Read-Owner
    Assert-Quiescent $Scope ([bool]$DiscardEvidence)
    if (-not $Apply) {
        $targets = @(Get-Targets $Scope)
        $fingerprint = Get-Fingerprint $targets
        $plan = [pscustomobject]@{
            schema_version = 1; plan_id = [Guid]::NewGuid().ToString(); project_instance_id = $owner.project_instance_id
            scope = $Scope; discard_evidence = [bool]$DiscardEvidence
            fingerprint = $fingerprint.hash; estimated_bytes = $fingerprint.bytes
            target_count = $targets.Count; created_at = [DateTimeOffset]::UtcNow.ToString('o')
        }
        [IO.Directory]::CreateDirectory($script:StatePath) | Out-Null
        $plan | ConvertTo-Json -Depth 6 | Set-Content -LiteralPath $script:PlanPath -Encoding UTF8
        [pscustomobject]@{ plan_id = $plan.plan_id; scope = $Scope; estimated_bytes = $plan.estimated_bytes
            target_count = $plan.target_count; preserved = @('data_B', 'archive', 'runs/exports', 'tracked files')
            dry_run = $true; apply_requires_plan_id = $true } | ConvertTo-Json -Depth 4
        exit 0
    }

    if (-not [Guid]::TryParse($PlanId, [ref][Guid]::Empty)) { throw 'A valid -PlanId is required' }
    if (-not [IO.File]::Exists($script:PlanPath)) { throw 'No cleanup plan exists' }
    $plan = Get-Content -LiteralPath $script:PlanPath -Raw -Encoding UTF8 | ConvertFrom-Json
    if ($plan.schema_version -ne 1 -or $plan.plan_id -cne $PlanId -or
        $plan.project_instance_id -cne $owner.project_instance_id -or $plan.scope -cne $Scope -or
        $plan.discard_evidence -ne [bool]$DiscardEvidence) { throw 'Cleanup plan no longer matches request' }
    $age = [DateTimeOffset]::UtcNow - [DateTimeOffset]::Parse($plan.created_at)
    if ($age.TotalMinutes -lt 0 -or $age.TotalMinutes -gt 10) { throw 'Cleanup plan expired' }
    $targets = @(Get-Targets $Scope)
    $fingerprint = Get-Fingerprint $targets
    if ($plan.fingerprint -cne $fingerprint.hash -or $plan.estimated_bytes -ne $fingerprint.bytes -or
        $plan.target_count -ne $targets.Count) { throw 'Cleanup targets changed since planning' }
    Assert-Quiescent $Scope ([bool]$DiscardEvidence)
    $deleted = [Collections.Generic.List[string]]::new()
    try {
        foreach ($target in $targets) {
            Remove-VerifiedTree $target
            $deleted.Add($target)
        }
    } catch {
        throw "Cleanup incomplete; fully removed targets: $(@($deleted) -join ', '); error: $($_.Exception.Message)"
    }
    if ($Scope -eq 'all') {
        [IO.Directory]::CreateDirectory($script:ExportPath) | Out-Null
        $receiptPath = Join-Path $script:ExportPath ("cleanup-$($plan.plan_id).json")
        [pscustomobject]@{ plan_id = $plan.plan_id; scope = $Scope; removed = @($deleted)
            finished_at = [DateTimeOffset]::UtcNow.ToString('o'); preserved = @('data_B', 'archive', 'runs/exports', 'tracked files')
        } | ConvertTo-Json -Depth 4 | Set-Content -LiteralPath $receiptPath -Encoding UTF8
    } else {
        Remove-Item -LiteralPath $script:PlanPath -Force
    }
    [pscustomobject]@{ plan_id = $plan.plan_id; applied = $true; removed = @($deleted)
        preserved = @('data_B', 'archive', 'runs/exports', 'tracked files') } | ConvertTo-Json -Depth 4
} finally {
    if ($locked) { $mutex.ReleaseMutex() }
    $mutex.Dispose()
}
#Requires -Version 7.3
