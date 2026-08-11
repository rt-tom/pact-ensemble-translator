#!/usr/bin/env python3
"""CLI: run the strict single-resident driver on a real chapter.

Backing task: ``docs/plans/V4_STRICT_DRIVER_CHAPTER_TRIAL_TASK_RU.md``.
Wires ``pact_v4.pipeline.v4_phase12_strict_runner.run_chapter_strict`` to
a real, self-started ``llama-server`` (SYCL build validated in
Measurement 2 -- ``C:\\llama-sycl-new``, same server flags) via
``build_strict_lifecycle``. Not a production entry point: this does not
touch ``pact_translate_v3.py`` or v3 config, and it is meant for one
explicit chapter trial run, not routine use.

Usage (``--chapter-id``/``--chapter-html``/``--memory-dir`` are required,
no default chapter -- this has already been re-run against chapter_046
more than once; a default would just make that easier to repeat by
accident)::

    python -m pact_full_pipeline_runner_v1.v4_phase12_strict_run \\
        --chapter-id "046_subordination-6-3" \\
        --chapter-html "D:/pact/pact_chapters/0046_subordination-6-3.html" \\
        --memory-dir "D:/pact/pact_chapters" \\
        --out-dir "D:/pact/gate_bench_runs/v4_phase12_strict_046/run_002"

V4 C3 (PR 4 of ``docs/plans/V4_OPENCODE_REMOTE_MODELS_INTEGRATION_PLAN_RU.md``):
``--runtime-config`` selects a tagged local/remote/composite backend profile
instead of the historical local default. Without it the CLI is byte-for-byte
the old local-only strict command (same model-call order, lifecycle and
record). ``--managed-server`` forces every OpenCode backend in the profile to
Pact-managed mode (Pact starts its own ``opencode serve`` and stops it after
the run). Remote profiles print a §12 acknowledgement that chapter text is
sent to the configured remote provider(s).
"""
from __future__ import annotations

import argparse
import json
import logging
import os
from dataclasses import replace
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence

from pact_v4.pipeline.v4_phase12_strict_runner import (
    StrictBackendConfig,
    StrictRunConfig,
    build_strict_lifecycle,
    run_chapter_strict,
)
from pact_v4.pipeline.phase_progress import PhaseProgressWriter
from pact_v4.runtime.json_resilience import JsonRetryPolicy
from pact_v4.runtime.runtime_config import (
    CompositeBackendConfig,
    LocalLlamaBackendConfig,
    OpenCodeBackendConfig,
    build_repair_adapters,
    build_role_adapters,
    build_role_backend,
    load_runtime_config,
    validate_reasoning_backend,
)
from pact_v4.runtime.runtime_coordinator import LocalLifecycleCoordinator

LOG = logging.getLogger("v4_phase12_strict_run")

LLAMA_ROOT = Path(r"C:\llama-cpp")
GEMMA_PATH = LLAMA_ROOT / "models" / "gemma-4-26B-A4B-it-UD-Q4_K_XL.gguf"
GEMMA_DRAFT_PATH = LLAMA_ROOT / "models" / "MTP" / "mtp-gemma-4-26B-A4B-it-Q8_0.gguf"
# V4.1 B3 (review fix F3): the default local Qwen AUDIT model is the MTP
# variant (C:\llama-cpp\models\Qwen3.6-35B-A3B-MTP\...) — the B3 contract
# declares the Qwen audit as MTP draft, reasoning 8192, context 49k. The
# historical non-MTP path ran the audit with --reasoning-budget 0 and no
# draft spec, which the server profile and the config identity must agree
# on. Effective transport is part of the backend identity (server_args in
# effective_options), so this change invalidates old B3 caches.
QWEN_PATH = LLAMA_ROOT / "models" / "Qwen3.6-35B-A3B-MTP" / "Qwen3.6-35B-A3B-UD-Q4_K_XL.gguf"

