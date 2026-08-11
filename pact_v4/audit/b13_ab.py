"""B1.3 SPIKE: entity-context A/B harness + 8 test cases (§9.1). NOT production.

This module is an ISOLATED experiment (plan ``V4_1_AUDIT_B1_RU.md`` §10
B1.3, §9.1/§9.2/§9.3). It is deliberately NOT exported from
``pact_v4.audit.__init__`` and NOT referenced by any production code path.
It exists to answer the decision gate: should ``chapter_entity_context`` be
enabled in the production audit (B3)?

Deliverables of this spike:

* ``B13_CASES`` — the 8 §9.1 fixtures: 2 positive (extraction recall),
  4 negative (FP rejection), 2 provenance (poisoned context / false
  validation). Each case carries a hermetic synthetic chapter
  (source/translation PID maps — no real chapter data, per data
  restrictions), the gold entity-context text, and the gold expectations
  (``gold_tp`` = (pid, category) pairs the audit MUST find;
  ``gold_negative`` = pids the audit MUST NOT flag).
* ``run_ab_chapter(...)`` — the A/B runner: on the SAME chunked chapter
  (greedy 8 chunks of chapter 0001 at max_input=3600, same translation
  artifact ``run_006_local_gemma/translations.json``) it runs the v4.1
  ``ChunkedAuditEvaluator`` in three configurations: (a) without
  entity-context, (b) with the manual gold context
  (``chapter_entity_context_0001.txt`` — the etalon), (c) with the
  auto-extracted context (B1.2 extractor on the source chapter).
* metrics — gold TP recall, gold negative rejection, NEW UNKNOWN issues
  (not the raw issue count): ``compute_metrics``.
* ``render_entity_context_text`` — structured ``ChapterEntityContext`` ->
  the prompt text block format (mirrors ``chapter_entity_context_0001.txt``).

Test leakage (§9.3): the v4.1 prompt examples are neutral (checked in B1);
the ONLY channel for chapter context is the ``entity_context`` parameter of
``ChunkedAuditEvaluator``. Nothing in this module edits the frozen prompt.

Real Qwen A/B runs are OWNER-ONLY (rule 2026-08-06). The developer validates
the harness with ``--backend mock`` (scripted responses, 0 Qwen calls) and
hands the owner exact commands (see ``run --help`` and
``docs/plans/V4_B1_3_ENTITY_AB_OWNER_RUN_RU.md``).
"""
from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from pact_v4.audit.chunked_audit import (
    AuditPair,
    ChunkedAuditConfig,
    ChunkedAuditEvaluator,
    ChunkedAuditOutcome,
    build_greedy_chunks,
    pairs_from_maps,
)
from pact_v4.audit.entity_extractor import (
    BackendEntityExtractor,
    BackendEntityExtractorConfig,
    ChapterEntityContext,
    EntityRecord,
    STATUS_CANDIDATE,
    STATUS_VERIFIED,
    extract_entity_context,
)
from pact_v4.phase1.models import SourceArtifact
from pact_v4.runtime.backend_protocol import (
    BackendDescriptor,
    CompletionBackend,
    CompletionRequest,
    CompletionResponse,
)


# ---------------------------------------------------------------------------
# §9.1 fixtures — the 8 cases
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class B13Case:
    """One §9.1 case: hermetic chapter + gold entity context + expectations.

    ``source``/``translation`` are PID->text maps of a small synthetic
    chapter that isolates the phenomenon the case measures. ``gold_tp`` is
    a tuple of ``(pid, category)`` the audit MUST report (positive cases);
    ``gold_negative`` is a tuple of PIDs the audit MUST NOT flag (negative /
    provenance cases). ``entity_context`` is the gold context text (the
    etalon block for this chapter, as ``chapter_entity_context_0001.txt``
    format); for negative/provenance cases it is the context that must NOT
    cause a false positive.
    """

    case_id: str
    kind: str  # "positive" | "negative" | "provenance"
    title: str
    source: Mapping[str, str]
    translation: Mapping[str, str]
    entity_context: str = ""
    gold_tp: Tuple[Tuple[str, str], ...] = ()
    gold_negative: Tuple[str, ...] = ()
    note: str = ""

    def audit_pairs(self) -> Tuple[AuditPair, ...]:
        return pairs_from_maps(self.source, self.translation)


