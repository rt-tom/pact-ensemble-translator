"""Phase 3B contract tests for pact_v4.phase3.audit (run_chapter_audit)."""
from __future__ import annotations

import json
from typing import Dict, Tuple

import pytest

from pact_v4.phase1.models import (
    Candidate,
    ChunkPlan,
    ChunkPlanArtifact,
    ConfigArtifact,
    Snapshot,
    SourceArtifact,
    canonical_json_hash,
)
from pact_v4.phase2.cascade import DeterministicGateData
from pact_v4.phase3.assembly import AssembledChapter
from pact_v4.phase3.audit import AuditCache, run_chapter_audit


def _hash(seed: str) -> str:
    return canonical_json_hash({"seed": seed})


def _source(chapter_id: str = "ch044", texts: Dict[str, str] | None = None) -> SourceArtifact:
    texts = texts or {}
    pairs = tuple(
        (f"p{i:05d}", texts.get(f"p{i:05d}", f"English sentence {i}."))
        for i in range(16)
    )
    return SourceArtifact(chapter_id=chapter_id, source=pairs)


def _snapshot(source: SourceArtifact) -> Snapshot:
    return Snapshot(
        chapter_id=source.chapter_id,
        pids=tuple(pid for pid, _ in source.source),
        context="ctx-v1",
        glossary_hash=_hash("glossary"),
        book_memory_hash=_hash("book_memory"),
        chapter_memory_hash=_hash("chapter_memory"),
    )


def _two_chunk_plan(snapshot: Snapshot) -> Tuple[ChunkPlanArtifact, ChunkPlan, ChunkPlan]:
    half = len(snapshot.pids) // 2
    # 50 words/PID keeps each half comfortably inside ChunkPlan's fixed
    # word-based bounds (MIN_WORDS=280/MAX_WORDS=640).
    chunk1 = ChunkPlan(
        chunk_id="chunk0001", snapshot_hash=snapshot.snapshot_hash,
        pids=snapshot.pids[:half], word_counts=tuple(50 for _ in snapshot.pids[:half]),
    )
    chunk2 = ChunkPlan(
        chunk_id="chunk0002", snapshot_hash=snapshot.snapshot_hash,
        pids=snapshot.pids[half:], word_counts=tuple(50 for _ in snapshot.pids[half:]),
    )
    artifact = ChunkPlanArtifact.create(snapshot, (chunk1, chunk2))
    return artifact, chunk1, chunk2


def _config() -> ConfigArtifact:
    return ConfigArtifact(version="v1", values={"model": "qwen-mock"})


def _candidate(
    *, chunk: ChunkPlan, suffix: str, source: SourceArtifact, snapshot: Snapshot,
    chunk_plan: ChunkPlanArtifact, config: ConfigArtifact, overrides: Dict[str, str] | None = None,
) -> Candidate:
    overrides = overrides or {}
    translation = tuple(
        (pid, overrides.get(pid, f"Перевод {int(pid[1:])}.")) for pid in chunk.pids
    )
    return Candidate.create(
        candidate_id=f"{chunk.chunk_id}:{suffix}",
        chunk_id=chunk.chunk_id,
        role="fidelity_first",
        translation=translation,
        source=source, snapshot=snapshot, chunk_plan=chunk_plan, config=config,
    )


def _env(source_texts: Dict[str, str] | None = None, translation_overrides: Dict[str, str] | None = None):
    source = _source(texts=source_texts)
    snapshot = _snapshot(source)
    chunk_plan, chunk1, chunk2 = _two_chunk_plan(snapshot)
    config = _config()
    overrides = translation_overrides or {}
    c1 = _candidate(chunk=chunk1, suffix="A", source=source, snapshot=snapshot, chunk_plan=chunk_plan, config=config, overrides=overrides)
    c2 = _candidate(chunk=chunk2, suffix="A", source=source, snapshot=snapshot, chunk_plan=chunk_plan, config=config, overrides=overrides)
    candidates = {chunk1.chunk_id: c1, chunk2.chunk_id: c2}
    chapter = AssembledChapter.assemble(source=source, snapshot=snapshot, chunk_plan=chunk_plan, config=config, candidates=candidates)
    return source, snapshot, chunk_plan, chunk1, chunk2, config, candidates, chapter


