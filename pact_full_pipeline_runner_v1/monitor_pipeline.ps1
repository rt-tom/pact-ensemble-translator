[CmdletBinding()]
param(
    [string]$ProjectRoot = 'D:\pact\pact_translator_v3',
    [string]$RunRoot = '',
    [int]$Start = 0,
    [int]$End = 0,
    [int]$RefreshSeconds = 5,
    [switch]$Once,
    [switch]$NoClear
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
$MonitorVersion = '1.2.0'

[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)

function Read-JsonSafe {
    param(
        [Parameter(Mandatory)]
        [string]$Path,
        $Default = $null
    )

    if (-not (Test-Path -LiteralPath $Path)) {
        return $Default
    }

    try {
        return Get-Content -LiteralPath $Path -Raw -Encoding UTF8 |
            ConvertFrom-Json
    }
    catch {
        return $Default
    }
}

function Get-ArrayCount {
    param($Value)

    if ($null -eq $Value) {
        return 0
    }

    return @($Value).Count
}

function Get-Percent {
    param(
        [double]$Done,
        [double]$Total
    )

    if ($Total -le 0) {
        return 0.0
    }

    return [math]::Round(
        [math]::Min(100.0, [math]::Max(0.0, 100.0 * $Done / $Total)),
        1
    )
}

function Get-ProgressBar {
    param(
        [double]$Done,
        [double]$Total,
        [int]$Width = 28
    )

    $percent = Get-Percent $Done $Total
    $filled = [int][math]::Floor($Width * $percent / 100.0)
    if ($filled -gt $Width) {
        $filled = $Width
    }

    return (
        '[' +
        ('#' * $filled) +
        ('-' * ($Width - $filled)) +
        ('] {0,5:N1}%' -f $percent)
    )
}

function Get-PropertyValue {
    param(
        $Object,
        [string]$Name,
        $Default = $null
    )

    if ($null -eq $Object) {
        return $Default
    }

    $property = $Object.PSObject.Properties[$Name]
    if ($null -eq $property) {
        return $Default
    }

    return $property.Value
}

function Get-LatestFile {
    param(
        [string]$Directory,
        [string]$Filter
    )

    if (-not (Test-Path -LiteralPath $Directory)) {
        return $null
    }

    return Get-ChildItem -LiteralPath $Directory -Filter $Filter -File `
        -ErrorAction SilentlyContinue |
        Sort-Object LastWriteTime -Descending |
        Select-Object -First 1
}

function Get-ServerInfo {
    $process = Get-CimInstance Win32_Process `
        -Filter "Name='llama-server.exe'" `
        -ErrorAction SilentlyContinue |
        Select-Object -First 1

    if ($null -eq $process) {
        return [pscustomobject]@{
            Process = $null
            Profile = 'none'
            Label = 'llama-server не запущен'
            Pid = 0
            CommandLine = ''
        }
    }

    $commandLine = [string]$process.CommandLine
    $profile = 'unknown'
    $label = 'Неизвестный профиль llama-server'

    if ($commandLine -match 'Qwen3\.6') {
        $profile = 'Qwen'
        $label = 'Qwen audit'
    }
    elseif ($commandLine -match 'gemma-4' -and $commandLine -match 'model-draft') {
        $profile = 'GemmaTranslate'
        $label = 'Gemma Translate / Repair / Finalize'
    }
    elseif (
        $commandLine -match 'gemma-4' -and
        $commandLine -match '--reasoning-budget(?:=|\s+)0(?:\s|$)'
    ) {
        $profile = 'GemmaRepair'
        $label = 'Gemma stable repair profile'
    }
    elseif ($commandLine -match 'gemma-4') {
        $profile = 'GemmaVerify'
        $label = 'Gemma verifier, thinking 128'
    }

    return [pscustomobject]@{
        Process = $process
        Profile = $profile
        Label = $label
        Pid = [int]$process.ProcessId
        CommandLine = $commandLine
    }
}


function Get-WorkerInfo {
    $candidates = @(
        Get-CimInstance Win32_Process -ErrorAction SilentlyContinue |
        Where-Object {
            $_.Name -match '^(?:py|python|python3)(?:\.exe)?$' -and
            $_.CommandLine
        }
    )

    foreach ($process in $candidates) {
        $commandLine = [string]$process.CommandLine
        $phase = 'unknown'
        $label = ''

        if ($commandLine -match 'verify_repair_results\.py') {
            $phase = 'PostRepairVerify'
            $label = '5b/6 Post-repair safety verification'
        }
        elseif ($commandLine -match 'verify_pipeline_issues\.py') {
            $phase = 'Verify'
            $label = '4/6 Gemma verifier'
        }
        elseif ($commandLine -match 'prepare_pipeline_context\.py') {
            $phase = 'Context'
            $label = '1/6 Prepare chapter context'
        }
        elseif (
            $commandLine -match 'pact_translate_v3\.py' -and
            $commandLine -match '--phase(?:=|\s+)audit(?:\s|$)'
        ) {
            $phase = 'Audit'
            $label = '3/6 Qwen bilingual audit'
        }
        elseif (
            $commandLine -match 'pact_translate_v3\.py' -and
            $commandLine -match '--phase(?:=|\s+)repair(?:\s|$)'
        ) {
            $phase = 'Repair'
            $label = '5a/6 Gemma repair'
        }
        elseif (
            $commandLine -match 'pact_translate_v3\.py' -and
            $commandLine -match '--phase(?:=|\s+)finalize(?:\s|$)'
        ) {
            $phase = 'Finalize'
            $label = '6/6 Formatting and finalization'
        }
        elseif (
            $commandLine -match 'pact_translate_v3\.py' -and
            $commandLine -match '--phase(?:=|\s+)translate(?:\s|$)'
        ) {
            $phase = 'Translate'
            $label = '2/6 Gemma translation'
        }
        else {
            continue
        }

        return [pscustomobject]@{
            Process = $process
            Phase = $phase
            Label = $label
            Pid = [int]$process.ProcessId
            StartedAt = [datetime]$process.CreationDate
            CommandLine = $commandLine
        }
    }

    return [pscustomobject]@{
        Process = $null
        Phase = 'none'
        Label = 'worker не запущен'
        Pid = 0
        StartedAt = $null
        CommandLine = ''
    }
}

function Get-GpuMemory {
    param([int]$ProcessId)

    if ($ProcessId -le 0) {
        return [pscustomobject]@{
            DedicatedGiB = $null
            SharedGiB = $null
        }
    }

    try {
        $samples = Get-Counter `
            '\GPU Process Memory(*)\Dedicated Usage',
            '\GPU Process Memory(*)\Shared Usage' `
            -ErrorAction Stop |
            Select-Object -ExpandProperty CounterSamples |
            Where-Object {
                $_.InstanceName -match "pid_$ProcessId(?:_|$)"
            }

        $dedicated = $samples |
            Where-Object { $_.Path -match 'Dedicated Usage$' } |
            Measure-Object -Property CookedValue -Sum

        $shared = $samples |
            Where-Object { $_.Path -match 'Shared Usage$' } |
            Measure-Object -Property CookedValue -Sum

        return [pscustomobject]@{
            DedicatedGiB = if ($dedicated.Count -gt 0) {
                [math]::Round($dedicated.Sum / 1GB, 2)
            } else {
                $null
            }
            SharedGiB = if ($shared.Count -gt 0) {
                [math]::Round($shared.Sum / 1GB, 2)
            } else {
                $null
            }
        }
    }
    catch {
        return [pscustomobject]@{
            DedicatedGiB = $null
            SharedGiB = $null
        }
    }
}

function Get-SystemMemory {
    try {
        $samples = Get-Counter `
            '\Memory\Available MBytes',
            '\Paging File(_Total)\% Usage' `
            -ErrorAction Stop |
            Select-Object -ExpandProperty CounterSamples

        $available = $samples |
            Where-Object { $_.Path -match 'available mbytes$' } |
            Select-Object -First 1

        $pagefile = $samples |
            Where-Object { $_.Path -match '% usage$' } |
            Select-Object -First 1

        return [pscustomobject]@{
            AvailableGiB = if ($available) {
                [math]::Round($available.CookedValue / 1024, 2)
            } else {
                $null
            }
            PagefilePercent = if ($pagefile) {
                [math]::Round($pagefile.CookedValue, 1)
            } else {
                $null
            }
        }
    }
    catch {
        return [pscustomobject]@{
            AvailableGiB = $null
            PagefilePercent = $null
        }
    }
}

function Get-SpeedInfo {
    param(
        [string]$ServerLogsDir,
        [string]$Profile
    )

    $patterns = switch ($Profile) {
        'Qwen' { @('Qwen_*_stderr.log') }
        'GemmaVerify' { @('GemmaVerify_*_stderr.log', 'Gemma_*_stderr.log') }
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

function Get-ChapterDirectories {
    param([string]$WorkDir)

    if (-not (Test-Path -LiteralPath $WorkDir)) {
        return @()
    }

    return @(
        Get-ChildItem -LiteralPath $WorkDir -Directory `
            -ErrorAction SilentlyContinue |
        Sort-Object Name
    )
}

