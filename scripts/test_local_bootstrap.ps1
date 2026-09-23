Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$repo = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..')).TrimEnd([IO.Path]::DirectorySeparatorChar)
$root = Join-Path $repo '.dfx-local'
$checks = 0
function Check([bool]$Condition, [string]$Message) {
    if (-not $Condition) { throw "CHECK FAILED: $Message" }
    $script:checks++
}
function Read-Environment {
    Get-Content -LiteralPath (Join-Path $root 'state/environment.json') -Raw -Encoding UTF8 | ConvertFrom-Json
}
$first = & (Join-Path $PSScriptRoot 'local.ps1') Init | ConvertFrom-Json
Check ($first.status -ceq 'finished' -and $first.task -ceq 'bootstrap') 'first protected Init'
$environment = Read-Environment
$bootstrap = Get-Content -LiteralPath (Join-Path $root 'state/bootstrap.json') -Raw -Encoding UTF8 | ConvertFrom-Json
$uv = Join-Path $root 'tools/uv.exe'
$python = Join-Path $root 'env/Scripts/python.exe'
Check ($environment.environment_python -ceq $python) 'environment must point to managed Python'
Check ([IO.File]::Exists($uv) -and [IO.File]::Exists($python)) 'uv and Python must stay inside managed root'
Check ($environment.uv_exe_sha256 -ceq (Get-FileHash -LiteralPath $uv -Algorithm SHA256).Hash) 'uv hash must match environment'
Check ($bootstrap.uv_exe_sha256 -ceq $environment.uv_exe_sha256) 'bootstrap identity must match environment'
Check ($environment.lock_sha256 -ceq (Get-FileHash -LiteralPath (Join-Path $repo 'uv.lock') -Algorithm SHA256).Hash) 'lock identity'
Check ($environment.pyproject_sha256 -ceq (Get-FileHash -LiteralPath (Join-Path $repo 'pyproject.toml') -Algorithm SHA256).Hash) 'project identity'
$installStamp = $bootstrap.installed_at
$second = & (Join-Path $PSScriptRoot 'local.ps1') Init | ConvertFrom-Json
Check ($second.status -ceq 'finished' -and $second.task -ceq 'bootstrap') 'repeat protected Init'
$reused = Get-Content -LiteralPath (Join-Path $root 'state/bootstrap.json') -Raw -Encoding UTF8 | ConvertFrom-Json
Check ($reused.installed_at -ceq $installStamp) 'repeat Init must reuse verified uv executable'
$after = Read-Environment
Check ($after.lock_sha256 -ceq $environment.lock_sha256 -and $after.uv_exe_sha256 -ceq $environment.uv_exe_sha256) 'repeat Init must preserve identities'
"PASS: $checks W03 bootstrap assertions, including repeat Init"
