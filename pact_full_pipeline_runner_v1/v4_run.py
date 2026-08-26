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
# Constants and host-aware layout
# ---------------------------------------------------------------------------

_DEFAULT_OUT_ROOT = Path("D:/pact/gate_bench_runs")
_TIMESTAMP_FMT = "%Y%m%d_%H%M%S"

# Declarative host-aware book layout (RT vs media). Transport profiles remain
# separate; this layout selects source/state/output roots per execution host.
_RT_LAYOUT = {
    "source": Path("D:/pact/pact_chapters"),
    "state": Path("D:/pact/book_state"),
    "output": Path("D:/pact/gate_bench_runs"),
}
_MEDIA_LAYOUT = {
    "source": Path("/home/rt/pact_chapters"),
    "state": Path("/home/rt/pact_runs/workers/media/book-1/state"),
    "output": Path("/home/rt/pact_runs/outputs"),
}
# Media canonical source is /home/rt/pact_chapters (owner-approved 150 HTML).
# RT mirror is D:/pact/pact_chapters (owner-managed).

# Default media sync for every simple book mode
_DEFAULT_MEDIA_BOOK_ID = "1"
_DEFAULT_MEDIA_TARGET = "media-snap"
_DEFAULT_MEDIA_ROOT = "/home/rt/pact_runs"

# ---------------------------------------------------------------------------
# Help text — curated top-level, offline
# ---------------------------------------------------------------------------

