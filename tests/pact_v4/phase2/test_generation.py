"""Tests for Phase 2B risk-gated A/B generation (pact_v4.phase2.generation).

All generator calls go through a mock ``ModelCaller`` — no real llama-server,
no production pipeline, no network access.
"""
from __future__ import annotations

import json
from dataclasses import replace
from typing import Callable, Dict, Tuple

import pytest

from pact_v4.phase1.models import (
    ChunkPlan,
    ChunkPlanArtifact,
    ConfigArtifact,
    Snapshot,
    SourceArtifact,
    canonical_json_hash,
)
from pact_v4.phase2.generation import (
    GenerationCache,
    GenerationErrorCode,
    GenerationParams,
    generate_for_chunk,
)
from pact_v4.phase2.risk import (
    REQUIRED_RISK_CATEGORIES,
    GlossaryEntry,
    RiskAssessment,
    RiskBand,
    RiskFeature,
)


def _hash(seed: str) -> str:
    return canonical_json_hash({"seed": seed})


def make_source(chapter_id: str = "ch1", pid_count: int = 10) -> SourceArtifact:
    pairs = tuple((f"p{i}", f"English sentence {i}.") for i in range(pid_count))
    return SourceArtifact(chapter_id=chapter_id, source=pairs)


def make_snapshot(source: SourceArtifact, context: str = "ctx-v1") -> Snapshot:
    pids = tuple(pid for pid, _ in source.source)
    return Snapshot(
        chapter_id=source.chapter_id,
        pids=pids,
        context=context,
        glossary_hash=_hash("glossary"),
        book_memory_hash=_hash("book_memory"),
        chapter_memory_hash=_hash("chapter_memory"),
        source_hash=source.source_hash,
        chapter_index_hash=_hash("chapter_index"),
    )


def make_chunk_plan_artifact(
    snapshot: Snapshot, chunk_id: str = "c1"
) -> Tuple[ChunkPlanArtifact, ChunkPlan]:
    # 50 words/PID keeps this fixture (used with pid_count up to 10) inside
    # ChunkPlan's fixed word-based hard cap (MAX_WORDS=640).
    word_counts = tuple(50 for _ in snapshot.pids)
    chunk = ChunkPlan(
        chunk_id=chunk_id,
        snapshot_hash=snapshot.snapshot_hash,
        pids=snapshot.pids,
        word_counts=word_counts,
        undersized_exception=sum(word_counts) < ChunkPlan.MIN_WORDS,
    )
    artifact = ChunkPlanArtifact.create(snapshot, (chunk,))
    return artifact, chunk


def make_config(version: str = "v1", **values: object) -> ConfigArtifact:
    return ConfigArtifact(version=version, values=values or {"model": "gemma-mock"})


def make_env(pid_count: int = 10, chunk_id: str = "c1"):
    source = make_source(pid_count=pid_count)
    snapshot = make_snapshot(source)
    chunk_plan, chunk = make_chunk_plan_artifact(snapshot, chunk_id=chunk_id)
    config = make_config()
    return source, snapshot, chunk_plan, chunk, config


def make_risk(band: RiskBand, features: Tuple[RiskFeature, ...] = ()) -> RiskAssessment:
    return RiskAssessment(
        policy_version="pact-v4-risk-source-en/v1", band=band, score=0, features=features
    )


def make_feature(code: str, weight: int = 1) -> RiskFeature:
    return RiskFeature(code=code, weight=weight, explanation=f"test:{code}", evidence=())


def make_params(**overrides: object) -> GenerationParams:
    values = {"temperature": 0.2, "seed": 7, "max_tokens": 512}
    values.update(overrides)
    return GenerationParams(**values)


def valid_output_for(chunk: ChunkPlan) -> str:
    return json.dumps({pid: f"Перевод {pid}" for pid in chunk.pids}, ensure_ascii=False)


class ScriptedGenerator:
    """Mock ModelCaller: returns a scripted string (or raises) per call, in order."""

    def __init__(self, outputs: list):
        self._outputs = list(outputs)
        self.calls = []

    def __call__(self, bundle) -> str:
        self.calls.append(bundle)
        if not self._outputs:
            raise AssertionError("ScriptedGenerator called more times than scripted")
        result = self._outputs.pop(0)
        if isinstance(result, Exception):
            raise result
        return result

    @property
    def call_count(self) -> int:
        return len(self.calls)


class ConstantGenerator:
    """Mock ModelCaller returning the same output for every call."""

    def __init__(self, output_fn: Callable):
        self._output_fn = output_fn
        self.calls = []

    def __call__(self, bundle) -> str:
        self.calls.append(bundle)
        return self._output_fn(bundle)

    @property
    def call_count(self) -> int:
        return len(self.calls)


# ---------------------------------------------------------------------------
# Risk gating
# ---------------------------------------------------------------------------


def test_low_risk_calls_generator_exactly_once():
    # V4 Efficiency A2: the lazy balanced-only default generates exactly one
    # primary candidate (balanced_literary) for every band, low risk included.
    source, snapshot, chunk_plan, chunk, config = make_env()
    generator = ConstantGenerator(lambda bundle: valid_output_for(chunk))

    outcome = generate_for_chunk(
        chunk_id=chunk.chunk_id,
        risk=make_risk(RiskBand.LOW),
        source=source,
        snapshot=snapshot,
        chunk_plan=chunk_plan,
        config=config,
        params=make_params(),
        model_caller=generator,
    )

    assert generator.call_count == 1
    assert outcome.status == "complete"
    assert set(outcome.candidates) == {"balanced_literary"}


