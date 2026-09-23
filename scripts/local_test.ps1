param(
    [Parameter(Mandatory = $true)][ValidateSet('test')][string]$Scenario,
    [ValidateRange(1, 120)][int]$Seconds = 1
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$repo = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..')).TrimEnd([IO.Path]::DirectorySeparatorChar)
$root = Join-Path $repo '.dfx-local'
$testsRoot = [IO.Path]::GetFullPath((Join-Path $repo 'tests')).TrimEnd([IO.Path]::DirectorySeparatorChar)
$testPath = [IO.Path]::GetFullPath($env:DFX_TEST_PATH)
if (-not $testPath.StartsWith(($testsRoot + [IO.Path]::DirectorySeparatorChar), [StringComparison]::OrdinalIgnoreCase) -or
    [IO.Path]::GetFileName($testPath) -cnotmatch '^test_[A-Za-z0-9_]+\.py$' -or -not [IO.File]::Exists($testPath)) {
    throw 'Invalid managed TestPath'
}
$environment = Get-Content -LiteralPath (Join-Path $root 'state/environment.json') -Raw -Encoding UTF8 | ConvertFrom-Json
$python = Join-Path $root 'env/Scripts/python.exe'
if ($environment.environment_python -cne $python -or
    $environment.lock_sha256 -cne (Get-FileHash -LiteralPath (Join-Path $repo 'uv.lock') -Algorithm SHA256).Hash -or
    $environment.pyproject_sha256 -cne (Get-FileHash -LiteralPath (Join-Path $repo 'pyproject.toml') -Algorithm SHA256).Hash -or
    -not [IO.File]::Exists($python)) { throw 'Managed environment identity mismatch' }
$env:PYTHONDONTWRITEBYTECODE = '1'
$env:PYTEST_DISABLE_PLUGIN_AUTOLOAD = '1'
$env:PYTEST_ADDOPTS = ''
$env:TEMP = Join-Path $root 'tmp'; $env:TMP = $env:TEMP
$env:PYTHONPATH = Join-Path $repo 'src'
$baseTemp = Join-Path $root ('tmp/pytest-' + [Guid]::NewGuid().ToString('N'))
[IO.Directory]::CreateDirectory($baseTemp) | Out-Null
Push-Location $repo
try {
    & $python -m pytest $testPath -c (Join-Path $repo 'pyproject.toml') --basetemp $baseTemp -p no:cacheprovider -q
    $testExitCode = $LASTEXITCODE
} finally {
    Pop-Location
    $tmpRoot = [IO.Path]::GetFullPath((Join-Path $root 'tmp')).TrimEnd([IO.Path]::DirectorySeparatorChar)
    $base = [IO.Path]::GetFullPath($baseTemp)
    if (-not $base.StartsWith(($tmpRoot + [IO.Path]::DirectorySeparatorChar), [StringComparison]::OrdinalIgnoreCase) -or
        [IO.Path]::GetFileName($base) -cnotmatch '^pytest-[0-9a-f]{32}$') { throw 'Pytest cleanup path verification failed' }
    if ([IO.Directory]::Exists($base)) {
        $pending = [Collections.Generic.Stack[string]]::new()
        $pending.Push($base)
        while ($pending.Count -gt 0) {
            $folder = $pending.Pop()
            foreach ($entry in [IO.DirectoryInfo]::new($folder).EnumerateFileSystemInfos()) {
                if (($entry.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
                    throw "Pytest temp contains a reparse point: $($entry.FullName)"
                }
                if (($entry.Attributes -band [IO.FileAttributes]::Directory) -ne 0) { $pending.Push($entry.FullName) }
            }
        }
        Remove-Item -LiteralPath $base -Recurse -Force
    }
}
exit $testExitCode