# Each fixture's PID count stays small so the whole case fits one chunk and
# the contract test can script exactly one model response per case.

B13_CASES: Tuple[B13Case, ...] = (
    # -- 2 positive (extraction recall) ------------------------------------
    B13Case(
        case_id="1",
        kind="positive",
        title="one object, different names: motorcycle -> bike (recall)",
        source={
            "p00001": "I pushed my motorcycle through the gap.",
            "p00002": "I set the motorcycle on the lawn.",
            "p00003": "\"Is that your bike?\"",
            "p00004": "I nodded. \"It's a cheap bike, but it's mine.\"",
        },
        translation={
            "p00001": "Я протолкнул свой мотоцикл сквозь проход.",
            "p00002": "Я поставил мотоцикл на газон.",
            "p00003": "«Это твой велосипед?»",
            "p00004": "Я кивнул. «Дешёвый велосипед, но мой.»",
        },
        entity_context=(
            "- entity: Blake's vehicle\n"
            "  aliases in source: motorcycle, bike\n"
            "  established_type: motorcycle\n"
            "  evidence: p00001 (\"motorcycle\"), p00003 (\"bike\")\n"
            "  note: later \"bike\" (p00003, p00004) refers to this same motorcycle\n"
        ),
        gold_tp=(("p00003", "changed_fact"), ("p00004", "changed_fact")),
        note="gold TP: 'bike' rendered as велосипед (bicycle) while the "
             "established type is motorcycle (p00236-class error).",
    ),
    B13Case(
        case_id="2",
        kind="positive",
        title="one person, different designations: man in scrubs -> nurse -> Rich (recall)",
        source={
            "p00001": "A man in scrubs followed him into the room.",
            "p00002": "The nurse handed her the cup of tea.",
            "p00003": "She smiled up at him. \"Thank you, Rich.\"",
            "p00004": "Nurse Rich looked at his watch.",
        },
        translation={
            "p00001": "Мужчина в медицинской форме вошёл следом.",
            "p00002": "Медсестра подала ей чашку чая.",
            "p00003": "Она улыбнулась ему. «Спасибо, Рич.»",
            "p00004": "Медсестра Рич взглянула на часы.",
        },
        entity_context=(
            "- entity: Rich (the nurse / man in scrubs)\n"
            "  gender: male (verified)\n"
            "  aliases in source: the nurse, the man in scrubs, Rich, Nurse Rich\n"
            "  evidence: p00002 (\"The nurse\"), p00004 (\"Nurse Rich\"), "
            "p00003 (\"him\")\n"
            "  note: the nurse in this chapter is Rich, male; do NOT map to "
            "any other nurse entity\n"
        ),
        gold_tp=(
            ("p00002", "invented_gender"),
            ("p00004", "invented_gender"),
        ),
        note="gold TPs (canon §9.5.3 Fix 3): in this synthetic chapter the "
             "nurse IS Rich (male), so BOTH 'The nurse ...' (p00002 -> feminine "
             "медсестра подала) and 'Nurse Rich ... his watch' (p00004 -> "
             "feminine медсестра Рич взглянула) are invented_gender. The real "
             "chapter-0001 'The Nurse' (female, generic, NOT Rich) does not "
             "apply here — the case's own context makes Rich the nurse.",
    ),
    # -- 4 negative (FP rejection) ----------------------------------------
    B13Case(
        case_id="3",
        kind="negative",
        title="motorcycle AND a separate bicycle/bike in the same chapter (FP: false link)",
        source={
            "p00001": "I pushed my motorcycle through the gap.",
            "p00002": "My sister parked her bicycle by the fence.",
            "p00003": "She rides that bike to school every day.",
        },
        translation={
            "p00001": "Я протолкнул свой мотоцикл сквозь проход.",
            "p00002": "Моя сестра поставила свой велосипед у забора.",
            "p00003": "Она каждый день ездит на этом велосипеде в школу.",
        },
        entity_context=(
            "- entity: Blake's vehicle\n"
            "  aliases in source: motorcycle\n"
            "  established_type: motorcycle\n"
            "  evidence: p00001 (\"motorcycle\")\n"
            "  note: the bicycle/bike in p00002/p00003 belongs to the sister "
            "and is a DIFFERENT vehicle — do not conflate\n"
        ),
        gold_negative=("p00002", "p00003"),
        note="negative: the separate bicycle is a different object; a false "
             "link here would be an FP (both are legitimately велосипед).",
    ),
    B13Case(
        case_id="4",
        kind="negative",
        title="two different nurses (FP: identity merge)",
        source={
            "p00001": "Nurse Anna checked the chart by the window.",
            "p00002": "The nurse by the door handed her the cup of tea.",
            "p00003": "Nurse Anna's shift ended at noon.",
        },
        translation={
            "p00001": "Медсестра Анна просмотрела карту у окна.",
            "p00002": "Медсестра у двери подала ей чашку чая.",
            "p00003": "Смена медсестры Анны закончилась в полдень.",
        },
        entity_context=(
            "- entity: Nurse Anna\n"
            "  gender: female\n"
            "  aliases in source: Nurse Anna\n"
            "  evidence: p00001 (\"Nurse Anna\"), p00003 (\"Nurse Anna\")\n"
            "  note: 'the nurse by the door' (p00002) is a DIFFERENT person "
            "— do not merge\n"
        ),
        gold_negative=("p00002",),
        note="negative: two different nurses; the generic 'the nurse' must "
             "not be merged with Nurse Anna (identity merge = FP).",
    ),
    B13Case(
        case_id="5",
        kind="negative",
        title="generic role matches a book-memory name/role but is a different character (FP: poisoned)",
        source={
            "p00001": "A nurse walked past the waiting room.",
            "p00002": "The nurse didn't look up.",
        },
        translation={
            "p00001": "Мимо приёмной прошла медсестра.",
            "p00002": "Медсестра не подняла взгляда.",
        },
        entity_context=(
            "- entity: Rich (the nurse / man in scrubs)\n"
            "  gender: male\n"
            "  aliases in source: Rich, Nurse Rich\n"
            "  evidence: p00003 (\"Rich\"), p00004 (\"Nurse Rich\")\n"
            "  note: 'the nurse' in p00001/p00002 is a GENERIC passer-by, "
            "NOT Rich — do not apply Rich's gender to her\n"
        ),
        gold_negative=("p00001", "p00002"),
        note="negative: generic role (the nurse) coincidentally matches a "
             "book-memory role (Rich is a nurse); the chapter context must "
             "not poison the generic mention into Rich's gender.",
    ),
    B13Case(
        case_id="6",
        kind="negative",
        title="one repeated term = two different objects (FP: term reuse)",
        source={
            "p00001": "I locked my bike to the rack.",
            "p00002": "A stranger's bike was parked next to mine.",
            "p00003": "My bike had a scratched fender.",
        },
        translation={
            "p00001": "Я пристегнул свой велосипед к стойке.",
            "p00002": "Рядом со мной стоял чужой велосипед.",
            "p00003": "У моего велосипеда было поцарапано крыло.",
        },
        entity_context=(
            "- entity: Blake's bike\n"
            "  aliases in source: my bike\n"
            "  established_type: bike\n"
            "  evidence: p00001 (\"my bike\"), p00003 (\"my bike\")\n"
            "  note: 'a stranger's bike' (p00002) is a DIFFERENT object "
            "despite the same term\n"
        ),
        gold_negative=("p00002",),
        note="negative: the same surface term 'bike' is used for two "
             "different objects; the context must not create a false "
             "object_identity.",
    ),
    # -- 2 provenance ------------------------------------------------------
    B13Case(
        case_id="7",
        kind="provenance",
        title="book memory says wrong gender, source contradicts (poisoned context)",
        source={
            "p00001": "The driver got out and waved at me.",
            "p00002": "He walked around the car slowly.",
            "p00003": "I saw him light a cigarette.",
        },
        translation={
            "p00001": "Водитель вышел и помахал мне.",
            "p00002": "Он медленно обошёл машину.",
            "p00003": "Я видел, как он закурил.",
        },
        entity_context=(
            "- entity: the driver\n"
            "  gender: male\n"
            "  aliases in source: the driver, him\n"
            "  evidence: p00002 (\"He walked\"), p00003 (\"him\")\n"
            "  note: source establishes male (he/him); book memory's female "
            "entry is WRONG for this chapter and must not be used\n"
        ),
        gold_negative=("p00001", "p00002", "p00003"),
        note="provenance: source gender (male, he/him) overrides any poisoned "
             "book-memory gender; the masculine translation is CORRECT and "
             "must pass.",
    ),
    B13Case(
        case_id="8",
        kind="provenance",
        title="spans exist but the evidence window does not prove same_entity (false validation)",
        source={
            "p00001": "The motorcycle sat in the garage all winter.",
            "p00002": "I opened the window.",
            "p00003": "In spring I took the bike out for a ride.",
        },
        translation={
            "p00001": "Мотоцикл всю зиму простоял в гараже.",
            "p00002": "Я открыл окно.",
            "p00003": "Весной я выкатил велосипед покататься.",
        },
        entity_context=(
            "- entity: Blake's vehicle\n"
            "  aliases in source: motorcycle\n"
            "  established_type: motorcycle\n"
            "  evidence: p00001 (\"motorcycle\")\n"
        ),
        gold_negative=("p00003",),
        note="provenance (post-fix §9.5.3): the extractor DID produce a "
             "candidate same_entity relation (bike p00003 = motorcycle?), but "
             "the renderer drops candidate claims — the auditor sees only the "
             "verified anchor, so it must NOT treat the unproven relation as "
             "a fact (no changed_fact on p00003). The audit must not invent "
             "the link from span presence alone.",
    ),
)