def _issues_json(issues: list) -> str:
    return json.dumps({"issues": issues}, ensure_ascii=False)


class ScriptedEvaluator:
    """Mock Qwen/Gemma audit evaluator: pops a scripted output (or raises) per chunk_id, in order."""

    def __init__(self, outputs: Dict[str, list]):
        self._outputs = {k: list(v) for k, v in outputs.items()}
        self.calls: list = []

    def __call__(self, **kwargs):
        chunk_id = kwargs["chunk_id"]
        self.calls.append(kwargs)
        queue = self._outputs.get(chunk_id, [])
        if not queue:
            raise AssertionError(f"ScriptedEvaluator: no more scripted outputs for {chunk_id}")
        result = queue.pop(0)
        if isinstance(result, Exception):
            raise result
        return result

    def call_count(self, chunk_id: str) -> int:
        return sum(1 for call in self.calls if call["chunk_id"] == chunk_id)


def _no_issue_evaluator(chunk_ids, repeats=1) -> ScriptedEvaluator:
    return ScriptedEvaluator({cid: [_issues_json([])] * repeats for cid in chunk_ids})


# 1. Deterministic findings: missing translation, numeric loss, mixed-script, glossary.
def test_deterministic_findings_cover_all_violation_categories():
    source_texts = {
        "p00001": "There are 3 cats here.",
        "p00002": "Meet Alice today.",
    }
    translation_overrides = {
        "p00000": "",  # missing
        "p00001": "Там кошки.",  # numeric value 3 lost
        "p00002": "Здравствуй, сегодня.",  # glossary term not used
        "p00003": "Привет OK мир.",  # mixed script
    }
    source, snapshot, chunk_plan, chunk1, chunk2, config, candidates, chapter = _env(
        source_texts=source_texts, translation_overrides=translation_overrides
    )
    det_data = DeterministicGateData(glossary_terms=(("Alice", "Алиса"),))

    outcome = run_chapter_audit(
        chapter=chapter, source=source, chunk_plan=chunk_plan, candidates=candidates,
        qwen_evaluator=_no_issue_evaluator([chunk1.chunk_id, chunk2.chunk_id]),
        gemma_evaluator=_no_issue_evaluator([chunk1.chunk_id, chunk2.chunk_id]),
        det_data=det_data,
    )

    assert outcome.status == "complete"
    by_category = {f.category for f in outcome.store if f.detector == "deterministic_integrity"}
    assert by_category == {"missing", "number", "glossary_consistency", "mixed_script"}
    missing = [f for f in outcome.store if f.category == "missing"][0]
    assert missing.region.pid == "p00000"


# 2. Full traversal: a violation on the very last PID of the very last chunk is still found.
def test_deterministic_findings_cover_last_pid_of_last_chunk():
    translation_overrides = {"p00015": ""}
    source, snapshot, chunk_plan, chunk1, chunk2, config, candidates, chapter = _env(
        translation_overrides=translation_overrides
    )
    outcome = run_chapter_audit(
        chapter=chapter, source=source, chunk_plan=chunk_plan, candidates=candidates,
        qwen_evaluator=_no_issue_evaluator([chunk1.chunk_id, chunk2.chunk_id]),
        gemma_evaluator=_no_issue_evaluator([chunk1.chunk_id, chunk2.chunk_id]),
    )
    assert outcome.status == "complete"
    pids_with_findings = {f.region.pid for f in outcome.store}
    assert "p00015" in pids_with_findings


