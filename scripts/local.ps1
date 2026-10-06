param(
    [Parameter(Position = 0, Mandatory = $true)]
    [ValidateSet('Status', 'Prepare', 'Clean', 'Run', 'Stop', 'Init', 'Export')][string]$Action,
    [ValidateSet('cache', 'testcache', 'tmp', 'work', 'env', 'all')][string]$Scope = 'cache',
    [switch]$Apply,
    [string]$PlanId,
    [switch]$DiscardEvidence,
    [ValidateSet('synthetic', 'test', 'docs', 'ruff', 'pyright', 'offline', 'aggregate')][string]$Task = 'synthetic',
    [string]$TestPath,
    [string]$OfflineTaskPath,
    [string]$OfflineScriptPath,
    [string]$OfflineWorkId,
    [string]$EvaluationManifestPath,
    [string]$EvaluationWorkId,
    [string]$ExportId,
    [switch]$Verify,
    [switch]$Resume,
    [ValidateSet('idle', 'child', 'noisy', 'memory')][string]$Scenario = 'idle',
    [ValidateRange(1, 120)][int]$Seconds = 3,
    [ValidateRange(1, 1800)][int]$TimeoutSeconds = 1800
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$script:Repo = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..')).TrimEnd([IO.Path]::DirectorySeparatorChar)
$script:PolicyPath = Join-Path $script:Repo 'configs/local-runtime.json'
$script:Policy = Get-Content -LiteralPath $script:PolicyPath -Raw -Encoding UTF8 | ConvertFrom-Json
if ($script:Policy.schema_version -ne 1 -or $script:Policy.paths.managed_root -cne '.dfx-local' -or $script:Policy.paths.export_root -cne 'runs/exports') {
    throw 'Unsupported local runtime policy or managed path'
}
if ($script:Policy.execution.application_target -cne 'local' -or
    $script:Policy.execution.require_windows_job_limits -ne $true -or
    $script:Policy.cleanup.default_dry_run -ne $true -or
    $script:Policy.cleanup.refuse_external_reparse_points -ne $true) {
    throw 'Local runtime safety settings cannot be disabled'
}
if ($script:Policy.enforcement_status -cne 'offline_agent_guarded') { throw 'Unsupported enforcement status' }
$script:Root = [IO.Path]::GetFullPath((Join-Path $script:Repo '.dfx-local'))
$script:OwnerPath = Join-Path $script:Root 'owner.json'
$script:StatePath = Join-Path $script:Root 'state'
$script:PlanPath = Join-Path $script:StatePath 'cleanup-plan.json'
$script:ExportPath = Join-Path $script:Repo 'runs/exports'
$script:ActivePath = Join-Path $script:StatePath 'active.json'
$script:StopPath = Join-Path $script:StatePath 'stop-request.json'
$script:Comparison = [StringComparison]::OrdinalIgnoreCase
$script:InaccessibleManagedPaths = [Collections.Generic.List[string]]::new()

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
            if (($part.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0 -or
                ($null -ne $part.LinkType -and $part.LinkType -cne 'HardLink')) {
                throw "Reparse point in managed path: $cursor"
            }
        }
    }
}

function Resolve-EvaluationManifest([string]$Path) {
    if (-not $Path -or [IO.Path]::IsPathRooted($Path) -or $Path -match '[:\x00-\x1f\x7f]') {
        throw 'EvaluationManifestPath must be a canonical repository-relative path'
    }
    $segments = @($Path -split '[\\/]')
    foreach ($segment in $segments) {
        if ($segment -in @('', '.', '..', 'data_B', 'archive', '.git', '.agents', '.codex') -or
            $segment -ne $segment.TrimEnd(' ', '.')) {
            throw 'Evaluation manifest path has an unsafe or forbidden component'
        }
    }
    $resolved = [IO.Path]::GetFullPath((Join-Path $script:Repo $Path))
    Assert-PathChain $resolved
    if (-not [IO.File]::Exists($resolved)) { throw 'Evaluation manifest is missing' }
    $item = Get-Item -LiteralPath $resolved -Force
    if ($item -isnot [IO.FileInfo] -or $null -ne $item.LinkType) {
        throw 'Evaluation manifest must be an unlinked regular file'
    }
    return $resolved
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

function Get-InternalLinkTarget([IO.FileSystemInfo]$Entry) {
    $resolved = $Entry.ResolveLinkTarget($true)
    if ($null -eq $resolved) { throw "Unresolved reparse point: $($Entry.FullName)" }
    $target = [IO.Path]::GetFullPath($resolved.FullName)
    if (-not (Test-Within $target $script:Root)) { throw "Reparse point outside managed root: $($Entry.FullName)" }
    Assert-PathChain $target
    return $target
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
            $isLink = ($entry.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0
            if ($isLink) { $linkTarget = Get-InternalLinkTarget $entry }
            else { Assert-PathChain $entry.FullName; $linkTarget = $null }
            $isDir = ($entry.Attributes -band [IO.FileAttributes]::Directory) -ne 0
            $relative = $entry.FullName.Substring($script:Root.Length).TrimStart([IO.Path]::DirectorySeparatorChar)
            if ($relative -in @('state\cleanup-plan.json', 'state/cleanup-plan.json')) { continue }
            $length = if ($isDir -or $isLink) { 0L } else { [long]([IO.FileInfo]$entry).Length }
            $entries.Add([pscustomobject]@{
                path = $relative; directory = $isDir; bytes = $length
                link_target = $linkTarget
                modified_utc_ticks = if ($isDir -or $isLink) { 0L } else { $entry.LastWriteTimeUtc.Ticks }
            })
            if ($isDir -and -not $isLink) { $pending.Push($entry.FullName) }
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
            $records.Add("$($member.path)|$($member.directory)|$($member.link_target)|$($member.bytes)|$($member.modified_utc_ticks)")
            $bytes += $member.bytes
        }
    }
    $sha = [Security.Cryptography.SHA256]::HashData([Text.Encoding]::UTF8.GetBytes(($records -join "`n")))
    return [pscustomobject]@{ hash = [Convert]::ToHexString($sha); bytes = $bytes; entries = $records.Count }
}