def case_by_id(case_id: str) -> B13Case:
    for case in B13_CASES:
        if case.case_id == case_id:
            return case
    raise KeyError(f"no B1.3 case with id {case_id!r}")


# ---------------------------------------------------------------------------
# Metrics (card: gold TP recall, gold negative rejection, NEW unknown issues
# — NOT the raw issue count)
# ---------------------------------------------------------------------------


def gold_tp_recall(
    issues: Sequence[Mapping[str, Any]],
    gold_tp: Sequence[Tuple[str, str]],
) -> float:
    """Fraction of gold (pid, category) pairs the audit actually reported.

    ``gold_tp`` entries are ``(pid, category)`` tuples. An issue matches a
    gold TP when BOTH its ``id`` and ``category`` equal the gold pair
    (a changed_fact at p00003 does not satisfy a gold invented_gender at
    p00003 — category is part of the contract, mirroring the B1 gold suite).
    """
    if not gold_tp:
        return 1.0
    found = {(str(i.get("id")), str(i.get("category"))) for i in issues}
    hits = sum(1 for pair in gold_tp if pair in found)
    return hits / len(gold_tp)


def gold_negative_rejection(
    issues: Sequence[Mapping[str, Any]],
    gold_negative: Sequence[str],
) -> float:
    """Fraction of gold-negative PIDs that got NO issue at all.

    A gold-negative PID must be completely clean (any category on it is a
    violation — the case says the translation of that PID is faithful).
    Empty ``gold_negative`` counts as full rejection (nothing to reject).
    """
    if not gold_negative:
        return 1.0
    flagged = {str(i.get("id")) for i in issues}
    rejected = sum(1 for pid in gold_negative if pid not in flagged)
    return rejected / len(gold_negative)


