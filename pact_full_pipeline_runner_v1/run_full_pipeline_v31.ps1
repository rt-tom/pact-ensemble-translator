[CmdletBinding()]
param(
    [string]$ProjectRoot = 'D:\pact\pact_translator_v3',
    [int]$Start = 60,
    [int]$End = 60,
    [switch]$Reset,
    [switch]$RedoSourceAnalysis,
    [switch]$RedoTranslation,
    [switch]$RedoQuality,
    [switch]$RedoFormatting,
    [switch]$DryRun,
    [switch]$SkipPreflight
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
# BuildIdentity is for release/milestone reporting only.  ArtifactVersion is
# the semantic identity shared by the config and every Python stage artifact.
$BuildIdentity = '3.1.3-03'
$ArtifactVersion = '3.1.3'

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

$RequiredRunnerFiles = @(
    'prepare_pipeline_context.py',
    'v31_chapter_resolver.py',
    'v31_common.py',
    'v31_preflight_policy.ps1',
    'v31_runner_model_policy.ps1',
    'v31_stage_protocol.py',
    'v31_source_analysis.py',
    'v31_artifact_dag.py',
    'v31_audit.py',
    'v31_merge_issues.py',
    'v31_cross_verify.py',
    'v31_finalize_verification.py',
    'v31_repair.py',
    'v31_postcheck.py',
    'v31_deterministic_gate.py',
    'v31_adjudicate.py',
    'v31_final_lifecycle.py',
    'v31_finalize_quality.py',
    'v31_build_review.py'
)
foreach ($required in @($LlamaExe, $GemmaModelPath, $GemmaMtpPath, $QwenModelPath,
    (Join-Path $ProjectRoot 'pact_translate_v3.py'),
    (Join-Path $ProjectRoot 'config.v3.json'),
    (Join-Path $ProjectRoot 'glossary'))) {
    if (-not (Test-Path $required)) { throw "Required path not found: $required" }
}
foreach ($name in $RequiredRunnerFiles) {
    $path = Join-Path $PackageRoot $name
    if (-not (Test-Path $path)) { throw "Required runner file not found: $path" }
}
. (Join-Path $PackageRoot 'v31_preflight_policy.ps1')
. (Join-Path $PackageRoot 'v31_runner_model_policy.ps1')

$RunName = "chapter_${Start}_to_${End}_v31"
$RunRoot = Join-Path $ProjectRoot "pipeline_runs\$RunName"
$WorkDir = Join-Path $RunRoot 'work'
$OutputDir = Join-Path $RunRoot 'output'
$LogsDir = Join-Path $RunRoot 'logs'
$ServerLogsDir = Join-Path $RunRoot 'server_logs'
$GlossaryDir = Join-Path $RunRoot 'glossary'
$ConfigPath = Join-Path $RunRoot 'config.full_pipeline.v31.json'
$BookBiblePath = Join-Path $RunRoot 'book_bible.json'
$ChapterManifestPath = Join-Path $RunRoot 'chapter_manifest.v31.json'
$MonitorStatePath = Join-Path $RunRoot 'monitor_state.v31.json'

$SelectedInputFiles = @()
$SelectedChapterStems = @()

if ($Reset -and (Test-Path $RunRoot)) {
    Write-Host "Removing previous v3.1 run: $RunRoot" -ForegroundColor Yellow
    Remove-Item $RunRoot -Recurse -Force
}
New-Item -ItemType Directory -Force -Path @($RunRoot, $WorkDir, $OutputDir, $LogsDir, $ServerLogsDir) | Out-Null
if (-not (Test-Path $GlossaryDir)) { Copy-Item (Join-Path $ProjectRoot 'glossary') $GlossaryDir -Recurse -Force }
if (-not (Test-Path $BookBiblePath)) {
    $sourceBookBible = Join-Path $ProjectRoot 'book_bible.json'
    if (Test-Path $sourceBookBible) { Copy-Item $sourceBookBible $BookBiblePath -Force }
    else {
        [System.IO.File]::WriteAllText(
            $BookBiblePath,
            '{}',
            [System.Text.UTF8Encoding]::new($false)
        )
    }
}

function Get-OrCreateSection {
    param([System.Collections.IDictionary]$Config, [string]$Name)
    if (-not $Config.Contains($Name) -or -not ($Config[$Name] -is [System.Collections.IDictionary])) { $Config[$Name] = @{} }
    return $Config[$Name]
}

# Do not rely on the optional hashtable-output parameter of ConvertFrom-Json.
# A user profile or compatibility module may expose an older implementation.
# Convert the ordinary PSCustomObject result recursively instead.
function ConvertTo-HashtableRecursive {
    param($InputObject)

    if ($null -eq $InputObject) {
        return $null
    }

    if ($InputObject -is [System.Collections.IDictionary]) {
        $result = @{}
        foreach ($key in $InputObject.Keys) {
            $result[[string]$key] = ConvertTo-HashtableRecursive -InputObject $InputObject[$key]
        }
        return $result
    }

    if ($InputObject -is [pscustomobject]) {
        $result = @{}
        foreach ($property in $InputObject.PSObject.Properties) {
            $result[$property.Name] = ConvertTo-HashtableRecursive -InputObject $property.Value
        }
        return $result
    }

    if (
        $InputObject -is [System.Collections.IEnumerable] -and
        -not ($InputObject -is [string])
    ) {
        $items = @()
        foreach ($item in $InputObject) {
            $items += ,(ConvertTo-HashtableRecursive -InputObject $item)
        }
        return ,$items
    }

    return $InputObject
}

$configJson = Get-Content (Join-Path $ProjectRoot 'config.v3.json') -Raw
$configObject = Microsoft.PowerShell.Utility\ConvertFrom-Json -InputObject $configJson
$cfg = ConvertTo-HashtableRecursive -InputObject $configObject
$translatorApi = Get-OrCreateSection $cfg 'translator_api'
$reviewerApi = Get-OrCreateSection $cfg 'reviewer_api'
$paths = Get-OrCreateSection $cfg 'paths'
$chapterBible = Get-OrCreateSection $cfg 'chapter_bible'
$translation = Get-OrCreateSection $cfg 'translation'
$audit = Get-OrCreateSection $cfg 'audit'
$repairLegacy = Get-OrCreateSection $cfg 'repair'
$formatting = Get-OrCreateSection $cfg 'formatting'
$glossary = Get-OrCreateSection $cfg 'glossary'
$validation = Get-OrCreateSection $cfg 'validation'
$postRepair = Get-OrCreateSection $cfg 'post_repair_verifier'
$ensemble = Get-OrCreateSection $cfg 'ensemble_v31'

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
$paths['chapter_manifest_file'] = $ChapterManifestPath

$chapterBible['enabled'] = $true
$chapterBible['required'] = $true
$chapterBible['temperature'] = 0.0
$chapterBible['enable_thinking'] = $false
$translation['temperature'] = 0.0
$translation['top_p'] = 0.95
$translation['top_k'] = 64
$translation['enable_thinking'] = $false
$translation['generation_retries'] = 3
$audit['enabled'] = $false
$repairLegacy['enabled'] = $false
$formatting['enabled'] = $true
$formatting['required'] = $false
$formatting['temperature'] = 0.0
$formatting['enable_thinking'] = $false
$formatting['retry_unresolved_spans'] = $true
$glossary['include_provisional_in_prompt'] = $false
$validation['english_sequence_min_words'] = 2
$validation['english_residue_is_error'] = $false
$deterministicQa = Get-OrCreateSection $cfg 'deterministic_qa'
$deterministicQa['mixed_script_check'] = $true
if (-not $deterministicQa.Contains('mixed_script_allow')) { $deterministicQa['mixed_script_allow'] = @() }
$postRepair['enabled'] = $true
$postRepair['required'] = $true
$postRepair['fail_on_unresolved'] = $true

$ensemble['version'] = $ArtifactVersion
$ensemble['source_analysis'] = @{ temperature=0.0; top_p=1.0; top_k=64; enable_thinking=$false; max_tokens=2400; attempts=3; batch_pids=4; context_before=2; context_after=2 }
$ensemble['qwen_semantic_audit'] = @{ temperature=0.0; top_p=1.0; top_k=64; enable_thinking=$false; max_tokens=1900; attempts=3; batch_pids=5; context_before=2; context_after=2 }
$ensemble['gemma_semantic_audit'] = @{ temperature=0.0; top_p=1.0; top_k=64; enable_thinking=$true; max_tokens=1900; attempts=3; batch_pids=5; context_before=2; context_after=2 }
$ensemble['gemma_russian_audit'] = @{ temperature=0.0; top_p=1.0; top_k=64; enable_thinking=$true; max_tokens=1800; attempts=3; batch_pids=6; context_before=3; context_after=3 }
$ensemble['gemma_discourse_audit'] = @{ temperature=0.0; top_p=1.0; top_k=64; enable_thinking=$true; max_tokens=2600; attempts=3; window_pids=30; overlap_pids=10 }
$ensemble['qwen_cross_verifier'] = @{ temperature=0.0; top_p=1.0; top_k=64; enable_thinking=$false; max_tokens=1400; length_retry_max_tokens=1600; attempts=3; context_size=2 }
$ensemble['gemma_cross_verifier'] = @{ temperature=0.0; top_p=1.0; top_k=64; enable_thinking=$true; max_tokens=800; attempts=3; context_size=2 }
$ensemble['repair'] = @{ temperature=0.0; top_p=1.0; top_k=64; enable_thinking=$false; max_tokens=1600; attempts=3; context_before=2; context_after=2; alternative_for_multiple_issues=$true; alternative_categories=@('idiom','meaning','register','dialogue','continuity'); max_changed_ratio_span=0.35 }
$ensemble['qwen_semantic_post_gate'] = @{ temperature=0.0; top_p=1.0; top_k=64; enable_thinking=$false; max_tokens=900; attempts=3; context_size=2 }
$ensemble['gemma_semantic_post_gate'] = @{ temperature=0.0; top_p=1.0; top_k=64; enable_thinking=$true; max_tokens=900; attempts=3; context_size=2 }
$ensemble['gemma_russian_post_gate'] = @{ temperature=0.0; top_p=1.0; top_k=64; enable_thinking=$true; max_tokens=900; attempts=3; context_size=2 }
$ensemble['verification'] = @{ fail_on_uncertain=$false; uncertain_policy='repair' }
$ensemble['max_repair_rounds'] = 3
$ensemble['final_quality'] = @{ fail_deterministic_categories=@('missing','mixed_script','english_residue','number','number_word','entity_consistency','name_consistency','narrator_gender') }
$ensemble['preflight'] = @{ enabled=$true; min_prompt_tps=100.0; min_generation_tps=20.0; max_tokens=512; warmup_runs=1; sample_runs=3; policy='median_advisory' }

$configJson = $cfg | ConvertTo-Json -Depth 60
[System.IO.File]::WriteAllText(
    $ConfigPath,
    $configJson,
    [System.Text.UTF8Encoding]::new($false)
)

$resolverPath = Join-Path $PackageRoot 'v31_chapter_resolver.py'
$chapterManifest = & $Python $resolverPath --project-root $ProjectRoot --input-dir $paths['input_dir'] --start $Start --end $End --manifest $ChapterManifestPath | ConvertFrom-Json
if ($LASTEXITCODE -ne 0) { throw "Canonical chapter resolver failed with exit code $LASTEXITCODE" }
$SelectedInputFiles = @($chapterManifest.chapters | ForEach-Object { Get-Item (Join-Path $ProjectRoot $_.source_path) })
$SelectedChapterStems = @($SelectedInputFiles | ForEach-Object BaseName)

$script:ServerProcess = $null
$script:CurrentServerStderr = $null
$script:CurrentServerProfile = $null
$script:ServerMetadata = $null
$script:MonitorStage = $null
function Write-MonitorState {
    param([string]$Stage,[string]$Status,[string]$FailureReason='')
    if ($Stage) { $script:MonitorStage = $Stage }
    $state = [ordered]@{
        schema = 'pact-v31-monitor-state/v1'
        runner_version = $BuildIdentity
        artifact_version = $ArtifactVersion
        stage = $script:MonitorStage
        status = $Status
        updated_at = (Get-Date).ToString('o')
        active_profile = if ($script:CurrentServerProfile) { $script:CurrentServerProfile } else { $null }
        owned_pid = if ($script:ServerProcess -and -not $script:ServerProcess.HasExited) { $script:ServerProcess.Id } else { $null }
        failure_reason = if ($FailureReason) { $FailureReason } else { $null }
    }
    $temporary = "$MonitorStatePath.tmp"
    [System.IO.File]::WriteAllText($temporary, ($state | ConvertTo-Json -Depth 5), [System.Text.UTF8Encoding]::new($false))
    Move-Item -LiteralPath $temporary -Destination $MonitorStatePath -Force
}
function Stop-LlamaServer {
    $owned = (
        $script:ServerProcess -and $script:ServerMetadata -and
        [int]$script:ServerMetadata.pid -eq [int]$script:ServerProcess.Id
    )
    if ($owned -and -not $script:ServerProcess.HasExited) {
        Stop-Process -Id $script:ServerProcess.Id -Force -ErrorAction SilentlyContinue
        try { $script:ServerProcess.WaitForExit(10000) | Out-Null } catch {}
        Start-Sleep -Seconds 2
    } elseif ($script:ServerProcess -and -not $script:ServerProcess.HasExited) {
        Write-Warning "Refusing to stop unowned llama-server PID $($script:ServerProcess.Id)."
    }
    $script:ServerProcess = $null
    $script:CurrentServerStderr = $null
    $script:CurrentServerProfile = $null
    $script:ServerMetadata = $null
}

function Get-LlamaServerProfile {
    param([ValidateSet('GemmaTranslate','GemmaRepair','GemmaVerify','Qwen')][string]$Profile)
    switch ($Profile) {
        'GemmaTranslate' { $serverArgs = @('-m',$GemmaModelPath,'--model-draft',$GemmaMtpPath,'--spec-type','draft-mtp','--spec-draft-n-max','4','--device','Vulkan0','--host','127.0.0.1','--port','8080','-ngl','99','-ncmoe','18','--no-mmap','--reasoning-budget','0','-np','1','-c','32768','-fa','on','--jinja','--cache-ram','0','--ctx-checkpoints','0') }
        'GemmaRepair' { $serverArgs = @('-m',$GemmaModelPath,'--device','Vulkan0','--host','127.0.0.1','--port','8080','-c','32768','-fit','on','-fitt','1536','-t','6','-tb','12','--no-mmap','--reasoning-budget','0','-np','1','-fa','on','--jinja','--cache-ram','0','--ctx-checkpoints','0') }
        'GemmaVerify' { $serverArgs = @('-m',$GemmaModelPath,'--device','Vulkan0','--host','127.0.0.1','--port','8080','-c','32768','-fit','on','-fitt','1536','-t','6','-tb','12','--no-mmap','--reasoning-budget','128','-np','1','-fa','on','--jinja','--cache-ram','0','--ctx-checkpoints','0') }
        'Qwen' { $serverArgs = @('-m',$QwenModelPath,'--device','Vulkan0','--host','127.0.0.1','--port','8080','-c','32768','-fit','on','-fitt','1280','-b','2048','-ub','512','-ctk','q8_0','-ctv','q8_0','-t','6','-tb','12','--no-mmap','--reasoning-budget','0','-np','1','-fa','on','--jinja','--cache-ram','0','--ctx-checkpoints','0') }
    }
    return $serverArgs
}

function Get-LlamaProcessCommandLine {
    param([int]$ProcessId)
    try { return [string](Get-CimInstance Win32_Process -Filter "ProcessId = $ProcessId" -ErrorAction Stop).CommandLine }
    catch { return '' }
}

function Test-OwnedHealthyLlamaServer {
    param([string]$Profile,[string[]]$ServerArgs)
    if (-not $script:ServerProcess) { return $false }
    $actualCommandLine = Get-LlamaProcessCommandLine $script:ServerProcess.Id
    try { $health = Invoke-RestMethod -Uri 'http://127.0.0.1:8080/health' -TimeoutSec 2 }
    catch { return $false }
    $signature = Get-V31ServerCommandSignature $LlamaExe $Profile $ServerArgs
    return Test-V31OwnedServerIdentity `
        -ProcessId $script:ServerProcess.Id `
        -HasExited $script:ServerProcess.HasExited `
        -Metadata $script:ServerMetadata `
        -ExpectedProfile $Profile `
        -ExpectedExecutable $LlamaExe `
        -ExpectedCommandSignature $signature `
        -ActualCommandLine $actualCommandLine `
        -HealthStatus ([string]$health.status)
}

function Start-LlamaServer {
    param([ValidateSet('GemmaTranslate','GemmaRepair','GemmaVerify','Qwen')][string]$Profile)
    [string[]]$serverArgs = @(Get-LlamaServerProfile $Profile)
    if (Test-OwnedHealthyLlamaServer $Profile $serverArgs) {
        Write-Host "Reusing owned healthy $Profile server (PID $($script:ServerProcess.Id))" -ForegroundColor Green
        return
    }
    Stop-LlamaServer
    $unownedEndpoint = $false
    try {
        $probe = Invoke-RestMethod -Uri 'http://127.0.0.1:8080/health' -TimeoutSec 2
        if ($null -ne $probe) { $unownedEndpoint = $true }
    } catch {}
    if ($unownedEndpoint) {
        throw 'Port 8080 is already served by an unowned endpoint; refusing to attach to or stop it.'
    }
    $env:GGML_VK_DISABLE_COOPMAT = '1'
    $stamp = Get-Date -Format 'yyyyMMdd_HHmmss'
    $stdout = Join-Path $ServerLogsDir "${Profile}_${stamp}_stdout.log"
    $stderr = Join-Path $ServerLogsDir "${Profile}_${stamp}_stderr.log"
    Write-Host "Starting $Profile..." -ForegroundColor Cyan
    $script:CurrentServerStderr = $stderr
    $script:CurrentServerProfile = $Profile
    $script:ServerProcess = Start-Process -FilePath $LlamaExe -WorkingDirectory $LlamaRoot -ArgumentList $serverArgs -RedirectStandardOutput $stdout -RedirectStandardError $stderr -WindowStyle Hidden -PassThru
    $script:ServerMetadata = [pscustomobject]@{
        pid = $script:ServerProcess.Id
        profile = $Profile
        executable = $LlamaExe
        command_signature = Get-V31ServerCommandSignature $LlamaExe $Profile $serverArgs
        command_line = ''
        health_uri = 'http://127.0.0.1:8080/health'
    }
    Write-MonitorState -Stage $script:MonitorStage -Status 'LOADING_MODEL'
    $ready = $false
    for ($i=0; $i -lt 240; $i++) {
        if ($script:ServerProcess.HasExited) { throw "$Profile llama-server exited. See $stderr" }
        try { $health = Invoke-RestMethod -Uri 'http://127.0.0.1:8080/health' -TimeoutSec 2; if ($health.status -in @('ok','no slot available')) { $ready=$true; break } } catch {}
        Start-Sleep -Seconds 1
    }
    if (-not $ready) { throw "$Profile server did not become ready. See $stderr" }
    $script:ServerMetadata.command_line = Get-LlamaProcessCommandLine $script:ServerProcess.Id
    if ([string]::IsNullOrWhiteSpace($script:ServerMetadata.command_line)) {
        Write-Warning "$Profile command metadata unavailable; this server will not be reused across stages."
    }
    Write-Host "$Profile ready (PID $($script:ServerProcess.Id))" -ForegroundColor Green
    Write-MonitorState -Stage $script:MonitorStage -Status 'ACTIVE'
}

function Get-GemmaPreflightLogSnapshot {
    if (-not $script:CurrentServerStderr -or -not (Test-Path $script:CurrentServerStderr)) {
        throw 'Gemma preflight could not locate the active llama-server stderr log.'
    }
    $logText = [string](Get-Content -LiteralPath $script:CurrentServerStderr -Raw)
    $promptMatches = [regex]::Matches(
        $logText,
        'prompt eval time\s*=\s*[\d.]+\s*ms\s*/\s*\d+\s*tokens.*?([\d.]+)\s*tokens per second'
    )
    $generationMatches = [regex]::Matches(
        $logText,
        '(?m)^.*?\|\s+eval time\s*=\s*[\d.]+\s*ms\s*/\s*\d+\s*tokens.*?([\d.]+)\s*tokens per second'
    )
    $acceptanceMatches = [regex]::Matches($logText, 'draft acceptance\s*=\s*([\d.]+)')
    return [pscustomobject]@{
        prompt_count = $promptMatches.Count
        generation_count = $generationMatches.Count
        acceptance_count = $acceptanceMatches.Count
        prompt_tps = if ($promptMatches.Count) { [double]$promptMatches[$promptMatches.Count - 1].Groups[1].Value } else { $null }
        generation_tps = if ($generationMatches.Count) { [double]$generationMatches[$generationMatches.Count - 1].Groups[1].Value } else { $null }
        mtp_acceptance = if ($acceptanceMatches.Count) { [double]$acceptanceMatches[$acceptanceMatches.Count - 1].Groups[1].Value } else { $null }
    }
}

function Invoke-GemmaPreflightProbe {
    param([string]$Body, [string]$Label)

    $before = Get-GemmaPreflightLogSnapshot
    $null = Invoke-RestMethod `
        -Uri 'http://127.0.0.1:8080/v1/chat/completions' `
        -Method Post `
        -ContentType 'application/json; charset=utf-8' `
        -Body $Body `
        -TimeoutSec 600

    $after = $null
    for ($poll = 0; $poll -lt 40; $poll++) {
        Start-Sleep -Milliseconds 250
        $candidate = Get-GemmaPreflightLogSnapshot
        if (
            $candidate.prompt_count -gt $before.prompt_count -and
            $candidate.generation_count -gt $before.generation_count
        ) {
            $after = $candidate
            break
        }
    }
    if ($null -eq $after) {
        throw "Gemma preflight $Label could not parse new timing data. See $($script:CurrentServerStderr)"
    }
    return [pscustomobject][ordered]@{
        label = $Label
        prompt_tps = $after.prompt_tps
        generation_tps = $after.generation_tps
        mtp_acceptance = if ($after.acceptance_count -gt $before.acceptance_count) { $after.mtp_acceptance } else { $null }
    }
}

function Invoke-GemmaPreflight {
    $preflight = $ensemble['preflight']
    if (-not $preflight -or -not [bool]$preflight['enabled']) { return }
    if ($script:CurrentServerProfile -ne 'GemmaTranslate') {
        throw 'Gemma preflight requires the GemmaTranslate profile.'
    }

    $minPrompt = [double]$preflight['min_prompt_tps']
    $minGeneration = [double]$preflight['min_generation_tps']
    $maxTokens = [int]$preflight['max_tokens']
    $warmupRuns = [int]$preflight['warmup_runs']
    $sampleRuns = [int]$preflight['sample_runs']
    $policy = [string]$preflight['policy']
    if ($warmupRuns -lt 1 -or $sampleRuns -lt 3 -or $policy -ne 'median_advisory') {
        throw "Invalid Gemma preflight policy configuration: warmup_runs=$warmupRuns sample_runs=$sampleRuns policy=$policy"
    }
    Write-Host "`n=== Preflight: GemmaTranslate performance ===" -ForegroundColor Magenta
    Write-Host "Advisory thresholds: prompt >= $minPrompt t/s; generation >= $minGeneration t/s" -ForegroundColor DarkGray
    Write-Host "Policy: $warmupRuns warm-up + $sampleRuns measured runs; median; valid low performance is advisory" -ForegroundColor DarkGray

    $source = @'
The rain had stopped sometime before dawn, but the streets were still shining beneath the streetlights. Daniel stood under the narrow awning of the closed bookstore and watched the water run along the curb. He had expected the package to be waiting for him, wrapped in brown paper and hidden behind the loose brick beside the door.

It was not there.

He checked the alley, then crouched and ran his fingers over the wet brickwork. The brick had been moved recently. There was fresh dust beneath it, protected from the rain, and a thin scrape across one corner. Someone had found the hiding place before him.

Across the street, a woman in a dark coat lowered her umbrella. She did not look directly at Daniel, but she had been standing in the same place for several minutes.

Daniel straightened slowly.

“You’re late,” the woman said.

“I wasn’t told there would be anyone else.”

“There wasn’t supposed to be.”

A bus passed between them, spraying water across the empty road. When it was gone, the woman was already walking away.

Daniel hesitated only a moment before following her.
'@

    $body = @{
        model = $GemmaModelName
        messages = @(
            @{
                role = 'system'
                content = 'Translate literary English prose into natural, polished Russian. Preserve meaning, tone, paragraphing, dialogue, and all factual details. Output only the Russian translation.'
            },
            @{
                role = 'user'
                content = $source
            }
        )
        temperature = 0.0
        max_tokens = $maxTokens
        stream = $false
    } | ConvertTo-Json -Depth 10

    $warmups = @()
    for ($index = 1; $index -le $warmupRuns; $index++) {
        $sample = Invoke-GemmaPreflightProbe -Body $body -Label "warmup-$index"
        $warmups += ,$sample
        Write-Host ('Warm-up {0}: prompt = {1:N2} t/s; generation = {2:N2} t/s' -f $index, $sample.prompt_tps, $sample.generation_tps) -ForegroundColor DarkGray
    }
    $samples = @()
    for ($index = 1; $index -le $sampleRuns; $index++) {
        $sample = Invoke-GemmaPreflightProbe -Body $body -Label "sample-$index"
        $samples += ,$sample
        Write-Host ('Sample {0}/{1}: prompt = {2:N2} t/s; generation = {3:N2} t/s' -f $index, $sampleRuns, $sample.prompt_tps, $sample.generation_tps) -ForegroundColor DarkGray
    }
    $summary = Get-V31PreflightSummary `
        -WarmupSamples $warmups `
        -Samples $samples `
        -ExpectedWarmupCount $warmupRuns `
        -ExpectedSampleCount $sampleRuns `
        -MinPromptTps $minPrompt `
        -MinGenerationTps $minGeneration

    $report = [ordered]@{
        version = $BuildIdentity
        artifact_version = $ArtifactVersion
        timestamp = (Get-Date).ToString('o')
        profile = 'GemmaTranslate'
        policy = $summary.policy
        status = $summary.status
        blocking = $summary.blocking
        warmup_samples = $summary.warmup_samples
        samples = $summary.samples
        sample_count = $summary.sample_count
        prompt_tps = $summary.prompt_tps
        generation_tps = $summary.generation_tps
        mtp_acceptance = $summary.mtp_acceptance
        thresholds = $summary.thresholds
        meets_thresholds = $summary.meets_thresholds
        passed = $summary.meets_thresholds
        execution_allowed = $summary.execution_allowed
        server_log = $script:CurrentServerStderr
    }
    $reportPath = Join-Path $RunRoot 'preflight_performance.json'
    $reportJson = $report | ConvertTo-Json -Depth 8
    [System.IO.File]::WriteAllText(
        $reportPath,
        $reportJson,
        [System.Text.UTF8Encoding]::new($false)
    )

    $acceptanceText = if ($null -ne $summary.mtp_acceptance) { '; median MTP acceptance = {0:P1}' -f $summary.mtp_acceptance } else { '' }
    $color = if ($summary.meets_thresholds) { 'Green' } else { 'Yellow' }
    Write-Host ('Preflight median: prompt = {0:N2} t/s; generation = {1:N2} t/s{2}' -f $summary.prompt_tps, $summary.generation_tps, $acceptanceText) -ForegroundColor $color
    if (-not $summary.meets_thresholds) {
        Write-Warning (
            'GemmaTranslate performance is below the advisory threshold. ' +
            "Required >= $minPrompt prompt t/s and >= $minGeneration generation t/s; " +
            "median measured $($summary.prompt_tps) / $($summary.generation_tps). Continuing without invalidating run data or caches."
        )
    }
}

function Invoke-PythonStage {
    param([string]$Label, [string[]]$Arguments, [string]$Outcome='COMPLETE')
    Write-Host "`n=== $Label ===" -ForegroundColor Magenta
    Write-MonitorState -Stage $Label -Status 'ACTIVE'
    Push-Location $ProjectRoot
    try {
        & $Python @Arguments
        if ($LASTEXITCODE -ne 0) { throw "$Label failed with exit code $LASTEXITCODE" }
        Write-MonitorState -Stage $Label -Status $Outcome
    } catch {
        Write-MonitorState -Stage $Label -Status 'FAILED' -FailureReason $_.Exception.Message
        throw
    } finally { Pop-Location }
}

function Invoke-AggregateModelStage {
    param(
        [string]$Label,
        [string[]]$Arguments,
        [ValidateSet('GemmaTranslate','GemmaRepair','GemmaVerify','Qwen')][string]$Profile,
        [string]$AggregateRelativePath,
        [bool]$Force
    )
    $probeArgs = @((Join-Path $PackageRoot 'v31_stage_protocol.py'), '--work-dir', $WorkDir, '--aggregate-relative-path', $AggregateRelativePath)
    foreach ($stem in $SelectedChapterStems) { $probeArgs += @('--chapter-stem', $stem) }
    if ($Force) { $probeArgs += '--force' }
    Push-Location $ProjectRoot
    try { & $Python @probeArgs; $probeExit = $LASTEXITCODE } finally { Pop-Location }
    if ($probeExit -eq 0) {
        Write-Host "`nStage protocol REUSED: $Label" -ForegroundColor DarkGray
        Invoke-PythonStage -Label $Label -Arguments $Arguments -Outcome 'REUSED'
        return
    }
    if ($probeExit -notin @(20, 22)) { throw "$Label stage probe FAILED with exit code $probeExit" }
    $runArguments = @($Arguments)
    if ($probeExit -eq 22 -and $runArguments -notcontains '--force') { $runArguments += '--force' }
    Start-LlamaServer $Profile
    Invoke-PythonStage -Label $Label -Arguments $runArguments
}

function Invoke-TranslationStage {
    param([string]$Label, [string[]]$Arguments)
    $probeArgs = @((Join-Path $PackageRoot 'v31_stage_protocol.py'), '--work-dir', $WorkDir, '--translation')
    foreach ($stem in $SelectedChapterStems) { $probeArgs += @('--chapter-stem', $stem) }
    Push-Location $ProjectRoot
    try { & $Python @probeArgs; $probeExit = $LASTEXITCODE } finally { Pop-Location }
    if ($probeExit -ne 20) { throw "$Label stage probe FAILED with exit code $probeExit" }
    Start-LlamaServer GemmaTranslate
    Invoke-PythonStage -Label $Label -Arguments $Arguments
}

function CommonArgs {
    return @('--project-root',$ProjectRoot,'--config',$ConfigPath,'--start',"$Start",'--end',"$End")
}

function Remove-SelectedOutputs {
    foreach ($inputFile in $SelectedInputFiles) {
        Remove-Item (Join-Path $OutputDir $inputFile.Name) -Force -ErrorAction SilentlyContinue
    }
}

function Remove-QualityArtifacts {
    foreach ($stem in $SelectedChapterStems) {
        $work = Join-Path $WorkDir $stem
        if (-not (Test-Path $work)) { continue }
        Remove-Item (Join-Path $work 'v31') -Recurse -Force -ErrorAction SilentlyContinue
        foreach ($name in @('issues.json','verified_issues.json','repaired_translations.json','repaired_translations.preverify.json','repair_records.json','post_repair_report.json','issue_lifecycle.json','v31_primary_translations.json','v31_final_translations.json','v31_quality_gate.json','quality_report.json','audit_report.html','state.json')) {
            Remove-Item (Join-Path $work $name) -Force -ErrorAction SilentlyContinue
        }
    }
    Remove-SelectedOutputs
}

function Get-RetryCount {
    param([string]$PassName)
    $total = 0
    foreach ($stem in $SelectedChapterStems) {
        $path = Join-Path (Join-Path (Join-Path (Join-Path $WorkDir $stem) 'v31') $PassName) 'status.json'
        if (-not (Test-Path $path)) { continue }
        $status = Get-Content $path -Raw | ConvertFrom-Json
        $total += [int]$status.retry_required
    }
    return $total
}

function Run-AuditPass {
    param([string]$PassName, [string]$TranslationsFile, [string]$PidFile = '')
    $extra = @('--pass-name',$PassName)
    if ($TranslationsFile) { $extra += @('--translations-file',$TranslationsFile) }
    if ($PidFile) { $extra += @('--pids-file',$PidFile) }
    if ($RedoQuality) { $extra += '--force' }

    Invoke-AggregateModelStage -Label "$PassName Qwen semantic audit" -Arguments (@((Join-Path $PackageRoot 'v31_audit.py')) + (CommonArgs) + $extra + @('--mode','qwen_semantic','--model',$QwenModelName)) -Profile Qwen -AggregateRelativePath "v31\$PassName\qwen_semantic.json" -Force ([bool]$RedoQuality)

    Invoke-AggregateModelStage -Label "$PassName Gemma semantic audit" -Arguments (@((Join-Path $PackageRoot 'v31_audit.py')) + (CommonArgs) + $extra + @('--mode','gemma_semantic','--model',$GemmaModelName)) -Profile GemmaVerify -AggregateRelativePath "v31\$PassName\gemma_semantic.json" -Force ([bool]$RedoQuality)
    Invoke-AggregateModelStage -Label "$PassName Gemma Russian audit" -Arguments (@((Join-Path $PackageRoot 'v31_audit.py')) + (CommonArgs) + $extra + @('--mode','gemma_russian','--model',$GemmaModelName)) -Profile GemmaVerify -AggregateRelativePath "v31\$PassName\gemma_russian.json" -Force ([bool]$RedoQuality)
    # Final smoke is intentionally one source-grounded Qwen chapter pass;
    # local semantic/Russian audits remain restricted to ledger PIDs.
    $discourseExtra = @($extra | Where-Object { $_ -ne '--pids-file' -and $_ -ne $PidFile })
    if ($PassName -eq 'final') {
        Invoke-AggregateModelStage -Label 'final Qwen source-grounded global smoke' -Arguments (@((Join-Path $PackageRoot 'v31_audit.py')) + (CommonArgs) + $discourseExtra + @('--mode','qwen_global_smoke','--model',$QwenModelName)) -Profile Qwen -AggregateRelativePath "v31\$PassName\qwen_global_smoke.json" -Force ([bool]$RedoQuality)
    } else {
        Invoke-AggregateModelStage -Label "$PassName Gemma discourse audit" -Arguments (@((Join-Path $PackageRoot 'v31_audit.py')) + (CommonArgs) + $discourseExtra + @('--mode','gemma_discourse','--model',$GemmaModelName)) -Profile GemmaVerify -AggregateRelativePath "v31\$PassName\gemma_discourse.json" -Force ([bool]$RedoQuality)
    }

    Invoke-PythonStage -Label "$PassName merge and deduplicate" -Arguments (@((Join-Path $PackageRoot 'v31_merge_issues.py')) + (CommonArgs) + $extra)

    Invoke-AggregateModelStage -Label "$PassName Gemma cross-verifies Qwen issues" -Arguments (@((Join-Path $PackageRoot 'v31_cross_verify.py')) + (CommonArgs) + $extra + @('--judge','gemma','--model',$GemmaModelName)) -Profile GemmaVerify -AggregateRelativePath "v31\$PassName\cross_verify_gemma.json" -Force ([bool]$RedoQuality)

    Invoke-AggregateModelStage -Label "$PassName Qwen cross-verifies Gemma issues" -Arguments (@((Join-Path $PackageRoot 'v31_cross_verify.py')) + (CommonArgs) + $extra + @('--judge','qwen','--model',$QwenModelName)) -Profile Qwen -AggregateRelativePath "v31\$PassName\cross_verify_qwen.json" -Force ([bool]$RedoQuality)

    Invoke-PythonStage -Label "$PassName finalize verification" -Arguments (@((Join-Path $PackageRoot 'v31_finalize_verification.py')) + (CommonArgs) + $extra)
}

function Run-RepairPass {
    param([string]$PassName, [string]$InitialTranslationsFile, [int]$MaxRounds = 0)
    $maxRounds = if ($MaxRounds -gt 0) { $MaxRounds } else { [int]$ensemble['max_repair_rounds'] }
    $currentFile = $InitialTranslationsFile
    for ($round=1; $round -le $maxRounds; $round++) {
        $baseArgs = @('--pass-name',$PassName,'--round',"$round")
        if ($currentFile) { $baseArgs += @('--translations-file',$currentFile) }
        if ($RedoQuality) { $baseArgs += '--force' }
        if ($round -gt 1) { $baseArgs += '--retry-only' }

        Invoke-AggregateModelStage -Label "$PassName repair round $round" -Arguments (@((Join-Path $PackageRoot 'v31_repair.py')) + (CommonArgs) + $baseArgs + @('--model',$GemmaModelName)) -Profile GemmaRepair -AggregateRelativePath ("v31\{0}\repair_candidates_round_{1:D2}.json" -f $PassName,$round) -Force ([bool]$RedoQuality)

        $gateArgs = @('--pass-name',$PassName,'--round',"$round")
        if ($currentFile) { $gateArgs += @('--translations-file',$currentFile) }
        if ($RedoQuality) { $gateArgs += '--force' }

        Invoke-AggregateModelStage -Label "$PassName Qwen semantic gate round $round" -Arguments (@((Join-Path $PackageRoot 'v31_postcheck.py')) + (CommonArgs) + $gateArgs + @('--judge','qwen_semantic','--model',$QwenModelName)) -Profile Qwen -AggregateRelativePath ("v31\{0}\post_gate_qwen_semantic_round_{1:D2}.json" -f $PassName,$round) -Force ([bool]$RedoQuality)

        Invoke-AggregateModelStage -Label "$PassName Gemma semantic gate round $round" -Arguments (@((Join-Path $PackageRoot 'v31_postcheck.py')) + (CommonArgs) + $gateArgs + @('--judge','gemma_semantic','--model',$GemmaModelName)) -Profile GemmaVerify -AggregateRelativePath ("v31\{0}\post_gate_gemma_semantic_round_{1:D2}.json" -f $PassName,$round) -Force ([bool]$RedoQuality)
        Invoke-AggregateModelStage -Label "$PassName Gemma Russian gate round $round" -Arguments (@((Join-Path $PackageRoot 'v31_postcheck.py')) + (CommonArgs) + $gateArgs + @('--judge','gemma_russian','--model',$GemmaModelName)) -Profile GemmaVerify -AggregateRelativePath ("v31\{0}\post_gate_gemma_russian_round_{1:D2}.json" -f $PassName,$round) -Force ([bool]$RedoQuality)

        Invoke-PythonStage -Label "$PassName deterministic gate round $round" -Arguments (@((Join-Path $PackageRoot 'v31_deterministic_gate.py')) + (CommonArgs) + $gateArgs)
        Invoke-PythonStage -Label "$PassName adjudication round $round" -Arguments (@((Join-Path $PackageRoot 'v31_adjudicate.py')) + (CommonArgs) + $gateArgs)

        $retry = Get-RetryCount $PassName
        if ($retry -eq 0) { return }
        Write-Host "$PassName has $retry unresolved PID(s) after round $round." -ForegroundColor Yellow
        $currentFile = if ($PassName -eq 'primary') { 'v31_primary_translations.json' } else { 'v31_final_translations.json' }
    }
    $remaining = Get-RetryCount $PassName
    if ($remaining -gt 0 -and $PassName -ne 'final') { throw "$PassName left $remaining unresolved PID(s) after $maxRounds repair rounds." }
}

try {
    Write-Host "Pact ensemble pipeline build $BuildIdentity (artifact v$ArtifactVersion)" -ForegroundColor White
    Write-Host "Run root: $RunRoot" -ForegroundColor White

    $prepareArgs = @((Join-Path $PackageRoot 'prepare_pipeline_context.py')) + (CommonArgs)
    Invoke-PythonStage -Label '1/11 Prepare manifest, chapter bible, frozen glossary' -Arguments $prepareArgs

    $dagArgs = @((Join-Path $PackageRoot 'v31_artifact_dag.py'), '--work-dir',$WorkDir,'--output-dir',$OutputDir,'--run-root',$RunRoot)
    if ($RedoSourceAnalysis) { $dagArgs += '--redo-source-analysis' }
    if ($RedoTranslation) { $dagArgs += '--redo-translation' }
    if ($RedoQuality) { $dagArgs += '--redo-quality' }
    if ($RedoFormatting) { $dagArgs += '--redo-formatting' }
    if ($DryRun) {
        Invoke-PythonStage -Label 'Artifact dependency plan (dry run)' -Arguments $dagArgs
        return
    }
    if ($RedoSourceAnalysis -or $RedoTranslation -or $RedoQuality -or $RedoFormatting) {
        Invoke-PythonStage -Label 'Apply artifact dependency plan' -Arguments ($dagArgs + '--apply')
    }

    $sourceArgs = @((Join-Path $PackageRoot 'v31_source_analysis.py')) + (CommonArgs) + @('--model',$QwenModelName)
    if ($RedoSourceAnalysis) { $sourceArgs += '--force' }
    Invoke-AggregateModelStage -Label '2/11 Qwen source scene analysis' -Arguments $sourceArgs -Profile Qwen -AggregateRelativePath 'source_scene_map.json' -Force ([bool]$RedoSourceAnalysis)

    $translateArgs = @('.\pact_translate_v3.py','--config',$ConfigPath,'--phase','translate','--start',"$Start",'--end',"$End")
    Invoke-TranslationStage -Label '3/11 Gemma translation with source invariants' -Arguments $translateArgs
    if (-not $SkipPreflight) { Invoke-GemmaPreflight }

    Run-AuditPass 'primary' 'draft_translations.json'
    Run-RepairPass 'primary' 'draft_translations.json'
    Invoke-PythonStage -Label '6b/11 Record primary changed-PID lineage' -Arguments (@((Join-Path $PackageRoot 'v31_final_lifecycle.py')) + (CommonArgs) + @('--before','draft_translations.json','--after','v31_primary_translations.json','--stage','primary_repair','--reason','accepted primary repair'))

    Run-AuditPass 'residual' 'v31_primary_translations.json'
    Run-RepairPass 'residual' 'v31_primary_translations.json'

    # The ledger is append-only and starts before the final targeted pass.
    Invoke-PythonStage -Label '9a/11 Append residual changed-PID lineage' -Arguments (@((Join-Path $PackageRoot 'v31_final_lifecycle.py')) + (CommonArgs) + @('--before','v31_primary_translations.json','--after','v31_final_translations.json','--stage','residual_repair','--reason','accepted residual repair'))
    $finalLedger = Join-Path $WorkDir '*\v31_final_changed_pid_ledger.json'
    # Per chapter paths are resolved by the audit script; the runner expands one
    # ledger only because selected chapter runs are currently one chapter.
    $finalLedger = (Get-ChildItem $WorkDir -Filter 'v31_final_changed_pid_ledger.json' -Recurse | Select-Object -First 1).FullName
    Run-AuditPass 'final' 'v31_final_translations.json' $finalLedger
    foreach ($chapter in Get-ChildItem $WorkDir -Directory) {
        Copy-Item (Join-Path $chapter.FullName 'v31_final_translations.json') (Join-Path $chapter.FullName 'v31_pre_final_repair_translations.json') -Force
    }
    Run-RepairPass 'final' 'v31_final_translations.json' 1
    Invoke-PythonStage -Label '9b/11 Append final repair lineage' -Arguments (@((Join-Path $PackageRoot 'v31_final_lifecycle.py')) + (CommonArgs) + @('--before','v31_pre_final_repair_translations.json','--after','v31_final_translations.json','--stage','final_repair','--reason','accepted final repair'))
    Run-AuditPass 'final' 'v31_final_translations.json' $finalLedger

    Invoke-PythonStage -Label '10/11 Final coverage and deterministic quality gate' -Arguments (@((Join-Path $PackageRoot 'v31_finalize_quality.py')) + (CommonArgs) + '--final-lifecycle')
    $quarantined = @(Get-ChildItem $WorkDir -Filter 'v31_quality_gate.json' -Recurse | Where-Object {
        (Get-Content $_.FullName -Raw | ConvertFrom-Json).status -eq 'quarantined'
    })
    if ($quarantined.Count -gt 0) {
        Write-Host "FINAL QUALITY QUARANTINED: $($quarantined.Count) chapter(s); finalization was not run." -ForegroundColor Yellow
        return
    }
    Invoke-PythonStage -Label '10b/11 Build v3.1 review report' -Arguments (@((Join-Path $PackageRoot 'v31_build_review.py')) + (CommonArgs))

    $finalizeArgs = @('.\pact_translate_v3.py','--config',$ConfigPath,'--phase','finalize','--start',"$Start",'--end',"$End")
    if ($RedoFormatting -or $RedoTranslation -or $RedoQuality) { $finalizeArgs += '--redo-formatting' }
    Invoke-PythonStage -Label '11/11 Restore formatting and finalize HTML' -Arguments $finalizeArgs

    Stop-LlamaServer
    $bundle = Join-Path $RunRoot "result_${RunName}.zip"
    $items = @($ConfigPath,$OutputDir,$WorkDir,$LogsDir,$ServerLogsDir) | Where-Object { Test-Path $_ }
    Compress-Archive -Path $items -DestinationPath $bundle -Force
    Write-Host "`nPIPELINE V3.1 COMPLETE" -ForegroundColor Green
    Write-Host "Output: $OutputDir" -ForegroundColor Green
    Write-Host "Bundle: $bundle" -ForegroundColor Green
}
catch {
    Write-MonitorState -Stage $script:MonitorStage -Status 'FAILED' -FailureReason $_.Exception.Message
    Write-Host "`nPIPELINE V3.1 FAILED: $($_.Exception.Message)" -ForegroundColor Red
    Write-Host "Run data preserved at: $RunRoot" -ForegroundColor Yellow
    throw
}
finally { Stop-LlamaServer }
