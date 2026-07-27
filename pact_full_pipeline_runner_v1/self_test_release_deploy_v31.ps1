$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

function Assert-V31Release([bool]$Condition, [string]$Message) { if (-not $Condition) { throw $Message } }

$script = Join-Path $PSScriptRoot 'v31_release_deploy.ps1'
$temp = Join-Path ([IO.Path]::GetTempPath()) ("pact-release-test-" + [guid]::NewGuid())
try {
    New-Item -ItemType Directory -Force -Path $temp | Out-Null
    & git init -q $temp
    & git -C $temp config user.email test@example.invalid
    & git -C $temp config user.name 'Pact Test'
    New-Item -ItemType Directory -Force -Path (Join-Path $temp 'pact_full_pipeline_runner_v1') | Out-Null
    [IO.File]::WriteAllText((Join-Path $temp 'pact_translate_v3.py'), "print('ok')`n", [Text.UTF8Encoding]::new($false))
    [IO.File]::WriteAllText((Join-Path $temp 'pact_full_pipeline_runner_v1\v31_common.py'), "VERSION = '3.1.3'`nARTIFACT_VERSION = VERSION`nTEST_SCHEMA = 'test/v1'`n", [Text.UTF8Encoding]::new($false))
    [IO.File]::WriteAllText((Join-Path $temp 'pact_full_pipeline_runner_v1\run_full_pipeline_v31.ps1'), "Write-Output 'ok'`n", [Text.UTF8Encoding]::new($false))
    & git -C $temp add .; & git -C $temp commit -qm base; & git -C $temp tag -a v3.1.3-base -m base
    [IO.File]::AppendAllText((Join-Path $temp 'pact_translate_v3.py'), "# release change`n", [Text.UTF8Encoding]::new($false))
    [IO.File]::AppendAllText((Join-Path $temp 'pact_full_pipeline_runner_v1\v31_common.py'), "TEST_SCHEMA = 'test/v2'`n", [Text.UTF8Encoding]::new($false))
    & git -C $temp add .; & git -C $temp commit -qm release; & git -C $temp tag -a v3.1.3-test -m test
    $manifest = Join-Path $temp 'release_manifest.v31.json'
    $failed = $false; try { & $script -NewReleaseManifest -ProjectRoot $temp -ReleaseRef v3.1.3-test -BaseReleaseRef v3.1.3-base -ManifestPath $manifest | Out-Null } catch { $failed = $true }
    Assert-V31Release $failed 'Schema change without migrations must fail.'
    $planPath = Join-Path $temp 'migrations.json'
    $migration = @{ source_schema='test/v1'; target_schema='test/v2'; affected_artifacts=@('pact_full_pipeline_runner_v1/v31_common.py::TEST_SCHEMA'); migration_tool='test-migrate'; backward_compatibility_policy='reversible'; rollback_implications=@{plan='revert'}; approval_provenance='test approval' }
    $unknown = $migration.Clone(); $unknown.affected_artifacts=@('unknown::SCHEMA')
    [IO.File]::WriteAllText($planPath, (@{migrations=@($unknown)} | ConvertTo-Json -Depth 8), [Text.UTF8Encoding]::new($false))
    $failed = $false; try { & $script -NewReleaseManifest -ProjectRoot $temp -ReleaseRef v3.1.3-test -BaseReleaseRef v3.1.3-base -MigrationPlanPath $planPath -ManifestPath $manifest | Out-Null } catch { $failed = $true }
    Assert-V31Release $failed 'Migration with unknown artifact must fail.'
    $irreversible = $migration.Clone(); $irreversible.backward_compatibility_policy='irreversible'; $irreversible.rollback_implications=@{plan='blocked'}
    [IO.File]::WriteAllText($planPath, (@{migrations=@($irreversible)} | ConvertTo-Json -Depth 8), [Text.UTF8Encoding]::new($false))
    $failed = $false; try { & $script -NewReleaseManifest -ProjectRoot $temp -ReleaseRef v3.1.3-test -BaseReleaseRef v3.1.3-base -MigrationPlanPath $planPath -ManifestPath $manifest | Out-Null } catch { $failed = $true }
    Assert-V31Release $failed 'Irreversible migration without blocker approval must fail.'
    [IO.File]::WriteAllText($planPath, (@{migrations=@($migration)} | ConvertTo-Json -Depth 8), [Text.UTF8Encoding]::new($false))
    & $script -NewReleaseManifest -ProjectRoot $temp -ReleaseRef v3.1.3-test -BaseReleaseRef v3.1.3-base -MigrationPlanPath $planPath -ManifestPath $manifest | Out-Null
    $falseFlag = Get-Content -LiteralPath $manifest -Raw | ConvertFrom-Json; $falseFlag.schema_changes = $false
    [IO.File]::WriteAllText($manifest, ($falseFlag | ConvertTo-Json -Depth 10), [Text.UTF8Encoding]::new($false))
    $failed = $false; try { & $script -ProjectRoot $temp -ReleaseRef v3.1.3-test -ManifestPath $manifest | Out-Null } catch { $failed = $true }
    Assert-V31Release $failed 'False schema_changes flag must not hide an actual schema diff.'
    & $script -NewReleaseManifest -ProjectRoot $temp -ReleaseRef v3.1.3-test -BaseReleaseRef v3.1.3-base -MigrationPlanPath $planPath -ManifestPath $manifest | Out-Null
    Assert-V31Release (Test-Path $manifest) 'Manifest was not created.'
    $raw = [IO.File]::ReadAllBytes($manifest)
    Assert-V31Release (-not ($raw[0] -eq 0xEF -and $raw[1] -eq 0xBB -and $raw[2] -eq 0xBF)) 'Manifest contains a BOM.'
    New-Item -ItemType Directory -Force -Path (Join-Path $temp 'not-active') | Out-Null
    $failed = $false; try { & $script -ProjectRoot (Join-Path $temp 'not-active') -ReleaseRef v3.1.3-test -ManifestPath $manifest | Out-Null } catch { $failed = $true }
    Assert-V31Release $failed 'Nested non-active path must fail.'
    [IO.File]::WriteAllBytes($manifest, [byte[]](@(0xEF,0xBB,0xBF) + $raw))
    $failed = $false; try { & $script -ProjectRoot $temp -ReleaseRef v3.1.3-test -ManifestPath $manifest | Out-Null } catch { $failed = $true }
    Assert-V31Release $failed 'BOM manifest must fail.'
    & $script -NewReleaseManifest -ProjectRoot $temp -ReleaseRef v3.1.3-test -BaseReleaseRef v3.1.3-base -MigrationPlanPath $planPath -ManifestPath $manifest | Out-Null
    [IO.File]::WriteAllText((Join-Path $temp 'PATCH_RELEASE_INSTALLED.json'), '{"marker":true}', [Text.UTF8Encoding]::new($false))
    $failed = $false; try { & $script -ProjectRoot $temp -ReleaseRef v3.1.3-test -ManifestPath (Join-Path $temp 'missing_manifest.json') | Out-Null } catch { $failed = $true }
    Assert-V31Release $failed 'Marker-only validation must fail.'
    & git -C $temp checkout -q v3.1.3-base
    & $script -ProjectRoot $temp -ReleaseRef v3.1.3-test -ManifestPath $manifest -Deploy | Out-Null
    Assert-V31Release ((& git -C $temp describe --exact-match --tags) -eq 'v3.1.3-test') 'Fast-forward deploy did not activate exact tag.'
    Assert-V31Release ((Get-Content (Join-Path $temp 'deployment_provenance.v31.json') -Raw | ConvertFrom-Json).rollback_implications.Count -eq 1) 'Rollback plan must include migration implications.'
    & $script -ProjectRoot $temp -ReleaseRef v3.1.3-base -BaseReleaseRef v3.1.3-base -NewReleaseManifest -ManifestPath (Join-Path $temp 'rollback_manifest.v31.json') | Out-Null
    & $script -ProjectRoot $temp -ReleaseRef v3.1.3-base -ManifestPath (Join-Path $temp 'rollback_manifest.v31.json') -Rollback | Out-Null
    Assert-V31Release ((Get-Content (Join-Path $temp 'pact_translate_v3.py') -Raw) -notmatch 'release change') 'Rollback did not restore tagged content.'
    $tampered = Get-Content -LiteralPath $manifest -Raw | ConvertFrom-Json
    $tampered.files[0].sha256 = '0' * 64
    [IO.File]::WriteAllText($manifest, ($tampered | ConvertTo-Json -Depth 10), [Text.UTF8Encoding]::new($false))
    $failed = $false; try { & $script -ProjectRoot $temp -ReleaseRef v3.1.3-test -ManifestPath $manifest | Out-Null } catch { $failed = $true }
    Assert-V31Release $failed 'Tampered manifest must fail.'
    Write-Output 'Pact v3.1 release deployment self-tests passed'
} finally { if (Test-Path $temp) { Remove-Item -LiteralPath $temp -Recurse -Force } }