def new_unknown_issues(
    issues: Sequence[Mapping[str, Any]],
    gold_tp: Sequence[Tuple[str, str]],
    gold_negative: Sequence[str],
) -> List[Dict[str, Any]]:
    """Issues NOT explained by the gold sets — the NEW unknowns.

    An issue is 'unknown' when its (id, category) is not a gold TP AND its
    id is not a gold negative (a gold-negative PID with any issue is a
    rejection violation, already counted there — it is not 'new unknown').
    Returns the list of issue dicts (each with id/category/note/_debug);
    the metric is the LIST, not the total issue count (card: 'НЕ число
    issues').
    """
    tp_set = {(str(pid), str(cat)) for pid, cat in gold_tp}
    neg_set = set(gold_negative)
    return [
        dict(issue)
        for issue in issues
        if (str(issue.get("id")), str(issue.get("category"))) not in tp_set
        and str(issue.get("id")) not in neg_set
    ]


def compute_metrics(
    outcome: ChunkedAuditOutcome,
    *,
    gold_tp: Sequence[Tuple[str, str]],
    gold_negative: Sequence[str],
) -> Dict[str, Any]:
    """All B1.3 decision-gate metrics for one audit outcome."""
    issues = list(outcome.issues)
    return {
        "audit_complete": outcome.audit_complete,
        "issue_count": outcome.issue_count,
        "gold_tp_recall": gold_tp_recall(issues, gold_tp),
        "gold_negative_rejection": gold_negative_rejection(issues, gold_negative),
        "new_unknown_count": len(new_unknown_issues(issues, gold_tp, gold_negative)),
        "new_unknown": [
            {
                "id": i.get("id"),
                "category": i.get("category"),
                "severity": i.get("severity"),
                "confidence": i.get("confidence"),
                "note": i.get("note"),
            }
            for i in new_unknown_issues(issues, gold_tp, gold_negative)
        ],
    }


