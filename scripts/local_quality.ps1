param(
    [Parameter(Mandatory = $true)][ValidateSet('docs', 'ruff', 'pyright')][string]$Scenario,
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
$env:PYTHONDONTWRITEBYTECODE = '1'
$env:PYRIGHT_PYTHON_CACHE_DIR = Join-Path $root 'cache/pyright'
$env:PYRIGHT_PYTHON_ENV_DIR = Join-Path $root 'tools/pyright-node'
$env:RUFF_CACHE_DIR = Join-Path $root 'cache/ruff'
$env:TEMP = Join-Path $root 'tmp'; $env:TMP = $env:TEMP
Push-Location $repo
try {
    switch ($Scenario) {
        'docs' { & $python (Join-Path $repo 'scripts/check_docs.py') }
        'ruff' { & $python -m ruff check --no-cache src tests scripts/check_docs.py scripts/dfx.py }
        'pyright' { & $python -m pyright --pythonpath $python src/dfx_agent tests/test_sdk_compat.py }
    }
    exit $LASTEXITCODE
} finally { Pop-Location }