@pytest.mark.parametrize("band", [RiskBand.MEDIUM, RiskBand.HIGH])
def test_medium_and_high_risk_produce_exactly_a_and_b(band):
    # Legacy 2-candidate scheme, explicitly opted out of the A2 lazy default
    # (lazy_balanced=False → fidelity_first A + balanced_literary B).
    source, snapshot, chunk_plan, chunk, config = make_env()
    generator = ConstantGenerator(lambda bundle: valid_output_for(chunk))

    outcome = generate_for_chunk(
        chunk_id=chunk.chunk_id,
        risk=make_risk(band),
        source=source,
        snapshot=snapshot,
        chunk_plan=chunk_plan,
        config=config,
        params=make_params(),
        model_caller=generator,
        lazy_balanced=False,
    )

    assert generator.call_count == 2
    assert set(outcome.candidates) == {"fidelity_first", "balanced_literary"}
    assert outcome.status == "complete"


@pytest.mark.parametrize("band", [RiskBand.LOW, RiskBand.MEDIUM, RiskBand.HIGH])
def test_lazy_balanced_generates_single_balanced_candidate_for_every_band(band):
    """A2 acceptance `low→1 balanced` (and the same single-candidate default
    for every band): lazy mode never generates more than one candidate up
    front — fidelity_first is deferred to the driver's lazy fallback."""
    source, snapshot, chunk_plan, chunk, config = make_env()
    generator = ConstantGenerator(lambda bundle: valid_output_for(chunk))

    outcome = generate_for_chunk(
        chunk_id=chunk.chunk_id,
        risk=make_risk(band),
        source=source,
        snapshot=snapshot,
        chunk_plan=chunk_plan,
        config=config,
        params=make_params(),
        model_caller=generator,
    )

    assert generator.call_count == 1
    assert outcome.status == "complete"
    assert set(outcome.candidates) == {"balanced_literary"}
    assert outcome.expected_roles == ("balanced_literary",)


def test_roles_override_generates_exactly_those_roles():
    """The strict driver's A2 lazy fallback re-generates a single
    fidelity_first candidate via the explicit ``roles`` override, bypassing
    risk-based routing; unknown/empty role lists are rejected."""
    source, snapshot, chunk_plan, chunk, config = make_env()
    generator = ConstantGenerator(lambda bundle: valid_output_for(chunk))

    outcome = generate_for_chunk(
        chunk_id=chunk.chunk_id,
        risk=make_risk(RiskBand.HIGH),
        source=source,
        snapshot=snapshot,
        chunk_plan=chunk_plan,
        config=config,
        params=make_params(),
        model_caller=generator,
        roles=("fidelity_first",),
    )
    assert generator.call_count == 1
    assert outcome.status == "complete"
    assert set(outcome.candidates) == {"fidelity_first"}
    assert outcome.expected_roles == ("fidelity_first",)
    assert generator.calls[0].role == "fidelity_first"

    with pytest.raises(ValueError, match="unknown role"):
        generate_for_chunk(
            chunk_id=chunk.chunk_id,
            risk=make_risk(RiskBand.HIGH),
            source=source,
            snapshot=snapshot,
            chunk_plan=chunk_plan,
            config=config,
            params=make_params(),
            model_caller=generator,
            roles=("synthesis",),
        )
    with pytest.raises(ValueError, match="non-empty"):
        generate_for_chunk(
            chunk_id=chunk.chunk_id,
            risk=make_risk(RiskBand.HIGH),
            source=source,
            snapshot=snapshot,
            chunk_plan=chunk_plan,
            config=config,
            params=make_params(),
            model_caller=generator,
            roles=(),
        )


def test_no_role_other_than_fidelity_first_or_balanced_literary_is_ever_produced():
    source, snapshot, chunk_plan, chunk, config = make_env()
    generator = ConstantGenerator(lambda bundle: valid_output_for(chunk))
    for band in (RiskBand.LOW, RiskBand.MEDIUM, RiskBand.HIGH):
        outcome = generate_for_chunk(
            chunk_id=chunk.chunk_id,
            risk=make_risk(band),
            source=source,
            snapshot=snapshot,
            chunk_plan=chunk_plan,
            config=config,
            params=make_params(),
            model_caller=generator,
        )
        assert set(outcome.candidates) <= {"fidelity_first", "balanced_literary"}
        assert "synthesis" not in outcome.candidates
        assert len(outcome.candidates) <= 2


def test_no_selection_or_winner_function_is_exported():
    import pact_v4.phase2.generation as generation_module

    forbidden_terms = ("select", "winner", "synthesis", "synthesize")
    for name in generation_module.__all__:
        lowered = name.lower()
        assert not any(term in lowered for term in forbidden_terms), name


# ---------------------------------------------------------------------------
# Prompt versioning
# ---------------------------------------------------------------------------


def test_a_and_b_use_different_versioned_templates():
    source, snapshot, chunk_plan, chunk, config = make_env()
    generator = ConstantGenerator(lambda bundle: valid_output_for(chunk))

    outcome = generate_for_chunk(
        chunk_id=chunk.chunk_id,
        risk=make_risk(RiskBand.HIGH),
        source=source,
        snapshot=snapshot,
        chunk_plan=chunk_plan,
        config=config,
        params=make_params(),
        model_caller=generator,
        lazy_balanced=False,
    )
    assert outcome.status == "complete"

    bundles = {call.role: call for call in generator.calls}
    assert bundles["fidelity_first"].template.role == "fidelity_first"
    assert bundles["balanced_literary"].template.role == "balanced_literary"
    assert bundles["fidelity_first"].template.version != bundles["balanced_literary"].template.version
    assert bundles["fidelity_first"].template.instructions != bundles["balanced_literary"].template.instructions
    assert bundles["fidelity_first"].bundle_hash != bundles["balanced_literary"].bundle_hash


