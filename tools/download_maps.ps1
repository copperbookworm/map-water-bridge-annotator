<#
  download_maps.ps1 - fetch the Squad map source images used by the annotator
  (and, with -WithTrees, by tools/build_maps.py). Pure PowerShell, no Python.

  Usage (from the repo root, or any folder with tools/maps_catalog.json):
    powershell -NoProfile -ExecutionPolicy Bypass -File tools\download_maps.ps1
    powershell -NoProfile -ExecutionPolicy Bypass -File tools\download_maps.ps1 -Only yehorivka,narva
    powershell -NoProfile -ExecutionPolicy Bypass -File tools\download_maps.ps1 -WithTrees

  -Only       only these map slugs (comma separated; default: all 25 maps)
  -WithTrees  also fetch 3d/trees.json + 3d/trees.bin, needed only when
              regenerating the app's map pages with tools/build_maps.py
  -Root       working root (default: SQUAD_MAP_ROOT, else the parent folder
              of this script)

  Files land in <ROOT>\.tmp\maps_source\<slug>\ ; existing non-empty files
  are skipped, so the script is safe to re-run and resumes after a failure.
  The map artwork belongs to its respective owners; this repo only ships the
  downloader, never the game assets.
#>
[CmdletBinding()]
param(
    [string[]]$Only = @(),
    [switch]$WithTrees,
    [string]$Root = ""
)

$ErrorActionPreference = "Stop"
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12

if (-not $Root) {
    if ($env:SQUAD_MAP_ROOT) { $Root = $env:SQUAD_MAP_ROOT }
    else { $Root = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path }
}
$Root = (Resolve-Path $Root).Path
$catalogPath = Join-Path $Root "tools\maps_catalog.json"
if (-not (Test-Path $catalogPath)) { throw "maps_catalog.json not found at $catalogPath (pass -Root <repo folder>)" }

$catalog = Get-Content $catalogPath -Raw -Encoding UTF8 | ConvertFrom-Json
$outRoot = Join-Path $Root ".tmp\maps_source"
New-Item -ItemType Directory -Force -Path $outRoot | Out-Null

$ua = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"
$files = @("heightmap.png", "basemap.webp", "topomap.webp")
if ($WithTrees) { $files += @("3d/trees.json", "3d/trees.bin") }

$wanted = @()
foreach ($entry in $Only) {
    foreach ($piece in ($entry -split ",")) {
        $slug = $piece.Trim().ToLower()
        if ($slug) { $wanted += $slug }
    }
}
$maps = @($catalog | Where-Object { $wanted.Count -eq 0 -or $wanted -contains $_.slug })

if ($maps.Count -eq 0) { throw ("no map matched -Only " + ($wanted -join ",")) }

Write-Host ("root  : " + $Root)
Write-Host ("files : " + ($files -join ", "))
Write-Host ("maps  : " + $maps.Count + " / " + $catalog.Count + " -> " + (($maps | ForEach-Object { $_.slug }) -join ", "))

$ok = 0
$cached = 0
$failed = 0
$failures = @()

foreach ($m in $maps) {
    foreach ($rel in $files) {
        $dst = Join-Path (Join-Path $outRoot $m.slug) ($rel -replace "/", "\")
        $dir = Split-Path -Parent $dst
        New-Item -ItemType Directory -Force -Path $dir | Out-Null

        if ((Test-Path $dst) -and ((Get-Item $dst).Length -gt 0)) {
            $cached++
            continue
        }

        $url = "https://squadcalc.app/api" + $m.path + $rel
        $done = $false
        for ($attempt = 1; $attempt -le 3 -and -not $done; $attempt++) {
            try {
                Invoke-WebRequest -UseBasicParsing -Uri $url -OutFile $dst -UserAgent $ua -TimeoutSec 180
                $size = (Get-Item $dst).Length
                if ($size -lt 1024) { throw ("suspiciously small reply (" + $size + " bytes)") }
                $done = $true
                $ok++
                $mb = [math]::Round($size / 1MB, 1)
                Write-Host ("  ok    " + $m.slug.PadRight(14) + $rel.PadRight(18) + $mb + " MB")
            } catch {
                if (Test-Path $dst) { Remove-Item $dst -Force -ErrorAction SilentlyContinue }
                if ($attempt -lt 3) {
                    Start-Sleep -Seconds 2
                } else {
                    $failed++
                    $msg = $m.slug + " " + $rel + " : " + $_.Exception.Message
                    $failures += $msg
                    Write-Host ("  FAIL  " + $msg)
                }
            }
        }
    }
}

Write-Host ""
Write-Host ("done: downloaded=" + $ok + " cached=" + $cached + " failed=" + $failed)
if ($failed -gt 0) {
    Write-Host "Failed items (re-run to resume; downloaded files are kept):"
    foreach ($f in $failures) { Write-Host ("  " + $f) }
    exit 1
}
exit 0