# ---------------------------------------------------------------------------
# Structured context -> prompt text block (§8.3 / chapter_entity_context.txt
# format). The B1.2 extractor produces a structured ChapterEntityContext;
# the audit prompt consumes a text block. This renderer is the bridge for
# the AUTO configuration of the A/B (production renderer would live in B3).
# ---------------------------------------------------------------------------


def _evidence_line(record: EntityRecord) -> str:
    parts: List[str] = []
    anchor = record.anchor
    if anchor.pid:
        parts.append(f'{anchor.pid} ("{anchor.span}")')
    for alias in record.aliases:
        parts.append(f'{alias.pid} ("{alias.span}")')
    return ", ".join(parts)


def render_entity_context_text(context: ChapterEntityContext) -> str:
    """Render a validated ``ChapterEntityContext`` into the prompt text block.

    Format mirrors ``chapter_entity_context_0001.txt`` (the etalon):
    per entity: ``- entity: <name>``, ``gender:``, ``aliases in source:``,
    ``established_type:``, ``evidence:``, ``note:`` (claims).

    DECISION GATE (§9.5.3, owner+architect 2026-08-10): ONLY verified
    claims are rendered. Candidate claims (``same_entity`` relations, which
    B1.2 always marks candidate) are DROPPED from the audit prompt — the
    real Qwen run showed the auditor accepts a rendered candidate as fact
    (case 8: changed_fact FP on an unproven relation). Candidates remain in
    the structured context for hard filters (forced TIER_B) and repair, but
    never reach the auditor as facts. Anchor/alias spans are code-verified
    by the extractor and always render.
    """
    blocks: List[str] = []
    for record in context.entities:
        lines = [f"- entity: {record.entity}"]
        for claim in record.claims:
            if claim.kind == "gender" and claim.status == STATUS_VERIFIED:
                lines.append(f"  gender: {claim.value} ({claim.status})")
        if record.canonical_type:
            lines.append(f"  established_type: {record.canonical_type}")
        surfaces = [alias.surface for alias in record.aliases]
        if record.entity not in surfaces:
            surfaces.insert(0, record.entity)
        if surfaces:
            lines.append("  aliases in source: " + ", ".join(surfaces))
        evidence = _evidence_line(record)
        if evidence:
            lines.append(f"  evidence: {evidence}")
        claims_note = [
            f"{c.kind}: {c.value} ({c.status})"
            for c in record.claims
            if c.kind != "gender" and c.status == STATUS_VERIFIED
        ]
        if claims_note:
            lines.append("  note: " + "; ".join(claims_note))
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks)


# ---------------------------------------------------------------------------
# A/B runner: the SAME chunks, three entity-context configurations
# ---------------------------------------------------------------------------


def build_source_artifact(chapter_id: str, source: Mapping[str, str]) -> SourceArtifact:
    return SourceArtifact(
        chapter_id=chapter_id,
        source=tuple((pid, text) for pid, text in source.items()),
    )