# V4.1 (2026-08-09): same validated SYCL profile as Measurement 2
# (docs/plans/V4_SINGLE_RESIDENT_DRIVER_ARCHITECTURE_RU.md, "Результат
# измерения 2") and the user's optimized production command line, so
# lifecycle numbers from this real chapter trial are comparable to that
# synthetic benchmark's. Gemma context raised 32768 -> 49152 and
# --reasoning-budget 0 -> 2048 per V4.1 §3.4 (owner-verified 2026-08-08:
# reasoning-budget 2048 works); MTP draft is OFF in v4.1 (binary
# C:\src\llama-sycl-edge\build\bin\llama-server.exe, -dev SYCL0 and env
# GGML_SYCL_FA_DECODE_KERNEL=auto are set at launch — see plan §3.4).
CONTEXT_SIZE = 32768
GEMMA_CONTEXT_SIZE = 49152
GEMMA_SERVER_ARGS = [
    "-ngl", "99",
    "-ncmoe", "18",
    "--load-mode", "mmap",
    "--reasoning-budget", "2048",
    "-np", "1",
    "-c", str(GEMMA_CONTEXT_SIZE),
    "-fa", "on",
    "--jinja",
    "-ctk", "q8_0",
    "-ctv", "q4_0",
    "--cache-ram", "0",
    "--ctx-checkpoints", "0",
]
# V4.1 B3 (review fix F3): the Qwen audit server args ARE the B3 profile —
# MTP draft spec, --reasoning on, --reasoning-budget 8192, context 49152
# (runtime_local.example.yaml qwen block, plan §3.4 / B1 49k contract).
# Keeping a stale reasoning-budget 0 / 32k profile here would silently run
# the audit server differently than the B3 config identity declares.
QWEN_SERVER_ARGS = [
    "--spec-type", "draft-mtp",
    "--spec-draft-n-max", "3",
    "--device", "SYCL0",
    "-fit", "on",
    "-fitt", "1280",
    "-b", "2048",
    "-ub", "512",
    "-ctk", "q8_0",
    "-ctv", "q4_0",
    "-t", "6",
    "-tb", "12",
    "--load-mode", "mmap",
    "--reasoning", "on",
    "--reasoning-budget", "8192",
    "-np", "1",
    "-c", "49152",
    "-fa", "on",
    "--jinja",
    "--cache-ram", "0",
    "--ctx-checkpoints", "0",
]


def _gemma_server_args_for_reasoning(reasoning: int) -> list:
    """The §3.4 sycl-edge Gemma server args bound to the selected reasoning.

    V4.1 A2 review fix (RV, commit 4ab250b): the local generator's reasoning
    budget is transported via ``--reasoning-budget`` in the server args, so
    the args must AGREE with the CLI/config reasoning value or the config
    identity would record a reasoning level the server does not actually
    run. ``reasoning > 0`` (the supported A2 profile, §3.4) keeps the fixed
    ``2048`` budget; ``reasoning == 0`` (the B1 baseline) pins the budget to
    ``0`` so a default run never silently starts the server with reasoning
    the identity denies. The base §3.4 args (``GEMMA_SERVER_ARGS``) are
    unchanged — only the budget value is derived here.
    """
    budget = "2048" if reasoning > 0 else "0"
    args = list(GEMMA_SERVER_ARGS)
    for index, arg in enumerate(args):
        if arg == "--reasoning-budget" and index + 1 < len(args):
            args[index + 1] = budget
            return args
    return args + ["--reasoning-budget", budget]


