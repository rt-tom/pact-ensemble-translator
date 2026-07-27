[CmdletBinding()]
param(
    [string]$ProjectRoot = 'D:\pact\pact_translator_v3',
    [int]$Start = 60,
    [int]$End = 60,
    [string]$Destination = "$HOME\Desktop"
)

$ErrorActionPreference = 'Stop'
$runner = Split-Path -Parent $MyInvocation.MyCommand.Path
$run = Join-Path $ProjectRoot "pipeline_runs\chapter_${Start}_to_${End}_v31"
if (-not (Test-Path $run)) { throw "Run not found: $run" }
$config = Join-Path $run 'config.full_pipeline.v31.json'
$chapterManifest = Join-Path $run 'chapter_manifest.v31.json'
if (-not (Test-Path $config) -or -not (Test-Path $chapterManifest)) { throw "Canonical run manifest/config not found in $run" }
$inputDir = (Get-Content $config -Raw | ConvertFrom-Json).paths.input_dir
& py (Join-Path $runner 'v31_chapter_resolver.py') --project-root $ProjectRoot --input-dir $inputDir --start $Start --end $End --manifest $chapterManifest | Out-Null
if ($LASTEXITCODE -ne 0) { throw "Canonical chapter manifest verification failed" }
$stamp = Get-Date -Format 'yyyyMMdd_HHmmss'
$stage = Join-Path $env:TEMP "pact_v31_bundle_$stamp"
$zip = Join-Path $Destination "chapter_${Start}_to_${End}_v31_bundle_$stamp.zip"
Remove-Item $stage -Recurse -Force -ErrorAction SilentlyContinue
New-Item -ItemType Directory -Path $stage -Force | Out-Null
Copy-Item $run (Join-Path $stage 'run_data') -Recurse -Force
New-Item -ItemType Directory -Path (Join-Path $stage 'program') -Force | Out-Null
Copy-Item (Join-Path $ProjectRoot 'pact_translate_v3.py') (Join-Path $stage 'program') -Force
Copy-Item (Join-Path $ProjectRoot 'config.v3.json') (Join-Path $stage 'program') -Force
Copy-Item (Join-Path $ProjectRoot 'arc_names.json') (Join-Path $stage 'program') -Force
Copy-Item $runner (Join-Path $stage 'runner') -Recurse -Force
if (Test-Path (Join-Path $ProjectRoot 'glossary')) { Copy-Item (Join-Path $ProjectRoot 'glossary') (Join-Path $stage 'glossary') -Recurse -Force }
@"
Pact pipeline: 3.1
Source run: $run
Created: $(Get-Date -Format o)
"@ | Set-Content (Join-Path $stage 'bundle_summary.txt') -Encoding UTF8
Compress-Archive -Path (Join-Path $stage '*') -DestinationPath $zip -Force
Remove-Item $stage -Recurse -Force
Write-Host "Created: $zip" -ForegroundColor Green
