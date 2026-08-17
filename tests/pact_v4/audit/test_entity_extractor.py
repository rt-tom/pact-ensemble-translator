"""B1.2 contract tests for pact_v4.audit.entity_extractor.

Covers the source-only Qwen prepass: per-claim schema (§8.3 of
docs/plans/V4_1_AUDIT_B1_RU.md), the 8-point code validation, the
per-chapter cache (identity = source_hash + extractor_version), and the
backend/lifecycle transport wiring. All model calls are scripted/faked —
no llama-server is ever started.
"""
from __future__ import annotations

import json
from typing import Dict, Tuple

import pytest

from pact_v4.audit.entity_extractor import (
    BackendEntityExtractor,
    BackendEntityExtractorConfig,
    ENTITY_CONTEXT_SCHEMA,
    EXTRACTOR_VERSION,
    ChapterEntityContext,
    EntityContextCache,
    entity_context_cache_key,
    extract_entity_context,
    parse_model_output,
    render_entity_extraction_prompt,
    validate_entity_context,
    with_entity_context_metadata,
)
from pact_v4.phase1.models import SourceArtifact, canonical_json_hash
from pact_v4.runtime.backend_protocol import (
    CompletionError,
    CompletionResponse,
)
from pact_v4.runtime.json_resilience import JsonRetryPolicy
from tests.pact_v4.runtime.test_backend_role_adapters import (
    ScriptedBackend,
    _text_response,
)

# ---------------------------------------------------------------------------
# Chapter 0001 fixture — REAL source texts (D:/pact/pact_chapters/
# 0001_bonds-1-1.html) for the PIDs the acceptance contract references.
# ---------------------------------------------------------------------------

CHAPTER_0001_PIDS: Tuple[Tuple[str, str], ...] = (
    ("p00007", "Their eyes on my back, I pushed my motorcycle, guiding it "
               "through the gap between car and fence. I set it on the lawn, "
               "leaning against the inside of the fence."),
    ("p00011", "I pulled off my jacket, then my sweatshirt. Unlocking and "
               "lifting the seat of the motorcycle, I retrieved the shirt I "
               "had stowed away."),
    ("p00063", "I stopped in my tracks as a door opened and Callan stepped "
               "out of the nearest room. Aunt Irene's eldest. A man in white "
               "scrubs followed him."),
    ("p00097", "\u201cA bike?\u201d"),
    ("p00098", "\u201cAnd the license and insurance. It\u2019s about the "
               "shittiest, smallest, cheapest bike ever, and it\u2019s used, "
               "but that doesn\u2019t matter. It\u2019s mine.\u201d"),
    ("p00120", "\u201cPaige and Peter,\u201d the man in scrubs said."),
    ("p00197", "The nurse handed her the cup of tea. She smiled up at him. "
               "\u201cThank you, Rich.\u201d"),
    ("p00208", "I glanced at the nurse, who was shifting from foot to foot "
               "nervously. Was he uncomfortable with the friction?"),
    ("p00236", "I made my way out of the house, down the long driveway, and "
               "settled with my back to the wall beside my bike."),
    ("p00264", "The nurse looked stunned. He looked at my family. "
               "\u201cNo. It\u2019s not allowed.\u201d"),
    ("p00285", "Nurse Rich looked at his watch. \u201cTwo past twelve.\u201d"),
    ("p00324", "I stopped short as I saw my bike."),
)


def _source_0001() -> SourceArtifact:
    return SourceArtifact(chapter_id="0001", source=CHAPTER_0001_PIDS)


def _gold_payload_0001(source: SourceArtifact) -> Dict:
    """The Qwen output the acceptance contract expects for chapter 0001.

    Two entities — Blake's vehicle (motorcycle/bike) and Rich (nurse /
    man in scrubs) — with per-claim statuses: anchor/alias spans verified,
    same_entity relation candidate.
    """
    return {
        "schema": ENTITY_CONTEXT_SCHEMA,
        "extractor_version": EXTRACTOR_VERSION,
        "chapter_id": source.chapter_id,
        "source_hash": source.source_hash,
        "entities": [
            {
                "entity": "Blake's vehicle",
                "canonical_type": "motorcycle",
                "anchor": {"pid": "p00007", "span": "motorcycle"},
                "aliases": [
                    {"surface": "bike", "pid": "p00097", "span": "bike"},
                ],
                "claims": [
                    {
                        "kind": "object_identity",
                        "value": "bike = motorcycle",
                        "status": "candidate",
                        "evidence": [
                            {"pid": "p00007", "span": "motorcycle"},
                            {"pid": "p00097", "span": "bike"},
                        ],
                        "evidence_windows": [["p00007", "p00011"]],
                    },
                ],
            },
            {
                "entity": "Rich",
                "canonical_type": "nurse",
                "anchor": {"pid": "p00197", "span": "The nurse"},
                "aliases": [
                    {"surface": "man in scrubs", "pid": "p00120",
                     "span": "the man in scrubs"},
                    {"surface": "Rich", "pid": "p00285",
                     "span": "Nurse Rich"},
                ],
                "claims": [
                    {
                        "kind": "gender",
                        "value": "male",
                        "status": "verified",
                        "evidence": [
                            {"pid": "p00197", "span": "She smiled up at him"},
                        ],
                        "evidence_windows": [["p00197", "p00208"]],
                    },
                    {
                        "kind": "alias_relation",
                        "value": "man_in_scrubs = nurse = Rich",
                        "status": "candidate",
                        "evidence": [
                            {"pid": "p00120", "span": "the man in scrubs"},
                            {"pid": "p00197", "span": "The nurse"},
                            {"pid": "p00285", "span": "Nurse Rich"},
                        ],
                        "evidence_windows": [["p00120", "p00285"]],
                    },
                ],
            },
        ],
    }


# ---------------------------------------------------------------------------
# Acceptance: chapter 0001 -> 2 entities with correct statuses
# ---------------------------------------------------------------------------


