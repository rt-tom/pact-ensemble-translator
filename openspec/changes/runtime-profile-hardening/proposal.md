## Why

The canonical local and remote runtime examples are not yet a dependable operator contract: local YAML accepts unknown or incoherent fields too late, remote configuration contains historical environment-specific commentary, and no offline check gives an operator the resolved identity, models, policy, paths, ports, and environment prerequisites before a run. This hardening must precede the simplified run command so it can select profiles without silently changing transport or quality policy.

## What Changes

- Define canonical `local_llama` and portable `opencode_server` profile contracts, retaining the historical no-config local CLI as a compatibility path.
- Make local profile loading fail closed on unsupported keys, malformed shapes, missing or incoherent model/path/name/server-argument relationships, and unsafe host/port settings.
- Add an offline, host-local runtime-profile preflight that runs by default before every configured run and reports the resolved sanitized profile, role/model bindings, effective policy, and identity; `--preflight` performs the same check and exits without running the pipeline. It validates local paths/ports and required environment-variable presence without starting a server, calling a provider, or writing run artifacts.
- Make provider aliases globally unambiguous and case-insensitive for future simple `--remote <translator>/<reviewer>` selection; any duplicate alias is a registry error, while explicit legacy provider-qualified resolution remains supported.
- Refresh the canonical local and remote examples and remove stale experimental or host-specific guidance. The remote canonical profile explicitly records all defaults, including role models and reasoning policy; optional CLI model/reasoning overrides use profile values when omitted. Leave the composite example advanced/test-only and do not update it in this change.

## Capabilities

### New Capabilities
- `runtime-profile-contract`: Fail-closed, portable runtime-profile loading, offline preflight, and globally unambiguous remote model alias resolution.

### Modified Capabilities
- None.

## Impact

- `pact_v4/runtime/runtime_config.py`, the strict-run CLI/profile loader and its tests.
- `configs/runtime_local.example.yaml`, `configs/runtime_remote.example.yaml`, and `configs/providers.yaml`; `runtime_composite.example.yaml` remains out of scope.
- No pipeline execution, model-server lifecycle operation, remote provider call, persistent artifact format, media/RT transport, or production policy change is included.
