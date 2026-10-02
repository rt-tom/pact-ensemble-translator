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
    "OWNED_SOURCE is an input list/map shown as lines \"PID: English text\" "
    "in source order; it is not an output format — do not reproduce its lines "
    "or its \"PID: text\" layout in your output. "
    "Return exactly one top-level JSON object and nothing else. The object maps "
    "each PID from OWNED_SOURCE to its Russian translation as a string value. "
    "Do not return an array, do not wrap the object in any outer key such as "
    "\"translations\", \"items\", \"paragraphs\" or \"data\", do not use pid/text "
    "records like {\"pid\": \"...\", \"text\": \"...\"} or "
    "{\"id\": \"...\", \"translation\": \"...\"}, and do not nest the translations "
    "inside another object or array. Every top-level value must be a Russian "
    "string. Keys must be exactly the PIDs that appear in OWNED_SOURCE — no "
    "missing keys, no extra keys, no duplicate keys, no keys outside "
    "OWNED_SOURCE — in exactly the same order as OWNED_SOURCE. Do not wrap the "
    "JSON in markdown fences (```) and do not add commentary, explanation, or "
    "extra text before or after the JSON. Shape example (illustrative only — do "
    "not output these placeholder PIDs or texts; use only the actual "
    "OWNED_SOURCE PIDs and your translations): "
    "{\"p00001\": \"Русский перевод...\", \"p00002\": \"Русский перевод...\"}"
)

FIDELITY_FIRST_V1 = PromptTemplate(
    role="fidelity_first",
    version="pact-v4-prompt-fidelity-first/v3",
    instructions=(
        "You are translating English fiction into Russian with maximum "
        "fidelity to the source: preserve meaning, register, negation scope, "
        "numbers, named entities and glossary terms exactly. Prefer a more "
        "literal rendering over a more natural-sounding one whenever they "
        "conflict. Respect the glossary and character/style/voice "
        "constraints in the frozen snapshot. " + _OWNERSHIP_GUARD
    ),
)

