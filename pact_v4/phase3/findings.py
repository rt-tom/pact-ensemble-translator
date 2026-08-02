"""Phase 3A: immutable finding model and finding store.

Canonical source: docs/architecture/V4_FINAL_REVIEW_AND_IMPLEMENTATION_PLAN_RU_v2.md
("Исполнимые контракты до реализации" item 6, "### 3A. Immutable finding
store"):

    Immutable stable IDs с detector/category/evidence/region. Разные
    findings нельзя объединять по span overlap.

This module intentionally replaces the placeholder ``Finding`` in
``pact_v4.phase1.models`` (which only carries finding_id/description/
severity) with a richer, still-immutable contract that also records the
source/snapshot/chunk/candidate identity a finding was produced against,
its detector's policy version, and a deterministic content-derived
identity hash. The Phase 1A ``Finding`` and its schema
(``docs/schemas/v4_findings.schema.json``) are left untouched — nothing
else in the repository depends on them.

Out of scope here: any Qwen/Gemma audit call, repair execution, challenge/
dispute flow, convergence, or terminal-state transitions (Phase 3B/4).
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, Mapping, Tuple

from pact_v4.phase1.models import Region, canonical_json_hash, _require_exact_keys

__all__ = [
    "Finding",
    "FindingStore",
    "ForeignIdentityError",
]


class ForeignIdentityError(ValueError):
    """Raised when a finding references identity outside the expected context.

    Subclasses ``ValueError`` so existing ``except ValueError`` handling
    elsewhere in the v4 contracts keeps working, while still giving callers
    a specific type to catch for this failure mode.
    """


def _require_nonempty_str(value: Any, field_name: str) -> None:
    if not isinstance(value, str) or not value:
        raise ValueError(f"Finding: {field_name} must be a non-empty string (incomplete identity)")


def _require_json_serializable(value: Any, field_name: str) -> Any:
    try:
        json.dumps(value, ensure_ascii=False, sort_keys=True)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"Finding: {field_name} must be JSON-serialisable structured evidence") from exc
    return value


@dataclass(frozen=True)
class Finding:
    """Immutable audit finding produced by a single detector run.

    Identity fields (``source_id``, ``snapshot_id``, ``chunk_id``,
    ``candidate_id``) are opaque hashable identifiers (strings) — Phase 2C
    candidate-scoring is not merged into ``v4.0`` yet, so this module does
    not import or assume any concrete candidate/source type. Callers pass
    whatever stable identity string they already have (e.g. a content hash
    from ``pact_v4.phase1.models``).

    ``content_hash`` is computed deterministically from the finding's full
    content (detector, category, evidence, region, all four identity
    fields, and policy_version) — two findings built from identical inputs
    always hash identically, and changing any single input changes the
    hash. It is the finding's stable content identity: the store and the
    region resolver both use it, never a caller-supplied id, to refer to
    "this exact finding".
    """

    detector: str
    category: str
    evidence: Any
    region: Region
    source_id: str
    snapshot_id: str
    chunk_id: str
    candidate_id: str
    policy_version: str
    finding_id: str = ""
    content_hash: str = field(init=False)

    def __post_init__(self) -> None:
        _require_nonempty_str(self.detector, "detector")
        _require_nonempty_str(self.category, "category")
        _require_nonempty_str(self.source_id, "source_id")
        _require_nonempty_str(self.snapshot_id, "snapshot_id")
        _require_nonempty_str(self.chunk_id, "chunk_id")
        _require_nonempty_str(self.candidate_id, "candidate_id")
        _require_nonempty_str(self.policy_version, "policy_version")
        if not isinstance(self.region, Region):
            raise ValueError("Finding: region must be a Region instance")
        evidence = _require_json_serializable(self.evidence, "evidence")
        object.__setattr__(self, "evidence", evidence)

        computed = canonical_json_hash({
            "artifact": "pact-v4-finding/v1",
            "detector": self.detector,
            "category": self.category,
            "evidence": evidence,
            "region": {
                "pid": self.region.pid,
                "start": self.region.start,
                "end": self.region.end,
            },
            "source_id": self.source_id,
            "snapshot_id": self.snapshot_id,
            "chunk_id": self.chunk_id,
            "candidate_id": self.candidate_id,
            "policy_version": self.policy_version,
        })
        object.__setattr__(self, "content_hash", computed)
        if not self.finding_id:
            object.__setattr__(self, "finding_id", computed)

    def to_payload(self) -> Dict[str, Any]:
        """Plain, JSON-serialisable representation for provenance/caching."""
        return {
            "finding_id": self.finding_id,
            "content_hash": self.content_hash,
            "detector": self.detector,
            "category": self.category,
            "evidence": self.evidence,
            "region": {
                "pid": self.region.pid,
                "start": self.region.start,
                "end": self.region.end,
            },
            "source_id": self.source_id,
            "snapshot_id": self.snapshot_id,
            "chunk_id": self.chunk_id,
            "candidate_id": self.candidate_id,
            "policy_version": self.policy_version,
        }

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> "Finding":
        """Reconstruct a ``Finding`` from ``to_payload`` output.

        ``content_hash`` is deliberately not read from the payload: it is
        recomputed deterministically from content in ``__post_init__``, so a
        tampered or stale hash in the payload cannot be smuggled in as the
        finding's identity. ``finding_id`` is preserved when present (it is
        only auto-derived when empty), keeping the persisted id stable across
        a resume round-trip.
        """
        _require_exact_keys(
            payload,
            {
                "finding_id", "content_hash", "detector", "category", "evidence",
                "region", "source_id", "snapshot_id", "chunk_id", "candidate_id",
                "policy_version",
            },
            "Finding payload",
        )
        region = payload["region"]
        if not isinstance(region, Mapping):
            raise ValueError("Finding payload: region must be a JSON object")
        _require_exact_keys(region, {"pid", "start", "end"}, "Finding payload region")
        return cls(
            detector=payload["detector"],
            category=payload["category"],
            evidence=payload["evidence"],
            region=Region(
                pid=region["pid"],
                start=region["start"],
                end=region["end"],
            ),
            source_id=payload["source_id"],
            snapshot_id=payload["snapshot_id"],
            chunk_id=payload["chunk_id"],
            candidate_id=payload["candidate_id"],
            policy_version=payload["policy_version"],
            finding_id=payload["finding_id"],
        )


@dataclass(frozen=True)
class FindingStore:
    """Immutable collection of findings scoped to one expected snapshot.

    Construction (``create``) and the functional ``add``/``add_many``
    methods are the only supported ways to build a store; both validate
    every finding against ``expected_snapshot_id`` and reject anything with
    a foreign or incomplete identity. Findings are never merged, deduped
    by region/PID, or given a hidden "primary" status — multiple findings
    that share a PID or overlapping region are all retained independently
    (see ``pact_v4.phase3.region_resolver`` for how repair regions are
    derived from them without collapsing the evidence).
    """

    expected_snapshot_id: str
    findings: Tuple[Finding, ...] = ()

    def __post_init__(self) -> None:
        _require_nonempty_str(self.expected_snapshot_id, "expected_snapshot_id")
        for finding in self.findings:
            self._validate(finding)

    def _validate(self, finding: Finding) -> None:
        if not isinstance(finding, Finding):
            raise ValueError("FindingStore: only Finding instances may be stored")
        if finding.snapshot_id != self.expected_snapshot_id:
            raise ForeignIdentityError(
                f"Foreign identity: finding {finding.finding_id} snapshot_id="
                f"{finding.snapshot_id!r}, expected {self.expected_snapshot_id!r}"
            )

    @classmethod
    def create(cls, expected_snapshot_id: str, findings: Iterable[Finding] = ()) -> "FindingStore":
        return cls(expected_snapshot_id=expected_snapshot_id, findings=tuple(findings))

    def add(self, finding: Finding) -> "FindingStore":
        """Return a new store with ``finding`` appended (functional immutability)."""
        self._validate(finding)
        return FindingStore(
            expected_snapshot_id=self.expected_snapshot_id,
            findings=self.findings + (finding,),
        )

    def add_many(self, findings: Iterable[Finding]) -> "FindingStore":
        result: "FindingStore" = self
        for finding in findings:
            result = result.add(finding)
        return result

    def by_pid(self, pid: str) -> Tuple[Finding, ...]:
        return tuple(f for f in self.findings if f.region.pid == pid)

    def __len__(self) -> int:
        return len(self.findings)

    def __iter__(self):
        return iter(self.findings)

    def to_payload(self) -> Dict[str, Any]:
        """Serialisable round-trip form (used by the strict driver's Step 6
        artifact: findings are persisted per run and reloaded on resume)."""
        return {
            "expected_snapshot_id": self.expected_snapshot_id,
            "findings": [finding.to_payload() for finding in self.findings],
        }

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> "FindingStore":
        """Rebuild a store from ``to_payload`` output.

        Every finding is re-validated against ``expected_snapshot_id`` on
        reconstruction (``create``), so a foreign-snapshot finding cannot
        silently re-enter the store through a stale artifact.
        """
        _require_exact_keys(
            payload, {"expected_snapshot_id", "findings"}, "FindingStore payload"
        )
        findings = payload["findings"]
        if not isinstance(findings, list):
            raise ValueError("FindingStore payload: findings must be an array")
        return cls.create(
            expected_snapshot_id=payload["expected_snapshot_id"],
            findings=[Finding.from_payload(item) for item in findings],
        )