# ---------------------------------------------------------------------------
# Cache reuse / invalidation
# ---------------------------------------------------------------------------


def test_identical_call_reuses_cache():
    source, snapshot, chunk_plan, chunk, config = make_env()
    generator = ConstantGenerator(lambda bundle: valid_output_for(chunk))
    cache = GenerationCache()
    kwargs = dict(
        chunk_id=chunk.chunk_id,
        risk=make_risk(RiskBand.LOW),
        source=source,
        snapshot=snapshot,
        chunk_plan=chunk_plan,
        config=config,
        params=make_params(),
        model_caller=generator,
        cache=cache,
    )

    first = generate_for_chunk(**kwargs)
    second = generate_for_chunk(**kwargs)

    assert generator.call_count == 1
    assert first.candidates["balanced_literary"].candidate_id == second.candidates["balanced_literary"].candidate_id


def _base_kwargs(source, snapshot, chunk_plan, chunk, config, generator, cache):
    return dict(
        chunk_id=chunk.chunk_id,
        risk=make_risk(RiskBand.LOW),
        source=source,
        snapshot=snapshot,
        chunk_plan=chunk_plan,
        config=config,
        params=make_params(),
        model_caller=generator,
        cache=cache,
    )


def test_changing_prompt_version_or_content_invalidates_cache():
    """Bundle-level check: version and instructions are both part of the
    hashed identity, independent of which role they belong to (the
    end-to-end route through ``generate_for_chunk`` always uses the fixed,
    frozen ``_TEMPLATES`` mapping, so this is exercised at the
    ``PromptBundle`` level where template content is a free parameter)."""
    from pact_v4.phase2.generation import PromptBundle
    from pact_v4.phase2.prompts import PromptTemplate

    common = dict(
        role="fidelity_first",
        risk_band="low",
        risk_policy_version="pact-v4-risk-source-en/v1",
        required_risk_feature_codes=(),
        snapshot_hash=_hash("snap"),
        source_hash=_hash("source"),
        chunk_id="c1",
        owned_pids=("p0", "p1"),
        owned_source=(("p0", "Hello."), ("p1", "World.")),
        left_context=(),
        right_context=(),
        glossary=(),
        style_constraints=(),
        bible_text="",
        config_identity=_hash("config"),
        params=make_params(),
    )

    template_v1 = PromptTemplate(role="fidelity_first", version="v1", instructions="Do X.")
    template_v2 = PromptTemplate(role="fidelity_first", version="v2", instructions="Do X.")
    template_v1_reworded = PromptTemplate(role="fidelity_first", version="v1", instructions="Do Y.")

    bundle_v1 = PromptBundle(template=template_v1, **common)
    bundle_v2 = PromptBundle(template=template_v2, **common)
    bundle_reworded = PromptBundle(template=template_v1_reworded, **common)

    assert bundle_v1.bundle_hash != bundle_v2.bundle_hash
    assert bundle_v1.bundle_hash != bundle_reworded.bundle_hash


def test_changing_prompt_version_invalidates_cache_end_to_end():
    """End-to-end: swapping which versioned template a role resolves to
    (simulated by generating the same chunk under two independently
    constructed environments whose only difference is impossible to reach
    via the public API without a template bump) is covered at the bundle
    level above; here we confirm the two roles never collide with each
    other's cache entry even though the risk band groups them together."""
    source, snapshot, chunk_plan, chunk, config = make_env()
    generator = ConstantGenerator(lambda bundle: valid_output_for(chunk))
    cache = GenerationCache()

    generate_for_chunk(
        chunk_id=chunk.chunk_id, risk=make_risk(RiskBand.HIGH), source=source,
        snapshot=snapshot, chunk_plan=chunk_plan, config=config, params=make_params(),
        model_caller=generator, cache=cache, lazy_balanced=False,
    )
    assert generator.call_count == 2
    assert len({call.bundle_hash for call in generator.calls}) == 2


def test_changing_role_invalidates_cache():
    source, snapshot, chunk_plan, chunk, config = make_env()
    generator = ConstantGenerator(lambda bundle: valid_output_for(chunk))
    cache = GenerationCache()

    generate_for_chunk(**_base_kwargs(source, snapshot, chunk_plan, chunk, config, generator, cache))
    generate_for_chunk(
        chunk_id=chunk.chunk_id,
        risk=make_risk(RiskBand.HIGH),
        source=source,
        snapshot=snapshot,
        chunk_plan=chunk_plan,
        config=config,
        params=make_params(),
        model_caller=generator,
        cache=cache,
        lazy_balanced=False,
    )

    # low (1 call, risk_band="low", lazy default -> balanced_literary) + high
    # (2 calls, legacy A/B: risk_band is itself part of the bundle identity,
    # so both high candidates are fresh cache misses) = 3.
    assert generator.call_count == 3


def test_changing_context_invalidates_cache():
    source, snapshot, chunk_plan, chunk, config = make_env()
    generator = ConstantGenerator(lambda bundle: valid_output_for(chunk))
    cache = GenerationCache()
    kwargs = _base_kwargs(source, snapshot, chunk_plan, chunk, config, generator, cache)

    generate_for_chunk(**kwargs)
    generate_for_chunk(**{**kwargs, "left_context": (("p_prev", "Уже переведено."),)})

    assert generator.call_count == 2