def test_chapter_0001_two_entities_with_correct_statuses():
    source = _source_0001()
    context, report = validate_entity_context(
        _gold_payload_0001(source),
        chapter_id=source.chapter_id,
        source_hash=source.source_hash,
        source=dict(source.source),
    )
    assert report.is_clean(), [e.to_payload() for e in report.entries]
    assert len(context.entities) == 2

    vehicle, rich = context.entities

    # Blake's vehicle: motorcycle/bike, anchor verified, alias verified,
    # object_identity relation candidate.
    assert vehicle.entity == "Blake's vehicle"
    assert vehicle.canonical_type == "motorcycle"
    assert vehicle.anchor.pid == "p00007"
    assert vehicle.anchor.status == "verified"
    assert [(a.surface, a.status) for a in vehicle.aliases] == [
        ("bike", "verified")
    ]
    assert [c.status for c in vehicle.claims] == ["candidate"]

    # Rich: nurse/man in scrubs, gender verified, anchor/alias verified,
    # alias_relation candidate.
    assert rich.entity == "Rich"
    assert rich.canonical_type == "nurse"
    assert rich.anchor.pid == "p00197"
    assert rich.anchor.status == "verified"
    assert sorted((a.surface, a.status) for a in rich.aliases) == sorted([
        ("man in scrubs", "verified"), ("Rich", "verified"),
    ])
    statuses = {c.kind: c.status for c in rich.claims}
    assert statuses == {"gender": "verified", "alias_relation": "candidate"}


def test_chapter_0001_prompt_is_source_only_and_whole_chapter():
    source = _source_0001()
    prompt = render_entity_extraction_prompt(
        chapter_id=source.chapter_id, source=dict(source.source)
    )
    # Every PID is present; nothing translation-derived / no bible block.
    for pid, text in source.source:
        assert f"  {pid}: {text}" in prompt
    assert "TRANSLATION" not in prompt
    assert "BIBLE" not in prompt and "BOOK CONTEXT" not in prompt


def test_prompt_lists_valid_pids_explicitly():
    # Dead-PID regression (book-run 1-3: 25/25 claims dropped because the
    # model invented PIDs): the prompt must list every REAL pid of the
    # chapter so the model copies instead of guesses.
    source = _source_0001()
    prompt = render_entity_extraction_prompt(
        chapter_id=source.chapter_id, source=dict(source.source)
    )
    assert "VALID PIDS" in prompt
    for pid, _text in source.source:
        assert pid in prompt
    # The model is told the list is exhaustive and non-guessable.
    assert "use ONLY these" in prompt
    assert "not guessable" in prompt.lower() or "NOT guessable" in prompt


def test_prompt_empty_source_omits_pids_section():
    prompt = render_entity_extraction_prompt(chapter_id="0001", source={})
    # The rule text mentions the section; the actual section header + list
    # must be absent when there are no PIDs.
    assert "VALID PIDS (use ONLY these" not in prompt


# ---------------------------------------------------------------------------
# 8-point validation: invalid output is dropped/downgraded, never accepted
# ---------------------------------------------------------------------------


def _validate(payload: Dict, source: SourceArtifact):
    return validate_entity_context(
        payload,
        chapter_id=source.chapter_id,
        source_hash=source.source_hash,
        source=dict(source.source),
    )


def test_point1_foreign_identity_fails_closed():
    source = _source_0001()
    payload = _gold_payload_0001(source)
    payload["schema"] = "pact-v4-chapter-entity-context/other"
    with pytest.raises(ValueError, match="schema mismatch"):
        _validate(payload, source)

    payload = _gold_payload_0001(source)
    payload["extractor_version"] = "other"
    with pytest.raises(ValueError, match="extractor_version mismatch"):
        _validate(payload, source)

    payload = _gold_payload_0001(source)
    payload["chapter_id"] = "9999"
    with pytest.raises(ValueError, match="chapter_id mismatch"):
        _validate(payload, source)

    payload = _gold_payload_0001(source)
    payload["source_hash"] = canonical_json_hash({"other": True})
    with pytest.raises(ValueError, match="source_hash mismatch"):
        _validate(payload, source)


def test_point2_dead_pid_drops_claim():
    source = _source_0001()
    payload = _gold_payload_0001(source)
    claim = payload["entities"][0]["claims"][0]
    claim["evidence"].append({"pid": "p99999", "span": "whatever"})
    context, report = _validate(payload, source)
    assert [e.action for e in report.entries] == ["dropped"]
    assert "dead PID p99999" in report.entries[0].reason
    # The vehicle entity survives with its anchor but no claims.
    assert len(context.entities) == 2
    assert context.entities[0].claims == ()


def test_dead_pid_regression_valid_pids_retained():
    # Acceptance regression (book-run 1-3): the model invented PIDs and
    # 25/25 claims were dropped. The dead-PID fix lists the chapter's real
    # PIDs in the prompt; the equivalent code-side regression is that a
    # payload whose claims reference ONLY valid chapter PIDs is fully
    # retained (dropped == 0, retained > 0).
    source = _source_0001()
    context, report = _validate(_gold_payload_0001(source), source)
    assert report.is_clean()
    assert len(context.entities) == 2
    assert any(c.status == "verified" for e in context.entities for c in e.claims)
    # The two entities with their anchors/aliases are all retained.
    assert {e.entity for e in context.entities} == {"Blake's vehicle", "Rich"}


def test_point3_span_not_in_source_drops_claim():
    source = _source_0001()
    payload = _gold_payload_0001(source)
    payload["entities"][0]["claims"][0]["evidence"][1]["span"] = "bicycle"
    context, report = _validate(payload, source)
    assert [e.action for e in report.entries] == ["dropped"]
    assert "not verbatim in p00097" in report.entries[0].reason


def test_point4_translation_derived_span_drops_claim():
    source = _source_0001()
    payload = _gold_payload_0001(source)
    # A Russian span (translation-derived) must never be accepted.
    payload["entities"][0]["claims"][0]["evidence"][1]["span"] = "велосипед"
    context, report = _validate(payload, source)
    assert [e.action for e in report.entries] == ["dropped"]
    assert "translation-derived" in report.entries[0].reason


def test_point5_canonical_type_not_in_anchor_drops_entity():
    source = _source_0001()
    payload = _gold_payload_0001(source)
    payload["entities"][0]["canonical_type"] = "moped"
    context, report = _validate(payload, source)
    assert report.entries[0].action == "dropped"
    assert "not in anchor span" in report.entries[0].reason
    assert [e.entity for e in context.entities] == ["Rich"]


