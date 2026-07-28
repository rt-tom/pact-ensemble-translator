[CmdletBinding()]
param(
    [string]$ProjectRoot = 'D:\pact\pact_translator_v3',
    [int]$Start = 60,
    [int]$End = 60,
    [switch]$SkipComparison
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
$CollectorVersion = '1.1.0'

$ProjectRoot = (Resolve-Path -LiteralPath $ProjectRoot).Path
$RunName = "chapter_${Start}_to_${End}"
$RunRoot = Join-Path $ProjectRoot "pipeline_runs\$RunName"
$RunnerRoot = Join-Path $ProjectRoot 'pact_full_pipeline_runner_v1'
$BenchmarkRoot = Join-Path $ProjectRoot 'benchmark_results'
$Stamp = Get-Date -Format 'yyyyMMdd_HHmmss'
$BundleRoot = Join-Path $BenchmarkRoot "${RunName}_final_bundle_$Stamp"
$ZipPath = "$BundleRoot.zip"

if (-not (Test-Path -LiteralPath $RunRoot)) {
    throw "Run directory not found: $RunRoot"
}

New-Item -ItemType Directory -Force `
    $BenchmarkRoot,
    $BundleRoot,
    (Join-Path $BundleRoot 'run_data'),
    (Join-Path $BundleRoot 'program'),
    (Join-Path $BundleRoot 'runner'),
    (Join-Path $BundleRoot 'glossary') |
Out-Null

# Generate the visual comparison before packaging.
$CompareScript = Join-Path $RunnerRoot 'compare_pipeline_review.py'
if (-not $SkipComparison -and (Test-Path -LiteralPath $CompareScript)) {
    Write-Host 'Generating review comparison...' -ForegroundColor Cyan
    & py $CompareScript `
        --project-root $ProjectRoot `
        --start $Start `
        --end $End

    if ($LASTEXITCODE -ne 0) {
        throw "compare_pipeline_review.py failed with exit code $LASTEXITCODE"
    }
}

Write-Host 'Copying complete run data...' -ForegroundColor Cyan
Copy-Item `
    -Path (Join-Path $RunRoot '*') `
    -Destination (Join-Path $BundleRoot 'run_data') `
    -Recurse `
    -Force

# Avoid recursively embedding a prior runner-generated ZIP.
Get-ChildItem `
    -LiteralPath (Join-Path $BundleRoot 'run_data') `
    -Filter '*.zip' `
    -File `
    -Recurse `
    -ErrorAction SilentlyContinue |
Remove-Item -Force

$ProgramFiles = @(
    'pact_translate_v3.py',
    'config.v3.json',
    'arc_names.json'
)

foreach ($Name in $ProgramFiles) {
    $Source = Join-Path $ProjectRoot $Name
    if (Test-Path -LiteralPath $Source) {
        Copy-Item `
            -LiteralPath $Source `
            -Destination (Join-Path $BundleRoot 'program') `
            -Force
    }
}

$RunnerFiles = @(
    'run_full_pipeline.ps1',
    'prepare_pipeline_context.py',
    'verify_pipeline_issues.py',
    'verify_repair_results.py',
    'apply_project_fixes.py',
    'monitor_pipeline.ps1',
    'compare_pipeline_review.py'
)

