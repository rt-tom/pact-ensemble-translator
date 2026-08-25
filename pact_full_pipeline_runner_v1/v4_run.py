#!/usr/bin/env python3
"""Thin v4 dispatcher — book-first workflow with retained chapter mode.

Dispatches to the existing strict chapter and book entrypoints without
changing translated output, audit/formatting/markup semantics, runtime
routing, resume identity, or artifact layout for an equivalent invocation.

Book mode:
  python -m pact_full_pipeline_runner_v1.v4_run book --chapters 27-32 --runtime-config FILE

The range is validated and expanded (``27-32`` -> ``0027 0028 ... 0032``)
and an output directory ``D:\\pact\\gate_bench_runs/book_0027-0032_local|remote_<timestamp>``
is created automatically with the local/remote label derived from the
resolved runtime descriptor (profile defaults + explicit overrides), not a
user claim.

Profile defaults:
  The selected runtime profile is the source of truth for role models,
  reasoning, transport, and identity-bearing policy. Omitted
  ``--translator``/``--reviewer``/``--reasoning`` use profile values;
  explicit overrides are validated against the provider/runtime contract
  and are identity-bearing.

Preflight:
  Offline host-local preflight (paths/ports/env presence) runs by default
  before every configured execution and before any output directory is
  created. ``--preflight`` and ``--preflight --json`` (or
  ``--preflight-json``) are check-only modes that emit the sanitized
  resolved report and exit without pipeline, lifecycle, provider, source,
  or artifact side effects.

Markup:
  Only ``--markup preserve`` is accepted and documents the existing
  preservation/normalization policy. Unsupported values fail before startup.
"""
from __future__ import annotations

import argparse
import datetime
import re
import sys
from pathlib import Path
from typing import Optional, Sequence

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_DEFAULT_OUT_ROOT = Path("D:/pact/gate_bench_runs")
_TIMESTAMP_FMT = "%Y%m%d_%H%M%S"

# ---------------------------------------------------------------------------
# Help text — curated top-level, offline
# ---------------------------------------------------------------------------

_TOP_LEVEL_HELP = """\
v4 — unified v4 pipeline launcher (book-first)

Production runs are owner-started on RT only (D:\\pact\\pact_translator_v4_1).
Do not start pipelines from the media dev host or from worktrees.
Agents inspect code and artifacts only.

Usage:
  python -m pact_full_pipeline_runner_v1.v4_run book --chapters START-END --runtime-config FILE [options]
  python -m pact_full_pipeline_runner_v1.v4_run chapter --chapter-id ID --chapter-html FILE --memory-dir DIR --out-dir DIR [options]
  python -m pact_full_pipeline_runner_v1.v4_run --help
  python -m pact_full_pipeline_runner_v1.v4_run book --help
  python -m pact_full_pipeline_runner_v1.v4_run chapter --help

Modes:
  book      Primary workflow — sequential chapters sharing memory, cross-chapter promotion.
            Requires --chapters START-END and --runtime-config FILE.
  chapter   Single-chapter strict run (retained). Requires --chapter-id / --chapter-html / --memory-dir / --out-dir.

Book range and source-pattern resolution:
  --chapters START-END   Closed numeric range, e.g. 27-32 or 0027-0032. Validated before startup;
                         reversed or malformed ranges are rejected. Expanded to zero-padded
                         IDs (0027 0028 ... 0032) and resolved via --chapter-html-pattern
                         '{chapter_id}' (default: D:/pact/pact_chapters/{chapter_id}.html).

Automatic output naming:
  Each book run creates a distinct subdirectory below D:\\pact\\gate_bench_runs
  named book_0027-0032_local_<timestamp> or book_0027-0032_remote_<timestamp>.
  The local/remote label is derived from the resolved runtime descriptor after
  profile defaults and explicit overrides, not a user-supplied topology claim.

Runtime configuration — profile defaults and optional overrides:
  --runtime-config FILE    Tagged runtime profile (local_llama | opencode_server | composite).
                           The profile supplies default role models, reasoning, transport,
                           and identity-bearing policy. Without it the historical no-config
                           local CLI is used (compatibility, chapter mode only).
  --translator PROVIDER/ALIAS   Override Translator role model via providers.yaml (e.g. opencode-go/deepseek4flash).
  --reviewer PROVIDER/ALIAS     Override Reviewer role models via providers.yaml.
  --reasoning {0,1,2,3}         Override generation reasoning budget (profile default when omitted).
  Omitted selections use profile defaults and do not introduce launcher-specific quality defaults.
  Explicit overrides are validated against the runtime/provider contract, forwarded to the
  underlying entrypoint, and included in resolved identity/reporting. Aliases are
  case-insensitive and fail-closed on ambiguous duplicates.

Automatic offline preflight and check-only modes:
  Offline host-local preflight (profile syntax, local paths/ports, required env vars)
  runs by default before every configured execution — before any output directory is created
  or a pipeline starts. On failure the command exits with a clear error and no artifacts.
  --preflight              Validate and print a sanitized human-readable preflight report and exit.
  --preflight --json       Machine-readable JSON preflight report (or --preflight-json alias).
  --preflight-json         Alias for --preflight --json.
  Check-only modes do NOT start the pipeline, open a model session, contact a provider,
  submit source text, or create run artifacts. Remote endpoint preflight remains a separate
  transport check during actual execution.

Runtime/provider, topology and resume:
  Runtime/provider configuration is resolved through the profile and exposed via preflight
  (sanitized public record, model bindings, effective options, identity hash).
  Topology/resume choices (out-dir isolation, config identity invalidation on profile or
  override change) follow the existing strict/book semantics unchanged.
  Use a NEW --out-dir when changing profile, translator/reviewer, or reasoning.

Audit/formatting and markup:
  Audit/formatting behavior is unchanged from the underlying strict/book path.
  --markup preserve        Explicitly request the existing preservation/normalization policy.
                           This is the only accepted markup value; unsupported values are
                           rejected before startup and no new tag transformation is performed.

Safety and owner-run boundary:
  Production runs are owner-started on RT only. The launcher does not add hidden defaults,
  does not silently alter identity, and never starts providers or model servers in
  --help or --preflight modes.

Get mode-specific detail:
  python -m pact_full_pipeline_runner_v1.v4_run book --help
  python -m pact_full_pipeline_runner_v1.v4_run chapter --help
"""

