[CmdletBinding()]
param(
    [string]$ProjectRoot = 'D:\pact\pact_translator_v3',
    [int]$Start = 60,
    [int]$End = 60,
    [int]$RefreshSeconds = 3,
    [switch]$Once
)

# This script is deliberately read-only: it never creates, updates, or removes
# run files and never starts, stops, or probes model endpoints.
$ErrorActionPreference = 'SilentlyContinue'
$MonitorVersion = '3.1.3-05'
$RunRoot = Join-Path $ProjectRoot "pipeline_runs\chapter_${Start}_to_${End}_v31"
$WorkDir = Join-Path $RunRoot 'work'

function Read-JsonSafe([string]$Path) {
    if (-not (Test-Path -LiteralPath $Path)) { return $null }
    try { return Get-Content -LiteralPath $Path -Raw | ConvertFrom-Json } catch { return $null }
}
function Get-Value($Object, [string]$Name, $Default=$null) {
    if ($null -eq $Object) { return $Default }
    $property = $Object.PSObject.Properties[$Name]
    if ($null -eq $property -or $null -eq $property.Value) { return $Default }
    return $property.Value
}
function Test-CompleteAggregate($Data) {
    if ($null -eq $Data -or -not (Get-Value $Data 'version')) { return $false }
    $expected = Get-Value $Data 'expected'
    $completed = Get-Value $Data 'completed'
    if ($null -ne $expected -or $null -ne $completed) {
        return ($expected -is [ValueType] -and $completed -is [ValueType] -and $expected -eq $completed)
    }
    return $true
}
function Test-CompatibleArtifactVersion($Version, [string]$ExpectedVersion, [string[]]$LegacyVersions) {
    if ($Version -eq $ExpectedVersion) { return $true }
    return $LegacyVersions -contains $Version
}
function Get-Progress($Data, [int]$FallbackTotal) {
    if ($null -eq $Data) { return @{ done=0; total=$FallbackTotal } }
    $coverage = Get-Value $Data 'coverage'
    $done = Get-Value $coverage 'completed' (Get-Value $Data 'completed' 0)
    $total = Get-Value $coverage 'expected' (Get-Value $Data 'expected' $FallbackTotal)
    return @{ done=[int]$done; total=[int]$total }
}
function Get-FirstMissing($Work, [int]$Total) {
    $source = Read-JsonSafe (Join-Path $Work 'source_scene_map.json')
    if (-not (Test-CompleteAggregate $source)) { return 'source_analysis' }
    $draft = Read-JsonSafe (Join-Path $Work 'draft_translations.json')
    if ($null -eq $draft -or @($draft.PSObject.Properties).Count -lt $Total) { return 'translation' }
    foreach ($pass in @('primary','residual')) {
        foreach ($name in @('qwen_semantic','gemma_semantic','gemma_russian','gemma_discourse','cross_verify_gemma','cross_verify_qwen')) {
            if (-not (Test-CompleteAggregate (Read-JsonSafe (Join-Path $Work "v31\$pass\$name.json")))) { return "$pass/$name" }
        }
        if (-not (Test-CompleteAggregate (Read-JsonSafe (Join-Path $Work "v31\$pass\verification_report.json")))) { return "$pass/finalize_verification" }
    }
    if (-not (Test-CompleteAggregate (Read-JsonSafe (Join-Path $Work 'v31_quality_gate.json')))) { return 'final_quality' }
    return 'finalization'
}
function Get-LatestArtifact($Root) {
    $file = Get-ChildItem -LiteralPath $Root -Recurse -File -ErrorAction SilentlyContinue |
        Where-Object { $_.Name -notlike '*.tmp' } | Sort-Object LastWriteTimeUtc -Descending | Select-Object -First 1
    if ($file) { return ("{0} ({1})" -f $file.FullName.Substring($Root.Length).TrimStart('\\'), $file.LastWriteTime.ToString('yyyy-MM-dd HH:mm:ss')) }
    return 'none'
}
function Test-OwnedProcess($State) {
    $ownedPid = Get-Value $State 'owned_pid'
    if ($null -eq $ownedPid) { return $false }
    return $null -ne (Get-Process -Id ([int]$ownedPid) -ErrorAction SilentlyContinue)
}
function Get-LiveDiagnostics($Root, $Work, $Profile) {
    # Diagnostics are deliberately one-way: they are never used by the state
    # machine below to decide ACTIVE/COMPLETE/READY.
    $logs = Join-Path $Root 'server_logs'
    $log = if (Test-Path -LiteralPath $logs) {
        Get-ChildItem -LiteralPath $logs -Filter $(if ($Profile -and $Profile -ne 'none') { "${Profile}_*_stderr.log" } else { '*_stderr.log' }) -File -ErrorAction SilentlyContinue |
            Sort-Object LastWriteTimeUtc -Descending | Select-Object -First 1
    }
    if (-not $log) { return @{ summary='server log: N/A'; progress=@() } }
    $age = ((Get-Date).ToUniversalTime() - $log.LastWriteTimeUtc).TotalSeconds
    $tail = @(Get-Content -LiteralPath $log.FullName -Tail 700 -ErrorAction SilentlyContinue)
    $prompt = ($tail | Where-Object { $_ -match 'prompt eval time\s*=' } | Select-Object -Last 1)
    $generation = ($tail | Where-Object { $_ -match '\beval time\s*=' -and $_ -notmatch 'prompt eval time' } | Select-Object -Last 1)
    $live = ($tail | Where-Object { $_ -match '\bn_decoded\s*=' -and $_ -match '\btg\s*=' } | Select-Object -Last 1)
    $pt = if ($prompt -match '([\d.]+)\s+tokens per second') { $Matches[1] } else { 'N/A' }
    $gt = if ($generation -match '([\d.]+)\s+tokens per second') { $Matches[1] } else { 'N/A' }
    $decoded = if ($live -match '\bn_decoded\s*=\s*(\d+)') { $Matches[1] } else { 'N/A' }
    $liveTps = if ($live -match '\btg\s*=\s*([\d.]+)\s+t/s') { $Matches[1] } else { 'N/A' }
    $label = if ($age -gt 30) { 'stale' } else { 'live' }
    $progress = @()
    foreach ($dir in @(Get-ChildItem -LiteralPath $Work -Directory -ErrorAction SilentlyContinue)) {
        $batches = @(Get-ChildItem -LiteralPath (Join-Path $dir.FullName 'v31_source_analysis') -Filter 'batch_*.json' -File -ErrorAction SilentlyContinue).Count
        if ($batches -gt 0) { $progress += "$($dir.Name) source batches: $batches" }
        $draft = Read-JsonSafe (Join-Path $dir.FullName 'draft_translations.json')
        $total = @((Read-JsonSafe (Join-Path $dir.FullName 'manifest.json')).blocks).Count
        if ($draft) { $progress += "$($dir.Name) translation: $(@($draft.PSObject.Properties).Count)/$total" }
    }
    return @{ summary="server log: $label; prompt: $pt t/s; generation: $gt t/s; live: $liveTps t/s; decoded: $decoded; MTP: N/A"; progress=$progress }
}
function Show-Monitor {
    if (-not (Test-Path -LiteralPath $RunRoot)) { Write-Host "Run has not started: $RunRoot" -ForegroundColor Yellow; return }
    $config = Read-JsonSafe (Join-Path $RunRoot 'config.full_pipeline.v31.json')
    $state = Read-JsonSafe (Join-Path $RunRoot 'monitor_state.v31.json')
    $manifest = Read-JsonSafe (Join-Path $RunRoot 'chapter_manifest.v31.json')
    $ensemble = Get-Value $config 'ensemble_v31'
    $expectedArtifactVersion = Get-Value $state 'artifact_version' (Get-Value $ensemble 'version' 'unknown')
    $legacyVersions = @((Get-Value $ensemble 'legacy_compatible_artifact_versions' @()))
    $legacyProvenance = Read-JsonSafe (Join-Path $RunRoot 'v31\legacy_reuse_provenance.json')
    $durableLegacyRecords = @((Get-Value $legacyProvenance 'records' @()))
    $buildIdentity = Get-Value $state 'runner_version' 'unknown'
    $chapters = @($manifest.chapters)
    $profile = Get-Value $state 'active_profile' 'none'
    $processActive = Test-OwnedProcess $state
    $status = Get-Value $state 'status' 'UNKNOWN'
    $stage = Get-Value $state 'stage' 'not recorded'
    if ($status -eq 'ACTIVE' -and -not $processActive) { $status = 'INTERRUPTED (owned process inactive)' }
    if ($status -eq 'LOADING_MODEL') { $stage = "$stage (model loading; stage not executing)" }
    $complete = 0; $reused = 0; $mixed = @(); $missing = @(); $partial = @()
    $legacyCompatible = @()
    foreach ($record in $durableLegacyRecords) {
        $legacyCompatible += ("{0} ({1})" -f (Get-Value $record 'artifact_path' 'unknown'), (Get-Value $record 'artifact_version' 'unknown'))
    }
    foreach ($chapter in $chapters) {
        $stem = [IO.Path]::GetFileNameWithoutExtension((Get-Value $chapter 'filename'))
        $work = Join-Path $WorkDir $stem
        $total = @((Read-JsonSafe (Join-Path $work 'manifest.json')).blocks).Count
        $first = Get-FirstMissing $work $total
        if ($first -eq 'finalization' -and (Test-Path -LiteralPath (Join-Path $RunRoot ("output\\" + (Get-Value $chapter 'filename'))))) { $complete++ } else { $missing += "${stem}:$first" }
        foreach ($path in @(Get-ChildItem -LiteralPath $work -Recurse -Filter '*.json' -File -ErrorAction SilentlyContinue)) {
            $data = Read-JsonSafe $path.FullName
            $observedArtifactVersion = Get-Value $data 'version'
            if ($observedArtifactVersion -and $observedArtifactVersion -ne $expectedArtifactVersion -and $legacyVersions -contains $observedArtifactVersion) {
                $legacyCompatible += ("{0} ({1})" -f $path.Name, $observedArtifactVersion)
            } elseif ($observedArtifactVersion -and -not (Test-CompatibleArtifactVersion $observedArtifactVersion $expectedArtifactVersion $legacyVersions)) { $mixed += $path.Name }
        }
        foreach ($pass in @('primary','residual')) {
            foreach ($name in @('qwen_semantic','gemma_semantic','gemma_russian','gemma_discourse')) {
                $progress = Get-Progress (Read-JsonSafe (Join-Path $work "v31\$pass\$name.json")) $total
                if ($progress.done -gt 0 -and $progress.done -lt $progress.total) { $partial += "$stem $pass audit/$name $($progress.done)/$($progress.total)" }
            }
            foreach ($name in @('cross_verify_gemma','cross_verify_qwen')) {
                $progress = Get-Progress (Read-JsonSafe (Join-Path $work "v31\$pass\$name.json")) 0
                if ($progress.done -gt 0 -and $progress.done -lt $progress.total) { $partial += "$stem $pass cross-verify/$name $($progress.done)/$($progress.total)" }
            }
        }
    }
    if ($status -eq 'REUSED') { $reused = $chapters.Count }
    $terminal = Read-JsonSafe (Join-Path $WorkDir ((Get-Value $chapters[0] 'filename' '').Replace('.html','') + '\state.json'))
    $stale = ($terminal -and $complete -lt $chapters.Count)
    $failure = Get-Value $state 'failure_reason' 'none'
    $blocked = ($status -eq 'FAILED' -or $mixed.Count -gt 0)
    if ($stale) { $blocked = $true }
    $diagnostics = Get-LiveDiagnostics $RunRoot $WorkDir $profile
    Write-Host "PACT MONITOR v$MonitorVersion  build: $buildIdentity; artifact version: $expectedArtifactVersion"
    Write-Host 'AUTHORITATIVE STATE'
    Write-Host "Stage: $stage"
    Write-Host "Outcome/status: $status"
    Write-Host "Owned model: $profile; process active: $processActive"
    Write-Host "Aggregate: complete $complete/$($chapters.Count); reused $reused/$($chapters.Count)"
    Write-Host "Partial: $(if ($partial) { $partial -join '; ' } else { 'none' })"
    Write-Host "First missing: $(if ($missing) { $missing -join '; ' } else { 'none' })"
    Write-Host "Latest artifact: $(Get-LatestArtifact $RunRoot)"
    Write-Host "Failure reason: $failure"
    Write-Host "Stale complete: $stale"
    Write-Host "Mixed-version artifacts: $(if ($mixed) { $mixed -join ', ' } else { 'none' })"
    Write-Host "Legacy-compatible artifacts: $(if ($legacyCompatible) { $legacyCompatible -join ', ' } else { 'none' })"
    Write-Host "Resume: $(if ($blocked) { 'BLOCKED' } else { 'READY' })"
    Write-Host 'LIVE DIAGNOSTICS (non-authoritative)'
    Write-Host $diagnostics.summary
    Write-Host "Progress: $(if ($diagnostics.progress) { $diagnostics.progress -join '; ' } else { 'N/A' })"
}
while ($true) { if (-not $Once) { Clear-Host }; Show-Monitor; if ($Once) { break }; Start-Sleep -Seconds ([math]::Max(1,$RefreshSeconds)) }