function Get-RunRange {
    param(
        [string]$ResolvedRunRoot,
        [int]$RequestedStart,
        [int]$RequestedEnd
    )

    if ($RequestedStart -gt 0) {
        $resolvedEnd = if ($RequestedEnd -gt 0) {
            $RequestedEnd
        } else {
            $RequestedStart
        }

        return [pscustomobject]@{
            Start = $RequestedStart
            End = $resolvedEnd
            Count = $resolvedEnd - $RequestedStart + 1
        }
    }

    $name = Split-Path -Leaf $ResolvedRunRoot
    if ($name -match '^chapter_(\d+)_to_(\d+)$') {
        $rangeStart = [int]$matches[1]
        $rangeEnd = [int]$matches[2]
        return [pscustomobject]@{
            Start = $rangeStart
            End = $rangeEnd
            Count = $rangeEnd - $rangeStart + 1
        }
    }

    return [pscustomobject]@{
        Start = 0
        End = 0
        Count = 0
    }
}

function Get-TranslationStats {
    param($ChapterDirectories)

    $totalPids = 0
    $translatedPids = 0
    $completeChapters = 0
    $latest = $null

    foreach ($chapter in $ChapterDirectories) {
        $manifest = Read-JsonSafe `
            (Join-Path $chapter.FullName 'manifest.json') @{}

        $blocks = @(Get-PropertyValue $manifest 'blocks' @())
        $totalPids += $blocks.Count

        $pidSet = @{}
        $draftDir = Join-Path $chapter.FullName 'drafts'

        if (Test-Path -LiteralPath $draftDir) {
            foreach ($file in Get-ChildItem -LiteralPath $draftDir `
                -Filter '*.json' -File -ErrorAction SilentlyContinue) {
                $data = Read-JsonSafe $file.FullName @{}
                $translations = Get-PropertyValue $data 'translations' $null
                if ($translations) {
                    foreach ($property in $translations.PSObject.Properties) {
                        $pidSet[$property.Name] = $true
                    }
                }
                if ($null -eq $latest -or $file.LastWriteTime -gt $latest.LastWriteTime) {
                    $latest = $file
                }
            }
        }

        $draftTranslations = Join-Path $chapter.FullName 'draft_translations.json'
        if (Test-Path -LiteralPath $draftTranslations) {
            $finalDraft = Read-JsonSafe $draftTranslations @{}
            foreach ($property in $finalDraft.PSObject.Properties) {
                $pidSet[$property.Name] = $true
            }
            $completeChapters++
        }

        $translatedPids += $pidSet.Count
    }

    return [pscustomobject]@{
        TotalPids = $totalPids
        TranslatedPids = $translatedPids
        CompleteChapters = $completeChapters
        Latest = $latest
    }
}