foreach ($Name in $RunnerFiles) {
    $Source = Join-Path $RunnerRoot $Name
    if (Test-Path -LiteralPath $Source) {
        Copy-Item `
            -LiteralPath $Source `
            -Destination (Join-Path $BundleRoot 'runner') `
            -Force
    }
}

$GlossaryRoot = Join-Path $ProjectRoot 'glossary'
if (Test-Path -LiteralPath $GlossaryRoot) {
    Copy-Item `
        -Path (Join-Path $GlossaryRoot '*') `
        -Destination (Join-Path $BundleRoot 'glossary') `
        -Recurse `
        -Force
}

$EnvironmentPath = Join-Path $BundleRoot 'environment_and_inventory.txt'
$LlamaExe = 'C:\llama-cpp\llama-server.exe'
$GemmaModel = 'C:\llama-cpp\models\gemma-4-26B-A4B-it-UD-Q4_K_XL.gguf'
$GemmaMtp = 'C:\llama-cpp\models\MTP\mtp-gemma-4-26B-A4B-it-Q8_0.gguf'
$QwenModel = 'C:\llama-cpp\models\Qwen3.6-35B-A3B\Qwen3.6-35B-A3B-UD-Q4_K_XL.gguf'

$Lines = [System.Collections.Generic.List[string]]::new()
$Lines.Add("Collector version: $CollectorVersion")
$Lines.Add("Collected: $(Get-Date -Format o)")
$Lines.Add("Project root: $ProjectRoot")
$Lines.Add("Run root: $RunRoot")
$Lines.Add("Chapters: $Start-$End")
$Lines.Add('')

$Lines.Add('Python:')
$Lines.Add((& py --version 2>&1 | Out-String).Trim())
$Lines.Add('')

if (Test-Path -LiteralPath $LlamaExe) {
    $Lines.Add('llama.cpp:')
    $Lines.Add((& $LlamaExe --version 2>&1 | Out-String).Trim())
    $LlamaInfo = Get-Item -LiteralPath $LlamaExe
    $Lines.Add("llama-server.exe bytes: $($LlamaInfo.Length)")
    $Lines.Add("llama-server.exe modified: $($LlamaInfo.LastWriteTime.ToString('o'))")
    $Lines.Add("llama-server.exe SHA256: $((Get-FileHash -LiteralPath $LlamaExe -Algorithm SHA256).Hash)")
    $Lines.Add('')
}

$Lines.Add('Model metadata (hashes intentionally omitted):')
foreach ($ModelPath in @($GemmaModel, $GemmaMtp, $QwenModel)) {
    if (Test-Path -LiteralPath $ModelPath) {
        $ModelInfo = Get-Item -LiteralPath $ModelPath
        $Lines.Add(
            "$ModelPath`t$($ModelInfo.Length)`t$($ModelInfo.LastWriteTime.ToString('o'))"
        )
    } else {
        $Lines.Add("MISSING`t$ModelPath")
    }
}
$Lines.Add('')

$Lines.Add('Display adapters:')
$Lines.Add(
    (
        Get-CimInstance Win32_VideoController |
        Select-Object Name, DriverVersion, AdapterRAM |
        Format-Table -AutoSize |
        Out-String
    ).TrimEnd()
)
$Lines.Add('')

$Lines.Add('Run files:')
$Lines.AddRange(
    [string[]](
        Get-ChildItem -LiteralPath $RunRoot -Recurse -File |
        Sort-Object FullName |
        ForEach-Object {
            "$($_.Length)`t$($_.LastWriteTime.ToString('o'))`t$($_.FullName)"
        }
    )
)

$Lines | Set-Content -LiteralPath $EnvironmentPath -Encoding UTF8

# Summarize the final state and important outputs separately.
$SummaryPath = Join-Path $BundleRoot 'bundle_summary.txt'
$FinalOutputs = @(
    Get-ChildItem `
        -LiteralPath (Join-Path $RunRoot 'output') `
        -File `
        -ErrorAction SilentlyContinue
)
$ReviewIndex = Join-Path $RunRoot 'review_comparison\index.html'
$RunnerZip = Get-ChildItem `
    -LiteralPath $RunRoot `
    -Filter 'result_*.zip' `
    -File `
    -ErrorAction SilentlyContinue |
    Select-Object -First 1

@(
    "Run: $RunName"
    "Collected: $(Get-Date -Format o)"
    "Final output files: $($FinalOutputs.Count)"
    "Review report present: $(Test-Path -LiteralPath $ReviewIndex)"
    "Runner result ZIP present before collection: $($null -ne $RunnerZip)"
    ""
    "Expected key files:"
    "  run_data\config.full_pipeline.json"
    "  run_data\work\<chapter>\manifest.json"
    "  run_data\work\<chapter>\draft_translations.json"
    "  run_data\work\<chapter>\issues.qwen_raw.json"
    "  run_data\work\<chapter>\verifier_report.json"
    "  run_data\work\<chapter>\issues.json"
    "  run_data\work\<chapter>\repaired_translations.preverify.json"
    "  run_data\work\<chapter>\post_repair_report.json"
    "  run_data\work\<chapter>\repaired_translations.json"
    "  run_data\output\*.html"
    "  run_data\review_comparison\index.html"
    "  run_data\server_logs\*.log"
) | Set-Content -LiteralPath $SummaryPath -Encoding UTF8

Write-Host 'Compressing bundle...' -ForegroundColor Cyan
if (Test-Path -LiteralPath $ZipPath) {
    Remove-Item -LiteralPath $ZipPath -Force
}

Compress-Archive `
    -Path (Join-Path $BundleRoot '*') `
    -DestinationPath $ZipPath `
    -CompressionLevel Optimal `
    -Force

$Hash = Get-FileHash -LiteralPath $ZipPath -Algorithm SHA256
$ZipInfo = Get-Item -LiteralPath $ZipPath

Write-Host ''
Write-Host 'Final bundle created:' -ForegroundColor Green
Write-Host $ZipPath
Write-Host ("Size: {0:N2} MiB" -f ($ZipInfo.Length / 1MB))
Write-Host "SHA256: $($Hash.Hash)"