BALANCED_LITERARY_V4 = PromptTemplate(
    role="balanced_literary",
    version="pact-v4-prompt-balanced-literary/v7",
    instructions=(
        "You are a professional literary translator rendering an English fiction\n"
        "chapter into natural, polished Russian. The complete chapter is provided\n"
        "below under OWNED_SOURCE. Read every OWNED_SOURCE entry before drafting\n"
        "any translation. Use the full-chapter context to maintain each character's\n"
        "voice, the emotional register of every scene, and consistent translation\n"
        "decisions from the first to the last paragraph. Translate only the PIDs\n"
        "listed under OWNED_SOURCE.\n\n"
        "BOOK CONTEXT (locked, authoritative — do not contradict):\n"
        "{book_context}\n\n"
        "Use BOOK CONTEXT and any CHAPTER ENTITY FACTS as reference constraints\n"
        "for continuity, names, terminology, and established forms. They are not\n"
        "source text and must not be translated, repeated, or added to the output.\n"
        "The English source determines what the passage says; the locked glossary\n"
        "determines established Russian forms. Do not invent facts or explanations.\n\n"
        "LOCKED GLOSSARY (canonical lexical choices and spellings):\n"
        "{glossary_entries}\n\n"
        "Use the listed Russian lemma/name as the mandatory base form.\n"
        "Apply normal Russian inflection when grammar requires it, but do not change\n"
        "the underlying lexical choice, spelling, or transliteration.\n\n"
        "Translate by EFFECT, not by dictionary match:\n"
        "- profanity: match the source's strength and register exactly. Never\n"
        "  soften or intensify it. \"Jesus fuck\" -> \"Господи блядь\", \"fuck off\" ->\n"
        "  \"отъебись\", \"I don't give a flying fuck\" -> \"мне до одного хуя\".\n"
        "  Mild substitutes (drat, darn) stay mild (\"чертовщина\", \"чёрт\").\n"
        "- sarcasm, humor, anger: preserve the character's voice, not the words.\n"
        "- formal/archaic address stays archaic (\"Master Blake\" -> \"мастер Блэйк\").\n\n"
        "Avoid calques: rebuild the sentence under Russian syntax and intonation\n"
        "(\"wannabe-architect\" -> \"недоархитектор\", \"two-theater podunk town\" ->\n"
        "\"городишко с двумя кинотеатрами\"). Do not keep English word order.\n\n"
        "RUSSIAN DIALOGUE TYPOGRAPHY:\n"
        "Use standard Russian literary dialogue formatting.\n"
        "- A spoken replica that forms its own paragraph MUST begin with an em dash (—)\n"
        "  and MUST NOT be enclosed in «quotation marks».\n"
        "- Author attribution:\n"
        "  — Реплика, — сказал он.\n"
        "- If the same sentence continues after the attribution:\n"
        "  — Реплика, — сказал он, — продолжение реплики.\n"
        "- If a new sentence of the same speaker follows the attribution:\n"
        "  — Реплика, — сказал он. — Новое предложение.\n"
        "- Use «...» only for actual quotations, quoted words/titles, or nested speech,\n"
        "  never for ordinary dialogue paragraphs.\n"
        "- Preserve one source paragraph/PID as one translation paragraph/PID.\n\n"
        "Preserve exact details: numbers, times, names, quantities (\"Two past\n"
        "twelve\" = 00:02 -> \"две минуты первого\").\n\n"
        "Do not omit, summarize, or add anything.\n\n"
        "REASONING STRATEGY:\n\n"
        "Use reasoning for chapter-level understanding and high-risk translation\n"
        "decisions only. The runtime will provide the available reasoning budget\n"
        "and may provide a later reminder as that budget is consumed.\n\n"
        "Do NOT translate the chapter paragraph-by-paragraph during reasoning.\n"
        "Do NOT draft Russian values for every PID and do NOT construct a partial\n"
        "or complete JSON object in reasoning.\n\n"
        "Before revisiting any passage in depth, make one complete analytical pass\n"
        "over OWNED_SOURCE from the first PID to the last PID.\n\n"
        "During that pass, build a compact chapter-level understanding of:\n\n"
        "- scene sequence and chronology;\n"
        "- character relationships, speakers, gender, and referents;\n"
        "- narrator state, voice, and emotional progression;\n"
        "- terminology and continuity constraints;\n"
        "- numbers, times, quantities, and exact physical details;\n"
        "- ambiguities whose resolution depends on wider chapter context.\n\n"
        "After complete chapter coverage, revisit only genuinely difficult or\n"
        "high-risk passages, such as:\n\n"
        "- ambiguous words or syntax;\n"
        "- idioms and figurative language;\n"
        "- unclear referents or speaker attribution;\n"
        "- chronology and time expressions;\n"
        "- exact physical details;\n"
        "- unusual register or profanity;\n"
        "- passages where literal translation would distort meaning.\n\n"
        "Do not repeatedly analyze the same PID from multiple perspectives unless\n"
        "resolving a concrete contradiction.\n\n"
        "Before producing the final answer, perform one compact global consistency\n"
        "review focused on:\n\n"
        "- narrator/character gender and referents;\n"
        "- names and locked glossary forms;\n"
        "- chronology, numbers, times, quantities, and physical details;\n"
        "- accidental concretization of ambiguous source meaning;\n"
        "- omissions, additions, or semantic reversals;\n"
        "- unintended English-language leakage;\n"
        "- complete PID coverage from the first required PID to the last.\n\n"
        "Then produce the full translation exactly once in the final output.\n\n"
        "LANGUAGE PURITY:\n"
        "The final translation must be Russian. Before finalizing, ensure that no\n"
        "unintended English words or source fragments are carried into translation\n"
        "values. Any Latin-script token must be intentional: a proper name or an\n"
        "explicitly locked foreign form. Otherwise translate it into Russian.\n\n"
        "TRANSLATION VALUE RULE:\n"
        "Inside each JSON string value, output only the Russian translation of the\n"
        "corresponding source paragraph. Do not output any HTML or markup inside\n"
        "translation values. Do not include Markdown, PID labels, comments,\n"
        "explanations, or translator notes. Russian punctuation, including em\n"
        "dashes and quotation marks, is allowed inside translation values.\n\n"
        "DATA BOUNDARIES:\n"
        "OWNED_SOURCE is the only translatable source. left_context and\n"
        "right_context, if present, are read-only reference data and must never\n"
        "be translated, echoed, or included in the output. In whole-chapter mode\n"
        "they may be shown as (none). If mandatory category instructions appear\n"
        "after OWNED_SOURCE, they remain instruction data; they are not part of the\n"
        "chapter and must not appear in the output.\n\n"
        "OUTPUT CONTRACT — MANDATORY AND AUTHORITATIVE:\n"
        "Return exactly one top-level JSON object and nothing else. The complete response must be valid JSON.\n"
        "The object maps each PID from OWNED_SOURCE to its Russian translation as a string value. Do not return an array, do "
        "not wrap the object in any outer key such as \"translations\", \"items\", "
        "\"paragraphs\" or \"data\", do not use pid/text records like "
        "{\"pid\": \"...\", \"text\": \"...\"} or "
        "{\"id\": \"...\", \"translation\": \"...\"}, and do not nest the translations "
        "inside another object or array. Every top-level value must be a Russian-language string. Keys must be exactly the PIDs from OWNED_SOURCE — no missing "
        "keys, no extra keys, no duplicate keys, no keys outside OWNED_SOURCE — "
        "in exactly the same order as OWNED_SOURCE. OWNED_SOURCE is an input "
        "list/map shown as lines \"PID: English text\"; it is not an output format. "
        "Do not wrap the JSON in markdown fences (```) and do not add commentary, "
        "explanation, or extra text before or after the JSON. Shape example "
        "(illustrative only — do not output these placeholder PIDs or texts; use "
        "only the actual OWNED_SOURCE PIDs and your translations): "
        "{\"p00001\": \"Русский перевод...\", \"p00002\": \"Русский перевод...\"}"
    ),
)


