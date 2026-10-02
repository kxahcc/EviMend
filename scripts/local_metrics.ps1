param(
    [Parameter(Mandatory = $true)][ValidateSet('aggregate')][string]$Scenario,
    [ValidateRange(1, 120)][int]$Seconds = 1
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$repo = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..')).TrimEnd([IO.Path]::DirectorySeparatorChar)
$root = Join-Path $repo '.dfx-local'
$python = Join-Path $root 'env/Scripts/python.exe'
$environment = Get-Content -LiteralPath (Join-Path $root 'state/environment.json') -Raw -Encoding UTF8 | ConvertFrom-Json
if ($environment.environment_python -cne $python -or
    $environment.lock_sha256 -cne (Get-FileHash -LiteralPath (Join-Path $repo 'uv.lock') -Algorithm SHA256).Hash -or
    $environment.pyproject_sha256 -cne (Get-FileHash -LiteralPath (Join-Path $repo 'pyproject.toml') -Algorithm SHA256).Hash -or
    -not [IO.File]::Exists($python)) { throw 'Managed environment identity mismatch' }
if (-not $env:DFX_METRICS_MANIFEST -or -not $env:DFX_METRICS_WORK) { throw 'Missing managed metrics inputs' }

function Assert-InputChain([string]$Path) {
    $full = [IO.Path]::GetFullPath($Path)
    if (-not $full.StartsWith($repo + [IO.Path]::DirectorySeparatorChar, [StringComparison]::OrdinalIgnoreCase)) {
        throw 'Metrics path is outside the repository'
    }
    $cursor = $repo
    $segments = $full.Substring($repo.Length).TrimStart([IO.Path]::DirectorySeparatorChar) -split '[\\/]'
    foreach ($segment in (@('') + $segments)) {
        if ($segment) { $cursor = Join-Path $cursor $segment }
        if (Test-Path -LiteralPath $cursor) {
            $entry = Get-Item -LiteralPath $cursor -Force
            if (($entry.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0 -or $null -ne $entry.LinkType) {
                throw 'Metrics path contains a link'
            }
        }
    }
}

$manifest = [IO.Path]::GetFullPath($env:DFX_METRICS_MANIFEST)
$work = [IO.Path]::GetFullPath($env:DFX_METRICS_WORK)
$workName = [IO.Path]::GetFileName($work)
if ($workName -cnotmatch '^aggregate-([A-Za-z0-9][A-Za-z0-9_-]{0,63})$' -or
    $work -cne [IO.Path]::GetFullPath((Join-Path $root ('work/' + $workName)))) {
    throw 'Aggregate work must have the fixed managed path'
}
$runId = $workName.Substring('aggregate-'.Length)
Assert-InputChain $manifest
Assert-InputChain $work
$manifestSegments = $manifest.Substring($repo.Length).TrimStart([IO.Path]::DirectorySeparatorChar) -split '[\\/]'
if (@($manifestSegments | Where-Object { $_ -in @('data_B', 'archive', '.git', '.agents', '.codex') }).Count -gt 0 -or
    -not [IO.File]::Exists($manifest)) { throw 'Evaluation manifest is missing or in a forbidden location' }
if (Test-Path -LiteralPath $work) { throw 'Aggregate work already exists; choose a new ID' }

$env:PYTHONDONTWRITEBYTECODE = '1'
$env:PYTHONPATH = Join-Path $repo 'src'
$env:TEMP = Join-Path $root 'tmp'; $env:TMP = $env:TEMP
$env:DFX_MANAGED_METRICS = 'offline-v1'
$exitCode = 1
Push-Location $repo
try {
    & $python (Join-Path $repo 'scripts/dfx.py') aggregate --manifest $manifest --root $repo `
        --output (Join-Path $work 'aggregate-report.json')
    $exitCode = $LASTEXITCODE
} finally {
    Pop-Location
    if ([IO.Directory]::Exists($work)) {
        Assert-InputChain $work
        $marker = Join-Path $work 'run-state.json'
        Assert-InputChain $marker
        if (Test-Path -LiteralPath $marker) { throw 'Aggregate terminal marker already exists' }
        $payload = [pscustomobject]@{ schema_version = 1; status = if ($exitCode -eq 0) { 'finished' } else { 'failed' }
            task = 'aggregate'; run_id = $runId; finished_at = [DateTimeOffset]::UtcNow.ToString('o') } |
            ConvertTo-Json -Depth 4
        $bytes = [Text.UTF8Encoding]::new($false).GetBytes($payload + [Environment]::NewLine)
        $stream = [IO.File]::Open($marker, [IO.FileMode]::CreateNew, [IO.FileAccess]::Write, [IO.FileShare]::None)
        try { $stream.Write($bytes, 0, $bytes.Length) } finally { $stream.Dispose() }
    }
}
exit $exitCode