_BOOK_HELP_EXTRA = """\
book mode — batch chapters sharing memory

Required:
  --chapters START-END       Closed range (e.g. 27-32). Validated and expanded to 0027 ... 0032.
  --runtime-config FILE      Tagged runtime profile (source of truth for models/reasoning/policy).

Optional (profile-aware):
  --translator PROVIDER/ALIAS
  --reviewer PROVIDER/ALIAS
  --reasoning {0,1,2,3}
  --markup preserve          Only 'preserve' is accepted.

Output / source:
  --chapter-html-pattern PATTERN   Pattern with {chapter_id} (default: D:/pact/pact_chapters/{chapter_id}.html)
  --memory-dir DIR                 (default: D:/pact/pact_chapters)
  --out-base DIR                   Overrides automatic D:\\pact\\gate_bench_runs/book_XXXX-XXXX_local|remote_<timestamp>
  Automatic output: D:\\pact\\gate_bench_runs/book_0027-0032_local|remote_<timestamp> (label from resolved descriptor).

Topology/resume (forwarded to strict per-chapter):
  --runtime-config FILE, --managed-server, --providers-config FILE;
  out-base / out-dir isolation; config/profile identity invalidation — use a NEW --out-base/--out-dir when changing profile or translator/reviewer/reasoning. Resume follows existing strict/book semantics.

Audit/formatting (forwarded to strict per-chapter):
  --run-audit / --skip-audit, --entity-context / --no-entity-context, --no-russian-editor;
  formatting and audit behavior unchanged; --markup preserve only.

Whole-chapter/generation (forwarded to strict per-chapter):
  --whole-chapter, --stop-after-generation, --lazy-balanced / --no-lazy-balanced, --reasoning, --mixed-script-allow, --arc-names.

Preflight:
  Automatic offline preflight before execution; --preflight / --preflight --json check-only modes.

Forwarding:
  Remaining arguments are forwarded to the existing book-run entrypoint (v4_book_run.main) and strict chapter options.
  Use --help to see the underlying parser's full option list after this summary.
"""

_CHAPTER_HELP_EXTRA = """\
chapter mode — single strict chapter run (retained)

Required (strict CLI):
  --chapter-id ID
  --chapter-html FILE
  --memory-dir DIR
  --out-dir DIR

Optional:
  --runtime-config FILE
  --translator / --reviewer / --reasoning overrides (profile-aware, see top-level help)
  --markup preserve

Preflight: automatic offline preflight before configured execution; --preflight / --json check-only.

Forwarding: remaining arguments are forwarded to the existing strict chapter entrypoint
            (v4_phase12_strict_run.main).
"""