def build_argparser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    # No defaults for chapter_id/chapter_html/memory_dir: this CLI has
    # been re-run against the same chapter_046 more than once already
    # (dry runs, the strict-driver trial, the max_tokens-fix re-check) --
    # a default here is exactly what silently reproduces that instead of
    # the chapter the caller actually meant to run. Required, always
    # explicit.
    p.add_argument("--chapter-id", required=True)
    p.add_argument("--chapter-html", type=Path, required=True,
                    help="e.g. D:/pact/pact_chapters/0046_subordination-6-3.html "
                         "(the chapter_046 trial used chapter-id 046_subordination-6-3).")
    p.add_argument("--memory-dir", type=Path, required=True,
                    help="Directory with glossary.json/book_memory.json (or neither, for "
                         "empty memory -- the chapter_046 trial used D:/pact/pact_chapters, "
                         "which has neither file).")
    p.add_argument("--out-dir", type=Path, required=True)
    p.add_argument("--run-label", default="v4-phase12-strict-chapter-trial",
                    help="Run label written to the strict chapter trial record "
                         "(B8: a re-validation run needs its own label, e.g. "
                         "v4-phase12-strict-0001-run002, so artifacts are not "
                         "confused with the historical trial run's). Does NOT "
                         "participate in config/snapshot identity -- use a "
                         "distinct --out-dir for cache/resume isolation.")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8094)
    p.add_argument("--max-consecutive-nonselections", type=int, default=3)
    p.add_argument("--mixed-script-allow", action="append", default=None,
                    metavar="ENTRY",
                    help="B5 manual mixed_script allowlist entry (repeatable). "
                         "Entries are tokenized like bible/glossary entries, so "
                         "'R.D.T.' unblocks the tokens R/D/T; 'Blake' unblocks "
                         "'Blake'. The combined allowlist is "
                         "bible + glossary + source-derived + this manual set.")
    p.add_argument("--startup-timeout", type=float, default=240.0)
    p.add_argument("--unload-timeout", type=float, default=30.0)
    p.add_argument("--lazy-balanced", action=argparse.BooleanOptionalAction, default=None,
                   help="V4 Efficiency A2: generate a single balanced_literary candidate "
                        "per chunk and lazily generate fidelity_first only when the primary "
                        "fails the Qwen/deterministic gates. Overrides the "
                        "PACT_EFFICIENCY_LAZY_BALANCED env var; default true. "
                        "--no-lazy-balanced restores the legacy 2-candidate A/B + Gemma "
                        "scheme (full rollback).")
    p.add_argument("--runtime-config", type=Path, default=None, metavar="FILE",
                    help="YAML/JSON tagged runtime profile (kind local_llama | "
                         "opencode_server | composite). When absent the historical "
                         "local llama-server profile is used unchanged.")
    p.add_argument("--managed-server", action="store_true",
                    help="Start Pact's own 'opencode serve' for every OpenCode "
                         "backend in the runtime config (server_mode=managed) and "
                         "stop it after the run. Ignored without --runtime-config.")
    p.add_argument("--reasoning", type=int, choices=(0, 1, 2, 3), default=0,
                   help="V4.1: Phase 2B generation reasoning budget (0=off, "
                        "1=low, 2=medium, 3=high). Applied ONLY to generation "
                        "(opencode serve 'reasoningEffort'); the Qwen audit / "
                        "repair / formatting phases are untouched. Part of the "
                        "config identity — a reasoning change invalidates "
                        "cache/resume, so use a NEW --out-dir.")
    p.add_argument("--stop-after-generation", action="store_true",
                   help="V4.1 A1: early exit right after Phase 1-2 generation "
                        "(chunked runs: generation + per-chunk selection). "
                        "Steps 6/7/8 are skipped and recorded as "
                        "skipped_stop_after_generation (translations.json keeps "
                        "the translation). Default: full cycle. Part of the "
                        "config identity — use a NEW --out-dir. "
                        "Renamed from --stop-after selection (A1).")
    p.add_argument("--whole-chapter", action="store_true",
                   help="V4.1 A1: whole-chapter generation — ONE model call "
                        "per chapter against the full ordered PID map "
                        "(WholeChapterPidMap derived from the chunk plan), "
                        "strict {pid: text} JSON contract, bounded retry on "
                        "malformed/missing/extra/reordered PID, empty/"
                        "truncated JSON and session aborts. No chunking, no "
                        "selection (selection_results.json is always written "
                        "with schema pact-v4-whole-chapter-selection/v1, "
                        "mode=not_applicable); translations_raw.json is the "
                        "validated generator snapshot. Part of the config "
                        "identity — use a NEW --out-dir.")
    audit_group = p.add_mutually_exclusive_group()
    audit_group.add_argument("--run-audit", action="store_true",
                   help="V4.1 B3 (production default): after whole-chapter "
                        "generation run the production audit/repair stage "
                        "(ChunkedAuditEvaluator -> hard filters -> selective "
                        "repair -> re-audit) and rewrite "
                        "translations_repaired.json / translations.json with "
                        "the repaired map. The stage is skipped when "
                        "--skip-audit is given or no audit machinery is "
                        "wired (A1 generation-only behavior). Part of the "
                        "config identity — use a NEW --out-dir when flipping "
                        "it against an existing run.")
    audit_group.add_argument("--skip-audit", action="store_true",
                   help="V4.1 B3: disable the production audit/repair stage "
                        "after whole-chapter generation (A1 behavior: steps "
                        "6/7/8 recorded as skipped). Mutually exclusive with "
                        "--run-audit; default is to run the audit.")
    p.add_argument("--entity-context", action=argparse.BooleanOptionalAction,
                   default=None,
                   help="V4.1 B3 (owner decision 2026-08-10, B1.3 gate "
                        "pending): enable the source-only entity prepass "
                        "(B1.2) feeding the auditor and the hard filters "
                        "(entity-PID issues forced to TIER_B). Default true; "
                        "--no-entity-context audits without the entity block. "
                        "Part of the config identity — use a NEW --out-dir "
                        "when flipping it against an existing run.")
    p.add_argument("-v", "--verbose", action="store_true")
    return p


# ---------------------------------------------------------------------------
# Runtime-config loading / profile helpers (V4 C3, plan §8/§12)
# ---------------------------------------------------------------------------


def _load_runtime_config_file(path: Path) -> Any:
    """Parse a ``--runtime-config`` YAML/JSON file into a tagged config.

    Dispatch by extension so a multi-megabyte YAML profile is not scanned as
    JSON first: ``.yaml``/``.yml`` go straight to PyYAML, everything else
    tries JSON and falls back to YAML. Secret values are never read here --
    only env-var *names* (plan §12).
    """
    raw = path.read_text(encoding="utf-8")
    if path.suffix.lower() in (".yaml", ".yml"):
        payload = _load_yaml(raw, path)
    else:
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            payload = _load_yaml(raw, path)
    if not isinstance(payload, Mapping):
        raise ValueError(
            f"{path}: runtime config must be a mapping, got {type(payload).__name__}"
        )
    return load_runtime_config(payload)


def _load_yaml(raw: str, path: Path) -> Any:
    try:
        import yaml  # type: ignore
    except ImportError as exc:
        raise ValueError(
            f"{path}: looks like YAML but PyYAML is not installed "
            "(pip install pyyaml) or the file is not valid JSON"
        ) from exc
    return yaml.safe_load(raw)