# translator-line-output: verbatim whole-chapter line-protocol output contract
# (openspec/changes/translator-line-output/design.md §1). Kept verbatim —
# including the TARGET boundary wording — never paraphrased or redesigned
# here. Architect decision: TARGET is disambiguated at render time by
# labelling the whole-chapter source block TARGET (OWNED_SOURCE) (see
# render_prompt), not by rewriting this block.
_WHOLE_CHAPTER_LINE_CONTRACT = (
    "OUTPUT CONTRACT — MANDATORY:\n"
    "\n"
    "Return only the translations for the PIDs in TARGET, in the exact order shown there.\n"
    "Output exactly one line per TARGET PID, using this format:\n"
    "\n"
    "p00001: Первая переведённая строка.\n"
    "p00002: Вторая переведённая строка.\n"
    "\n"
    "Preserve each TARGET PID exactly and output it exactly once.\n"
    "Put exactly one space after the colon.\n"
    "Keep each translation on a single line; do not merge or split segments.\n"
    "Do not output context-only PIDs.\n"
    "Do not output JSON, Markdown, headings, blank lines, notes, or commentary.\n"
    "Do not add wrapper quotation marks around PIDs or translations.\n"
    "Preserve punctuation and quotation marks that belong in the Russian translation."
)

_WHOLE_CHAPTER_JSON_CONTRACT_MARKER = "OUTPUT CONTRACT — MANDATORY AND AUTHORITATIVE:"


def _derive_whole_chapter_line_instructions(base: str) -> str:
    """Derive whole-chapter line instructions from the shared template.

    translator-line-output (Architect decision): the shared
    BALANCED_LITERARY_V4 template keeps its JSON contract for the chunked
    path; the whole-chapter-only template reuses every instruction
    byte-for-byte except the output-contract block, which is replaced IN
    PLACE (same position, no second contract, no leftover JSON block) with
    the verbatim line contract above. Fail-closed: exactly one contract
    marker must exist, it must run to the end of the template, and the
    removed tail must carry the JSON-contract sentinels — otherwise a base
    edit has moved the block and derivation refuses to guess.
    """
    if base.count(_WHOLE_CHAPTER_JSON_CONTRACT_MARKER) != 1:
        raise AssertionError(
            "prompts: expected exactly one JSON output-contract block in "
            "BALANCED_LITERARY_V4; refusing to derive the whole-chapter "
            "line template"
        )
    head, _, tail = base.partition(_WHOLE_CHAPTER_JSON_CONTRACT_MARKER)
    if "exactly one top-level JSON object" not in tail or "markdown fences" not in tail:
        raise AssertionError(
            "prompts: the JSON output-contract block in BALANCED_LITERARY_V4 "
            "does not carry the expected JSON sentinels; refusing to derive "
            "the whole-chapter line template"
        )
    return head + _WHOLE_CHAPTER_LINE_CONTRACT