# ---------------------------------------------------------------------------
# Range helpers
# ---------------------------------------------------------------------------

_RANGE_RE = re.compile(r"^\s*0*(\d+)\s*-\s*0*(\d+)\s*$")


def parse_range(text: str) -> tuple[int, int]:
    """Parse START-END range, return (start, end) as ints. Fail with ValueError on invalid."""
    if text is None or not str(text).strip():
        raise ValueError(f"chapter range must be START-END, got {text!r}")
    m = _RANGE_RE.match(str(text))
    if not m:
        raise ValueError(
            f"invalid chapter range {text!r}: expected START-END (e.g. 27-32 or 0027-0032)"
        )
    try:
        start = int(m.group(1))
        end = int(m.group(2))
    except ValueError as exc:
        raise ValueError(f"invalid chapter range {text!r}: non-numeric") from exc
    if start < 1 or end < 1:
        raise ValueError(f"invalid chapter range {text!r}: chapters start at 1")
    if start > end:
        raise ValueError(f"invalid chapter range {text!r}: start > end (reversed range)")
    if end - start > 500:
        raise ValueError(f"invalid chapter range {text!r}: range too large (>500 chapters)")
    return start, end


def expand_range(start: int, end: int) -> list[str]:
    return [f"{i:04d}" for i in range(start, end + 1)]


def range_label(start: int, end: int) -> str:
    return f"{start:04d}-{end:04d}"


# ---------------------------------------------------------------------------
# Runtime profile helpers (thin wrappers over runtime_config)
# ---------------------------------------------------------------------------

def _load_runtime_config_file(path: Path):
    raw = path.read_text(encoding="utf-8")
    if path.suffix.lower() in (".yaml", ".yml"):
        try:
            import yaml  # type: ignore
        except ImportError as exc:
            raise ValueError(f"{path}: YAML profile requires PyYAML (pip install pyyaml)") from exc
        payload = yaml.safe_load(raw)
    else:
        import json
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            try:
                import yaml  # type: ignore
            except ImportError as exc:
                raise ValueError(f"{path}: looks like YAML but PyYAML not installed") from exc
            payload = yaml.safe_load(raw)
    if not isinstance(payload, dict):
        raise ValueError(f"{path}: runtime config must be a mapping")
    from pact_v4.runtime.runtime_config import load_runtime_config
    return load_runtime_config(payload)


def _apply_overrides(cfg, translator: Optional[str], reviewer: Optional[str], reasoning: Optional[int], providers_config: Optional[str | Path] = None):
    # Apply provider aliases first — must use effective providers registry (honors --providers-config)
    if translator or reviewer:
        from pact_v4.runtime.runtime_config import load_providers_registry, apply_provider_flags
        if providers_config is not None:
            prov_path = Path(providers_config)
        else:
            prov_path = Path(__file__).resolve().parent.parent / "configs" / "providers.yaml"
        registry = load_providers_registry(prov_path)
        cfg = apply_provider_flags(cfg, registry, translator=translator, reviewer=reviewer)
    if reasoning is not None:
        # Validate reasoning value
        if reasoning not in (0, 1, 2, 3):
            raise ValueError(f"--reasoning must be 0-3, got {reasoning!r}")
        # Apply reasoning override via helper from strict run (mirrors runtime_config logic)
        # Use runtime_config helpers directly
        from pact_v4.runtime.runtime_config import LocalLlamaBackendConfig, OpenCodeBackendConfig, CompositeBackendConfig
        from dataclasses import replace
        # Reuse logic from v4_phase12_strict_run._with_reasoning_override if available
        try:
            from pact_full_pipeline_runner_v1.v4_phase12_strict_run import _with_reasoning_override as _wro
            cfg = _wro(cfg, reasoning)
        except Exception:
            # Fallback minimal reasoning override for opencode
            if isinstance(cfg, OpenCodeBackendConfig):
                cfg = replace(cfg, server=replace(cfg.server, reasoning=int(reasoning)))
            elif isinstance(cfg, CompositeBackendConfig):
                # For composite, override generator sub-backend
                # Simple fallback: override any opencode sub-backend
                new_backends = dict(cfg.backends)
                for n, sub in new_backends.items():
                    if isinstance(sub, OpenCodeBackendConfig):
                        new_backends[n] = replace(sub, server=replace(sub.server, reasoning=int(reasoning)))
                        break
                cfg = replace(cfg, backends=new_backends)
            elif isinstance(cfg, LocalLlamaBackendConfig):
                # Local reasoning via server_args: need to adjust gemma args
                try:
                    from pact_full_pipeline_runner_v1.v4_phase12_strict_run import _gemma_server_args_for_reasoning
                    new_args = dict(cfg.server_args)
                    new_args["gemma"] = _gemma_server_args_for_reasoning(int(reasoning))
                    cfg = replace(cfg, server_args=new_args)
                except Exception:
                    pass
    return cfg


