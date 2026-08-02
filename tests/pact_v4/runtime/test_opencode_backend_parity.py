"""Pipeline parity tests for the OpenCode backend (plan §14.3).

The same canned model output is fed through (a) a plain fake ``ModelCaller``
and (b) the backend role adapters over an ``OpenCodeServerBackend`` backed by
the offline ``FakeOpenCodeServer``. Everything that must not depend on the
transport must match exactly:

* ``PromptBundle.bundle_hash``;
* rendered prompt bytes;
* Candidate PID-maps;
* Qwen ``GateResult``;
* Gemma selected candidate;
* ``SelectionResult`` / gate trace;
* final translations.

This mirrors ``test_backend_parity.py`` but routes the canned output through
the OpenCode REST/OpenAPI contract instead of a fake ``ApiClient``.
"""
from __future__ import annotations

import json

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
from pact_v4.phase2.risk import RiskAssessment, RiskBand
from pact_v4.runtime.backend_protocol import (
    CompletionRequest,
)
from pact_v4.runtime.backend_role_adapters import (
    BackendGemmaSelector,
    BackendModelCaller,
    BackendModelCallerConfig,
    BackendQwenEvaluator,
)
from pact_v4.runtime.opencode_backend import (
    OpenCodeServerBackend,
    OpenCodeServerBackendConfig,
)
from tests.pact_v4.runtime.opencode_fake_server import FakeOpenCodeServer


def _hash(seed: str) -> str:
    return canonical_json_hash({"seed": seed})


def make_source(chapter_id: str = "ch1", pid_count: int = 10) -> SourceArtifact:
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
    return ConfigArtifact(version="parity-v1", values={"model": "opencode-mock"})


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
    return json.dumps(
        {pid: f"Перевод предложения {i}" for i, pid in enumerate(chunk.pids)},
        ensure_ascii=False,
    )


class ConstantGenerator:
    def __init__(self, output_fn):
        self._output_fn = output_fn
        self.calls = []

    def __call__(self, bundle):
        self.calls.append(bundle)
        return self._output_fn(bundle)


class OpenCodeStub:
    """Canned ``CompletionBackend`` responses via the fake OpenCode server.

    Each ``complete()`` pops one canned text/verdict and returns it as the
    assistant message text part — the transport-agnostic view. One persistent
    ``OpenCodeServerBackend`` backs the stub so ``descriptor`` /
    ``call_records`` behave like the real transport.
    """

    def __init__(self, script) -> None:
        self.script = list(script)
        self.fake = FakeOpenCodeServer()
        for item in self.script:
            self.fake.script_message(_text_message(item))
        self._backend = OpenCodeServerBackend(
            OpenCodeServerBackendConfig(
                base_url="http://127.0.0.1:4096",
                model_bindings={"default": "opencode-go/deepseek-v4-flash"},
                structured_output_mode="prompt_only",
            ),
            session=self.fake,
        )

    @property
    def descriptor(self):
        return self._backend.descriptor

    def complete(self, request: CompletionRequest):
        return self._backend.complete(request)

    def call_records(self):
        return self._backend.call_records()


def _text_message(text: str) -> dict:
    return {
        "info": {
            "id": "msg_parity",
            "role": "assistant",
            "providerID": "opencode-go",
            "modelID": "deepseek-v4-flash",
            "finish": "end_turn",
            "cost": 0.01,
            "tokens": {"input": 10, "output": 20, "reasoning": 0, "cache": {"read": 0, "write": 0}},
        },
        "parts": [{"id": "p1", "type": "text", "text": text}],
    }


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