def force_managed(cfg: Any, *, nested: bool = False) -> Any:
    """Apply ``--managed-server`` to a loaded runtime config.

    Every ``opencode_server`` profile becomes ``server_mode=managed`` (Pact
    starts its own ``opencode serve`` and stops only that process). A
    top-level ``local_llama`` profile has nothing to manage and is an error;
    a local sub-backend inside a composite is left as-is (Pact already owns
    the llama-server lifecycle).
    """
    if isinstance(cfg, OpenCodeBackendConfig):
        if cfg.server_mode == "managed":
            return cfg
        return replace(cfg, server_mode="managed")
    if isinstance(cfg, CompositeBackendConfig):
        return replace(cfg, backends={
            name: force_managed(sub, nested=True)
            for name, sub in cfg.backends.items()
        })
    if isinstance(cfg, LocalLlamaBackendConfig):
        if nested:
            return cfg
        raise ValueError(
            "--managed-server given but the runtime config is local_llama; "
            "there is no opencode server to manage"
        )
    raise ValueError(
        f"--managed-server: unsupported config kind {type(cfg).__name__}"
    )


def _remote_endpoints(cfg: Any) -> Sequence[str]:
    """Public endpoints of the remote providers a profile will talk to."""
    if isinstance(cfg, OpenCodeBackendConfig):
        return (cfg.server.base_url,)
    if isinstance(cfg, CompositeBackendConfig):
        endpoints: list = []
        for sub in cfg.backends.values():
            if isinstance(sub, OpenCodeBackendConfig):
                endpoints.append(sub.server.base_url)
        return endpoints
    return ()


def _warn_remote_acknowledgement(cfg: Any) -> None:
    """§12 acknowledgement: chapter text is sent to the configured provider."""
    endpoints = _remote_endpoints(cfg)
    if not endpoints:
        return
    LOG.warning(
        "PROFILE SENDS CHAPTER TEXT TO A REMOTE PROVIDER: this run sends the "
        "chapter source text to the remote provider(s) configured in the "
        "runtime profile (endpoints: %s). OpenCode sessions are isolated per "
        "work unit, tools are disabled, and no credentials are persisted. "
        "Run only if you accept this.",
        ", ".join(endpoints),
    )


def _log_result(result: Any) -> None:
    step6_status = result.step6.get("status")
    step6_extra = ""
    if step6_status not in (None, "complete", "skipped"):
        failed_units = result.step6.get("failed_units") or []
        step6_extra = (
            f" (failed_units={len(failed_units)}, "
            f"error={result.step6.get('error')!r})"
        )
    LOG.info(
        "Done: chunks=%d/%d selected=%d quarantined=%d needs_synthesis=%d "
        "incomplete_generation=%d halted_early=%s restarts=%d wall_clock=%.1fs "
        "step6=%s%s step7=%s terminal=%s",
        result.processed_count, result.chunk_count, result.selected_count,
        result.quarantined_count, result.needs_synthesis_count,
        result.incomplete_generation_count, result.halted_early,
        result.record["lifecycle"]["restart_count"], result.record["wall_clock_seconds"],
        step6_status, step6_extra,
        result.step7.get("status"),
        result.step8.get("status"),
    )


def _cli_exit_code(result: Any) -> int:
    """CLI exit semantics (F2, B3 review): a run is a FAILURE when the
    chapter was not released as audited.

    * 0 — success: generation-only / intentional ``--skip-audit`` /
      ``--stop-after-generation`` runs, or a chapter that completed and was
      released as audited.
    * 2 — halted early (existing lifecycle contract).
    * 3 — B3 fail-closed (audit incomplete) or B3 failed (exception):
      the runner recorded step6/7/8 failed / fail_closed_audit_incomplete
      and the chapter was NOT released as audited. Before this fix the CLI
      returned 0 for these, silently reporting success on a failed audit.

    ``--skip-audit`` / generation-only stays 0 because the audit stage is
    intentionally off — the steps are recorded as ``skipped``, never
    fabricated as complete.
    """
    if result.halted_early:
        return 2
    step8 = result.step8 or {}
    status = step8.get("status")
    if status in ("failed", "fail_closed_audit_incomplete"):
        return 3
    if step8.get("released_as_audited") is False:
        # F1: a repair-debt terminal (accepted_degraded) is not an audited
        # release — the run did not achieve its publication gate.
        return 3
    return 0


def _env_flag(name: str, *, default: bool) -> bool:
    """Parse a boolean env-var override (e.g. ``PACT_EFFICIENCY_LAZY_BALANCED``).

    Accepts ``1/true/yes/on`` and ``0/false/no/off`` (case-insensitive);
    anything else is an error rather than a silent misconfiguration.
    """
    raw = os.environ.get(name)
    if raw is None:
        return default
    normalized = raw.strip().lower()
    if normalized in ("1", "true", "yes", "on"):
        return True
    if normalized in ("0", "false", "no", "off"):
        return False
    raise ValueError(
        f"{name}: expected a boolean value, got {raw!r} "
        "(use 1/true/yes/on or 0/false/no/off)"
    )


