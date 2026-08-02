"""Phase 3A contract tests for pact_v4.phase3.findings."""
import dataclasses

import pytest

from pact_v4.phase1.models import Region
from pact_v4.phase3.findings import Finding, FindingStore, ForeignIdentityError

SNAPSHOT = "snap-ch044-0001"
CHUNK = "c0001"
CANDIDATE = "c0001-A"
SOURCE = "src-ch044"
POLICY = "qwen_fidelity/v1"


def _finding(**overrides) -> Finding:
    kwargs = dict(
        detector="qwen_fidelity",
        category="semantic_drop",
        evidence={"note": "dropped clause", "excerpt": "..."},
        region=Region(pid="p00001", start=0, end=10),
        source_id=SOURCE,
        snapshot_id=SNAPSHOT,
        chunk_id=CHUNK,
        candidate_id=CANDIDATE,
        policy_version=POLICY,
    )
    kwargs.update(overrides)
    return Finding(**kwargs)


# 1. Multiple findings on the same PID — all retained, each evidence intact.
def test_multiple_findings_same_pid_all_retained():
    f1 = _finding(evidence={"note": "issue A"})
    f2 = _finding(evidence={"note": "issue B"}, detector="deterministic_integrity")
    store = FindingStore.create(SNAPSHOT, [f1, f2])
    assert len(store) == 2
    pid_findings = store.by_pid("p00001")
    assert len(pid_findings) == 2
    evidences = {f.evidence["note"] for f in pid_findings}
    assert evidences == {"issue A", "issue B"}


# 4. Evidence and identity round-trip through store.
def test_round_trip_through_store():
    f = _finding()
    store = FindingStore.create(SNAPSHOT).add(f)
    stored = store.findings[0]
    assert stored.evidence == f.evidence
    assert stored.detector == f.detector
    assert stored.category == f.category
    assert stored.source_id == SOURCE
    assert stored.snapshot_id == SNAPSHOT
    assert stored.chunk_id == CHUNK
    assert stored.candidate_id == CANDIDATE
    assert stored.policy_version == POLICY
    assert stored.content_hash == f.content_hash
    payload = stored.to_payload()
    assert payload["evidence"] == f.evidence
    assert payload["region"] == {"pid": "p00001", "start": 0, "end": 10}


# 5a. Reject foreign snapshot identity.
def test_reject_foreign_snapshot_identity():
    f = _finding(snapshot_id="some-other-snapshot")
    with pytest.raises(ForeignIdentityError):
        FindingStore.create(SNAPSHOT, [f])


def test_reject_foreign_snapshot_identity_via_add():
    store = FindingStore.create(SNAPSHOT)
    f = _finding(snapshot_id="some-other-snapshot")
    with pytest.raises(ForeignIdentityError):
        store.add(f)


# 5b. Reject stale identity (e.g. snapshot that no longer matches expected
# context after a chapter re-snapshot).
def test_reject_stale_snapshot_identity():
    store = FindingStore.create(SNAPSHOT)
    stale = _finding(snapshot_id="snap-ch044-0000-stale")
    with pytest.raises(ForeignIdentityError):
        store.add(stale)


# 5c. Reject incomplete identity (missing required identity fields).
@pytest.mark.parametrize(
    "field_name",
    ["detector", "category", "source_id", "snapshot_id", "chunk_id", "candidate_id", "policy_version"],
)
def test_reject_incomplete_identity(field_name):
    with pytest.raises(ValueError):
        _finding(**{field_name: ""})


def test_reject_non_finding_in_store():
    with pytest.raises(ValueError):
        FindingStore.create(SNAPSHOT, [object()])  # type: ignore[list-item]


# 6. Immutability.
def test_finding_is_immutable():
    f = _finding()
    with pytest.raises(dataclasses.FrozenInstanceError):
        f.detector = "other"  # type: ignore[misc]


def test_finding_store_is_immutable():
    store = FindingStore.create(SNAPSHOT)
    with pytest.raises(dataclasses.FrozenInstanceError):
        store.expected_snapshot_id = "other"  # type: ignore[misc]


def test_content_hash_stable_and_reproducible():
    f1 = _finding()
    f2 = _finding()
    assert f1.content_hash == f2.content_hash
    assert f1.content_hash == _finding().content_hash  # repeated computation


def test_content_hash_changes_with_content():
    f1 = _finding()
    f2 = _finding(evidence={"note": "different evidence"})
    assert f1.content_hash != f2.content_hash

    f3 = _finding(region=Region(pid="p00001", start=0, end=11))
    assert f1.content_hash != f3.content_hash

    f4 = _finding(category="other_category")
    assert f1.content_hash != f4.content_hash


# 8. Conflicting detector/category findings on the same region — both retained.
def test_conflicting_findings_same_region_both_retained():
    region = Region(pid="p00002", start=5, end=15)
    f_a = _finding(region=region, detector="qwen_fidelity", category="mistranslation",
                    evidence={"verdict": "fidelity issue"})
    f_b = _finding(region=region, detector="gemma_russian", category="awkward_phrasing",
                    evidence={"verdict": "style issue"})
    store = FindingStore.create(SNAPSHOT, [f_a, f_b])
    assert len(store) == 2
    kept = store.by_pid("p00002")
    detectors = {f.detector for f in kept}
    categories = {f.category for f in kept}
    assert detectors == {"qwen_fidelity", "gemma_russian"}
    assert categories == {"mistranslation", "awkward_phrasing"}
    # Neither overwrote the other's evidence.
    for f in kept:
        assert f.evidence["verdict"] in ("fidelity issue", "style issue")


# 9. Finding.from_payload round-trip: content_hash is recomputed, finding_id
#    is preserved, and a stale/tampered content_hash is ignored.
def test_finding_from_payload_round_trip():
    f = _finding()
    restored = Finding.from_payload(f.to_payload())
    assert restored == f
    assert restored.content_hash == f.content_hash
    assert restored.finding_id == f.finding_id


def test_finding_from_payload_recomputes_content_hash():
    f = _finding()
    payload = f.to_payload()
    payload["content_hash"] = "0" * 64  # tampered; must not be trusted
    restored = Finding.from_payload(payload)
    assert restored.content_hash == f.content_hash
    assert restored.content_hash != payload["content_hash"]


def test_finding_from_payload_rejects_bad_shape():
    with pytest.raises(ValueError):
        Finding.from_payload({"detector": "x"})


def test_finding_store_to_from_payload_round_trip():
    store = FindingStore.create(SNAPSHOT, [
        _finding(evidence={"note": "a"}, category="omission"),
        _finding(evidence={"note": "b"}, category="addition"),
    ])
    restored = FindingStore.from_payload(store.to_payload())
    assert restored.expected_snapshot_id == SNAPSHOT
    assert len(restored) == len(store)
    assert {f.content_hash for f in restored} == {f.content_hash for f in store}
    assert {f.category for f in restored} == {"omission", "addition"}


def test_finding_store_from_payload_rejects_foreign_snapshot():
    store = FindingStore.create(SNAPSHOT, [_finding()])
    payload = store.to_payload()
    payload["expected_snapshot_id"] = "some-other-snapshot"
    with pytest.raises(ForeignIdentityError):
        FindingStore.from_payload(payload)
