[CmdletBinding()]
param(
    [string]$ProjectRoot = 'D:\pact\pact_translator_v3',
    [int]$Start = 60,
    [int]$End = 60,
    [switch]$Reset,
    [switch]$RedoAudit,
    [switch]$RedoVerifier,
    [switch]$RedoRepair,
    [switch]$RedoFormatting
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
$RunnerVersion = '1.2.0'

$EffectiveRedoRepair = $RedoRepair -or $RedoAudit -or $RedoVerifier
$EffectiveRedoFormatting = $RedoFormatting -or $EffectiveRedoRepair

$ProjectRoot = (Resolve-Path $ProjectRoot).Path
$PackageRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$LlamaRoot = 'C:\llama-cpp'
$LlamaExe = Join-Path $LlamaRoot 'llama-server.exe'
$GemmaModelPath = Join-Path $LlamaRoot 'models\gemma-4-26B-A4B-it-UD-Q4_K_XL.gguf'
$GemmaModelName = 'gemma-4-26B-A4B-it-UD-Q4_K_XL.gguf'
$GemmaMtpPath = Join-Path $LlamaRoot 'models\MTP\mtp-gemma-4-26B-A4B-it-Q8_0.gguf'
$QwenModelPath = Join-Path $LlamaRoot 'models\Qwen3.6-35B-A3B\Qwen3.6-35B-A3B-UD-Q4_K_XL.gguf'
$QwenModelName = 'Qwen3.6-35B-A3B-UD-Q4_K_XL.gguf'
$Python = 'py'

foreach ($required in @(
    $LlamaExe,
    $GemmaModelPath,
    $GemmaMtpPath,
    $QwenModelPath,
    (Join-Path $ProjectRoot 'pact_translate_v3.py'),
    (Join-Path $ProjectRoot 'config.v3.json'),
    (Join-Path $ProjectRoot 'glossary'),
    (Join-Path $PackageRoot 'verify_pipeline_issues.py'),
    (Join-Path $PackageRoot 'verify_repair_results.py'),
    (Join-Path $PackageRoot 'retry_rejected_repairs.py')
)) {
    if (-not (Test-Path $required)) {
        throw "Required path not found: $required"
    }
}

$RunName = "chapter_${Start}_to_${End}"
$RunRoot = Join-Path $ProjectRoot "pipeline_runs\$RunName"
$WorkDir = Join-Path $RunRoot 'work'
$OutputDir = Join-Path $RunRoot 'output'
$LogsDir = Join-Path $RunRoot 'logs'
$ServerLogsDir = Join-Path $RunRoot 'server_logs'
$GlossaryDir = Join-Path $RunRoot 'glossary'
$ConfigPath = Join-Path $RunRoot 'config.full_pipeline.json'
$BookBiblePath = Join-Path $RunRoot 'book_bible.json'

$AllInputFiles = @(
    Get-ChildItem (Join-Path $ProjectRoot 'pact_chapters') -Filter '*.html' -File |
        Sort-Object Name
)
$SelectedInputFiles = @(
    $AllInputFiles |
        Select-Object -Skip ([math]::Max(0, $Start - 1)) `
            -First ([math]::Max(0, $End - $Start + 1))
)
if ($SelectedInputFiles.Count -ne ($End - $Start + 1)) {
    throw "Requested chapter range $Start-$End is outside available inputs."
}
$SelectedChapterStems = @($SelectedInputFiles | ForEach-Object BaseName)

function Get-PropertyValue {
    param($Object, [string]$Name, $Default = $null)
    if ($null -eq $Object) { return $Default }
    if ($Object -is [System.Collections.IDictionary]) {
        if ($Object.Contains($Name)) { return $Object[$Name] }
        return $Default
    }
    $property = $Object.PSObject.Properties[$Name]
    if ($null -ne $property) { return $property.Value }
    return $Default
}

function Test-AllChapterArtifact {
    param([Parameter(Mandatory)][string]$RelativePath)
    foreach ($stem in $SelectedChapterStems) {
        if (-not (Test-Path (Join-Path (Join-Path $WorkDir $stem) $RelativePath))) {
            return $false
        }
    }
    return $true
}

function Test-AllChapterReportMajorVersion {
    param(
        [Parameter(Mandatory)][string]$RelativePath,
        [Parameter(Mandatory)][int]$MinimumMajor
    )
    foreach ($stem in $SelectedChapterStems) {
        $path = Join-Path (Join-Path $WorkDir $stem) $RelativePath
        if (-not (Test-Path -LiteralPath $path)) {
            return $false
        }
        $data = Get-Content $path -Raw | ConvertFrom-Json
        $version = [string](Get-PropertyValue $data 'version' '0')
        $majorText = ($version -split '\.')[0]
        $major = 0
        if (-not [int]::TryParse($majorText, [ref]$major)) {
            return $false
        }
        if ($major -lt $MinimumMajor) {
            return $false
        }
    }
    return $true
}

function Test-AllPostRepairResolved {
    foreach ($stem in $SelectedChapterStems) {
        $path = Join-Path (Join-Path $WorkDir $stem) 'post_repair_report.json'
        if (-not (Test-Path -LiteralPath $path)) {
            return $false
        }
        $data = Get-Content $path -Raw | ConvertFrom-Json
        $unresolved = [int](Get-PropertyValue $data 'unresolved_total' -1)
        if ($unresolved -ne 0) {
            return $false
        }
    }
    return $true
}

if ($Reset -and (Test-Path $RunRoot)) {
    Write-Host "Removing previous run: $RunRoot" -ForegroundColor Yellow
    Remove-Item $RunRoot -Recurse -Force
}

New-Item -ItemType Directory -Force -Path @($RunRoot, $WorkDir, $OutputDir, $LogsDir, $ServerLogsDir) | Out-Null
if (-not (Test-Path $GlossaryDir)) {
    Copy-Item (Join-Path $ProjectRoot 'glossary') $GlossaryDir -Recurse -Force
}

# Generate an isolated production config. The installed project glossary is copied
# into this run and cannot be mutated by model-generated chapter-bible candidates.
$cfg = Get-Content (Join-Path $ProjectRoot 'config.v3.json') -Raw |
    ConvertFrom-Json -AsHashtable

function Get-OrCreateConfigSection {
    param(
        [Parameter(Mandatory)]
        [System.Collections.IDictionary]$Config,

        [Parameter(Mandatory)]
        [string]$Name
    )

    if (
        -not $Config.Contains($Name) -or
        -not ($Config[$Name] -is [System.Collections.IDictionary])
    ) {
        $Config[$Name] = @{}
    }

    return $Config[$Name]
}

$translatorApi = Get-OrCreateConfigSection $cfg 'translator_api'
$reviewerApi = Get-OrCreateConfigSection $cfg 'reviewer_api'
$paths = Get-OrCreateConfigSection $cfg 'paths'
$chapterBible = Get-OrCreateConfigSection $cfg 'chapter_bible'
$translation = Get-OrCreateConfigSection $cfg 'translation'
$audit = Get-OrCreateConfigSection $cfg 'audit'
$repair = Get-OrCreateConfigSection $cfg 'repair'
$formatting = Get-OrCreateConfigSection $cfg 'formatting'
$glossary = Get-OrCreateConfigSection $cfg 'glossary'
$validation = Get-OrCreateConfigSection $cfg 'validation'
$verifier = Get-OrCreateConfigSection $cfg 'verifier'
$postRepair = Get-OrCreateConfigSection $cfg 'post_repair_verifier'

$translatorApi['model'] = $GemmaModelName
$translatorApi['context_size'] = 32768

$reviewerApi['enabled'] = $true
$reviewerApi['model'] = $QwenModelName
$reviewerApi['context_size'] = 32768

$paths['input_dir'] = (Join-Path $ProjectRoot 'pact_chapters')
$paths['output_dir'] = $OutputDir
$paths['work_dir'] = $WorkDir
$paths['logs_dir'] = $LogsDir
$paths['glossary_dir'] = $GlossaryDir
$paths['book_bible_file'] = $BookBiblePath
$paths['arc_names_file'] = (Join-Path $ProjectRoot 'arc_names.json')

$chapterBible['enabled'] = $true
$chapterBible['required'] = $true
$chapterBible['temperature'] = 0.0
$chapterBible['enable_thinking'] = $false

$translation['temperature'] = 0.0
$translation['top_p'] = 0.95
$translation['top_k'] = 64
$translation['enable_thinking'] = $false

$audit['enabled'] = $true
$audit['required'] = $true
$audit['temperature'] = 0.0
$audit['top_p'] = 1.0
$audit['top_k'] = 64
$audit['enable_thinking'] = $false
$audit['batch_pids'] = 8
$audit['context_before'] = 2
$audit['context_after'] = 2
$audit['max_tokens'] = 1200
$audit['generation_retries'] = 2
$audit['max_issues_per_batch'] = 5
$audit['include_deterministic_suspects'] = $false
$audit['split_on_failure'] = $true
$audit['fail_open'] = $true
$audit['minimum_success_rate'] = 0.90

$repair['enabled'] = $true
$repair['required'] = $true
$repair['temperature'] = 0.0
$repair['top_p'] = 1.0
$repair['top_k'] = 64
$repair['enable_thinking'] = $false
$repair['max_pids_per_call'] = 1
$repair['max_tokens'] = 1200
$repair['generation_retries'] = 3
$repair['auto_repair_verified_decisions'] = @('repair')
$repair['auto_repair_verifier_confidences'] = @('high', 'deterministic')
$repair['retry_on_keep_or_invalid'] = $true
$repair['context_before'] = 1
$repair['context_after'] = 1
$repair['auto_repair_deterministic_categories'] = @(
    'missing', 'number', 'number_word', 'english_residue', 'mixed_script',
    'entity_consistency', 'narrator_gender', 'name_consistency',
    'tone_profanity', 'length_outlier'
)

$formatting['enabled'] = $true
$formatting['required'] = $false
$formatting['temperature'] = 0.0
$formatting['enable_thinking'] = $false
$formatting['retry_unresolved_spans'] = $true

$glossary['include_provisional_in_prompt'] = $false
$validation['english_sequence_min_words'] = 2
$validation['english_residue_is_error'] = $false

$verifier['hard_deterministic_categories'] = @(
    'missing', 'mixed_script'
)
$verifier['fail_on_uncertain'] = $true

$postRepair['enabled'] = $true
$postRepair['required'] = $true
$postRepair['temperature'] = 0.0
$postRepair['top_p'] = 1.0
$postRepair['enable_thinking'] = $true
$postRepair['reasoning_budget'] = 128
$postRepair['reject_policy'] = 'revert_to_draft'
$postRepair['uncertain_policy'] = 'revert_to_draft'
$postRepair['max_repair_rounds'] = 2
$postRepair['fail_on_unresolved'] = $true
$postRepair['accept_confidences'] = @('high')

$cfg | ConvertTo-Json -Depth 50 |
    Set-Content $ConfigPath -Encoding UTF8

$script:ServerProcess = $null

function Stop-LlamaServer {
    if ($script:ServerProcess -and -not $script:ServerProcess.HasExited) {
        Write-Host "Stopping llama-server PID $($script:ServerProcess.Id)..." -ForegroundColor DarkYellow
        Stop-Process -Id $script:ServerProcess.Id -Force -ErrorAction SilentlyContinue
        try { $script:ServerProcess.WaitForExit(10000) | Out-Null } catch {}
    }
    $script:ServerProcess = $null
    Get-Process llama-server -ErrorAction SilentlyContinue |
        Stop-Process -Force -ErrorAction SilentlyContinue
    Start-Sleep -Seconds 2
}

function Start-LlamaServer {
    param(
        [ValidateSet('GemmaTranslate','GemmaRepair','GemmaVerify','Qwen')]
        [string]$Profile
    )

    Stop-LlamaServer
    $env:GGML_VK_DISABLE_COOPMAT = '1'
    $stamp = Get-Date -Format 'yyyyMMdd_HHmmss'
    $stdout = Join-Path $ServerLogsDir "${Profile}_${stamp}_stdout.log"
    $stderr = Join-Path $ServerLogsDir "${Profile}_${stamp}_stderr.log"

    switch ($Profile) {
        'GemmaTranslate' {
            # Exact tuned Pact translation profile:
            # MTP Q8 draft, n_max=4, 32K context, 18 CPU MoE layers,
            # no reasoning. Used for context preparation, translation,
            # repair, formatting, and finalization.
            $serverArgs = @(
                '-m', $GemmaModelPath,
                '--model-draft', $GemmaMtpPath,
                '--spec-type', 'draft-mtp',
                '--spec-draft-n-max', '4',
                '--device', 'Vulkan0',
                '--host', '127.0.0.1',
                '--port', '8080',
                '-ngl', '99',
                '-ncmoe', '18',
                '--no-mmap',
                '--reasoning-budget', '0',
                '-np', '1',
                '-c', '32768',
                '-fa', 'on',
                '--jinja',
                '--cache-ram', '0',
                '--ctx-checkpoints', '0'
            )
        }

        'GemmaRepair' {
            # Stable non-speculative profile for constrained JSON repair calls.
            # MTP remains enabled for bulk translation, but repair uses the
            # target model alone to avoid llama-server 500s on runaway draft
            # generation and structured responses.
            $serverArgs = @(
                '-m', $GemmaModelPath,
                '--device', 'Vulkan0',
                '--host', '127.0.0.1',
                '--port', '8080',
                '-c', '32768',
                '-fit', 'on',
                '-fitt', '1536',
                '-t', '6',
                '-tb', '12',
                '--no-mmap',
                '--reasoning-budget', '0',
                '-np', '1',
                '-fa', 'on',
                '--jinja',
                '--cache-ram', '0',
                '--ctx-checkpoints', '0'
            )
        }

        'GemmaVerify' {
            # Profile used in the verifier benchmark:
            # no speculative MTP; thinking is available and capped at 128.
            # verify_pipeline_issues.py explicitly sends enable_thinking=true.
            $serverArgs = @(
                '-m', $GemmaModelPath,
                '--device', 'Vulkan0',
                '--host', '127.0.0.1',
                '--port', '8080',
                '-c', '32768',
                '-fit', 'on',
                '-fitt', '1536',
                '-t', '6',
                '-tb', '12',
                '--no-mmap',
                '--reasoning-budget', '128',
                '-np', '1',
                '-fa', 'on',
                '--jinja',
                '--cache-ram', '0',
                '--ctx-checkpoints', '0'
            )
        }

        'Qwen' {
            # Restored final Qwen profile confirmed by the 3-round benchmark:
            # 32K, fitt=1280, batch=2048, ubatch=512, KV Q8, mmap off.
            $serverArgs = @(
                '-m', $QwenModelPath,
                '--device', 'Vulkan0',
                '--host', '127.0.0.1',
                '--port', '8080',
                '-c', '32768',
                '-fit', 'on',
                '-fitt', '1280',
                '-b', '2048',
                '-ub', '512',
                '-ctk', 'q8_0',
                '-ctv', 'q8_0',
                '-t', '6',
                '-tb', '12',
                '--no-mmap',
                '--reasoning-budget', '0',
                '-np', '1',
                '-fa', 'on',
                '--jinja',
                '--cache-ram', '0',
                '--ctx-checkpoints', '0'
            )
        }
    }

    Write-Host "Starting $Profile..." -ForegroundColor Cyan
    $script:ServerProcess = Start-Process `
        -FilePath $LlamaExe `
        -WorkingDirectory $LlamaRoot `
        -ArgumentList $serverArgs `
        -RedirectStandardOutput $stdout `
        -RedirectStandardError $stderr `
        -WindowStyle Hidden `
        -PassThru

    $ready = $false
    for ($i = 0; $i -lt 240; $i++) {
        if ($script:ServerProcess.HasExited) {
            throw "$Profile llama-server exited during startup. See $stderr"
        }
        try {
            $health = Invoke-RestMethod `
                -Uri 'http://127.0.0.1:8080/health' `
                -TimeoutSec 2
            if (
                $health.status -eq 'ok' -or
                $health.status -eq 'no slot available'
            ) {
                $ready = $true
                break
            }
        } catch {}
        Start-Sleep -Seconds 1
    }

    if (-not $ready) {
        throw "$Profile server did not become ready. See $stderr"
    }

    Write-Host `
        "$Profile ready (PID $($script:ServerProcess.Id)). Log: $stderr" `
        -ForegroundColor Green
}

function Invoke-PythonStage {
    param(
        [string]$Label,
        [string[]]$Arguments
    )
    Write-Host "`n=== $Label ===" -ForegroundColor Magenta
    Push-Location $ProjectRoot
    try {
        & $Python @Arguments
        if ($LASTEXITCODE -ne 0) {
            throw "$Label failed with exit code $LASTEXITCODE"
        }
    } finally {
        Pop-Location
    }
}

try {
    Write-Host "Pact full pipeline runner v$RunnerVersion" -ForegroundColor White
    Write-Host "Run root: $RunRoot" -ForegroundColor White
    Write-Host "Chapters: $Start-$End" -ForegroundColor White

    Start-LlamaServer -Profile GemmaTranslate
    Invoke-PythonStage -Label '1/6 Prepare and sanitize chapter context' -Arguments @(
        (Join-Path $PackageRoot 'prepare_pipeline_context.py'),
        '--project-root', $ProjectRoot,
        '--config', $ConfigPath,
        '--start', "$Start", '--end', "$End"
    )
    Invoke-PythonStage -Label '2/6 Gemma translation' -Arguments @(
        '.\pact_translate_v3.py', '--config', $ConfigPath,
        '--phase', 'translate', '--start', "$Start", '--end', "$End"
    )

    $verifierArtifactExists = Test-AllChapterArtifact -RelativePath 'verifier_report.json'
    $verifierComplete = Test-AllChapterReportMajorVersion -RelativePath 'verifier_report.json' -MinimumMajor 2
    $autoRedoVerifier = $verifierArtifactExists -and -not $verifierComplete -and -not $RedoAudit
    $effectiveRedoVerifier = $RedoVerifier -or $autoRedoVerifier
    if ($autoRedoVerifier) {
        Write-Host "`nVerifier policy contract changed; saved raw Qwen candidates will be re-verified." -ForegroundColor Yellow
        $EffectiveRedoRepair = $true
        $EffectiveRedoFormatting = $true
    }
    if ($verifierComplete -and -not $RedoAudit -and -not $effectiveRedoVerifier) {
        Write-Host "`n=== 3-4/6 Audit and candidate verification already complete; reusing saved results ===" -ForegroundColor DarkGreen
    }
    else {
        if (-not $effectiveRedoVerifier -or $RedoAudit) {
            Start-LlamaServer -Profile Qwen
            $auditArguments = @(
                '.\pact_translate_v3.py', '--config', $ConfigPath,
                '--phase', 'audit', '--start', "$Start", '--end', "$End"
            )
            if ($RedoAudit) {
                $auditArguments += '--redo-audit'
            }
            Invoke-PythonStage -Label '3/6 Qwen bilingual audit' -Arguments $auditArguments
        }
        else {
            Write-Host "`n=== 3/6 Reusing saved raw Qwen candidates ===" -ForegroundColor DarkGreen
        }

        Start-LlamaServer -Profile GemmaVerify
        $verifierArguments = @(
            (Join-Path $PackageRoot 'verify_pipeline_issues.py'),
            '--config', $ConfigPath,
            '--start', "$Start", '--end', "$End",
            '--model', $GemmaModelName,
            '--attempts', '3', '--max-tokens', '768', '--context-size', '2'
        )
        if ($RedoAudit -or $effectiveRedoVerifier) {
            $verifierArguments += '--force'
        }
        if ($effectiveRedoVerifier -and -not $RedoAudit) {
            $verifierArguments += '--reuse-raw-backup'
        }
        Invoke-PythonStage -Label '4/6 Gemma verifies Qwen candidates (thinking 128)' -Arguments $verifierArguments
    }

    $postArtifactExists = Test-AllChapterArtifact -RelativePath 'post_repair_report.json'
    $postRepairComplete = (
        (Test-AllChapterReportMajorVersion -RelativePath 'post_repair_report.json' -MinimumMajor 2) -and
        (Test-AllPostRepairResolved)
    )
    if ($postArtifactExists -and -not $postRepairComplete) {
        Write-Host "`nPost-repair policy/results are stale or unresolved; repair stages will be rebuilt." -ForegroundColor Yellow
        $EffectiveRedoRepair = $true
        $EffectiveRedoFormatting = $true
    }
    if ($postRepairComplete -and -not $EffectiveRedoRepair) {
        Write-Host "`n=== 5/6 Repair and post-repair safety verification already complete; reusing saved results ===" -ForegroundColor DarkGreen
    }
    else {
        # Repair uses a stable thinking-off target-only profile.
        Start-LlamaServer -Profile GemmaRepair
        $repairArguments = @(
            '.\pact_translate_v3.py', '--config', $ConfigPath,
            '--phase', 'repair', '--start', "$Start", '--end', "$End"
        )
        if ($EffectiveRedoRepair) {
            $repairArguments += '--redo-repair'
        }
        Invoke-PythonStage -Label '5a/6 Gemma repairs confirmed issues' -Arguments $repairArguments

        # Every candidate is checked on four independent gates. Rejected, uncertain,
        # keep, and no-change results enter an autonomous repair -> verify retry loop.
        Start-LlamaServer -Profile GemmaVerify
        $postRepairArguments = @(
            (Join-Path $PackageRoot 'verify_repair_results.py'),
            '--config', $ConfigPath,
            '--start', "$Start", '--end', "$End",
            '--model', $GemmaModelName,
            '--attempts', '3', '--max-tokens', '768', '--context-size', '1',
            '--round', '1'
        )
        if ($EffectiveRedoRepair) {
            $postRepairArguments += '--force'
        }
        Invoke-PythonStage -Label '5b/6 Gemma safety-checks every repair' -Arguments $postRepairArguments

        function Get-UnresolvedRepairCount {
            $total = 0
            foreach ($stem in $SelectedChapterStems) {
                $reportPath = Join-Path (Join-Path $WorkDir $stem) 'post_repair_report.json'
                if (-not (Test-Path -LiteralPath $reportPath)) {
                    throw "Post-repair report missing: $reportPath"
                }
                $report = Get-Content $reportPath -Raw | ConvertFrom-Json
                $retryFallback = [int](Get-PropertyValue $report 'retry_required' 0)
                $value = [int](Get-PropertyValue $report 'unresolved_total' $retryFallback)
                $total += $value
            }
            return $total
        }

        $maxRetryRounds = [int]$postRepair['max_repair_rounds']
        for ($retryRound = 1; $retryRound -le $maxRetryRounds; $retryRound++) {
            $unresolved = Get-UnresolvedRepairCount
            if ($unresolved -eq 0) {
                break
            }

            Write-Host "`n=== 5c/6 Repair retry round $retryRound for $unresolved unresolved PID(s) ===" -ForegroundColor Yellow
            Start-LlamaServer -Profile GemmaRepair
            Invoke-PythonStage -Label "5c/6 Gemma retries rejected repairs (round $retryRound)" -Arguments @(
                (Join-Path $PackageRoot 'retry_rejected_repairs.py'),
                '--config', $ConfigPath,
                '--start', "$Start", '--end', "$End",
                '--model', $GemmaModelName,
                '--attempts', '3', '--max-tokens', '1200',
                '--round', "$retryRound"
            )

            $verifyRound = $retryRound + 1
            Start-LlamaServer -Profile GemmaVerify
            Invoke-PythonStage -Label "5d/6 Gemma rechecks retry round $retryRound" -Arguments @(
                (Join-Path $PackageRoot 'verify_repair_results.py'),
                '--config', $ConfigPath,
                '--start', "$Start", '--end', "$End",
                '--model', $GemmaModelName,
                '--attempts', '3', '--max-tokens', '768', '--context-size', '1',
                '--round', "$verifyRound", '--continue-round'
            )
        }

        $unresolved = Get-UnresolvedRepairCount
        if ($unresolved -gt 0 -and [bool]$postRepair['fail_on_unresolved']) {
            throw "$unresolved verifier-approved repair PID(s) remain unresolved after $maxRetryRounds retry round(s)."
        }
    }

    # Formatting/finalization returns to the fast MTP profile.
    Start-LlamaServer -Profile GemmaTranslate
    $finalizeArguments = @(
        '.\pact_translate_v3.py', '--config', $ConfigPath,
        '--phase', 'finalize', '--start', "$Start", '--end', "$End"
    )
    if ($EffectiveRedoFormatting) {
        $finalizeArguments += '--redo-formatting'
    }
    Invoke-PythonStage -Label '6/6 Restore formatting and finalize HTML' -Arguments $finalizeArguments

    Stop-LlamaServer

    $bundle = Join-Path $RunRoot "result_${RunName}.zip"
    $items = @($ConfigPath, $OutputDir, $WorkDir, $LogsDir, $ServerLogsDir) |
        Where-Object { Test-Path $_ }
    Compress-Archive -Path $items -DestinationPath $bundle -Force

    Write-Host "`nPIPELINE COMPLETE" -ForegroundColor Green
    Write-Host "Output directory: $OutputDir" -ForegroundColor Green
    Write-Host "Diagnostic bundle: $bundle" -ForegroundColor Green
    Get-ChildItem $OutputDir -File | Select-Object Name, Length, LastWriteTime | Format-Table -AutoSize
}
catch {
    Write-Host "`nPIPELINE FAILED: $($_.Exception.Message)" -ForegroundColor Red
    Write-Host "Run data preserved at: $RunRoot" -ForegroundColor Yellow
    throw
}
finally {
    Stop-LlamaServer
}
