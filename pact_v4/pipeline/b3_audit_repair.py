"""B3: production audit/repair pipeline (concept §10 B3 + §9.4).

Wires the B-series components into one orchestrator the strict runner can
call after whole-chapter generation:

    1. entity context (B1.2, optional, ``entity_context_enabled``) —
       ``extract_entity_context(source)`` -> per-chapter cache
       (``source_hash + extractor_version``), rendered into the audit
       prompt's ``CHAPTER ENTITY FACTS`` block and passed to the hard
       filters (entity-PID issues are forced to TIER_B, §5.3);
    2. chunked audit (B1) — ``ChunkedAuditEvaluator`` over the whole
       chapter pairs (narrator + entity blocks), replacing the old
       ``gemma_russian_review`` / ``qwen_fidelity`` gates;
    3. gate: ``audit_complete == False`` -> FAIL-CLOSED (no repair, the
       chapter is never released as passed audit);
    4. hard filters (B1.1) — ``apply_hard_filters`` -> CONFIRMED /
       REJECTED / TIER_B;
    5. selective repair (B2) — ``SelectiveRepairEvaluator`` (Tier A
       direct, Tier B verify-before-repair) with its single post-repair
       re-audit of the changed PIDs;
    6. ``translations_repaired`` = raw map + committed repairs.

Provenance / cache contract:

* ``audit_journal.ndjson`` (schema ``pact-v4-b3-audit-journal/v1``) —
  append-only events ``audit_started`` / ``audit_chunk_started`` /
  ``audit_chunk_done`` / ``audit_complete`` / ``audit_failed`` (terminal
  pre/model-call evaluator failure — always followed by a fail-closed
  ``gate``) / ``finding`` / ``repair_round`` / ``reaudit_scope`` /
  ``gate``. This is a SEPARATE
  file from the generation ``journal.ndjson``: the whole-chapter resume
  contract requires exactly one generation entry, so audit events can
  never share that file.
* ``audit_cache_b3.json`` (schema ``pact-v4-b3-audit-cache/v1``) — audit
  cache identity = ``snapshot_hash + translation_hash + config_identity +
  backend_identity_hash + prompt_version + harness_version +
  entity_context_hash`` (the card's identity, plus the exact translation
  content — the audit outcome is a function of both source and translation;
  entity hash present only when entity context is enabled). A full cache
  hit reuses the stored outcome (0 model calls); a cached
  ``audit_complete=False`` is NEVER reused — the audit re-runs (fail-closed,
  "resume does not skip an incomplete audit").
* ``entity_context_cache.json`` (schema from ``entity_extractor``) — the
  B1.2 per-chapter entity cache (``source_hash + extractor_version``).

Transport: audit/repair/entity calls go through ``CompletionBackend``
(``build_role_backend``), so the same pipeline serves local, remote and
composite profiles. Remote audit through ``opencode serve`` is a
CONTRACT, NOT tested yet (owner decision: test remote audit after the
B-phase; the evaluators never emit ``request_options`` — the reasoning
budget is a server arg).
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Mapping, Optional, Sequence, Tuple

from pact_v4.audit.chunked_audit import (
    DEFAULT_MAX_INPUT_TOKENS,
    DEFAULT_MAX_TOKENS,
    DEFAULT_OVERLAP_TOKENS,
    DEFAULT_REASONING_BUDGET,
    HARNESS_VERSION,
    PROMPT_VERSION,
    AuditPair,
    ChunkedAuditConfig,
    ChunkedAuditEvaluator,
    ChunkedAuditOutcome,
    build_narrator_context,
    pairs_from_maps,
)
from pact_v4.audit.entity_extractor import (
    EXTRACTOR_VERSION,
    BackendEntityExtractor,
    BackendEntityExtractorConfig,
    ChapterEntityContext,
    EntityContextCache,
    EntityExtractionResult,
    extract_entity_context,
)
from pact_v4.audit.hard_filters import FilteredIssue, apply_hard_filters
from pact_v4.phase1.models import SourceArtifact, canonical_json_hash
from pact_v4.repair.selective_repair import (
    DEFAULT_REAUDIT_FULL_THRESHOLD,
    DEFAULT_REAUDIT_NEIGHBOUR_WINDOW,
    MICROBATCH_TARGET,
    MICROBATCH_TRIGGER,
    REPAIR_FINDINGS_CAP,
    SelectiveRepairConfig,
    SelectiveRepairEvaluator,
    SelectiveRepairOutcome,
)
from pact_v4.runtime.backend_protocol import (
    BackendDescriptor,
    CompletionBackend,
    CompletionRequest,
)

LOG = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Artifact schemas (identity-bearing, never reused across a schema change)
# ---------------------------------------------------------------------------

B3_AUDIT_CACHE_SCHEMA = "pact-v4-b3-audit-cache/v1"
B3_AUDIT_JOURNAL_SCHEMA = "pact-v4-b3-audit-journal/v1"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _atomic_write_json(path: Path, payload: Any) -> None:
    """Atomic write (write-then-rename) with a UTF-8 JSON payload."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


# ---------------------------------------------------------------------------
# Entity-context block renderer (deterministic, source-derived)
# ---------------------------------------------------------------------------


