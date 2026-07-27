function Get-V31ServerCommandSignature {
    param(
        [string]$Executable,
        [string]$Profile,
        [string[]]$Arguments
    )
    $payload = @($Executable, $Profile) + @($Arguments)
    $bytes = [System.Text.Encoding]::UTF8.GetBytes(($payload -join "`n"))
    $hash = [System.Security.Cryptography.SHA256]::Create()
    try { return ([BitConverter]::ToString($hash.ComputeHash($bytes)) -replace '-', '').ToLowerInvariant() }
    finally { $hash.Dispose() }
}

function Test-V31AggregateSetComplete {
    param(
        [string]$WorkDir,
        [string[]]$ChapterStems,
        [string]$RelativePath,
        [bool]$Force
    )
    if ($Force -or $ChapterStems.Count -eq 0) { return $false }
    foreach ($stem in $ChapterStems) {
        if (-not (Test-Path -LiteralPath (Join-Path (Join-Path $WorkDir $stem) $RelativePath) -PathType Leaf)) {
            return $false
        }
    }
    return $true
}

function Test-V31OwnedServerIdentity {
    param(
        [int]$ProcessId,
        [bool]$HasExited,
        $Metadata,
        [string]$ExpectedProfile,
        [string]$ExpectedExecutable,
        [string]$ExpectedCommandSignature,
        [string]$ActualCommandLine,
        [string]$HealthStatus
    )
    if ($HasExited -or $null -eq $Metadata) { return $false }
    if ([int]$Metadata.pid -ne $ProcessId) { return $false }
    if ([string]$Metadata.profile -ne $ExpectedProfile) { return $false }
    if ([string]$Metadata.executable -ne $ExpectedExecutable) { return $false }
    if ([string]$Metadata.command_signature -ne $ExpectedCommandSignature) { return $false }
    if ([string]::IsNullOrWhiteSpace([string]$Metadata.command_line)) { return $false }
    if ([string]$Metadata.command_line -ne $ActualCommandLine) { return $false }
    if ([string]$Metadata.health_uri -ne 'http://127.0.0.1:8080/health') { return $false }
    return $HealthStatus -in @('ok', 'no slot available')
}
