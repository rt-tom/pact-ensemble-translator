"""Versioned prompt templates for Phase 2B A/B generation.

Two, and only two, candidate roles are wired to prompt templates here:
``fidelity_first`` (candidate A) and ``balanced_literary`` (candidate B).
There is deliberately no third template and no synthesis template — the
Phase 2C cascaded-selection/synthesis role is out of scope for this module
and must not be stubbed here (see
docs/architecture/V4_FINAL_REVIEW_AND_IMPLEMENTATION_PLAN_RU_v2.md, "2B. A/B
generation" vs "2C. Cascaded selection").

Each template is a frozen, versioned bundle: the *version* string is part of
the prompt bundle identity (see ``pact_v4.phase2.generation.PromptBundle``),
so changing the instructions without bumping the version would silently
change generation behaviour without invalidating caches — that is treated as
a bug, not a feature, hence the version is required and immutable.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class PromptTemplate:
    """An immutable, versioned instruction template for one candidate role."""

    role: str
    version: str
    instructions: str

    def __post_init__(self) -> None:
        if self.role not in ("fidelity_first", "balanced_literary"):
            raise ValueError(
                f"PromptTemplate: unsupported role {self.role!r}; Phase 2B only "
                "defines fidelity_first (A) and balanced_literary (B)"
            )
        if not self.version:
            raise ValueError("PromptTemplate: version must be non-empty")
        if not self.instructions:
            raise ValueError("PromptTemplate: instructions must be non-empty")


_OWNERSHIP_GUARD = (
    "You will be given: (1) a frozen book/chapter memory snapshot, (2) the "
    "chunk's own source PIDs in source order, (3) read-only left_context "
    "(already-committed Russian translation), and (4) read-only right_context "
    "(English source only, not yet translated). "
    "You must translate ONLY the chunk's own owned PIDs listed under "
    "'OWNED_PIDS'. Never translate, return, echo, or paraphrase any PID that "
    "appears only in left_context or right_context — those are read-only "
    "context and are not part of your output. "
    "Return STRICT JSON: an object mapping each owned PID to its Russian "
    "translation, with keys in exactly the same order as OWNED_PIDS, no "
    "missing keys, no extra keys, no duplicate keys, and no keys outside "
    "OWNED_PIDS. Do not wrap the JSON in markdown fences or add commentary."
)

FIDELITY_FIRST_V1 = PromptTemplate(
    role="fidelity_first",
    version="pact-v4-prompt-fidelity-first/v1",
    instructions=(
        "You are translating English fiction into Russian with maximum "
        "fidelity to the source: preserve meaning, register, negation scope, "
        "numbers, named entities and glossary terms exactly. Prefer a more "
        "literal rendering over a more natural-sounding one whenever they "
        "conflict. Respect the glossary and character/style/voice "
        "constraints in the frozen snapshot. " + _OWNERSHIP_GUARD
    ),
)

BALANCED_LITERARY_V1 = PromptTemplate(
    role="balanced_literary",
    version="pact-v4-prompt-balanced-literary/v1",
    instructions=(
        "You are translating English fiction into natural, idiomatic "
        "literary Russian. Balance fidelity to the source with fluency and "
        "voice: prefer the more natural-sounding rendering when a strictly "
        "literal one would read as awkward or foreign, provided meaning, "
        "negation scope, numbers, named entities and glossary terms are "
        "still preserved. Respect the glossary and character/style/voice "
        "constraints in the frozen snapshot. " + _OWNERSHIP_GUARD
    ),
)


def render_prompt(bundle: "Any") -> str:
    """Render the concrete request text for one generation call.

    ``bundle`` is a ``pact_v4.phase2.generation.PromptBundle``; typed loosely
    here to avoid a circular import (``generation`` imports this module).
    Production ``ModelCaller`` implementations may use this to turn a bundle
    into request text; nothing here adds any input that isn't already part
    of ``PromptBundle.bundle_hash``.
    """
    owned_pids = ", ".join(bundle.owned_pids)
    left_context = (
        ", ".join(f"{pid}: {text}" for pid, text in bundle.left_context) or "(none)"
    )
    right_context = (
        ", ".join(f"{pid}: {text}" for pid, text in bundle.right_context) or "(none)"
    )
    return (
        f"{bundle.template.instructions}\n\n"
        f"CHUNK_ID: {bundle.chunk_id}\n"
        f"RISK_BAND: {bundle.risk_band}\n"
        f"OWNED_PIDS: {owned_pids}\n"
        f"left_context: {left_context}\n"
        f"right_context: {right_context}\n"
        f"GLOSSARY_AND_STYLE_CONTEXT: {bundle.glossary_context}\n"
    )