def render_entity_context_block(context: ChapterEntityContext) -> str:
    """Render a validated ``ChapterEntityContext`` into the audit prompt's
    ``CHAPTER ENTITY FACTS - SOURCE-DERIVED`` block.

    Deterministic (sorted by entity name); the block is data for the
    auditor (evidence level 3: source > adjacent > chapter facts), never
    an instruction. Empty context -> empty string (caller omits the
    block).
    """
    if not context.entities:
        return ""
    lines: list = []
    for record in sorted(context.entities, key=lambda r: r.entity):
        lines.append(f"- entity: {record.entity}")
        lines.append(f"  established_type: {record.canonical_type}")
        anchor = record.anchor
        lines.append(
            f"  anchor: \"{anchor.span}\" (pid {anchor.pid}, {anchor.status})"
        )
        for alias in record.aliases:
            lines.append(
                f"  alias: \"{alias.surface}\" (pid {alias.pid}, {alias.status})"
            )
        for claim in record.claims:
            evidence = ", ".join(
                f"{ev.pid} \"{ev.span}\"" for ev in claim.evidence
            )
            windows = ", ".join(
                f"[{a}-{b}]" for a, b in claim.evidence_windows
            )
            detail = f"evidence: {evidence}" if evidence else ""
            if windows:
                detail += f" windows: {windows}"
            lines.append(
                f"  claim: {claim.kind}={claim.value!r} ({claim.status})"
                + (f" {detail}" if detail else "")
            )
    return "\n".join(lines) + "\n"


def render_entity_context_to_hard_filters(
    context: ChapterEntityContext,
) -> Mapping[str, Any]:
    """Payload form for ``apply_hard_filters`` (the ``entities`` list)."""
    return context.to_payload()


# ---------------------------------------------------------------------------
# Audit journal (append-only, one event per line, crash-safe)
# ---------------------------------------------------------------------------


class AuditJournal:
    """Append-only audit provenance journal (write-only, one line/event).

    Deliberately separate from the generation ``journal.ndjson``: the
    whole-chapter resume contract requires exactly one generation entry,
    so audit events are recorded here. A write failure disables the
    writer and is logged, never raised — provenance must not break a run.
    """

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._handle: Optional[Any] = None
        self._disabled = False

    def _ensure_open(self) -> None:
        if self._handle is None:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self._handle = open(self.path, "a", encoding="utf-8")

    def emit(self, event: str, **fields: Any) -> None:
        if self._disabled:
            return
        record = {
            "schema": B3_AUDIT_JOURNAL_SCHEMA,
            "event": event,
            "ts": _now_iso(),
            **fields,
        }
        try:
            self._ensure_open()
            assert self._handle is not None
            self._handle.write(json.dumps(record, ensure_ascii=False) + "\n")
            self._handle.flush()
        except OSError as exc:
            LOG.warning("B3 audit journal write failed (%s); disabling", exc)
            self._disabled = True

    def close(self) -> None:
        if self._handle is not None:
            try:
                self._handle.close()
            finally:
                self._handle = None


# ---------------------------------------------------------------------------
# Config / result shapes
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class B3AuditRepairConfig:
    """Settings for one B3 audit/repair pass (frozen contract of the run).

    ``entity_context_enabled`` (runtime config, default True — owner
    decision 2026-08-10, B1.3 gate pending): True runs the source-only
    entity prepass and feeds both the auditor and the hard filters; False
    audits without the entity block. The audit input params
    (``max_input_tokens``/``max_tokens``/``overlap_tokens``) are part of
    the run config identity via ``StrictRunConfig`` and of the audit
    cache identity via the stored payload.
    """

    entity_context_enabled: bool = True
    max_input_tokens: int = DEFAULT_MAX_INPUT_TOKENS
    max_tokens: int = DEFAULT_MAX_TOKENS
    overlap_tokens: int = DEFAULT_OVERLAP_TOKENS
    reasoning_budget: int = DEFAULT_REASONING_BUDGET
    repair_findings_cap: int = REPAIR_FINDINGS_CAP
    repair_microbatch_trigger: int = MICROBATCH_TRIGGER
    repair_microbatch_target: int = MICROBATCH_TARGET
    repair_reaudit_neighbour_window: int = DEFAULT_REAUDIT_NEIGHBOUR_WINDOW
    repair_reaudit_full_threshold: int = DEFAULT_REAUDIT_FULL_THRESHOLD
    prompt_version: str = PROMPT_VERSION
    harness_version: str = HARNESS_VERSION
    extractor_version: str = EXTRACTOR_VERSION

    def to_payload(self) -> Dict[str, Any]:
        return {
            "entity_context_enabled": self.entity_context_enabled,
            "max_input_tokens": self.max_input_tokens,
            "max_tokens": self.max_tokens,
            "overlap_tokens": self.overlap_tokens,
            "reasoning_budget": self.reasoning_budget,
            "repair_findings_cap": self.repair_findings_cap,
            "repair_microbatch_trigger": self.repair_microbatch_trigger,
            "repair_microbatch_target": self.repair_microbatch_target,
            "repair_reaudit_neighbour_window": self.repair_reaudit_neighbour_window,
            "repair_reaudit_full_threshold": self.repair_reaudit_full_threshold,
            "prompt_version": self.prompt_version,
            "harness_version": self.harness_version,
            "extractor_version": self.extractor_version,
        }


