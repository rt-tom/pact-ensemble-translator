$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
. (Join-Path $PSScriptRoot 'v31_runner_model_policy.ps1')

function Assert-True { param([bool]$Value,[string]$Message) if (-not $Value) { throw $Message } }
function Assert-False { param([bool]$Value,[string]$Message) if ($Value) { throw $Message } }

$root = Join-Path ([System.IO.Path]::GetTempPath()) ("pact-v31-policy-" + [guid]::NewGuid())
try {
    New-Item -ItemType Directory -Path (Join-Path $root 'one\v31\primary') -Force | Out-Null
    New-Item -ItemType Directory -Path (Join-Path $root 'two\v31\primary\cross_verify\qwen') -Force | Out-Null
    Set-Content -LiteralPath (Join-Path $root 'one\v31\primary\cross_verify_qwen.json') -Value '{}'

    Assert-False (Test-V31AggregateSetComplete $root @('one','two') 'v31\primary\cross_verify_qwen.json' $false) 'Partial cache must never skip a model.'
    Set-Content -LiteralPath (Join-Path $root 'two\v31\primary\cross_verify_qwen.json') -Value '{}'
    Assert-True (Test-V31AggregateSetComplete $root @('one','two') 'v31\primary\cross_verify_qwen.json' $false) 'All aggregates should skip model startup.'
    Assert-False (Test-V31AggregateSetComplete $root @('one','two') 'v31\primary\cross_verify_qwen.json' $true) 'Force must disable aggregate skip.'

    $exe = 'C:\llama-cpp\llama-server.exe'
    $args = @('-m','model.gguf','--port','8080')
    $signature = Get-V31ServerCommandSignature $exe 'Qwen' $args
    Assert-True ($signature -eq (Get-V31ServerCommandSignature $exe 'Qwen' $args)) 'Command signature must be stable.'
    Assert-False ($signature -eq (Get-V31ServerCommandSignature $exe 'Qwen' (@($args) + '--jinja'))) 'Command signature must cover arguments.'

    $metadata = [pscustomobject]@{
        pid=42; profile='Qwen'; executable=$exe; command_signature=$signature
        command_line='llama-server.exe -m model.gguf --port 8080'
        health_uri='http://127.0.0.1:8080/health'
    }
    $valid = @{ ProcessId=42; HasExited=$false; Metadata=$metadata; ExpectedProfile='Qwen'; ExpectedExecutable=$exe; ExpectedCommandSignature=$signature; ActualCommandLine=$metadata.command_line; HealthStatus='ok' }
    Assert-True (Test-V31OwnedServerIdentity @valid) 'Owned healthy same-profile server should be reusable.'
    foreach ($change in @(
        @{ProcessId=43}, @{HasExited=$true}, @{ExpectedProfile='GemmaVerify'},
        @{ExpectedCommandSignature='wrong'}, @{ActualCommandLine='foreign'}, @{HealthStatus='loading model'}
    )) {
        $case = $valid.Clone()
        foreach ($key in $change.Keys) { $case[$key] = $change[$key] }
        Assert-False (Test-V31OwnedServerIdentity @case) "Ownership mismatch $($change.Keys) must reject reuse."
    }

    $runner = Get-Content -LiteralPath (Join-Path $PSScriptRoot 'run_full_pipeline_v31.ps1') -Raw
    Assert-False ($runner -match 'Get-Process\s+llama-server[^\r\n]*Stop-Process') 'Runner must not stop foreign llama-server processes.'
    Assert-True ($runner -match 'Port 8080 is already served by an unowned endpoint') 'Runner must fail closed on a foreign endpoint.'
    Assert-False ($runner -match 'Start-LlamaServer GemmaTranslate\s*\r?\n\s*\$finalizeArgs') 'Model-free finalization must not start a model.'
    Assert-False ($runner -match 'Start-LlamaServer GemmaTranslate\s*\r?\n\s*if \(-not \$SkipPreflight\)') 'Runner must not start a model before a stage requires it.'
    Assert-True ($runner -match 'function Test-PrepareContextModelRequired') 'Prepare must explicitly declare when a chapter bible needs GemmaTranslate.'
    Assert-True ($runner -match 'if \(Test-PrepareContextModelRequired\)\s*\{\s*Start-LlamaServer GemmaTranslate') 'A clean run must start the owned GemmaTranslate server before model-required chapter-bible preparation.'
    Assert-True ($runner -match 'Test-Path \(Join-Path \(Join-Path \$WorkDir \$stem\) ''chapter_bible\.json''\)') 'Existing chapter bibles must keep preparation model-free on resume.'
    Assert-True ($runner -match 'v31_stage_protocol\.py') 'Runner must use the structured stage protocol.'
    Assert-True ($runner -match '\$probeExit -notin @\(20, 22\)') 'Only stable MODEL_REQUIRED exit codes may trigger model startup.'
    Assert-True ($runner -match '\$probeExit -eq 22.*--force') 'Invalid aggregate MODEL_REQUIRED must retry the stage instead of accepting the file.'
    Assert-True ($runner -match '\$auditScript = Join-Path \$PackageRoot ''v31_audit\.py''') 'Audit aggregate reuse must use a cache-only identity probe.'
    Assert-True ($runner -match "@Arguments '--cache-check'") 'Audit cache identity must be checked without model HTTP calls.'
    Assert-True ($runner -match '\$cacheExit -eq 0\)\s*\{\s*Write-MonitorState -Stage \$Label -Status ''REUSED''') 'A matching audit cache must remain model-free.'
    Assert-True ($runner -match '(?s)\$cacheExit -ne 20.*Start-LlamaServer \$Profile') 'Only an audit cache miss may start an owned server.'
    Assert-True ($runner -match 'Invoke-TranslationStage') 'Translation must be routed through the protocol, never a file-exists shortcut.'
    Assert-True ($runner -match '\$probeExit -eq 0\)\s*\{\s*Invoke-PythonStage -Label \$Label -Arguments \$Arguments -Outcome ''REUSED''') 'A complete translation cache must run model-free without starting GemmaTranslate.'
    Assert-True ($runner -match 'Stop-LlamaServer\s*\r?\n\s*\$unownedEndpoint') 'A profile switch must stop only the tracked owned server before startup.'
    Assert-True ($runner -match '\$ensemble\[''qwen_global_smoke''\].*max_tokens=5000') 'Only final Qwen global smoke must receive the 5K JSON output budget.'
    Write-Host 'Pact v3.1 runner model policy self-tests passed'
} finally {
    if (Test-Path -LiteralPath $root) { Remove-Item -LiteralPath $root -Recurse -Force }
}