def _build_run_config(args: argparse.Namespace, backend: Any) -> StrictRunConfig:
    return StrictRunConfig(
        chapter_id=args.chapter_id, chapter_html_path=args.chapter_html, memory_dir=args.memory_dir,
        out_dir=args.out_dir, backend=backend,
        max_consecutive_terminal_nonselections=args.max_consecutive_nonselections,
        deterministic_mixed_script_allow=tuple(args.mixed_script_allow or ()),
        run_label=args.run_label,
        # V4.1: reasoning budget for Phase 2B generation and early exit after
        # Phase 1-2 generation. Both are part of the config identity
        # (StrictRunConfig.to_config_artifact), so a run with either set is
        # NOT resumable from a prior out-dir — the owner must pass a NEW
        # --out-dir for these experiment runs.
        reasoning=args.reasoning,
        stop_after=("generation" if args.stop_after_generation else ""),
        # V4.1 A1: whole-chapter mode (one generation call per chapter).
        whole_chapter=args.whole_chapter,
        # V4.1 B3: production audit/repair after whole-chapter generation.
        # Default = run (production); --skip-audit turns the stage off.
        # Both flags together are a contradiction and rejected at parse.
        run_audit=not args.skip_audit or args.run_audit,
        # V4.1 B3 (owner decision 2026-08-10): entity prepass default true;
        # --no-entity-context disables it (audit without the entity block).
        entity_context_enabled=(
            True if args.entity_context is None else args.entity_context
        ),
        # V4 Efficiency A2: CLI flag (--lazy-balanced/--no-lazy-balanced)
        # overrides the env var, which defaults to true (lazy mode on).
        lazy_balanced=(
            args.lazy_balanced
            if args.lazy_balanced is not None
            else _env_flag("PACT_EFFICIENCY_LAZY_BALANCED", default=True)
        ),
    )


def _build_b3_audit_repair(cfg: StrictRunConfig, backend: Any, runtime: Any):
    """Build the V4.1 B3 audit/repair bundle for whole-chapter runs.

    Whole-chapter + ``run_audit`` only: the production audit/repair stage
    (ChunkedAuditEvaluator -> hard filters -> selective repair -> re-audit)
    runs over the coordinator ``CompletionBackend`` (local/remote/composite
    alike — the same backend routes the Qwen audit ref and the generator
    repair ref). Returns ``None`` for chunked runs, ``--skip-audit`` runs,
    or when the audit machinery would have nothing to do (generation-only).

    Remote audit through ``opencode serve`` is a CONTRACT, NOT tested yet
    (owner decision: test remote audit after the B-phase); the evaluators
    never emit ``request_options`` — the reasoning budget is a server arg.
    """
    from pact_v4.pipeline.b3_audit_repair import (
        B3AuditRepair,
        B3AuditRepairConfig,
    )

    if not cfg.whole_chapter or not cfg.run_audit:
        return None
    completion_backend = build_role_backend(backend, runtime)
    return B3AuditRepair(
        audit_backend=completion_backend,
        repair_backend=completion_backend,
        config=B3AuditRepairConfig(
            entity_context_enabled=cfg.entity_context_enabled,
            max_input_tokens=cfg.audit_max_input_tokens,
            max_tokens=cfg.audit_max_tokens,
            overlap_tokens=cfg.audit_overlap_tokens,
            # F5 (B3 review): every repair-policy knob and prompt/extractor/
            # harness version is WIRED from the run config (not silently left
            # at module defaults), and it is part of the config identity — a
            # policy change invalidates the cached repaired map.
            reasoning_budget=cfg.audit_reasoning_budget,
            repair_findings_cap=cfg.audit_repair_findings_cap,
            repair_microbatch_trigger=cfg.audit_repair_microbatch_trigger,
            repair_microbatch_target=cfg.audit_repair_microbatch_target,
            repair_reaudit_neighbour_window=cfg.audit_repair_reaudit_neighbour_window,
            repair_reaudit_full_threshold=cfg.audit_repair_reaudit_full_threshold,
            # RV 71b7cbc fix (F5): the re-audit output budget and bounded B4
            # JSON retry policy are part of the config identity and are wired
            # through to the selective-repair evaluator by B3AuditRepair —
            # never silently left at module defaults.
            repair_reaudit_max_tokens=cfg.audit_repair_reaudit_max_tokens,
            repair_reaudit_max_retries=cfg.audit_repair_reaudit_max_retries,
            repair_reaudit_base_delay_seconds=cfg.audit_repair_reaudit_base_delay_seconds,
            prompt_version=cfg.audit_prompt_version,
            harness_version=cfg.audit_harness_version,
            extractor_version=cfg.audit_extractor_version,
        ),
    )


