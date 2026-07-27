$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

function Assert-True { param([bool]$Value,[string]$Message) if (-not $Value) { throw $Message } }

function Get-TestLlamaServerProfile {
    $serverArgs = @('-m', 'model.gguf', '--host', '127.0.0.1', '--port', '8080')
    return $serverArgs
}

[string[]]$serverArgs = @(Get-TestLlamaServerProfile)
Assert-True ($serverArgs -is [string[]]) 'Server arguments must be a flat string array.'
Assert-True ($serverArgs.Count -eq 6) 'Server argument count changed unexpectedly.'
Assert-True ($serverArgs[0] -eq '-m' -and $serverArgs[-1] -eq '8080') 'Server argument order changed unexpectedly.'

$runner = Get-Content -LiteralPath (Join-Path $PSScriptRoot 'run_full_pipeline_v31.ps1') -Raw
Assert-True ($runner -match 'return\s+\$serverArgs') 'Runner profile helper must return the argument array directly.'
Assert-True (-not ($runner -match 'return\s+,\$serverArgs')) 'Runner profile helper must not wrap the argument array in an extra array.'
Assert-True ($runner -match '\[string\[\]\]\$serverArgs\s*=\s*@\(Get-LlamaServerProfile \$Profile\)') 'Runner must bind server arguments to a string array.'

Write-Host 'Pact v3.1 runner startup argument self-tests passed'