function Get-AuditStats {
    param(
        $ChapterDirectories,
        $Config
    )

    $batchSize = 8
    $auditConfig = Get-PropertyValue $Config 'audit' $null
    if ($auditConfig) {
        $configuredBatch = Get-PropertyValue $auditConfig 'batch_pids' 8
        if ([int]$configuredBatch -gt 0) {
            $batchSize = [int]$configuredBatch
        }
    }

    $initialUnits = 0
    $splitNodes = 0
    $completed = 0
    $failedLeaves = 0
    $liveIssues = 0
    $rawIssues = 0
    $latest = $null
    $completeChapters = 0
    $postTotal = 0
    $postChecked = 0
    $postAccepted = 0
    $postReverted = 0
    $postUnresolved = 0
    $postRound = 0
    $postCompleteChapters = 0
    $postLatest = $null

    foreach ($chapter in $ChapterDirectories) {
        $manifest = Read-JsonSafe `
            (Join-Path $chapter.FullName 'manifest.json') @{}

        foreach ($chunk in @(Get-PropertyValue $manifest 'chunks' @())) {
            $pids = @(Get-PropertyValue $chunk 'pids' @())
            $initialUnits += [math]::Ceiling($pids.Count / $batchSize)
        }

        $auditDir = Join-Path $chapter.FullName 'audit'
        if (Test-Path -LiteralPath $auditDir) {
            $files = @(
                Get-ChildItem -LiteralPath $auditDir -Filter '*.json' -File `
                    -ErrorAction SilentlyContinue |
                Where-Object {
                    $_.Name -match '^c\d+_q\d+(?:[ab]+)?\.json$'
                }
            )

            foreach ($file in $files) {
                $data = Read-JsonSafe $file.FullName @{}
                $splitInto = Get-PropertyValue $data 'split_into' $null
                $failed = Get-PropertyValue $data 'failed' $null

                if ($splitInto) {
                    $splitNodes++
                }
                elseif ($failed) {
                    $failedLeaves++
                }
                else {
                    $completed++
                    $liveIssues += @(
                        Get-PropertyValue $data 'issues' @()
                    ).Count
                }

                if ($null -eq $latest -or $file.LastWriteTime -gt $latest.LastWriteTime) {
                    $latest = $file
                }
            }
        }

        $rawPath = Join-Path $chapter.FullName 'issues.qwen_raw.json'
        if (Test-Path -LiteralPath $rawPath) {
            $rawIssues += @(
                Read-JsonSafe $rawPath @()
            ).Count
            $completeChapters++
        }
        else {
            $issuesPath = Join-Path $chapter.FullName 'issues.json'
            if (Test-Path -LiteralPath $issuesPath) {
                $rawIssues += @(
                    Read-JsonSafe $issuesPath @()
                ).Count
            }
        }
    }

    $currentExpected = $initialUnits + $splitNodes
    $processed = $completed + $failedLeaves

    return [pscustomobject]@{
        InitialUnits = $initialUnits
        SplitNodes = $splitNodes
        ExpectedUnits = $currentExpected
        CompletedUnits = $completed
        FailedLeaves = $failedLeaves
        ProcessedUnits = $processed
        LiveIssues = $liveIssues
        RawIssues = if ($rawIssues -gt 0) { $rawIssues } else { $liveIssues }
        CompleteChapters = $completeChapters
        Latest = $latest
    }
}

