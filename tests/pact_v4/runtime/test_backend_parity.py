"""Pipeline parity tests (plan §14.3).

The same canned model output is fed through (a) a plain fake ModelCaller /
evaluator / selector and (b) the backend role adapters over a
``LocalOpenAIBackend`` (with a fake ``ApiClient``). Everything that must
not depend on the transport must match exactly:

* ``PromptBundle.bundle_hash``;
* rendered prompt bytes;
* Candidate PID-maps;
* Qwen ``GateResult``;
* Gemma selected candidate;
* ``SelectionResult`` / gate trace;
* final translations.
"""
from __future__ import annotations

import json
from typing import Callable, Dict, List, Optional, Tuple

import pytest

from pact_v4.phase1.models import (
    ChunkPlan,
    ChunkPlanArtifact,
    ConfigArtifact,
    Snapshot,
    SourceArtifact,
    canonical_json_hash,
)
from pact_v4.phase2.cascade import select_candidate
from pact_v4.phase2.generation import (
    GenerationParams,
    generate_for_chunk,
)
from pact_v4.phase2.prompts import render_prompt
from pact_v4.phase2.risk import RiskAssessment, RiskBand, RiskFeature
from pact_v4.runtime.api_client import ApiClient, ApiClientConfig
from pact_v4.runtime.backend_role_adapters import (
    BackendGemmaSelector,
    BackendModelCaller,
    BackendModelCallerConfig,
    BackendQwenEvaluator,
)
from pact_v4.runtime.gemma_selector import _parse_gemma_preference
from pact_v4.runtime.local_openai_backend import LocalOpenAIBackend
from pact_v4.runtime.prompts_runtime import (
    render_gemma_preference_prompt,
    render_qwen_review_prompt,
)
from pact_v4.runtime.qwen_evaluator import _parse_qwen_verdict


# ---------------------------------------------------------------------------
# Fixtures (same shape as tests/pact_v4/phase2/test_generation.py)
# ---------------------------------------------------------------------------


def _hash(seed: str) -> str:
    return canonical_json_hash({"seed": seed})


def make_source(chapter_id: str = "ch1", pid_count: int = 10) -> SourceArtifact:
    # Digit-free sentences so the deterministic consistency gate (number
    # preservation) stays satisfied by the canned translations.
    pairs = tuple(
        (f"p{i}", f"Sentence {chr(97 + i)}.") for i in range(pid_count)
    )
    return SourceArtifact(chapter_id=chapter_id, source=pairs)


def make_snapshot(source: SourceArtifact) -> Snapshot:
    return Snapshot(
        chapter_id=source.chapter_id,
        pids=tuple(pid for pid, _ in source.source),
        context="ctx-v1",
        glossary_hash=_hash("glossary"),
        book_memory_hash=_hash("book_memory"),
        chapter_memory_hash=_hash("chapter_memory"),
        source_hash=source.source_hash,
        chapter_index_hash=_hash("chapter_index"),
    )


def make_chunk_plan_artifact(snapshot: Snapshot, chunk_id: str = "c1"):
    chunk = ChunkPlan(
        chunk_id=chunk_id,
        snapshot_hash=snapshot.snapshot_hash,
        pids=snapshot.pids,
        word_counts=tuple(50 for _ in snapshot.pids),
        undersized_exception=False,
    )
    artifact = ChunkPlanArtifact.create(snapshot, (chunk,))
    return artifact, chunk


def make_config() -> ConfigArtifact:
    return ConfigArtifact(version="parity-v1", values={"model": "gemma-mock"})


def make_env():
    source = make_source(pid_count=10)
    snapshot = make_snapshot(source)
    chunk_plan, chunk = make_chunk_plan_artifact(snapshot)
    config = make_config()
    return source, snapshot, chunk_plan, chunk, config


def make_risk(band: RiskBand) -> RiskAssessment:
    return RiskAssessment(
        policy_version="pact-v4-risk-source-en/v1", band=band, score=0, features=()
    )


def make_params(**overrides: object) -> GenerationParams:
    values = {"temperature": 0.2, "seed": 7, "max_tokens": 512}
    values.update(overrides)
    return GenerationParams(**values)