def _load_bible_text(memory_dir: Path, chapter_id: str) -> str:
    """Render the bible for adapter injection (B7, V4.1 A2).

    The strict driver renders the bible from the per-chapter
    ``chapter_index.json`` for the generation prompt itself (see
    ``run_chapter_strict``), but the audit, fidelity and repair adapters
    are constructed *before* the driver reloads memory. Loading the bible
    here and threading it into every adapter at construction time is the
    only point where the v4 model actually sees narrator
    gender/characters/facts — everywhere ``run_chapter_strict`` would not
    re-render the bible. When no ``chapter_index.json`` exists the
    renderer falls back to the legacy full-memory render.
    """
    from pact_v4.runtime.bible_renderer import render_bible_section
    from pact_v4.runtime.snapshot_factory import ChapterMemory

    memory = ChapterMemory.from_directory(memory_dir)
    return render_bible_section(chapter_id, memory.chapter_index, memory.book_memory)


# The B3 contract binds the Qwen audit server to the MTP build of the model:
# C:\llama-cpp\models\Qwen3.6-35B-A3B-MTP\Qwen3.6-35B-A3B-UD-Q4_K_XL.gguf.
# The MTP marker is the model-variant DIRECTORY (…\Qwen3.6-35B-A3B-MTP\…); the
# file stem itself carries no MTP marker (configs/runtime_local.example.yaml
# model_names.qwen is the plain file name). Identity is therefore verified as an
# EXACT whole path-component (or file-stem) match against the canonical variant
# name — a substring test would admit lookalikes (Qwen-non-MTP.gguf,
# …/MTP-disabled/…, forged model_names.qwen=Qwen-MTP.gguf) as MTP.
_B3_QWEN_MTP_VARIANT = "Qwen3.6-35B-A3B-MTP"
# A model NAME that explicitly negates MTP contradicts a valid MTP path —
# name/path coherence guard (the name must never override the path verdict).
_B3_QWEN_MTP_NEGATION_MARKERS = (
    "non-mtp",
    "no-mtp",
    "mtp-disabled",
    "mtp-off",
    "mtp-free",
    "without-mtp",
)


def _is_b3_qwen_mtp_identity(value: str) -> bool:
    """Exact MTP-variant identity: a path component or the file stem EQUALS
    the canonical MTP build name (case-insensitive). Substring lookalikes
    never match.

    The path is NORMALIZED (dot segments collapsed) before identity
    evaluation: a canonical MTP component followed by a ``..`` segment can
    resolve to a NON-MTP directory (…\\Qwen3.6-35B-A3B-MTP\\..\\
    Qwen3.6-35B-A3B\\…) while still appearing in the raw Path.parts listing
    — identity is judged on the EFFECTIVE path, not the literal spelling
    (RV4 HIGH). Malformed/ambiguous values fail closed (False)."""
    if not value:
        return False
    try:
        path = Path(os.path.normpath(value))
    except (TypeError, ValueError):
        # Malformed value (e.g. embedded null byte, invalid Windows path
        # characters) cannot satisfy the exact MTP identity — fail closed.
        return False
    return any(
        part.lower() == _B3_QWEN_MTP_VARIANT.lower()
        for part in path.parts
    ) or path.stem.lower() == _B3_QWEN_MTP_VARIANT.lower()


def _name_negates_b3_qwen_mtp(name: str) -> bool:
    lowered = name.lower()
    return any(marker in lowered for marker in _B3_QWEN_MTP_NEGATION_MARKERS)