function Get-VerifierStats {
    param(
        $ChapterDirectories,
        $Config
    )

    $hardCategories = @('missing', 'mixed_script')
    $verifierConfig = Get-PropertyValue $Config 'verifier' $null
    if ($verifierConfig) {
        $configured = @(
            Get-PropertyValue $verifierConfig `
                'hard_deterministic_categories' @()
        )
        if ($configured.Count -gt 0) {
            $hardCategories = $configured
        }
    }

    $rawTotal = 0
    $hardTotal = 0
    $checked = 0
    $repairDecision = 0
    $keepDecision = 0
    $uncertain = 0
    $latest = $null
    $completeChapters = 0

    foreach ($chapter in $ChapterDirectories) {
        $rawPath = Join-Path $chapter.FullName 'issues.qwen_raw.json'
        if (-not (Test-Path -LiteralPath $rawPath)) {
            continue
        }

        $raw = @(Read-JsonSafe $rawPath @())
        $rawTotal += $raw.Count

        foreach ($issue in $raw) {
            $isDeterministic = [bool](
                Get-PropertyValue $issue 'deterministic' $false
            )
            $category = [string](
                Get-PropertyValue $issue 'category' ''
            )
            if ($isDeterministic -and $category -in $hardCategories) {
                $hardTotal++
            }
        }

        $verifierDir = Join-Path $chapter.FullName 'verifier'
        if (Test-Path -LiteralPath $verifierDir) {
            foreach ($file in Get-ChildItem -LiteralPath $verifierDir `
                -Filter '*.json' -File -ErrorAction SilentlyContinue) {
                $data = Read-JsonSafe $file.FullName @{}
                $result = Get-PropertyValue $data 'result' $null
                $decision = [string](
                    Get-PropertyValue $result 'decision' ''
                )
                if (-not $decision) {
                    $legacyVerdict = [string](
                        Get-PropertyValue $result 'verdict' ''
                    )
                    $decision = switch ($legacyVerdict) {
                        'confirm' { 'repair' }
                        'reject' { 'keep' }
                        default { $legacyVerdict }
                    }
                }

                if ($decision) {
                    $checked++
                    switch ($decision) {
                        'repair' { $repairDecision++ }
                        'keep' { $keepDecision++ }
                        'uncertain' { $uncertain++ }
                    }
                }

                if ($null -eq $latest -or $file.LastWriteTime -gt $latest.LastWriteTime) {
                    $latest = $file
                }
            }
        }

        if (Test-Path -LiteralPath `
            (Join-Path $chapter.FullName 'verifier_report.json')) {
            $completeChapters++
        }
    }

    return [pscustomobject]@{
        RawTotal = $rawTotal
        HardTotal = $hardTotal
        ModelTotal = [math]::Max(0, $rawTotal - $hardTotal)
        Checked = $checked
        Processed = $checked + $hardTotal
        Repair = $repairDecision
        Keep = $keepDecision
        Confirm = $repairDecision
        Reject = $keepDecision
        Uncertain = $uncertain
        CompleteChapters = $completeChapters
        Latest = $latest
    }
}

function Get-RepairStats {
    param(
        $ChapterDirectories,
        $Config
    )

    $repairConfig = Get-PropertyValue $Config 'repair' $null
    $batchSize = [int](
        Get-PropertyValue $repairConfig 'max_pids_per_call' 4
    )
    if ($batchSize -le 0) {
        $batchSize = 4
    }

    $severities = @(
        Get-PropertyValue $repairConfig `
            'auto_repair_severities' @('critical', 'major')
    )
    $verifiedDecisions = @(
        Get-PropertyValue $repairConfig `
            'auto_repair_verified_decisions' @('repair')
    )
    $verifiedConfidences = @(
        Get-PropertyValue $repairConfig `
            'auto_repair_verifier_confidences' @('high', 'deterministic')
    )
    $deterministicCategories = @(
        Get-PropertyValue $repairConfig `
            'auto_repair_deterministic_categories' @()
    )

    $selectedPids = @{}
    $expectedBatches = 0
    $completedBatches = 0
    $accepted = 0
    $kept = 0
    $latest = $null
    $completeChapters = 0
    $postTotal = 0
    $postChecked = 0
    $postAccepted = 0
    $postReverted = 0
    $postUnresolved = 0
    $postRound = 0
    $postCompleteChapters = 0
    $postLatest = $null

    foreach ($chapter in $ChapterDirectories) {
        $issuesPath = Join-Path $chapter.FullName 'issues.json'
        $chapterPids = @{}

        if (Test-Path -LiteralPath $issuesPath) {
            foreach ($issue in @(Read-JsonSafe $issuesPath @())) {
                $issuePid = [string](Get-PropertyValue $issue 'pid' '')
                if (-not $issuePid) {
                    continue
                }

                $deterministic = [bool](
                    Get-PropertyValue $issue 'deterministic' $false
                )
                $category = [string](
                    Get-PropertyValue $issue 'category' ''
                )
                $severity = [string](
                    Get-PropertyValue $issue 'severity' ''
                )

                $verifierDecision = [string](
                    Get-PropertyValue $issue 'verifier_decision' ''
                )
                $verifierConfidence = [string](
                    Get-PropertyValue $issue 'verifier_confidence' ''
                )
                $selected = if ($verifierDecision) {
                    ($verifierDecision -in $verifiedDecisions) -and
                    ($verifierConfidence -in $verifiedConfidences)
                } elseif ($deterministic) {
                    $category -in $deterministicCategories
                } else {
                    $severity -in $severities
                }

                if ($selected) {
                    $chapterPids[$issuePid] = $true
                    $selectedPids["$($chapter.Name):$issuePid"] = $true
                }
            }
        }

        $expectedBatches += [math]::Ceiling(
            $chapterPids.Count / $batchSize
        )

        $repairDir = Join-Path $chapter.FullName 'repairs'
        if (Test-Path -LiteralPath $repairDir) {
            foreach ($file in Get-ChildItem -LiteralPath $repairDir `
                -Filter 'batch_*.json' -File -ErrorAction SilentlyContinue) {
                $data = Read-JsonSafe $file.FullName @{}
                $completedBatches++

                $acceptedMap = Get-PropertyValue $data 'accepted' $null
                if ($acceptedMap) {
                    $accepted += @($acceptedMap.PSObject.Properties).Count
                }

                foreach ($record in @(
                    Get-PropertyValue $data 'records' @()
                )) {
                    if (
                        (Get-PropertyValue $record 'action' '') -eq 'keep'
                    ) {
                        $kept++
                    }
                }

                if ($null -eq $latest -or $file.LastWriteTime -gt $latest.LastWriteTime) {
                    $latest = $file
                }
            }
        }

        if (Test-Path -LiteralPath `
            (Join-Path $chapter.FullName 'repaired_translations.json')) {
            $completeChapters++
        }

        $preverifyPath = Join-Path $chapter.FullName 'repaired_translations.preverify.json'
        $draftPath = Join-Path $chapter.FullName 'draft_translations.json'
        if ((Test-Path -LiteralPath $preverifyPath) -and (Test-Path -LiteralPath $draftPath)) {
            $preverify = Read-JsonSafe $preverifyPath @{}
            $draft = Read-JsonSafe $draftPath @{}
            foreach ($property in @($draft.PSObject.Properties)) {
                $candidateProperty = $preverify.PSObject.Properties[$property.Name]
                if ($candidateProperty -and [string]$candidateProperty.Value -ne [string]$property.Value) {
                    $postTotal++
                }
            }
        }

        $postDir = Join-Path $chapter.FullName 'post_repair_verifier'
        if (Test-Path -LiteralPath $postDir) {
            $postFiles = @(Get-ChildItem -LiteralPath $postDir -Filter '*.json' -File -ErrorAction SilentlyContinue)
            $postChecked += $postFiles.Count
            $candidateLatest = $postFiles | Sort-Object LastWriteTime -Descending | Select-Object -First 1
            if ($candidateLatest -and ($null -eq $postLatest -or $candidateLatest.LastWriteTime -gt $postLatest.LastWriteTime)) {
                $postLatest = $candidateLatest
            }
        }

        $postReportPath = Join-Path $chapter.FullName 'post_repair_report.json'
        if (Test-Path -LiteralPath $postReportPath) {
            $postReport = Read-JsonSafe $postReportPath @{}
            $postAccepted += [int](Get-PropertyValue $postReport 'accepted' 0)
            $postReverted += [int](Get-PropertyValue $postReport 'reverted' 0)
            $postUnresolved += [int](Get-PropertyValue $postReport 'unresolved_total' 0)
            $candidateRound = [int](Get-PropertyValue $postReport 'round' 1)
            if ($candidateRound -gt $postRound) { $postRound = $candidateRound }
            $postCompleteChapters++
        }
    }

    return [pscustomobject]@{
        SelectedPids = $selectedPids.Count
        ExpectedBatches = $expectedBatches
        CompletedBatches = $completedBatches
        Accepted = $accepted
        Kept = $kept
        CompleteChapters = $completeChapters
        Latest = $latest
        PostTotal = $postTotal
        PostChecked = $postChecked
        PostAccepted = $postAccepted
        PostReverted = $postReverted
        PostUnresolved = $postUnresolved
        PostRound = $postRound
        PostCompleteChapters = $postCompleteChapters
        PostLatest = $postLatest
    }
}


function Get-ActivePostRepairStats {
    param(
        $ChapterDirectories,
        [datetime]$Since
    )

    $checked = 0
    $accepted = 0
    $reverted = 0
    $uncertain = 0
    $latest = $null

    foreach ($chapter in $ChapterDirectories) {
        $postDir = Join-Path $chapter.FullName 'post_repair_verifier'
        if (-not (Test-Path -LiteralPath $postDir)) {
            continue
        }

        $files = @(
            Get-ChildItem -LiteralPath $postDir -Filter '*.json' -File `
                -ErrorAction SilentlyContinue |
            Where-Object {
                $_.LastWriteTime -ge $Since.AddSeconds(-2)
            }
        )

        foreach ($file in $files) {
            $data = Read-JsonSafe $file.FullName @{}
            $result = Get-PropertyValue $data 'result' $null
            $verdict = [string](
                Get-PropertyValue $result 'verdict' ''
            )

            if ($verdict) {
                $checked++
                switch ($verdict) {
                    'accept' { $accepted++ }
                    'reject' { $reverted++ }
                    'uncertain' {
                        $uncertain++
                        $reverted++
                    }
                }
            }

            if (
                $null -eq $latest -or
                $file.LastWriteTime -gt $latest.LastWriteTime
            ) {
                $latest = $file
            }
        }
    }

    return [pscustomobject]@{
        Checked = $checked
        Accepted = $accepted
        Reverted = $reverted
        Uncertain = $uncertain
        Latest = $latest
    }
}

