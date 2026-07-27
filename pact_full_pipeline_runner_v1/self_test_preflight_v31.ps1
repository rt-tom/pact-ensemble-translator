$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
. (Join-Path $PSScriptRoot 'v31_preflight_policy.ps1')

function Assert-V31 {
    param([bool]$Condition, [string]$Message)
    if (-not $Condition) { throw $Message }
}

function New-Sample {
    param([double]$Prompt, [double]$Generation, [double]$Acceptance = 0.40)
    return [pscustomobject]@{
        prompt_tps = $Prompt
        generation_tps = $Generation
        mtp_acceptance = $Acceptance
    }
}

$coldWarmup = @(New-Sample 220.37 12.06 0.434)
$healthyMeasured = @(
    (New-Sample 218.0 21.0 0.44),
    (New-Sample 222.0 24.0 0.46),
    (New-Sample 220.0 23.0 0.45)
)
$healthy = Get-V31PreflightSummary `
    -WarmupSamples $coldWarmup -Samples $healthyMeasured `
    -ExpectedWarmupCount 1 -ExpectedSampleCount 3 `
    -MinPromptTps 100 -MinGenerationTps 20
Assert-V31 ($healthy.prompt_tps -eq 220.0) 'Prompt median mismatch.'
Assert-V31 ($healthy.generation_tps -eq 23.0) 'Generation median mismatch.'
Assert-V31 ($healthy.mtp_acceptance -eq 0.45) 'Acceptance median mismatch.'
Assert-V31 $healthy.meets_thresholds 'Cold warm-up must not fail healthy measured samples.'
Assert-V31 ($healthy.status -eq 'pass' -and $healthy.execution_allowed) 'Healthy summary must pass.'

$slowMeasured = @(
    (New-Sample 216.0 11.0),
    (New-Sample 220.0 12.0),
    (New-Sample 218.0 13.0)
)
$advisory = Get-V31PreflightSummary `
    -WarmupSamples $coldWarmup -Samples $slowMeasured `
    -ExpectedWarmupCount 1 -ExpectedSampleCount 3 `
    -MinPromptTps 100 -MinGenerationTps 20
Assert-V31 (-not $advisory.meets_thresholds) 'Slow median must miss the threshold.'
Assert-V31 ($advisory.status -eq 'advisory') 'Slow valid samples must be advisory.'
Assert-V31 ($advisory.execution_allowed -and -not $advisory.blocking) 'Advisory must allow execution.'

$incompleteFailed = $false
try {
    $null = Get-V31PreflightSummary `
        -WarmupSamples $coldWarmup -Samples $slowMeasured[0..1] `
        -ExpectedWarmupCount 1 -ExpectedSampleCount 3 `
        -MinPromptTps 100 -MinGenerationTps 20
} catch { $incompleteFailed = $_.Exception.Message -like 'Expected 3 measured*' }
Assert-V31 $incompleteFailed 'Incomplete measurements must remain blocking.'

$invalidFailed = $false
try {
    $invalid = @((New-Sample 220 21), (New-Sample 220 0), (New-Sample 220 22))
    $null = Get-V31PreflightSummary `
        -WarmupSamples $coldWarmup -Samples $invalid `
        -ExpectedWarmupCount 1 -ExpectedSampleCount 3 `
        -MinPromptTps 100 -MinGenerationTps 20
} catch { $invalidFailed = $_.Exception.Message -like '*out-of-range generation_tps*' }
Assert-V31 $invalidFailed 'Invalid measurements must remain blocking.'

Write-Output 'Pact v3.1 preflight policy self-tests passed'
