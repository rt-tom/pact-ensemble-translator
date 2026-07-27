$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

function Assert-Match { param([string]$Text,[string]$Pattern,[string]$Message) if ($Text -notmatch $Pattern) { throw "$Message`n$Text" } }
function Put-Json { param([string]$Path,$Value) New-Item -ItemType Directory -Force -Path (Split-Path $Path) | Out-Null; $Value | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath $Path -Encoding utf8 }
function New-Fixture { param([string]$Root,[string]$Kind,[string]$ArtifactVersion)
    $run = Join-Path $Root 'pipeline_runs\chapter_1_to_1_v31'; $work = Join-Path $run 'work\001_test'
    New-Item -ItemType Directory -Force -Path $work | Out-Null
    Put-Json (Join-Path $run 'chapter_manifest.v31.json') @{ chapters=@(@{filename='001_test.html'}) }
    $ensemble = @{version=$ArtifactVersion}
    if ($Kind -eq 'legacy-compatible') { $ensemble['legacy_compatible_artifact_versions'] = @('3.1.2j') }
    Put-Json (Join-Path $run 'config.full_pipeline.v31.json') @{ensemble_v31=$ensemble}
    Put-Json (Join-Path $run 'monitor_state.v31.json') @{runner_version='3.1.3-03';artifact_version=$ArtifactVersion;stage='primary audit';status='ACTIVE';active_profile='Qwen';owned_pid=999999;updated_at='2026-01-01T00:00:00Z'}
    Put-Json (Join-Path $work 'manifest.json') @{blocks=@(@{pid=1},@{pid=2})}
    if ($Kind -eq 'fresh') { return }
    Put-Json (Join-Path $work 'source_scene_map.json') @{version=$ArtifactVersion;expected=2;completed=2}
    Put-Json (Join-Path $work 'draft_translations.json') @{a='x';b='y'}
    if ($Kind -eq 'partial-audit') { Put-Json (Join-Path $work 'v31\primary\qwen_semantic.json') @{version=$ArtifactVersion;expected=2;completed=1}; return }
    Put-Json (Join-Path $work 'v31\primary\qwen_semantic.json') @{version=$ArtifactVersion;expected=2;completed=2}
    if ($Kind -eq 'partial-cross') { Put-Json (Join-Path $work 'v31\primary\gemma_semantic.json') @{version=$ArtifactVersion;expected=2;completed=2}; Put-Json (Join-Path $work 'v31\primary\gemma_russian.json') @{version=$ArtifactVersion;expected=2;completed=2}; Put-Json (Join-Path $work 'v31\primary\gemma_discourse.json') @{version=$ArtifactVersion;expected=2;completed=2}; Put-Json (Join-Path $work 'v31\primary\cross_verify_gemma.json') @{version=$ArtifactVersion;expected=2;completed=2}; Put-Json (Join-Path $work 'v31\primary\cross_verify_qwen.json') @{version=$ArtifactVersion;expected=2;completed=1}; return }
    foreach ($pass in @('primary','residual')) { foreach ($name in @('qwen_semantic','gemma_semantic','gemma_russian','gemma_discourse','cross_verify_gemma','cross_verify_qwen','verification_report')) { Put-Json (Join-Path $work "v31\$pass\$name.json") @{version=$ArtifactVersion;expected=2;completed=2} } }
    if ($Kind -eq 'failed') { Put-Json (Join-Path $run 'monitor_state.v31.json') @{runner_version='3.1.3-03';artifact_version=$ArtifactVersion;stage='repair';status='FAILED';failure_reason='synthetic failure'}; return }
    Put-Json (Join-Path $work 'v31_quality_gate.json') @{version=$ArtifactVersion;expected=2;completed=2}
    if ($Kind -eq 'mixed') { Put-Json (Join-Path $work 'v31\primary\legacy.json') @{version='3.1.2'}; return }
    if ($Kind -eq 'legacy-compatible') { Put-Json (Join-Path $work 'v31\primary\legacy.json') @{version='3.1.2j'} }
    if ($Kind -eq 'reused') { Put-Json (Join-Path $run 'monitor_state.v31.json') @{runner_version='3.1.3';stage='source';status='REUSED'}; return }
    if ($Kind -in @('complete','legacy-compatible')) { New-Item -ItemType Directory -Force -Path (Join-Path $run 'output') | Out-Null; Set-Content -LiteralPath (Join-Path $run 'output\001_test.html') -Value 'done'; return }
}

$root = Join-Path ([IO.Path]::GetTempPath()) ('pact-monitor-' + [guid]::NewGuid())
$monitor = Join-Path $PSScriptRoot 'monitor_pipeline_v31.ps1'
Push-Location $PSScriptRoot
try { $actualArtifactVersion = (& python -c "from v31_common import VERSION; print(VERSION)" 2>$null | Select-Object -First 1).Trim() }
finally { Pop-Location }
if (-not $actualArtifactVersion) { throw 'Could not read v31_common.VERSION.' }
try {
    $cases = @{
        fresh='First missing: 001_test:source_analysis'; 'partial-audit'='Partial: 001_test primary audit/qwen_semantic 1/2'; 'partial-cross'='Partial: 001_test primary cross-verify/cross_verify_qwen 1/2'; reused='reused 1/1'; failed='Failure reason: synthetic failure'; stale='Stale complete: True'; mixed='Mixed-version artifacts: legacy.json'; inactive='INTERRUPTED \(owned process inactive\)'; complete='Aggregate: complete 1/1'; 'legacy-compatible'='Resume: READY'
    }
    foreach ($kind in $cases.Keys) {
        $caseRoot = Join-Path $root $kind; New-Fixture $caseRoot $kind $actualArtifactVersion
        $before = Get-ChildItem -LiteralPath $caseRoot -Recurse -File | Get-FileHash | Select-Object Path,Hash
        $text = (& $monitor -ProjectRoot $caseRoot -Start 1 -End 1 -Once 6>&1 | Out-String)
        Assert-Match $text $cases[$kind] "Monitor did not report $kind correctly."
        if ($kind -eq 'complete') {
            Assert-Match $text 'Mixed-version artifacts: none' 'Real v31_common.VERSION must be healthy.'
            Assert-Match $text 'Resume: READY' 'Healthy artifacts must allow resume.'
        }
        $after = Get-ChildItem -LiteralPath $caseRoot -Recurse -File | Get-FileHash | Select-Object Path,Hash
        if (Compare-Object $before $after) { throw "Monitor changed synthetic fixture $kind." }
    }
    Write-Host 'Pact v3.1 monitor synthetic fixture tests passed'
} finally { if (Test-Path -LiteralPath $root) { Remove-Item -LiteralPath $root -Recurse -Force } }