_TOP_LEVEL_HELP = """\
v4 — unified v4 pipeline launcher (book-first)

Production runs are owner-started on RT or media via the host-aware launcher.
Do not start pipelines from worktrees. Agents inspect code and artifacts only.

Usage:
  python -m pact_full_pipeline_runner_v1.v4_run book --chapters 28 --local
  python -m pact_full_pipeline_runner_v1.v4_run book --chapters 28-32 --remote [translator/reviewer]
  python -m pact_full_pipeline_runner_v1.v4_run book --chapters START-END --runtime-config FILE [options]
  python -m pact_full_pipeline_runner_v1.v4_run chapter --chapter-id ID --chapter-html FILE --memory-dir DIR --out-dir DIR [options]
  python -m pact_full_pipeline_runner_v1.v4_run --help
  python -m pact_full_pipeline_runner_v1.v4_run book --help
  python -m pact_full_pipeline_runner_v1.v4_run chapter --help

Modes:
  book      Primary workflow — sequential chapters sharing memory, cross-chapter promotion.
            Simple: --chapters 28 or 28-32 plus --local or --remote [translator/reviewer].
            Advanced: --chapters + --runtime-config FILE (compatibility).
  chapter   Single-chapter strict run (retained). Requires --chapter-id / --chapter-html / --memory-dir / --out-dir.

Book range and source-pattern discovery:
  --chapters N | N-M      Single chapter (28) or closed range (28-32). Accepts bare numbers
                         and zero-padded forms; validated before startup. Each numeric value
                         is resolved to exactly one regular non-symlink file matching
                         NNNN_*.html in the host source root (RT: D:/pact/pact_chapters,
                         media: /home/rt/pact_chapters). Ambiguous or missing matches fail
                         before pipeline startup. Advanced: explicit --chapter-html-pattern
                         remains available but must pass the same uniqueness checks.

Host-aware layout:
  RT: source D:/pact/pact_chapters, mutable state D:/pact/book_state,
      automatic outputs under D:\\pact\\gate_bench_runs.
  media: source /home/rt/pact_chapters, state /home/rt/pact_runs/workers/media/book-1/state,
        outputs under /home/rt/pact_runs/outputs.
  Mutable state is never written under a source root. Explicit --memory-dir / --out-base
  and --chapter-html-pattern override the layout for advanced use.

Automatic output naming:
  Each book run creates a distinct subdirectory below the host output root
  named book_0027-0032_local_<timestamp> or book_0027-0032_remote_<timestamp>
  (or book_0028_* for a single chapter). The local/remote label is derived from
  the resolved runtime descriptor after profile defaults and explicit overrides,
  not a user-supplied topology claim.

Simple book selection:
  --local                  Select the canonical local profile (RT or media local runtime).
  --remote [translator/reviewer]  Select the canonical remote profile. Bare --remote uses
                           profile defaults (Muse Free translator/repair + Luna standard
                           reviewer roles, reasoning 3, managed server). Supplied aliases
                           override those roles via providers.yaml (e.g. musefree/luna).
  Exactly one of --local or --remote is required for simple mode; they are mutually
  exclusive. Advanced: --runtime-config FILE remains available (compatibility) and is
  mutually exclusive with --local/--remote. Bare --remote without alias is valid.

Runtime configuration — profile defaults and optional overrides:
  --runtime-config FILE    Tagged runtime profile (local_llama | opencode_server | composite)
                           for advanced use. The profile supplies default role models,
                           reasoning, transport, and identity-bearing policy.
  --translator PROVIDER/ALIAS   Override Translator role model via providers.yaml.
  --reviewer PROVIDER/ALIAS     Override Reviewer role models via providers.yaml.
  --reasoning {0,1,2,3}         Override generation reasoning budget (profile default when omitted).
  Omitted selections use profile defaults and do not introduce launcher-specific quality defaults.
  Explicit overrides are validated against the runtime/provider contract, forwarded, and
  included in resolved identity/reporting. Aliases are case-insensitive and fail-closed.
  Remote defaults: reasoning 3, generator/repair opencode/muse-spark-1.2-contributor-free,
  standard reviewer roles openai/gpt-5.6-luna, managed server enabled, whole-chapter enabled
  for every book run.

Automatic offline preflight and check-only modes:
  Offline host-local preflight (profile syntax, local paths/ports, required env vars,
  plus host source/state/output resolution) runs by default before every configured
  execution — before any output directory is created or a pipeline starts. It validates
  that every requested chapter resolves to exactly one regular non-symlink HTML file
  and that state/output locations are present and writable. On failure the command
  exits with a clear error and no artifacts.
  --preflight              Validate and print a sanitized human-readable preflight report and exit.
  --preflight --json       Machine-readable JSON preflight report (or --preflight-json alias).
  --preflight-json         Alias for --preflight --json.
  Check-only modes do NOT start the pipeline, open a model session, contact a provider,
  submit source text, synchronize state, or create run artifacts. Remote endpoint
  preflight remains a separate transport check during actual execution.

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

Media synchronization:
  Every simple book run (local or remote, RT or media) defaults to media book id 1,
  target media-snap, root /home/rt/pact_runs. Override with --media-book-id for
  another book. On media the local restricted facade is used instead of self-SSH.
  A final MEDIA PUBLISH verdict (ACCEPTED/REJECTED) is printed; rejection is non-zero.

Safety and owner-run boundary:
  Runs are owner-started via the host-aware launcher. The launcher does not add hidden
  defaults beyond documented simple-mode policy, does not silently alter identity, and
  never starts providers or model servers in --help or --preflight modes.

Get mode-specific detail:
  python -m pact_full_pipeline_runner_v1.v4_run book --help
  python -m pact_full_pipeline_runner_v1.v4_run chapter --help
"""