def test_changing_snapshot_invalidates_cache():
    source, _, chunk_plan, chunk, config = make_env()
    generator = ConstantGenerator(lambda bundle: valid_output_for(chunk))
    cache = GenerationCache()

    snapshot_a = make_snapshot(source, context="ctx-a")
    plan_a, chunk_a = make_chunk_plan_artifact(snapshot_a)
    generate_for_chunk(
        chunk_id=chunk_a.chunk_id, risk=make_risk(RiskBand.LOW), source=source,
        snapshot=snapshot_a, chunk_plan=plan_a, config=config, params=make_params(),
        model_caller=generator, cache=cache,
    )

    snapshot_b = make_snapshot(source, context="ctx-b")
    plan_b, chunk_b = make_chunk_plan_artifact(snapshot_b)
    generate_for_chunk(
        chunk_id=chunk_b.chunk_id, risk=make_risk(RiskBand.LOW), source=source,
        snapshot=snapshot_b, chunk_plan=plan_b, config=config, params=make_params(),
        model_caller=generator, cache=cache,
    )

    assert generator.call_count == 2


def test_changing_model_config_identity_invalidates_cache():
    source, snapshot, chunk_plan, chunk, config_a = make_env()
    generator = ConstantGenerator(lambda bundle: valid_output_for(chunk))
    cache = GenerationCache()

    generate_for_chunk(
        chunk_id=chunk.chunk_id, risk=make_risk(RiskBand.LOW), source=source,
        snapshot=snapshot, chunk_plan=chunk_plan, config=config_a, params=make_params(),
        model_caller=generator, cache=cache,
    )

    config_b = make_config(model="gemma-mock-v2")
    generate_for_chunk(
        chunk_id=chunk.chunk_id, risk=make_risk(RiskBand.LOW), source=source,
        snapshot=snapshot, chunk_plan=chunk_plan, config=config_b, params=make_params(),
        model_caller=generator, cache=cache,
    )

    assert generator.call_count == 2


def test_changing_pids_invalidates_cache():
    generator_holder = {}

    def gen(bundle):
        return json.dumps({pid: f"Перевод {pid}" for pid in bundle.owned_pids}, ensure_ascii=False)

    generator = ConstantGenerator(gen)
    cache = GenerationCache()

    source_a, snapshot_a, chunk_plan_a, chunk_a, config = make_env(pid_count=8, chunk_id="cA")
    generate_for_chunk(
        chunk_id=chunk_a.chunk_id, risk=make_risk(RiskBand.LOW), source=source_a,
        snapshot=snapshot_a, chunk_plan=chunk_plan_a, config=config, params=make_params(),
        model_caller=generator, cache=cache,
    )

    source_b, snapshot_b, chunk_plan_b, chunk_b, _ = make_env(pid_count=9, chunk_id="cB")
    generate_for_chunk(
        chunk_id=chunk_b.chunk_id, risk=make_risk(RiskBand.LOW), source=source_b,
        snapshot=snapshot_b, chunk_plan=chunk_plan_b, config=config, params=make_params(),
        model_caller=generator, cache=cache,
    )

    assert generator.call_count == 2


def test_changing_generation_params_invalidates_cache():
    source, snapshot, chunk_plan, chunk, config = make_env()
    generator = ConstantGenerator(lambda bundle: valid_output_for(chunk))
    cache = GenerationCache()

    generate_for_chunk(
        chunk_id=chunk.chunk_id, risk=make_risk(RiskBand.LOW), source=source,
        snapshot=snapshot, chunk_plan=chunk_plan, config=config, params=make_params(seed=1),
        model_caller=generator, cache=cache,
    )
    generate_for_chunk(
        chunk_id=chunk.chunk_id, risk=make_risk(RiskBand.LOW), source=source,
        snapshot=snapshot, chunk_plan=chunk_plan, config=config, params=make_params(seed=2),
        model_caller=generator, cache=cache,
    )

    assert generator.call_count == 2


# ---------------------------------------------------------------------------
# Output validation
# ---------------------------------------------------------------------------


def test_reasoning_contract():
    # V4.1: reasoning is a range {0,1,2,3} (0=off, 1=low, 2=medium, 3=high);
    # the historical hard-zero ban is gone, out-of-range still rejects.
    for level in (0, 1, 2, 3):
        params = GenerationParams(temperature=0.2, seed=1, max_tokens=100, reasoning=level)
        assert params.reasoning == level
    with pytest.raises(ValueError):
        GenerationParams(temperature=0.2, seed=1, max_tokens=100, reasoning=4)
    with pytest.raises(ValueError):
        GenerationParams(temperature=0.2, seed=1, max_tokens=100, reasoning=-1)


@pytest.mark.parametrize(
    "corrupt",
    [
        "extra",
        "missing",
        "reordered",
        "duplicate",
    ],
)
def test_pid_mismatch_variants_are_rejected(corrupt):
    source, snapshot, chunk_plan, chunk, config = make_env(pid_count=4)

    base = {pid: f"Перевод {pid}" for pid in chunk.pids}
    if corrupt == "extra":
        base["p999"] = "Лишний PID"
        raw = json.dumps(base, ensure_ascii=False)
    elif corrupt == "missing":
        base.pop(chunk.pids[0])
        raw = json.dumps(base, ensure_ascii=False)
    elif corrupt == "reordered":
        keys = list(reversed(chunk.pids))
        raw = json.dumps({k: base[k] for k in keys}, ensure_ascii=False)
    elif corrupt == "duplicate":
        # Hand-build the JSON text with a literal repeated key: dict-based
        # construction can't represent this (the second value would just
        # overwrite the first), but Phase 2B must still reject a raw wire
        # payload that repeats a PID key.
        entries = ", ".join(f'"{pid}": "{base[pid]}"' for pid in chunk.pids)
        first_pid = chunk.pids[0]
        raw = "{" + entries + f', "{first_pid}": "Повтор"' + "}"

    generator = ConstantGenerator(lambda bundle: raw)
    outcome = generate_for_chunk(
        chunk_id=chunk.chunk_id, risk=make_risk(RiskBand.LOW), source=source,
        snapshot=snapshot, chunk_plan=chunk_plan, config=config, params=make_params(),
        model_caller=generator,
    )

    assert outcome.status == "incomplete"
    assert "balanced_literary" in outcome.errors
    assert outcome.errors["balanced_literary"].code == GenerationErrorCode.PID_MISMATCH
    assert outcome.candidates == {}


