param(
    [Parameter(Mandatory = $true)][ValidateSet('bootstrap')][string]$Scenario,
    [ValidateRange(1, 120)][int]$Seconds = 1
)

# This script is launched only after scripts/local.ps1 has assigned it to a verified Job.
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$repo = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..')).TrimEnd([IO.Path]::DirectorySeparatorChar)
$root = Join-Path $repo '.dfx-local'
$config = Get-Content -LiteralPath (Join-Path $repo 'configs/bootstrap.json') -Raw -Encoding UTF8 | ConvertFrom-Json
$policy = Get-Content -LiteralPath (Join-Path $repo 'configs/local-runtime.json') -Raw -Encoding UTF8 | ConvertFrom-Json
if ($config.schema_version -ne 1 -or $config.uv_version -cne '0.12.9' -or
    $config.uv_archive_url -cne 'https://releases.astral.sh/github/uv/releases/download/0.12.9/uv-x86_64-pc-windows-msvc.zip' -or
    $config.uv_archive_sha256 -cne 'ddbfcee1ac615a0499f6aa97b5ec8ebdf3ee4a7714a48055ec2ba0030e3cf810' -or
    $config.uv_archive_max_bytes -gt 52428800 -or $config.uv_archive_max_bytes -lt 1048576 -or
    $config.python_version -cne '3.13.5' -or
    [Runtime.InteropServices.RuntimeInformation]::OSArchitecture -ne [Runtime.InteropServices.Architecture]::X64) {
    throw 'Unsupported bootstrap configuration or architecture'
}
$uvPath = Join-Path $root 'tools/uv.exe'
$statePath = Join-Path $root 'state/bootstrap.json'
$tmp = Join-Path $root 'tmp'
$offlineSync = $env:DFX_BOOTSTRAP_OFFLINE_SYNC -eq '1'
$pythonExe = Join-Path $root 'env/Scripts/python.exe'
if ($offlineSync -and (-not [IO.File]::Exists($uvPath) -or -not [IO.File]::Exists($statePath) -or
    -not [IO.File]::Exists($pythonExe))) {
    throw 'Offline sync requires the existing project uv and Python; no bootstrap download is allowed'
}
[IO.Directory]::CreateDirectory((Join-Path $root 'tools')) | Out-Null
[IO.Directory]::CreateDirectory((Join-Path $root 'state')) | Out-Null
[IO.Directory]::CreateDirectory($tmp) | Out-Null

if ([IO.File]::Exists($uvPath) -and [IO.File]::Exists($statePath)) {
    $state = Get-Content -LiteralPath $statePath -Raw -Encoding UTF8 | ConvertFrom-Json
    if ($state.uv_version -cne $config.uv_version -or
        (Get-FileHash -LiteralPath $uvPath -Algorithm SHA256).Hash -cne $state.uv_exe_sha256) {
        throw 'Project uv executable does not match recorded bootstrap identity'
    }
    [Console]::Out.WriteLine("uv=$($config.uv_version) reused")
} else {
    if ([IO.File]::Exists($uvPath)) { [IO.File]::Delete($uvPath) }
    if ([IO.File]::Exists($statePath)) { [IO.File]::Delete($statePath) }
    $zipPath = Join-Path $tmp 'uv-0.12.9.zip'
    $exeTemp = Join-Path $tmp 'uv-0.12.9.exe'
    if ([IO.File]::Exists($zipPath)) { [IO.File]::Delete($zipPath) }
    if ([IO.File]::Exists($exeTemp)) { [IO.File]::Delete($exeTemp) }
    $client = [Net.Http.HttpClient]::new()
    $client.Timeout = [TimeSpan]::FromMinutes(3)
    try {
        $response = $client.GetAsync($config.uv_archive_url, [Net.Http.HttpCompletionOption]::ResponseHeadersRead).GetAwaiter().GetResult()
        $response.EnsureSuccessStatusCode() | Out-Null
        if ($response.Content.Headers.ContentLength -gt [long]$config.uv_archive_max_bytes) { throw 'uv archive exceeds allowed download size' }
        $inputStream = $response.Content.ReadAsStreamAsync().GetAwaiter().GetResult()
        $outputStream = [IO.File]::Open($zipPath, [IO.FileMode]::CreateNew, [IO.FileAccess]::Write, [IO.FileShare]::None)
        try {
            $buffer = [byte[]]::new(65536)
            [long]$total = 0
            while (($count = $inputStream.Read($buffer, 0, $buffer.Length)) -gt 0) {
                $total += $count
                if ($total -gt [long]$config.uv_archive_max_bytes) { throw 'uv archive exceeds allowed download size' }
                $outputStream.Write($buffer, 0, $count)
            }
        } finally { $outputStream.Dispose(); $inputStream.Dispose(); $response.Dispose() }
    } finally { $client.Dispose() }
    if ((Get-FileHash -LiteralPath $zipPath -Algorithm SHA256).Hash.ToLowerInvariant() -cne $config.uv_archive_sha256) {
        throw 'uv archive checksum mismatch'
    }
    $archive = [IO.Compression.ZipFile]::OpenRead($zipPath)
    try {
        $entry = @($archive.Entries | Where-Object { $_.FullName -ceq 'uv.exe' })
        if ($entry.Count -ne 1 -or $entry[0].Length -gt 52428800) { throw 'uv.exe missing or oversized in archive' }
        $source = $entry[0].Open()
        $dest = [IO.File]::Open($exeTemp, [IO.FileMode]::CreateNew, [IO.FileAccess]::Write, [IO.FileShare]::None)
        try { $source.CopyTo($dest) } finally { $dest.Dispose(); $source.Dispose() }
    } finally { $archive.Dispose() }
    $exeHash = (Get-FileHash -LiteralPath $exeTemp -Algorithm SHA256).Hash
    Move-Item -LiteralPath $exeTemp -Destination $uvPath
    [pscustomobject]@{ schema_version = 1; uv_version = $config.uv_version
        uv_archive_sha256 = $config.uv_archive_sha256; uv_exe_sha256 = $exeHash
        installed_at = [DateTimeOffset]::UtcNow.ToString('o') } |
        ConvertTo-Json | Set-Content -LiteralPath $statePath -Encoding UTF8
    Remove-Item -LiteralPath $zipPath -Force
    [Console]::Out.WriteLine("uv=$($config.uv_version) installed")
}

