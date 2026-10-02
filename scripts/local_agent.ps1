param(
    [Parameter(Mandatory = $true)][ValidateSet('offline')][string]$Scenario,
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
if (-not $env:DFX_OFFLINE_TASK -or -not $env:DFX_OFFLINE_SCRIPT -or
    -not $env:DFX_OFFLINE_WORK -or $env:DFX_OFFLINE_RESUME -notin @('0', '1')) {
    throw 'Missing managed offline inputs'
}
$env:PYTHONDONTWRITEBYTECODE = '1'
$env:PYTHONPATH = Join-Path $repo 'src'
$env:TEMP = Join-Path $root 'tmp'; $env:TMP = $env:TEMP
$env:DFX_MANAGED_AGENT = 'offline-v1'
$work = $env:DFX_OFFLINE_WORK
$runId = [IO.Path]::GetFileName($work).Substring('offline-'.Length)
$command = if ($env:DFX_OFFLINE_RESUME -eq '1') { 'resume' } else { 'repair' }
$exitCode = 1
Push-Location $repo
try {
    & $python (Join-Path $repo 'scripts/dfx.py') $command --task $env:DFX_OFFLINE_TASK `
        --work $work --run-id $runId --fixed-script $env:DFX_OFFLINE_SCRIPT
    $exitCode = $LASTEXITCODE
} finally {
    Pop-Location
    # A rejected resume is a read-only refusal, not a new terminal execution.
    if ([IO.Directory]::Exists($work) -and -not ($command -eq 'resume' -and $exitCode -eq 3)) {
        [pscustomobject]@{ schema_version = 1; status = if ($exitCode -eq 0) { 'finished' } else { 'failed' }
            task = 'offline'; run_id = $runId; finished_at = [DateTimeOffset]::UtcNow.ToString('o') } |
            ConvertTo-Json -Depth 4 | Set-Content -LiteralPath (Join-Path $work 'run-state.json') -Encoding UTF8
    }
}
exit $exitCode
