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
from dataclasses import replace
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence

from pact_v4.pipeline.v4_phase12_strict_runner import (
    StrictBackendConfig,
    StrictRunConfig,
    build_strict_lifecycle,
    run_chapter_strict,
)
from pact_v4.runtime.runtime_config import (
    CompositeBackendConfig,
    LocalLlamaBackendConfig,
    OpenCodeBackendConfig,
    build_repair_adapters,
    build_role_adapters,
    load_runtime_config,
)
from pact_v4.runtime.runtime_coordinator import LocalLifecycleCoordinator

LOG = logging.getLogger("v4_phase12_strict_run")

LLAMA_ROOT = Path(r"C:\llama-cpp")
GEMMA_PATH = LLAMA_ROOT / "models" / "gemma-4-26B-A4B-it-UD-Q4_K_XL.gguf"
GEMMA_DRAFT_PATH = LLAMA_ROOT / "models" / "MTP" / "mtp-gemma-4-26B-A4B-it-Q8_0.gguf"
QWEN_PATH = LLAMA_ROOT / "models" / "Qwen3.6-35B-A3B" / "Qwen3.6-35B-A3B-UD-Q4_K_XL.gguf"

# Same validated SYCL profile as Measurement 2
# (docs/plans/V4_SINGLE_RESIDENT_DRIVER_ARCHITECTURE_RU.md, "Результат
# измерения 2") and the user's optimized production command line, so
# lifecycle numbers from this real chapter trial are comparable to that
# synthetic benchmark's.
CONTEXT_SIZE = 32768
GEMMA_SERVER_ARGS = [
    "--model-draft", str(GEMMA_DRAFT_PATH),
    "--spec-type", "draft-mtp",
    "--spec-draft-n-max", "4",
    "-ngl", "99",
    "-ncmoe", "18",
    "--load-mode", "mmap",
    "--reasoning-budget", "0",
    "-np", "1",
    "-c", str(CONTEXT_SIZE),
    "-fa", "on",
    "--jinja",
    "--cache-ram", "0",
    "--ctx-checkpoints", "0",
]
QWEN_SERVER_ARGS = [
    "-fit", "on",
    "-fitt", "1280",
    "-b", "2048",
    "-ub", "512",
    "-ctk", "q8_0",
    "-ctv", "q8_0",
    "-t", "6",
    "-tb", "12",
    "--load-mode", "mmap",
    "--reasoning-budget", "0",
    "-np", "1",
    "-c", str(CONTEXT_SIZE),
    "-fa", "on",
    "--jinja",
    "--cache-ram", "0",
    "--ctx-checkpoints", "0",
]


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
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8094)
    p.add_argument("--max-consecutive-nonselections", type=int, default=3)
    p.add_argument("--startup-timeout", type=float, default=240.0)
    p.add_argument("--unload-timeout", type=float, default=30.0)
    p.add_argument("--runtime-config", type=Path, default=None, metavar="FILE",
                    help="YAML/JSON tagged runtime profile (kind local_llama | "
                         "opencode_server | composite). When absent the historical "
                         "local llama-server profile is used unchanged.")
    p.add_argument("--managed-server", action="store_true",
                    help="Start Pact's own 'opencode serve' for every OpenCode "
                         "backend in the runtime config (server_mode=managed) and "
                         "stop it after the run. Ignored without --runtime-config.")
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


def _build_run_config(args: argparse.Namespace, backend: Any) -> StrictRunConfig:
    return StrictRunConfig(
        chapter_id=args.chapter_id, chapter_html_path=args.chapter_html, memory_dir=args.memory_dir,
        out_dir=args.out_dir, backend=backend,
        max_consecutive_terminal_nonselections=args.max_consecutive_nonselections,
        run_label="v4-phase12-strict-chapter-trial",
    )


def run_local_default(args: argparse.Namespace) -> int:
    """The historical local-only path -- unchanged from before C3.

    Since B2 the Phase 4 repair adapters are also wired here: the same
    ``llama-server`` router now serves the repair/re-gate/re-check/re-audit
    calls through the backend boundary (``build_repair_adapters``), so local
    and remote profiles run the identical Phase 4 algorithm.
    """
    backend = StrictBackendConfig(
        exe=Path(r"C:\llama-sycl-new\llama-server.exe"), device="SYCL0", host=args.host,
        model_paths={"gemma": GEMMA_PATH, "qwen": QWEN_PATH},
        model_names={"gemma": GEMMA_PATH.name, "qwen": QWEN_PATH.name},
        server_args={"gemma": GEMMA_SERVER_ARGS, "qwen": QWEN_SERVER_ARGS},
        port=args.port, startup_timeout=args.startup_timeout, unload_timeout=args.unload_timeout,
    )
    cfg = _build_run_config(args, backend)
    router, model_caller, qwen_evaluator, gemma_selector, \
        qwen_audit_evaluator, gemma_audit_evaluator = build_strict_lifecycle(
            backend, log_dir=args.out_dir / "server_logs",
        )
    runtime = LocalLifecycleCoordinator(router, descriptor=backend.build_descriptor())
    repair_adapters = build_repair_adapters(backend, runtime)
    result = run_chapter_strict(
        cfg, runtime=runtime, model_caller=model_caller,
        qwen_evaluator=qwen_evaluator, gemma_selector=gemma_selector,
        qwen_audit_evaluator=qwen_audit_evaluator,
        gemma_audit_evaluator=gemma_audit_evaluator,
        repair_adapters=repair_adapters,
    )
    _log_result(result)
    return 0 if not result.halted_early else 2


def run_with_runtime_config(args: argparse.Namespace) -> int:
    """Generic backend path: load profile -> runtime -> role adapters."""
    backend = _load_runtime_config_file(args.runtime_config)
    if args.managed_server:
        backend = force_managed(backend)
    _warn_remote_acknowledgement(backend)
    args.out_dir.mkdir(parents=True, exist_ok=True)
    runtime = backend.build_runtime(log_dir=args.out_dir / "server_logs")
    model_caller, qwen_evaluator, gemma_selector, \
        qwen_audit_evaluator, gemma_audit_evaluator = build_role_adapters(
            backend, runtime,
        )
    repair_adapters = build_repair_adapters(backend, runtime)
    cfg = _build_run_config(args, backend)
    result = run_chapter_strict(
        cfg, runtime=runtime, model_caller=model_caller,
        qwen_evaluator=qwen_evaluator, gemma_selector=gemma_selector,
        qwen_audit_evaluator=qwen_audit_evaluator,
        gemma_audit_evaluator=gemma_audit_evaluator,
        repair_adapters=repair_adapters,
    )
    _log_result(result)
    return 0 if not result.halted_early else 2


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_argparser().parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                         format="%(asctime)s %(levelname)s %(message)s")

    if args.runtime_config is not None:
        return run_with_runtime_config(args)
    return run_local_default(args)


if __name__ == "__main__":
    raise SystemExit(main())