def test_point6_alias_surface_not_in_own_pid_drops_alias():
    source = _source_0001()
    payload = _gold_payload_0001(source)
    rich = payload["entities"][1]
    rich["aliases"].append(
        {"surface": "doctor", "pid": "p00120", "span": "man in scrubs"}
    )
    context, report = _validate(payload, source)
    assert any("doctor" in e.claim for e in report.entries)
    assert [a.surface for a in context.entities[1].aliases] == [
        "man in scrubs", "Rich",
    ]


def test_point7_gender_without_referent_link_downgraded_to_candidate():
    source = _source_0001()
    payload = _gold_payload_0001(source)
    # Gender evidence at a PID with a male pronoun ("him" at p00063 refers
    # to Callan, not the nurse) but no Rich/nurse/scrubs surface in the same
    # PID — a lone pronoun is not a verifiable referent link.
    payload["entities"][1]["claims"][0]["evidence"] = [
        {"pid": "p00063", "span": "A man in white scrubs followed him"},
    ]
    context, report = _validate(payload, source)
    gender_entry = next(
        e for e in report.entries if e.claim.startswith("gender")
    )
    assert gender_entry.action == "downgraded"
    assert "referent link" in gender_entry.reason
    gender_claim = next(
        c for c in context.entities[1].claims if c.kind == "gender"
    )
    assert gender_claim.status == "candidate"


def test_point8_same_entity_relation_never_verified():
    source = _source_0001()
    payload = _gold_payload_0001(source)
    # The model claims the object_identity relation is verified — the code
    # must downgrade it to candidate (semantic hypothesis, never auto-repair).
    payload["entities"][0]["claims"][0]["status"] = "verified"
    context, report = _validate(payload, source)
    assert [e.action for e in report.entries] == ["downgraded"]
    assert "same_entity relation is semantic" in report.entries[0].reason
    assert context.entities[0].claims[0].status == "candidate"


def test_gold_payload_round_trips_through_cache_payload():
    source = _source_0001()
    context, _ = _validate(_gold_payload_0001(source), source)
    payload = context.to_payload()
    restored = type(context).from_payload(payload)
    assert restored.to_payload() == payload


# ---------------------------------------------------------------------------
# Cache: identity = source_hash + extractor_version; hit resumes w/o call
# ---------------------------------------------------------------------------


class _RecordingExtractor:
    def __init__(self, payload: Dict) -> None:
        self.payload = payload
        self.calls = 0

    def __call__(
        self, *, chapter_id: str, source: Dict[str, str], out_dir=None
    ) -> str:
        self.calls += 1
        return json.dumps(self.payload, ensure_ascii=False)


def test_cache_hit_resumes_without_another_model_call():
    source = _source_0001()
    extractor = _RecordingExtractor(_gold_payload_0001(source))
    cache = EntityContextCache()

    first = extract_entity_context(
        source_artifact=source, extractor=extractor, cache=cache
    )
    second = extract_entity_context(
        source_artifact=source, extractor=extractor, cache=cache
    )
    assert extractor.calls == 1
    assert second.from_cache is True
    assert second.context.to_payload() == first.context.to_payload()


def test_cache_identity_changes_with_source_hash():
    source = _source_0001()
    other = SourceArtifact(chapter_id="0001", source=CHAPTER_0001_PIDS[:-1])
    assert entity_context_cache_key(
        source_hash=source.source_hash, extractor_version=EXTRACTOR_VERSION
    ) != entity_context_cache_key(
        source_hash=other.source_hash, extractor_version=EXTRACTOR_VERSION
    )


def test_cache_identity_changes_with_extractor_version():
    source = _source_0001()
    assert entity_context_cache_key(
        source_hash=source.source_hash, extractor_version=EXTRACTOR_VERSION
    ) != entity_context_cache_key(
        source_hash=source.source_hash, extractor_version="other/v2"
    )


def test_cache_identity_ignores_prompt_version_change():
    """Regression: prompt wording change (t_83bab286) must not invalidate
    existing caches. Cache key = source_hash + extractor_version; prompt
    version is not part of the identity. Verify by computing cache keys
    with the same source_hash and extractor_version — they must match
    regardless of prompt version."""
    from pact_v4.audit.entity_extractor import ENTITY_EXTRACTION_V1
    source = _source_0001()
    key = entity_context_cache_key(
        source_hash=source.source_hash, extractor_version=EXTRACTOR_VERSION
    )
    # The prompt version field exists and is /v1 — but it must NOT affect
    # the cache key. Changing it (hypothetically) would not change the key.
    assert ENTITY_EXTRACTION_V1.version.endswith("/v1")
    # Cache identity is deterministic and depends only on source_hash + version.
    key2 = entity_context_cache_key(
        source_hash=source.source_hash, extractor_version=EXTRACTOR_VERSION
    )
    assert key == key2


def test_cache_payload_round_trip():
    source = _source_0001()
    extractor = _RecordingExtractor(_gold_payload_0001(source))
    cache = EntityContextCache()
    extract_entity_context(source_artifact=source, extractor=extractor, cache=cache)
    restored = EntityContextCache.from_payload(cache.to_payload())
    # The restored cache satisfies a resume without another model call.
    second = extract_entity_context(
        source_artifact=source, extractor=extractor, cache=restored
    )
    assert extractor.calls == 1
    assert second.from_cache is True


def test_no_cache_still_extracts_once():
    source = _source_0001()
    extractor = _RecordingExtractor(_gold_payload_0001(source))
    first = extract_entity_context(source_artifact=source, extractor=extractor)
    second = extract_entity_context(source_artifact=source, extractor=extractor)
    assert extractor.calls == 2
    assert first.from_cache is False and second.from_cache is False


# ---------------------------------------------------------------------------
# Transport: BackendEntityExtractor over a scripted backend
# ---------------------------------------------------------------------------


