param(
    [Parameter(Mandatory = $true)][ValidateSet('competition')][string]$Scenario,
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
if (-not $env:DFX_COMPETITION_TASK -or -not $env:DFX_COMPETITION_CONFIG -or
    -not $env:DFX_COMPETITION_WORK -or $env:DFX_COMPETITION_RESUME -notin @('0', '1')) {
    throw 'Missing managed competition inputs'
}

function Assert-CompetitionPath([string]$Path) {
    $full = [IO.Path]::GetFullPath($Path)
    if (-not $full.StartsWith($repo + [IO.Path]::DirectorySeparatorChar, [StringComparison]::OrdinalIgnoreCase)) {
        throw 'Competition path is outside the repository'
    }
    $cursor = $repo
    $segments = $full.Substring($repo.Length).TrimStart([IO.Path]::DirectorySeparatorChar) -split '[\\/]'
    foreach ($segment in (@('') + $segments)) {
        if ($segment) { $cursor = Join-Path $cursor $segment }
        if (Test-Path -LiteralPath $cursor) {
            $entry = Get-Item -LiteralPath $cursor -Force
            if (($entry.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0 -or $null -ne $entry.LinkType) {
                throw 'Competition path contains a link'
            }
        }
    }
}

$work = [IO.Path]::GetFullPath($env:DFX_COMPETITION_WORK)
$workName = [IO.Path]::GetFileName($work)
if ($workName -cnotmatch '^competition-([A-Za-z0-9][A-Za-z0-9_-]{0,63})$' -or
    $work -cne [IO.Path]::GetFullPath((Join-Path $root ('work/' + $workName)))) {
    throw 'Competition work must have the fixed managed path'
}
$runId = $workName.Substring('competition-'.Length)
Assert-CompetitionPath $env:DFX_COMPETITION_TASK
Assert-CompetitionPath $env:DFX_COMPETITION_CONFIG
Assert-CompetitionPath $work
$resume = $env:DFX_COMPETITION_RESUME -eq '1'
$env:PYTHONDONTWRITEBYTECODE = '1'
$env:PYTHONPATH = Join-Path $repo 'src'
$env:TEMP = Join-Path $root 'tmp'; $env:TMP = $env:TEMP
$env:DFX_MANAGED_COMPETITION = 'local-v1'
$competitionArguments = @(
    (Join-Path $repo 'scripts/dfx.py'), 'competition', '--task', $env:DFX_COMPETITION_TASK,
    '--work', $work, '--run-id', $runId, '--config', $env:DFX_COMPETITION_CONFIG, '--project-root', $repo
)
if ($resume) { $competitionArguments += '--resume' }
$exitCode = 1
Push-Location $repo
try {
    & $python @competitionArguments
    $exitCode = $LASTEXITCODE
} finally {
    Pop-Location
    # A rejected resume does not overwrite the prior run's terminal observation.
    if ([IO.Directory]::Exists($work) -and -not ($resume -and $exitCode -eq 3)) {
        Assert-CompetitionPath $work
        $marker = Join-Path $work 'run-state.json'
        Assert-CompetitionPath $marker
        [pscustomobject]@{ schema_version = 1; status = if ($exitCode -eq 0) { 'finished' } else { 'failed' }
            task = 'competition'; run_id = $runId; finished_at = [DateTimeOffset]::UtcNow.ToString('o') } |
            ConvertTo-Json -Depth 4 | Set-Content -LiteralPath $marker -Encoding UTF8
    }
}
exit $exitCode