def test_context_pid_leakage_is_rejected():
    source, snapshot, chunk_plan, chunk, config = make_env(pid_count=4)
    leaked = dict(zip(chunk.pids, (f"Перевод {p}" for p in chunk.pids)))
    leaked["p_context"] = "Не должно сюда попадать"
    raw = json.dumps(leaked, ensure_ascii=False)

    generator = ConstantGenerator(lambda bundle: raw)
    outcome = generate_for_chunk(
        chunk_id=chunk.chunk_id, risk=make_risk(RiskBand.LOW), source=source,
        snapshot=snapshot, chunk_plan=chunk_plan, config=config, params=make_params(),
        model_caller=generator,
        left_context=(("p_context", "Контекстный PID."),),
    )

    assert outcome.status == "incomplete"
    assert outcome.errors["balanced_literary"].code == GenerationErrorCode.CONTEXT_LEAKAGE


@pytest.mark.parametrize(
    "raw",
    [
        "{not valid json",
        '{"p0": "Перевод"',  # truncated
        "",
        "null",
        "[1, 2, 3]",
    ],
)
def test_truncated_or_invalid_json_never_becomes_a_candidate(raw):
    source, snapshot, chunk_plan, chunk, config = make_env(pid_count=3)
    generator = ConstantGenerator(lambda bundle: raw)

    outcome = generate_for_chunk(
        chunk_id=chunk.chunk_id, risk=make_risk(RiskBand.LOW), source=source,
        snapshot=snapshot, chunk_plan=chunk_plan, config=config, params=make_params(),
        model_caller=generator,
    )

    assert outcome.status == "incomplete"
    assert outcome.candidates == {}
    assert outcome.errors["balanced_literary"].code == GenerationErrorCode.INVALID_JSON


def test_one_of_ab_failing_yields_incomplete_never_a_substitute():
    # Legacy A/B scheme (lazy_balanced=False): one role failing validation
    # must never let the other role's candidate substitute for it.
    source, snapshot, chunk_plan, chunk, config = make_env(pid_count=5)

    def gen(bundle):
        if bundle.role == "fidelity_first":
            return valid_output_for(chunk)
        return "{not valid json"

    generator = ConstantGenerator(gen)
    outcome = generate_for_chunk(
        chunk_id=chunk.chunk_id, risk=make_risk(RiskBand.HIGH), source=source,
        snapshot=snapshot, chunk_plan=chunk_plan, config=config, params=make_params(),
        model_caller=generator, lazy_balanced=False,
    )

    assert outcome.status == "incomplete"
    assert set(outcome.candidates) == {"fidelity_first"}
    assert "balanced_literary" not in outcome.candidates
    assert outcome.errors["balanced_literary"].code == GenerationErrorCode.INVALID_JSON
    # The successful role's candidate must never be relabelled/duplicated to
    # stand in for the missing one.
    assert outcome.candidates["fidelity_first"].role == "fidelity_first"


# ---------------------------------------------------------------------------
# Review follow-ups: owned source text, snapshot glossary/style content,
# cache-hit defense in depth, and full provenance of the bundle hash.
# ---------------------------------------------------------------------------


def test_owned_source_text_is_actually_sent_to_the_generator():
    """The model must receive the English text of its owned PIDs, not just
    the PID labels — otherwise it cannot translate anything."""
    source, snapshot, chunk_plan, chunk, config = make_env(pid_count=4)
    generator = ConstantGenerator(lambda bundle: valid_output_for(chunk))

    generate_for_chunk(
        chunk_id=chunk.chunk_id, risk=make_risk(RiskBand.LOW), source=source,
        snapshot=snapshot, chunk_plan=chunk_plan, config=config, params=make_params(),
        model_caller=generator,
    )

    bundle = generator.calls[0]
    assert bundle.owned_source == tuple(
        (pid, text) for pid, text in source.source if pid in set(chunk.pids)
    )
    from pact_v4.phase2.prompts import render_prompt

    rendered = render_prompt(bundle)
    for pid, text in bundle.owned_source:
        assert pid in rendered
        assert text in rendered


def test_changing_glossary_or_style_constraints_invalidates_cache_and_prompt():
    source, snapshot, chunk_plan, chunk, config = make_env(pid_count=4)
    generator = ConstantGenerator(lambda bundle: valid_output_for(chunk))
    cache = GenerationCache()

    glossary_a = (GlossaryEntry(source_term="steward", target_terms=("стюард",)),)
    generate_for_chunk(
        chunk_id=chunk.chunk_id, risk=make_risk(RiskBand.LOW), source=source,
        snapshot=snapshot, chunk_plan=chunk_plan, config=config, params=make_params(),
        model_caller=generator, cache=cache, glossary=glossary_a,
        style_constraints={"narrator_voice": "formal"},
    )

    glossary_b = (GlossaryEntry(source_term="steward", target_terms=("дворецкий",)),)
    generate_for_chunk(
        chunk_id=chunk.chunk_id, risk=make_risk(RiskBand.LOW), source=source,
        snapshot=snapshot, chunk_plan=chunk_plan, config=config, params=make_params(),
        model_caller=generator, cache=cache, glossary=glossary_b,
        style_constraints={"narrator_voice": "formal"},
    )

    generate_for_chunk(
        chunk_id=chunk.chunk_id, risk=make_risk(RiskBand.LOW), source=source,
        snapshot=snapshot, chunk_plan=chunk_plan, config=config, params=make_params(),
        model_caller=generator, cache=cache, glossary=glossary_b,
        style_constraints={"narrator_voice": "informal"},
    )

    assert generator.call_count == 3

    from pact_v4.phase2.prompts import render_prompt

    first_prompt = render_prompt(generator.calls[0])
    second_prompt = render_prompt(generator.calls[1])
    assert "стюард" in first_prompt
    assert "дворецкий" in second_prompt
    assert "стюард" not in second_prompt