def run_ab(
    *,
    chapter_id: str,
    source: Mapping[str, str],
    translation: Mapping[str, str],
    gold_entity_context: str,
    backend: CompletionBackend,
    out_dir: Optional[Path] = None,
    max_input_tokens: int = 3600,
    auto_context: Optional[str] = None,
) -> Dict[str, Any]:
    """Run the A/B on identical chunks with three entity-context configs.

    Configurations (card §1): (a) WITHOUT entity-context; (b) WITH the
    manual gold context (``chapter_entity_context_0001.txt`` etalon);
    (c) WITH the auto-extracted context (B1.2 extractor on the source
    chapter — pass the already-rendered text as ``auto_context``, or ``""``
    when auto-extraction is unavailable for the mock run).

    The chunking is IDENTICAL across configs (same pairs, same
    ``max_input_tokens``), so any difference in the outcome is caused ONLY
    by the entity-context block (isolated experiment).

    Returns a dict with per-config outcomes (``ChunkedAuditOutcome``) plus
    the chunk layout, so the caller can compute metrics per config.
    """
    pairs = pairs_from_maps(source, translation)
    chunks = build_greedy_chunks(pairs, max_input=max_input_tokens)
    configs = {
        "none": "",
        "gold": gold_entity_context,
        "auto": auto_context or "",
    }
    results: Dict[str, Any] = {
        "chapter_id": chapter_id,
        "pair_count": len(pairs),
        "chunk_count": len(chunks),
        "chunk_sizes": [len(c) for c in chunks],
        "configs": {},
    }
    for name, entity_context in configs.items():
        evaluator = ChunkedAuditEvaluator(
            backend,
            config=ChunkedAuditConfig(max_input_tokens=max_input_tokens),
        )
        outcome = evaluator(
            chapter_id=chapter_id,
            pairs=pairs,
            entity_context=entity_context,
            out_dir=out_dir / name if out_dir else None,
            out_base=f"ab_{name}",
        )
        results["configs"][name] = {
            "entity_context_chars": len(entity_context),
            "outcome": outcome.to_payload(),
        }
    return results


def save_ab_json(results: Dict[str, Any], out_dir: Path, name: str) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{name}.json"
    path.write_text(
        json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return path


# ---------------------------------------------------------------------------
# Mock backend (developer validation: 0 Qwen calls)
# ---------------------------------------------------------------------------


class MockABBackend(CompletionBackend):
    """Scripted in-memory backend for the B1.3 mock A/B (0 model calls).

    Serves per-request scripts: ``audit_script`` is a list of
    ``CompletionResponse`` (one per chunk call in config order none/gold/
    auto); ``extract_script`` serves the B1.2 extractor prepass when the
    auto config needs one. ``requests`` records every ``CompletionRequest``
    so tests can assert prompt content (e.g. that the entity block only
    appears in the gold/auto prompts).
    """

    _BINDINGS = {
        "default": "qwen-3.6-35b",
        "qwen_audit": "qwen-3.6-35b",
        "entity_extractor": "qwen-3.6-35b",
    }

    def __init__(
        self,
        audit_script: Optional[Sequence[CompletionResponse]] = None,
        extract_script: Optional[Sequence[CompletionResponse]] = None,
    ) -> None:
        self._audit = list(audit_script or [])
        self._extract = list(extract_script or [])
        self.requests: List[CompletionRequest] = []

    @property
    def descriptor(self) -> BackendDescriptor:
        return BackendDescriptor(
            kind="local_llama",
            transport_version="openai-chat-completions/v1",
            endpoint_family="openai_chat_completions",
            public_endpoint="http://127.0.0.1:8094/v1/chat/completions",
            model_bindings=dict(self._BINDINGS),
            effective_options={"temperature": 0.0, "context_size": 49152},
        )

    def complete(self, request: CompletionRequest) -> CompletionResponse:
        self.requests.append(request)
        label = request.label or ""
        if "entity_extractor" in label or "b1.2/entity_extractor" in label:
            if not self._extract:
                raise AssertionError("MockABBackend: extractor script exhausted")
            return self._extract.pop(0)
        if not self._audit:
            raise AssertionError("MockABBackend: audit script exhausted")
        return self._audit.pop(0)

    def close(self) -> None:
        pass


def _ok_response(issues: Sequence[Mapping[str, Any]]) -> CompletionResponse:
    return CompletionResponse(
        text=json.dumps({"issues": list(issues)}, ensure_ascii=False),
        model="qwen-3.6-35b",
        finish_reason="stop",
    )


def _issue(
    pid: str,
    category: str = "changed_fact",
    severity: str = "major",
    confidence: str = "high",
    note: str = "b13 mock",
) -> Dict[str, Any]:
    return {
        "id": pid,
        "category": category,
        "severity": severity,
        "confidence": confidence,
        "note": note,
    }


# ---------------------------------------------------------------------------
# CLI (owner-run / developer mock-run)
# ---------------------------------------------------------------------------


def _load_source_map(path: str) -> Dict[str, str]:
    """Load a PID->source map from a JSON artifact (like gate0_source_*.json)."""
    raw = Path(path).read_text(encoding="utf-8")
    data = json.loads(raw)
    if not isinstance(data, dict):
        raise ValueError(f"source artifact {path}: expected a JSON object (PID->text)")
    return {str(pid): str(text) for pid, text in data.items()}


def _load_translation_map(path: str) -> Dict[str, str]:
    raw = Path(path).read_text(encoding="utf-8")
    data = json.loads(raw)
    if not isinstance(data, dict):
        raise ValueError(f"translation artifact {path}: expected a JSON object (PID->text)")
    return {str(pid): str(text) for pid, text in data.items()}


def build_argparser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="b13_ab",
        description=(
            "B1.3 spike: entity-context A/B on chapter 0001 (8 chunks, "
            "run_006_local_gemma translation). Developer: --backend mock "
            "(0 Qwen calls). Owner: --backend real (local llama-server, "
            "Qwen, port 8094)."
        ),
    )
    parser.add_argument("--source", required=True,
                        help="PID->English source JSON (e.g. gate0_source_0001.json)")
    parser.add_argument("--translation", required=True,
                        help="PID->Russian translation JSON "
                             "(e.g. .../run_006_local_gemma/translations.json)")
    parser.add_argument("--gold-context", default="",
                        help="manual gold entity context (chapter_entity_context_0001.txt)")
    parser.add_argument("--out-dir", required=True, help="output dir for A/B JSONs")
    parser.add_argument("--backend", choices=("mock", "real"), default="mock",
                        help="mock = scripted (0 model calls); real = llama-server")
    parser.add_argument("--chat-url", default="http://127.0.0.1:8094/v1/chat/completions",
                        help="real backend endpoint (default local Qwen llama-server)")
    parser.add_argument("--model", default="Qwen3.6-35B-A3B-UD-Q4_K_XL.gguf",
                        help="real backend model name")
    parser.add_argument("--chapter-id", default="0001")
    parser.add_argument("--run-cases", action="store_true",
                        help="also run the 8 §9.1 case fixtures in mock mode "
                             "and write per-case metrics JSON")
    return parser


