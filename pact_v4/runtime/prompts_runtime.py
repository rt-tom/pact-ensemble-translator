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