def test_style_constraints_dict_order_does_not_cause_spurious_cache_miss():
    """Two calls with the same style constraints but different dict
    insertion order must hit the same cache entry — order isn't semantic."""
    source, snapshot, chunk_plan, chunk, config = make_env(pid_count=4)
    generator = ConstantGenerator(lambda bundle: valid_output_for(chunk))
    cache = GenerationCache()

    generate_for_chunk(
        chunk_id=chunk.chunk_id, risk=make_risk(RiskBand.LOW), source=source,
        snapshot=snapshot, chunk_plan=chunk_plan, config=config, params=make_params(),
        model_caller=generator, cache=cache,
        style_constraints={"a": "1", "b": "2"},
    )
    generate_for_chunk(
        chunk_id=chunk.chunk_id, risk=make_risk(RiskBand.LOW), source=source,
        snapshot=snapshot, chunk_plan=chunk_plan, config=config, params=make_params(),
        model_caller=generator, cache=cache,
        style_constraints={"b": "2", "a": "1"},
    )

    assert generator.call_count == 1


def test_cache_hit_revalidates_candidate_identity_defense_in_depth():
    """A cache entry that (by bug or tamper) maps a bundle_hash to a
    candidate for a different chunk/role must never be handed back
    silently — Phase 2B must not "trust the hash" alone on read."""
    from pact_v4.phase2.generation import GenerationCandidateResult
    from pact_v4.phase1.models import Candidate

    source, snapshot, chunk_plan, chunk, config = make_env(pid_count=4)
    generator = ConstantGenerator(lambda bundle: valid_output_for(chunk))
    cache = GenerationCache()

    outcome = generate_for_chunk(
        chunk_id=chunk.chunk_id, risk=make_risk(RiskBand.LOW), source=source,
        snapshot=snapshot, chunk_plan=chunk_plan, config=config, params=make_params(),
        model_caller=generator, cache=cache,
    )
    real_candidate = outcome.candidates["balanced_literary"]

    # Simulate cache poisoning: plant a *different-role* candidate under
    # some other bundle hash, then generate again with a request whose
    # bundle hash happens to collide with that planted key (we simulate the
    # collision directly by writing under the exact hash the next call will
    # compute, rather than trying to find a real sha256 collision).
    other_source, other_snapshot, other_chunk_plan, other_chunk, other_config = make_env(
        pid_count=4, chunk_id="other-chunk"
    )
    other_generator = ConstantGenerator(lambda bundle: valid_output_for(other_chunk))
    other_outcome = generate_for_chunk(
        chunk_id=other_chunk.chunk_id, risk=make_risk(RiskBand.LOW), source=other_source,
        snapshot=other_snapshot, chunk_plan=other_chunk_plan, config=other_config,
        params=make_params(), model_caller=other_generator,
    )
    foreign_candidate = other_outcome.candidates["balanced_literary"]

    # Recompute the exact bundle_hash our victim call will use, then poison
    # the shared cache at that key with the foreign candidate.
    from pact_v4.phase2.generation import PromptBundle
    from pact_v4.phase2.prompts import BALANCED_LITERARY_V4

    victim_bundle = PromptBundle(
        template=BALANCED_LITERARY_V4,
        role="balanced_literary",
        risk_band="low",
        risk_policy_version=make_risk(RiskBand.LOW).policy_version,
        required_risk_feature_codes=(),
        snapshot_hash=snapshot.snapshot_hash,
        source_hash=source.source_hash,
        chunk_id=chunk.chunk_id,
        owned_pids=chunk.pids,
        owned_source=tuple((pid, text) for pid, text in source.source if pid in set(chunk.pids)),
        left_context=(),
        right_context=(),
        glossary=(),
        style_constraints=(),
        bible_text="",
        config_identity=config.config_identity,
        params=make_params(),
    )
    cache.put(victim_bundle.bundle_hash, GenerationCandidateResult(candidate=foreign_candidate, error=None))

    with pytest.raises(AssertionError):
        generate_for_chunk(
            chunk_id=chunk.chunk_id, risk=make_risk(RiskBand.LOW), source=source,
            snapshot=snapshot, chunk_plan=chunk_plan, config=config, params=make_params(),
            model_caller=generator, cache=cache,
        )


def test_candidate_provenance_carries_the_full_bundle_hash():
    """16 hex characters in candidate_id is not provenance; the full
    bundle_hash must be recoverable from the Candidate itself."""
    source, snapshot, chunk_plan, chunk, config = make_env(pid_count=4)
    generator = ConstantGenerator(lambda bundle: valid_output_for(chunk))

    outcome = generate_for_chunk(
        chunk_id=chunk.chunk_id, risk=make_risk(RiskBand.LOW), source=source,
        snapshot=snapshot, chunk_plan=chunk_plan, config=config, params=make_params(),
        model_caller=generator,
    )
    candidate = outcome.candidates["balanced_literary"]
    bundle = generator.calls[0]

    matching = [
        gate for gate in candidate.decision_trace
        if gate.gate == "phase2b_prompt_bundle" and gate.detail == bundle.bundle_hash
    ]
    assert matching, "full bundle_hash must be recoverable from Candidate.decision_trace"
    assert candidate.candidate_id.endswith(bundle.bundle_hash[:16])


