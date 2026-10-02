param(
    [Parameter(Mandatory = $true)][ValidateSet('export')][string]$Scenario,
    [ValidateRange(1, 120)][int]$Seconds = 1
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$repo = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..')).TrimEnd([IO.Path]::DirectorySeparatorChar)
$root = Join-Path $repo '.dfx-local'

function Assert-InputChain([string]$Path) {
    $full = [IO.Path]::GetFullPath($Path)
    if (-not $full.StartsWith($repo + [IO.Path]::DirectorySeparatorChar, [StringComparison]::OrdinalIgnoreCase)) {
        throw 'Export path is outside the repository'
    }
    $cursor = $repo
    $segments = $full.Substring($repo.Length).TrimStart([IO.Path]::DirectorySeparatorChar) -split '[\\/]'
    foreach ($segment in (@('') + $segments)) {
        if ($segment) { $cursor = Join-Path $cursor $segment }
        if (Test-Path -LiteralPath $cursor) {
            $entry = Get-Item -LiteralPath $cursor -Force
            if (($entry.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0 -or $null -ne $entry.LinkType) {
                throw 'Export path contains a link'
            }
        }
    }
}

$python = Join-Path $root 'env/Scripts/python.exe'
$environmentPath = Join-Path $root 'state/environment.json'
$ownerPath = Join-Path $root 'owner.json'
foreach ($path in @($python, $environmentPath, $ownerPath, (Join-Path $repo 'uv.lock'), (Join-Path $repo 'pyproject.toml'))) {
    Assert-InputChain $path
}
$environment = Get-Content -LiteralPath $environmentPath -Raw -Encoding UTF8 | ConvertFrom-Json
if ($environment.environment_python -cne $python -or
    $environment.lock_sha256 -cne (Get-FileHash -LiteralPath (Join-Path $repo 'uv.lock') -Algorithm SHA256).Hash -or
    $environment.pyproject_sha256 -cne (Get-FileHash -LiteralPath (Join-Path $repo 'pyproject.toml') -Algorithm SHA256).Hash -or
    -not [IO.File]::Exists($python)) { throw 'Managed environment identity mismatch' }
$owner = Get-Content -LiteralPath $ownerPath -Raw -Encoding UTF8 | ConvertFrom-Json
if ($env:DFX_EXPORT_ID -cnotmatch '^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$' -or
    -not [Guid]::TryParse([string]$env:DFX_EXPORT_OWNER, [ref][Guid]::Empty) -or
    -not [Guid]::TryParse([string]$env:DFX_EXPORT_RUN_ID, [ref][Guid]::Empty) -or
    $owner.schema_version -ne 1 -or $owner.project_instance_id -cne $env:DFX_EXPORT_OWNER -or
    $owner.repo_real_path -cne $repo -or $owner.managed_real_path -cne $root -or
    $env:DFX_EXPORT_APPLY -notin @('0', '1') -or $env:DFX_EXPORT_VERIFY -notin @('0', '1')) {
    throw 'Managed export identity or mode mismatch'
}
$plan = [IO.Path]::GetFullPath($env:DFX_EXPORT_PLAN)
$package = [IO.Path]::GetFullPath($env:DFX_EXPORT_PACKAGE)
if ($plan -cne [IO.Path]::GetFullPath((Join-Path $root ('state/export-plan-' + $env:DFX_EXPORT_ID + '.json'))) -or
    $package -cne [IO.Path]::GetFullPath((Join-Path $repo ('runs/exports/' + $env:DFX_EXPORT_ID)))) {
    throw 'Export plan or package does not have its fixed managed path'
}
Assert-InputChain $plan
Assert-InputChain $package
$verify = $env:DFX_EXPORT_VERIFY -eq '1'
$apply = $env:DFX_EXPORT_APPLY -eq '1'
if ($verify) {
    if ($apply -or $env:DFX_EXPORT_MANIFEST -or $env:DFX_EXPORT_PLAN_ID -or -not [IO.Directory]::Exists($package)) {
        throw 'Export verification accepts only an existing package'
    }
} else {
    if (-not $env:DFX_EXPORT_MANIFEST) { throw 'Missing managed export manifest' }
    $manifest = [IO.Path]::GetFullPath($env:DFX_EXPORT_MANIFEST)
    Assert-InputChain $manifest
    $segments = $manifest.Substring($repo.Length).TrimStart([IO.Path]::DirectorySeparatorChar) -split '[\\/]'
    if (@($segments | Where-Object { $_ -in @('data_B', 'archive', '.git', '.agents', '.codex') }).Count -gt 0 -or
        -not [IO.File]::Exists($manifest)) { throw 'Evaluation manifest is missing or in a forbidden location' }
    if (Test-Path -LiteralPath $package) { throw 'Export target already exists; choose a new ID' }
    if ($apply) {
        if ($env:DFX_EXPORT_PLAN_ID -cnotmatch '^[a-f0-9]{64}$' -or -not [IO.File]::Exists($plan)) {
            throw 'Applying an export requires its saved reviewed plan'
        }
    } elseif ($env:DFX_EXPORT_PLAN_ID) { throw 'Export PlanId requires apply mode' }
}

# The manager creates its lease immediately after starting this guarded worker.
$activePath = Join-Path $root 'state/active.json'
$leaseWait = [Diagnostics.Stopwatch]::StartNew()
$active = $null
while ($null -eq $active -and $leaseWait.Elapsed.TotalSeconds -lt 5) {
    Assert-InputChain $activePath
    if ([IO.File]::Exists($activePath)) {
        try { $active = Get-Content -LiteralPath $activePath -Raw -Encoding UTF8 | ConvertFrom-Json }
        catch { $active = $null }
        if ($null -ne $active) { break }
    }
    Start-Sleep -Milliseconds 50
}
if ($null -eq $active) { throw 'Managed export lease was not established' }
if ($active.task -cne 'export' -or $active.run_id -cne $env:DFX_EXPORT_RUN_ID -or
    $active.project_instance_id -cne $env:DFX_EXPORT_OWNER -or $active.child_pid -ne $PID) {
    throw 'Managed export lease does not belong to this worker'
}

$env:PYTHONDONTWRITEBYTECODE = '1'
$env:PYTHONPATH = Join-Path $repo 'src'
$env:TEMP = Join-Path $root 'tmp'; $env:TMP = $env:TEMP
$env:DFX_MANAGED_EXPORT = 'offline-v1'
Push-Location $repo
try {
    if ($verify) {
        # Package owner identifies its origin, not authority over this prepared runtime.
        # This permits read-only verification after Clean all and a new Prepare instance.
        try {
            $metadataPath = Join-Path $package 'package.json'
            Assert-InputChain $metadataPath
            if ([IO.FileInfo]::new($metadataPath).Length -gt 16777216) { throw 'Invalid metadata size' }
            $metadata = Get-Content -LiteralPath $metadataPath -Raw -Encoding UTF8 | ConvertFrom-Json
            $packageOwner = [string]$metadata.owner_id
            if (-not [Guid]::TryParse($packageOwner, [ref][Guid]::Empty)) { throw 'Invalid origin ID' }
        } catch { throw 'Export package origin metadata is invalid' }
        & $python (Join-Path $repo 'scripts/dfx.py') verify-export --package $package --owner-id $packageOwner
    } else {
        $arguments = @((Join-Path $repo 'scripts/dfx.py'), 'export', '--manifest', $manifest,
            '--root', $repo, '--export-id', $env:DFX_EXPORT_ID, '--owner-id', $env:DFX_EXPORT_OWNER, '--plan', $plan)
        if ($apply) { $arguments += @('--apply', '--plan-id', $env:DFX_EXPORT_PLAN_ID) }
        & $python @arguments
    }
    $exitCode = $LASTEXITCODE
} finally {
    Pop-Location
}
exit $exitCode