def valid_output_for(chunk: ChunkPlan) -> str:
    # Pure-Cyrillic values (no Latin tokens) so the deterministic
    # consistency gate's mixed-script check stays satisfied.
    return json.dumps(
        {pid: f"Перевод предложения {i}" for i, pid in enumerate(chunk.pids)},
        ensure_ascii=False,
    )


class ConstantGenerator:
    """Fake ``ModelCaller`` returning the same output for every call."""

    def __init__(self, output_fn: Callable):
        self._output_fn = output_fn
        self.calls = []

    def __call__(self, bundle):
        self.calls.append(bundle)
        return self._output_fn(bundle)


class StubApiClient:
    """Fake ``ApiClient`` recording calls and returning scripted text."""

    def __init__(self, script: List[str]) -> None:
        self.script = list(script)
        self.calls: List[Dict] = []

    @property
    def name(self) -> str:
        return "stub-parity"

    @property
    def config(self) -> ApiClientConfig:
        return ApiClientConfig()

    def complete(
        self,
        messages: List[Dict[str, str]],
        *,
        max_tokens: int,
        temperature: Optional[float] = None,
        response_format_json: bool = True,
        label: str = "stub",
    ) -> str:
        self.calls.append({
            "messages": list(messages),
            "max_tokens": max_tokens,
            "temperature": temperature,
            "label": label,
        })
        if not self.script:
            raise AssertionError("StubApiClient: script exhausted")
        return self.script.pop(0)


def _qwen_pass_verdict() -> str:
    return json.dumps({
        "faithful_to_source": True,
        "completeness": True,
        "introduced_errors": False,
        "confidence": "high",
        "reason": "Parity fixture.",
        "passed": True,
    })


# ---------------------------------------------------------------------------
# Generation parity
# ---------------------------------------------------------------------------


def test_generation_parity_fake_vs_local_backend():
    source, snapshot, chunk_plan, chunk, config = make_env()
    risk = make_risk(RiskBand.MEDIUM)
    canned = valid_output_for(chunk)

    # Reference path: a plain fake ModelCaller.
    # lazy_balanced=False: the parity contract compares the FULL legacy A/B
    # candidate pair (both roles' bundle hashes / prompt bytes / translations)
    # across backend wirings; the A2 lazy default would shrink it to one.
    ref_gen = ConstantGenerator(lambda bundle: canned)
    ref_outcome = generate_for_chunk(
        chunk_id=chunk.chunk_id,
        risk=risk,
        source=source,
        snapshot=snapshot,
        chunk_plan=chunk_plan,
        config=config,
        params=make_params(),
        model_caller=ref_gen,
        lazy_balanced=False,
    )
    ref_bundle = ref_gen.calls[0]
    ref_prompt_bytes = render_prompt(ref_bundle).encode("utf-8")

    # Backend path: BackendModelCaller over LocalOpenAIBackend (fake ApiClient).
    stub = StubApiClient([canned, canned])
    backend = LocalOpenAIBackend(api=stub)  # type: ignore[arg-type]
    back_caller = BackendModelCaller(
        backend,
        config=BackendModelCallerConfig(max_tokens=512),
    )
    back_outcome = generate_for_chunk(
        chunk_id=chunk.chunk_id,
        risk=risk,
        source=source,
        snapshot=snapshot,
        chunk_plan=chunk_plan,
        config=config,
        params=make_params(),
        model_caller=back_caller,
        lazy_balanced=False,
    )

    assert set(back_outcome.candidates) == set(ref_outcome.candidates)

    # PromptBundle.bundle_hash parity (recorded in the candidate trace).
    ref_hash = ref_bundle.bundle_hash
    back_hash = back_outcome.candidates["fidelity_first"].decision_trace[0].detail
    assert back_hash == ref_hash

    # Rendered prompt bytes parity.
    sent_content = stub.calls[0]["messages"][0]["content"]
    assert sent_content.encode("utf-8") == ref_prompt_bytes

    # Candidate PID-maps parity.
    for role in ("fidelity_first", "balanced_literary"):
        assert (
            back_outcome.candidates[role].translation
            == ref_outcome.candidates[role].translation
        )


# ---------------------------------------------------------------------------
# Gate parity
# ---------------------------------------------------------------------------


