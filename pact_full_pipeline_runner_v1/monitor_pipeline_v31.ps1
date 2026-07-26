[CmdletBinding()]
param(
    [string]$ProjectRoot = 'D:\pact\pact_translator_v3',
    [int]$Start = 60,
    [int]$End = 60,
    [int]$RefreshSeconds = 3,
    [switch]$Once
)

$ErrorActionPreference = 'SilentlyContinue'
$RunRoot = Join-Path $ProjectRoot "pipeline_runs\chapter_${Start}_to_${End}_v31"
$WorkDir = Join-Path $RunRoot 'work'
$OutputDir = Join-Path $RunRoot 'output'
$ServerLogs = Join-Path $RunRoot 'server_logs'

function Read-JsonSafe([string]$Path) {
    if (-not (Test-Path $Path)) { return $null }
    try { return Get-Content $Path -Raw | ConvertFrom-Json } catch { return $null }
}

function Bar([int]$Done, [int]$Total, [int]$Width = 24) {
    if ($Total -le 0) { return ('·' * $Width) }
    $n = [math]::Min($Width, [math]::Floor($Width * $Done / $Total))
    return ('█' * $n) + ('░' * ($Width - $n))
}

function Line([string]$Label, [int]$Done, [int]$Total, [string]$Extra='') {
    $pct = if ($Total -gt 0) { [math]::Round(100 * $Done / $Total, 1) } else { 0 }
    '{0,-31} [{1}] {2,6}%  {3}/{4} {5}' -f $Label, (Bar $Done $Total), $pct, $Done, $Total, $Extra
}

function Latest-Profile {
    if (-not (Test-Path $ServerLogs)) { return 'not started' }
    $file = Get-ChildItem $ServerLogs -File | Sort-Object LastWriteTime -Descending | Select-Object -First 1
    if (-not $file) { return 'not started' }
    if ($file.Name -match '^(GemmaTranslate|GemmaRepair|GemmaVerify|Qwen)_') { return $Matches[1] }
    return $file.Name
}


