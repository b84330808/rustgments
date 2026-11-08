# Many highlight() calls in one process: Pygments vs rustgments.install().
# One process at a time, order alternating every run; Stopwatch around the
# whole process, plus the loop time the process reports.
# Usage: powershell -NoProfile -ExecutionPolicy Bypass -File bench\many_blocks.ps1 `
#          -Py C:\path\python.exe -List C:\scratch\many-blocks.txt [-Runs 5]
param(
    [Parameter(Mandatory = $true)][string]$Py,
    [Parameter(Mandatory = $true)][string]$List,
    [int]$Runs = 5,
    [string]$Out = ''
)
# ($PSScriptRoot is empty in parameter defaults under Windows PowerShell 5.1)
if (-not $Out) { $Out = Join-Path $PSScriptRoot 'results' }

$ErrorActionPreference = 'Stop'
$one = Join-Path $PSScriptRoot 'many_blocks.py'
New-Item -ItemType Directory -Force $Out | Out-Null
$stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
$csv = Join-Path $Out "many-blocks-$stamp.csv"
"run,impl,wall_s,loop_s,calls,bytes,sha" | Out-File -Encoding utf8 $csv

function Median($xs) {
    $s = $xs | Sort-Object
    $n = $s.Count
    if ($n % 2 -eq 1) { return $s[($n - 1) / 2] }
    return ($s[$n / 2 - 1] + $s[$n / 2]) / 2
}

$res = @{ pygments = @(); rustgments = @() }
for ($r = 1; $r -le $Runs; $r++) {
    $order = if ($r % 2 -eq 1) { @('pygments', 'rustgments') } else { @('rustgments', 'pygments') }
    foreach ($impl in $order) {
        $sw = [Diagnostics.Stopwatch]::StartNew()
        $json = & $Py -I $one $impl $List
        $sw.Stop()
        if ($LASTEXITCODE -ne 0) { throw "many_blocks failed: $impl run $r" }
        $o = $json | ConvertFrom-Json
        $res[$impl] += [pscustomobject]@{ wall = $sw.Elapsed.TotalSeconds; loop = $o.loop_s; sha = $o.sha }
        "$r,$impl,$($sw.Elapsed.TotalSeconds),$($o.loop_s),$($o.calls),$($o.bytes),$($o.sha)" | Out-File -Append -Encoding utf8 $csv
        Write-Host ("run {0} {1,-10} wall {2,7:N2} s  loop {3,7:N2} s  calls={4} sha={5}" -f $r, $impl, $sw.Elapsed.TotalSeconds, $o.loop_s, $o.calls, $o.sha)
    }
}
foreach ($m in @('wall', 'loop')) {
    $p = $res['pygments'] | ForEach-Object { $_.$m }
    $q = $res['rustgments'] | ForEach-Object { $_.$m }
    Write-Host ("{0,-5} pygments median {1:N2} s ({2:N2}-{3:N2})  rustgments median {4:N2} s ({5:N2}-{6:N2})  speedup {7:N2}x" -f $m,
        (Median $p), ($p | Measure-Object -Minimum).Minimum, ($p | Measure-Object -Maximum).Maximum,
        (Median $q), ($q | Measure-Object -Minimum).Minimum, ($q | Measure-Object -Maximum).Maximum, ((Median $p) / (Median $q)))
}
$all = @($res['pygments'] + $res['rustgments'] | ForEach-Object { $_.sha } | Select-Object -Unique)
Write-Host "distinct HTML digests across all runs: $($all.Count)"
Write-Host "raw: $csv"