# 3. Qwen and Gemma issues are parsed into correctly-identified Findings.
def test_qwen_and_gemma_issues_parsed_into_findings():
    source, snapshot, chunk_plan, chunk1, chunk2, config, candidates, chapter = _env()
    qwen = ScriptedEvaluator({
        chunk1.chunk_id: [_issues_json([{"pid": "p00000", "category": "omission", "note": "dropped a clause"}])],
        chunk2.chunk_id: [_issues_json([])],
    })
    gemma = ScriptedEvaluator({
        chunk1.chunk_id: [_issues_json([])],
        chunk2.chunk_id: [_issues_json([{"pid": "p00008", "category": "register", "note": "too formal", "excerpt": "..."}])],
    })

    outcome = run_chapter_audit(
        chapter=chapter, source=source, chunk_plan=chunk_plan, candidates=candidates,
        qwen_evaluator=qwen, gemma_evaluator=gemma,
    )

    assert outcome.status == "complete"
    qwen_findings = [f for f in outcome.store if f.detector == "qwen_chapter_audit"]
    gemma_findings = [f for f in outcome.store if f.detector == "gemma_russian_review"]
    assert len(qwen_findings) == 1
    assert qwen_findings[0].category == "omission"
    assert qwen_findings[0].region.pid == "p00000"
    assert qwen_findings[0].chunk_id == chunk1.chunk_id
    assert qwen_findings[0].candidate_id == candidates[chunk1.chunk_id].candidate_id
    assert qwen_findings[0].policy_version == "qwen_chapter_audit/v1"
    assert len(gemma_findings) == 1
    assert gemma_findings[0].category == "register"
    assert gemma_findings[0].region.pid == "p00008"
    assert gemma_findings[0].chunk_id == chunk2.chunk_id


# 4. Gemma is never given the English source (spec: "Russian-only review без оригинала").
def test_gemma_evaluator_never_receives_source():
    source, snapshot, chunk_plan, chunk1, chunk2, config, candidates, chapter = _env()
    gemma = _no_issue_evaluator([chunk1.chunk_id, chunk2.chunk_id])

    run_chapter_audit(
        chapter=chapter, source=source, chunk_plan=chunk_plan, candidates=candidates,
        qwen_evaluator=_no_issue_evaluator([chunk1.chunk_id, chunk2.chunk_id]),
        gemma_evaluator=gemma,
    )

    assert len(gemma.calls) == 2
    for call in gemma.calls:
        assert set(call) == {"chunk_id", "translation"}
        assert "source" not in call


# 5. Resumability: a failed unit is retried on a second run with the same cache; the
#    already-successful units are not re-called.
def test_resume_retries_only_failed_units():
    source, snapshot, chunk_plan, chunk1, chunk2, config, candidates, chapter = _env()
    cache = AuditCache()

    qwen = ScriptedEvaluator({
        chunk1.chunk_id: [RuntimeError("llama-server timeout"), _issues_json([])],
        chunk2.chunk_id: [_issues_json([])],
    })
    gemma = ScriptedEvaluator({
        chunk1.chunk_id: [_issues_json([])],
        chunk2.chunk_id: [_issues_json([])],
    })

    first = run_chapter_audit(
        chapter=chapter, source=source, chunk_plan=chunk_plan, candidates=candidates,
        qwen_evaluator=qwen, gemma_evaluator=gemma, cache=cache,
    )
    assert first.status == "incomplete"
    assert first.failed_units == ((chunk1.chunk_id, "qwen_chapter_audit", "llama-server timeout"),)
    # even though nothing else found any issues, incompleteness is never silently read as success
    assert len(first.store) == 0

    second = run_chapter_audit(
        chapter=chapter, source=source, chunk_plan=chunk_plan, candidates=candidates,
        qwen_evaluator=qwen, gemma_evaluator=gemma, cache=cache,
    )
    assert second.status == "complete"
    assert second.failed_units == ()

    # Only the previously-failed unit was re-attempted; everything else was reused from cache.
    assert qwen.call_count(chunk1.chunk_id) == 2
    assert qwen.call_count(chunk2.chunk_id) == 1
    assert gemma.call_count(chunk1.chunk_id) == 1
    assert gemma.call_count(chunk2.chunk_id) == 1