def test_generation_parity_fake_vs_opencode_backend():
    source, snapshot, chunk_plan, chunk, config = make_env()
    risk = make_risk(RiskBand.MEDIUM)
    canned = valid_output_for(chunk)

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
    )
    ref_bundle = ref_gen.calls[0]

    stub = OpenCodeStub([canned, canned])
    back_caller = BackendModelCaller(
        stub,
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
    )

    assert set(back_outcome.candidates) == set(ref_outcome.candidates)

    # PromptBundle.bundle_hash parity.
    ref_hash = ref_bundle.bundle_hash
    back_hash = back_outcome.candidates["fidelity_first"].decision_trace[0].detail
    assert back_hash == ref_hash

    # Rendered prompt bytes parity: the first message sent to the OpenCode
    # fake is exactly the reference prompt (roles render in a fixed order).
    first_sent = [
        b["parts"][0]["text"]
        for m, p, b in stub.fake.requests_log
        if m == "POST" and "/message" in p
    ][0]
    from pact_v4.phase2.prompts import render_prompt

    assert first_sent.encode("utf-8") == render_prompt(ref_bundle).encode("utf-8")

    # Candidate PID-maps parity.
    for role in ("fidelity_first", "balanced_literary"):
        assert (
            back_outcome.candidates[role].translation
            == ref_outcome.candidates[role].translation
        )


# ---------------------------------------------------------------------------
# Gate parity
# ---------------------------------------------------------------------------


def test_qwen_gate_parity_via_opencode():
    source = {"p1": "Hello.", "p2": "World."}
    translation = {"p1": "Привет.", "p2": "Мир."}
    canned = _qwen_pass_verdict()

    from pact_v4.runtime.qwen_evaluator import _parse_qwen_verdict

    ref = _parse_qwen_verdict(canned)

    stub = OpenCodeStub([canned])
    evaluator = BackendQwenEvaluator(stub)
    got = evaluator(source, translation)

    assert got == ref


def test_gemma_gate_parity_via_opencode():
    candidates = [
        ("A", {"p1": "Стюард открыл дверь."}),
        ("B", {"p1": "Управляющий распахнул дверь."}),
    ]
    canned = json.dumps({"preferred_candidate_id": "B", "reason": "parity"})

    from pact_v4.runtime.gemma_selector import _parse_gemma_preference

    ref = _parse_gemma_preference(canned, valid_candidate_ids=["A", "B"])

    stub = OpenCodeStub([canned])
    selector = BackendGemmaSelector(stub)
    got = selector(candidates)

    assert got == ref


# ---------------------------------------------------------------------------
# Selection parity (full pipeline slice)
# ---------------------------------------------------------------------------


def test_selection_parity_fake_vs_opencode_backend():
    source, snapshot, chunk_plan, chunk, config = make_env()
    risk = make_risk(RiskBand.MEDIUM)
    canned_out = valid_output_for(chunk)
    canned_qwen = _qwen_pass_verdict()

    ref_outcome = generate_for_chunk(
        chunk_id=chunk.chunk_id,
        risk=risk,
        source=source,
        snapshot=snapshot,
        chunk_plan=chunk_plan,
        config=config,
        params=make_params(),
        model_caller=ConstantGenerator(lambda bundle: canned_out),
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
            OpenCodeStub([canned_out, canned_out]),
            config=BackendModelCallerConfig(max_tokens=512),
        ),
    )

    preferred = ref_outcome.candidates["fidelity_first"].candidate_id
    canned_gemma = json.dumps({"preferred_candidate_id": preferred, "reason": "parity"})

    from pact_v4.runtime.gemma_selector import _parse_gemma_preference
    from pact_v4.runtime.qwen_evaluator import _parse_qwen_verdict

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

    back_qwen = BackendQwenEvaluator(OpenCodeStub([canned_qwen, canned_qwen]))
    back_gemma = BackendGemmaSelector(OpenCodeStub([canned_gemma]))
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
# Prompt / identity parity (backend must not change the prompt)
# ---------------------------------------------------------------------------


def test_opencode_backend_sends_exact_rendered_prompt():
    from pact_v4.phase2.prompts import render_prompt
    from pact_v4.runtime.prompts_runtime import render_qwen_review_prompt

    source = {"p1": "Hello.", "p2": "World."}
    translation = {"p1": "Привет.", "p2": "Мир."}
    prompt = render_qwen_review_prompt(source=source, translation=translation)

    stub = OpenCodeStub([_qwen_pass_verdict()])
    BackendQwenEvaluator(stub)(source, translation)

    sent = stub.fake.last_message_body()["parts"][0]["text"]
    assert sent == prompt