def _derive_label(cfg) -> str:
    """Return 'local' for LocalLlamaBackendConfig, 'remote' otherwise."""
    from pact_v4.runtime.runtime_config import LocalLlamaBackendConfig
    if isinstance(cfg, LocalLlamaBackendConfig):
        return "local"
    # OpenCode and Composite => remote (composite may contain local but identity is composite)
    kind = getattr(cfg.build_descriptor(), "kind", "")
    if kind == "local_llama":
        return "local"
    return "remote"


def _timestamp() -> str:
    # Include microseconds for finer granularity than seconds-only prefix
    return datetime.datetime.now().strftime("%Y%m%d_%H%M%S_%f")


def _allocate_book_out_dir(root: Path, label_range: str, label: str) -> Path:
    """Allocate a collision-safe book output directory under root.

    Uses timestamp with microseconds and retries with a counter suffix if the
    directory already exists (same-second concurrent runs). Uses exist_ok=False
    so reuse is never silent.
    """
    base = root / f"book_{label_range}_{label}_{_timestamp()}"
    attempt = base
    counter = 0
    while True:
        try:
            attempt.mkdir(parents=True, exist_ok=False)
            return attempt
        except FileExistsError:
            counter += 1
            # Keep original timestamp, add numeric suffix
            attempt = Path(f"{base}_{counter}")
            if counter > 1000:
                # Extra safety: use uuid fallback
                import uuid
                attempt = root / f"book_{label_range}_{label}_{_timestamp()}_{uuid.uuid4().hex[:8]}"
                attempt.mkdir(parents=True, exist_ok=False)
                return attempt


def _apply_managed(cfg, managed_flag: bool):
    """Apply --managed-server override to cfg if requested, validating early.

    Mirrors pact_full_pipeline_runner_v1.v4_phase12_strict_run.force_managed
    so dispatcher preflight uses the effective delegated config.
    Local_llama with managed-server is a validation error and must fail before artifacts.
    """
    if not managed_flag:
        return cfg
    from pact_full_pipeline_runner_v1.v4_phase12_strict_run import force_managed
    return force_managed(cfg)


# ---------------------------------------------------------------------------
# Validation helpers
# ---------------------------------------------------------------------------

def _validate_markup(value: Optional[str]) -> None:
    if value is None:
        return
    if value != "preserve":
        print(f"error: --markup {value!r} is not supported; only --markup preserve is accepted", file=sys.stderr)
        sys.exit(2)


def _error_exit(msg: str, code: int = 2) -> None:
    print(f"error: {msg}", file=sys.stderr)
    sys.exit(code)


# ---------------------------------------------------------------------------
# Book and chapter dispatch
# ---------------------------------------------------------------------------