def _validate_b3_qwen_profile(args: argparse.Namespace, backend: Any) -> None:
    """Fail loudly when a local profile cannot serve the B3 Qwen audit.

    F3 (B3 review): the B3 contract declares the Qwen audit server as MTP
    draft, reasoning 8192, context 49k. The historical default local
    profile launched the audit with ``--reasoning-budget 0`` and no MTP
    spec — the actual server then disagreed with the B3 config identity.
    The default local path now carries the B3 profile (QWEN_PATH /
    QWEN_SERVER_ARGS); this validation fails closed when a runtime-config
    profile that WILL run the B3 audit (whole-chapter + run_audit) cannot
    express the B3 profile. Remote/composite profiles are the provider's
    transport (contract, not testable here) — left to the remote audit
    contract.
    """
    if not (args.whole_chapter and not args.skip_audit):
        return
    if args.stop_after_generation:
        return
    from pact_v4.runtime.runtime_config import LocalLlamaBackendConfig

    if not isinstance(backend, LocalLlamaBackendConfig):
        # Remote/composite audit transport is provider-side; the remote
        # audit is a CONTRACT not tested yet (owner decision).
        return
    qwen_args = list((backend.server_args or {}).get("qwen") or [])

    def _arg_value(flag: str) -> Optional[str]:
        for index, arg in enumerate(qwen_args):
            if arg == flag and index + 1 < len(qwen_args):
                return qwen_args[index + 1]
        return None

    spec_type = _arg_value("--spec-type")
    reasoning_budget = _arg_value("--reasoning-budget")
    context = _arg_value("-c") or _arg_value("--ctx-size")
    # F1 (RV2): the qwen MODEL must itself be the MTP variant when the
    # server args declare MTP draft transport. The B3 contract binds the
    # Qwen audit to the MTP build (…/Qwen3.6-35B-A3B-MTP/…); a non-MTP
    # model path with MTP flags is an unsupported mismatch (the draft spec
    # is a property of the model build, not just the server args), so it
    # must fail loudly here instead of silently starting a non-MTP server.
    qwen_path = (backend.model_paths or {}).get("qwen")
    qwen_name = (backend.model_names or {}).get("qwen")
    path_str = str(qwen_path) if qwen_path is not None else ""
    name_str = str(qwen_name) if qwen_name is not None else ""
    problems: list = []
    if spec_type != "draft-mtp":
        problems.append("--spec-type draft-mtp (MTP draft)")
    if spec_type == "draft-mtp":
        # MTP transport declared -> the ACTUAL model PATH must carry the
        # exact MTP-variant identity (…/Qwen3.6-35B-A3B-MTP/…). The check is
        # exact (whole path component / file stem equal to the canonical MTP
        # build name), so a substring lookalike (Qwen-non-MTP.gguf,
        # …/MTP-disabled/…) can never pass, and a misleading model NAME can
        # never override a non-MTP path. A name that explicitly negates MTP
        # also contradicts a valid MTP path (name/path coherence).
        if not _is_b3_qwen_mtp_identity(path_str):
            problems.append(
                "qwen model_paths.qwen must be the exact MTP variant build "
                f"({_B3_QWEN_MTP_VARIANT!r}, e.g. …/Qwen3.6-35B-A3B-MTP/…) — "
                "substring lookalikes and misleading model_names cannot "
                "satisfy the B3 MTP identity when --spec-type draft-mtp is "
                f"set (got path={path_str!r}, name={name_str!r})"
            )
        elif name_str and _name_negates_b3_qwen_mtp(name_str):
            problems.append(
                "qwen model_names.qwen contradicts the exact MTP model path "
                f"({_B3_QWEN_MTP_VARIANT!r}) — the name must not negate MTP "
                f"(got name={name_str!r}, path={path_str!r})"
            )
    try:
        budget_ok = reasoning_budget is not None and int(reasoning_budget) >= 8192
    except ValueError:
        budget_ok = False
    if not budget_ok:
        problems.append("--reasoning-budget >= 8192")
    try:
        context_ok = context is not None and int(context) >= 49152
    except ValueError:
        context_ok = False
    if not context_ok:
        problems.append("context -c >= 49152")
    if problems:
        raise ValueError(
            "B3 audit requires a Qwen server profile that is B3-capable "
            "(missing: " + "; ".join(problems) + "). The Qwen audit server "
            "args and the B3 config identity must agree — add the B3 profile "
            "to the runtime config's qwen server_args "
            "(see configs/runtime_local.example.yaml), or pass --skip-audit."
        )


