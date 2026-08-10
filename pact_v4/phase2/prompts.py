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
from types import MappingProxyType
from typing import Iterable, Mapping, Tuple

from pact_v4.phase2.risk import REQUIRED_RISK_CATEGORIES


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
    "You will be given: (1) a frozen book/chapter memory snapshot (glossary "
    "and style/voice constraints), (2) the chunk's own source PIDs and their "
    "English text under 'OWNED_SOURCE', in source order, (3) read-only "
    "left_context (already-committed Russian translation), and (4) read-only "
    "right_context (English source only, not yet translated). "
    "You must translate ONLY the PIDs listed under 'OWNED_SOURCE'. Never "
    "translate, return, echo, or paraphrase any PID that appears only in "
    "left_context or right_context — those are read-only context and are not "
    "part of your output. "
    "Return STRICT JSON: an object mapping each PID from OWNED_SOURCE to its "
    "Russian translation, with keys in exactly the same order as "
    "OWNED_SOURCE, no missing keys, no extra keys, no duplicate keys, and no "
    "keys outside OWNED_SOURCE. Do not wrap the JSON in markdown fences or "
    "add commentary."
)

FIDELITY_FIRST_V1 = PromptTemplate(
    role="fidelity_first",
    version="pact-v4-prompt-fidelity-first/v2",
    instructions=(
        "You are translating English fiction into Russian with maximum "
        "fidelity to the source: preserve meaning, register, negation scope, "
        "numbers, named entities and glossary terms exactly. Prefer a more "
        "literal rendering over a more natural-sounding one whenever they "
        "conflict. Respect the glossary and character/style/voice "
        "constraints in the frozen snapshot. " + _OWNERSHIP_GUARD
    ),
)

BALANCED_LITERARY_V3 = PromptTemplate(
    role="balanced_literary",
    version="pact-v4-prompt-balanced-literary/v3",
    instructions=(
        "You are a professional literary translator rendering an English fiction\n"
        "chapter into natural, polished Russian. You have already read the whole\n"
        "chapter: use that context to hold each character's voice, the emotional\n"
        "register of every scene, and consistent decisions from the first to the\n"
        "last paragraph.\n\n"
        "BOOK CONTEXT (locked, authoritative — do not contradict):\n"
        "{book_context}\n\n"
        "LOCKED GLOSSARY (use these translations consistently, do not vary):\n"
        "{glossary_entries}\n\n"
        "Translate by EFFECT, not by dictionary match:\n"
        "- profanity: match the source's strength and register exactly. Never\n"
        "  soften or intensify it. \"Jesus fuck\" -> \"Господи блядь\", \"fuck off\" ->\n"
        "  \"отъебись\", \"I don't give a flying fuck\" -> \"мне до одного хуя\".\n"
        "  Mild substitutes (drat, darn) stay mild (\"чертовщина\", \"чёрт\").\n"
        "- sarcasm, humor, anger: preserve the character's voice, not the words.\n"
        "- formal/archaic address stays archaic (\"Master Blake\" -> \"мастер Блейк\").\n\n"
        "Avoid calques: rebuild the sentence under Russian syntax and intonation\n"
        "(\"wannabe-architect\" -> \"недоархитектор\", \"two-theater podunk town\" ->\n"
        "\"городишко с двумя кинотеатрами\"). Do not keep English word order.\n\n"
        "Preserve exact details: numbers, times, names, quantities (\"Two past\n"
        "twelve\" = 00:02 -> \"две минуты первого\").\n\n"
        "Do not omit, summarize, or add anything. Do not output any HTML or\n"
        "markup — plain Russian text only.\n\n"
        "Return STRICT JSON: an object mapping every PID from the SOURCE map to\n"
        "its Russian translation, keys in exactly the same order as the source,\n"
        "no missing keys, no extra keys, no duplicate keys. Do not wrap the JSON\n"
        "in markdown fences or add commentary."
    ),
)


# Explicit per-category instructions for the risk categories that Phase 2A's
# REQUIRED_RISK_CATEGORIES (pact_v4.phase2.risk) always screens for. Kept as
# a separate, version-controlled mapping (not inlined into the templates
# above) so it is added to the prompt only when the source risk pre-screen
# actually flagged that category for the chunk being generated, not
# unconditionally on every request.
_REQUIRED_CATEGORY_INSTRUCTIONS: Mapping[str, str] = MappingProxyType({
    "number_word": (
        "Preserve written-out numbers exactly. Do not paraphrase 'twelve' "
        "to 'a dozen'."
    ),
    "tone_profanity": (
        "Preserve source profanity/tone exactly. Do not soften or omit."
    ),
})

