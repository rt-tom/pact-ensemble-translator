"""Versioned, frozen prompt templates for Phase 2C reviewer calls.

These are deliberately separate from ``pact_v4.phase2.prompts`` (which only
defines the two A/B generation templates). Reviewer/selector prompts are
out of scope for the Phase 2B library module, by design — they live at the
runtime layer and are versioned here so any change to the wording forces
a version bump that propagates through QwenEvaluator/GemmaSelector
provenance rather than silently changing review behaviour.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ReviewerPrompt:
    """One versioned reviewer prompt template."""

    role: str
    version: str
    instructions: str

    def __post_init__(self) -> None:
        if not self.role:
            raise ValueError("ReviewerPrompt: role must be non-empty")
        if not self.version:
            raise ValueError("ReviewerPrompt: version must be non-empty")
        if not self.instructions:
            raise ValueError("ReviewerPrompt: instructions must be non-empty")


# Qwen fidelity review. The reviewer receives the source (EN) and a
# candidate's translation (RU) PID-map and is asked to emit a strict JSON
# object. The expected schema is documented in
# ``pact_v4.phase2.cascade.QwenEvaluator`` — see that protocol for the
# ``faithful_to_source``/``completeness``/``introduced_errors``/
# ``confidence``/``reason``/``passed`` fields.
QWEN_FIDELITY_V1 = ReviewerPrompt(
    role="qwen_fidelity",
    version="pact-v4-reviewer-qwen-fidelity/v1",
    instructions=(
        "You are a strict fidelity reviewer for Russian translations of "
        "English fiction. You will be given two ordered PID maps: SOURCE "
        "(PID -> English text, source order) and TRANSLATION (PID -> "
        "Russian text, same PIDs in the same order). Your job is to "
        "judge whether the translation preserves the meaning, register, "
        "negation scope, named entities, and numeric values of the "
        "source. Return STRICT JSON, no markdown fences, no commentary, "
        "matching exactly this schema:\n"
        "  faithful_to_source: bool\n"
        "  completeness: bool\n"
        "  introduced_errors: bool\n"
        "  confidence: 'high' | 'medium' | 'low'\n"
        "  reason: short string (one or two sentences)\n"
        "  passed: bool (true iff the translation is acceptable)\n"
        "Do not include any other keys. Do not translate or paraphrase "
        "the source — judge it."
    ),
)


# Gemma Russian preference. The selector receives a list of (candidate_id,
# PID -> RU text) maps with NO English source and chooses the best Russian.
# Expected output schema is documented in
# ``pact_v4.phase2.cascade.GemmaSelector`` — see that protocol for the
# ``detail``-as-preferred-candidate-id convention.
GEMMA_RUSSIAN_PREFERENCE_V1 = ReviewerPrompt(
    role="gemma_russian_preference",
    version="pact-v4-reviewer-gemma-russian-preference/v1",
    instructions=(
        "You are a Russian-language editor. You will be given a list of "
        "candidate translations of the same chunk. Each entry is a JSON "
        "object with a candidate_id and a PID -> Russian-text map. No "
        "source text is provided: judge ONLY the Russian on fluency, "
        "register, natural-sounding word order, idiomatic vocabulary, and "
        "internal consistency across the PIDs. Return STRICT JSON, no "
        "markdown fences, no commentary, with exactly these keys:\n"
        "  preferred_candidate_id: string (the candidate_id of the best "
        "Russian)\n"
        "  reason: short string (one or two sentences)\n"
        "If you cannot choose (all candidates have comparable or "
        "indistinguishable Russian), set preferred_candidate_id to an "
        "empty string and explain in reason."
    ),
)


# Phase 3B Step 6 audit prompts. The output schema (a JSON object with an
# ``issues`` array of ``{pid, category, note, excerpt?}``) and the category
# sets are the contract enforced by ``pact_v4.phase3.audit``'s
# ``_parse_issues`` — Qwen's allowed categories are
# ``QWEN_AUDIT_CATEGORIES``, Gemma's ``GEMMA_AUDIT_CATEGORIES``. Wording
# changes here must bump the version so provenance propagates (the
# ``policy_version`` tags on the produced findings are separate frozen
# strings in ``pact_v4.phase3.audit``).

# Qwen EN<->RU fidelity (source + translation both visible). The prompt
# instructs Qwen to consider idioms/numbers/names/ты-вы while judging
# fidelity, but issues are still reported under the four allowed
# categories (omission/addition/referent/scene) — the parser rejects any
# other category.
QWEN_AUDIT_V1 = ReviewerPrompt(
    role="qwen_chapter_audit",
    version="pact-v4-reviewer-qwen-audit/v1",
    instructions=(
        "You are a strict fidelity auditor for a Russian translation of an "
        "English fiction chapter. You are given one chunk as two ordered PID "
        "maps: SOURCE (PID -> English text) and TRANSLATION (PID -> Russian "
        "text, same PIDs in the same order). Judge whether the translation "
        "preserves the source: meaning, register, negation scope, named "
        "entities, referents, numeric values, idioms, and ты/вы usage. For "
        "every problem you find, report one issue. Return STRICT JSON, no "
        "markdown fences, no commentary, with exactly this schema:\n"
        "  issues: array of objects, each with:\n"
        "    pid: string (the PID the issue is in)\n"
        "    category: exactly one of\n"
        "      'omission' — a source element (word, clause, name, number, "
        "idiom) is missing from the translation\n"
        "      'addition' — content not present in the source was introduced\n"
        "      'referent' — a pronoun/referent/named entity is wrong or "
        "ambiguous\n"
        "      'scene' — scene, narrative voice, register or continuity "
        "drift versus the source\n"
        "    note: short string describing the problem\n"
        "    excerpt: optional short quoted fragment from the translation\n"
        "Do not include any other keys. If the chunk is faithful, return "
        '{"issues": []}.'
    ),
)


# Gemma Russian-only review (spec: "Russian-only review без оригинала").
# Never given the source — same contract as ``GemmaAuditEvaluator``.
GEMMA_AUDIT_V1 = ReviewerPrompt(
    role="gemma_russian_review",
    version="pact-v4-reviewer-gemma-audit/v1",
    instructions=(
        "You are a Russian-language editor reviewing one chunk of a Russian "
        "translation. You are given one ordered PID map: TRANSLATION (PID -> "
        "Russian text). No source text is provided: judge ONLY the Russian on "
        "naturalness, fluency, register, repetition, dialogue, and ты/вы "
        "consistency. For every problem you find, report one issue. Return "
        "STRICT JSON, no markdown fences, no commentary, with exactly this "
        "schema:\n"
        "  issues: array of objects, each with:\n"
        "    pid: string (the PID the issue is in)\n"
        "    category: exactly one of\n"
        "      'calque' — unnatural word-for-word English calque\n"
        "      'register' — inconsistent or wrong register\n"
        "      'repetition' — unnecessary repeated words or phrases\n"
        "      'dialogue' — unnatural or inconsistent dialogue phrasing\n"
        "      'ty_vy' — inconsistent ты/вы address within the chunk\n"
        "    note: short string describing the problem\n"
        "    excerpt: optional short quoted fragment\n"
        "Do not include any other keys. If the chunk is clean, return "
        '{"issues": []}.'
    ),
)


# Phase 4A region/PID repair (V4_MVP_SPEC_RU.md §2 Step 7). The model is
# given a chunk's source + current translation, a located region, and the
# findings it must fix, and is asked to make a *minimal targeted edit*:
# change only what the finding requires, keep everything else verbatim.
REPAIR_REGION_V1 = ReviewerPrompt(
    role="region_repair",
    version="pact-v4-repair-region/v1",
    instructions=(
        "You are a Russian-language editor fixing specific problems in a "
        "Russian translation of English fiction. You are given one chunk as "
        "two ordered PID maps: SOURCE (PID -> English text) and TRANSLATION "
        "(PID -> Russian text, same PIDs in the same order). A REGION and the "
        "FINDINGS describe what to fix. Make a minimal targeted edit: change "
        "only the text needed to resolve the findings, and keep every other "
        "PID and the rest of the affected PID verbatim. Do not re-translate "
        "the whole chunk. Return STRICT JSON, no markdown fences, no "
        "commentary, with exactly this schema:\n"
        "  repaired: object mapping each target PID (only the ones you were "
        "asked to fix) to its corrected Russian text\n"
        "  reason: short string explaining the edit\n"
        "Do not include any other keys. Do not add or remove PIDs."
    ),
)


# --- Render helpers --------------------------------------------------------


def render_qwen_review_prompt(
    *,
    source: dict[str, str],
    translation: dict[str, str],
    template: ReviewerPrompt = QWEN_FIDELITY_V1,
) -> str:
    """Render the Qwen fidelity review request as a single user message."""
    src_lines = "\n".join(f"  {pid}: {text}" for pid, text in source.items())
    tr_lines = "\n".join(f"  {pid}: {text}" for pid, text in translation.items())
    return (
        f"{template.instructions}\n\n"
        f"SOURCE (PID -> English text, source order):\n{src_lines}\n\n"
        f"TRANSLATION (PID -> Russian text, same PIDs in the same order):\n{tr_lines}\n"
    )


def render_gemma_preference_prompt(
    *,
    candidates: list[tuple[str, dict[str, str]]],
    template: ReviewerPrompt = GEMMA_RUSSIAN_PREFERENCE_V1,
) -> str:
    """Render the Gemma Russian-preference request as a single user message."""
    parts: list[str] = []
    for index, (candidate_id, mapping) in enumerate(candidates, start=1):
        body = "\n".join(f"  {pid}: {text}" for pid, text in mapping.items())
        parts.append(
            f"CANDIDATE {index} (candidate_id={candidate_id}):\n{body}\n"
        )
    joined = "\n".join(parts) if parts else "(no candidates provided)"
    return f"{template.instructions}\n\n{joined}"


def render_qwen_audit_prompt(
    *,
    chunk_id: str,
    source: dict[str, str],
    translation: dict[str, str],
    template: ReviewerPrompt = QWEN_AUDIT_V1,
) -> str:
    """Render the Qwen Step 6 fidelity-audit request as one user message."""
    src_lines = "\n".join(f"  {pid}: {text}" for pid, text in source.items())
    tr_lines = "\n".join(f"  {pid}: {text}" for pid, text in translation.items())
    return (
        f"{template.instructions}\n\n"
        f"CHUNK: {chunk_id}\n\n"
        f"SOURCE (PID -> English text):\n{src_lines}\n\n"
        f"TRANSLATION (PID -> Russian text, same PIDs in the same order):\n{tr_lines}\n"
    )


def render_gemma_audit_prompt(
    *,
    chunk_id: str,
    translation: dict[str, str],
    template: ReviewerPrompt = GEMMA_AUDIT_V1,
) -> str:
    """Render the Gemma Russian-only Step 6 review as one user message."""
    tr_lines = "\n".join(f"  {pid}: {text}" for pid, text in translation.items())
    return (
        f"{template.instructions}\n\n"
        f"CHUNK: {chunk_id}\n\n"
        f"TRANSLATION (PID -> Russian text):\n{tr_lines}\n"
    )


def render_repair_prompt(
    *,
    chunk_id: str,
    source: dict[str, str],
    translation: dict[str, str],
    region: Any,
    findings: list[dict[str, str]],
    template: ReviewerPrompt = REPAIR_REGION_V1,
) -> str:
    """Render the Phase 4A region-repair request as one user message.

    ``region`` is a ``pact_v4.phase1.models.Region`` (pid/start/end);
    ``findings`` is a list of serialized finding evidence dicts with
    ``category``/``note``/``excerpt`` keys. Everything the model needs to
    make a minimal targeted edit is rendered here; nothing is hidden in
    caller state.
    """
    src_lines = "\n".join(f"  {pid}: {text}" for pid, text in source.items())
    tr_lines = "\n".join(f"  {pid}: {text}" for pid, text in translation.items())
    region_line = (
        f"pid={region.pid} span=[{region.start}, {region.end})"
    )
    finding_lines = "\n".join(
        f"  - category={f.get('category')} note={f.get('note')}"
        f" excerpt={f.get('excerpt') or '(none)'}"
        for f in findings
    ) or "  (none)"
    return (
        f"{template.instructions}\n\n"
        f"CHUNK: {chunk_id}\n\n"
        f"SOURCE (PID -> English text):\n{src_lines}\n\n"
        f"TRANSLATION (PID -> Russian text, same PIDs in the same order):\n{tr_lines}\n\n"
        f"REGION (the located problem span):\n  {region_line}\n\n"
        f"FINDINGS (what to fix):\n{finding_lines}\n"
    )
