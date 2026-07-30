"""Phase 3A: deterministic region resolver.

Builds the set of regions requiring audit/repair coverage from a
collection of ``pact_v4.phase3.findings.Finding`` objects, per
docs/architecture/V4_FINAL_REVIEW_AND_IMPLEMENTATION_PLAN_RU_v2.md
("### 3A. Immutable finding store": "Separate detector findings, region
resolver, no overlap merge. Evidence and multiple findings per region
remain intact.").

Design constraints enforced here:

  * Overlapping and *adjacent* (touching) regions on the same PID are
    grouped into one resolved coverage region — but the resolver never
    merges the underlying findings into a combined finding, and never
    elects one finding as "the" finding for a region. Each
    ``ResolvedRegion`` only references the *content hashes* of the
    findings that produced it; the findings themselves are untouched and
    remain individually retrievable (e.g. from the ``FindingStore`` they
    came from).
  * The result is order-independent: building the plan from the same
    findings in any input order yields an identical ``RegionPlan`` once
    PIDs/regions are sorted and each region's finding-hash list is sorted
    by content hash (see ``RegionPlan.to_payload`` / equality of the
    dataclasses themselves, which is exactly this normalised form).
  * No side effects on disk — this is a pure, in-memory computation.

Out of scope: repair execution, the assembled-chapter audit itself
(Phase 3B), and any model calls.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Sequence, Tuple

from pact_v4.phase3.findings import Finding

__all__ = ["ResolvedRegion", "RegionPlan", "resolve_regions"]


@dataclass(frozen=True)
class ResolvedRegion:
    """One coverage region on a single PID, with its contributing evidence trail.

    ``finding_content_hashes`` is sorted and deduplicated so it is a
    stable, order-independent reference to every finding that contributed
    to this region — the findings themselves are not embedded here (to
    avoid a second, possibly-stale copy of their content); callers look
    them up by content hash in the originating ``FindingStore``.
    """

    pid: str
    start: int
    end: int
    finding_content_hashes: Tuple[str, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.pid, str) or not self.pid:
            raise ValueError("ResolvedRegion: pid must be a non-empty string")
        if self.start < 0 or self.end < self.start:
            raise ValueError(f"ResolvedRegion: invalid span [{self.start}, {self.end}) for {self.pid}")
        if not self.finding_content_hashes:
            raise ValueError(f"ResolvedRegion: {self.pid} must reference at least one finding")

    def to_payload(self) -> Dict[str, Any]:
        return {
            "pid": self.pid,
            "start": self.start,
            "end": self.end,
            "finding_content_hashes": list(self.finding_content_hashes),
        }


@dataclass(frozen=True)
class RegionPlan:
    """Deterministic, order-independent set of regions requiring coverage.

    ``regions`` is always sorted by ``(pid, start, end)`` regardless of the
    order findings were supplied in, so two plans built from the same
    finding set in different input orders compare equal.
    """

    regions: Tuple[ResolvedRegion, ...]

    def to_payload(self) -> Dict[str, Any]:
        return {"regions": [region.to_payload() for region in self.regions]}

    def for_pid(self, pid: str) -> Tuple[ResolvedRegion, ...]:
        return tuple(region for region in self.regions if region.pid == pid)

    def __len__(self) -> int:
        return len(self.regions)

    def __iter__(self):
        return iter(self.regions)


def resolve_regions(findings: Sequence[Finding] | Iterable[Finding]) -> RegionPlan:
    """Build a deterministic ``RegionPlan`` from an unordered collection of findings.

    Findings sharing a PID whose regions overlap or touch (``next.start <=
    current.end``) are grouped into one ``ResolvedRegion`` spanning their
    union; findings on the same PID whose regions do not touch produce
    separate regions. No finding content is combined or dropped: every
    contributing finding's ``content_hash`` is recorded on the resulting
    region.
    """
    findings = list(findings)

    by_pid: Dict[str, List[Finding]] = {}
    for finding in findings:
        by_pid.setdefault(finding.region.pid, []).append(finding)

    resolved: List[ResolvedRegion] = []
    for pid in sorted(by_pid):
        items = sorted(
            by_pid[pid],
            key=lambda f: (f.region.start, f.region.end, f.content_hash),
        )
        current_start = None
        current_end = None
        current_hashes: List[str] = []
        for f in items:
            if current_start is None:
                current_start, current_end = f.region.start, f.region.end
                current_hashes = [f.content_hash]
                continue
            if f.region.start <= current_end:
                current_end = max(current_end, f.region.end)
                current_hashes.append(f.content_hash)
            else:
                resolved.append(_make_region(pid, current_start, current_end, current_hashes))
                current_start, current_end = f.region.start, f.region.end
                current_hashes = [f.content_hash]
        if current_start is not None:
            resolved.append(_make_region(pid, current_start, current_end, current_hashes))

    resolved.sort(key=lambda region: (region.pid, region.start, region.end))
    return RegionPlan(regions=tuple(resolved))


def _make_region(pid: str, start: int, end: int, hashes: List[str]) -> ResolvedRegion:
    deduped = tuple(sorted(set(hashes)))
    return ResolvedRegion(pid=pid, start=start, end=end, finding_content_hashes=deduped)