def test_qwen_gate_parity():
    source = {"p1": "Hello.", "p2": "World."}
    translation = {"p1": "Привет.", "p2": "Мир."}
    canned = _qwen_pass_verdict()

    ref = _parse_qwen_verdict(canned)

    stub = StubApiClient([canned])
    evaluator = BackendQwenEvaluator(LocalOpenAIBackend(api=stub))  # type: ignore[arg-type]
    got = evaluator(source, translation)

    assert got == ref
    sent = stub.calls[0]["messages"][0]["content"]
    assert sent == render_qwen_review_prompt(source=source, translation=translation)


def test_gemma_gate_parity():
    candidates = [
        ("A", {"p1": "Стюард открыл дверь."}),
        ("B", {"p1": "Управляющий распахнул дверь."}),
    ]
    canned = json.dumps({"preferred_candidate_id": "B", "reason": "parity"})

    ref = _parse_gemma_preference(canned, valid_candidate_ids=["A", "B"])

    stub = StubApiClient([canned])
    selector = BackendGemmaSelector(LocalOpenAIBackend(api=stub))  # type: ignore[arg-type]
    got = selector(candidates)

    assert got == ref
    sent = stub.calls[0]["messages"][0]["content"]
    assert sent == render_gemma_preference_prompt(candidates=candidates)


# ---------------------------------------------------------------------------
# Selection parity (full pipeline slice)
# ---------------------------------------------------------------------------


def test_selection_parity_fake_vs_local_backend():
    source, snapshot, chunk_plan, chunk, config = make_env()
    risk = make_risk(RiskBand.MEDIUM)
    canned_out = valid_output_for(chunk)
    canned_qwen = _qwen_pass_verdict()

    # --- generate candidates through both paths -----------------------------
    # lazy_balanced=False: parity compares the full legacy A/B pair and the
    # Gemma preference over two passing candidates (see below).
    ref_outcome = generate_for_chunk(
        chunk_id=chunk.chunk_id,
        risk=risk,
        source=source,
        snapshot=snapshot,
        chunk_plan=chunk_plan,
        config=config,
        params=make_params(),
        model_caller=ConstantGenerator(lambda bundle: canned_out),
        lazy_balanced=False,
    )
    back_outcome = generate_for_chunk(
        chunk_id=chunk.chunk_id,
        risk=risk,
        source=source,
        snapshot=snapshot,
        chunk_plan=chunk_plan,
        config=config,
        params=make_params(),
        model_caller=BackendModelCaller(
            LocalOpenAIBackend(api=StubApiClient([canned_out, canned_out])),  # type: ignore[arg-type]
            config=BackendModelCallerConfig(max_tokens=512),
        ),
        lazy_balanced=False,
    )

    # Candidate ids are content-derived, so both paths share them.
    preferred = ref_outcome.candidates["fidelity_first"].candidate_id
    canned_gemma = json.dumps({"preferred_candidate_id": preferred, "reason": "parity"})

    # --- reference gates ----------------------------------------------------
    def ref_qwen(s, t):
        return _parse_qwen_verdict(canned_qwen)

    def ref_gemma(cands):
        return _parse_gemma_preference(
            canned_gemma, valid_candidate_ids=[cid for cid, _ in cands]
        )

    ref_selection = select_candidate(
        chunk_id=chunk.chunk_id,
        candidates=list(ref_outcome.candidates.values()),
        source=source,
        qwen_evaluator=ref_qwen,
        gemma_selector=ref_gemma,
        risk=risk,
    )

    # --- backend gates ------------------------------------------------------
    back_qwen = BackendQwenEvaluator(
        LocalOpenAIBackend(api=StubApiClient([canned_qwen, canned_qwen]))  # type: ignore[arg-type]
    )
    back_gemma = BackendGemmaSelector(
        LocalOpenAIBackend(api=StubApiClient([canned_gemma]))  # type: ignore[arg-type]
    )
    back_selection = select_candidate(
        chunk_id=chunk.chunk_id,
        candidates=list(back_outcome.candidates.values()),
        source=source,
        qwen_evaluator=back_qwen,
        gemma_selector=back_gemma,
        risk=risk,
    )

    # SelectionResult / gate trace parity.
    assert back_selection == ref_selection
    assert back_selection.selected_role == "fidelity_first"

    # Final translations parity.
    ref_final = ref_outcome.candidates[ref_selection.selected_role].translation
    back_final = back_outcome.candidates[back_selection.selected_role].translation
    assert back_final == ref_final


