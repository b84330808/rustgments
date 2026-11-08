# Benchmark: Pygments vs rustgments, end-to-end highlight() to HTML.
# Runs one process at a time, alternating base and ours (order flips every run).
# Usage: powershell -NoProfile -ExecutionPolicy Bypass -File bench\bench.ps1 [-Runs 7] [-Warm 5]
param(
    [int]$Runs = 7,
    [int]$Warm = 5,
    [string]$Cases = "$PSScriptRoot\cases.example.txt",
    [string]$Out = "$PSScriptRoot\results"
)

$ErrorActionPreference = 'Stop'
$py = Join-Path $PSScriptRoot '..\.venv\Scripts\python.exe'
$one = Join-Path $PSScriptRoot 'bench_one.py'
New-Item -ItemType Directory -Force $Out | Out-Null
$stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
$csv = Join-Path $Out "bench-$stamp.csv"
"case,lexer,file,bytes,impl,run,wall_ms,first_ms,warm_median_ms,sha" | Out-File -Encoding utf8 $csv

function Median($xs) {
    $s = $xs | Sort-Object
    $n = $s.Count
    if ($n % 2 -eq 1) { return $s[($n - 1) / 2] }
    return ($s[$n / 2 - 1] + $s[$n / 2]) / 2
}

$rows = @()
$caseLines = Get-Content $Cases | Where-Object { $_ -and -not $_.StartsWith('#') }
foreach ($line in $caseLines) {
    $parts = $line -split "`t"
    $lexer = $parts[0]; $file = $parts[1]
    $res = @{ pygments = @(); rustgments = @() }
    for ($r = 1; $r -le $Runs; $r++) {
        $order = if ($r % 2 -eq 1) { @('pygments', 'rustgments') } else { @('rustgments', 'pygments') }
        foreach ($impl in $order) {
            # 1) cold process: start Python, import, one highlight() call, exit
            $sw = [Diagnostics.Stopwatch]::StartNew()
            $json = & $py -I $one $impl $lexer $file 0
            $sw.Stop()
            if ($LASTEXITCODE -ne 0) { throw "bench_one failed: $impl $lexer $file" }
            $o = $json | ConvertFrom-Json
            # 2) warm process: same call repeated $Warm times after the first
            $json2 = & $py -I $one $impl $lexer $file $Warm
            if ($LASTEXITCODE -ne 0) { throw "bench_one failed: $impl $lexer $file" }
            $o2 = $json2 | ConvertFrom-Json
            $rec = [pscustomobject]@{
                wall = $sw.Elapsed.TotalMilliseconds
                first = $o.first_s * 1000
                warm = $o2.warm_median_s * 1000
                sha = $o.sha
                bytes = $o.bytes
                backend = $o.backend
            }
            $res[$impl] += $rec
            "$($rows.Count),$lexer,$file,$($o.bytes),$impl,$r,$($rec.wall),$($rec.first),$($rec.warm),$($o.sha)" | Out-File -Append -Encoding utf8 $csv
        }
    }
    $shaP = ($res['pygments'] | Select-Object -ExpandProperty sha -Unique)
    $shaR = ($res['rustgments'] | Select-Object -ExpandProperty sha -Unique)
    $row = [ordered]@{
        lexer = $lexer
        file = Split-Path $file -Leaf
        kb = [math]::Round($res['pygments'][0].bytes / 1024, 1)
        backend = $res['rustgments'][0].backend
        same_html = ($shaP -eq $shaR)
    }
    foreach ($m in @('wall', 'first', 'warm')) {
        $p = $res['pygments'] | ForEach-Object { $_.$m }
        $q = $res['rustgments'] | ForEach-Object { $_.$m }
        $pm = Median $p; $qm = Median $q
        $row["pyg_${m}_med"] = [math]::Round($pm, 2)
        $row["pyg_${m}_rng"] = "{0:N2}-{1:N2}" -f ($p | Measure-Object -Minimum).Minimum, ($p | Measure-Object -Maximum).Maximum
        $row["rg_${m}_med"] = [math]::Round($qm, 2)
        $row["rg_${m}_rng"] = "{0:N2}-{1:N2}" -f ($q | Measure-Object -Minimum).Minimum, ($q | Measure-Object -Maximum).Maximum
        $row["${m}_speedup"] = [math]::Round($pm / $qm, 2)
    }
    $rows += [pscustomobject]$row
    Write-Host ("{0,-16} {1,-28} {2,8} KB  wall {3,8} / {4,8} ms ({5}x)  first {6,8} / {7,8} ms ({8}x)  warm {9,8} / {10,8} ms ({11}x)  same_html={12}" -f `
        $row.lexer, $row.file, $row.kb, $row.pyg_wall_med, $row.rg_wall_med, $row.wall_speedup, `
        $row.pyg_first_med, $row.rg_first_med, $row.first_speedup, $row.pyg_warm_med, $row.rg_warm_med, $row.warm_speedup, $row.same_html)
}
$summary = Join-Path $Out "summary-$stamp.csv"
$rows | Export-Csv -NoTypeInformation -Encoding utf8 $summary
Write-Host "raw: $csv"
Write-Host "summary: $summary"