def _handle_book(argv: Sequence[str]) -> int:
    # Minimal arg parsing for book mode, forwarding remainder.
    parser = argparse.ArgumentParser(prog="v4_run book", add_help=False)
    parser.add_argument("--chapters", required=False, default=None)
    parser.add_argument("--runtime-config", dest="runtime_config", required=False, default=None)
    parser.add_argument("--profile", dest="profile", required=False, default=None)
    parser.add_argument("--chapter-html-pattern", dest="chapter_html_pattern", required=False, default=None)
    parser.add_argument("--memory-dir", dest="memory_dir", required=False, default=None)
    parser.add_argument("--out-base", dest="out_base", required=False, default=None)
    parser.add_argument("--translator", required=False, default=None)
    parser.add_argument("--reviewer", required=False, default=None)
    parser.add_argument("--reasoning", type=int, required=False, default=None)
    parser.add_argument("--markup", required=False, default=None)
    parser.add_argument("--preflight", action="store_true", default=False)
    parser.add_argument("--preflight-json", action="store_true", default=False)
    parser.add_argument("--json", action="store_true", default=False)
    parser.add_argument("-h", "--help", action="store_true", default=False)
    # Accept --managed-server forwarding? include for completeness
    parser.add_argument("--managed-server", action="store_true", default=False)
    parser.add_argument("--providers-config", required=False, default=None)

    args, remaining = parser.parse_known_args(argv)

    if args.help:
        # Curated extra plus delegated parser help — include both book and forwarded strict options
        print(_BOOK_HELP_EXTRA)
        # Also print underlying book parser help for source-of-truth
        try:
            from pact_full_pipeline_runner_v1.v4_book_run import build_argparser as _book_parser
            _book_parser().print_help()
        except Exception:
            pass
        # Forwarded strict operational options (topology/resume, audit/formatting, whole-chapter)
        try:
            from pact_full_pipeline_runner_v1.v4_phase12_strict_run import build_argparser as _strict_parser
            print("\n--- Forwarded strict per-chapter options (topology/resume, audit/formatting, whole-chapter) ---")
            _strict_parser().print_help()
        except Exception:
            pass
        return 0

    _validate_markup(args.markup)
    if args.json and not args.preflight and not args.preflight_json:
        _error_exit("--json requires --preflight (use --preflight --json or --preflight-json)")

    # Require range and runtime profile
    profile_path = args.runtime_config or args.profile
    if not args.chapters:
        _error_exit("--chapters START-END is required for book mode (e.g. --chapters 27-32)")
    if not profile_path:
        _error_exit("--runtime-config FILE is required for book mode")

    # Parse range
    try:
        start, end = parse_range(args.chapters)
    except ValueError as exc:
        _error_exit(str(exc))
    chapter_ids = expand_range(start, end)
    label_range = range_label(start, end)

    # Load profile and apply overrides
    cfg_path = Path(profile_path)
    if not cfg_path.is_file():
        _error_exit(f"runtime profile not found: {cfg_path}")
    try:
        cfg = _load_runtime_config_file(cfg_path)
    except Exception as exc:
        _error_exit(f"invalid runtime profile {cfg_path}: {exc}")

    try:
        cfg = _apply_overrides(cfg, args.translator, args.reviewer, args.reasoning, providers_config=args.providers_config)
        cfg = _apply_managed(cfg, bool(args.managed_server))
    except Exception as exc:
        _error_exit(str(exc))

    # Reasoning for preflight (resolve effective)
    effective_reasoning: Optional[int] = args.reasoning
    if effective_reasoning is None:
        # Derive from cfg if profile bears reasoning
        try:
            from pact_v4.runtime.runtime_config import OpenCodeBackendConfig, LocalLlamaBackendConfig
            if isinstance(cfg, OpenCodeBackendConfig) and cfg.server.reasoning is not None:
                effective_reasoning = int(cfg.server.reasoning)
            elif isinstance(cfg, LocalLlamaBackendConfig):
                # Keep None -> preflight will treat as 0 baseline
                effective_reasoning = None
        except Exception:
            pass

    # Preflight (check-only or default gate) — uses effective cfg with managed-server applied
    from pact_v4.runtime.runtime_config import run_runtime_preflight
    # Check-only modes — support --preflight, --preflight --json, --preflight-json, --preflight-json --json
    is_check_only = bool(args.preflight or args.preflight_json)
    is_json = bool(args.json or args.preflight_json)
    if is_check_only:
        report = run_runtime_preflight(cfg, reasoning=effective_reasoning if effective_reasoning is not None else None)
        if is_json:
            print(report.to_json())
        else:
            print(report.format_human())
        return 0 if report.ok else 1

    # Default preflight gate — before any output dir creation
    report = run_runtime_preflight(cfg, reasoning=effective_reasoning if effective_reasoning is not None else None)
    if not report.ok:
        # Emit report to stderr and fail
        print(report.format_human(), file=sys.stderr)
        _error_exit(f"offline preflight failed — refusing to start book run (see report above)", code=3)

    # Derive label after overrides and preflight success
    try:
        label = _derive_label(cfg)
    except Exception as exc:
        _error_exit(f"cannot derive local/remote label from runtime descriptor: {exc}")

    if label not in ("local", "remote"):
        _error_exit(f"unknown runtime descriptor label {label!r}; expected local or remote")

    # Automatic output directory — collision-safe
    if args.out_base:
        out_base = Path(args.out_base)
        try:
            out_base.mkdir(parents=True, exist_ok=True)
        except Exception as exc:
            _error_exit(f"cannot create output directory {out_base}: {exc}")
    else:
        import os
        root_env = os.environ.get("PACT_V4_OUT_ROOT")
        root = Path(root_env) if root_env else _DEFAULT_OUT_ROOT
        try:
            out_base = _allocate_book_out_dir(root, label_range, label)
        except Exception as exc:
            _error_exit(f"cannot create output directory {root}: {exc}")

    # Defaults for forwarded required args
    chapter_html_pattern = args.chapter_html_pattern or "D:/pact/pact_chapters/{chapter_id}.html"
    memory_dir = args.memory_dir or "D:/pact/pact_chapters"

    # Build delegated argv for v4_book_run.main
    delegated: list[str] = []
    delegated += ["--chapters"] + chapter_ids
    delegated += ["--chapter-html-pattern", chapter_html_pattern]
    delegated += ["--memory-dir", str(memory_dir)]
    delegated += ["--out-base", str(out_base)]
    # Forward profile and overrides as extra_args (book_run forwards to strict per chapter)
    delegated += ["--runtime-config", str(cfg_path)]
    if args.translator:
        delegated += ["--translator", args.translator]
    if args.reviewer:
        delegated += ["--reviewer", args.reviewer]
    if args.reasoning is not None:
        delegated += ["--reasoning", str(args.reasoning)]
    # --markup preserve is consumed as a guard; do not forward unsupported syntax
    if args.managed_server:
        delegated += ["--managed-server"]
    if args.providers_config:
        delegated += ["--providers-config", str(args.providers_config)]
    # Forward remaining unknown args (e.g. --whole-chapter, --skip-audit, etc.)
    delegated += list(remaining)

    # Log sanitized preflight summary for auditability (before pipeline)
    print(report.format_human())

    from pact_full_pipeline_runner_v1.v4_book_run import main as book_main
    return int(book_main(delegated))