def _real_backend(chat_url: str, model: str) -> CompletionBackend:
    from pact_v4.runtime.api_client import ApiClient, ApiClientConfig
    from pact_v4.runtime.local_openai_backend import LocalOpenAIBackend

    api_cfg = ApiClientConfig(
        chat_url=chat_url,
        model=model,
        timeout_seconds=1800.0,
        context_size=49152,
        temperature=0.0,
    )
    backend = LocalOpenAIBackend(api=ApiClient(api_cfg, name="b13-ab"))
    return backend


def run_cases(
    backend_factory,
    out_dir: Path,
) -> Dict[str, Any]:
    """Run all 8 §9.1 fixtures and save per-case metrics.

    ``backend_factory`` is a callable ``(case: B13Case) ->
    CompletionBackend`` returning a fresh backend per case (one case = one
    chunk = one audit call). Mock mode (``MockABBackend`` scripted like the
    IDEAL auditor) validates the harness + metrics wiring with 0 Qwen
    calls; real mode (``_real_backend``) measures actual Qwen precision per
    case.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    summary: Dict[str, Any] = {"schema": "pact-b13-ab-cases/v1", "cases": {}}
    for case in B13_CASES:
        backend = backend_factory(case)
        evaluator = ChunkedAuditEvaluator(backend)
        outcome = evaluator(
            chapter_id="b13case",
            pairs=case.audit_pairs(),
            entity_context=case.entity_context,
        )
        metrics = compute_metrics(
            outcome, gold_tp=case.gold_tp, gold_negative=case.gold_negative
        )
        summary["cases"][case.case_id] = {
            "kind": case.kind,
            "title": case.title,
            "gold_tp": list(case.gold_tp),
            "gold_negative": list(case.gold_negative),
            "metrics": metrics,
        }
        (out_dir / f"case_{case.case_id}.json").write_text(
            json.dumps(
                {"case": case.case_id, "kind": case.kind,
                 "gold_tp": list(case.gold_tp),
                 "gold_negative": list(case.gold_negative),
                 "metrics": metrics},
                ensure_ascii=False, indent=2,
            ),
            encoding="utf-8",
        )
    (out_dir / "cases_summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return summary


def run_cases_mock(out_dir: Path) -> Dict[str, Any]:
    """Mock-mode 8 cases (scripted IDEAL auditor, 0 Qwen calls).

    The ideal auditor returns the gold TP issues for positive cases and
    nothing for negative/provenance cases; the mock therefore pins the
    harness + metrics wiring (a working harness MUST produce recall 1.0 /
    rejection 1.0 / unknown 0 under an ideal auditor). Real precision is
    measured by the owner's ``--backend real`` run.
    """

    def _factory(case: B13Case) -> MockABBackend:
        issues = [_issue(pid, category=cat, note=f"gold {case.case_id}")
                  for pid, cat in case.gold_tp]
        return MockABBackend(audit_script=[_ok_response(issues)])

    return run_cases(_factory, out_dir)


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_argparser().parse_args(argv)
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    source = _load_source_map(args.source)
    translation = _load_translation_map(args.translation)
    gold_context = (
        Path(args.gold_context).read_text(encoding="utf-8")
        if args.gold_context else ""
    )

    auto_context = ""
    if args.backend == "mock":
        # Developer harness-validation: scripted audit (empty issues) for
        # all chunk calls. Chapter 0001 = 8 chunks x 3 configs = 24 calls.
        script = [_ok_response([]) for _ in range(24)]
        backend: CompletionBackend = MockABBackend(audit_script=script)
    else:
        backend = _real_backend(args.chat_url, args.model)
        # AUTO config: B1.2 source-only extractor -> validated context ->
        # rendered text block (owner-run; 1 Qwen call for the whole chapter).
        source_artifact = build_source_artifact(args.chapter_id, source)
        extractor = BackendEntityExtractor(
            backend, config=BackendEntityExtractorConfig()
        )
        result = extract_entity_context(
            source_artifact=source_artifact, extractor=extractor
        )
        auto_context = render_entity_context_text(result.context)

    results = run_ab(
        chapter_id=args.chapter_id,
        source=source,
        translation=translation,
        gold_entity_context=gold_context,
        backend=backend,
        out_dir=out_dir,
        auto_context=auto_context,
    )
    path = save_ab_json(results, out_dir, f"ab_{args.backend}")

    # metrics per config against the chapter-0001 gold sets (B1 §6)
    gold_tp = [
        ("p00010", "invented_gender"), ("p00013", "changed_fact"),
        ("p00032", "invented_gender"), ("p00035", "changed_fact"),
        ("p00093", "negation"), ("p00132", "addition"),
        ("p00193", "invented_gender"), ("p00236", "changed_fact"),
    ]
    gold_negative = ["p00075", "p00106", "p00136", "p00151", "p00184", "p00309"]
    metrics_out: Dict[str, Any] = {"schema": "pact-b13-ab-metrics/v1", "configs": {}}
    for name, cfg in results["configs"].items():
        from pact_v4.audit.chunked_audit import ChunkedAuditOutcome

        outcome = ChunkedAuditOutcome(
            schema=cfg["outcome"]["schema"],
            harness_version=cfg["outcome"]["harness_version"],
            prompt_version=cfg["outcome"]["prompt_version"],
            model=cfg["outcome"]["model"],
            reasoning_budget=cfg["outcome"]["reasoning_budget"],
            max_input_tokens=cfg["outcome"]["max_input_tokens"],
            max_tokens=cfg["outcome"]["max_tokens"],
            overlap_tokens=cfg["outcome"]["overlap_tokens"],
            narrator_context=cfg["outcome"]["narrator_context"],
            entity_context=cfg["outcome"]["entity_context"],
            chunk_count=cfg["outcome"]["chunk_count"],
            successful_chunks=cfg["outcome"]["successful_chunks"],
            failed_chunks=tuple(cfg["outcome"]["failed_chunks"]),
            audit_complete=cfg["outcome"]["audit_complete"],
            issue_count=cfg["outcome"]["issue_count"],
            issues=tuple(cfg["outcome"]["issues"]),
            chunks=tuple(cfg["outcome"]["chunks"]),
        )
        metrics_out["configs"][name] = compute_metrics(
            outcome, gold_tp=gold_tp, gold_negative=gold_negative
        )
    metrics_path = out_dir / f"metrics_{args.backend}.json"
    metrics_path.write_text(
        json.dumps(metrics_out, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    if args.run_cases:
        if args.backend == "mock":
            run_cases_mock(out_dir)
        else:
            run_cases(
                lambda case: _real_backend(args.chat_url, args.model),
                out_dir,
            )

    print(f"A/B results:  {path}")
    print(f"Metrics:      {metrics_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