BALANCED_LITERARY_WHOLE_CHAPTER_LINE_V1 = PromptTemplate(
    role="balanced_literary",
    version="pact-v4-prompt-balanced-literary-wc-line/v1",
    instructions=_derive_whole_chapter_line_instructions(
        BALANCED_LITERARY_V4.instructions
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


# The FINAL PRE-OUTPUT CHECK checklist, byte-identical for chunked
# (JSON-contract) generation. The whole-chapter line variant is derived
# from it by replacing ONLY the JSON-specific pre-output/output-contract
# validation with line-protocol checks (Architect decision); every
# fidelity/risk check is retained verbatim. Derived once at import with
# exact-occurrence asserts so a checklist edit fails closed instead of
# silently keeping a contradictory JSON mandate in the line prompt.
_FINAL_CHECK_JSON = (
    "FINAL PRE-OUTPUT CHECK — APPLY NOW:\n\n"
    "Before emitting the JSON, perform one final compact risk check. Do not\n"
    "draft or reproduce the full translation in reasoning.\n\n"
    "Re-check the high-risk decisions identified during reasoning, especially:\n\n"
    "- locked glossary spelling/transliteration, allowing normal Russian inflection;\n"
    "- narrator/character gender, speakers, and referents;\n"
    "- chronology, numbers, times, quantities, and exact physical details;\n"
    "- ambiguous passages where the source does not justify added certainty;\n"
    "- omissions, additions, semantic reversals, or unjustified substitutions;\n"
    "- unintended English or Latin-script fragments.\n\n"
    "Then verify the output contract:\n"
    "- one source PID = one output PID;\n"
    "- no missing, extra, duplicate, split, merged, or reordered PIDs;\n"
    "- no translator notes, alternatives, self-corrections, or reasoning text;\n"
    "- exactly one valid JSON object and nothing else.\n\n"
    "Then emit the complete translation.\n"
)


def _derive_whole_chapter_final_check(base: str) -> str:
    checks = base.replace(
        "Before emitting the JSON, perform one final compact risk check.",
        "Before emitting the translation, perform one final compact risk check.",
    )
    if checks.count(
        "Before emitting the translation, perform one final compact risk check."
    ) != 1:
        raise AssertionError(
            "prompts: FINAL CHECK pre-output sentence not found exactly once; "
            "refusing to derive the whole-chapter line checklist"
        )
    checks = checks.replace(
        "- exactly one valid JSON object and nothing else.",
        "- exactly one `PID: translation` line per TARGET PID, "
        "in source order, and nothing else.",
    )
    if checks.count(
        "- exactly one `PID: translation` line per TARGET PID, "
        "in source order, and nothing else."
    ) != 1:
        raise AssertionError(
            "prompts: FINAL CHECK output-contract bullet not found exactly "
            "once; refusing to derive the whole-chapter line checklist"
        )
    if "valid JSON object" in checks or "emitting the JSON" in checks:
        raise AssertionError(
            "prompts: leftover JSON mandate in the whole-chapter line checklist"
        )
    return checks


_FINAL_CHECK_WHOLE_CHAPTER_LINE = _derive_whole_chapter_final_check(
    _FINAL_CHECK_JSON
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
    # translator-line-output (Architect decision): the FINAL CHECK keeps its
    # JSON wording exactly for chunked generation; whole-chapter generation
    # retains every fidelity/risk check but validates the line protocol
    # instead of a JSON object (a JSON mandate here would contradict the
    # whole-chapter OUTPUT CONTRACT). Selection is per-request from the
    # bundle identity — "whole_chapter" is the whole-chapter unit marker
    # (see pact_v4.phase2.generation.generate_whole_chapter).
    if bundle.chunk_id == "whole_chapter":
        final_check = _FINAL_CHECK_WHOLE_CHAPTER_LINE
        owned_source_header = (
            "TARGET (OWNED_SOURCE — translate exactly these PIDs, "
            "in this order):"
        )
    else:
        final_check = _FINAL_CHECK_JSON
        owned_source_header = (
            "OWNED_SOURCE (translate exactly these PIDs, in this order):"
        )
    return (
        f"{template_instructions}\n\n"
        f"{bible_block}{bible_sep}"
        f"STYLE_VOICE_CONSTRAINTS: {style_constraints}\n\n"
        f"CHUNK_ID: {bundle.chunk_id}\n"
        f"RISK_BAND: {bundle.risk_band}\n"
        f"{owned_source_header}\n{owned_source}\n"
        f"left_context (read-only, already-committed Russian): {left_context}\n"
        f"right_context (read-only English source): {right_context}\n"
        f"{glossary_block}"
        f"{required_category_block}"
        f"{final_check}"
    )
