# Handoff — runtime profiles, simple launcher и media artifact authority

**Дата:** 2026-08-25
**Статус:** planning/research; pipeline на media/RT в этой сессии не запускался.

## Зафиксированные решения владельца

### Runtime profiles и simple launcher

- `configs/runtime_local.example.yaml` признан актуальным canonical local profile. Исторический no-config local CLI остаётся backward-compatible до отдельного решения; простой `--local` не должен молча менять его identity/transport.
- `runtime_composite.example.yaml` — advanced/test-only. Не включать в simple launcher и не обновлять сейчас; при необходимости собрать заново из обновлённых local/remote профилей.
- Simple UX после config hardening: book-first команда с диапазоном глав, `--local` либо `--remote <translator>/<reviewer>`, например `--remote luna/musefree`; provider prefixes в команде не требуются, aliases должны быть глобально уникально резолвимы fail-closed.
- `--markup preserve` не должен быть обязательным в happy path: existing preserve/normalization policy остаётся default и объясняется в help; новая markup semantics — отдельный formatting change.
- Для remote canonical policy хранится в profile, code defaults — compatibility fallback. Simple mode не даёт скрытых quality overrides; preflight должен печатать resolved profile/models/policy/identity.
- Требуется отдельный runtime-profile hardening/review change: local allowlist/cross-field validation, host-only preflight paths/ports/env, stale comments cleanup, portable remote profile. До него `v4-run-command-help` не реализовывать.

### Media/RT execution и artifacts

- media — canonical artifact/state authority.
- media разрешён только для owner-started **remote** runs; RT остаётся host для local-model runs и может также запускать remote runs.
- Future media-controller → RT llama-worker protocol отложен в отдельный high-risk change.
- media store layout: `/home/rt/pact_runs/books/<book-id>/` с `CURRENT.json`, `locks/`, `incoming/`, `quarantine/`, immutable `snapshots/`.
- Один active writer/lease на одну книгу независимо от host. Никакого shared writable folder, two-way sync, merge mutable memory или cross-host resume.
- RT/media worker всегда использует local staging; `v4_book_run` выполняет всю causal promotion локально на worker, а публикуется только terminal snapshot bundle.
- Handoff transport: RT SFTP-upload в media `incoming/`, затем ограниченная SSH-команда `pact-promote` на media валидирует candidate и атомарно обновляет `CURRENT` либо отправляет bundle в quarantine.
- RT console обязан сообщать конечный cross-host verdict: `MEDIA PUBLISH: ACCEPTED` с revision/manifest/current evidence или `REJECTED` с причиной/quarantine location; rejection даёт non-zero exit.
- `complete` и `accepted_degraded` автоматически publish/promote только после полного manifest/identity/PID/quarantine validation.
- Accepted snapshots и terminal bundles хранятся immutable; failed/invalid candidates — quarantine 30 дней, затем automatic cleanup.
- Lease TTL не даёт automatic takeover: recovery/release только owner-managed после проверки staging.
- Для remote model run source/prompt/context закономерно передаётся выбранному provider; это принято владельцем как scope owner-started remote runs. Секреты/env values никогда не входят в manifests/transfers/artifacts.

## Ключевые ограничения

- Current `README.md`/`AGENTS.md` всё ещё говорят production pipeline only on RT. Перед implementation/media execution нужна отдельная policy update: media как approved owner-started **remote** execution host; agents pipeline не запускают.
- SFTP push требует media SSH/SFTP service и restricted RT key/account. `pact-promote` должен быть restricted command, не interactive shell.
- SSH host keys, accounts, path ACL, bootstrap procedure, RT mirror/adoption pointer и lease-recovery audit format ещё не спроектированы.
- Не копировать/не сливать по частям: `glossary.json`, `book_memory.json`, `chapter_index.json`, observations, ledgers, `book_run.json`, live journals/progress/usage, credentials/server state. Переносится целый immutable terminal bundle с manifest.

## Active OpenSpec changes

| Change | State | Next action |
|---|---|---|
| `v41-runtime-efficiency` | Complete, merged | Archive only on owner command. |
| `v4-phase12-strict-runner` | Planning complete | Future Stage 2–4 only through separate approved changes. |
| `v4-strict-runner-characterization-baseline` | Complete, merged `72a1736` | Archive only on owner command. |
| `v4-run-command-help` | Proposal in `main` | Revise/finalize only after runtime profile hardening decisions; do not implement yet. |
| `v3-deprecation-plan` | Planning | Policy C/tag-only selected; implementation deferred. |
| `book-state-snapshot-handoff` | Planning, currently uncommitted before this handoff commit | Needs owner review then separate implementation OpenSpec after transport/policy details. |

## Main commits already pushed

- `72a1736` — strict-runner characterization baseline.
- `36e9e1f` — `v4-run-command-help` proposal.
- `df4339f`, `9507566`, `ef42eb5` — strict-runner/v3 planning decisions.
- `21ac0fb` — unsupported live diagnostic removal.

## Recommended next sequence

1. Commit/push this handoff plus `book-state-snapshot-handoff` planning artifacts.
2. Create a planning-only `runtime-profile-hardening` OpenSpec and reconcile local/remote canonical profiles; no host/server changes yet.
3. Update `v4-run-command-help` design against the approved profile model.
4. Owner approves policy wording plus SSH/SFTP restricted-command host setup design.
5. Create a separate implementation change for the smallest snapshot protocol slice (manifest/layout/validator first, no live pipeline).

## Do not do automatically

No RT deploy/sync, no pipeline run, no server start/stop, no artifact/data transfer, no OpenSpec archive, no production policy change without explicit owner command.