@dataclass(frozen=True)
class B3AuditRepairResult:
    """Aggregated result of one B3 pass (feeds the run record)."""

    step6: Dict[str, Any]  # audit summary
    step7: Dict[str, Any]  # repair summary
    step8: Dict[str, Any]  # gate / terminal
    translations_repaired: Dict[str, str]
    audit_complete: bool
    from_cache: bool
    entity_context_hash: Optional[str]
    audit_cache_path: Path
    journal_path: Path


# ---------------------------------------------------------------------------
# Entity-extractor backend view (role resolution without identity change)
# ---------------------------------------------------------------------------


class _EntityRoleView:
    """CompletionBackend view presenting an ``entity_extractor`` binding.

    The local/remote descriptors do not carry an ``entity_extractor``
    role; ``BackendEntityExtractor`` needs one to resolve its model ref.
    This view adds the role pointing at the audit (Qwen) ref WITHOUT
    touching the real backend's identity — the descriptor is only used
    for role resolution, never for cache/resume identity (the run records
    ``cfg.backend.identity_hash``).
    """

    def __init__(self, backend: CompletionBackend, entity_ref: str) -> None:
        self._backend = backend
        self._entity_ref = entity_ref

    @property
    def descriptor(self) -> BackendDescriptor:
        base = self._backend.descriptor
        bindings = dict(base.model_bindings)
        bindings["entity_extractor"] = self._entity_ref
        bindings.setdefault("default", self._entity_ref)
        return replace(base, model_bindings=bindings)

    def complete(self, request: CompletionRequest) -> Any:
        return self._backend.complete(request)

    def close(self) -> None:
        self._backend.close()

    def call_records(self) -> Sequence[Any]:
        return self._backend.call_records()


# ---------------------------------------------------------------------------
# Audit cache (resume identity)
# ---------------------------------------------------------------------------


def _audit_cache_path(out_dir: Path) -> Path:
    return out_dir / "audit_cache_b3.json"


def _entity_cache_path(out_dir: Path) -> Path:
    return out_dir / "entity_context_cache.json"


def _journal_path(out_dir: Path) -> Path:
    return out_dir / "audit_journal.ndjson"


def _load_entity_cache(out_dir: Path) -> EntityContextCache:
    path = _entity_cache_path(out_dir)
    if not path.exists():
        return EntityContextCache()
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        return EntityContextCache.from_payload(payload)
    except Exception as exc:  # noqa: BLE001
        # F6 (B3 review): a structurally corrupt dependent cache (JSON
        # list/object with missing keys, wrong entry shape, foreign schema)
        # can raise KeyError/TypeError/AttributeError deep inside
        # from_payload — NOT just OSError/ValueError. Any such failure is a
        # cache MISS (fail-closed): discard and recompute, never abort B3.
        LOG.warning(
            "B3: entity_context_cache.json unreadable/foreign (%s: %s); "
            "starting a fresh cache",
            type(exc).__name__, exc,
        )
        return EntityContextCache()


def _save_entity_cache(out_dir: Path, cache: EntityContextCache) -> None:
    _atomic_write_json(_entity_cache_path(out_dir), cache.to_payload())