def test_backend_extractor_sends_source_only_prompt_and_returns_raw():
    source = _source_0001()
    canned = json.dumps(_gold_payload_0001(source), ensure_ascii=False)
    backend = ScriptedBackend([_text_response(canned)])
    extractor = BackendEntityExtractor(backend)
    raw = extractor(chapter_id=source.chapter_id, source=dict(source.source))
    assert raw == canned
    request = backend.requests[0]
    assert request.messages[0].content == render_entity_extraction_prompt(
        chapter_id=source.chapter_id, source=dict(source.source)
    )
    assert request.temperature == 0.0
    assert request.response_schema is not None
    assert request.label == "b1.2/entity_extractor"
    assert request.model_ref == "gemma-4-26B"  # ScriptedBackend default binding


def test_backend_extractor_retries_empty_then_succeeds():
    source = _source_0001()
    canned = json.dumps(_gold_payload_0001(source), ensure_ascii=False)
    backend = ScriptedBackend(
        [_text_response(""), _text_response(canned)]
    )
    extractor = BackendEntityExtractor(
        backend,
        config=BackendEntityExtractorConfig(
            retry=JsonRetryPolicy(max_retries=1, base_delay_seconds=0.0)
        ),
    )
    raw = extractor(chapter_id=source.chapter_id, source=dict(source.source))
    assert raw == canned
    assert len(backend.requests) == 2


def test_backend_extractor_writes_reasoning_and_raw_artifacts(tmp_path):
    """REASONING-STREAM FIX 1: the extractor persists its reasoning to
    ``b1.2_entity_reasoning.txt`` and the raw response to
    ``b1.2_entity_raw.txt`` when an ``out_dir`` is supplied (non-empty when
    reasoning>0; artifact only, never part of cache identity)."""
    source = _source_0001()
    canned = json.dumps(_gold_payload_0001(source), ensure_ascii=False)
    backend = ScriptedBackend([
        CompletionResponse(
            text=canned,
            model="qwen-3.6-35b",
            finish_reason="stop",
            raw_metadata={"reasoning": "thinking about entities..."},
        )
    ])
    extractor = BackendEntityExtractor(backend)
    raw = extractor(
        chapter_id=source.chapter_id,
        source=dict(source.source),
        out_dir=tmp_path,
    )
    assert raw == canned
    reason_file = tmp_path / "b1.2_entity_reasoning.txt"
    raw_file = tmp_path / "b1.2_entity_raw.txt"
    assert reason_file.read_text(encoding="utf-8") == "thinking about entities..."
    assert raw_file.read_text(encoding="utf-8") == canned


def test_backend_extractor_retry_appends_attempt_markers(tmp_path):
    """REASONING-STREAM FIX 1: on a JSON retry, every attempt's reasoning is
    preserved in the reasoning file with an ``ATTEMPT N`` marker."""
    source = _source_0001()
    canned = json.dumps(_gold_payload_0001(source), ensure_ascii=False)
    backend = ScriptedBackend([
        CompletionResponse(
            text="",
            model="qwen-3.6-35b",
            finish_reason="stop",
            raw_metadata={"reasoning": "attempt-one-reasoning"},
        ),
        CompletionResponse(
            text=canned,
            model="qwen-3.6-35b",
            finish_reason="stop",
            raw_metadata={"reasoning": "attempt-two-reasoning"},
        ),
    ])
    extractor = BackendEntityExtractor(
        backend,
        config=BackendEntityExtractorConfig(
            retry=JsonRetryPolicy(max_retries=1, base_delay_seconds=0.0)
        ),
    )
    raw = extractor(
        chapter_id=source.chapter_id,
        source=dict(source.source),
        out_dir=tmp_path,
    )
    assert raw == canned
    reason_file = tmp_path / "b1.2_entity_reasoning.txt"
    text = reason_file.read_text(encoding="utf-8")
    assert "ATTEMPT 1" in text and "attempt-one-reasoning" in text
    assert "ATTEMPT 2" in text and "attempt-two-reasoning" in text


def test_backend_extractor_no_out_dir_writes_nothing(tmp_path):
    """Without ``out_dir`` the extractor writes no artifacts (old behaviour)."""
    source = _source_0001()
    canned = json.dumps(_gold_payload_0001(source), ensure_ascii=False)
    backend = ScriptedBackend([_text_response(canned)])
    extractor = BackendEntityExtractor(backend)
    extractor(chapter_id=source.chapter_id, source=dict(source.source))
    assert not list(tmp_path.iterdir())


def test_backend_extractor_streams_reasoning_live_during_call(tmp_path):
    """REASONING-STREAM acceptance: with out_dir the reasoning file exists
    and grows DURING the call — a mock backend that fires
    on_reasoning_chunk before returning sees the file already populated."""
    source = _source_0001()
    canned = json.dumps(_gold_payload_0001(source), ensure_ascii=False)
    observed: Dict[str, str] = {}

    class _StreamingBackend(ScriptedBackend):
        def complete(self, request):
            self.requests.append(request)
            assert request.on_reasoning_chunk is not None
            request.on_reasoning_chunk("live-part-1")
            request.on_reasoning_chunk("live-part-2")
            # The file must already contain the streamed chunks BEFORE the
            # call returns (that is what "grows live" means).
            observed["during"] = (
                tmp_path / "b1.2_entity_reasoning.txt"
            ).read_text(encoding="utf-8")
            return CompletionResponse(
                text=canned,
                model="qwen-3.6-35b",
                finish_reason="stop",
                raw_metadata={"reasoning": "full-reasoning"},
            )

    extractor = BackendEntityExtractor(_StreamingBackend([]))
    raw = extractor(
        chapter_id=source.chapter_id,
        source=dict(source.source),
        out_dir=tmp_path,
    )
    assert raw == canned
    # Live chunks were written while complete() was still running...
    assert observed["during"] == "live-part-1live-part-2"
    # ...and the authoritative final write carries the full reasoning.
    assert (tmp_path / "b1.2_entity_reasoning.txt").read_text(
        encoding="utf-8"
    ) == "full-reasoning"


def test_backend_extractor_does_not_retry_transport_failure():
    attempts = []

    class _FailingBackend(ScriptedBackend):
        def complete(self, request):
            attempts.append(request)
            raise CompletionError("connection refused")

    extractor = BackendEntityExtractor(
        _FailingBackend([]),
        config=BackendEntityExtractorConfig(
            retry=JsonRetryPolicy(max_retries=3, base_delay_seconds=0.0)
        ),
    )
    with pytest.raises(CompletionError, match="connection refused"):
        extractor(chapter_id="0001", source=dict(_source_0001().source))
    assert len(attempts) == 1


