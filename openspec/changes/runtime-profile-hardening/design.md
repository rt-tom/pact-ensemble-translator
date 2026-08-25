## Context

`load_runtime_config()` is already fail-closed for most `opencode_server` fields, but `_load_local()` forwards a permissive mapping into `LocalLlamaBackendConfig`; unknown fields are ignored and related model maps need not agree. The strict CLI loads a profile immediately before constructing a runtime, while local path and port checks currently live in lifecycle code and remote preflight contacts the endpoint. See `proposal.md` for motivation and `specs/runtime-profile-contract/spec.md` for the required behavior.

The existing checks are therefore layered rather than absent: remote backend preflight already performs endpoint health/provider checks before the first call, and local lifecycle code checks prerequisites before starting a server. This change adds a unified offline profile preflight before configured runs; it does not remove those later transport checks.

## Goals / Non-Goals

**Goals:**
- Make the config-file path deterministic, self-describing, and reject malformed local profiles before lifecycle construction.
- Provide one explicit host-local, offline admission path that resolves provider selections and prints only public configuration information.
- Keep the canonical local and remote examples accurate and portable enough to be copied to an intended execution host.

**Non-Goals:**
- Do not implement the simple book launcher or its command grammar.
- Do not alter the historical no-config local CLI, run identity compatibility, audit/markup semantics, runtime routing policy, or persistent artifacts.
- Do not start, stop, inspect, or reconfigure servers; perform a remote endpoint/provider preflight; make a network request; or change the advanced composite example.
- Do not change the media/RT execution policy or implement artifact transfer, lease, or snapshot protocols.

## Decisions

- **Separate structural, semantic, and host checks.** The runtime loader will first apply strict per-kind field allowlists and shape checks, then validate related local model mappings and local host/port values. A dedicated offline host preflight will run automatically before every configured run and will check filesystem paths, local port readiness, and required auth environment-variable presence. An explicit `--preflight` mode will run this check and exit. This gives profile authors a deterministic error without requiring a server or network. Reusing the normal remote backend preflight as this stage is rejected because it calls the configured endpoint; the existing remote transport preflight remains later in the run lifecycle.

- **Treat the canonical profile as data, not a replacement default.** `runtime_local.example.yaml` is the current config-form canonical local identity, but a no-config invocation continues through the historical constructor. The loader does not substitute either form for the other. This preserves existing journals and transport behavior while letting later simple mode select an explicit profile.

- **Validate local mappings as a closed supported set.** A local profile will explicitly define the supported local model keys and require each of `model_paths`, `model_names`, and `server_args` to describe the same nonempty keys; values are validated before the lifecycle can inspect them. This prevents an unused model path, a name that cannot be routed, or arguments for another model from being silently accepted. Permissive arbitrary keys are rejected because the runtime's role mapping only has an explicit Gemma/Qwen contract.

- **Use sanitized resolved descriptors for reporting.** The preflight will resolve the profile and any existing provider/model/reasoning selections exactly as the run path does, then produce public descriptor/policy fields and status results. The canonical remote profile explicitly carries all default role models, reasoning policy/mapping, timeout, budget, structured-output policy, and other identity-bearing values; omitted invocation overrides use those values. Credentials are represented only by variable names; their values are neither copied into descriptors nor output. The report includes the final identity so a future simple launcher can expose the same provenance before startup. Human-readable output is the default; a machine-readable JSON form is available for launcher automation.

- **Use case-insensitive globally unique aliases.** The registry will normalize aliases case-insensitively, reject any duplicate normalized alias while loading, and resolve bare aliases through the resulting global index. Existing `provider/alias` resolution remains supported for valid registries, so existing scripted callers retain their syntax; a malformed duplicate registry is rejected rather than allowing one caller to choose a different provider.

- **Keep remote example transport host-neutral.** The remote example will retain a transport endpoint field and env-variable references, but its comments will explain only durable operator obligations. Historic experiment notes, stale plan references, and assumptions about a particular machine are removed. The profile remains policy-bearing: identity-affecting values are explicit where the canonical remote policy requires them.

## Risks / Trade-offs

- [Stricter local validation rejects previously tolerated custom YAML] → Fail before execution with field-specific errors; preserve the no-config path and document the canonical contract.
- [Filesystem or port readiness differs between media and RT] → Preflight is intentionally run only on the host that will execute; no result is treated as portable host evidence.
- [Environment validation exposes a secret] → Emit only the declared environment-variable name and a present/absent status; never evaluate or serialize its value.
- [A new alias collides with a future registry entry] → Bare lookup fails closed; automation can keep using provider-qualified legacy syntax until the owner resolves the registry.
- [Example comments drift again] → Cover examples with loader/preflight tests and keep comments limited to current behavior rather than dated experimental history.

## Migration Plan

1. Add structural and cross-field validation with focused loader tests.
2. Add the offline preflight interface and tests that assert no runtime, network, or artifact side effects.
3. Refresh the canonical local/remote examples and provider registry tests; leave the composite example untouched.
4. Run focused runtime/CLI tests and strict OpenSpec validation. Owner review and approval precede any implementation; no pipeline is run.

Rollback removes the new preflight interface and restores the prior example files/loader behavior. No run artifacts or external state require migration.
