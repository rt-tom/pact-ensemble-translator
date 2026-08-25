# Pact Translator v4

`main` now carries the v4 development tree. v3 is archived
(`archive/v3-main-20260802`, tags `v3.1.3*`) and no longer used.

> **Production runs are owner-started on RT only** (`D:\pact\pact_translator_v4_1`).
> Do not start pipelines from the `media` dev host or from worktrees.
> Agents inspect code and artifacts only.

## v4 navigation

Architecture and plans:

- `docs/architecture/V4_MVP_SPEC_RU.md` — canonical v4 MVP spec
- `docs/architecture/V4_FINAL_REVIEW_AND_IMPLEMENTATION_PLAN_RU_v2.md` — final review and implementation plan
- `docs/plans/V4_IMPLEMENTATION_ORDER_PLAN_RU.md` — implementation order
- `docs/plans/V4_OPENCODE_REMOTE_MODELS_INTEGRATION_PLAN_RU.md` — remote models integration

Pipeline and runtime:

- `pact_v4/pipeline/` — pipeline entry points (`v4_phase12_strict_runner.py` is the production driver)
- `pact_v4/runtime/` — runtime backends and coordination
- `configs/runtime_local.example.yaml`, `configs/runtime_remote.example.yaml`, `configs/runtime_composite.example.yaml` — runtime config templates
- `pact_v4/pipeline/phase_progress.py` — progress reporting (see `docs/plans/V4_PHASE12_RUN_PROGRESS_TRACKER_TASK_RU.md`)

Workspace:

- `AGENTS.md` — workspace safety, review workflow, mandatory skills
- `DECISIONS.md` — architectural decisions
- `docs/` — additional architecture, plans, and handoff notes

## v3 (archived, non-operational)

v3 code and prior operational procedures are archived and not executed from this tree.
All former v3 operational paths and scripts referenced in earlier README versions
no longer exist in the v4 tree and are not used.
For historical v3 context, see `archive/v3-main-20260802` and tags `v3.1.3*`.