# ---------------------------------------------------------------------------
# Phase 4A repair-caller parity (B2): transport-neutral repair prompt/output
# ---------------------------------------------------------------------------


def test_repair_caller_parity_rendered_prompt_is_backend_agnostic():
    """The same repair request renders the same prompt regardless of backend.

    The repair caller is transport-only: prompt rendering lives in
    ``prompts_runtime``, parsing in ``pact_v4.phase4.repair``. This pins the
    rendered-prompt identity so a transport change cannot silently alter what
    the model sees.
    """
    from pact_v4.phase1.models import Region
    from pact_v4.runtime.backend_role_adapters import (
        BackendRepairCaller,
        BackendRepairCallerConfig,
    )
    from pact_v4.runtime.prompts_runtime import render_repair_prompt

    region = Region(pid="p1", start=0, end=6)
    findings = [{"category": "omission", "note": "dropped clause", "excerpt": ""}]
    source = {"p1": "Hello.", "p2": "World."}
    translation = {"p1": "Привет.", "p2": "Мир."}

    # Reference: the pure render function.
    ref_prompt = render_repair_prompt(
        chunk_id="c1", source=source, translation=translation,
        region=region, findings=findings,
    )

    # Backend path: the caller must send exactly the rendered prompt.
    stub = StubApiClient([
        json.dumps({"repaired": {"p1": "Здравствуйте."}, "reason": "parity"}, ensure_ascii=False)
    ])
    caller = BackendRepairCaller(
        LocalOpenAIBackend(api=stub),  # type: ignore[arg-type]
        config=BackendRepairCallerConfig(max_tokens=512),
    )
    raw = caller(
        chunk_id="c1", source=source, translation=translation,
        region=region, findings=findings,
    )
    assert "Здравствуйте" in raw
    sent = stub.calls[0]["messages"][0]["content"]
    assert sent == ref_prompt
    # The repair role resolves the generator binding.
    assert stub.calls[0]["label"].startswith("phase4/")


# ---------------------------------------------------------------------------
# L2b narrow region re-gate parity: rendered prompt is backend-agnostic
# ---------------------------------------------------------------------------


def test_region_fidelity_gate_parity_rendered_prompt_is_backend_agnostic():
    """The narrow L2b re-gate renders exactly ``render_region_fidelity_gate_prompt``
    and parses the verdict with the same ``_parse_qwen_verdict`` the full-chunk
    fidelity gate uses, so narrow verdicts are directly comparable to full ones.
    """
    from pact_v4.phase1.models import GateResult, Region
    from pact_v4.runtime.backend_role_adapters import (
        BackendRegionFidelityGate,
        BackendRegionFidelityGateConfig,
    )
    from pact_v4.runtime.prompts_runtime import render_region_fidelity_gate_prompt

    region = Region(pid="p1", start=0, end=6)
    source_text = "Hello, world."
    repaired_text = "Здравствуй, мир."

    ref_prompt = render_region_fidelity_gate_prompt(
        source_text=source_text, repaired_text=repaired_text, region=region,
    )

    stub = StubApiClient([
        json.dumps({
            "faithful_to_source": True,
            "completeness": True,
            "introduced_errors": False,
            "confidence": "high",
            "reason": "parity",
            "passed": True,
        }, ensure_ascii=False)
    ])
    gate = BackendRegionFidelityGate(
        LocalOpenAIBackend(api=stub),  # type: ignore[arg-type]
        config=BackendRegionFidelityGateConfig(max_tokens=512),
    )
    result = gate(
        source_text=source_text, repaired_text=repaired_text, region=region,
    )
    assert isinstance(result, GateResult)
    assert result.passed is True
    sent = stub.calls[0]["messages"][0]["content"]
    assert sent == ref_prompt
    # The narrow re-gate stays on the Qwen fidelity role (label carries the
    # phase4/region_fidelity_gate role).
    assert stub.calls[0]["label"].startswith("phase4/region_fidelity_gate")
