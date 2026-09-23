param(
    [Parameter(Mandatory = $true)][ValidateSet('idle', 'child', 'noisy', 'memory')][string]$Scenario,
    [ValidateRange(1, 120)][int]$Seconds = 3
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
[Console]::Out.WriteLine("scenario=$Scenario pid=$PID")
switch ($Scenario) {
    'idle' { Start-Sleep -Seconds $Seconds }
    'child' {
        $exe = (Get-Process -Id $PID).Path
        $child = Start-Process -FilePath $exe -ArgumentList @('-NoProfile', '-NonInteractive', '-Command', "Start-Sleep -Seconds $Seconds") -PassThru -WindowStyle Hidden
        [Console]::Out.WriteLine("child_pid=$($child.Id)")
        Start-Sleep -Seconds $Seconds
    }
    'noisy' {
        $chunk = 'x' * 8192
        $until = [Diagnostics.Stopwatch]::StartNew()
        while ($until.Elapsed.TotalSeconds -lt $Seconds) {
            [Console]::Out.Write($chunk)
            Start-Sleep -Milliseconds 10
        }
    }
    'memory' {
        $blocks = [Collections.Generic.List[byte[]]]::new()
        $until = [Diagnostics.Stopwatch]::StartNew()
        while ($until.Elapsed.TotalSeconds -lt $Seconds) {
            $blocks.Add([byte[]]::new(8MB))
            Start-Sleep -Milliseconds 30
        }
    }
}
