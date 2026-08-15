<#
.SYNOPSIS
  Build book.html from v4 book-run chapters in explicitly listed out-bases.

.DESCRIPTION
  Collects chapter_* run dirs (each holding a final translations.json)
  from the given out-bases under <Root> and assembles one book.html via
  v4_book_html. Chapter ORDER is the resolved chapter_id natural sort
  (0001..0005..), never file mtime or scan order.

  Explicit -Runs list: old/experimental out-bases are NOT auto-included,
  so duplicate chapter_ids cannot accidentally shadow the current runs
  (e.g. v4_book_0001-0002_remote also holds chapter_0001 — auto-scan
  would pick it first and silently use the stale translation).

  Usage:
    .\build-book.ps1 -Runs v4_book_0001-0003_local,v4_book_0004-0005_remote_luna
    .\build-book.ps1 -Runs "v4_book_*"                     # glob allowed
    .\build-book.ps1 -Runs v4_book_0001-0003_local -Title "Bonds 1.1-1.3"
    .\build-book.ps1 -Runs v4_book_* -Output D:\pact\book.html

.PARAMETER Root
  Parent directory holding the out-bases. Default D:\pact\gate_bench_runs.

.PARAMETER Runs
  Comma-separated out-base dir names (or glob patterns) under Root.
  Required. Example: v4_book_0001-0003_local,v4_book_0004-0005_remote_luna

.PARAMETER ChapterHtmlPattern
  Source-chapter HTML pattern with {chapter_id}.
  Default D:/pact/pact_chapters/{chapter_id}.html

.PARAMETER Title
  Book title. Default "Book".

.PARAMETER Output
  Where to write book.html. Default <Root>\book\book.html (report next to it).
#>
[CmdletBinding()]
param(
    [string]$Root = "D:\pact\gate_bench_runs",
    [Parameter(Mandatory = $true)]
    [string]$Runs,
    [string]$ChapterHtmlPattern = "D:/pact/pact_chapters/{chapter_id}.html",
    [string]$Title = "Book",
    [string]$Output = ""
)

$ErrorActionPreference = "Stop"

if (-not (Test-Path $Root)) {
    Write-Error "Root directory not found: $Root"
    exit 2
}

$runDirs = @()
$baseNames = @()
foreach ($entry in ($Runs -split "," | ForEach-Object { $_.Trim() })) {
    if ($entry -eq "") { continue }
    $bases = Get-ChildItem -Path $Root -Directory -Filter $entry
    if (-not $bases) {
        Write-Warning "No out-base matching '$entry' under $Root"
        continue
    }
    foreach ($base in $bases) {
        $baseNames += $base.Name
        foreach ($ch in Get-ChildItem -Path $base.FullName -Directory -Filter "chapter_*") {
            if (Test-Path (Join-Path $ch.FullName "translations.json")) {
                $runDirs += $ch.FullName
            }
        }
    }
}

if ($runDirs.Count -eq 0) {
    Write-Warning "No chapter_* run dirs with translations.json found (Runs: $Runs)"
    exit 1
}

$outBase = Join-Path $Root "book"
if ($Output -ne "") {
    $outBase = Split-Path $Output -Parent
}

Write-Host "Out-bases: $($baseNames -join ', ')"
Write-Host "Found $($runDirs.Count) chapter run dirs. Chapter order = resolved chapter_id natural sort."
$runDirs | Sort-Object | ForEach-Object { Write-Host "  $_" }

$args = @(
    "-m", "pact_full_pipeline_runner_v1.v4_book_html",
    "--out-base", $outBase,
    "--chapter-html-pattern", $ChapterHtmlPattern,
    "--title", $Title,
    "--run-dirs"
)
foreach ($d in ($runDirs | Sort-Object)) {
    $args += $d
}
if ($Output -ne "") {
    $args += "--output", $Output
}

$py = Get-Command python -ErrorAction SilentlyContinue
# Prefer the project interpreter that has beautifulsoup4 (the Hermes venv
# on PATH may not); C:\Python314 is the known-good one for this repo.
if (-not $py) { Write-Error "python not found on PATH"; exit 2 }
if (Test-Path "C:\Python314\python.exe") {
    & "C:\Python314\python.exe" @args
} else {
    & python @args
}
exit $LASTEXITCODE