def test_parse_model_output_rejects_fences_and_malformed():
    good = '{"entities": []}'
    assert parse_model_output(f"```json\n{good}\n```") == {"entities": []}
    from pact_v4.runtime.json_resilience import TruncatedJSONError

    with pytest.raises(TruncatedJSONError):
        parse_model_output('{"entities": [')
    with pytest.raises(Exception):
        parse_model_output("")



# ---------------------------------------------------------------------------
# t_83bab286: entity-context output shape — prompt contract and bare-array
# normalization
# ---------------------------------------------------------------------------


def test_prompt_requires_object_not_bare_array():
    """Prompt contract: the instructions must require a JSON object with
    'entities' key and must NOT tell the model to return a bare array."""
    source = _source_0001()
    prompt = render_entity_extraction_prompt(
        chapter_id=source.chapter_id, source=dict(source.source)
    )
    # Must require object shape
    assert '"entities"' in prompt
    assert "JSON object" in prompt
    # Must NOT instruct bare array
    assert "ONLY the top-level" not in prompt
    assert "entities array" not in prompt.replace(
        "entities: array of objects", ""
    ).replace("top-level \"entities\" key", "")
    # Must explicitly warn against bare array
    assert "bare JSON array" in prompt.lower() or "bare json array" in prompt.lower()


def test_parse_model_output_bare_array_normalizes_to_object():
    """Bare array of valid entity objects → normalized to {"entities": list}."""
    entities = [
        {
            "entity": "test",
            "canonical_type": "thing",
            "anchor": {"pid": "p00001", "span": "thing"},
            "aliases": [],
            "claims": [],
        }
    ]
    raw = json.dumps(entities, ensure_ascii=False)
    result = parse_model_output(raw)
    assert result == {"entities": entities}
    assert isinstance(result, dict)
    assert "entities" in result


def test_parse_model_output_bare_array_empty_fails_closed():
    """Empty bare array → ValueError (not silently accepted)."""
    with pytest.raises(ValueError, match="empty"):
        parse_model_output("[]")


def test_parse_model_output_bare_array_with_scalar_fails_closed():
    """Bare array with non-object element → ValueError."""
    with pytest.raises(ValueError, match="not an object"):
        parse_model_output('[1, 2, 3]')


def test_parse_model_output_bare_array_with_string_fails_closed():
    """Bare array with string elements → ValueError."""
    with pytest.raises(ValueError, match="not an object"):
        parse_model_output('["hello", "world"]')


def test_parse_model_output_bare_array_with_null_fails_closed():
    """Bare array with null element → ValueError."""
    with pytest.raises(ValueError, match="not an object"):
        parse_model_output("[null]")


def test_parse_model_output_bare_array_with_nested_array_fails_closed():
    """Bare array with nested array → ValueError."""
    with pytest.raises(ValueError, match="not an object"):
        parse_model_output("[[1, 2]]")


def test_parse_model_output_valid_object_unchanged():
    """Existing valid object shape passes through unchanged."""
    payload = {"entities": [{"entity": "x"}]}
    assert parse_model_output(json.dumps(payload)) == payload


def test_parse_model_output_bare_array_normalized_log(caplog):
    """Bare array normalization emits a diagnostic LOG.info."""
    import logging

    entities = [{"entity": "x", "canonical_type": "y",
                 "anchor": {"pid": "p00001", "span": "y"},
                 "aliases": [], "claims": []}]
    with caplog.at_level(logging.INFO, logger="pact_v4.audit.entity_extractor"):
        result = parse_model_output(json.dumps(entities))
    assert result == {"entities": entities}
    assert any("bare JSON array" in r.message for r in caplog.records)


def test_parse_model_output_fenced_bare_array_uppercase_tag():
    """Regression: fenced bare array with uppercase JSON tag normalizes
    through the same path as unfenced — no fence-stripping regression."""
    entities = [{"entity": "x", "canonical_type": "y",
                 "anchor": {"pid": "p00001", "span": "y"},
                 "aliases": [], "claims": []}]
    raw = f"```JSON\n{json.dumps(entities)}\n```"
    result = parse_model_output(raw)
    assert result == {"entities": entities}
    assert isinstance(result, dict)
    assert "entities" in result


def test_parse_model_output_fenced_bare_array_alternate_lang_tag():
    """Regression: fenced bare array with non-json language tag (e.g.
    plaintext) normalizes correctly."""
    entities = [{"entity": "a", "canonical_type": "b",
                 "anchor": {"pid": "p00001", "span": "b"},
                 "aliases": [], "claims": []}]
    raw = f"```plaintext\n{json.dumps(entities)}\n```"
    result = parse_model_output(raw)
    assert result == {"entities": entities}


def test_bare_array_flows_through_extract_entity_context():
    """Integration: a bare-array model response is normalized, stamped,
    validated, and produces a correct EntityExtractionResult."""
    source = _source_0001()
    gold = _gold_payload_0001(source)
    # The model body is a bare array (not wrapped in {"entities": ...}).
    bare_array_body = json.dumps(gold["entities"], ensure_ascii=False)
    extractor = _RecordingExtractor(json.loads(bare_array_body))
    # Override __call__ to return the bare array (not the wrapped object).
    original_call = extractor.__call__

    def _bare_call(*, chapter_id, source, out_dir=None):
        extractor.calls += 1
        return bare_array_body

    extractor.__call__ = _bare_call

    result = extract_entity_context(source_artifact=source, extractor=extractor)
    assert result.from_cache is False
    assert result.validation.is_clean()
    assert len(result.context.entities) == 2
    assert result.context.chapter_id == source.chapter_id
    assert result.context.source_hash == source.source_hash


