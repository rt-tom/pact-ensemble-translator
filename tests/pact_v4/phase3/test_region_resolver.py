"""Phase 3A contract tests for pact_v4.phase3.region_resolver."""
import random

from pact_v4.phase1.models import Region
from pact_v4.phase3.findings import Finding
from pact_v4.phase3.region_resolver import RegionPlan, resolve_regions

SNAPSHOT = "snap-ch044-0001"
CHUNK = "c0001"
CANDIDATE = "c0001-A"
SOURCE = "src-ch044"


def _finding(pid: str, start: int, end: int, detector: str = "qwen_fidelity", note: str = "x") -> Finding:
    return Finding(
        detector=detector,
        category="semantic_drop",
        evidence={"note": note},
        region=Region(pid=pid, start=start, end=end),
        source_id=SOURCE,
        snapshot_id=SNAPSHOT,
        chunk_id=CHUNK,
        candidate_id=CANDIDATE,
        policy_version="v1",
    )


# 7. Empty finding set.
def test_empty_findings_produce_empty_plan():
    plan = resolve_regions([])
    assert isinstance(plan, RegionPlan)
    assert len(plan) == 0
    assert plan.to_payload() == {"regions": []}


# 2. Overlapping regions merge into one coverage region, findings not merged.
def test_overlapping_regions_grouped_without_merging_findings():
    f1 = _finding("p00001", 0, 10, note="first")
    f2 = _finding("p00001", 5, 15, note="second")
    plan = resolve_regions([f1, f2])
    assert len(plan) == 1
    region = plan.regions[0]
    assert (region.pid, region.start, region.end) == ("p00001", 0, 15)
    assert set(region.finding_content_hashes) == {f1.content_hash, f2.content_hash}


# 2b. Adjacent (touching) regions also grouped.
def test_adjacent_regions_grouped():
    f1 = _finding("p00001", 0, 10, note="first")
    f2 = _finding("p00001", 10, 20, note="second")  # touches at 10
    plan = resolve_regions([f1, f2])
    assert len(plan) == 1
    region = plan.regions[0]
    assert (region.start, region.end) == (0, 20)
    assert set(region.finding_content_hashes) == {f1.content_hash, f2.content_hash}


# 2c. Non-touching regions on the same PID stay separate.
def test_non_touching_regions_stay_separate():
    f1 = _finding("p00001", 0, 5, note="first")
    f2 = _finding("p00001", 10, 15, note="second")
    plan = resolve_regions([f1, f2])
    assert len(plan) == 2
    spans = sorted((r.start, r.end) for r in plan.regions)
    assert spans == [(0, 5), (10, 15)]


def test_different_pids_stay_separate():
    f1 = _finding("p00001", 0, 5)
    f2 = _finding("p00002", 0, 5)
    plan = resolve_regions([f1, f2])
    assert len(plan) == 2
    assert {r.pid for r in plan.regions} == {"p00001", "p00002"}


# 3. Order independence.
def test_order_independence():
    findings = [
        _finding("p00003", 20, 30, note="c"),
        _finding("p00001", 0, 10, note="a"),
        _finding("p00001", 5, 15, note="b"),
        _finding("p00002", 0, 5, note="d"),
        _finding("p00001", 100, 110, note="e"),
    ]
    shuffled = list(findings)
    random.Random(42).shuffle(shuffled)
    assert shuffled != findings or len(findings) <= 1  # sanity: order actually differs

    plan_a = resolve_regions(findings)
    plan_b = resolve_regions(shuffled)

    payload_a = plan_a.to_payload()
    payload_b = plan_b.to_payload()
    # Normalise finding-hash list order defensively (resolver already sorts it).
    for payload in (payload_a, payload_b):
        for region in payload["regions"]:
            region["finding_content_hashes"] = sorted(region["finding_content_hashes"])
    assert payload_a == payload_b


# 4. Finding identities survive resolution (region references content hashes).
def test_region_references_contributing_finding_identities():
    f1 = _finding("p00001", 0, 10, note="a")
    f2 = _finding("p00001", 3, 12, note="b")
    f3 = _finding("p00001", 50, 60, note="c")
    plan = resolve_regions([f1, f2, f3])
    assert len(plan) == 2
    merged = plan.for_pid("p00001")[0]
    assert set(merged.finding_content_hashes) == {f1.content_hash, f2.content_hash}
    isolated = plan.for_pid("p00001")[1]
    assert isolated.finding_content_hashes == (f3.content_hash,)


# 8. Conflicting detector/category findings on same region both contribute,
# no implicit "primary" selection.
def test_conflicting_findings_both_referenced_in_region():
    region = Region(pid="p00002", start=5, end=15)
    f_a = Finding(
        detector="qwen_fidelity", category="mistranslation", evidence={"verdict": "a"},
        region=region, source_id=SOURCE, snapshot_id=SNAPSHOT, chunk_id=CHUNK,
        candidate_id=CANDIDATE, policy_version="v1",
    )
    f_b = Finding(
        detector="gemma_russian", category="awkward_phrasing", evidence={"verdict": "b"},
        region=region, source_id=SOURCE, snapshot_id=SNAPSHOT, chunk_id=CHUNK,
        candidate_id=CANDIDATE, policy_version="v1",
    )
    plan = resolve_regions([f_a, f_b])
    assert len(plan) == 1
    resolved = plan.regions[0]
    assert set(resolved.finding_content_hashes) == {f_a.content_hash, f_b.content_hash}