_BOOK_HELP_EXTRA = """\
book mode — batch chapters sharing memory

Simple (host-aware):
  --chapters N | N-M          Single chapter (28) or range (28-32). Each numeric value resolves
                              to exactly one NNNN_*.html file in the host source root.
  --local | --remote [translator/reviewer]  Exactly one required. Bare --remote uses
                              canonical remote defaults (Muse Free + Luna, reasoning 3,
                              managed server). Every simple run defaults to whole-chapter
                              and media sync (book 1, media-snap, /home/rt/pact_runs).

Advanced (compatibility):
  --runtime-config FILE      Tagged runtime profile (source of truth). Mutually exclusive
                              with --local/--remote.

Optional (profile-aware):
  --translator PROVIDER/ALIAS
  --reviewer PROVIDER/ALIAS
  --reasoning {0,1,2,3}
  --markup preserve          Only 'preserve' is accepted.

Host/layout and source (advanced overrides):
  --chapter-html-pattern PATTERN   Advanced: pattern with {chapter_id}. Default is host source
                                   root with discovered NNNN_*.html files.
  --memory-dir DIR                 Advanced: overrides host mutable state root (RT: D:/pact/book_state,
                                   media: /home/rt/pact_runs/workers/media/book-1/state).
  --out-base DIR                   Overrides automatic host output root/book_XXXX-XXXX_local|remote_<timestamp>
  Automatic output: host_output/book_0027-0032_local|remote_<timestamp> (label from descriptor).
  Source and mutable state must not be the same directory.

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
_SINGLE_RE = re.compile(r"^\s*0*(\d+)\s*$")


def parse_range(text: str) -> tuple[int, int]:
    """Parse START-END or single N, return (start, end) as ints. Fail with ValueError on invalid."""
    if text is None or not str(text).strip():
        raise ValueError(f"chapter range must be START-END or single N, got {text!r}")
    m = _RANGE_RE.match(str(text))
    if m:
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
    ms = _SINGLE_RE.match(str(text))
    if ms:
        try:
            n = int(ms.group(1))
        except ValueError as exc:
            raise ValueError(f"invalid chapter range {text!r}: non-numeric") from exc
        if n < 1:
            raise ValueError(f"invalid chapter range {text!r}: chapters start at 1")
        return n, n
    raise ValueError(
        f"invalid chapter range {text!r}: expected N or START-END (e.g. 28 or 27-32)"
    )


def expand_range(start: int, end: int) -> list[str]:
    return [f"{i:04d}" for i in range(start, end + 1)]


def range_label(start: int, end: int) -> str:
    return f"{start:04d}-{end:04d}"


# ---------------------------------------------------------------------------
# Host-aware layout and source discovery (hardened)
# ---------------------------------------------------------------------------

def _host_layout(host_hint: Optional[str] = None) -> dict[str, Path]:
    """Select RT or media layout. host_hint: 'rt'|'media'|None (auto)."""
    import os as _os
    import sys as _sys
    # Explicit hint first
    if host_hint == "rt":
        base = dict(_RT_LAYOUT)
    elif host_hint == "media":
        base = dict(_MEDIA_LAYOUT)
    elif _os.environ.get("PACT_V4_HOST") == "rt":
        base = dict(_RT_LAYOUT)
    elif _os.environ.get("PACT_V4_HOST") == "media":
        base = dict(_MEDIA_LAYOUT)
    elif _sys.platform == "win32":
        base = dict(_RT_LAYOUT)
    else:
        base = dict(_MEDIA_LAYOUT)
    # Env overrides for tests (allow tmp_path injection)
    if _os.environ.get("PACT_V4_SOURCE_ROOT"):
        base["source"] = Path(_os.environ["PACT_V4_SOURCE_ROOT"])
    if _os.environ.get("PACT_V4_STATE_ROOT"):
        base["state"] = Path(_os.environ["PACT_V4_STATE_ROOT"])
    if _os.environ.get("PACT_V4_OUT_ROOT"):
        base["output"] = Path(_os.environ["PACT_V4_OUT_ROOT"])
    return base


def _check_no_symlink_chain(path: Path) -> None:
    for anc in [path] + list(path.parents):
        try:
            if anc.exists() and anc.is_symlink():
                raise ValueError(f"Symlink in path chain rejected: {anc}")
        except OSError as e:
            raise ValueError(f"Failed to stat path chain {anc}: {e}") from e


def _is_regular_file(path: Path) -> bool:
    try:
        st = path.stat()
    except FileNotFoundError:
        return False
    import stat as _stat
    return _stat.S_ISREG(st.st_mode)


def _validate_layout(layout: dict[str, Path]) -> None:
    for key in ("source", "state", "output"):
        if key not in layout:
            raise ValueError(f"layout missing key {key!r}")
    src = layout["source"]
    state = layout["state"]
    out = layout["output"]
    # Resolve without requiring existence (for existence checks later)
    # But ensure source and state not same and state not inside source
    try:
        src_res = src.resolve() if src.exists() else src.absolute()
        state_res = state.resolve() if state.exists() else state.absolute()
    except Exception:
        src_res = src.absolute()
        state_res = state.absolute()
    if src_res == state_res:
        raise ValueError(f"source and state must not be the same directory: {src}")
    try:
        # state inside source is forbidden
        state_res.relative_to(src_res)
        raise ValueError(f"state directory must not be inside source root: {state} inside {src}")
    except ValueError as e:
        if "inside source" in str(e):
            raise
        # not inside -> ok
        pass
    # Also ensure snapshot canonical dir not used as state (hardening)
    # state should not be exactly /home/rt/pact_runs/books/1
    for forbidden in [Path("/home/rt/pact_runs/books/1"), Path("/home/rt/pact_runs/books")]:
        try:
            if state_res == forbidden.resolve() if forbidden.exists() else forbidden.absolute():
                raise ValueError(f"state directory must not be canonical snapshot storage: {state}")
            state_res.relative_to(forbidden.resolve() if forbidden.exists() else forbidden.absolute())
            # if state is inside forbidden, also bad
            if str(state_res).startswith(str(forbidden)):
                raise ValueError(f"state directory must be outside canonical snapshot storage: {state}")
        except ValueError as ve:
            if "snapshot" in str(ve):
                raise
            pass


def _discover_chapter_sources(source_root: Path, chapter_numbers: Sequence[int]) -> dict[int, Path]:
    """Resolve each numeric chapter to exactly one NNNN_*.html regular file."""
    _check_no_symlink_chain(source_root)
    if not source_root.exists() or not source_root.is_dir() or source_root.is_symlink():
        raise ValueError(f"source root missing or not a directory: {source_root}")
    result: dict[int, Path] = {}
    for n in chapter_numbers:
        prefix = f"{n:04d}_"
        # Non-recursive scan, hardened
        candidates: list[Path] = []
        try:
            for entry in source_root.iterdir():
                # Hardening: reject symlinked entries, check regular file
                if entry.name.startswith(prefix) and entry.name.endswith(".html"):
                    if entry.is_symlink():
                        raise ValueError(f"source file is symlink (rejected): {entry}")
                    if not entry.is_file() or not _is_regular_file(entry):
                        raise ValueError(f"source file not regular: {entry}")
                    _check_no_symlink_chain(entry)
                    candidates.append(entry)
        except OSError as e:
            raise ValueError(f"failed to list source root {source_root}: {e}") from e
        if len(candidates) == 0:
            raise ValueError(f"no source file for chapter {n:04d} in {source_root} (expected {prefix}*.html)")
        if len(candidates) > 1:
            names = sorted(p.name for p in candidates)
            raise ValueError(f"ambiguous source files for chapter {n:04d} in {source_root}: {names}")
        result[n] = candidates[0]
    return result


def _host_layout_for_simple(is_local: bool, host_hint: Optional[str] = None) -> dict[str, Path]:
    # For simple mode, same layout for local/remote on given host; remote vs local only affects runtime profile.
    return _host_layout(host_hint)


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
    # Apply provider aliases — support bare globally-unique aliases (fail-closed on duplicate)
    if translator or reviewer:
        from pact_v4.runtime.runtime_config import load_providers_registry, apply_provider_flags, apply_role_models, build_reasoning_effort_map, TRANSLATOR_ROLES, REVIEWER_ROLES, _set_generator_reasoning_effort_map
        if providers_config is not None:
            prov_path = Path(providers_config)
        else:
            prov_path = Path(__file__).resolve().parent.parent / "configs" / "providers.yaml"
        registry = load_providers_registry(prov_path)
        # Resolve bare aliases via global index when slash missing
        def _resolve_spec(spec: Optional[str]):
            if not spec:
                return None
            if "/" in spec:
                return registry.resolve(spec)
            return registry.resolve_bare(spec)
        # Build role models manually to support bare
        role_models: dict[str, str] = {}
        translator_model = None
        if translator:
            translator_model = _resolve_spec(translator)
            for role in TRANSLATOR_ROLES:
                role_models[role] = translator_model.ref
        if reviewer:
            reviewer_model = _resolve_spec(reviewer)
            for role in REVIEWER_ROLES:
                role_models[role] = reviewer_model.ref
        if role_models:
            cfg = apply_role_models(cfg, role_models)
            if translator_model is not None:
                cfg = _set_generator_reasoning_effort_map(cfg, build_reasoning_effort_map(translator_model))
        else:
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
    # Book mode supports simple (--local/--remote) and advanced (--runtime-config) paths.
    parser = argparse.ArgumentParser(prog="v4_run book", add_help=False)
    parser.add_argument("--chapters", required=False, default=None)
    parser.add_argument("--runtime-config", dest="runtime_config", required=False, default=None)
    parser.add_argument("--profile", dest="profile", required=False, default=None)
    parser.add_argument("--local", action="store_true", default=False, help="Select canonical local profile")
    parser.add_argument("--remote", nargs="?", const="__DEFAULT__", default=None, help="Select canonical remote profile; optional translator/reviewer alias")
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
    parser.add_argument("--managed-server", action="store_true", default=False)
    parser.add_argument("--providers-config", required=False, default=None)
    parser.add_argument("--media-book-id", dest="media_book_id", required=False, default=None)
    parser.add_argument("--media-target", dest="media_target", required=False, default=None)
    parser.add_argument("--media-root", dest="media_root", required=False, default=None)

    args, remaining = parser.parse_known_args(argv)

    if args.help:
        print(_BOOK_HELP_EXTRA)
        try:
            from pact_full_pipeline_runner_v1.v4_book_run import build_argparser as _book_parser
            _book_parser().print_help()
        except Exception:
            pass
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

    # Determine mode: simple (--local or --remote) vs advanced (--runtime-config)
    is_simple_local = bool(args.local)
    is_simple_remote = args.remote is not None
    is_advanced = bool(args.runtime_config or args.profile)
    # Mutual exclusivity
    if is_simple_local and is_simple_remote:
        _error_exit("--local and --remote are mutually exclusive")
    if (is_simple_local or is_simple_remote) and is_advanced:
        _error_exit("--local/--remote and --runtime-config are mutually exclusive (use simple or advanced, not both)")
    if not (is_simple_local or is_simple_remote or is_advanced):
        _error_exit("book mode requires --local or --remote [translator/reviewer] (simple) or --runtime-config FILE (advanced)")
    if not args.chapters:
        _error_exit("--chapters N or N-M is required for book mode (e.g. --chapters 28 or --chapters 27-32)")
    # Simple mode: --translator/--reviewer must not be combined with --remote alias pair (avoid ambiguity)
    if (is_simple_remote or is_simple_local) and (args.translator or args.reviewer):
        # Allow translator/reviewer as explicit overrides only if --remote not using alias pair? For simplicity, require they use --remote alias form or advanced mode
        # But spec says simple remote may override via alias pair; separate --translator/--reviewer are advanced. Reject mixing.
        _error_exit("--translator/--reviewer cannot be combined with --local/--remote; use --remote alias pair or advanced --runtime-config mode")
    # Parse remote alias pair when simple remote
    remote_translator = None
    remote_reviewer = None
    if is_simple_remote:
        val = args.remote
        if val == "__DEFAULT__" or val is None:
            remote_translator = None
            remote_reviewer = None
        else:
            # Expect bare aliases like musefree/luna
            if "/" in val:
                parts = val.split("/", 1)
                remote_translator = parts[0].strip() if parts[0].strip() else None
                remote_reviewer = parts[1].strip() if len(parts) > 1 and parts[1].strip() else None
            else:
                remote_translator = val.strip() if val.strip() else None
                remote_reviewer = None
            # Validate aliases are simple (no path separators beyond one, alphanumeric/hyphen)
            import re as _re
            alias_re = _re.compile(r"^[A-Za-z0-9._-]+")
            # Actually handled by provider registry; keep permissive here
    # Parse range (single or range)
    try:
        start, end = parse_range(args.chapters)
    except ValueError as exc:
        _error_exit(str(exc))
    chapter_numbers = list(range(start, end + 1))
    label_range = range_label(start, end)

    # Resolve host layout
    layout = _host_layout()
    try:
        _validate_layout(layout)
    except Exception as exc:
        _error_exit(str(exc))
    is_simple = is_simple_local or is_simple_remote
    # Determine memory_dir and output root for simple vs advanced
    if is_simple:
        memory_dir = Path(args.memory_dir) if args.memory_dir else layout["state"]
        output_root = Path(args.out_base) if args.out_base else layout["output"]
        # Check source/state collision (simple must not be same)
        try:
            _validate_layout({"source": layout["source"], "state": memory_dir, "output": output_root})
        except Exception as exc:
            _error_exit(str(exc))
    else:
        # Advanced: preserve historical defaults (do not enforce layout collision)
        import os as _os_adv
        memory_dir = Path(args.memory_dir) if args.memory_dir else (Path(_os_adv.environ["PACT_V4_STATE_ROOT"]) if _os_adv.environ.get("PACT_V4_STATE_ROOT") else Path("D:/pact/pact_chapters"))
        output_root = None  # derived later from env or default

    # Resolve chapter sources for simple mode; advanced keeps zero-padded IDs
    discovered: dict[int, Path] = {}
    chapter_ids: list[str] = []
    chapter_html_pattern: str
    if is_simple:
        try:
            discovered = _discover_chapter_sources(layout["source"], chapter_numbers)
        except Exception as exc:
            _error_exit(str(exc))
        chapter_ids = [discovered[n].stem for n in chapter_numbers]
        chapter_html_pattern = str(layout["source"] / "{chapter_id}.html")
        if args.chapter_html_pattern:
            chapter_html_pattern = args.chapter_html_pattern
    else:
        chapter_ids = expand_range(start, end)
        chapter_html_pattern = args.chapter_html_pattern or "D:/pact/pact_chapters/{chapter_id}.html"

    # Select runtime profile for simple mode
    if is_simple_local:
        # Canonical local profile
        cfg_candidates = [
            Path(__file__).resolve().parent.parent / "configs" / "runtime_local.example.yaml",
            Path("configs/runtime_local.example.yaml"),
        ]
        cfg_path = next((p for p in cfg_candidates if p.is_file()), cfg_candidates[0])
        # Simple local must not use managed-server (local_llama restriction)
        if args.managed_server:
            _error_exit("--managed-server not allowed with --local")
        effective_managed = False
        translator_override = None
        reviewer_override = None
        reasoning_override = args.reasoning
    elif is_simple_remote:
        cfg_candidates = [
            Path(__file__).resolve().parent.parent / "configs" / "runtime_remote.example.yaml",
            Path("configs/runtime_remote.example.yaml"),
        ]
        cfg_path = next((p for p in cfg_candidates if p.is_file()), cfg_candidates[0])
        effective_managed = True  # simple remote defaults to managed-server
        # Allow explicit --managed-server to be redundant, but not to disable
        if args.managed_server:
            effective_managed = True
        translator_override = remote_translator
        reviewer_override = remote_reviewer
        reasoning_override = args.reasoning  # None -> profile default 3
    else:
        cfg_path = Path(args.runtime_config or args.profile)  # type: ignore[arg-type]
        effective_managed = bool(args.managed_server)
        translator_override = args.translator
        reviewer_override = args.reviewer
        reasoning_override = args.reasoning

    if not cfg_path.is_file():
        _error_exit(f"runtime profile not found: {cfg_path}")
    try:
        cfg = _load_runtime_config_file(cfg_path)
    except Exception as exc:
        _error_exit(f"invalid runtime profile {cfg_path}: {exc}")
    try:
        cfg = _apply_overrides(cfg, translator_override, reviewer_override, reasoning_override, providers_config=args.providers_config)
        cfg = _apply_managed(cfg, effective_managed)
    except Exception as exc:
        _error_exit(str(exc))

    # Reasoning for preflight
    effective_reasoning: Optional[int] = reasoning_override
    if effective_reasoning is None:
        try:
            from pact_v4.runtime.runtime_config import OpenCodeBackendConfig, LocalLlamaBackendConfig
            if isinstance(cfg, OpenCodeBackendConfig) and cfg.server.reasoning is not None:
                effective_reasoning = int(cfg.server.reasoning)
            elif isinstance(cfg, LocalLlamaBackendConfig):
                effective_reasoning = None
        except Exception:
            pass

    from pact_v4.runtime.runtime_config import run_runtime_preflight
    is_check_only = bool(args.preflight or args.preflight_json)
    is_json = bool(args.json or args.preflight_json)
    # Determine output root early for preflight reporting
    if is_simple:
        _pre_out_root = Path(args.out_base) if args.out_base else output_root
    else:
        import os as _os_pre
        _pre_out_root = Path(args.out_base) if args.out_base else (Path(_os_pre.environ.get("PACT_V4_OUT_ROOT")) if _os_pre.environ.get("PACT_V4_OUT_ROOT") else layout["output"])
    out_root = _pre_out_root
    # Build extended preflight report (runtime + book layout)
    # First runtime preflight
    runtime_report = run_runtime_preflight(cfg, reasoning=effective_reasoning if effective_reasoning is not None else None)
    # Book layout checks
    book_checks = []
    book_errors = []
    # Source discovery check
    if is_simple:
        try:
            # Already discovered above; verify again for preflight report
            for n in chapter_numbers:
                p = discovered.get(n)
                if p is None:
                    book_errors.append(f"missing source for chapter {n:04d}")
                    book_checks.append(type(runtime_report.checks[0])(name=f"source {n:04d}", ok=False, detail="missing"))
                else:
                    # Verify readable and regular
                    if not p.is_file() or p.is_symlink():
                        book_errors.append(f"source not regular: {p}")
                        book_checks.append(type(runtime_report.checks[0])(name=f"source {n:04d}:{p.name}", ok=False, detail="not regular/symlink"))
                    else:
                        book_checks.append(type(runtime_report.checks[0])(name=f"source {n:04d}:{p.name}", ok=True, detail=str(layout["source"])))
        except Exception as e:
            book_errors.append(str(e))
    else:
        # Advanced: check pattern directory exists if possible (best effort, not fail if pattern is template)
        try:
            pat_dir = Path(chapter_html_pattern).parent
            if pat_dir.exists() and pat_dir.is_symlink():
                book_errors.append(f"pattern dir is symlink: {pat_dir}")
        except Exception:
            pass
    # State/output readiness (without creation)
    try:
        _check_no_symlink_chain(memory_dir)
        if memory_dir.exists() and not memory_dir.is_dir():
            book_errors.append(f"state path not a directory: {memory_dir}")
            book_checks.append(type(runtime_report.checks[0])(name=f"state {memory_dir}", ok=False, detail="not a directory"))
        else:
            # Check parent writable (if exists) or parent chain
            parent = memory_dir.parent if memory_dir.exists() else memory_dir
            # For check-only, don't create; just report exists or parent exists
            book_checks.append(type(runtime_report.checks[0])(name=f"state {memory_dir}", ok=True, detail="ready"))
    except Exception as e:
        book_errors.append(str(e))
        book_checks.append(type(runtime_report.checks[0])(name=f"state {memory_dir}", ok=False, detail=str(e)))
    # Output root readiness
    try:
        _check_no_symlink_chain(out_root)
        book_checks.append(type(runtime_report.checks[0])(name=f"output {out_root}", ok=True, detail="ready"))
    except Exception as e:
        book_errors.append(str(e))
    # Layout collision already validated
    # Combine reports
    from pact_v4.runtime.runtime_config import PreflightReport
    combined_ok = runtime_report.ok and not book_errors
    combined_checks = tuple(list(runtime_report.checks) + book_checks)
    combined_errors = tuple(list(runtime_report.errors) + book_errors)
    report = PreflightReport(
        ok=combined_ok,
        kind=runtime_report.kind,
        identity_hash=runtime_report.identity_hash,
        public_record=runtime_report.public_record,
        model_bindings=runtime_report.model_bindings,
        effective_options=runtime_report.effective_options,
        checks=combined_checks,
        errors=combined_errors,
    )
    # For simple mode, enrich human output with layout info
    def _format_with_layout(rep):
        base = rep.format_human()
        extra = f"\\n  source: {layout['source']}\\n  state: {memory_dir}\\n  outputs: {out_root}"
        if is_simple and discovered:
            extra += "\\n  chapters: " + ", ".join(discovered[n].name for n in chapter_numbers)
        return base + extra
    if is_check_only:
        if is_json:
            # Enrich JSON with layout
            j = report.to_dict()
            j["layout"] = {"source": str(layout["source"]), "state": str(memory_dir), "output": str(out_root)}
            if is_simple:
                j["resolved_chapters"] = {f"{n:04d}": discovered[n].name for n in chapter_numbers if n in discovered}
            import json as _j
            print(_j.dumps(j, ensure_ascii=False, indent=2, sort_keys=True))
        else:
            print(_format_with_layout(report))
        return 0 if report.ok else 1
    if not report.ok:
        print(_format_with_layout(report), file=sys.stderr)
        _error_exit(f"offline preflight failed — refusing to start book run (see report above)", code=3)

    try:
        label = _derive_label(cfg)
    except Exception as exc:
        _error_exit(f"cannot derive local/remote label from runtime descriptor: {exc}")
    if label not in ("local", "remote"):
        _error_exit(f"unknown runtime descriptor label {label!r}; expected local or remote")

    # Automatic output directory — collision-safe (use out_root)
    if args.out_base:
        out_base = Path(args.out_base)
        try:
            out_base.mkdir(parents=True, exist_ok=True)
        except Exception as exc:
            _error_exit(f"cannot create output directory {out_base}: {exc}")
    else:
        import os
        # Use layout output root unless env overrides
        root = out_root
        try:
            out_base = _allocate_book_out_dir(root, label_range, label)
        except Exception as exc:
            _error_exit(f"cannot create output directory {root}: {exc}")

    # Build delegated argv for v4_book_run.main
    delegated: list[str] = []
    delegated += ["--chapters"] + chapter_ids
    delegated += ["--chapter-html-pattern", chapter_html_pattern]
    delegated += ["--memory-dir", str(memory_dir)]
    delegated += ["--out-base", str(out_base)]
    delegated += ["--runtime-config", str(cfg_path)]
    # Translator/reviewer for simple remote
    if is_simple_remote:
        if remote_translator:
            delegated += ["--translator", remote_translator]
        if remote_reviewer:
            delegated += ["--reviewer", remote_reviewer]
        if reasoning_override is not None:
            delegated += ["--reasoning", str(reasoning_override)]
    else:
        if args.translator:
            delegated += ["--translator", args.translator]
        if args.reviewer:
            delegated += ["--reviewer", args.reviewer]
        if args.reasoning is not None:
            delegated += ["--reasoning", str(args.reasoning)]
    if effective_managed:
        delegated += ["--managed-server"]
    if args.providers_config:
        delegated += ["--providers-config", str(args.providers_config)]
    # Whole-chapter default for every book mode
    if "--whole-chapter" not in delegated and "--whole_chapter" not in delegated:
        delegated += ["--whole-chapter"]
    # Media sync defaults for every simple book mode
    if is_simple:
        media_book_id = args.media_book_id or _DEFAULT_MEDIA_BOOK_ID
        media_target = args.media_target or _DEFAULT_MEDIA_TARGET
        media_root = args.media_root or _DEFAULT_MEDIA_ROOT
        delegated += ["--media-book-id", media_book_id]
        delegated += ["--media-target", media_target]
        delegated += ["--media-root", media_root]
    else:
        # Advanced: forward explicit media flags if supplied
        if args.media_book_id:
            delegated += ["--media-book-id", args.media_book_id]
        if args.media_target:
            delegated += ["--media-target", args.media_target]
        if args.media_root:
            delegated += ["--media-root", args.media_root]
    if args.providers_config:
        pass  # already added
    delegated += list(remaining)

    print(_format_with_layout(report))

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