class B3AuditCache:
    """Persistent audit cache with resume identity (card §10 B3 item 3).

    Identity = snapshot_hash + translation_hash + config_identity +
    backend_identity_hash + prompt_version + harness_version +
    entity_context_hash (present only when entity context is enabled).
    ``audit_complete=False`` is NEVER a reusable state: on resume an
    incomplete audit re-runs (fail-closed).
    """

    def __init__(self, path: Path, payload: Optional[Mapping[str, Any]] = None) -> None:
        self.path = Path(path)
        self._payload: Optional[Dict[str, Any]] = (
            dict(payload) if payload is not None else None
        )

    @classmethod
    def load(
        cls,
        path: Path,
        *,
        snapshot_hash: str,
        translation_hash: str,
        config_identity: str,
        backend_identity_hash: str,
        prompt_version: str,
        harness_version: str,
        entity_context_hash: Optional[str],
        entity_context_enabled: bool,
        expected_pids: Optional[Sequence[str]] = None,
    ) -> Optional["B3AuditCache"]:
        if not Path(path).exists():
            return None
        try:
            payload = json.loads(Path(path).read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            LOG.warning("B3: audit cache unreadable (%s); re-running audit", exc)
            return None
        if not isinstance(payload, dict):
            LOG.warning("B3: audit cache is not an object; re-running audit")
            return None
        expected = {
            "snapshot_hash": snapshot_hash,
            "translation_hash": translation_hash,
            "config_identity": config_identity,
            "backend_identity_hash": backend_identity_hash,
            "prompt_version": prompt_version,
            "harness_version": harness_version,
            "entity_context_enabled": entity_context_enabled,
            "entity_context_hash": entity_context_hash,
        }
        for key, value in expected.items():
            if payload.get(key) != value:
                LOG.info(
                    "B3: audit cache identity mismatch on %s "
                    "(stored=%r expected=%r); re-running audit",
                    key, payload.get(key), value,
                )
                return None
        if payload.get("schema") != B3_AUDIT_CACHE_SCHEMA:
            LOG.info("B3: audit cache schema mismatch; re-running audit")
            return None
        if payload.get("audit_complete") is not True:
            LOG.info(
                "B3: cached audit is incomplete (audit_complete=%r); "
                "re-running audit (fail-closed, never skip an incomplete audit)",
                payload.get("audit_complete"),
            )
            return None
        # F4 (B3 review): the cached repaired map is validated before it can
        # ever be reused/publicized. A structurally tampered cache (extra /
        # missing / reordered PIDs, non-string values, or a repaired-map hash
        # that does not bind to the stored map) is a MISS — the audit re-runs
        # and the tampered map is never published. Old-schema caches (no
        # ``translations_repaired_hash`` field) also miss.
        repaired = payload.get("translations_repaired")
        if not isinstance(repaired, dict):
            LOG.warning(
                "B3: audit cache translations_repaired is not an object; "
                "re-running audit"
            )
            return None
        if expected_pids is not None:
            stored_pids = list(repaired.keys())
            if stored_pids != list(expected_pids):
                LOG.warning(
                    "B3: audit cache translations_repaired PID set mismatch "
                    "(stored=%r expected=%r); re-running audit",
                    stored_pids, list(expected_pids),
                )
                return None
        if any(not isinstance(value, str) for value in repaired.values()):
            LOG.warning(
                "B3: audit cache translations_repaired contains non-string "
                "values; re-running audit"
            )
            return None
        stored_hash = payload.get("translations_repaired_hash")
        computed_hash = canonical_json_hash(dict(sorted(repaired.items())))
        if not isinstance(stored_hash, str) or stored_hash != computed_hash:
            LOG.warning(
                "B3: audit cache translations_repaired hash mismatch "
                "(stored=%r computed=%r) — old schema or tampered cache; "
                "re-running audit",
                stored_hash, computed_hash,
            )
            return None
        return cls(path, payload)

    def is_hit(self) -> bool:
        return self._payload is not None

    def audit_complete(self) -> bool:
        return bool(self._payload and self._payload.get("audit_complete") is True)

    def entity_context_hash(self) -> Optional[str]:
        if not self._payload:
            return None
        return self._payload.get("entity_context_hash")

    def stored_issues(self) -> Tuple[Dict[str, Any], ...]:
        if not self._payload:
            return ()
        return tuple(self._payload.get("issues") or ())

    def stored_filtered(self) -> Tuple[FilteredIssue, ...]:
        if not self._payload:
            return ()
        filtered: list = []
        for item in self._payload.get("filtered") or ():
            filtered.append(FilteredIssue(**item))
        return tuple(filtered)

    def stored_repair(self) -> Optional[Dict[str, Any]]:
        if not self._payload:
            return None
        return self._payload.get("repair")

    def stored_translations_repaired(self) -> Optional[Dict[str, str]]:
        if not self._payload:
            return None
        value = self._payload.get("translations_repaired")
        # F4: values are validated at load() (non-string values reject the
        # cache), so no stringification is applied here — a tampered non-
        # string value must never be silently coerced into publication.
        if not isinstance(value, dict):
            return None
        return dict(value)

    def save(
        self,
        *,
        snapshot_hash: str,
        translation_hash: str,
        config_identity: str,
        backend_identity_hash: str,
        entity_context_hash: Optional[str],
        entity_context_enabled: bool,
        outcome: ChunkedAuditOutcome,
        filtered: Sequence[FilteredIssue],
        repair: Optional[SelectiveRepairOutcome],
        translations_repaired: Mapping[str, str],
    ) -> None:
        payload = {
            "schema": B3_AUDIT_CACHE_SCHEMA,
            "snapshot_hash": snapshot_hash,
            "translation_hash": translation_hash,
            "config_identity": config_identity,
            "backend_identity_hash": backend_identity_hash,
            "prompt_version": outcome.prompt_version,
            "harness_version": outcome.harness_version,
            "entity_context_enabled": entity_context_enabled,
            "entity_context_hash": entity_context_hash,
            "audit_complete": outcome.audit_complete,
            "issue_count": outcome.issue_count,
            "issues": [dict(issue) for issue in outcome.issues],
            "chunks": outcome.to_payload()["chunks"],
            "filtered": [
                {
                    "issue": dict(f.issue),
                    "verdict": f.verdict,
                    "filter_name": f.filter_name,
                    "reason": f.reason,
                }
                for f in filtered
            ],
            "repair": repair.to_payload() if repair is not None else None,
            "translations_repaired": dict(translations_repaired),
            # F4: canonical hash of the repaired map binds the map to this
            # cache record. load() recomputes it and rejects a mismatch (old
            # schema / tampered map), so a structurally tampered
            # translations_repaired can never be replayed or publicized.
            "translations_repaired_hash": canonical_json_hash(
                dict(sorted(translations_repaired.items()))
            ),
        }
        _atomic_write_json(self.path, payload)
        self._payload = payload


# ---------------------------------------------------------------------------
# Orchestrator bundle
# ---------------------------------------------------------------------------


class B3AuditRepair:
    """Production B3 pipeline bundle (transport-neutral, injectable).

    Usage (CLI wires it from ``build_role_backend``)::

        b3 = B3AuditRepair(
            audit_backend=completion_backend,      # Qwen (qwen_audit role)
            repair_backend=completion_backend,     # generator role (Gemma)
            config=B3AuditRepairConfig(entity_context_enabled=cfg.entity_context_enabled),
        )
        result = b3.run(
            chapter_id=cfg.chapter_id,
            source=source,                          # SourceArtifact
            translation=dict(final_text_by_pid),    # raw generator map
            book_memory=memory.book_memory,
            out_dir=cfg.out_dir,
            config_identity=config.config_identity,
            backend_identity_hash=cfg.backend.identity_hash,
        )

    ``audit_backend`` must serve the ``qwen_audit`` (or ``default``)
    role; ``repair_backend`` the generator role (Kocmi-safe: auditor !=
    repairer). Entity extraction runs on the audit model.
    """

    def __init__(
        self,
        *,
        audit_backend: CompletionBackend,
        repair_backend: Optional[CompletionBackend] = None,
        entity_backend: Optional[CompletionBackend] = None,
        config: Optional[B3AuditRepairConfig] = None,
        progress: Optional[Any] = None,
    ) -> None:
        self._audit_backend = audit_backend
        self._repair_backend = repair_backend or audit_backend
        self._config = config or B3AuditRepairConfig()
        # Entity extraction runs on the audit (Qwen) model; the view adds
        # the missing entity_extractor role without touching backend
        # identity (role resolution only).
        from pact_v4.audit.chunked_audit import audit_model_ref

        entity_ref = audit_model_ref(audit_backend)
        self._entity_backend = entity_backend or _EntityRoleView(
            audit_backend, entity_ref
        )
        self._progress = progress

    # ------------------------------------------------------------------
    # Public entry
    # ------------------------------------------------------------------

    def run(
        self,
        *,
        chapter_id: str,
        source: SourceArtifact,
        snapshot_hash: str,
        translation: Mapping[str, str],
        book_memory: Mapping[str, Any],
        out_dir: Path,
        config_identity: str,
        backend_identity_hash: str,
    ) -> B3AuditRepairResult:
        cfg = self._config
        cache_path = _audit_cache_path(out_dir)
        journal = AuditJournal(_journal_path(out_dir))
        try:
            return self._run_impl(
                chapter_id=chapter_id,
                source=source,
                snapshot_hash=snapshot_hash,
                translation=translation,
                book_memory=book_memory,
                out_dir=out_dir,
                config_identity=config_identity,
                backend_identity_hash=backend_identity_hash,
                cache_path=cache_path,
                journal=journal,
            )
        finally:
            journal.close()

    # ------------------------------------------------------------------
    # internals
    # ------------------------------------------------------------------

    def _run_impl(
        self,
        *,
        chapter_id: str,
        source: SourceArtifact,
        snapshot_hash: str,
        translation: Mapping[str, str],
        book_memory: Mapping[str, Any],
        out_dir: Path,
        config_identity: str,
        backend_identity_hash: str,
        cache_path: Path,
        journal: AuditJournal,
    ) -> B3AuditRepairResult:
        cfg = self._config
        source_map = dict(source.source)
        translation_map = dict(translation)
        # The audit outcome is a function of BOTH the source and the
        # translation being audited, so the audit cache identity binds to
        # the exact translation content too — a regenerated/tampered raw
        # map with the same snapshot hash must never be a stale cache hit.
        translation_hash = canonical_json_hash(dict(sorted(translation_map.items())))

        # ------------------------------------------------------------------
        # 1. Entity context prepass (B1.2), when enabled.
        # ------------------------------------------------------------------
        entity_context: str = ""
        entity_hash: Optional[str] = None
        entity_payload: Optional[Mapping[str, Any]] = None
        entity_from_cache = False
        if cfg.entity_context_enabled:
            entity_cache = _load_entity_cache(out_dir)
            try:
                extraction = extract_entity_context(
                    source_artifact=source,
                    extractor=BackendEntityExtractor(
                        self._entity_backend,
                        config=BackendEntityExtractorConfig(),
                    ),
                    cache=entity_cache,
                    extractor_version=cfg.extractor_version,
                )
            except Exception as exc:  # noqa: BLE001 — fail-closed, never silent skip
                LOG.exception("B3: entity context extraction failed for %s", chapter_id)
                raise RuntimeError(
                    f"B3 entity context extraction failed: {exc}"
                ) from exc
            entity_from_cache = extraction.from_cache
            entity_payload = extraction.context.to_payload()
            entity_hash = canonical_json_hash(entity_payload)
            entity_context = render_entity_context_block(extraction.context)
            _save_entity_cache(out_dir, entity_cache)
            journal.emit(
                "entity_context",
                enabled=True,
                from_cache=entity_from_cache,
                entity_count=len(extraction.context.entities),
                entity_context_hash=entity_hash,
            )
            self._emit_progress(
                "entity_context_done",
                from_cache=entity_from_cache,
                entity_count=len(extraction.context.entities),
            )

        # ------------------------------------------------------------------
        # 2. Audit cache: full hit -> reuse (0 model calls); incomplete
        #    cached audit -> re-run (fail-closed).
        # ------------------------------------------------------------------
        cache = B3AuditCache.load(
            cache_path,
            snapshot_hash=snapshot_hash,
            translation_hash=translation_hash,
            config_identity=config_identity,
            backend_identity_hash=backend_identity_hash,
            prompt_version=cfg.prompt_version,
            harness_version=cfg.harness_version,
            entity_context_hash=entity_hash,
            entity_context_enabled=cfg.entity_context_enabled,
            # F4: exact PID set/order validation — a cache whose
            # translations_repaired has missing/extra/reordered PIDs is a miss.
            expected_pids=tuple(translation_map),
        )
        if cache is not None and cache.is_hit():
            repaired = cache.stored_translations_repaired()
            issues = cache.stored_issues()
            filtered = cache.stored_filtered()
            repair_payload = cache.stored_repair()
            step6, step7, step8 = _reports_from_cache(
                cache=cache, issue_count=len(issues),
            )
            cache_repair_complete = bool(
                repair_payload and repair_payload.get("repair_complete") is True
            )
            LOG.info(
                "B3: audit cache full hit for %s (0 model calls)", chapter_id
            )
            journal.emit(
                "audit_started",
                snapshot_hash=snapshot_hash,
                entity_context_enabled=cfg.entity_context_enabled,
                entity_context_hash=entity_hash,
                prompt_version=cfg.prompt_version,
                harness_version=cfg.harness_version,
            )
            journal.emit(
                "audit_complete",
                audit_complete=True,
                issue_count=len(issues),
                from_cache=True,
            )
            journal.emit(
                "gate",
                audit_complete=True,
                # F1: a cached repair with repair_complete=False (failed
                # batch / failed re-audit) is replayed as NOT released — the
                # cache hit must never upgrade debt into an audited release.
                released_as_audited=cache_repair_complete,
                repair_complete=cache_repair_complete,
                from_cache=True,
            )
            return B3AuditRepairResult(
                step6=step6,
                step7=step7,
                step8=step8,
                translations_repaired=(
                    repaired if repaired is not None else dict(translation_map)
                ),
                audit_complete=True,
                from_cache=True,
                entity_context_hash=cache.entity_context_hash(),
                audit_cache_path=cache_path,
                journal_path=journal.path,
            )

        journal.emit(
            "audit_started",
            snapshot_hash=snapshot_hash,
            entity_context_enabled=cfg.entity_context_enabled,
            entity_context_hash=entity_hash,
            prompt_version=cfg.prompt_version,
            harness_version=cfg.harness_version,
        )

        # ------------------------------------------------------------------
        # 3. Chunked audit (B1).
        # ------------------------------------------------------------------
        # F2 (RV2): the audit stage is wrapped so a pre/model-call evaluator
        # failure (CoverageError/empty input, BudgetOverflowError, missing
        # role, …) writes a TERMINAL audit failure event and a fail-closed
        # gate into the append-only journal BEFORE the exception propagates
        # to the strict runner — the journal must never end on
        # audit_started/started alone. Per-chunk TRANSPORT_ERROR failures
        # are handled inside the evaluator (failed chunks -> audit_complete
        # False -> the fail-closed gate below); that transport path is
        # preserved unchanged.
        try:
            pairs = pairs_from_maps(source_map, translation_map)
            narrator_context = build_narrator_context(
                book_memory, " ".join(source_map.values())
            )
            def _journal_chunk_event(kind: str, fields: Dict[str, Any]) -> None:
                # F7: journal causality — the started event is emitted BEFORE the
                # model call (inside ChunkedAuditEvaluator._run_one_chunk) and the
                # terminal done/failed after it, so a crash during a chunk leaves
                # item-start evidence in the append-only journal instead of nothing.
                if kind == "started":
                    journal.emit(
                        "audit_chunk_started",
                        chunk=fields.get("chunk"),
                        total=fields.get("total"),
                        sub=fields.get("sub") or "",
                    )
                else:
                    journal.emit(
                        "audit_chunk_done",
                        chunk=fields.get("chunk"),
                        total=fields.get("total"),
                        status=fields.get("status"),
                        issue_count=fields.get("issue_count", 0),
                        error=fields.get("error"),
                    )

            evaluator = ChunkedAuditEvaluator(
                self._audit_backend,
                config=ChunkedAuditConfig(
                    max_input_tokens=cfg.max_input_tokens,
                    max_tokens=cfg.max_tokens,
                    overlap_tokens=cfg.overlap_tokens,
                    reasoning_budget=cfg.reasoning_budget,
                    harness_version=cfg.harness_version,
                    prompt_version=cfg.prompt_version,
                ),
                on_chunk_event=_journal_chunk_event,
            )
            outcome = evaluator(
                chapter_id=chapter_id,
                pairs=pairs,
                narrator_context=narrator_context,
                entity_context=entity_context,
                out_dir=out_dir,
                out_base="b3_audit",
            )
        except Exception as exc:  # noqa: BLE001 — fail-closed: a pre/model-call
            # audit failure is TERMINAL. The strict runner records the failed
            # step fields, but the B3 journal must carry its own terminal
            # failure event + fail-closed gate/provenance too — recorded
            # BEFORE the exception is re-raised.
            LOG.exception("B3: chunked audit evaluator failed for %s", chapter_id)
            error = f"{type(exc).__name__}: {exc}"
            journal.emit(
                "audit_failed",
                error=error,
                audit_complete=False,
            )
            journal.emit(
                "gate",
                audit_complete=False,
                released_as_audited=False,
                error=error,
            )
            raise
        journal.emit(
            "audit_complete",
            audit_complete=outcome.audit_complete,
            issue_count=outcome.issue_count,
            failed_chunks=list(outcome.failed_chunks),
            from_cache=False,
        )
        self._emit_progress(
            "b3_audit_done",
            audit_complete=outcome.audit_complete,
            issue_count=outcome.issue_count,
            chunk_count=outcome.chunk_count,
            successful_chunks=outcome.successful_chunks,
            failed_chunks=list(outcome.failed_chunks),
        )

        # ------------------------------------------------------------------
        # 4. Gate: incomplete audit -> fail-closed (never repair, never
        #    release the chapter as passed audit).
        # ------------------------------------------------------------------
        if not outcome.audit_complete:
            LOG.error(
                "B3: audit incomplete for %s (failed chunks %s) — "
                "fail-closed: chapter is NOT released as passed audit",
                chapter_id, list(outcome.failed_chunks),
            )
            cache_writer = B3AuditCache(cache_path)
            cache_writer.save(
                snapshot_hash=snapshot_hash,
                translation_hash=translation_hash,
                config_identity=config_identity,
                backend_identity_hash=backend_identity_hash,
                entity_context_hash=entity_hash,
                entity_context_enabled=cfg.entity_context_enabled,
                outcome=outcome,
                filtered=(),
                repair=None,
                translations_repaired=dict(translation_map),
            )
            step6 = {
                "status": "incomplete",
                "audit_complete": False,
                "chunk_count": outcome.chunk_count,
                "successful_chunks": outcome.successful_chunks,
                "failed_chunks": list(outcome.failed_chunks),
                "issue_count": outcome.issue_count,
                "entity_context_enabled": cfg.entity_context_enabled,
                "entity_context_hash": entity_hash,
                "from_cache": False,
            }
            step7 = {"status": "skipped", "reason": "audit_incomplete_fail_closed"}
            step8 = {
                "status": "fail_closed_audit_incomplete",
                "audit_complete": False,
                "released_as_audited": False,
            }
            journal.emit(
                "gate",
                audit_complete=False,
                released_as_audited=False,
                failed_chunks=list(outcome.failed_chunks),
            )
            return B3AuditRepairResult(
                step6=step6,
                step7=step7,
                step8=step8,
                translations_repaired=dict(translation_map),
                audit_complete=False,
                from_cache=False,
                entity_context_hash=entity_hash,
                audit_cache_path=cache_path,
                journal_path=journal.path,
            )

        # ------------------------------------------------------------------
        # 5. Hard filters (B1.1) — entity-PID issues are forced TIER_B.
        # ------------------------------------------------------------------
        filtered = apply_hard_filters(
            outcome.issues,
            source=source_map,
            translation=translation_map,
            entity_context=entity_payload,
        )
        for f in filtered:
            journal.emit(
                "finding",
                pid=str(f.issue.get("id", "")),
                category=str(f.issue.get("category", "")),
                severity=str(f.issue.get("severity", "")),
                confidence=str(f.issue.get("confidence", "")),
                verdict=f.verdict,
                filter_name=f.filter_name,
                reason=f.reason,
            )

        # ------------------------------------------------------------------
        # 6. Selective repair (B2) + single re-audit of changed PIDs.
        # ------------------------------------------------------------------
        repair_outcome: Optional[SelectiveRepairOutcome] = None
        try:
            repair_evaluator = SelectiveRepairEvaluator(
                self._repair_backend,
                reaudit_backend=self._audit_backend,
                config=SelectiveRepairConfig(
                    findings_cap=cfg.repair_findings_cap,
                    microbatch_trigger=cfg.repair_microbatch_trigger,
                    microbatch_target=cfg.repair_microbatch_target,
                    reaudit_neighbour_window=cfg.repair_reaudit_neighbour_window,
                    reaudit_full_threshold=cfg.repair_reaudit_full_threshold,
                ),
            )
            repair_outcome = repair_evaluator(
                chapter_id=chapter_id,
                source=source_map,
                translation=translation_map,
                filtered=filtered,
                entity_context=entity_context,
                narrator_context=narrator_context,
            )
        except Exception as exc:  # noqa: BLE001 — a repair failure is debt, never a crash
            LOG.exception("B3: selective repair failed for %s", chapter_id)
            repair_outcome = None
            # Fall through: the cache is written with repair=None and the
            # gate stays honest (repair_complete=False).

        if repair_outcome is not None:
            journal.emit(
                "repair_round",
                round=1,
                eligible_count=repair_outcome.eligible_count,
                committed_pids=[pid for pid, _ in repair_outcome.committed],
                passed_pids=list(repair_outcome.passed_pids),
                debt_trace=list(repair_outcome.debt_trace),
                repair_complete=repair_outcome.repair_complete,
                skipped=repair_outcome.skipped,
            )
            reaudit = repair_outcome.reaudit
            if reaudit is not None:
                journal.emit(
                    "reaudit_scope",
                    scope_pids=list(reaudit.scope),
                    full=reaudit.full,
                    issue_count=len(reaudit.issues),
                    failed=reaudit.failed,
                )

        committed = (
            {pid: text for pid, text in repair_outcome.committed}
            if repair_outcome is not None
            else {}
        )
        translations_repaired = {**translation_map, **committed}
        repair_complete = (
            repair_outcome.repair_complete if repair_outcome is not None else False
        )

        # ------------------------------------------------------------------
        # 7. Persist cache + build reports.
        # ------------------------------------------------------------------
        cache_writer = B3AuditCache(cache_path)
        cache_writer.save(
            snapshot_hash=snapshot_hash,
            translation_hash=translation_hash,
            config_identity=config_identity,
            backend_identity_hash=backend_identity_hash,
            entity_context_hash=entity_hash,
            entity_context_enabled=cfg.entity_context_enabled,
            outcome=outcome,
            filtered=filtered,
            repair=repair_outcome,
            translations_repaired=translations_repaired,
        )

        step6 = {
            "status": "complete",
            "audit_complete": True,
            "chunk_count": outcome.chunk_count,
            "successful_chunks": outcome.successful_chunks,
            "failed_chunks": list(outcome.failed_chunks),
            "issue_count": outcome.issue_count,
            "entity_context_enabled": cfg.entity_context_enabled,
            "entity_context_hash": entity_hash,
            "from_cache": False,
        }
        step7 = {
            "status": (
                "complete" if repair_complete else (
                    "failed" if repair_outcome is None else "incomplete"
                )
            ),
            "repair_complete": repair_complete,
            "eligible_count": (
                repair_outcome.eligible_count if repair_outcome is not None else 0
            ),
            "committed_pids": [pid for pid, _ in committed.items()],
            "passed_pids": (
                list(repair_outcome.passed_pids) if repair_outcome is not None else []
            ),
            "debt_trace": (
                list(repair_outcome.debt_trace) if repair_outcome is not None else []
            ),
        }
        # F1 (B3 review): the terminal gate is honest about repair debt. The
        # chapter is released as audited ONLY when the audit completed AND
        # the repair completed (every batch GOOD and the post-repair re-audit
        # succeeded). repair_complete=False (failed batch / failed re-audit /
        # repair exception) degrades the release to accepted_degraded with
        # released_as_audited=False — never a silent complete/PASS.
        if repair_complete:
            step8 = {
                "status": "complete",
                "audit_complete": True,
                "released_as_audited": True,
            }
        else:
            step8 = {
                "status": "accepted_degraded",
                "audit_complete": True,
                "released_as_audited": False,
                "repair_complete": False,
                "reason": (
                    "repair_failed" if repair_outcome is None else "repair_incomplete"
                ),
                "debt_trace": step7["debt_trace"],
            }
        journal.emit(
            "gate",
            audit_complete=True,
            released_as_audited=repair_complete,
            repair_complete=repair_complete,
        )
        self._emit_progress(
            "b3_repair_done",
            repair_complete=repair_complete,
            committed_pids=[pid for pid, _ in committed.items()],
        )

        return B3AuditRepairResult(
            step6=step6,
            step7=step7,
            step8=step8,
            translations_repaired=translations_repaired,
            audit_complete=True,
            from_cache=False,
            entity_context_hash=entity_hash,
            audit_cache_path=cache_path,
            journal_path=journal.path,
        )

    def _emit_progress(self, event: str, **fields: Any) -> None:
        progress = self._progress
        if progress is None:
            return
        emit = getattr(progress, "emit", None)
        if callable(emit):
            try:
                emit(event, **fields)
            except Exception:  # noqa: BLE001 — progress is diagnostics
                LOG.debug("B3: progress emit failed for %s", event, exc_info=True)


# ---------------------------------------------------------------------------
# Reports
# ---------------------------------------------------------------------------


def _reports_from_cache(
    *,
    cache: B3AuditCache,
    issue_count: int,
) -> Tuple[Dict[str, Any], Dict[str, Any], Dict[str, Any]]:
    repair_payload = cache.stored_repair()
    repair_complete = bool(
        repair_payload and repair_payload.get("repair_complete") is True
    )
    committed_pids = [
        pair[0] for pair in (repair_payload or {}).get("committed") or []
    ]
    passed_pids = list((repair_payload or {}).get("passed_pids") or [])
    debt_trace = list((repair_payload or {}).get("debt_trace") or [])
    step6 = {
        "status": "complete",
        "audit_complete": True,
        "issue_count": issue_count,
        "from_cache": True,
    }
    step7 = {
        "status": (
            "complete"
            if repair_complete
            else ("incomplete" if repair_payload else "failed")
        ),
        "repair_complete": repair_complete,
        "committed_pids": committed_pids,
        "passed_pids": passed_pids,
        "debt_trace": debt_trace,
        "from_cache": True,
    }
    # F1: the cache replay honors the same terminal gate as a live run — a
    # cached repair that did not complete is replayed as accepted_degraded /
    # NOT released, never silently upgraded to complete/released_as_audited.
    if repair_complete:
        step8 = {
            "status": "complete",
            "audit_complete": True,
            "released_as_audited": True,
            "from_cache": True,
        }
    else:
        step8 = {
            "status": "accepted_degraded",
            "audit_complete": True,
            "released_as_audited": False,
            "repair_complete": False,
            "reason": (
                "repair_failed" if repair_payload is None else "repair_incomplete"
            ),
            "debt_trace": debt_trace,
            "from_cache": True,
        }
    return step6, step7, step8


__all__ = [
    "B3_AUDIT_CACHE_SCHEMA",
    "B3_AUDIT_JOURNAL_SCHEMA",
    "B3AuditCache",
    "B3AuditRepair",
    "B3AuditRepairConfig",
    "B3AuditRepairResult",
    "AuditJournal",
    "render_entity_context_block",
]