def _handle_chapter(argv: Sequence[str]) -> int:
    parser = argparse.ArgumentParser(prog="v4_run chapter", add_help=False)
    parser.add_argument("--runtime-config", dest="runtime_config", required=False, default=None)
    parser.add_argument("--profile", dest="profile", required=False, default=None)
    parser.add_argument("--translator", required=False, default=None)
    parser.add_argument("--reviewer", required=False, default=None)
    parser.add_argument("--reasoning", type=int, required=False, default=None)
    parser.add_argument("--markup", required=False, default=None)
    parser.add_argument("--preflight", action="store_true", default=False)
    parser.add_argument("--preflight-json", action="store_true", default=False)
    parser.add_argument("--json", action="store_true", default=False)
    parser.add_argument("--managed-server", action="store_true", default=False)
    parser.add_argument("--providers-config", required=False, default=None)
    parser.add_argument("-h", "--help", action="store_true", default=False)
    args, remaining = parser.parse_known_args(argv)

    if args.help:
        print(_CHAPTER_HELP_EXTRA)
        try:
            from pact_full_pipeline_runner_v1.v4_phase12_strict_run import build_argparser as _strict_parser
            _strict_parser().print_help()
        except Exception:
            pass
        return 0

    _validate_markup(args.markup)
    if args.json and not args.preflight and not args.preflight_json:
        _error_exit("--json requires --preflight (use --preflight --json or --preflight-json)")

    # Preflight handling if runtime-config present — unify check-only detection
    profile_path = args.runtime_config or args.profile
    # Detect check-only early: any of --preflight, --preflight-json, or --preflight+--json
    is_check_only = bool(args.preflight or args.preflight_json)
    # Note: bare --json without --preflight is not a check-only trigger
    if profile_path and is_check_only:
        cfg_path = Path(profile_path)
        if not cfg_path.is_file():
            _error_exit(f"runtime profile not found: {cfg_path}")
        try:
            cfg = _load_runtime_config_file(cfg_path)
            cfg = _apply_overrides(cfg, args.translator, args.reviewer, args.reasoning, providers_config=args.providers_config)
            cfg = _apply_managed(cfg, bool(args.managed_server))
        except Exception as exc:
            _error_exit(str(exc))
        effective_reasoning = args.reasoning
        from pact_v4.runtime.runtime_config import run_runtime_preflight
        report = run_runtime_preflight(cfg, reasoning=effective_reasoning if effective_reasoning is not None else None)
        is_json = bool(args.json or args.preflight_json)
        if is_json:
            print(report.to_json())
        else:
            print(report.format_human())
        return 0 if report.ok else 1

    # Default preflight gate before delegation if configured
    if profile_path:
        cfg_path = Path(profile_path)
        if not cfg_path.is_file():
            _error_exit(f"runtime profile not found: {cfg_path}")
        try:
            cfg = _load_runtime_config_file(cfg_path)
            cfg = _apply_overrides(cfg, args.translator, args.reviewer, args.reasoning, providers_config=args.providers_config)
            cfg = _apply_managed(cfg, bool(args.managed_server))
        except Exception as exc:
            _error_exit(str(exc))
        effective_reasoning = args.reasoning
        from pact_v4.runtime.runtime_config import run_runtime_preflight
        report = run_runtime_preflight(cfg, reasoning=effective_reasoning if effective_reasoning is not None else None)
        if is_check_only:
            is_json = bool(args.json or args.preflight_json)
            if is_json:
                print(report.to_json())
            else:
                print(report.format_human())
            return 0 if report.ok else 1
        if not report.ok:
            print(report.format_human(), file=sys.stderr)
            _error_exit("offline preflight failed — refusing to start chapter run", code=3)
        print(report.format_human())

        # Build delegated argv: include original profile and overrides plus remaining
        # --markup preserve is consumed as a guard; do not forward unsupported syntax
        delegated = []
        delegated += ["--runtime-config", str(cfg_path)]
        if args.translator:
            delegated += ["--translator", args.translator]
        if args.reviewer:
            delegated += ["--reviewer", args.reviewer]
        if args.reasoning is not None:
            delegated += ["--reasoning", str(args.reasoning)]
        if args.managed_server:
            delegated += ["--managed-server"]
        if args.providers_config:
            delegated += ["--providers-config", str(args.providers_config)]
        delegated += list(remaining)
        from pact_full_pipeline_runner_v1.v4_phase12_strict_run import main as strict_main
        return int(strict_main(delegated))

    # No profile: delegate directly (no-config compatibility) — markup guard consumed
    # Forward reasoning/translator/reviewer unchanged to preserve retained legacy semantics
    # and fail-closed validation in strict CLI (invalid provider overrides must error).
    delegated = []
    if args.translator:
        delegated += ["--translator", args.translator]
    if args.reviewer:
        delegated += ["--reviewer", args.reviewer]
    if args.reasoning is not None:
        delegated += ["--reasoning", str(args.reasoning)]
    if args.managed_server:
        delegated += ["--managed-server"]
    if args.providers_config:
        delegated += ["--providers-config", str(args.providers_config)]
    delegated += list(remaining)
    # Also need to handle case where --preflight without profile was requested -> error
    if args.preflight or args.preflight_json:
        _error_exit("--preflight requires --runtime-config")
    from pact_full_pipeline_runner_v1.v4_phase12_strict_run import main as strict_main
    return int(strict_main(delegated))