# 5b. Resumability must key on candidate_id, not just chapter_hash: two different winning
#     candidates for the same chunk that happen to produce byte-identical translation text
#     yield the same chapter_hash, but must NOT share a cache hit -- otherwise a resumed
#     run could return findings tagged with a stale candidate_id (wrong provenance).
def test_resume_does_not_reuse_cache_across_different_candidate_ids_same_text():
    source, snapshot, chunk_plan, chunk1, chunk2, config, candidates, chapter = _env()
    # Same translation text as the default `chunk1_a`, deliberately built as a second,
    # differently-identified winning Candidate for chunk1.
    chunk1_b = _candidate(
        chunk=chunk1, suffix="B", source=source, snapshot=snapshot, chunk_plan=chunk_plan, config=config,
    )
    assert chunk1_b.translation == candidates[chunk1.chunk_id].translation
    assert chunk1_b.candidate_id != candidates[chunk1.chunk_id].candidate_id

    candidates_v2 = dict(candidates)
    candidates_v2[chunk1.chunk_id] = chunk1_b
    chapter_v2 = AssembledChapter.assemble(
        source=source, snapshot=snapshot, chunk_plan=chunk_plan, config=config, candidates=candidates_v2
    )
    # The premise of the bug: identical text -> identical chapter_hash despite a different
    # winning candidate_id for chunk1.
    assert chapter_v2.chapter_hash == chapter.chapter_hash

    cache = AuditCache()
    qwen = ScriptedEvaluator({
        chunk1.chunk_id: [
            _issues_json([{"pid": "p00000", "category": "omission", "note": "first candidate"}]),
            _issues_json([{"pid": "p00000", "category": "omission", "note": "second candidate"}]),
        ],
        chunk2.chunk_id: [_issues_json([]), _issues_json([])],
    })
    gemma = _no_issue_evaluator([chunk1.chunk_id, chunk2.chunk_id], repeats=2)

    first = run_chapter_audit(
        chapter=chapter, source=source, chunk_plan=chunk_plan, candidates=candidates,
        qwen_evaluator=qwen, gemma_evaluator=gemma, cache=cache,
    )
    second = run_chapter_audit(
        chapter=chapter_v2, source=source, chunk_plan=chunk_plan, candidates=candidates_v2,
        qwen_evaluator=qwen, gemma_evaluator=gemma, cache=cache,
    )

    # Not a stale cache hit: the evaluator was actually called again for chunk1.
    assert qwen.call_count(chunk1.chunk_id) == 2

    first_finding = [f for f in first.store if f.detector == "qwen_chapter_audit"][0]
    second_finding = [f for f in second.store if f.detector == "qwen_chapter_audit"][0]
    assert first_finding.candidate_id == candidates[chunk1.chunk_id].candidate_id
    assert second_finding.candidate_id == chunk1_b.candidate_id
    assert second_finding.candidate_id != first_finding.candidate_id


# 6. Malformed/truncated JSON from an evaluator is a failure, not zero findings.
def test_malformed_json_is_a_failure_not_zero_findings():
    source, snapshot, chunk_plan, chunk1, chunk2, config, candidates, chapter = _env()
    qwen = ScriptedEvaluator({
        chunk1.chunk_id: ["{not valid json"],
        chunk2.chunk_id: [_issues_json([])],
    })
    outcome = run_chapter_audit(
        chapter=chapter, source=source, chunk_plan=chunk_plan, candidates=candidates,
        qwen_evaluator=qwen, gemma_evaluator=_no_issue_evaluator([chunk1.chunk_id, chunk2.chunk_id]),
    )
    assert outcome.status == "incomplete"
    assert any(unit[0] == chunk1.chunk_id and unit[1] == "qwen_chapter_audit" for unit in outcome.failed_units)


# 7. An issue naming a PID outside the queried chunk is rejected as a unit failure.
def test_issue_referencing_foreign_pid_is_rejected():
    source, snapshot, chunk_plan, chunk1, chunk2, config, candidates, chapter = _env()
    qwen = ScriptedEvaluator({
        # chunk1's evaluator claims an issue on a chunk2 PID — not owned by chunk1.
        chunk1.chunk_id: [_issues_json([{"pid": "p00008", "category": "omission", "note": "x"}])],
        chunk2.chunk_id: [_issues_json([])],
    })
    outcome = run_chapter_audit(
        chapter=chapter, source=source, chunk_plan=chunk_plan, candidates=candidates,
        qwen_evaluator=qwen, gemma_evaluator=_no_issue_evaluator([chunk1.chunk_id, chunk2.chunk_id]),
    )
    assert outcome.status == "incomplete"
    assert any(unit[0] == chunk1.chunk_id and unit[1] == "qwen_chapter_audit" for unit in outcome.failed_units)