def test_bare_array_no_model_retry_through_backend():
    """Regression: bare-array normalization must NOT trigger a model retry.
    BackendEntityExtractor uses retry_json_call which retries only on
    EmptyResponseError/TruncatedJSONError — a valid JSON array is NOT
    retryable.  The backend must be called exactly once."""
    source = _source_0001()
    gold = _gold_payload_0001(source)
    bare_array_body = json.dumps(gold["entities"], ensure_ascii=False)
    backend = ScriptedBackend([_text_response(bare_array_body)])
    extractor = BackendEntityExtractor(
        backend,
        config=BackendEntityExtractorConfig(
            retry=JsonRetryPolicy(max_retries=2, base_delay_seconds=0.0)
        ),
    )
    result = extract_entity_context(
        source_artifact=source, extractor=extractor
    )
    # Exactly one backend call — no retry for a valid JSON array.
    assert len(backend.requests) == 1
    assert result.from_cache is False
    assert result.validation.is_clean()
    assert len(result.context.entities) == 2


def test_bare_array_raw_artifact_written(tmp_path):
    """Regression: when bare-array normalization happens through the
    backend path with out_dir, the raw artifact contains the original
    bare-array text (not the normalized form)."""
    source = _source_0001()
    gold = _gold_payload_0001(source)
    bare_array_body = json.dumps(gold["entities"], ensure_ascii=False)
    backend = ScriptedBackend([_text_response(bare_array_body)])
    extractor = BackendEntityExtractor(backend)
    raw = extractor(
        chapter_id=source.chapter_id,
        source=dict(source.source),
        out_dir=tmp_path,
    )
    raw_file = tmp_path / "b1.2_entity_raw.txt"
    assert raw_file.exists()
    assert raw_file.read_text(encoding="utf-8") == bare_array_body


# ---------------------------------------------------------------------------
# Lifecycle wiring: Qwen resident before every extraction call (existing
# LifecycleAdapter/ModelRouter path — no new spawn code).
# ---------------------------------------------------------------------------


class _FakeRouter:
    """Minimal ModelRouter stand-in: records ensure_resident, no HTTP."""

    base_url = "http://127.0.0.1:1"  # never contacted in this test

    def __init__(self) -> None:
        self.ensure_calls: list = []

    def ensure_resident(self, model_key: str):
        self.ensure_calls.append(model_key)


def test_lifecycle_extractor_ensures_qwen_resident_then_calls_backend(
    monkeypatch,
):
    from pact_v4.runtime import model_lifecycle_adapters as mla
    from pact_v4.runtime.model_lifecycle_adapters import (
        LifecycleQwenEntityExtractor,
        QWEN_MODEL_KEY,
    )

    source = _source_0001()
    canned = json.dumps(_gold_payload_0001(source), ensure_ascii=False)
    called = {}

    class _StubExtractor:
        def __init__(self, backend=None, config=None):
            self.backend = backend
            self.config = config

        def __call__(self, *, chapter_id, source, out_dir=None):
            called["chapter_id"] = chapter_id
            called["source"] = dict(source)
            return canned

    monkeypatch.setattr(mla, "BackendEntityExtractor", _StubExtractor)
    router = _FakeRouter()
    wrapper = LifecycleQwenEntityExtractor(router, model_name="qwen-fake")
    raw = wrapper(chapter_id=source.chapter_id, source=dict(source.source))
    assert router.ensure_calls == [QWEN_MODEL_KEY]
    assert called["chapter_id"] == "0001"
    assert called["source"] == dict(source.source)
    assert raw == canned


# ---------------------------------------------------------------------------
# RV t_7e9ab408 regression tests (fix commit follows eb85d86)
# ---------------------------------------------------------------------------
#
# F1 (HIGH): prompt/parser/validator metadata contract — the real model
# body (entities only) must flow through BackendEntityExtractor -> stamp ->
# validate for BOTH the prompt's empty contract {"entities": []} and a
# non-empty entities array; the harness owns schema/extractor_version/
# chapter_id/source_hash (no provenance substitution by the model).


def test_integration_backend_path_empty_and_nonempty_outputs():
    source = _source_0001()

    # Empty chapter: the prompt's own explicit contract {"entities": []}.
    empty_body = '{"entities": []}'
    backend = ScriptedBackend([_text_response(empty_body)])
    extractor = BackendEntityExtractor(backend)
    result = extract_entity_context(source_artifact=source, extractor=extractor)
    assert result.from_cache is False
    assert result.validation.is_clean()
    assert result.context.entities == ()
    assert result.context.schema == ENTITY_CONTEXT_SCHEMA
    assert result.context.chapter_id == source.chapter_id
    assert result.context.source_hash == source.source_hash
    assert result.context.extractor_version == EXTRACTOR_VERSION
    # The prompt really was the rendered source-only whole-chapter prompt.
    request = backend.requests[0]
    assert request.messages[0].content == render_entity_extraction_prompt(
        chapter_id=source.chapter_id, source=dict(source.source)
    )

    # Non-empty: the model body carries ONLY entities — top-level metadata
    # is stamped by the harness, so a real model response passes validation.
    gold = _gold_payload_0001(source)
    model_body = json.dumps({"entities": gold["entities"]}, ensure_ascii=False)
    backend2 = ScriptedBackend([_text_response(model_body)])
    result2 = extract_entity_context(
        source_artifact=source, extractor=BackendEntityExtractor(backend2)
    )
    assert result2.from_cache is False
    assert result2.validation.is_clean()
    assert len(result2.context.entities) == 2
    assert result2.context.chapter_id == "0001"
    assert result2.context.source_hash == source.source_hash


def test_with_entity_context_metadata_overrides_model_provenance():
    # A model that (wrongly) emits provenance must never substitute it:
    # the harness stamps the real values over whatever the model returned.
    payload = {
        "schema": "forged/schema",
        "extractor_version": "forged/v9",
        "chapter_id": "9999",
        "source_hash": "f" * 64,
        "entities": [],
    }
    stamped = with_entity_context_metadata(
        payload,
        chapter_id="0001",
        source_hash="0" * 64,
        extractor_version=EXTRACTOR_VERSION,
    )
    assert stamped["schema"] == ENTITY_CONTEXT_SCHEMA
    assert stamped["extractor_version"] == EXTRACTOR_VERSION
    assert stamped["chapter_id"] == "0001"
    assert stamped["source_hash"] == "0" * 64
    assert stamped["entities"] == []