function Get-ManagedBytes([string[]]$Targets = @(), [bool]$VerifyLinks = $false) {
    Assert-PathChain $script:Root
    [long]$bytes = 0
    $pending = [Collections.Generic.Stack[string]]::new()
    if ($Targets.Count -eq 0) {
        $Targets = @('tools', 'python', 'env', 'cache', 'tmp', 'work', 'artifacts', 'state') |
            ForEach-Object { Join-Path $script:Root $_ }
    }
    foreach ($folder in $Targets) {
        Assert-PathChain $folder
        if ([IO.Directory]::Exists($folder)) { $pending.Push($folder) }
    }
    while ($pending.Count -gt 0) {
        $folder = $pending.Pop()
        try { $entries = @([IO.DirectoryInfo]::new($folder).EnumerateFileSystemInfos()) }
        catch [IO.DirectoryNotFoundException] { continue }
        catch [UnauthorizedAccessException] {
            $script:InaccessibleManagedPaths.Add($folder)
            continue
        }
        foreach ($entry in $entries) {
            try {
                $entry.Refresh()
                if (-not $entry.Exists) { continue }
                if (($entry.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
                    if ($VerifyLinks) { $null = Get-InternalLinkTarget $entry }
                    continue
                }
                if (($entry.Attributes -band [IO.FileAttributes]::Directory) -ne 0) { $pending.Push($entry.FullName) }
                else { $bytes += [long]([IO.FileInfo]$entry).Length }
            } catch [IO.FileNotFoundException] { continue }
            catch [IO.DirectoryNotFoundException] { continue }
            catch [UnauthorizedAccessException] {
                $script:InaccessibleManagedPaths.Add($entry.FullName)
                continue
            }
        }
    }
    return $bytes
}

function Get-ExportBytes() {
    Assert-PathChain $script:ExportPath
    [long]$bytes = 0
    if (-not [IO.Directory]::Exists($script:ExportPath)) { return $bytes }
    $pending = [Collections.Generic.Stack[string]]::new()
    $pending.Push($script:ExportPath)
    while ($pending.Count -gt 0) {
        $folder = $pending.Pop()
        Assert-PathChain $folder
        foreach ($entry in [IO.DirectoryInfo]::new($folder).EnumerateFileSystemInfos()) {
            $item = Get-Item -LiteralPath $entry.FullName -Force
            if (($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0 -or $null -ne $item.LinkType) {
                throw 'Export directory contains a link; refusing resource accounting'
            }
            Assert-PathChain $item.FullName
            if ($item -is [IO.DirectoryInfo]) { $pending.Push($item.FullName) }
            elseif ($item -is [IO.FileInfo]) { $bytes += [long]$item.Length }
            else { throw 'Export directory contains an unsupported entry' }
        }
    }
    return $bytes
}

function Get-Targets([string]$SelectedScope) {
    $children = switch ($SelectedScope) {
        'cache' { @('cache', 'tmp') }
        'testcache' { @('cache/pytest', 'cache/pycache') }
        'tmp' { @('tmp') }
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
        if (-not $AllowDiscard -and @([IO.Directory]::EnumerateFileSystemEntries((Join-Path $script:Root 'work'))).Count -gt 0) {
            throw 'Evidence remains in work; review an export and explicitly use -DiscardEvidence'
        }
    }
    if ($SelectedScope -in @('cache', 'tmp', 'all') -and -not $AllowDiscard) {
        $temporary = Join-Path $script:Root 'tmp'
        if ([IO.Directory]::Exists($temporary)) {
            $pending = [Collections.Generic.Stack[string]]::new()
            $pending.Push($temporary)
            while ($pending.Count -gt 0) {
                $folder = $pending.Pop()
                Assert-PathChain $folder
                foreach ($entry in [IO.DirectoryInfo]::new($folder).EnumerateFileSystemInfos()) {
                    Assert-PathChain $entry.FullName
                    if ($entry.Name -in @('task.json', 'source-manifest.json', 'package.json') -or
                        ($entry -is [IO.DirectoryInfo] -and $entry.Name -like 'ex-*')) {
                        throw 'Evidence remains in tmp; review it and explicitly use -DiscardEvidence'
                    }
                    if (($entry.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
                        throw 'Reparse point blocks temporary evidence inspection'
                    }
                    if ($entry -is [IO.DirectoryInfo]) { $pending.Push($entry.FullName) }
                }
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
        $isLink = ($entry.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0
        if ($isLink) { $null = Get-InternalLinkTarget $entry }
        else { Assert-PathChain $entry.FullName }
        if ($isLink -and ($entry.Attributes -band [IO.FileAttributes]::Directory) -ne 0) {
            [IO.Directory]::Delete($entry.FullName, $false)
        } elseif ($isLink) {
            [IO.File]::Delete($entry.FullName)
        } elseif (($entry.Attributes -band [IO.FileAttributes]::Directory) -ne 0) {
            Remove-VerifiedTree $entry.FullName
        } else {
            [IO.File]::Delete($entry.FullName)
        }
    }
    [IO.Directory]::Delete($Target, $false)
}

function Remove-VerifiedLinks([string]$Target) {
    Assert-PathChain $Target
    if (-not [IO.Directory]::Exists($Target)) { return }
    foreach ($entry in [IO.DirectoryInfo]::new($Target).EnumerateFileSystemInfos()) {
        if (($entry.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
            $null = Get-InternalLinkTarget $entry
            if (($entry.Attributes -band [IO.FileAttributes]::Directory) -ne 0) {
                [IO.Directory]::Delete($entry.FullName, $false)
            } else { [IO.File]::Delete($entry.FullName) }
        } else {
            Assert-PathChain $entry.FullName
            if (($entry.Attributes -band [IO.FileAttributes]::Directory) -ne 0) {
                Remove-VerifiedLinks $entry.FullName
            }
        }
    }
}

function Get-VerifiedManager($Marker) {
    if ($Marker.schema_version -ne 1 -or $Marker.project_instance_id -cne (Read-Owner).project_instance_id -or
        -not [Guid]::TryParse([string]$Marker.run_id, [ref][Guid]::Empty) -or
        $Marker.manager_pid -lt 1 -or $Marker.manager_start_utc_ticks -lt 1 -or
        $Marker.child_pid -lt 1 -or $Marker.child_start_utc_ticks -lt 1) {
        throw 'Active marker identity is invalid'
    }
    $process = Get-Process -Id ([int]$Marker.manager_pid) -ErrorAction SilentlyContinue
    if ($null -eq $process) { return $null }
    if ($process.StartTime.ToUniversalTime().Ticks -ne [long]$Marker.manager_start_utc_ticks -or
        $process.Path -cne [string]$Marker.manager_executable) { throw 'Manager PID identity mismatch; refusing Stop' }
    return $process
}

function Get-RunLimits() {
    $limits = $script:Policy.limits
    if ($limits.max_concurrent_runs -ne 1 -or $limits.max_active_processes -lt 1 -or
        $limits.cpu_rate_percent -lt 1 -or $limits.cpu_rate_percent -gt 100 -or
        $limits.job_memory_cap_bytes -lt 16777216 -or
        $limits.job_memory_fraction_of_physical -le 0 -or $limits.job_memory_fraction_of_physical -gt 1 -or
        $limits.monitor_interval_seconds -lt 1 -or $limits.monitor_interval_seconds -gt 30 -or
        $limits.default_wall_timeout_seconds -lt 1 -or $limits.stop_grace_seconds -lt 1 -or
        $limits.max_log_file_bytes -lt 1024 -or $limits.max_run_log_bytes -lt $limits.max_log_file_bytes) {
        throw 'Invalid local resource policy'
    }
    $physical = [DfxLocal.BoundedJob]::PhysicalMemory()
    [ulong]$memoryCap = [Math]::Min([double]$limits.job_memory_cap_bytes,
        [Math]::Floor([double]$physical[0] * [double]$limits.job_memory_fraction_of_physical))
    if ($memoryCap -lt 16777216 -or [double]$physical[1] -lt ([double]$memoryCap + [double]$limits.min_free_memory_bytes)) {
        throw 'Insufficient free physical memory for Job and reserve'
    }
    $drive = [IO.DriveInfo]::new([IO.Path]::GetPathRoot($script:Root))
    [double]$diskReserve = [Math]::Max([double]$limits.min_free_disk_bytes,
        [Math]::Ceiling([double]$drive.TotalSize * [double]$limits.min_free_disk_fraction))
    if ($drive.AvailableFreeSpace -lt $diskReserve) { throw 'Insufficient free disk for reserve' }
    [long]$managedBytes = Get-ManagedBytes -VerifyLinks $true
    if ($script:InaccessibleManagedPaths.Count -gt 0) {
        throw 'Managed resource accounting is incomplete because a managed path is not readable'
    }
    [long]$exportBytes = Get-ExportBytes
    if (($managedBytes + $exportBytes) -gt [long]$limits.managed_disk_budget_bytes) { throw 'Managed and export disk budget exceeded' }
    return [pscustomobject]@{
        cpu_percent = [int]$limits.cpu_rate_percent; max_processes = [uint32]$limits.max_active_processes
        memory_bytes = $memoryCap; disk_reserve_bytes = [long]$diskReserve
        disk_budget_bytes = [long]$limits.managed_disk_budget_bytes
        min_free_memory_bytes = [long]$limits.min_free_memory_bytes
        max_log_file_bytes = [long]$limits.max_log_file_bytes
        monitor_seconds = [int]$limits.monitor_interval_seconds
        timeout_seconds = [Math]::Min($TimeoutSeconds, [int]$limits.default_wall_timeout_seconds)
    }
}

$aggregateManifest = $null; $aggregateWork = $null
$exportManifest = $null; $exportPlan = $null; $exportPackage = $null
if ($Action -eq 'Export') {
    if ($PSBoundParameters.ContainsKey('Task') -or $TestPath -or $OfflineTaskPath -or $OfflineScriptPath -or
        $OfflineWorkId -or $Resume -or $EvaluationWorkId -or $DiscardEvidence -or
        $PSBoundParameters.ContainsKey('Scope') -or $PSBoundParameters.ContainsKey('Scenario')) {
        throw 'Export does not accept task, test, offline, resume, cleanup or aggregate work parameters'
    }
    if ($ExportId -cnotmatch '^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$') { throw 'ExportId must be a safe ID' }
    $exportPlan = Join-Path $script:StatePath ('export-plan-' + $ExportId + '.json')
    $exportPackage = Join-Path $script:ExportPath $ExportId
    Assert-PathChain $exportPlan
    Assert-PathChain $exportPackage
    if ($Verify) {
        if ($EvaluationManifestPath -or $Apply -or $PlanId) { throw 'Export verification accepts only ExportId' }
        if (-not [IO.Directory]::Exists($exportPackage)) { throw 'Export package is missing' }
    } else {
        if ($Apply) {
            if ($PlanId -cnotmatch '^[a-f0-9]{64}$') { throw 'Applying an export requires the reviewed lowercase SHA256 PlanId' }
            if (-not [IO.File]::Exists($exportPlan)) { throw 'Reviewed export plan is missing' }
        } elseif ($PlanId) { throw 'Export PlanId requires -Apply' }
        $exportManifest = Resolve-EvaluationManifest $EvaluationManifestPath
        if (Test-Path -LiteralPath $exportPackage) { throw 'Export target already exists; choose a new ID' }
    }
} elseif ($PSBoundParameters.ContainsKey('ExportId') -or $PSBoundParameters.ContainsKey('Verify')) {
    throw 'Export parameters require the Export action'
}
if ($PSBoundParameters.ContainsKey('EvaluationManifestPath') -or
    $PSBoundParameters.ContainsKey('EvaluationWorkId') -or $Task -eq 'aggregate') {
    if ($Action -ne 'Export') {
        if ($Action -ne 'Run' -or $Task -ne 'aggregate') { throw 'Evaluation parameters require Run -Task aggregate' }
        if ($TestPath -or $OfflineTaskPath -or $OfflineScriptPath -or $OfflineWorkId -or $Resume) {
            throw 'Aggregate does not accept test, offline or resume parameters'
        }
        if ($EvaluationWorkId -cnotmatch '^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$') {
            throw 'EvaluationWorkId must be a new safe ID'
        }
        $aggregateManifest = Resolve-EvaluationManifest $EvaluationManifestPath
        $aggregateWork = Join-Path $script:Root ('work/aggregate-' + $EvaluationWorkId)
        Assert-PathChain $aggregateWork
        if (Test-Path -LiteralPath $aggregateWork) { throw 'Aggregate work already exists; choose a new ID' }
    }
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
        $sizes[$name] = if ([IO.Directory]::Exists($folder)) { Get-ManagedBytes @($folder) } else { 0L }
    }
    $exportBytes = Get-ExportBytes
    $activeState = 'none'
    if ([IO.File]::Exists($script:ActivePath)) {
        try {
            $marker = Get-Content -LiteralPath $script:ActivePath -Raw -Encoding UTF8 | ConvertFrom-Json
            $manager = Get-VerifiedManager $marker
            $activeState = if ($null -eq $manager) { 'stale_requires_stop' } else { 'running' }
        } catch { $activeState = 'identity_unverified' }
    }
    [pscustomobject]@{
        managed_root = $script:Root; owner_valid = $ownerValid
        managed_bytes_by_category = $sizes; export_bytes = $exportBytes
        resource_accounting = if ($script:InaccessibleManagedPaths.Count -eq 0) { 'complete' } else { 'incomplete' }
        inaccessible_paths = @($script:InaccessibleManagedPaths | Sort-Object -Unique)
        active_marker = [IO.File]::Exists((Join-Path $script:StatePath 'active.json'))
        active_state = $activeState
        remote_jobs_unknown = [IO.File]::Exists((Join-Path $script:StatePath 'remote-jobs.json'))
        resource_enforcement = 'job_guarded_setup_quality_offline_agent_metrics_and_export'
        cleanup_available = $activeState -eq 'none' -and
            $script:InaccessibleManagedPaths.Count -eq 0 -and
            -not [IO.File]::Exists((Join-Path $script:StatePath 'remote-jobs.json'))
    } | ConvertTo-Json -Depth 5
    exit 0
}

if ($Action -eq 'Stop') {
    Assert-PathChain $script:ActivePath
    if (-not [IO.File]::Exists($script:ActivePath)) {
        [pscustomobject]@{ stopped = $true; active_run = $false; remote_jobs_unknown = [IO.File]::Exists((Join-Path $script:StatePath 'remote-jobs.json')) } | ConvertTo-Json
        exit 0
    }
    $marker = Get-Content -LiteralPath $script:ActivePath -Raw -Encoding UTF8 | ConvertFrom-Json
    $manager = Get-VerifiedManager $marker
    if ($null -ne $manager) {
        [pscustomobject]@{ schema_version = 1; run_id = $marker.run_id; requested_at = [DateTimeOffset]::UtcNow.ToString('o') } |
            ConvertTo-Json | Set-Content -LiteralPath $script:StopPath -Encoding UTF8
        $deadline = [Diagnostics.Stopwatch]::StartNew()
        while ($deadline.Elapsed.TotalSeconds -lt [int]$script:Policy.limits.stop_grace_seconds) {
            Start-Sleep -Milliseconds 200
            $manager = Get-VerifiedManager $marker
            if ($null -eq $manager) { break }
        }
        if ($null -ne $manager) {
            $manager = Get-VerifiedManager $marker
            if ($null -ne $manager) { Stop-Process -InputObject $manager -Force -ErrorAction Stop }
        }
    }
    $wait = [Diagnostics.Stopwatch]::StartNew()
    while ($wait.Elapsed.TotalSeconds -lt 5 -and $null -ne (Get-VerifiedManager $marker)) { Start-Sleep -Milliseconds 100 }
    if ($null -ne (Get-VerifiedManager $marker)) { throw 'Manager did not exit after Stop' }
    if ([IO.File]::Exists($script:ActivePath)) {
        $current = Get-Content -LiteralPath $script:ActivePath -Raw -Encoding UTF8 | ConvertFrom-Json
        if ($current.run_id -cne $marker.run_id) { throw 'Active run changed during Stop' }
        if ($current.child_pid -gt 0) {
            $child = Get-Process -Id ([int]$current.child_pid) -ErrorAction SilentlyContinue
            if ($null -ne $child -and $child.StartTime.ToUniversalTime().Ticks -eq [long]$current.child_start_utc_ticks) {
                throw 'Managed child survived manager exit; refusing to clear activity marker'
            }
        }
        Remove-Item -LiteralPath $script:ActivePath -Force
        if ([IO.File]::Exists($script:StopPath)) { Remove-Item -LiteralPath $script:StopPath -Force }
    }
    [pscustomobject]@{ stopped = $true; active_run = $false; run_id = $marker.run_id
        remote_jobs_unknown = [IO.File]::Exists((Join-Path $script:StatePath 'remote-jobs.json')) } | ConvertTo-Json
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
    if ($Action -in @('Run', 'Init', 'Export')) {
        if ([IO.File]::Exists($script:ActivePath)) { throw 'Unresolved active marker; use Stop before Run' }
        if ([IO.File]::Exists((Join-Path $script:StatePath 'remote-jobs.json'))) { throw 'Unresolved remote job marker blocks Run' }
        Assert-PathChain (Join-Path $script:Root 'tmp')
        Assert-PathChain (Join-Path $script:Root 'artifacts')
        [IO.Directory]::CreateDirectory((Join-Path $script:Root 'tmp')) | Out-Null
        [IO.Directory]::CreateDirectory((Join-Path $script:Root 'artifacts')) | Out-Null
        if ($Action -eq 'Export') {
            Assert-PathChain $script:ExportPath
            [IO.Directory]::CreateDirectory($script:ExportPath) | Out-Null
        }
        $savedTemp = $env:TEMP; $savedTmp = $env:TMP; $savedTestPath = $env:DFX_TEST_PATH
        $savedOfflineTask = $env:DFX_OFFLINE_TASK; $savedOfflineScript = $env:DFX_OFFLINE_SCRIPT
        $savedOfflineWork = $env:DFX_OFFLINE_WORK; $savedOfflineResume = $env:DFX_OFFLINE_RESUME
        $savedMetricsManifest = $env:DFX_METRICS_MANIFEST; $savedMetricsWork = $env:DFX_METRICS_WORK
        $savedExportManifest = $env:DFX_EXPORT_MANIFEST; $savedExportId = $env:DFX_EXPORT_ID
        $savedExportOwner = $env:DFX_EXPORT_OWNER; $savedExportPlan = $env:DFX_EXPORT_PLAN
        $savedExportApply = $env:DFX_EXPORT_APPLY; $savedExportPlanId = $env:DFX_EXPORT_PLAN_ID
        $savedExportVerify = $env:DFX_EXPORT_VERIFY; $savedExportPackage = $env:DFX_EXPORT_PACKAGE
        $savedExportRunId = $env:DFX_EXPORT_RUN_ID
        $job = $null; $markerCreated = $false
        $runId = [Guid]::NewGuid().ToString()
        $taskKind = if ($Action -eq 'Init') { 'bootstrap' } elseif ($Action -eq 'Export') { 'export' } elseif ($Task -ne 'synthetic') { $Task } else { $Scenario }
        $logPath = Join-Path $script:Root ("artifacts/$runId.log")
        $recordPath = Join-Path $script:Root ("artifacts/$runId.json")
        $outcome = 'failed'; $reason = $null; $childPid = 0
        try {
            $env:TEMP = Join-Path $script:Root 'tmp'; $env:TMP = $env:TEMP
            if (-not ('DfxLocal.BoundedJob' -as [type])) {
                Add-Type -TypeDefinition (Get-Content -LiteralPath (Join-Path $PSScriptRoot 'local_job.cs') -Raw -Encoding UTF8) -ErrorAction Stop
            }
            $effective = Get-RunLimits
            if ($taskKind -in @('test', 'docs', 'ruff', 'pyright', 'offline', 'aggregate', 'export')) {
                $environmentPath = Join-Path $script:StatePath 'environment.json'
                if (-not [IO.File]::Exists($environmentPath)) { throw 'Managed environment is not initialized' }
                $environment = Get-Content -LiteralPath $environmentPath -Raw -Encoding UTF8 | ConvertFrom-Json
                if ($environment.lock_sha256 -cne (Get-FileHash -LiteralPath (Join-Path $script:Repo 'uv.lock') -Algorithm SHA256).Hash -or
                    $environment.pyproject_sha256 -cne (Get-FileHash -LiteralPath (Join-Path $script:Repo 'pyproject.toml') -Algorithm SHA256).Hash -or
                    $environment.environment_python -cne (Join-Path $script:Root 'env/Scripts/python.exe')) {
                    throw 'Managed environment no longer matches lock or path'
                }
            }
            if ($taskKind -eq 'test') {
                $testRoot = [IO.Path]::GetFullPath((Join-Path $script:Repo 'tests'))
                $resolvedTest = [IO.Path]::GetFullPath((Join-Path $script:Repo $TestPath))
                if (-not (Test-Within $resolvedTest $testRoot) -or
                    [IO.Path]::GetFileName($resolvedTest) -cnotmatch '^test_[A-Za-z0-9_]+\.py$' -or
                    -not [IO.File]::Exists($resolvedTest)) { throw 'TestPath must name an existing tests/test_*.py file' }
                Assert-PathChain $resolvedTest
                $env:DFX_TEST_PATH = $resolvedTest
            } elseif ($Action -eq 'Run' -and $TestPath) { throw 'TestPath requires -Task test' }
            if ($taskKind -eq 'offline') {
                if ($OfflineWorkId -cnotmatch '^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$' -or
                    -not $OfflineTaskPath -or -not $OfflineScriptPath) { throw 'Offline task, script and work ID are required' }
                $resolvedTask = [IO.Path]::GetFullPath((Join-Path $script:Repo $OfflineTaskPath))
                $resolvedScript = [IO.Path]::GetFullPath((Join-Path $script:Repo $OfflineScriptPath))
                foreach ($path in @($resolvedTask, $resolvedScript)) {
                    if (-not (Test-Within $path $script:Repo) -or
                        (Test-Within $path (Join-Path $script:Repo 'data_B')) -or
                        (Test-Within $path (Join-Path $script:Repo 'archive'))) {
                        throw 'Offline inputs must be reviewed task/script paths within this repository, outside data_B/archive'
                    }
                    Assert-PathChain $path
                }
                if (-not [IO.Directory]::Exists($resolvedTask) -or -not [IO.File]::Exists($resolvedScript)) {
                    throw 'Offline task or script is missing'
                }
                $offlineWork = Join-Path $script:Root ("work/offline-" + $OfflineWorkId)
                Assert-PathChain $offlineWork
                if ($Resume -and -not [IO.Directory]::Exists($offlineWork)) { throw 'Offline work does not exist for resume' }
                if (-not $Resume -and [IO.Directory]::Exists($offlineWork)) { throw 'Offline work already exists' }
                $env:DFX_OFFLINE_TASK = $resolvedTask; $env:DFX_OFFLINE_SCRIPT = $resolvedScript
                $env:DFX_OFFLINE_WORK = $offlineWork; $env:DFX_OFFLINE_RESUME = if ($Resume) { '1' } else { '0' }
            } elseif ($OfflineTaskPath -or $OfflineScriptPath -or $OfflineWorkId -or $Resume) {
                throw 'Offline parameters require -Task offline'
            }
            if ($taskKind -eq 'aggregate') {
                $env:DFX_METRICS_MANIFEST = $aggregateManifest; $env:DFX_METRICS_WORK = $aggregateWork
            }
            if ($taskKind -eq 'export') {
                $env:DFX_EXPORT_MANIFEST = $exportManifest; $env:DFX_EXPORT_ID = $ExportId
                $env:DFX_EXPORT_OWNER = $owner.project_instance_id; $env:DFX_EXPORT_PLAN = $exportPlan
                $env:DFX_EXPORT_APPLY = if ($Apply) { '1' } else { '0' }; $env:DFX_EXPORT_PLAN_ID = $PlanId
                $env:DFX_EXPORT_VERIFY = if ($Verify) { '1' } else { '0' }; $env:DFX_EXPORT_PACKAGE = $exportPackage
                $env:DFX_EXPORT_RUN_ID = $runId
            }
            $job = [DfxLocal.BoundedJob]::new($effective.cpu_percent, $effective.memory_bytes, $effective.max_processes)
            $exe = (Get-Process -Id $PID).Path
            if (-not [IO.File]::Exists($exe)) { throw 'PowerShell executable identity unavailable' }
            $workerName = if ($Action -eq 'Init') { 'local_bootstrap.ps1' } elseif ($taskKind -eq 'test') { 'local_test.ps1' } elseif ($taskKind -in @('docs', 'ruff', 'pyright')) { 'local_quality.ps1' } elseif ($taskKind -eq 'offline') { 'local_agent.ps1' } elseif ($taskKind -eq 'aggregate') { 'local_metrics.ps1' } elseif ($taskKind -eq 'export') { 'local_export.ps1' } else { 'local_synthetic.ps1' }
            $workerPath = Join-Path $PSScriptRoot $workerName
            Assert-PathChain $workerPath
            $job.Start($exe, $workerPath, $taskKind, $logPath, $script:Repo, $Seconds,
                [long]$effective.max_log_file_bytes)
            $childPid = [int]$job.ProcessId
            $child = Get-Process -Id $childPid -ErrorAction Stop
            $self = Get-Process -Id $PID -ErrorAction Stop
            [pscustomobject]@{
                schema_version = 1; run_id = $runId; project_instance_id = $owner.project_instance_id
                manager_pid = $PID; manager_start_utc_ticks = $self.StartTime.ToUniversalTime().Ticks
                manager_executable = $exe; child_pid = $childPid
                child_start_utc_ticks = $child.StartTime.ToUniversalTime().Ticks
                task = $taskKind; effective_limits = $effective; started_at = [DateTimeOffset]::UtcNow.ToString('o')
            } | ConvertTo-Json -Depth 7 | Set-Content -LiteralPath $script:ActivePath -Encoding UTF8
            $markerCreated = $true
            $elapsed = [Diagnostics.Stopwatch]::StartNew()
            while (-not $job.HasExited()) {
                if ([IO.File]::Exists($script:StopPath)) {
                    $request = Get-Content -LiteralPath $script:StopPath -Raw -Encoding UTF8 | ConvertFrom-Json
                    if ($request.run_id -cne $runId) { throw 'Stop request has mismatched run ID' }
                    $outcome = 'canceled'; $reason = 'stop_requested'; $job.Terminate(); break
                }
                if ($job.LogExceeded) {
                    $outcome = 'failed'; $reason = 'log_limit'; $job.Terminate(); break
                }
                if ($null -ne $job.OutputError) {
                    $outcome = 'failed'; $reason = 'output_capture_failed'; $job.Terminate(); break
                }
                if ($elapsed.Elapsed.TotalSeconds -ge $effective.timeout_seconds) {
                    $outcome = 'canceled'; $reason = 'timeout'; $job.Terminate(); break
                }
                if ([DfxLocal.BoundedJob]::PhysicalMemory()[1] -lt [ulong]$effective.min_free_memory_bytes) {
                    $outcome = 'failed'; $reason = 'memory_reserve'; $job.Terminate(); break
                }
                $drive = [IO.DriveInfo]::new([IO.Path]::GetPathRoot($script:Root))
                if ($drive.AvailableFreeSpace -lt $effective.disk_reserve_bytes -or
                    ((Get-ManagedBytes) + (Get-ExportBytes)) -gt $effective.disk_budget_bytes) {
                    $outcome = 'failed'; $reason = 'disk_limit'; $job.Terminate(); break
                }
                Start-Sleep -Seconds $effective.monitor_seconds
            }
            if ($null -eq $reason -and $job.LogExceeded) { $outcome = 'failed'; $reason = 'log_limit' }
            if ($null -eq $reason -and $null -ne $job.OutputError) { $outcome = 'failed'; $reason = 'output_capture_failed' }
            if ($null -eq $reason) {
                if ($job.ExitCode() -eq 0) { $outcome = 'finished'; $reason = 'exited_zero' }
                else { $outcome = 'failed'; $reason = 'child_exit_nonzero' }
            }
        } catch {
            $outcome = 'failed'; $reason = $_.Exception.Message
            if ($null -ne $job) { try { $job.Terminate() } catch {} }
        } finally {
            if ($null -ne $job) {
                if ($childPid -gt 0 -and -not $job.WaitForExit(5000)) {
                    try { $job.Terminate(); $null = $job.WaitForExit(5000) } catch {}
                }
                $job.Dispose()
            }
            $env:TEMP = $savedTemp; $env:TMP = $savedTmp; $env:DFX_TEST_PATH = $savedTestPath
            $env:DFX_OFFLINE_TASK = $savedOfflineTask; $env:DFX_OFFLINE_SCRIPT = $savedOfflineScript
            $env:DFX_OFFLINE_WORK = $savedOfflineWork; $env:DFX_OFFLINE_RESUME = $savedOfflineResume
            $env:DFX_METRICS_MANIFEST = $savedMetricsManifest; $env:DFX_METRICS_WORK = $savedMetricsWork
            $env:DFX_EXPORT_MANIFEST = $savedExportManifest; $env:DFX_EXPORT_ID = $savedExportId
            $env:DFX_EXPORT_OWNER = $savedExportOwner; $env:DFX_EXPORT_PLAN = $savedExportPlan
            $env:DFX_EXPORT_APPLY = $savedExportApply; $env:DFX_EXPORT_PLAN_ID = $savedExportPlanId
            $env:DFX_EXPORT_VERIFY = $savedExportVerify; $env:DFX_EXPORT_PACKAGE = $savedExportPackage
            $env:DFX_EXPORT_RUN_ID = $savedExportRunId
            $logTruncated = $null -ne $job -and $job.LogExceeded
            if ($logTruncated -and $reason -notin @('stop_requested', 'timeout')) {
                $outcome = 'failed'; $reason = 'log_limit'
            }
            if ($null -ne $job -and $null -ne $job.OutputError -and $null -eq $reason) {
                $outcome = 'failed'; $reason = 'output_capture_failed'
            }
            if ([IO.File]::Exists($logPath) -and [IO.FileInfo]::new($logPath).Length -gt $script:Policy.limits.max_log_file_bytes) {
                $stream = [IO.File]::Open($logPath, [IO.FileMode]::Open, [IO.FileAccess]::Write, [IO.FileShare]::None)
                try { $stream.SetLength([long]$script:Policy.limits.max_log_file_bytes) } finally { $stream.Dispose() }
                $logTruncated = $true
            }
            [pscustomobject]@{ schema_version = 1; run_id = $runId; task = $taskKind
                status = $outcome; reason = $reason
                child_pid = $childPid; log_path = $logPath; log_truncated = $logTruncated
                finished_at = [DateTimeOffset]::UtcNow.ToString('o') } |
                ConvertTo-Json -Depth 5 | Set-Content -LiteralPath $recordPath -Encoding UTF8
            if ($markerCreated -and [IO.File]::Exists($script:ActivePath)) {
                $current = Get-Content -LiteralPath $script:ActivePath -Raw -Encoding UTF8 | ConvertFrom-Json
                if ($current.run_id -ceq $runId) { Remove-Item -LiteralPath $script:ActivePath -Force }
            }
            if ([IO.File]::Exists($script:StopPath)) {
                $request = Get-Content -LiteralPath $script:StopPath -Raw -Encoding UTF8 | ConvertFrom-Json
                if ($request.run_id -ceq $runId) { Remove-Item -LiteralPath $script:StopPath -Force }
            }
        }
        Get-Content -LiteralPath $recordPath -Raw -Encoding UTF8
        if ($outcome -ne 'finished') { exit 2 }
        exit 0
    }
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
        foreach ($target in $targets) { Remove-VerifiedLinks $target }
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