function Get-FinalizeStats {
    param(
        $ChapterDirectories,
        $Config,
        [string]$OutputDir
    )

    $formatConfig = Get-PropertyValue $Config 'formatting' $null
    $batchSize = [int](
        Get-PropertyValue $formatConfig 'max_blocks_per_call' 12
    )
    if ($batchSize -le 0) {
        $batchSize = 12
    }

    $formatPids = 0
    $expectedBatches = 0
    $completedBatches = 0
    $completeChapters = 0
    $latest = $null

    foreach ($chapter in $ChapterDirectories) {
        $manifest = Read-JsonSafe `
            (Join-Path $chapter.FullName 'manifest.json') @{}

        $chapterFormatPids = 0
        foreach ($block in @(
            Get-PropertyValue $manifest 'blocks' @()
        )) {
            $inlineSpans = @(
                Get-PropertyValue $block 'inline_spans' @()
            )
            if ($inlineSpans.Count -gt 0) {
                $chapterFormatPids++
            }
        }

        $formatPids += $chapterFormatPids
        $expectedBatches += [math]::Ceiling(
            $chapterFormatPids / $batchSize
        )

        $formatDir = Join-Path $chapter.FullName 'formatting'
        if (Test-Path -LiteralPath $formatDir) {
            foreach ($file in Get-ChildItem -LiteralPath $formatDir `
                -Filter 'batch_*.json' -File -ErrorAction SilentlyContinue) {
                $completedBatches++
                if ($null -eq $latest -or $file.LastWriteTime -gt $latest.LastWriteTime) {
                    $latest = $file
                }
            }
        }

        $state = Read-JsonSafe `
            (Join-Path $chapter.FullName 'state.json') @{}
        if ((Get-PropertyValue $state 'status' '') -eq 'complete') {
            $completeChapters++
        }
    }

    $outputFiles = if (Test-Path -LiteralPath $OutputDir) {
        @(
            Get-ChildItem -LiteralPath $OutputDir -File `
                -ErrorAction SilentlyContinue
        ).Count
    } else {
        0
    }

    return [pscustomobject]@{
        FormatPids = $formatPids
        ExpectedBatches = $expectedBatches
        CompletedBatches = $completedBatches
        OutputFiles = $outputFiles
        CompleteChapters = $completeChapters
        Latest = $latest
    }
}

