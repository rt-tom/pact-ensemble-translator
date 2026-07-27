# PR #8 Review Packet — v3.1.2j owned model lifecycle

## Risk classification

**REVIEW REQUIRED.** This changes runner orchestration and server lifecycle,
without changing model profiles, inference parameters, Python stage behavior,
run artifacts, or cache formats.

## Policy implemented

- Model startup is skipped only when every selected chapter has the exact
  stage aggregate guarded by the current Python stage.
- A partial issue/chunk cache never authorizes a skip.
- The Python stage is still invoked and follows its existing `Reusing` path.
- Translation retains unconditional profile assurance and no file-exists
  shortcut.
- Model-free finalization no longer starts GemmaTranslate.
- A running server is reused only when tracked ownership metadata, PID,
  profile, executable, full argument signature, actual process command line,
  and live health all match.
- The runner stops only its tracked process. Global llama-server termination is
  removed.
- An already responding unowned port is a fail-closed error: it is neither used
  nor stopped.

## Aggregate matrix

| Stage | Aggregate required for every selected chapter |
| --- | --- |
| Source analysis | `source_scene_map.json` |
| Audit | `v31/<pass>/<mode>.json` |
| Cross-verification | `v31/<pass>/cross_verify_<judge>.json` |
| Repair | `v31/<pass>/repair_candidates_round_<NN>.json` |
| Postcheck | `v31/<pass>/post_gate_<judge>_round_<NN>.json` |

Missing aggregate, force/RedoQuality, RedoSourceAnalysis, or RedoTranslation
selects the normal model-required path. Cache directories or individual cache
files are deliberately ignored by the decision.

## MODEL_REQUIRED boundary

The general protocol is not added to five Python CLIs in this focused change.
Skip is limited to guards that unconditionally return `Reusing` for an existing
aggregate. The explicit structured protocol is recorded as a mandatory v3.1.3
task in `docs/plans/V3.1.3_MODEL_REQUIRED_PROTOCOL.md` and is required before
this optimization can be broadened.

## Open-PR overlap

PR #7 is independent but overlaps in:

- `run_full_pipeline_v31.ps1` (release version only);
- `v31_common.py` (release version only).

This branch starts from current `main`, not PR #7. Before merge it must be
refreshed against the then-current main and all tests rerun. The expected final
release number must be resolved during that refresh.

## Tests

- all aggregates versus one missing aggregate;
- partial cache directory does not skip;
- force disables skip;
- stable command signature and argument mismatch;
- PID/profile/executable/signature/command-line/health mismatches reject reuse;
- global process termination is absent;
- foreign endpoint protection is present;
- model-free finalization has no model start;
- Python compilation and existing offline self-tests;
- PowerShell AST checks and `git diff --check`.

## Production safety

Production, the active/stopped run, and its 58 existing Qwen cache files are
not modified. No pipeline or model is launched by development tests. No Reset,
Redo, or force flag is used.