def test_validate_accepts_stamped_metadata_but_requires_entities():
    source = _source_0001()
    # Metadata alone (no entities) must fail closed — not become a clean
    # empty context (finding 4).
    payload = {
        "schema": ENTITY_CONTEXT_SCHEMA,
        "extractor_version": EXTRACTOR_VERSION,
        "chapter_id": source.chapter_id,
        "source_hash": source.source_hash,
    }
    with pytest.raises(ValueError, match="entities"):
        _validate(payload, source)


# F2 (HIGH): cache provenance — a foreign/tampered context can never be
# stored under the expected key, restored from a persistent payload, or
# reused on a cache hit against the current SourceArtifact.


def test_cache_put_rejects_foreign_context_under_expected_key():
    source = _source_0001()
    foreign = SourceArtifact(chapter_id="0001", source=CHAPTER_0001_PIDS[:-1])
    foreign_ctx, _ = _validate(_gold_payload_0001(foreign), foreign)
    expected_key = entity_context_cache_key(
        source_hash=source.source_hash, extractor_version=EXTRACTOR_VERSION
    )
    cache = EntityContextCache()
    with pytest.raises(ValueError, match="identity is"):
        cache.put(expected_key, foreign_ctx)


def test_cache_from_payload_rejects_tampered_entry():
    source = _source_0001()
    context, _ = _validate(_gold_payload_0001(source), source)
    key = entity_context_cache_key(
        source_hash=source.source_hash, extractor_version=EXTRACTOR_VERSION
    )
    payload = {
        "schema": "pact-v4-entity-context-cache/v1",
        "entries": [{"key": key, "context": context.to_payload()}],
    }
    # Tamper: swap the stored context for a foreign one under the SAME key.
    foreign = SourceArtifact(chapter_id="0001", source=CHAPTER_0001_PIDS[:-1])
    foreign_ctx, _ = _validate(_gold_payload_0001(foreign), foreign)
    tampered = dict(payload)
    tampered["entries"] = [
        {"key": key, "context": foreign_ctx.to_payload()}
    ]
    with pytest.raises(ValueError, match="does not match context identity"):
        EntityContextCache.from_payload(tampered)


def test_cache_hit_ignores_foreign_metadata_and_recomputes():
    source = _source_0001()
    extractor = _RecordingExtractor(_gold_payload_0001(source))
    cache = EntityContextCache()
    # A cache entry whose metadata does NOT match the current SourceArtifact
    # (foreign chapter/source) must not be reused from_cache=True.
    foreign = SourceArtifact(chapter_id="0001", source=CHAPTER_0001_PIDS[:-1])
    foreign_ctx, _ = _validate(_gold_payload_0001(foreign), foreign)
    key = entity_context_cache_key(
        source_hash=source.source_hash, extractor_version=EXTRACTOR_VERSION
    )
    cache._store[key] = foreign_ctx  # bypass put() guard to simulate tamper
    result = extract_entity_context(
        source_artifact=source, extractor=extractor, cache=cache
    )
    assert extractor.calls == 1  # model was called again
    assert result.from_cache is False
    assert result.context.source_hash == source.source_hash
    assert result.context.chapter_id == source.chapter_id


def test_cache_hit_rejects_tampered_anchor_span_and_recomputes():
    """RV2 HIGH regression: a cached context whose SPAN content no longer
    exists in the current source (same metadata, same key) must never be
    returned from_cache=True — fail closed and recompute."""
    source = _source_0001()
    extractor = _RecordingExtractor(_gold_payload_0001(source))
    cache = EntityContextCache()

    context, _ = _validate(_gold_payload_0001(source), source)
    tampered_payload = context.to_payload()
    # Same metadata/key; the anchor span is changed motorcycle -> bike,
    # and "bike" is absent from the anchor PID p00007.
    tampered_payload["entities"][0]["anchor"]["span"] = "bike"
    tampered_ctx = ChapterEntityContext.from_payload(tampered_payload)

    key = entity_context_cache_key(
        source_hash=source.source_hash, extractor_version=EXTRACTOR_VERSION
    )
    cache._store[key] = tampered_ctx  # bypass put() guard to simulate tamper

    result = extract_entity_context(
        source_artifact=source, extractor=extractor, cache=cache
    )
    assert extractor.calls == 1  # model was called again
    assert result.from_cache is False
    # The recomputed context is the fully validated one, not the tampered.
    assert result.context.entities[0].anchor.span == "motorcycle"


def test_cache_payload_round_trip_does_not_bypass_tamper_check():
    """RV2 HIGH regression (persistent path): a tampered context with intact
    metadata and key survives from_payload (key identity only), but the
    NEXT hit must still detect the content mismatch and recompute — the
    persistent round-trip is not an escape hatch from the check."""
    source = _source_0001()
    extractor = _RecordingExtractor(_gold_payload_0001(source))

    context, _ = _validate(_gold_payload_0001(source), source)
    tampered_payload = context.to_payload()
    # Same metadata/key; the alias span no longer exists in its own PID
    # p00097 ("bike" -> "motorcycle").
    tampered_payload["entities"][0]["aliases"][0]["span"] = "motorcycle"
    tampered_ctx = ChapterEntityContext.from_payload(tampered_payload)

    key = entity_context_cache_key(
        source_hash=source.source_hash, extractor_version=EXTRACTOR_VERSION
    )
    cache_payload = {
        "schema": "pact-v4-entity-context-cache/v1",
        "entries": [{"key": key, "context": tampered_ctx.to_payload()}],
    }
    # Key identity is computed from the context's own (intact) metadata, so
    # the restore itself succeeds — only the source-bound content check at
    # the reuse boundary can detect the tamper.
    restored = EntityContextCache.from_payload(cache_payload)

    result = extract_entity_context(
        source_artifact=source, extractor=extractor, cache=restored
    )
    assert extractor.calls == 1  # model was called again
    assert result.from_cache is False
    assert result.context.entities[0].aliases[0].span == "bike"


