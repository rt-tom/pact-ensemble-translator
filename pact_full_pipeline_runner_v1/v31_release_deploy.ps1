[CmdletBinding(DefaultParameterSetName = 'Verify')]
param(
    [Parameter(Mandatory, ParameterSetName = 'Manifest')][switch]$NewReleaseManifest,
    [Parameter(Mandatory, ParameterSetName = 'Deploy')][switch]$Deploy,
    [Parameter(Mandatory, ParameterSetName = 'Rollback')][switch]$Rollback,
    [Parameter(Mandatory)][string]$ProjectRoot,
    [Parameter(Mandatory)][string]$ReleaseRef,
    [string]$BaseReleaseRef,
    [string]$MigrationPlanPath,
    [string]$ManifestPath,
    [string]$BackupRoot,
    [switch]$SkipSmokeTest
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

$ReleaseManifestSchema = 'pact-v31-release-manifest/v1'
$DeploymentProvenanceSchema = 'pact-v31-installed-provenance/v1'

function Invoke-Git {
    param([string]$Root, [string[]]$Arguments)
    $result = & git -C $Root @Arguments 2>&1
    if ($LASTEXITCODE -ne 0) { throw "git $($Arguments -join ' ') failed: $result" }
    return @($result)
}

function Get-ProjectRoot {
    param([string]$Path)
    $resolved = (Resolve-Path -LiteralPath $Path -ErrorAction Stop).Path
    $root = (Invoke-Git -Root $resolved -Arguments @('rev-parse', '--show-toplevel') | Select-Object -Last 1).Trim()
    $normalizedResolved = [IO.Path]::GetFullPath($resolved).TrimEnd('\\')
    $normalizedRoot = [IO.Path]::GetFullPath($root).TrimEnd('\\')
    if (-not [System.StringComparer]::OrdinalIgnoreCase.Equals($normalizedResolved, $normalizedRoot)) {
        throw "ProjectRoot must be the active Git worktree root, not a parent or nested path: $resolved"
    }
    return $root
}

function Get-TagCommit {
    param([string]$Root, [string]$Ref)
    $tagType = (Invoke-Git -Root $Root -Arguments @('cat-file', '-t', "$Ref`^{tag}") | Select-Object -Last 1).Trim()
    if ($tagType -ne 'tag') { throw "ReleaseRef must name an annotated Git tag: $Ref" }
    return (Invoke-Git -Root $Root -Arguments @('rev-parse', "$Ref`^{commit}") | Select-Object -Last 1).Trim()
}

function Get-Hash {
    param([string]$Path)
    $bytes = [IO.File]::ReadAllBytes($Path)
    # Git checkout may materialize CRLF on Windows. Release hashes are of the
    # canonical Git text; normalize only line endings for the active-path check.
    $normalized = [Text.Encoding]::UTF8.GetBytes(([Text.Encoding]::UTF8.GetString($bytes) -replace "`r`n", "`n"))
    return ([Convert]::ToHexString([Security.Cryptography.SHA256]::HashData($normalized))).ToLowerInvariant()
}

function Read-JsonFile {
    param([string]$Path)
    $bytes = [System.IO.File]::ReadAllBytes($Path)
    if ($bytes.Length -ge 3 -and $bytes[0] -eq 0xEF -and $bytes[1] -eq 0xBB -and $bytes[2] -eq 0xBF) {
        throw "BOM is not permitted in release control file: $Path"
    }
    return [System.Text.Encoding]::UTF8.GetString($bytes) | ConvertFrom-Json
}

function Write-JsonNoBom {
    param([string]$Path, $Value)
    $json = $Value | ConvertTo-Json -Depth 20
    [System.IO.File]::WriteAllText($Path, $json + "`n", [System.Text.UTF8Encoding]::new($false))
}

function Get-ReleaseVersion {
    param([string]$Root, [string]$Commit = '')
    if ($Commit) {
        $common = "${Commit}:pact_full_pipeline_runner_v1/v31_common.py"
        $text = (& git -C $Root show $common) -join "`n"
        if ($LASTEXITCODE -ne 0) { throw "Version identity source is absent from $Commit" }
    } else {
        $common = Join-Path $Root 'pact_full_pipeline_runner_v1\v31_common.py'
        if (-not (Test-Path -LiteralPath $common)) { throw "Version identity source not found: $common" }
        $text = [System.IO.File]::ReadAllText($common, [System.Text.UTF8Encoding]::new($false))
    }
    $artifact = [regex]::Match($text, '(?m)^ARTIFACT_VERSION\s*=\s*(?<value>VERSION|["''][^"'']+["''])\s*$')
    if (-not $artifact.Success) { throw "Could not resolve ARTIFACT_VERSION from $common" }
    $value = $artifact.Groups['value'].Value.Trim("'`"")
    if ($value -eq 'VERSION') {
        $version = [regex]::Match($text, '(?m)^VERSION\s*=\s*["''](?<version>[^"'']+)["'']\s*$')
        if (-not $version.Success) { throw "ARTIFACT_VERSION aliases missing VERSION in $common" }
        $value = $version.Groups['version'].Value
    }
    if ($value -match '(?i)(rc|build)') { throw "Semantic artifact version must not contain RC/build label: $value" }
    return $value
}

function Get-TrackedFilesAtRef {
    param([string]$Root, [string]$Commit)
    return @(Invoke-Git -Root $Root -Arguments @('ls-tree', '-r', '--name-only', $Commit) | Where-Object { $_ })
}

function Get-AuthoritativeSchemas {
    param([string]$Root, [string]$Commit)
    $result = [ordered]@{}
    foreach ($relative in Get-TrackedFilesAtRef $Root $Commit) {
        if ($relative -notmatch '\.(py|ps1)$') { continue }
        $text = (& git -C $Root show "${Commit}:$relative") -join "`n"
        if ($LASTEXITCODE -ne 0) { throw "Cannot inspect schema declarations in $relative" }
        foreach ($match in [regex]::Matches($text, '(?m)^(?:\$)?(?<name>[A-Z][A-Z0-9_]*SCHEMA(?:_VERSION)?)\s*=\s*["''](?<value>[^"'']+)["'']\s*$')) {
            $result["$relative::$($match.Groups['name'].Value)"] = $match.Groups['value'].Value
        }
    }
    return $result
}

function Get-MigrationPlan { param([string]$Path)
    if (-not $Path -or -not (Test-Path -LiteralPath $Path)) { return @() }
    $plan = Read-JsonFile $Path
    if ($plan -is [array]) { return @($plan) }
    return @($plan.migrations)
}

function Assert-Migrations {
    param($BaseSchemas, $TargetSchemas, $Migrations)
    $migrationItems = @($Migrations | Where-Object { $null -ne $_ })
    $changed = @()
    foreach ($key in @($BaseSchemas.Keys + $TargetSchemas.Keys | Sort-Object -Unique)) {
        if ($BaseSchemas[$key] -ne $TargetSchemas[$key]) { $changed += $key }
    }
    if (-not $changed.Count) {
        if ($migrationItems.Count) { throw 'Migrations are forbidden when no authoritative schema changed.' }
        return [ordered]@{ schema_changes=$false; changed=@() }
    }
    if (-not $migrationItems.Count) { throw 'Authoritative schema changed; approved non-empty migrations list is required.' }
    foreach ($migration in $migrationItems) {
        foreach ($field in @('source_schema','target_schema','affected_artifacts','migration_tool','backward_compatibility_policy','rollback_implications','approval_provenance')) {
            if ($null -eq $migration.$field -or -not [string]$migration.$field) { throw "Migration lacks required $field" }
        }
        if (-not (@($BaseSchemas.Values) -contains [string]$migration.source_schema) -or -not (@($TargetSchemas.Values) -contains [string]$migration.target_schema)) { throw 'Migration references unknown source or target schema.' }
        foreach ($artifact in @($migration.affected_artifacts)) { if (-not $TargetSchemas.Contains($artifact)) { throw "Migration references unknown affected artifact: $artifact" } }
        if ([string]$migration.backward_compatibility_policy -eq 'irreversible' -and -not [string]$migration.rollback_implications.blocker_approval) { throw 'Irreversible migration requires rollback blocker approval.' }
    }
    return [ordered]@{ schema_changes=$true; changed=@($changed) }
}

function Get-GitBlobSha256 {
    param([string]$Root, [string]$Commit, [string]$RelativePath)
    $psi = [Diagnostics.ProcessStartInfo]::new()
    $psi.FileName = 'git'
    $psi.WorkingDirectory = $Root
    $psi.UseShellExecute = $false
    $psi.RedirectStandardOutput = $true
    $psi.RedirectStandardError = $true
    [void]$psi.ArgumentList.Add('cat-file')
    [void]$psi.ArgumentList.Add('blob')
    [void]$psi.ArgumentList.Add("${Commit}:$RelativePath")
    $process = [Diagnostics.Process]::Start($psi)
    $buffer = [IO.MemoryStream]::new()
    $process.StandardOutput.BaseStream.CopyTo($buffer)
    $stderr = $process.StandardError.ReadToEnd()
    $process.WaitForExit()
    if ($process.ExitCode -ne 0) { throw "Cannot hash $RelativePath from ${Commit}: $stderr" }
    return ([Convert]::ToHexString([Security.Cryptography.SHA256]::HashData($buffer.ToArray()))).ToLowerInvariant()
}

function New-Manifest {
    param([string]$Root, [string]$Ref, [string]$BaseRef, [string]$Path, [string]$PlanPath)
    $commit = Get-TagCommit $Root $Ref
    if (-not $BaseRef) { throw 'BaseReleaseRef is required: schema comparison must use a base annotated release.' }
    $baseCommit = Get-TagCommit $Root $BaseRef
    $baseSchemas = Get-AuthoritativeSchemas $Root $baseCommit
    $targetSchemas = Get-AuthoritativeSchemas $Root $commit
    $migrations = Get-MigrationPlan $PlanPath
    $schemaState = Assert-Migrations -BaseSchemas $baseSchemas -TargetSchemas $targetSchemas -Migrations $migrations
    $files = foreach ($relative in Get-TrackedFilesAtRef $Root $commit) {
        [ordered]@{ path = $relative; sha256 = Get-GitBlobSha256 -Root $Root -Commit $commit -RelativePath $relative }
    }
    $manifest = [ordered]@{
        schema = $ReleaseManifestSchema
        release_tag = $Ref
        base_release_tag = $BaseRef
        commit = $commit
        version = Get-ReleaseVersion $Root $commit
        files = @($files)
        schemas = $targetSchemas
        schema_changes = $schemaState.schema_changes
        schema_changes_from_base = $schemaState.changed
        migrations = @($migrations)
    }
    Write-JsonNoBom -Path $Path -Value $manifest
    return $manifest
}

function Assert-CleanTrackedTree { param([string]$Root)
    $dirty = @(Invoke-Git -Root $Root -Arguments @('status', '--porcelain', '--untracked-files=no'))
    if ($dirty.Count) { throw "Active project has tracked changes; deployment is unsafe: $($dirty -join '; ')" }
}

function Assert-Manifest {
    param([string]$Root, [string]$Ref, [string]$Path)
    $manifest = Read-JsonFile $Path
    if ($manifest.schema -ne $ReleaseManifestSchema) { throw "Unsupported release manifest schema: $($manifest.schema)" }
    if ($manifest.release_tag -ne $Ref) { throw "Manifest tag mismatch: expected $Ref, got $($manifest.release_tag)" }
    $commit = Get-TagCommit $Root $Ref
    if ($manifest.commit -ne $commit) { throw "Manifest commit does not resolve from exact tag $Ref" }
    if ($manifest.version -ne (Get-ReleaseVersion $Root $commit)) { throw 'Manifest version differs from the tagged version identity.' }
    $actual = @(Get-TrackedFilesAtRef $Root $commit)
    $listed = @($manifest.files | ForEach-Object { [string]$_.path })
    if (@(Compare-Object $actual $listed).Count) { throw 'Manifest file set differs from exact tagged tree.' }
    foreach ($item in @($manifest.files)) {
        if ($item.sha256 -ne (Get-GitBlobSha256 -Root $Root -Commit $commit -RelativePath ([string]$item.path))) {
            throw "Manifest hash mismatch for $($item.path)"
        }
    }
    $baseCommit = Get-TagCommit $Root ([string]$manifest.base_release_tag)
    $state = Assert-Migrations -BaseSchemas (Get-AuthoritativeSchemas $Root $baseCommit) -TargetSchemas (Get-AuthoritativeSchemas $Root $commit) -Migrations @($manifest.migrations)
    if ([bool]$manifest.schema_changes -ne [bool]$state.schema_changes) { throw 'Manifest schema_changes contradicts actual base release diff.' }
    return $manifest
}

function Save-Backup {
    param([string]$Root, [string]$OldCommit, [string]$NewCommit, [string]$Destination)
    New-Item -ItemType Directory -Force -Path $Destination | Out-Null
    $changed = @(Invoke-Git -Root $Root -Arguments @('diff', '--name-only', $OldCommit, $NewCommit))
    foreach ($relative in $changed) {
        $source = Join-Path $Root $relative
        if (Test-Path -LiteralPath $source -PathType Leaf) {
            $target = Join-Path $Destination $relative
            New-Item -ItemType Directory -Force -Path (Split-Path -Parent $target) | Out-Null
            Copy-Item -LiteralPath $source -Destination $target -Force
        }
    }
    Write-JsonNoBom -Path (Join-Path $Destination 'backup_manifest.json') -Value ([ordered]@{ old_commit=$OldCommit; new_commit=$NewCommit; files=@($changed) })
}

function Assert-InstalledFiles {
    param([string]$Root, $Manifest)
    foreach ($item in @($Manifest.files)) {
        $path = Join-Path $Root ([string]$item.path)
        if (-not (Test-Path -LiteralPath $path -PathType Leaf)) { throw "Installed file is absent from active path: $($item.path)" }
        if ((Get-Hash $path) -ne $item.sha256) { throw "Installed hash differs from release manifest: $($item.path)" }
    }
}

function Get-CacheSnapshot {
    param([string]$Root)
    $cache = Join-Path $Root 'pipeline_runs'
    if (-not (Test-Path -LiteralPath $cache)) { return @() }
    return @(Get-ChildItem -LiteralPath $cache -Recurse -File | ForEach-Object {
        [ordered]@{ path=$_.FullName.Substring($cache.Length).TrimStart('\\'); length=$_.Length; sha256=Get-Hash $_.FullName; last_write_utc=$_.LastWriteTimeUtc.ToString('o') }
    })
}

function Assert-CachePreserved { param($Before, $After)
    $a = $Before | ConvertTo-Json -Depth 5 -Compress
    $b = $After | ConvertTo-Json -Depth 5 -Compress
    if ($a -ne $b) { throw 'Cache preservation check failed; deployment must not alter pipeline_runs.' }
}

function Invoke-OfflineChecks { param([string]$Root, [string]$ExpectedVersion, [switch]$SkipSmoke)
    $pythonFiles = @(Get-ChildItem -LiteralPath (Join-Path $Root 'pact_full_pipeline_runner_v1') -Filter '*.py' -File | Select-Object -ExpandProperty FullName) + (Join-Path $Root 'pact_translate_v3.py')
    & py -m py_compile @pythonFiles
    if ($LASTEXITCODE -ne 0) { throw 'Python compilation failed.' }
    $parseErrors = $null
    [void][System.Management.Automation.Language.Parser]::ParseFile((Join-Path $Root 'pact_full_pipeline_runner_v1\run_full_pipeline_v31.ps1'), [ref]$null, [ref]$parseErrors)
    if ($parseErrors.Count) { throw "Runner PowerShell AST validation failed: $parseErrors" }
    if (-not $SkipSmoke) {
        & py (Join-Path $Root 'pact_translate_v3.py') --version
        if ($LASTEXITCODE -ne 0) { throw 'Installed smoke test (--version) failed.' }
        $runtimeVersion = (& py -c "import sys; sys.path.insert(0, r'$Root\\pact_full_pipeline_runner_v1'); from v31_common import ARTIFACT_VERSION; print(ARTIFACT_VERSION)" | Select-Object -Last 1).Trim()
        if ($LASTEXITCODE -ne 0 -or $runtimeVersion -ne $ExpectedVersion) { throw "Active semantic version mismatch: expected $ExpectedVersion, got $runtimeVersion" }
    }
}

function Write-Provenance {
    param([string]$Root, $Manifest, [string]$PreviousCommit, [string]$Backup)
    $path = Join-Path $Root 'deployment_provenance.v31.json'
    Write-JsonNoBom -Path $path -Value ([ordered]@{ schema=$DeploymentProvenanceSchema; tag=$Manifest.release_tag; commit=$Manifest.commit; version=$Manifest.version; previous_commit=$PreviousCommit; backup=$Backup; migrations=@($Manifest.migrations); rollback_implications=@($Manifest.migrations | ForEach-Object { $_.rollback_implications }); installed_at=(Get-Date).ToUniversalTime().ToString('o') })
    $provenance = Read-JsonFile $path
    if ($provenance.commit -ne $Manifest.commit -or $provenance.version -ne $Manifest.version) { throw 'Installed provenance read-back failed.' }
}

$root = Get-ProjectRoot $ProjectRoot
if (-not $ManifestPath) { $ManifestPath = Join-Path $root 'release_manifest.v31.json' }

if ($NewReleaseManifest) { New-Manifest -Root $root -Ref $ReleaseRef -BaseRef $BaseReleaseRef -Path $ManifestPath -PlanPath $MigrationPlanPath | ConvertTo-Json -Depth 8; exit 0 }

$manifest = Assert-Manifest -Root $root -Ref $ReleaseRef -Path $ManifestPath
if (-not $Deploy -and -not $Rollback) { Write-Output 'Release manifest and active path verified.'; exit 0 }

Assert-CleanTrackedTree $root
$current = (Invoke-Git -Root $root -Arguments @('rev-parse', 'HEAD') | Select-Object -Last 1).Trim()
$target = [string]$manifest.commit
& git -C $root merge-base --is-ancestor $current $target
$isForward = $LASTEXITCODE -eq 0
& git -C $root merge-base --is-ancestor $target $current
$isRollback = $LASTEXITCODE -eq 0
if ($Deploy -and -not $isForward) { throw "Refusing non-fast-forward deployment: $current -> $target" }
if ($Rollback -and -not $isRollback) { throw "Rollback target is not an ancestor of active HEAD: $target" }
if (-not $BackupRoot) { $BackupRoot = Join-Path $root ("deployment_backups\\{0}_{1}" -f $manifest.version, (Get-Date -Format 'yyyyMMdd_HHmmss')) }
Save-Backup -Root $root -OldCommit $current -NewCommit $target -Destination $BackupRoot
$cacheBefore = Get-CacheSnapshot $root
if ($Deploy) { Invoke-Git -Root $root -Arguments @('merge', '--ff-only', $ReleaseRef) | Out-Null }
else { Invoke-Git -Root $root -Arguments @('revert', '--no-edit', "$target..$current") | Out-Null }
Invoke-OfflineChecks -Root $root -ExpectedVersion ([string]$manifest.version) -SkipSmoke:$SkipSmokeTest
Assert-CleanTrackedTree $root
Assert-InstalledFiles -Root $root -Manifest $manifest
Assert-CachePreserved -Before $cacheBefore -After (Get-CacheSnapshot $root)
Write-Provenance -Root $root -Manifest $manifest -PreviousCommit $current -Backup $BackupRoot
Write-Output "Deployment complete: $current -> $target"