# ---------------------------------------------------------------------------
# Top-level entry
# ---------------------------------------------------------------------------

def main(argv: Optional[Sequence[str]] = None) -> int:
    if argv is None:
        argv = sys.argv[1:]

    # Top-level --help or no args
    if not argv or argv == ["--help"] or argv == ["-h"]:
        print(_TOP_LEVEL_HELP)
        return 0
    # Handle --help as first arg with no subcommand
    if "--help" in argv and not any(x in argv for x in ("book", "chapter")):
        # Check if it's top-level help request
        if argv[0] in ("--help", "-h"):
            print(_TOP_LEVEL_HELP)
            return 0

    # Subcommand dispatch
    if argv[0] == "book":
        # Support book --help
        return _handle_book(argv[1:])
    if argv[0] == "chapter":
        return _handle_chapter(argv[1:])

    # No subcommand but contains --preflight etc -> treat as error with help hint
    if argv[0].startswith("-"):
        print(_TOP_LEVEL_HELP)
        # Also indicate unknown invocation
        if "--help" not in argv and "-h" not in argv:
            _error_exit(f"unknown command {argv[0]!r}; expected 'book' or 'chapter' (try --help)")
        return 0

    _error_exit(f"unknown command {argv[0]!r}; expected 'book' or 'chapter'")


if __name__ == "__main__":
    raise SystemExit(main())