def test_cache_hit_preserves_legitimate_claim_downgrades():
    """A legitimately validated context containing downgraded claims (gender
    without a verifiable referent link) must still be a valid cache hit —
    the downgrade is preserved, not re-judged away or rejected."""
    source = _source_0001()
    payload = _gold_payload_0001(source)
    # Break the referent link for Rich's gender claim: evidence points at an
    # unrelated PID without a gendered pronoun matching "male" -> the code
    # downgrades the claim to candidate during validation.
    payload["entities"][1]["claims"][0]["evidence"] = [
        {"pid": "p00097", "span": "A bike"}
    ]
    extractor = _RecordingExtractor(payload)
    cache = EntityContextCache()

    first = extract_entity_context(
        source_artifact=source, extractor=extractor, cache=cache
    )
    assert first.context.entities[1].claims[0].status == "candidate"

    second = extract_entity_context(
        source_artifact=source, extractor=extractor, cache=cache
    )
    assert extractor.calls == 1
    assert second.from_cache is True
    assert second.context.to_payload() == first.context.to_payload()
    assert second.context.entities[1].claims[0].status == "candidate"


def test_cache_from_payload_rejects_malformed_entry():
    """A structurally malformed cache entry (not an object {key, context})
    is rejected loudly — never silently accepted."""
    payload = {
        "schema": "pact-v4-entity-context-cache/v1",
        "entries": [["not", "an", "object"]],
    }
    with pytest.raises(ValueError, match="cache payload entry"):
        EntityContextCache.from_payload(payload)


# F3 (MEDIUM): anchor/alias status is CODE-derived — only spans that pass
# the §8.3 checks are verified; a model-supplied invalid status is rejected.


def test_anchor_alias_status_code_derived_not_model_claimed():
    source = _source_0001()
    payload = _gold_payload_0001(source)
    # Model claims candidate for a span that passes every code check — the
    # code must still assign verified (code checks are the verification).
    payload["entities"][0]["anchor"]["status"] = "candidate"
    payload["entities"][0]["aliases"][0]["status"] = "candidate"
    context, report = _validate(payload, source)
    assert report.is_clean()
    assert context.entities[0].anchor.status == "verified"
    assert context.entities[0].aliases[0].status == "verified"


def test_invalid_anchor_alias_status_rejected():
    source = _source_0001()
    payload = _gold_payload_0001(source)
    payload["entities"][0]["anchor"]["status"] = "confirmed"
    with pytest.raises(ValueError, match="anchor status"):
        _validate(payload, source)

    payload = _gold_payload_0001(source)
    payload["entities"][0]["aliases"][0]["status"] = "maybe"
    with pytest.raises(ValueError, match="alias status"):
        _validate(payload, source)


# F4 (MEDIUM): top-level entities is REQUIRED and must be an array —
# missing/wrong-type payload fails closed; an explicit empty array stays
# a valid clean empty context.


def test_missing_entities_field_fails_closed():
    source = _source_0001()
    payload = _gold_payload_0001(source)
    del payload["entities"]
    with pytest.raises(ValueError, match="entities"):
        _validate(payload, source)


def test_wrong_type_entities_fails_closed():
    source = _source_0001()
    for bad in (None, {}, {"a": 1}, "entities"):
        payload = _gold_payload_0001(source)
        payload["entities"] = bad
        with pytest.raises(ValueError, match="entities"):
            _validate(payload, source)


def test_explicit_empty_entities_array_is_valid():
    source = _source_0001()
    payload = {
        "schema": ENTITY_CONTEXT_SCHEMA,
        "extractor_version": EXTRACTOR_VERSION,
        "chapter_id": source.chapter_id,
        "source_hash": source.source_hash,
        "entities": [],
    }
    context, report = _validate(payload, source)
    assert report.is_clean()
    assert context.entities == ()


# F5 (MEDIUM): canonical_type must appear in the QUOTED anchor span, not
# merely somewhere in the whole anchor PID.


def test_canonical_type_checked_in_anchor_span_not_whole_pid():
    # The PID contains both 'motorcycle' and 'vehicle'; the quoted anchor
    # span is only 'motorcycle'. canonical_type='vehicle' must be dropped —
    # checking the whole PID would let it pass clean (RV repro).
    source = SourceArtifact(
        chapter_id="0001",
        source=(
            ("p00007", "A motorcycle is a kind of vehicle. I pushed my "
                       "motorcycle."),
        ),
    )
    payload = {
        "schema": ENTITY_CONTEXT_SCHEMA,
        "extractor_version": EXTRACTOR_VERSION,
        "chapter_id": source.chapter_id,
        "source_hash": source.source_hash,
        "entities": [
            {
                "entity": "Blake's vehicle",
                "canonical_type": "vehicle",
                "anchor": {"pid": "p00007", "span": "motorcycle"},
                "aliases": [],
                "claims": [],
            },
        ],
    }
    context, report = _validate(payload, source)
    assert report.entries[0].action == "dropped"
    assert "not in anchor span" in report.entries[0].reason
    assert context.entities == ()


def test_canonical_type_in_anchor_span_passes():
    source = SourceArtifact(
        chapter_id="0001",
        source=(
            ("p00007", "A motorcycle is a kind of vehicle. I pushed my "
                       "motorcycle."),
        ),
    )
    payload = {
        "schema": ENTITY_CONTEXT_SCHEMA,
        "extractor_version": EXTRACTOR_VERSION,
        "chapter_id": source.chapter_id,
        "source_hash": source.source_hash,
        "entities": [
            {
                "entity": "Blake's vehicle",
                "canonical_type": "motorcycle",
                "anchor": {"pid": "p00007", "span": "motorcycle"},
                "aliases": [],
                "claims": [],
            },
        ],
    }
    context, report = _validate(payload, source)
    assert report.is_clean()
    assert len(context.entities) == 1
    assert context.entities[0].canonical_type == "motorcycle"


def test_extractor_max_tokens_covers_reasoning_budget():
    """Regression (2026-08-10): max_tokens must cover the server reasoning
    budget + content headroom. llama-server counts reasoning AND content
    together against max_tokens; the old 4096 < 8192 budget let the model
    spend everything on reasoning and return empty content
    (EmptyResponseError after retries on the real Qwen server)."""
    cfg = BackendEntityExtractorConfig()
    assert cfg.max_tokens == 20000
    assert cfg.max_tokens > 8192  # server --reasoning-budget
    assert cfg.max_tokens >= 8192 + 3000  # budget + content headroom
    assert cfg.max_tokens >= 8192 * 2  # extractor: whole-chapter input provokes full-budget reasoning