function Resolve-MonitorRunRoot {
    param(
        [string]$ResolvedProjectRoot,
        [string]$RequestedRunRoot,
        [int]$RequestedStart,
        [int]$RequestedEnd
    )

    if ($RequestedRunRoot) {
        if (-not (Test-Path -LiteralPath $RequestedRunRoot)) {
            throw "RunRoot не найден: $RequestedRunRoot"
        }
        return (Resolve-Path -LiteralPath $RequestedRunRoot).Path
    }

    if ($RequestedStart -gt 0) {
        $resolvedEnd = if ($RequestedEnd -gt 0) {
            $RequestedEnd
        } else {
            $RequestedStart
        }

        $candidate = Join-Path $ResolvedProjectRoot `
            "pipeline_runs\chapter_${RequestedStart}_to_${resolvedEnd}"

        if (-not (Test-Path -LiteralPath $candidate)) {
            throw "Каталог запуска не найден: $candidate"
        }

        return (Resolve-Path -LiteralPath $candidate).Path
    }

    $pipelineRuns = Join-Path $ResolvedProjectRoot 'pipeline_runs'
    $latest = Get-ChildItem -LiteralPath $pipelineRuns -Directory `
        -ErrorAction SilentlyContinue |
        Where-Object {
            Test-Path -LiteralPath `
                (Join-Path $_.FullName 'config.full_pipeline.json')
        } |
        Sort-Object LastWriteTime -Descending |
        Select-Object -First 1

    if ($null -eq $latest) {
        throw "В $pipelineRuns не найдено ни одного запуска pipeline."
    }

    return $latest.FullName
}

$ProjectRoot = (Resolve-Path -LiteralPath $ProjectRoot).Path
$ResolvedRunRoot = Resolve-MonitorRunRoot `
    $ProjectRoot $RunRoot $Start $End

$WorkDir = Join-Path $ResolvedRunRoot 'work'
$OutputDir = Join-Path $ResolvedRunRoot 'output'
$ServerLogsDir = Join-Path $ResolvedRunRoot 'server_logs'
$ConfigPath = Join-Path $ResolvedRunRoot 'config.full_pipeline.json'
$Range = Get-RunRange $ResolvedRunRoot $Start $End

do {
    try {
        $config = Read-JsonSafe $ConfigPath @{}
        $chapters = @(Get-ChapterDirectories $WorkDir)
        $expectedChapters = if ($Range.Count -gt 0) {
            $Range.Count
        } else {
            $chapters.Count
        }

        $preparedChapters = @(
            $chapters | Where-Object {
                Test-Path -LiteralPath `
                    (Join-Path $_.FullName 'chapter_bible.json')
            }
        ).Count

        $translation = Get-TranslationStats $chapters
        $audit = Get-AuditStats $chapters $config
        $verifier = Get-VerifierStats $chapters $config
        $repair = Get-RepairStats $chapters $config
        $finalize = Get-FinalizeStats $chapters $config $OutputDir

        $server = Get-ServerInfo
        $worker = Get-WorkerInfo
        $gpu = Get-GpuMemory $server.Pid
        $memory = Get-SystemMemory
        $speed = Get-SpeedInfo $ServerLogsDir $server.Profile

        $activePost = $null
        if (
            $worker.Phase -eq 'PostRepairVerify' -and
            $null -ne $worker.StartedAt
        ) {
            $activePost = Get-ActivePostRepairStats `
                $chapters $worker.StartedAt
        }

        $postDisplayChecked = if ($activePost) {
            $activePost.Checked
        } else {
            $repair.PostChecked
        }
        $postDisplayAccepted = if ($activePost) {
            $activePost.Accepted
        } else {
            $repair.PostAccepted
        }
        $postDisplayReverted = if ($activePost) {
            $activePost.Reverted
        } else {
            $repair.PostReverted
        }
        $postDisplayLatest = if ($activePost -and $activePost.Latest) {
            $activePost.Latest
        } else {
            $repair.PostLatest
        }

        $stageNumber = 0
        $stageLabel = 'Ожидание запуска или этапы завершены'
        $stageDone = 0
        $stageTotal = 1
        $currentItem = ''

        # The active Python worker is authoritative. This avoids stale
        # state.json/output/post-repair files from an earlier rerun making the
        # monitor report PIPELINE COMPLETE while a downstream stage is active.
        if ($worker.Phase -eq 'Context') {
            $stageNumber = 1
            $stageLabel = 'Prepare and sanitize chapter context'
            $stageDone = $preparedChapters
            $stageTotal = $expectedChapters
        }
        elseif ($worker.Phase -eq 'Translate') {
            $stageNumber = 2
            $stageLabel = 'Gemma translation'
            $stageDone = $translation.TranslatedPids
            $stageTotal = $translation.TotalPids
            if ($translation.Latest) {
                $currentItem = $translation.Latest.BaseName
            }
        }
        elseif ($worker.Phase -eq 'Audit') {
            $stageNumber = 3
            $stageLabel = 'Qwen bilingual audit'
            $stageDone = [math]::Min(
                $audit.ProcessedUnits,
                $audit.ExpectedUnits
            )
            $stageTotal = $audit.ExpectedUnits
            if ($audit.Latest) {
                $currentItem = $audit.Latest.BaseName
            }
        }
        elseif ($worker.Phase -eq 'Verify') {
            $stageNumber = 4
            $stageLabel = 'Gemma verifier, thinking 128'
            $stageDone = $verifier.Processed
            $stageTotal = $verifier.RawTotal
            if ($verifier.Latest) {
                $currentItem = $verifier.Latest.BaseName
            }
        }
        elseif ($worker.Phase -eq 'Repair') {
            $stageNumber = 5
            $stageLabel = 'Gemma repairs confirmed issues'
            $stageDone = $repair.CompletedBatches
            $stageTotal = $repair.ExpectedBatches
            if ($repair.Latest) {
                $currentItem = $repair.Latest.BaseName
            }
        }
        elseif ($worker.Phase -eq 'PostRepairVerify') {
            $stageNumber = 55
            $stageLabel = 'Post-repair safety verification'
            $stageDone = $postDisplayChecked
            $stageTotal = $repair.PostTotal
            if ($postDisplayLatest) {
                $currentItem = $postDisplayLatest.BaseName
            }
        }
        elseif ($worker.Phase -eq 'Finalize') {
            $stageNumber = 6
            $stageLabel = 'Restore formatting and finalize HTML'
            $stageDone = if ($finalize.ExpectedBatches -gt 0) {
                $finalize.CompletedBatches
            } else {
                $finalize.CompleteChapters
            }
            $stageTotal = if ($finalize.ExpectedBatches -gt 0) {
                $finalize.ExpectedBatches
            } else {
                $expectedChapters
            }
            if ($finalize.Latest) {
                $currentItem = $finalize.Latest.BaseName
            }
        }
        elseif ($server.Profile -eq 'Qwen') {
            $stageNumber = 3
            $stageLabel = 'Qwen bilingual audit — между запросами'
            $stageDone = [math]::Min(
                $audit.ProcessedUnits,
                $audit.ExpectedUnits
            )
            $stageTotal = $audit.ExpectedUnits
        }
        elseif ($server.Profile -eq 'GemmaRepair') {
            $stageNumber = 5
            $stageLabel = 'Gemma repair — между запросами'
            $stageDone = $repair.CompletedBatches
            $stageTotal = $repair.ExpectedBatches
        }
        elseif (
            $server.Profile -eq 'GemmaVerify' -and
            $repair.CompleteChapters -ge $expectedChapters -and
            $repair.PostCompleteChapters -lt $expectedChapters
        ) {
            $stageNumber = 55
            $stageLabel = 'Post-repair safety verification — запуск'
            $stageDone = $postDisplayChecked
            $stageTotal = $repair.PostTotal
        }
        elseif ($server.Profile -eq 'GemmaVerify') {
            $stageNumber = 4
            $stageLabel = 'Gemma verifier — между запросами'
            $stageDone = $verifier.Processed
            $stageTotal = $verifier.RawTotal
        }
        elseif ($server.Profile -eq 'GemmaTranslate') {
            $stageNumber = 6
            $stageLabel = 'Formatting/finalization — запуск'
            $stageDone = $finalize.CompletedBatches
            $stageTotal = $finalize.ExpectedBatches
        }
        elseif (
            $expectedChapters -gt 0 -and
            $finalize.CompleteChapters -ge $expectedChapters -and
            $server.Profile -eq 'none'
        ) {
            $stageNumber = 7
            $stageLabel = 'PIPELINE COMPLETE'
            $stageDone = 1
            $stageTotal = 1
        }
        elseif (
            $verifier.RawTotal -gt 0 -and
            $verifier.CompleteChapters -lt $expectedChapters
        ) {
            $stageNumber = 4
            $stageLabel = 'Verifier остановлен или между процессами'
            $stageDone = $verifier.Processed
            $stageTotal = $verifier.RawTotal
        }
        elseif (
            $audit.ExpectedUnits -gt 0 -and
            $audit.ProcessedUnits -lt $audit.ExpectedUnits
        ) {
            $stageNumber = 3
            $stageLabel = 'Audit остановлен или между процессами'
            $stageDone = $audit.ProcessedUnits
            $stageTotal = $audit.ExpectedUnits
        }
        elseif (
            $translation.TotalPids -gt 0 -and
            $translation.TranslatedPids -lt $translation.TotalPids
        ) {
            $stageNumber = 2
            $stageLabel = 'Translation остановлен или между процессами'
            $stageDone = $translation.TranslatedPids
            $stageTotal = $translation.TotalPids
        }

        if (-not $NoClear) {
            Clear-Host
        }

        Write-Host "PACT PIPELINE MONITOR v$MonitorVersion" `
            -ForegroundColor Cyan
        Write-Host ('=' * 72)
        Write-Host "Run:        $ResolvedRunRoot"
        if ($Range.Count -gt 0) {
            Write-Host "Chapters:   $($Range.Start)-$($Range.End)"
        }
        Write-Host "Updated:    $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')"
        Write-Host ''

        if ($stageNumber -eq 7) {
            Write-Host "Status:     $stageLabel" -ForegroundColor Green
        }
        elseif ($stageNumber -gt 0) {
            $stageCode = if ($stageNumber -eq 55) {
                '5b'
            } elseif ($stageNumber -eq 5) {
                '5a'
            } else {
                [string]$stageNumber
            }
            Write-Host "Stage:      $stageCode/6 — $stageLabel" `
                -ForegroundColor Yellow
        }
        else {
            Write-Host "Status:     $stageLabel" -ForegroundColor DarkYellow
        }

        Write-Host "Progress:   $(Get-ProgressBar $stageDone $stageTotal)"
        if ($currentItem) {
            Write-Host "Current:    $currentItem"
        }

        Write-Host ''
        Write-Host 'STAGES' -ForegroundColor Cyan
        Write-Host (
            "1 Context     {0,4}/{1,-4} {2}" -f
            $preparedChapters,
            $expectedChapters,
            (Get-ProgressBar $preparedChapters $expectedChapters 18)
        )
        Write-Host (
            "2 Translation {0,4}/{1,-4} PID {2}" -f
            $translation.TranslatedPids,
            $translation.TotalPids,
            (Get-ProgressBar `
                $translation.TranslatedPids $translation.TotalPids 18)
        )
        Write-Host (
            "3 Qwen audit  {0,4}/{1,-4} units {2}" -f
            $audit.ProcessedUnits,
            $audit.ExpectedUnits,
            (Get-ProgressBar $audit.ProcessedUnits $audit.ExpectedUnits 18)
        )
        Write-Host (
            "4 Verifier    {0,4}/{1,-4} issues {2}" -f
            $verifier.Processed,
            $verifier.RawTotal,
            (Get-ProgressBar $verifier.Processed $verifier.RawTotal 18)
        )
        Write-Host (
            "5a Repair     {0,4}/{1,-4} batches {2}" -f
            $repair.CompletedBatches,
            $repair.ExpectedBatches,
            (Get-ProgressBar `
                $repair.CompletedBatches $repair.ExpectedBatches 18)
        )
        Write-Host (
            "5b Safety     {0,4}/{1,-4} repairs {2}" -f
            $postDisplayChecked,
            $repair.PostTotal,
            (Get-ProgressBar `
                $postDisplayChecked $repair.PostTotal 18)
        )

        $finalizeDisplayOutputs = if (
            $stageNumber -in @(1, 2, 3, 4, 5, 55)
        ) {
            0
        } else {
            $finalize.OutputFiles
        }

        Write-Host (
            "6 Finalize    {0,4}/{1,-4} outputs {2}" -f
            $finalizeDisplayOutputs,
            $expectedChapters,
            (Get-ProgressBar `
                $finalizeDisplayOutputs $expectedChapters 18)
        )

        Write-Host ''
        Write-Host 'CURRENT STAGE DETAILS' -ForegroundColor Cyan
        switch ($stageNumber) {
            1 {
                Write-Host "Prepared chapters:       $preparedChapters / $expectedChapters"
            }
            2 {
                Write-Host "Translated PID:          $($translation.TranslatedPids) / $($translation.TotalPids)"
                Write-Host "Completed chapters:      $($translation.CompleteChapters) / $expectedChapters"
            }
            3 {
                Write-Host "Base audit units:        $($audit.InitialUnits)"
                Write-Host "Additional split nodes:  $($audit.SplitNodes)"
                Write-Host "Successful units:        $($audit.CompletedUnits)"
                Write-Host "Failed leaf units:       $($audit.FailedLeaves)"
                Write-Host "Raw issues so far:       $($audit.RawIssues)"
            }
            4 {
                Write-Host "Raw issues:              $($verifier.RawTotal)"
                Write-Host "Hard deterministic:      $($verifier.HardTotal)"
                Write-Host "Checked by Gemma:        $($verifier.Checked) / $($verifier.ModelTotal)"
                Write-Host "Repair / Keep:           $($verifier.Repair) / $($verifier.Keep)"
                Write-Host "Uncertain:               $($verifier.Uncertain)"
            }
            5 {
                Write-Host "Selected PID:            $($repair.SelectedPids)"
                Write-Host "Repair batches:          $($repair.CompletedBatches) / $($repair.ExpectedBatches)"
                Write-Host "Accepted replacements:   $($repair.Accepted)"
                Write-Host "Unresolved after 5a:     $($repair.Kept)"
                Write-Host "Post-check round:        $($repair.PostRound)"
                Write-Host "Still unresolved:        $($repair.PostUnresolved)"
            }
            55 {
                Write-Host "Repairs to safety-check: $($repair.PostTotal)"
                Write-Host "Checked this run:        $postDisplayChecked / $($repair.PostTotal)"
                Write-Host "Accepted this run:       $postDisplayAccepted"
                Write-Host "Still unresolved:        $($repair.PostUnresolved)"
                Write-Host "Verification round:      $($repair.PostRound)"
                Write-Host "Reverted this run:       $postDisplayReverted"
                if ($activePost) {
                    Write-Host "Uncertain this run:      $($activePost.Uncertain)"
                }
            }
            6 {
                Write-Host "Formatting PID:          $($finalize.FormatPids)"
                Write-Host "Formatting batches:      $($finalize.CompletedBatches) / $($finalize.ExpectedBatches)"
                Write-Host "Final outputs:           $($finalize.OutputFiles) / $expectedChapters"
            }
            7 {
                Write-Host "Final outputs:           $($finalize.OutputFiles)"
                Write-Host "Completed chapters:      $($finalize.CompleteChapters)"
            }
        }

        Write-Host ''
        Write-Host 'SERVER' -ForegroundColor Cyan
        Write-Host "Profile:                 $($server.Label)"
        Write-Host "Server PID:              $($server.Pid)"
        Write-Host "Worker:                  $($worker.Label)"
        Write-Host "Worker PID:              $($worker.Pid)"

        if ($null -ne $gpu.DedicatedGiB) {
            Write-Host (
                "GPU memory:              {0:N2} GiB dedicated / {1:N2} GiB shared" -f
                $gpu.DedicatedGiB,
                $gpu.SharedGiB
            )
        } else {
            Write-Host "GPU memory:              unavailable"
        }

        if ($null -ne $memory.AvailableGiB) {
            Write-Host (
                "System memory:           {0:N2} GiB available; pagefile {1:N1}%" -f
                $memory.AvailableGiB,
                $memory.PagefilePercent
            )
        }

        $promptText = if ($null -ne $speed.PromptSpeed) {
            "{0:N2} t/s" -f $speed.PromptSpeed
        } else {
            'ещё нет'
        }

        $generationText = if ($null -ne $speed.GenerationSpeed) {
            "{0:N2} t/s" -f $speed.GenerationSpeed
        } else {
            'ещё нет'
        }

        $liveText = if ($null -ne $speed.LiveGenerationSpeed) {
            "{0:N2} t/s ({1} decoded)" -f
                $speed.LiveGenerationSpeed,
                $speed.LastDecoded
        } else {
            'нет активной выборки'
        }

        Write-Host "Prompt speed:            $promptText"
        Write-Host "Generation speed:        $generationText"
        Write-Host "Live generation:         $liveText"

        if ($speed.Log) {
            Write-Host "Server log:              $($speed.Log.Name)"
        }

        Write-Host ''
        if ($Once) {
            Write-Host 'Однократный снимок завершён.'
        }
        else {
            Write-Host (
                "Обновление каждые $RefreshSeconds сек. " +
                'Ctrl+C остановит только монитор.'
            )
        }
    }
    catch {
        if (-not $NoClear) {
            Clear-Host
        }
        Write-Host "MONITOR ERROR: $($_.Exception.Message)" `
            -ForegroundColor Red
        Write-Host "Run: $ResolvedRunRoot"

        if ($_.InvocationInfo.PositionMessage) {
            Write-Host ''
            Write-Host 'Location:' -ForegroundColor DarkYellow
            Write-Host $_.InvocationInfo.PositionMessage
        }

        if ($_.ScriptStackTrace) {
            Write-Host ''
            Write-Host 'Stack:' -ForegroundColor DarkYellow
            Write-Host $_.ScriptStackTrace
        }

        if ($Once) {
            throw
        }
    }

    if (-not $Once) {
        Start-Sleep -Seconds ([math]::Max(1, $RefreshSeconds))
    }
}
while (-not $Once)