def test_prompt_instructions_reference_the_section_that_is_actually_rendered():
    """The instructions must not tell the model to look for an 'OWNED_PIDS'
    section that render_prompt never emits (it emits 'OWNED_SOURCE')."""
    from pact_v4.phase2.prompts import (
        BALANCED_LITERARY_V4,
        FIDELITY_FIRST_V1,
        render_prompt,
    )

    source, snapshot, chunk_plan, chunk, config = make_env(pid_count=4)
    generator = ConstantGenerator(lambda bundle: valid_output_for(chunk))
    outcome = generate_for_chunk(
        chunk_id=chunk.chunk_id, risk=make_risk(RiskBand.HIGH), source=source,
        snapshot=snapshot, chunk_plan=chunk_plan, config=config, params=make_params(),
        model_caller=generator,
    )
    assert outcome.status == "complete"

    for template in (FIDELITY_FIRST_V1, BALANCED_LITERARY_V4):
        assert "OWNED_PIDS" not in template.instructions
    # v1/v2 templates name the block explicitly; the v3 literary template
    # refers to the same rendered section as "the SOURCE map".
    assert "OWNED_SOURCE" in FIDELITY_FIRST_V1.instructions
    assert "SOURCE map" in BALANCED_LITERARY_V4.instructions

    for bundle in generator.calls:
        rendered = render_prompt(bundle)
        assert "OWNED_SOURCE" in rendered
        assert "OWNED_PIDS" not in rendered


# ---------------------------------------------------------------------------
# Work 2B: REQUIRED_RISK_CATEGORIES propagation into the prompt
#
# generation.py imports pact_v4.phase2.risk.REQUIRED_RISK_CATEGORIES rather
# than redeclaring {"number_word", "tone_profanity"}; whichever of those
# categories the source risk pre-screen actually flagged for a chunk is
# threaded onto PromptBundle.required_risk_feature_codes and rendered as an
# explicit instruction — conditionally, not unconditionally.
# ---------------------------------------------------------------------------


def test_number_word_feature_propagates_explicit_instruction_into_prompt():
    from pact_v4.phase2.prompts import render_prompt

    source, snapshot, chunk_plan, chunk, config = make_env(pid_count=4)
    generator = ConstantGenerator(lambda bundle: valid_output_for(chunk))

    risk = make_risk(RiskBand.MEDIUM, features=(make_feature("number_word"),))
    outcome = generate_for_chunk(
        chunk_id=chunk.chunk_id, risk=risk, source=source,
        snapshot=snapshot, chunk_plan=chunk_plan, config=config, params=make_params(),
        model_caller=generator,
    )
    assert outcome.status == "complete"

    for bundle in generator.calls:
        assert bundle.required_risk_feature_codes == ("number_word",)
        assert "Preserve written-out numbers" in render_prompt(bundle)


def test_tone_profanity_feature_propagates_explicit_instruction_into_prompt():
    from pact_v4.phase2.prompts import render_prompt

    source, snapshot, chunk_plan, chunk, config = make_env(pid_count=4)
    generator = ConstantGenerator(lambda bundle: valid_output_for(chunk))

    risk = make_risk(RiskBand.MEDIUM, features=(make_feature("tone_profanity"),))
    outcome = generate_for_chunk(
        chunk_id=chunk.chunk_id, risk=risk, source=source,
        snapshot=snapshot, chunk_plan=chunk_plan, config=config, params=make_params(),
        model_caller=generator,
    )
    assert outcome.status == "complete"

    for bundle in generator.calls:
        assert bundle.required_risk_feature_codes == ("tone_profanity",)
        assert "Preserve source profanity" in render_prompt(bundle)


def test_digit_numbers_feature_does_not_trigger_number_word_instruction():
    """Propagation is conditional on the actual pre-screen result: a plain
    ``numbers`` feature (digits, e.g. "42") is not the ``number_word``
    required category (written-out, e.g. "forty-two"), so it must not add
    the number_word-specific instruction."""
    from pact_v4.phase2.prompts import render_prompt

    source, snapshot, chunk_plan, chunk, config = make_env(pid_count=4)
    generator = ConstantGenerator(lambda bundle: valid_output_for(chunk))

    risk = make_risk(RiskBand.MEDIUM, features=(make_feature("numbers"),))
    outcome = generate_for_chunk(
        chunk_id=chunk.chunk_id, risk=risk, source=source,
        snapshot=snapshot, chunk_plan=chunk_plan, config=config, params=make_params(),
        model_caller=generator,
    )
    assert outcome.status == "complete"

    for bundle in generator.calls:
        assert bundle.required_risk_feature_codes == ()
        rendered = render_prompt(bundle)
        assert "Preserve written-out numbers" not in rendered
        assert "REQUIRED_CATEGORY_INSTRUCTIONS" not in rendered


def test_no_required_risk_features_omits_the_instruction_block_entirely():
    from pact_v4.phase2.prompts import render_prompt

    source, snapshot, chunk_plan, chunk, config = make_env(pid_count=4)
    generator = ConstantGenerator(lambda bundle: valid_output_for(chunk))

    outcome = generate_for_chunk(
        chunk_id=chunk.chunk_id, risk=make_risk(RiskBand.LOW), source=source,
        snapshot=snapshot, chunk_plan=chunk_plan, config=config, params=make_params(),
        model_caller=generator,
    )
    assert outcome.status == "complete"

    rendered = render_prompt(generator.calls[0])
    assert "REQUIRED_CATEGORY_INSTRUCTIONS" not in rendered


