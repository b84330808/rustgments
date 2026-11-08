# Docs build benchmark: Sphinx with plain Pygments vs with rustgments.install().
# One build at a time, order alternating every run; each output tree is hashed
# file by file (outside the timed region) and compared with the first one.
# Usage:
#   powershell -NoProfile -ExecutionPolicy Bypass -File bench\docs_bench.ps1 `
#     -Py C:\path\python.exe -Src C:\docs\src -Work C:\scratch\work [-Runs 5] [-Label django] `
#     [-Defines 'extensions=a,b;other=value']   (each becomes a sphinx-build -D option)
param(
    [Parameter(Mandatory = $true)][string]$Py,
    [Parameter(Mandatory = $true)][string]$Src,
    [Parameter(Mandatory = $true)][string]$Work,
    [int]$Runs = 5,
    [string]$Label = 'docs',
    [string]$Defines = '',
    [string]$Out = ''
)
# ($PSScriptRoot is empty in parameter defaults under Windows PowerShell 5.1)
if (-not $Out) { $Out = Join-Path $PSScriptRoot 'results' }

$ErrorActionPreference = 'Stop'
$sphinxArgs = @()
foreach ($d in ($Defines -split ';' | Where-Object { $_ })) { $sphinxArgs += @('-D', $d) }
$runner = Join-Path $PSScriptRoot 'docs_build.py'
$hasher = Join-Path $PSScriptRoot 'tree_hash.py'
New-Item -ItemType Directory -Force $Out, $Work | Out-Null
$stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
$csv = Join-Path $Out "docs-$Label-$stamp.csv"
"run,impl,wall_s,inner_s,files,bytes,tree_sha256" | Out-File -Encoding utf8 $csv
# fixed build date so that "last updated" stamps cannot differ between runs
$env:SOURCE_DATE_EPOCH = '1794100000'

function Median($xs) {
    $s = $xs | Sort-Object
    $n = $s.Count
    if ($n % 2 -eq 1) { return $s[($n - 1) / 2] }
    return ($s[$n / 2 - 1] + $s[$n / 2]) / 2
}

$res = @{ pygments = @(); rustgments = @() }
$ref = $null
for ($r = 1; $r -le $Runs; $r++) {
    $order = if ($r % 2 -eq 1) { @('pygments', 'rustgments') } else { @('rustgments', 'pygments') }
    foreach ($impl in $order) {
        $outDir = Join-Path $Work "html-$impl"
        $dtDir = Join-Path $Work "doctrees-$impl"
        foreach ($d in @($outDir, $dtDir)) { if (Test-Path $d) { Remove-Item -Recurse -Force $d } }
        $sw = [Diagnostics.Stopwatch]::StartNew()
        # Sphinx warnings go to stderr; with 'Stop', Windows PowerShell would
        # turn each stderr line of a native command into a terminating error.
        $ErrorActionPreference = 'Continue'
        $json = & $Py -I $runner $impl $Src $outDir $dtDir @sphinxArgs 2> (Join-Path $Work "stderr-$impl-$r.txt")
        $sw.Stop()
        $ErrorActionPreference = 'Stop'
        if ($LASTEXITCODE -ne 0) { throw "build failed: $impl run $r (see stderr-$impl-$r.txt)" }
        $o = ($json | Select-Object -Last 1) | ConvertFrom-Json
        $manifest = Join-Path $Work "manifest-$impl-$r.tsv"
        $h = (& $Py -I $hasher $outDir $manifest) | ConvertFrom-Json
        if ($null -eq $ref) { $ref = $h.tree_sha256 }
        $rec = [pscustomobject]@{ wall = $sw.Elapsed.TotalSeconds; inner = $o.seconds; sha = $h.tree_sha256 }
        $res[$impl] += $rec
        "$r,$impl,$($rec.wall),$($o.seconds),$($h.files),$($h.bytes),$($h.tree_sha256)" | Out-File -Append -Encoding utf8 $csv
        Write-Host ("run {0} {1,-10} {2,8:N2} s  files={3} identical_to_first={4}" -f $r, $impl, $rec.wall, $h.files, ($h.tree_sha256 -eq $ref))
    }
}
foreach ($impl in @('pygments', 'rustgments')) {
    $w = $res[$impl] | ForEach-Object { $_.wall }
    $m = Median $w
    Write-Host ("{0,-10} median {1:N2} s  range {2:N2}-{3:N2} s  (n={4})" -f $impl, $m,
        ($w | Measure-Object -Minimum).Minimum, ($w | Measure-Object -Maximum).Maximum, $w.Count)
}
$mp = Median ($res['pygments'] | ForEach-Object { $_.wall })
$mr = Median ($res['rustgments'] | ForEach-Object { $_.wall })
$all = @($res['pygments'] + $res['rustgments'] | ForEach-Object { $_.sha } | Select-Object -Unique)
Write-Host ("speedup {0:N3}x; distinct output trees across all {1} builds: {2}" -f ($mp / $mr), (2 * $Runs), $all.Count)
Write-Host "raw: $csv"
