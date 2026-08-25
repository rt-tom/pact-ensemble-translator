## 1. Fail-closed runtime profile loading

- [x] 1.1 Define and implement the `local_llama` field allowlist and scalar/mapping shape validation in the runtime-config loader; verify focused loader tests reject every unsupported top-level field and malformed local mapping before lifecycle construction.
- [x] 1.2 Implement local cross-field validation for the supported Gemma/Qwen model-key set, executable/path/name values, string-only server arguments, and local host/port range; verify focused tests reject missing, extra, mismatched, and invalid values while the canonical local profile loads.
- [x] 1.3 Preserve the historical no-config local strict-CLI construction unchanged; verify its existing identity/transport characterization and existing local reasoning tests still pass.

## 2. Resolved profile admission and model aliases

- [x] 2.1 Add case-insensitive global alias indexing to the provider registry; reject every duplicate normalized alias during registry loading while retaining provider-qualified lookup; verify unique aliases resolve to the correct model/contract and duplicates fail before runtime construction.
- [x] 2.2 Implement the side-effect-free host-local runtime-profile preflight interface for the strict runtime-config path, resolving profile defaults plus optional translator/reviewer/model/reasoning selections before reporting sanitized descriptor, policy, bindings, and identity; run it automatically before every configured run and expose `--preflight` as check-and-exit, with human-readable output by default and a JSON output form for automation; verify tests assert no model lifecycle, remote HTTP/provider call, source submission, or run-artifact creation.
- [x] 2.3 Implement preflight prerequisite checks for local executable/model paths and port readiness plus remote auth environment-variable presence, emitting only names/statuses and returning non-zero on failure; verify focused tests cover missing paths, unavailable/invalid port, and absent/empty environment variables without leaking values. Preserve the existing later remote endpoint preflight as a separate network check.

## 3. Canonical examples and regression coverage

- [x] 3.1 Refresh `runtime_local.example.yaml` as the canonical config-form local profile and remove stale comments while preserving its current supported identity/transport; verify loader and preflight tests exercise the shipped file.
- [x] 3.2 Refresh `runtime_remote.example.yaml` as a portable, policy-bearing remote profile without credentials or host-specific model paths; explicitly record default role models, reasoning level/mapping, timeout, budget, and structured-output policy, with optional CLI overrides falling back to these values; verify its resolved descriptor exposes all configured defaults.
- [x] 3.3 Keep `runtime_composite.example.yaml` byte-for-byte out of the change and update `configs/providers.yaml`/tests only as needed for global alias validation; verify the focused diff contains no composite-profile edit.

## 4. Verification and review

- [x] 4.1 Run the narrow runtime-config, provider-registry, and strict-CLI preflight test selection plus `git diff --check`; verify no pipeline, server lifecycle action, or remote provider call occurred.
- [x] 4.2 Run `openspec validate runtime-profile-hardening --strict` and prepare the implementation for the required independent Pact review; verify the final report lists changed files, commands, and residual host-specific validation limits.