def test_both_required_categories_present_propagate_both_instructions():
    from pact_v4.phase2.prompts import render_prompt

    source, snapshot, chunk_plan, chunk, config = make_env(pid_count=4)
    generator = ConstantGenerator(lambda bundle: valid_output_for(chunk))

    risk = make_risk(
        RiskBand.HIGH,
        features=(make_feature("number_word"), make_feature("tone_profanity")),
    )
    outcome = generate_for_chunk(
        chunk_id=chunk.chunk_id, risk=risk, source=source,
        snapshot=snapshot, chunk_plan=chunk_plan, config=config, params=make_params(),
        model_caller=generator,
    )
    assert outcome.status == "complete"

    for bundle in generator.calls:
        assert bundle.required_risk_feature_codes == ("number_word", "tone_profanity")
        rendered = render_prompt(bundle)
        assert "Preserve written-out numbers" in rendered
        assert "Preserve source profanity" in rendered


def test_required_risk_categories_change_in_risk_module_is_reflected_by_generation():
    """generation.py must consume risk.REQUIRED_RISK_CATEGORIES via import,
    not a redeclared literal: patching the risk module's constant changes
    what generation.py filters against, at runtime, with no code edit in
    generation.py itself."""
    import pact_v4.phase2.generation as generation_module
    import pact_v4.phase2.risk as risk_module

    assert generation_module.REQUIRED_RISK_CATEGORIES is risk_module.REQUIRED_RISK_CATEGORIES

    source, snapshot, chunk_plan, chunk, config = make_env(pid_count=4)
    generator = ConstantGenerator(lambda bundle: valid_output_for(chunk))

    original = generation_module.REQUIRED_RISK_CATEGORIES
    try:
        generation_module.REQUIRED_RISK_CATEGORIES = frozenset({"idiom_or_metaphor"})
        risk = make_risk(RiskBand.MEDIUM, features=(make_feature("idiom_or_metaphor"),))
        outcome = generate_for_chunk(
            chunk_id=chunk.chunk_id, risk=risk, source=source,
            snapshot=snapshot, chunk_plan=chunk_plan, config=config, params=make_params(),
            model_caller=generator,
        )
        assert outcome.status == "complete"
        for bundle in generator.calls:
            assert bundle.required_risk_feature_codes == ("idiom_or_metaphor",)
    finally:
        generation_module.REQUIRED_RISK_CATEGORIES = original


def test_generation_module_has_no_hardcoded_required_category_literals():
    """generation.py must not redeclare {"number_word", "tone_profanity"} —
    it may only reference them via the imported REQUIRED_RISK_CATEGORIES
    (or re-export it), never as its own literal set/frozenset."""
    import inspect

    import pact_v4.phase2.generation as generation_module

    source_text = inspect.getsource(generation_module)
    assert "REQUIRED_RISK_CATEGORIES" in source_text
    assert '"number_word"' not in source_text
    assert '"tone_profanity"' not in source_text
    assert "'number_word'" not in source_text
    assert "'tone_profanity'" not in source_text


def test_bundle_hash_changes_only_when_glossary_was_filtered():
    """V4 Efficiency A1.1: ``bundle_hash`` must follow the *filtered*
    glossary set — a chunk whose glossary was not filtered (nothing
    dropped) keeps the identical hash (no spurious cache/resume
    invalidation), while a chunk whose glossary lost a pair gets a
    different hash (cache identity reflects the content actually sent).
    """
    from pact_v4.pipeline._shared_runner_helpers import _glossary_entries_for_chunk

    source, snapshot, chunk_plan, chunk, config = make_env(pid_count=4)
    generator = ConstantGenerator(lambda bundle: valid_output_for(chunk))
    risk = make_risk(RiskBand.LOW)

    full_glossary = (
        GlossaryEntry(source_term="Blake", target_terms=("Блэйк",)),
        GlossaryEntry(source_term="steward", target_terms=("стюард",)),
    )

    def _hash_of(glossary):
        # Fresh cache per measurement: an identical bundle hash would be a
        # cache hit (no model call), which is exactly the property under
        # test — but the hash must be captured from a real call, so each
        # measurement starts from an empty cache.
        generator.calls.clear()
        generate_for_chunk(
            chunk_id=chunk.chunk_id, risk=risk, source=source,
            snapshot=snapshot, chunk_plan=chunk_plan, config=config,
            params=make_params(), model_caller=generator,
            cache=GenerationCache(), glossary=glossary,
        )
        return generator.calls[0].bundle_hash

    def _filtered_for(chunk_text, **filter_kwargs):
        filtered, _dropped = _glossary_entries_for_chunk(
            full_glossary, chunk_text=chunk_text, **filter_kwargs
        )
        return filtered

    # (1) Nothing to drop: every term present -> filtered == full -> the
    # exact same bundle_hash as the unfiltered run (no spurious
    # invalidation).
    both_present = "Blake and the steward spoke."
    assert _filtered_for(both_present) == full_glossary
    assert _hash_of(_filtered_for(both_present)) == _hash_of(full_glossary)

    # (2) One pair dropped: "Blake" absent here and no narrator lock ->
    # filtered set differs -> hash differs from the unfiltered run.
    only_steward = "The steward entered."
    assert _filtered_for(only_steward) == (
        GlossaryEntry(source_term="steward", target_terms=("стюард",)),
    )
    assert _hash_of(_filtered_for(only_steward)) != _hash_of(full_glossary)

    # (3) Same chunk, but the narrator lock keeps "Blake": with the lock the
    # filtered set equals the full set again -> hash matches the unfiltered
    # run (the lock prevents a spurious hash change on an otherwise-absent
    # locked pair).
    with_narrator_lock = _filtered_for(
        only_steward, narrator_gender="male", narrator_source_terms=("Blake",)
    )
    assert with_narrator_lock == full_glossary
    assert _hash_of(with_narrator_lock) == _hash_of(full_glossary)
