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
- `configs/providers.yaml` — provider/model registry for `--translator`/`--reviewer` aliases (case-insensitive, fail-closed on duplicates)
- `pact_v4/pipeline/phase_progress.py` — progress reporting (see `docs/plans/V4_PHASE12_RUN_PROGRESS_TRACKER_TASK_RU.md`)

## v4 run command (unified dispatcher, book-first)

The supported launch surface is the thin dispatcher `pact_full_pipeline_runner_v1.v4_run` with primary `book`
and retained `chapter` modes. It forwards to the existing strict/book entrypoints without changing pipeline semantics.

```powershell
# Book — chapters 27-32 on a local profile (automatic output D:\pact\gate_bench_runs/book_0027-0032_local_<timestamp>)
python -m pact_full_pipeline_runner_v1.v4_run book --chapters 27-32 --runtime-config configs/runtime_local.example.yaml

# Book — remote profile, explicit model aliases and reasoning override
python -m pact_full_pipeline_runner_v1.v4_run book --chapters 27-32 --runtime-config configs/runtime_remote.example.yaml --translator opencode-go/musefree --reviewer openai/luna --reasoning 2

# Book — explicit markup (preserve only; the existing preservation/normalization policy)
python -m pact_full_pipeline_runner_v1.v4_run book --chapters 27-32 --runtime-config configs/runtime_local.example.yaml --markup preserve

# Chapter — single chapter, no-config local compatibility (no profile) or with profile
python -m pact_full_pipeline_runner_v1.v4_run chapter --chapter-id 0001 --chapter-html D:/pact/pact_chapters/0001.html --memory-dir D:/pact/pact_chapters --out-dir D:/pact/gate_bench_runs/chapter_0001
python -m pact_full_pipeline_runner_v1.v4_run chapter --chapter-id 0001 --chapter-html D:/pact/pact_chapters/0001.html --memory-dir D:/pact/pact_chapters --out-dir D:/pact/gate_bench_runs/chapter_0001 --runtime-config configs/runtime_remote.example.yaml
```

Runtime profile defaults: the selected profile supplies default role models, reasoning, transport, and
identity-bearing policy. Omitted `--translator`/`--reviewer`/`--reasoning` use profile values; explicit values
are validated against the runtime/provider contract, are identity-bearing, and alias selection is
case-insensitive and fail-closed.

Offline preflight (host-local, no network/model/source/artifact side effects) runs by default before every
configured execution and before any output directory is created:
```powershell
python -m pact_full_pipeline_runner_v1.v4_run book --chapters 27-32 --runtime-config configs/runtime_remote.example.yaml --preflight
python -m pact_full_pipeline_runner_v1.v4_run book --chapters 27-32 --runtime-config configs/runtime_remote.example.yaml --preflight --json
# alias: --preflight-json
```
Check-only modes report the sanitized resolved profile, model bindings, effective policy, topology, and
identity and exit without starting the pipeline or creating artifacts.

Output naming: a distinct subdirectory below `D:\pact\gate_bench_runs` is created automatically,
named `book_0027-0032_local_<timestamp>` or `book_0027-0032_remote_<timestamp>` — the `local|remote` label
is derived from the resolved runtime descriptor after profile defaults and explicit overrides.

Help is offline-only (no pipeline/model/artifact side effects):
```powershell
python -m pact_full_pipeline_runner_v1.v4_run --help
python -m pact_full_pipeline_runner_v1.v4_run book --help
python -m pact_full_pipeline_runner_v1.v4_run chapter --help
```

Historical `run_full_pipeline*.ps1` / v3 launchers are not supported v4 commands.

Workspace:

- `AGENTS.md` — workspace safety, review workflow, mandatory skills
- `DECISIONS.md` — architectural decisions
- `docs/` — additional architecture, plans, and handoff notes

## v3 (archived, non-operational)

v3 code and prior operational procedures are archived and not executed from this tree.
All former v3 operational paths and scripts referenced in earlier README versions
no longer exist in the v4 tree and are not used.
For historical v3 context, see `archive/v3-main-20260802` and tags `v3.1.3*`.