if frozenset(_REQUIRED_CATEGORY_INSTRUCTIONS) != REQUIRED_RISK_CATEGORIES:
    raise AssertionError(
        "prompts._REQUIRED_CATEGORY_INSTRUCTIONS has drifted from "
        "risk.REQUIRED_RISK_CATEGORIES; every required risk category needs "
        "an explicit propagation instruction here."
    )


def required_category_instructions(risk_feature_codes: Iterable[str]) -> Tuple[str, ...]:
    """Explicit instructions for whichever required categories are present.

    ``risk_feature_codes`` is the set of ``RiskFeature.code`` values the
    source risk pre-screen actually flagged for this chunk (see
    ``pact_v4.phase2.risk.assess_source_risk``). Categories outside
    ``REQUIRED_RISK_CATEGORIES`` (e.g. plain ``numbers``, a digit match, not
    the written-out ``number_word`` category) never produce an instruction
    here — propagation is conditional on the actual pre-screen result, not
    unconditional.
    """
    present = REQUIRED_RISK_CATEGORIES & set(risk_feature_codes)
    return tuple(
        _REQUIRED_CATEGORY_INSTRUCTIONS[code] for code in sorted(present)
    )


def render_prompt(bundle: "Any") -> str:
    """Render the concrete request text for one generation call.

    ``bundle`` is a ``pact_v4.phase2.generation.PromptBundle``; typed loosely
    here to avoid a circular import (``generation`` imports this module).
    Production ``ModelCaller`` implementations may use this to turn a bundle
    into request text; nothing here adds any input that isn't already part
    of ``PromptBundle.bundle_hash``.
    """
    owned_source = (
        "\n".join(f"  {pid}: {text}" for pid, text in bundle.owned_source) or "  (none)"
    )
    left_context = (
        ", ".join(f"{pid}: {text}" for pid, text in bundle.left_context) or "(none)"
    )
    right_context = (
        ", ".join(f"{pid}: {text}" for pid, text in bundle.right_context) or "(none)"
    )
    glossary = (
        "\n".join(
            f"  {term} -> {'/'.join(targets)}" for term, targets in bundle.glossary
        )
        or "  (none)"
    )
    style_constraints = (
        ", ".join(f"{key}={value}" for key, value in bundle.style_constraints) or "(none)"
    )
    instructions = required_category_instructions(bundle.required_risk_feature_codes)
    required_category_block = (
        f"REQUIRED_CATEGORY_INSTRUCTIONS:\n"
        + "\n".join(f"  - {line}" for line in instructions)
        + "\n"
        if instructions
        else ""
    )
    bible_block = bundle.bible_text if bundle.bible_text else ""
    # V4.1 A2: the v3 balanced_literary template declares BOOK CONTEXT and
    # LOCKED GLOSSARY inline via {book_context}/{glossary_entries} tokens.
    # When the template uses these tokens the dynamic blocks are substituted
    # in place and NOT appended again below; older templates keep the
    # append-only layout.
    template_instructions = bundle.template.instructions
    inline_book_context = "{book_context}" in template_instructions
    inline_glossary = "{glossary_entries}" in template_instructions
    if inline_book_context:
        template_instructions = template_instructions.replace(
            "{book_context}", bible_block.strip() or "(none)"
        )
        bible_block = ""
    if inline_glossary:
        template_instructions = template_instructions.replace(
            "{glossary_entries}", glossary.strip() or "(none)"
        )
    # V4 Efficiency A1.2 (provider cache): the static blocks (template
    # instructions, the full bible, style/policy constants) are placed at
    # the START of the message so they form a common prefix across chunks
    # of one run (cached_input_tokens on the provider side). The dynamic
    # blocks (CHUNK_ID, risk band, source, context, glossary) follow.
    # Content is unchanged — only the order moves.
    # A1.2 review fix (LOW): a valid non-empty ``bible_text`` may lack a
    # trailing newline; the bible block must still be separated from the
    # next block by an explicit delimiter, so the following block never
    # glues onto the bible's last line (reproduced "...maleSTYLE_VOICE_..."
    # when the bible ended in "male" with no newline).
    bible_sep = "\n" if bible_block and not bible_block.endswith("\n") else ""
    glossary_block = "" if inline_glossary else f"GLOSSARY:\n{glossary}\n"
    return (
        f"{template_instructions}\n\n"
        f"{bible_block}{bible_sep}"
        f"STYLE_VOICE_CONSTRAINTS: {style_constraints}\n\n"
        f"CHUNK_ID: {bundle.chunk_id}\n"
        f"RISK_BAND: {bundle.risk_band}\n"
        f"OWNED_SOURCE (translate exactly these PIDs, in this order):\n{owned_source}\n"
        f"left_context (read-only, already-committed Russian): {left_context}\n"
        f"right_context (read-only English source): {right_context}\n"
        f"{glossary_block}"
        f"{required_category_block}"
    )