# 8. An unknown category is rejected as a unit failure.
def test_issue_with_unknown_category_is_rejected():
    source, snapshot, chunk_plan, chunk1, chunk2, config, candidates, chapter = _env()
    qwen = ScriptedEvaluator({
        chunk1.chunk_id: [_issues_json([{"pid": "p00000", "category": "not_a_real_category", "note": "x"}])],
        chunk2.chunk_id: [_issues_json([])],
    })
    outcome = run_chapter_audit(
        chapter=chapter, source=source, chunk_plan=chunk_plan, candidates=candidates,
        qwen_evaluator=qwen, gemma_evaluator=_no_issue_evaluator([chunk1.chunk_id, chunk2.chunk_id]),
    )
    assert outcome.status == "incomplete"


# 9. Findings from different detectors on the same region are never merged (delegated to
#    resolve_regions, sanity-checked at the audit integration level).
def test_findings_from_different_detectors_on_same_pid_all_retained():
    source, snapshot, chunk_plan, chunk1, chunk2, config, candidates, chapter = _env()
    qwen = ScriptedEvaluator({
        chunk1.chunk_id: [_issues_json([{"pid": "p00000", "category": "omission", "note": "qwen finding"}])],
        chunk2.chunk_id: [_issues_json([])],
    })
    gemma = ScriptedEvaluator({
        chunk1.chunk_id: [_issues_json([{"pid": "p00000", "category": "register", "note": "gemma finding"}])],
        chunk2.chunk_id: [_issues_json([])],
    })
    outcome = run_chapter_audit(
        chapter=chapter, source=source, chunk_plan=chunk_plan, candidates=candidates,
        qwen_evaluator=qwen, gemma_evaluator=gemma,
    )
    assert outcome.status == "complete"
    pid_findings = outcome.store.by_pid("p00000")
    assert len(pid_findings) == 2
    assert {f.detector for f in pid_findings} == {"qwen_chapter_audit", "gemma_russian_review"}

    region = outcome.region_plan.for_pid("p00000")[0]
    assert set(f.content_hash for f in pid_findings) == set(region.finding_content_hashes)


# 10. A missing/empty translation produces a zero-length Region(pid, 0, 0). This is
#     intentional: it still groups with any other finding on that same empty PID into one
#     coverage region (resolve_regions treats touching spans, start <= end, as adjacent),
#     without merging or dropping either finding's own evidence.
def test_zero_length_region_for_empty_translation_still_groups_by_pid():
    translation_overrides = {"p00000": ""}
    source, snapshot, chunk_plan, chunk1, chunk2, config, candidates, chapter = _env(
        translation_overrides=translation_overrides
    )
    qwen = ScriptedEvaluator({
        chunk1.chunk_id: [_issues_json([{"pid": "p00000", "category": "omission", "note": "qwen finding"}])],
        chunk2.chunk_id: [_issues_json([])],
    })
    outcome = run_chapter_audit(
        chapter=chapter, source=source, chunk_plan=chunk_plan, candidates=candidates,
        qwen_evaluator=qwen, gemma_evaluator=_no_issue_evaluator([chunk1.chunk_id, chunk2.chunk_id]),
    )
    assert outcome.status == "complete"
    pid_findings = outcome.store.by_pid("p00000")
    detectors = {f.detector for f in pid_findings}
    assert detectors == {"deterministic_integrity", "qwen_chapter_audit"}
    for f in pid_findings:
        assert f.region.start == 0
        assert f.region.end == 0

    # Both zero-length findings on the same PID are grouped into one coverage region,
    # with both content hashes referenced — neither finding's evidence is lost.
    regions = outcome.region_plan.for_pid("p00000")
    assert len(regions) == 1
    assert set(f.content_hash for f in pid_findings) == set(regions[0].finding_content_hashes)