function Get-SpeedInfo {
    param(
        [string]$ServerLogsDir,
        [string]$Profile
    )

    # Reused from the previous monitor: read llama-server timing lines
    # from the active profile's stderr log.
    $patterns = switch ($Profile) {
        'Qwen' { @('Qwen_*_stderr.log') }
        'GemmaVerify' { @('GemmaVerify_*_stderr.log', 'Gemma_*_stderr.log') }
        'GemmaRepair' { @('GemmaRepair_*_stderr.log', 'Gemma_*_stderr.log') }
        'GemmaTranslate' { @('GemmaTranslate_*_stderr.log', 'Gemma_*_stderr.log') }
        default { @('*_stderr.log') }
    }

    $logs = @()
    foreach ($pattern in $patterns) {
        if (Test-Path -LiteralPath $ServerLogsDir) {
            $logs += Get-ChildItem -LiteralPath $ServerLogsDir `
                -Filter $pattern -File -ErrorAction SilentlyContinue
        }
    }

    $log = $logs |
        Sort-Object LastWriteTime -Descending |
        Select-Object -First 1

    if ($null -eq $log) {
        return [pscustomobject]@{
            Log = $null
            PromptSpeed = $null
            GenerationSpeed = $null
            LiveGenerationSpeed = $null
            LastDecoded = $null
            LastTaskTime = $null
        }
    }

    $tail = @(Get-Content -LiteralPath $log.FullName -Tail 700 `
        -ErrorAction SilentlyContinue)

    $promptLine = $tail |
        Where-Object { $_ -match 'prompt eval time\s*=' } |
        Select-Object -Last 1

    $generationLine = $tail |
        Where-Object {
            $_ -match '\beval time\s*=' -and
            $_ -notmatch 'prompt eval time'
        } |
        Select-Object -Last 1

    $liveLine = $tail |
        Where-Object { $_ -match '\bn_decoded\s*=' -and $_ -match '\btg\s*=' } |
        Select-Object -Last 1

    $promptSpeed = $null
    $generationSpeed = $null
    $liveSpeed = $null
    $decoded = $null

    if ($promptLine -and
        $promptLine -match '([\d.]+)\s+tokens per second') {
        $promptSpeed = [double]$matches[1]
    }

    if ($generationLine -and
        $generationLine -match '([\d.]+)\s+tokens per second') {
        $generationSpeed = [double]$matches[1]
    }

    if ($liveLine) {
        if ($liveLine -match '\btg\s*=\s*([\d.]+)\s+t/s') {
            $liveSpeed = [double]$matches[1]
        }
        if ($liveLine -match '\bn_decoded\s*=\s*(\d+)') {
            $decoded = [int]$matches[1]
        }
    }

    return [pscustomobject]@{
        Log = $log
        PromptSpeed = $promptSpeed
        GenerationSpeed = $generationSpeed
        LiveGenerationSpeed = $liveSpeed
        LastDecoded = $decoded
        LastTaskTime = $log.LastWriteTime
    }
}

function Audit-Stats([string]$Work, [string]$Pass, [string]$Detector) {
    $path = Join-Path $Work "v31\$Pass\$Detector.json"
    $data = Read-JsonSafe $path
    if (-not $data) { return @{done=0; total=0; extra=''} }
    $cov = $data.coverage
    return @{done=[int]$cov.completed; total=[int]$cov.expected; extra=("issues=" + [int]$data.issue_count)}
}

function Show-Pass([string]$Work, [string]$Pass, [int]$TotalPids) {
    Write-Host ""
    Write-Host ("{0} QUALITY PASS" -f $Pass.ToUpper()) -ForegroundColor Cyan
    foreach ($detector in @('qwen_semantic','gemma_semantic','gemma_russian','gemma_discourse')) {
        $s = Audit-Stats $Work $Pass $detector
        $total = if ($s.total -gt 0) { $s.total } else { $TotalPids }
        Write-Host (Line ("  audit " + $detector) $s.done $total $s.extra)
    }
    $merge = Read-JsonSafe (Join-Path $Work "v31\$Pass\merged_issues.json")
    if ($merge) {
        Write-Host ("  merged issues                 {0} (raw {1})" -f [int]$merge.merged_issue_count, [int]$merge.raw_issue_count)
    } else { Write-Host '  merged issues                 pending' }

    foreach ($judge in @('gemma','qwen')) {
        $report = Read-JsonSafe (Join-Path $Work "v31\$Pass\cross_verify_$judge.json")
        if ($report) { Write-Host (Line ("  cross verify " + $judge) ([int]$report.completed) ([int]$report.expected)) }
        else { Write-Host ("  cross verify {0,-14} pending" -f $judge) }
    }
    $verification = Read-JsonSafe (Join-Path $Work "v31\$Pass\verification_report.json")
    if ($verification) {
        Write-Host ("  verified                      repair={0} keep={1} uncertain={2}" -f [int]$verification.repair, [int]$verification.keep, [int]$verification.uncertain)
    } else { Write-Host '  verified                      pending' }

    $candidateFiles = @(Get-ChildItem (Join-Path $Work "v31\$Pass") -Filter 'repair_candidates_round_*.json' -File)
    if ($candidateFiles.Count) {
        $latest = $candidateFiles | Sort-Object Name | Select-Object -Last 1
        $candidate = Read-JsonSafe $latest.FullName
        $round = [int]$candidate.round
        $expectedCandidates = 0
        foreach ($record in @($candidate.records)) { $expectedCandidates += @($record.candidates).Count }
        foreach ($gate in @('qwen_semantic','gemma_semantic','gemma_russian')) {
            $gateReport = Read-JsonSafe (Join-Path $Work ("v31\$Pass\post_gate_{0}_round_{1:D2}.json" -f $gate,$round))
            if ($gateReport) { Write-Host (Line ("  post gate " + $gate) ([int]$gateReport.completed) ([int]$gateReport.expected)) }
            elseif ($expectedCandidates -gt 0) { Write-Host ("  post gate {0,-18} pending" -f $gate) }
        }
    }

    $status = Read-JsonSafe (Join-Path $Work "v31\$Pass\status.json")
    if ($status) {
        Write-Host ("  repair lifecycle              resolved={0}/{1} retry={2} round={3}" -f [int]$status.resolved, [int]$status.total, [int]$status.retry_required, [int]$status.last_round)
    } else {
        if ($candidateFiles.Count) {
            $latest = $candidateFiles | Sort-Object Name | Select-Object -Last 1
            $candidate = Read-JsonSafe $latest.FullName
            Write-Host ("  repair candidates             pids={0} round={1}" -f [int]$candidate.pid_count, [int]$candidate.round)
        } else { Write-Host '  repair lifecycle              pending' }
    }
}

while ($true) {
    Clear-Host
    Write-Host 'PACT ENSEMBLE TRANSLATOR v3.1' -ForegroundColor White
    Write-Host (Get-Date -Format 'yyyy-MM-dd HH:mm:ss')
    Write-Host "Run: $RunRoot"
    $activeProfile = Latest-Profile
    $speed = Get-SpeedInfo $ServerLogs $activeProfile
    Write-Host ("Active profile: " + $activeProfile) -ForegroundColor DarkYellow

    $promptText = if ($null -ne $speed.PromptSpeed) {
        "{0:N2} t/s" -f $speed.PromptSpeed
    } else {
        'not available yet'
    }

    $generationText = if ($null -ne $speed.GenerationSpeed) {
        "{0:N2} t/s" -f $speed.GenerationSpeed
    } else {
        'not available yet'
    }

    $liveText = if ($null -ne $speed.LiveGenerationSpeed) {
        "{0:N2} t/s ({1} decoded)" -f `
            $speed.LiveGenerationSpeed,
            $speed.LastDecoded
    } else {
        'no active sample'
    }

    Write-Host "Prompt speed:    $promptText"
    Write-Host "Generation speed: $generationText"
    Write-Host "Live generation:  $liveText" -ForegroundColor Cyan
    if ($speed.Log) {
        Write-Host ("Server log:       " + $speed.Log.Name) -ForegroundColor DarkGray
    }

    if (-not (Test-Path $RunRoot)) {
        Write-Host "`nRun has not started." -ForegroundColor Yellow
    } else {
        $stems = @()
        if (Test-Path $WorkDir) { $stems = @(Get-ChildItem $WorkDir -Directory | Sort-Object Name) }
        foreach ($chapter in $stems) {
            $work = $chapter.FullName
            $manifest = Read-JsonSafe (Join-Path $work 'manifest.json')
            $total = if ($manifest) { @($manifest.blocks).Count } else { 0 }
            $scene = Read-JsonSafe (Join-Path $work 'source_scene_map.json')
            $draft = Read-JsonSafe (Join-Path $work 'draft_translations.json')
            $draftDone = if ($draft) { @($draft.PSObject.Properties).Count } else { 0 }

            Write-Host ""
            Write-Host $chapter.Name -ForegroundColor White
            Write-Host (Line '1. Manifest/context' $(if ($manifest){$total}else{0}) $total)
            $sceneDone = if ($scene) { [int]$scene.coverage.completed } else { 0 }
            Write-Host (Line '2. Qwen source analysis' $sceneDone $total)
            Write-Host (Line '3. Gemma translation' $draftDone $total)
            Show-Pass $work 'primary' $total
            Show-Pass $work 'residual' $total

            $quality = Read-JsonSafe (Join-Path $work 'v31_quality_gate.json')
            if ($quality) {
                $statusText = if ($quality.ok) { 'PASS' } else { 'FAIL' }
                $color = if ($quality.ok) { 'Green' } else { 'Red' }
                Write-Host ""
                Write-Host ("FINAL QUALITY GATE: " + $statusText) -ForegroundColor $color
                if ($quality.changed_pids) { Write-Host ("Changed PIDs: " + @($quality.changed_pids).Count) }
            } else { Write-Host "`nFINAL QUALITY GATE: pending" }
        }
        $outputs = if (Test-Path $OutputDir) { @(Get-ChildItem $OutputDir -File) } else { @() }
        Write-Host ""
        if ($outputs.Count -gt 0) {
            Write-Host 'PIPELINE V3.1 COMPLETE' -ForegroundColor Green
            $outputs | ForEach-Object { Write-Host ("  " + $_.Name + "  " + $_.Length + " bytes") }
        } else { Write-Host 'Pipeline running or waiting.' -ForegroundColor Yellow }
    }

    if ($Once) { break }
    Start-Sleep -Seconds ([math]::Max(1,$RefreshSeconds))
}