def run_local_default(args: argparse.Namespace) -> int:
    """The historical local-only path -- unchanged from before C3.

    Since B2 the Phase 4 repair adapters are also wired here: the same
    ``llama-server`` router now serves the repair/re-gate/re-check/re-audit
    calls through the backend boundary (``build_repair_adapters``), so local
    and remote profiles run the identical Phase 4 algorithm.
    """
    backend = StrictBackendConfig(
        # V4.1 §3.4: sycl-edge build (reasoning-budget 2048 works; MTP off).
        exe=Path(r"C:\src\llama-sycl-edge\build\bin\llama-server.exe"),
        device="SYCL0", host=args.host,
        model_paths={"gemma": GEMMA_PATH, "qwen": QWEN_PATH},
        model_names={"gemma": GEMMA_PATH.name, "qwen": QWEN_PATH.name},
        # V4.1 A2 review fix: the Gemma server args are DERIVED from the
        # selected reasoning (budget 2048 for --reasoning>0 per §3.4, 0 for
        # the B1 baseline), so CLI/config identity, server args and the
        # actual transport always agree — a default --reasoning 0 run never
        # starts the server with a reasoning budget the identity denies.
        server_args={
            "gemma": _gemma_server_args_for_reasoning(args.reasoning),
            "qwen": QWEN_SERVER_ARGS,
        },
        port=args.port, startup_timeout=args.startup_timeout, unload_timeout=args.unload_timeout,
    )
    # V4.1 A2: local no longer blocks --reasoning > 0 — the Gemma reasoning
    # budget is transported via the server args (--reasoning-budget 2048),
    # not request_options (validate_reasoning_backend accepts local now).
    validate_reasoning_backend(args.reasoning, backend)
    # F3 (B3 review): when the B3 audit will run, the local Qwen profile
    # must be B3-capable (MTP draft, reasoning 8192, context 49k) or the
    # run fails loudly — never silently audits with a non-B3 server.
    _validate_b3_qwen_profile(args, backend)
    cfg = _build_run_config(args, backend)
    args.out_dir.mkdir(parents=True, exist_ok=True)
    bible_text = _load_bible_text(args.memory_dir, args.chapter_id)
    # A2 review fix (whole-chapter retry ownership): in whole-chapter mode
    # the GENERATION layer (WholeChapterRetryPolicy) is the single retry
    # owner — the adapter-level JSON retry (JsonRetryPolicy) is disabled for
    # the run so total model attempts stay exactly
    # WholeChapterRetryPolicy.max_attempts (see build_strict_lifecycle).
    # The chunked path keeps the default adapter budget (None -> max_retries=2).
    json_retry = JsonRetryPolicy(max_retries=0) if args.whole_chapter else None
    router, model_caller, qwen_evaluator, gemma_selector, \
        qwen_audit_evaluator, gemma_audit_evaluator = build_strict_lifecycle(
            backend, log_dir=args.out_dir / "server_logs", bible_text=bible_text,
            json_retry_policy=json_retry,
        )
    runtime = LocalLifecycleCoordinator(router, descriptor=backend.build_descriptor())
    repair_adapters = build_repair_adapters(backend, runtime, bible_text=bible_text)
    b3_audit_repair = _build_b3_audit_repair(cfg, backend, runtime)
    progress = PhaseProgressWriter(cfg.out_dir)
    result = run_chapter_strict(
        cfg, runtime=runtime, model_caller=model_caller,
        qwen_evaluator=qwen_evaluator, gemma_selector=gemma_selector,
        qwen_audit_evaluator=qwen_audit_evaluator,
        gemma_audit_evaluator=gemma_audit_evaluator,
        repair_adapters=repair_adapters,
        b3_audit_repair=b3_audit_repair,
        progress=progress,
    )
    progress.close()
    _log_result(result)
    return _cli_exit_code(result)


def run_with_runtime_config(args: argparse.Namespace) -> int:
    """Generic backend path: load profile -> runtime -> role adapters."""
    backend = _load_runtime_config_file(args.runtime_config)
    if args.managed_server:
        backend = force_managed(backend)
    # V4.1 A2: local no longer blocks --reasoning > 0 — reasoning for local
    # is transported via server args (--reasoning-budget), not
    # request_options; validate_reasoning_backend accepts local now.
    validate_reasoning_backend(args.reasoning, backend)
    # F3 (B3 review): a local runtime profile that will run the B3 audit
    # must be B3-capable (MTP draft, reasoning 8192, context 49k) — fail
    # loudly instead of silently auditing with a non-B3 server profile.
    _validate_b3_qwen_profile(args, backend)
    _warn_remote_acknowledgement(backend)
    args.out_dir.mkdir(parents=True, exist_ok=True)
    bible_text = _load_bible_text(args.memory_dir, args.chapter_id)
    runtime = backend.build_runtime(log_dir=args.out_dir / "server_logs")
    # A2 review fix (whole-chapter retry ownership): in whole-chapter mode the
    # GENERATION layer (WholeChapterRetryPolicy) is the single retry owner —
    # it already retries every failure class (empty/truncated JSON included)
    # with its own bounded budget. The adapter-level JSON retry
    # (JsonRetryPolicy) is disabled for the whole run so total model attempts
    # stay exactly WholeChapterRetryPolicy.max_attempts instead of
    # max_attempts × adapter-budget, and an adapter-budget exhaustion cannot
    # re-enter the generation loop as a fresh "attempt 1/3".
    json_retry = JsonRetryPolicy(max_retries=0) if args.whole_chapter else None
    model_caller, qwen_evaluator, gemma_selector, \
        qwen_audit_evaluator, gemma_audit_evaluator = build_role_adapters(
            backend, runtime, bible_text=bible_text, json_retry_policy=json_retry,
        )
    repair_adapters = build_repair_adapters(backend, runtime, bible_text=bible_text)
    cfg = _build_run_config(args, backend)
    b3_audit_repair = _build_b3_audit_repair(cfg, backend, runtime)
    progress = PhaseProgressWriter(cfg.out_dir)
    result = run_chapter_strict(
        cfg, runtime=runtime, model_caller=model_caller,
        qwen_evaluator=qwen_evaluator, gemma_selector=gemma_selector,
        qwen_audit_evaluator=qwen_audit_evaluator,
        gemma_audit_evaluator=gemma_audit_evaluator,
        repair_adapters=repair_adapters,
        b3_audit_repair=b3_audit_repair,
        progress=progress,
    )
    progress.close()
    _log_result(result)
    return _cli_exit_code(result)


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_argparser().parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                         format="%(asctime)s %(levelname)s %(message)s")

    if args.runtime_config is not None:
        return run_with_runtime_config(args)
    return run_local_default(args)


if __name__ == "__main__":
    raise SystemExit(main())