$env:UV_CACHE_DIR = Join-Path $root 'cache/uv'
$env:UV_PYTHON_CACHE_DIR = Join-Path $root 'cache/python'
$env:UV_PYTHON_INSTALL_DIR = Join-Path $root 'python'
$env:UV_PYTHON_BIN_DIR = Join-Path $root 'tools/python-bin'
$env:UV_PROJECT_ENVIRONMENT = Join-Path $root 'env'
$env:UV_TOOL_DIR = Join-Path $root 'tools/uv-tools'
$env:UV_TOOL_BIN_DIR = Join-Path $root 'tools/bin'
$env:UV_MANAGED_PYTHON = '1'
$env:UV_PYTHON_NO_REGISTRY = '1'
$env:UV_NO_CONFIG = '1'
if ($offlineSync) { $env:UV_PYTHON_DOWNLOADS = 'never' }
$env:UV_CONCURRENT_DOWNLOADS = [string][int]$policy.limits.max_download_concurrency
$env:UV_CONCURRENT_BUILDS = [string][int]$policy.limits.max_build_concurrency
$env:PYTHONPYCACHEPREFIX = Join-Path $root 'cache/pycache'
$env:RUFF_CACHE_DIR = Join-Path $root 'cache/ruff'
$env:TEMP = $tmp; $env:TMP = $tmp
Remove-Item Env:VIRTUAL_ENV -ErrorAction SilentlyContinue
Remove-Item Env:CONDA_PREFIX -ErrorAction SilentlyContinue
foreach ($dir in @($env:UV_CACHE_DIR, $env:UV_PYTHON_CACHE_DIR, $env:UV_PYTHON_INSTALL_DIR,
        $env:UV_PYTHON_BIN_DIR, $env:UV_TOOL_DIR, $env:UV_TOOL_BIN_DIR, $env:PYTHONPYCACHEPREFIX)) {
    [IO.Directory]::CreateDirectory($dir) | Out-Null
}

Push-Location $repo
try {
    & $uvPath --version
    if ($LASTEXITCODE -ne 0) { throw 'Project uv executable failed' }
    if ($offlineSync) {
        & $pythonExe -c 'import sys; print(sys.version); raise SystemExit(0 if sys.version_info[:3] == (3, 13, 5) else 1)'
        if ($LASTEXITCODE -ne 0) { throw 'Existing managed Python version does not match bootstrap configuration' }
        & $uvPath lock --offline --no-progress
        if ($LASTEXITCODE -ne 0) { throw 'Offline dependency lock failed; missing cached build requirements must be admitted separately' }
        & $uvPath sync --offline --locked --python $pythonExe --no-progress
    } else {
        & $uvPath python install $config.python_version --no-progress
        if ($LASTEXITCODE -ne 0) { throw 'Managed Python install failed' }
        & $uvPath lock --no-progress
        if ($LASTEXITCODE -ne 0) { throw 'Dependency lock failed' }
        & $uvPath sync --locked --python $config.python_version --no-progress
    }
    if ($LASTEXITCODE -ne 0) { throw 'Locked dependency sync failed' }
} finally { Pop-Location }
if (-not [IO.File]::Exists($pythonExe)) { throw 'Managed environment Python missing after sync' }
& $pythonExe -c 'import sys; print(sys.version); import deepagents, pydantic, langgraph'
if ($LASTEXITCODE -ne 0) { throw 'Managed SDK import failed' }
$productExe = Join-Path $root 'env/Scripts/dfx.exe'
if (-not [IO.File]::Exists($productExe)) { throw 'Installed product console command is missing after sync' }
& $productExe --version
if ($LASTEXITCODE -ne 0) { throw 'Installed product console version command failed' }
& $productExe --help
if ($LASTEXITCODE -ne 0) { throw 'Installed product console help command failed' }
[pscustomobject]@{ schema_version = 1; uv_version = $config.uv_version
    python_version = $config.python_version
    uv_exe_sha256 = (Get-FileHash -LiteralPath $uvPath -Algorithm SHA256).Hash
    lock_sha256 = (Get-FileHash -LiteralPath (Join-Path $repo 'uv.lock') -Algorithm SHA256).Hash
    pyproject_sha256 = (Get-FileHash -LiteralPath (Join-Path $repo 'pyproject.toml') -Algorithm SHA256).Hash
    environment_python = $pythonExe; completed_at = [DateTimeOffset]::UtcNow.ToString('o') } |
    ConvertTo-Json | Set-Content -LiteralPath (Join-Path $root 'state/environment.json') -Encoding UTF8
[Console]::Out.WriteLine('managed environment ready')
