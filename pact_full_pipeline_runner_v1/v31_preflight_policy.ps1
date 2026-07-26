function Get-V31FiniteSampleValue {
    param($Sample, [string]$PropertyName, [switch]$AllowZero)

    $property = $Sample.PSObject.Properties[$PropertyName]
    if ($null -eq $property -or $null -eq $property.Value) {
        throw "Preflight sample is missing $PropertyName"
    }
    try { $value = [double]$property.Value }
    catch { throw "Preflight sample has invalid ${PropertyName}: $($property.Value)" }
    if ([double]::IsNaN($value) -or [double]::IsInfinity($value)) {
        throw "Preflight sample has non-finite ${PropertyName}: $value"
    }
    if (($AllowZero -and $value -lt 0) -or (-not $AllowZero -and $value -le 0)) {
        throw "Preflight sample has out-of-range ${PropertyName}: $value"
    }
    return $value
}

function Get-V31Median {
    param([object[]]$Values)

    $items = @($Values | ForEach-Object { [double]$_ } | Sort-Object)
    if ($items.Count -eq 0) { throw 'Cannot calculate a median without values.' }
    $middle = [math]::Floor($items.Count / 2)
    if ($items.Count % 2) { return [double]$items[$middle] }
    return [double](($items[$middle - 1] + $items[$middle]) / 2.0)
}

function Get-V31PreflightSummary {
    param(
        [object[]]$WarmupSamples,
        [object[]]$Samples,
        [int]$ExpectedWarmupCount,
        [int]$ExpectedSampleCount,
        [double]$MinPromptTps,
        [double]$MinGenerationTps
    )

    $warmups = @($WarmupSamples)
    $measured = @($Samples)
    if ($warmups.Count -ne $ExpectedWarmupCount) {
        throw "Expected $ExpectedWarmupCount preflight warm-up sample(s), got $($warmups.Count)"
    }
    if ($measured.Count -ne $ExpectedSampleCount) {
        throw "Expected $ExpectedSampleCount measured preflight sample(s), got $($measured.Count)"
    }
    if ($ExpectedWarmupCount -lt 1 -or $ExpectedSampleCount -lt 3) {
        throw 'Preflight requires at least one warm-up and three measured samples.'
    }

    foreach ($sample in @($warmups + $measured)) {
        $null = Get-V31FiniteSampleValue -Sample $sample -PropertyName 'prompt_tps'
        $null = Get-V31FiniteSampleValue -Sample $sample -PropertyName 'generation_tps'
    }
    $promptValues = @($measured | ForEach-Object { Get-V31FiniteSampleValue -Sample $_ -PropertyName 'prompt_tps' })
    $generationValues = @($measured | ForEach-Object { Get-V31FiniteSampleValue -Sample $_ -PropertyName 'generation_tps' })
    $acceptanceValues = @(
        $measured | Where-Object { $null -ne $_.PSObject.Properties['mtp_acceptance'] -and $null -ne $_.mtp_acceptance } |
            ForEach-Object { Get-V31FiniteSampleValue -Sample $_ -PropertyName 'mtp_acceptance' -AllowZero }
    )
    foreach ($value in $acceptanceValues) {
        if ($value -gt 1.0) { throw "Preflight sample has out-of-range mtp_acceptance: $value" }
    }

    $promptMedian = Get-V31Median $promptValues
    $generationMedian = Get-V31Median $generationValues
    $acceptanceMedian = if ($acceptanceValues.Count) { Get-V31Median $acceptanceValues } else { $null }
    $meetsThresholds = $promptMedian -ge $MinPromptTps -and $generationMedian -ge $MinGenerationTps
    return [pscustomobject][ordered]@{
        policy = 'median_advisory'
        status = if ($meetsThresholds) { 'pass' } else { 'advisory' }
        blocking = $false
        execution_allowed = $true
        warmup_samples = $warmups
        samples = $measured
        sample_count = $measured.Count
        prompt_tps = $promptMedian
        generation_tps = $generationMedian
        mtp_acceptance = $acceptanceMedian
        thresholds = [ordered]@{
            min_prompt_tps = $MinPromptTps
            min_generation_tps = $MinGenerationTps
        }
        meets_thresholds = $meetsThresholds
    }
}
