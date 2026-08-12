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
from typing import Any


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


# B1 (v4.1): the chunked audit prompt ported from ``audit_v4.ps1`` (concept
# ``V4_1_AUDIT_B1_RU.md`` §4/§8; harness = the single source of truth for the
# v4 prompt semantics). Versioned separately from ``QWEN_AUDIT_V1`` so a
# wording change forces a version bump that propagates through provenance.
#
# Compared to the v4.2 harness text: v4.1 semantics WITHOUT the procedural
# "MANDATORY GENDER CHECK" rule (that 5-step procedural per-pair check is the
# v4.2 addition). All examples are NEUTRAL — deliberately NOT taken from Pact
# chapter 0001 (test leakage: a model must not be able to pass the §6 gold
# suite by copying an example; ``V4_1_AUDIT_B1_RU.md`` §9.3). The renderer
# appends the BOOK CONTEXT / CHAPTER ENTITY FACTS / CONTEXT_ONLY /
# AUDIT_PAIRS blocks (``render_chunked_audit_prompt``); this template carries
# only the fixed instruction text.
QWEN_AUDIT_V4_1 = ReviewerPrompt(
    role="qwen_chapter_audit",
    version="pact-v4-reviewer-qwen-audit/v4.1",
    instructions=(
        "You are a strict but conservative fidelity auditor for an English-to-Russian\n"
        "literary translation.\n"
        "\n"
        "Below are ordered PAIRs. Each PAIR contains an English SOURCE and its\n"
        "already matched Russian TRANSLATION.\n"
        "\n"
        "Some preceding pairs may be supplied as CONTEXT_ONLY. They exist only to help\n"
        "resolve speakers, referents, ellipsis, and local continuity. NEVER report an\n"
        "issue for a CONTEXT_ONLY pair.\n"
        "\n"
        "Audit ONLY AUDIT_PAIRS.\n"
        "\n"
        "Audit ONLY these error classes:\n"
        "\n"
        "- omission:\n"
        "  a source element (word, clause, name, number, idiom, meaningful semantic\n"
        "  feature) is missing from the translation\n"
        "\n"
        "- addition:\n"
        "  content not present in the source was introduced into the translation\n"
        "\n"
        "- referent:\n"
        "  a pronoun, named entity, speaker, addressee, object, or other referent is\n"
        "  assigned to the wrong entity, or the translation creates a materially wrong\n"
        "  referential relationship\n"
        "\n"
        "- invented_gender:\n"
        "  SOURCE and available context do not establish a person's gender, but the\n"
        "  translation introduces male or female gender\n"
        "\n"
        "- changed_fact:\n"
        "  a number, time, quantity, physical detail, object identity/property,\n"
        "  location, relationship, mental state, certainty level, or other semantic\n"
        "  proposition is materially changed\n"
        "\n"
        "- negation:\n"
        "  negation, temporal scope, aspect, or logical scope changes the proposition\n"
        "\n"
        "\n"
        "GENERAL DECISION RULE\n"
        "\n"
        "Audit SEMANTIC fidelity, not style.\n"
        "\n"
        "PASS when the translation is semantically faithful even if:\n"
        "- wording is freer;\n"
        "- syntax differs;\n"
        "- a different valid synonym is used;\n"
        "- an implicit relation is naturally made explicit;\n"
        "- Russian grammar expresses information already established by context;\n"
        "- the Russian wording is awkward or stylistically imperfect.\n"
        "\n"
        "When genuinely uncertain whether meaning changed, PASS.\n"
        "\n"
        "IMPORTANT:\n"
        "Do NOT use \"stylistic difference\", \"minor wording difference\", or\n"
        "\"when uncertain, PASS\" to discard a candidate AFTER you have established a\n"
        "concrete difference in semantic proposition, object identity/property,\n"
        "character state, negation/scope, modality/certainty, or gender.\n"
        "\n"
        "\n"
        "RULES\n"
        "\n"
        "1. SILENTLY VERIFY EACH CANDIDATE\n"
        "\n"
        "Before reporting an issue, verify it against:\n"
        "- current SOURCE;\n"
        "- current TRANSLATION;\n"
        "- CONTEXT_ONLY and adjacent PAIRs when relevant;\n"
        "- CHAPTER ENTITY FACTS when relevant;\n"
        "- BOOK CONTEXT only as fallback.\n"
        "\n"
        "If verification shows no semantic error, do not include the candidate.\n"
        "\n"
        "Do not expose this reasoning in the final response.\n"
        "\n"
        "\n"
        "2. FINAL NOTES ONLY\n"
        "\n"
        "The note must contain only the confirmed error.\n"
        "\n"
        "Never include internal deliberation such as:\n"
        "\"acceptable\", \"this is correct\", \"wait\", \"let's check\",\n"
        "\"on second thought\", \"stylistic\", \"I'll skip\", \"maybe\", \"probably\".\n"
        "\n"
        "\n"
        "3. EVIDENCE PRIORITY\n"
        "\n"
        "Use evidence in this order:\n"
        "\n"
        "1) explicit current SOURCE;\n"
        "2) explicit adjacent SOURCE / CONTEXT_ONLY;\n"
        "3) source-derived CHAPTER ENTITY FACTS;\n"
        "4) BOOK CONTEXT;\n"
        "5) inference.\n"
        "\n"
        "SOURCE always overrides external context.\n"
        "\n"
        "External context must never make a translation wrong when explicit SOURCE\n"
        "evidence supports it.\n"
        "\n"
        "\n"
        "4. SPEAKER, ADDRESSEE, AND REFERENT ARE DIFFERENT ROLES\n"
        "\n"
        "Do NOT assume that a gender-marked Russian form describes the speaker.\n"
        "\n"
        "Before judging gender, determine WHO the form describes:\n"
        "\n"
        "- speaker;\n"
        "- addressee;\n"
        "- third-person referent.\n"
        "\n"
        "Inside quoted dialogue:\n"
        "- first-person forms describe the SPEAKER;\n"
        "- second-person gender-marked forms describe the ADDRESSEE;\n"
        "- third-person forms describe their REFERENT.\n"
        "\n"
        "Example principle:\n"
        "A male speaker asking a female addressee \"Did you understand?\" — Russian\n"
        "\"Ponyala?\" — is correct. The feminine form describes the female addressee,\n"
        "not the male speaker.\n"
        "\n"
        "Book context may establish a character's gender, but it does NOT establish that\n"
        "the character is the current speaker or addressee.\n"
        "\n"
        "\n"
        "5. INVENTED GENDER - CHECK MORPHOLOGY, NOT ONLY EXPLICIT NOUNS\n"
        "\n"
        "Flag invented_gender ONLY when:\n"
        "- the SOURCE leaves the person's gender unspecified;\n"
        "- adjacent SOURCE/context does not establish it;\n"
        "- and the Russian translation nevertheless selects male or female gender.\n"
        "\n"
        "Do not look only for explicit nouns such as \"man\", \"woman\", \"boy\", \"girl\".\n"
        "Also inspect Russian grammatical morphology that can silently encode gender:\n"
        "kinship terms, pronouns, adjectives, participles, predicatives, ordinal and\n"
        "relative forms, past-tense verbs, nouns such as \"vnuk/vnuchka\", \"syn/doch\".\n"
        "\n"
        "If English identifies a person with a gender-neutral expression such as\n"
        "\"a doctor\", \"a visitor\", \"a neighbor\", \"a relative\", verify that Russian has\n"
        "not silently selected male or female unless context establishes it.\n"
        "\n"
        "Russian grammatical gender is NOT an error when the character's gender is\n"
        "already established by SOURCE/context.\n"
        "\n"
        "\n"
        "6. NEGATION, TEMPORAL SCOPE, ASPECT, AND MODALITY\n"
        "\n"
        "Treat changes in logical proposition, temporal state, or certainty as semantic\n"
        "errors even when the Russian sentence sounds natural.\n"
        "\n"
        "Pay special attention to:\n"
        "already, still, yet, no longer, again, never, only, even,\n"
        "seemed, apparently, probably, certainly, must, might, could, would,\n"
        "thought, believed, appeared.\n"
        "\n"
        "These are NOT equivalent, for example:\n"
        "\n"
        "\"He still worked there.\"\n"
        "!=\n"
        "\"He no longer worked there.\"\n"
        "\n"
        "\"He was right.\"\n"
        "!=\n"
        "\"He seemed to be right.\"\n"
        "\n"
        "A change from asserted fact to perception, belief, appearance, possibility, or\n"
        "uncertainty is a semantic change and should be reported.\n"
        "\n"
        "\n"
        "7. CHARACTER STATE, MOTIVE, AND TRAIT\n"
        "\n"
        "A lexical substitution is NOT merely stylistic if it changes or introduces a\n"
        "distinct:\n"
        "- mental state;\n"
        "- emotion;\n"
        "- motive;\n"
        "- character trait;\n"
        "- intention;\n"
        "- degree of certainty.\n"
        "\n"
        "If SOURCE and TRANSLATION naturally describe meaningfully different mental\n"
        "states or traits, report the issue.\n"
        "\n"
        "For example:\n"
        "\"lost in thought\"\n"
        "is not automatically equivalent to\n"
        "\"self-absorbed\".\n"
        "\n"
        "Do not PASS merely because the words are loosely related.\n"
        "\n"
        "\n"
        "8. CONCRETE OBJECT IDENTITY AND PHYSICAL FACTS\n"
        "\n"
        "Object identity and physical properties are semantic facts.\n"
        "\n"
        "Report changed_fact when SOURCE and TRANSLATION materially differ in:\n"
        "- object type;\n"
        "- physical mechanism;\n"
        "- mobility/capability;\n"
        "- location;\n"
        "- quantity;\n"
        "- time;\n"
        "- physical relation.\n"
        "\n"
        "Examples of distinctions that can matter:\n"
        "\n"
        "van != truck\n"
        "carved != painted\n"
        "wagon != car\n"
        "\n"
        "If the difference changes what the object physically IS or CAN DO, do not\n"
        "dismiss it as stylistic or \"minor wording\".\n"
        "\n"
        "\n"
        "9. CHAPTER ENTITY FACTS\n"
        "\n"
        "CHAPTER ENTITY FACTS are source-derived facts about persistent entities in this\n"
        "chapter.\n"
        "\n"
        "Use them to resolve long-range references and terminology.\n"
        "\n"
        "If a later SOURCE uses a shorter or ambiguous reference, use the established\n"
        "chapter entity identity when auditing its translation.\n"
        "\n"
        "CHAPTER ENTITY FACTS are stronger than BOOK CONTEXT but weaker than explicit\n"
        "current/adjacent SOURCE.\n"
        "\n"
        "\n"
        "10. SHORT AND ELLIPTICAL DIALOGUE\n"
        "\n"
        "Recover omitted grammatical material from adjacent/context pairs before\n"
        "reporting an error.\n"
        "\n"
        "Example:\n"
        "\n"
        "SOURCE:\n"
        "\"How old is she?\"\n"
        "\"Sixteen.\"\n"
        "\n"
        "TRANSLATION:\n"
        "\"Skolko yey let?\"\n"
        "\"Shestnadtsati.\"\n"
        "\n"
        "PASS: the recovered Russian meaning is \"shestnadtsati let\".\n"
        "\n"
        "\n"
        "11. RUSSIAN ELLIPSIS\n"
        "\n"
        "Russian may omit a repeated noun when its referent remains unambiguous.\n"
        "\n"
        "Do not report omission when no semantic information is lost.\n"
        "\n"
        "Example:\n"
        "\n"
        "\"She put her hand on his\"\n"
        "->\n"
        "\"Ona polozhila ruku na yego [ruku]\"\n"
        "\n"
        "can be PASS.\n"
        "\n"
        "\n"
        "12. RUSSIAN MORPHOLOGICAL SYNCRETISM\n"
        "\n"
        "Do not infer case, number, or gender from a surface ending alone.\n"
        "\n"
        "Before reporting such an error, verify:\n"
        "- syntax;\n"
        "- governing preposition;\n"
        "- grammatical role;\n"
        "- surrounding words;\n"
        "- actual contextual meaning.\n"
        "\n"
        "\n"
        "13. GENERIC DESCRIPTIONS VS CANONICAL ENTITIES\n"
        "\n"
        "Do not automatically map generic descriptions such as:\n"
        "\"the attendant\", \"the man\", \"the woman\", \"the driver\", \"the dog\"\n"
        "to canonical book entities merely because their labels match.\n"
        "\n"
        "Require explicit SOURCE or CHAPTER ENTITY FACT evidence.\n"
        "\n"
        "\n"
        "14. DO NOT OVER-POLICE STYLE\n"
        "\n"
        "Do not report:\n"
        "- punctuation;\n"
        "- typography;\n"
        "- word order;\n"
        "- ordinary Russian restructuring;\n"
        "- stylistic roughness;\n"
        "- ordinary register variation;\n"
        "- harmless lexical variation;\n"
        "\n"
        "unless it changes an allowed semantic error class.\n"
        "\n"
        "\n"
        "15. CHECK ALL ERROR CLASSES FOR EACH PAIR\n"
        "\n"
        "Finding one candidate does NOT end the audit of that pair.\n"
        "\n"
        "Check all allowed classes before moving on.\n"
        "\n"
        "A single pair may contain multiple independent confirmed issues.\n"
        "If so, multiple issues with the same id are allowed.\n"
        "\n"
        "\n"
        "16. CONSERVATIVE DOES NOT MEAN IGNORE A PROVEN DIFFERENCE\n"
        "\n"
        "\"When uncertain, PASS\" applies when you genuinely cannot establish whether\n"
        "meaning changed.\n"
        "\n"
        "If your comparison establishes a concrete semantic difference, do not discard\n"
        "it merely because:\n"
        "- it seems small;\n"
        "- both Russian and English sentences are plausible;\n"
        "- the translation reads naturally;\n"
        "- the difference could be described as nuance.\n"
        "\n"
        "Report concrete semantic differences within the allowed categories.\n"
        "\n"
        "\n"
        "17. ISSUE LIMIT\n"
        "\n"
        "Return at most 20 confirmed issues.\n"
        "\n"
        "If more exist, return the 20 highest-confidence fidelity errors.\n"
        "\n"
        "\n"
        "18. PAIR IDS\n"
        "\n"
        "Copy exact ids.\n"
        "Never invent, count, or renumber ids.\n"
        "Never report CONTEXT_ONLY ids.\n"
        "\n"
        "\n"
        "OUTPUT\n"
        "\n"
        "Return STRICT JSON only.\n"
        "No markdown.\n"
        "No commentary.\n"
        "\n"
        "{\n"
        "  \"issues\": [\n"
        "    {\n"
        "      \"id\": \"p00045\",\n"
        "      \"category\": \"omission | addition | referent | invented_gender | changed_fact | negation\",\n"
        "      \"severity\": \"major | minor\",\n"
        "      \"confidence\": \"high | medium | low\",\n"
        "      \"note\": \"short description of confirmed semantic error\",\n"
        "      \"excerpt\": \"optional short fragment from the Russian translation\"\n"
        "    }\n"
        "  ]\n"
        "}\n"
        "\n"
        "If no confirmed issues exist:\n"
        "\n"
        "{\"issues\": []}"
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


# B2 (v4.1): selective repair batch with repair-as-verifier
# (``V4_1_AUDIT_B1_RU.md`` §5.2/§5.3, §10 B2; card t_73e190f7). One call per
# group of eligible findings (microbatches of 3-4 when eligible > 4, Cheng et
# al. explicit ``[index]`` identifiers). The repair model = the GENERATOR
# (Gemma local / DeepSeek remote) — Kocmi-safe (auditor ≠ repairer).
#
# Mandatory repair-as-verifier semantics: the audit issue is a candidate, not
# an established fact; the model first independently verifies against SOURCE
# and TRANSLATION, returns PASS (no change) when the auditor is wrong, and
# repairs only after confirming. Tier A findings (already confirmed by the
# B1.1 deterministic hard filters) are marked CONFIRMED and are repaired
# directly (no re-verification needed); Tier B findings (including entity
# relations) carry the verify-before-repair contract. The expected FP class
# (out-of-sample review 2026-08-10) is dialogue tags (said → позвала/буркнула/
# перебила) — a literary interpretation of a speech verb is NOT a fidelity
# defect, so the model must PASS those. Output schema: per-``[index]`` JSON
# results; every index must be answered exactly once (fail-closed).
REPAIR_AS_VERIFIER_V1 = ReviewerPrompt(
    role="selective_repair",
    version="pact-v4-repair-as-verifier/v2",
    instructions=(
        "You are a Russian-language repair editor for an English-to-Russian "
        "literary translation. You are given the SOURCE (PID -> English text) "
        "and TRANSLATION (PID -> Russian text) maps and a batch of FINDINGS "
        "reported by an automated auditor.\n"
        "\n"
        "The audit issue is a candidate, not an established fact. First "
        "independently verify against SOURCE and TRANSLATION. If incorrect -> "
        "return PASS, no change. Only repair after confirming.\n"
        "\n"
        "Each finding is labelled with a tier:\n"
        "  [CONFIRMED] already verified by a deterministic code check "
        "(numbers/times, exact duplicates, structure) — repair it directly, "
        "no re-verification needed.\n"
        "  [CANDIDATE] not code-verifiable — independently verify it yourself "
        "against SOURCE and TRANSLATION before repairing; return PASS if the "
        "auditor is wrong.\n"
        "\n"
        "Expected false-positive class: a literary interpretation of a speech "
        "verb (e.g. 'said' rendered as позвала/буркнула/перебила) is NOT a "
        "fidelity defect — return PASS for such findings.\n"
        "\n"
        "Repair rules: fix only the stated issue in the target PID and keep "
        "every other PID and the rest of the affected PID verbatim. Do not "
        "re-translate the whole chapter. Do not change names, numbers, ты/вы "
        "or adjacent text unless the finding requires it.\n"
        "\n"
        "CRITICAL: repaired_translation MUST be the FULL corrected text of "
        "the entire PID — every sentence of the paragraph, with ONLY the "
        "stated defect fixed inside it. Never return a fragment, a partial "
        "sentence, or a single corrected clause: the value is written back as "
        "the whole paragraph. If you are not sure, return decision 'pass' "
        "instead of a truncated repair.\n"
        "\n"
        "Return STRICT JSON, no markdown fences, no commentary, with exactly "
        "this schema:\n"
        "  results: array of objects, one per finding [index], each with:\n"
        "    index: integer (the [index] of the finding)\n"
        "    decision: 'pass' | 'repair'\n"
        "    pid: string (only for decision 'repair'; the target PID)\n"
        "    repaired_translation: string (only for decision 'repair'; the "
        "corrected Russian text of that PID)\n"
        "    reason: short string (one or two sentences)\n"
        "Every finding index must appear exactly once. Do not include any "
        "other keys."
    ),
)


# Phase 4A narrow re-gate (L2b, DECISIONS 2026-08-03). Unlike the full-chunk
# ``QWEN_FIDELITY_V1`` re-gate, the model is given ONLY the edited PID's
# source text, its repaired Russian text and the located region — a short
# JSON verdict per region. Unedited PIDs are covered by the convergence
# re-audit. The verdict schema matches ``QWEN_FIDELITY_V1`` (parsed via the
# same ``_parse_qwen_verdict``), so narrow verdicts are directly comparable
# to full-chunk verdicts on a fixture.
REGION_FIDELITY_GATE_V1 = ReviewerPrompt(
    role="region_fidelity_gate",
    version="pact-v4-reviewer-qwen-region-fidelity/v1",
    instructions=(
        "You are a strict fidelity reviewer for a single repaired region of "
        "a Russian translation of English fiction. You are given the SOURCE "
        "text of one PID and the REPAIRED translation of that same PID, "
        "located at a REGION span. Judge whether the repaired text preserves "
        "the meaning, register, negation scope, named entities, and numeric "
        "values of the source within this region. Return STRICT JSON, no "
        "markdown fences, no commentary, matching exactly this schema:\n"
        "  faithful_to_source: bool\n"
        "  completeness: bool\n"
        "  introduced_errors: bool\n"
        "  confidence: 'high' | 'medium' | 'low'\n"
        "  reason: short string (one or two sentences)\n"
        "  passed: bool (true iff the repaired region is acceptable)\n"
        "Do not include any other keys."
    ),
)

REGION_FIDELITY_GATE_BATCH_V1 = ReviewerPrompt(
    role="region_fidelity_gate_batch",
    version="pact-v4-reviewer-qwen-region-fidelity-batch/v1",
    instructions=(
        "You are a strict fidelity reviewer for several repaired regions of "
        "a Russian translation of English fiction. Each REGION entry gives "
        "the SOURCE text of one PID, the REPAIRED translation of that same "
        "PID, and the located REGION span. For every region judge whether "
        "the repaired text preserves the meaning, register, negation scope, "
        "named entities, and numeric values of the source within that "
        "region. Return STRICT JSON, no markdown fences, no commentary, "
        "with exactly this schema:\n"
        "  verdicts: array of objects, one per region in the given order, "
        "each with:\n"
        "    faithful_to_source: bool\n"
        "    completeness: bool\n"
        "    introduced_errors: bool\n"
        "    confidence: 'high' | 'medium' | 'low'\n"
        "    reason: short string (one or two sentences)\n"
        "    passed: bool (true iff the repaired region is acceptable)\n"
        "Do not include any other keys."
    ),
)


# ---------------------------------------------------------------------------
# Render helpers
# ---------------------------------------------------------------------------


# V4.2 R (card t_4707e6e5, R1): Russian-only editor stage. Qwen edits the
# RUSSIAN translation WITHOUT the English source, right after whole-chapter
# generation and BEFORE the audit (owner decision 2026-08-11; Qwen already
# resident — 0 restarts). Edits-only JSON contract v4.2-R1:
# ``{edits: [{pid, original, rewritten, reason, class}]}``. Each edit is
# tagged with exactly one class:
#   SAFE (auto-apply with a diff-gate rewritten != original):
#     typo | grammar | duplicate | preposition
#   REVIEW (written to edit_candidates.json, NEVER auto-applied; verified
#     later by the B2 repair-as-verifier against the ORIGINAL):
#     calque | logic | ambiguity | unnatural | register
# The chunked input (50 PIDs + CONTEXT_ONLY preceding pairs) mirrors the
# gemma_rewrite_v4.py test pattern — the model must never propose an edit
# for a CONTEXT_ONLY pid.
RUSSIAN_EDITOR_V4_2_R1 = ReviewerPrompt(
    role="russian_editor",
    # R-FIX2 (run_012, 2026-08-12): v3 — original is now a VERBATIM FRAGMENT
    # of the PID text (one sentence or a shorter span), not the whole PID.
    # The parse/apply contract mirrors this (substring validation +
    # substring-replace). Identity-bearing: bumping invalidates the R cache.
    version="pact-v4.2-russian-editor/v3",
    instructions=(
        "You are a Russian-language editor for a Russian literary "
        "translation. You are given the RUSSIAN text of a chapter as a map "
        "(PID -> Russian text). The English source is NOT provided — work "
        "only with the Russian text.\n"
        "\n"
        "Find only genuine defects in the Russian text and propose minimal "
        "edits. Classify each edit into exactly one class:\n"
        "\n"
        "SAFE classes (mechanical, safe to apply automatically):\n"
        "- typo: spelling or punctuation error\n"
        "- grammar: agreement, case, verb form, word order\n"
        "- duplicate: an accidentally repeated word or phrase\n"
        "- preposition: wrong, missing, or extra preposition\n"
        "\n"
        "REVIEW classes (need verification, do NOT apply automatically):\n"
        "- calque: a construction copied literally from English\n"
        "- logic: a logical inconsistency (pronoun/referent/number/tense)\n"
        "- ambiguity: a phrase that can be read in two ways\n"
        "- unnatural: an unidiomatic or awkward Russian phrasing\n"
        "- register: a stylistic register mismatch\n"
        "\n"
        "Rules:\n"
        "- Only edit the target PID; keep every other PID verbatim.\n"
        "- Fix only the stated defect; do not rewrite the whole paragraph.\n"
        "- original is the exact fragment you are fixing, quoted verbatim "
        "from the PID text (may be one sentence or a shorter span); it must "
        "appear in the PID text word-for-word.\n"
        "- rewritten must actually differ from original (no-op edits are "
        "invalid).\n"
        "- Do not change names, numbers, or already-correct text.\n"
        "- Never propose an edit for a CONTEXT_ONLY pid.\n"
        "\n"
        "Return STRICT JSON, no markdown fences, no commentary, with exactly "
        "this schema:\n"
        "  edits: array of objects, one per proposed edit, each with:\n"
        "    pid: string (the target PID)\n"
        "    original: string (the exact fragment you are fixing, quoted "
        "verbatim from the PID text — may be one sentence or a shorter span; "
        "it must appear in the PID text word-for-word)\n"
        "    rewritten: string (the corrected Russian text of that "
        "fragment)\n"
        "    reason: short string (one or two sentences)\n"
        "    class: one of typo|grammar|duplicate|preposition|calque|logic|"
        "ambiguity|unnatural|register\n"
        "\n"
        "Example of a valid response (every edit MUST include its class):\n"
        "{\"edits\": [{\"pid\": \"p00042\", \"original\": \"Он сказал что "
        "придёт позже.\", \"rewritten\": \"Он сказал, что придёт позже.\", "
        "\"reason\": \"пропущена запятая\", \"class\": \"typo\"}]}\n"
        "Do not include any other keys."
    ),
)


def render_russian_editor_prompt(
    *,
    chunk_id: str,
    edit_pairs: Sequence[Any],
    context_pairs: Sequence[Any] = (),
    chunk_index: int = 0,
    chunk_total: int = 1,
    template: ReviewerPrompt = RUSSIAN_EDITOR_V4_2_R1,
) -> str:
    """Render the v4.2-R1 Russian-editor request for one chunk.

    ``edit_pairs``/``context_pairs`` are sequences of objects exposing
    ``pid`` and ``text`` (``pact_v4.audit.russian_editor.TranslationPair``).
    Blocks in order:

    1. fixed instructions (``template.instructions``);
    2. ``CONTEXT_ONLY`` preceding pairs (model must NEVER propose an edit
       for them — the gemma_rewrite_v4.py pattern);
    3. ``EDIT_PAIRS (chunk X of Y):`` + the pairs to edit.

    Input is the RUSSIAN translation ONLY — no English source anywhere.
    """
    ctx_block = ""
    if context_pairs:
        rendered_ctx = "\n".join(
            f"  {p.pid}: {p.text}" for p in context_pairs
        )
        ctx_block = (
            "\n\nCONTEXT_ONLY (preceding Russian text; for continuity).\n"
            "NEVER propose an edit for a CONTEXT_ONLY pid.\n\n"
            f"{rendered_ctx}"
        )
    header = (
        f"EDIT_PAIRS (chunk {chunk_index} of {chunk_total}):"
        if chunk_total > 1 else "EDIT_PAIRS:"
    )
    rendered_edits = "\n".join(f"  {p.pid}: {p.text}" for p in edit_pairs)
    return (
        f"{template.instructions}"
        f"{ctx_block}\n\n"
        f"{header}\n{rendered_edits}"
    )


def render_qwen_review_prompt(
    *,
    source: dict[str, str],
    translation: dict[str, str],
    template: ReviewerPrompt = QWEN_FIDELITY_V1,
    bible_text: str = "",
) -> str:
    """Render the Qwen fidelity review request as a single user message."""
    src_lines = "\n".join(f"  {pid}: {text}" for pid, text in source.items())
    tr_lines = "\n".join(f"  {pid}: {text}" for pid, text in translation.items())
    bible_block = bible_text if bible_text else ""
    # V4 Efficiency A1.2 (provider cache): static blocks (instructions, the
    # full bible) come first as a common prefix across chunks; the dynamic
    # SOURCE/TRANSLATION blocks follow. Content unchanged — only the order.
    # A1.2 review fix (LOW): a valid non-empty ``bible_text`` may lack a
    # trailing newline; add an explicit delimiter so the SOURCE block never
    # glues onto the bible's last line (reproduced "...maleSOURCE ...").
    bible_sep = "\n" if bible_block and not bible_block.endswith("\n") else ""
    return (
        f"{template.instructions}\n\n"
        f"{bible_block}{bible_sep}"
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
    bible_text: str = "",
) -> str:
    """Render the Qwen Step 6 fidelity-audit request as one user message."""
    src_lines = "\n".join(f"  {pid}: {text}" for pid, text in source.items())
    tr_lines = "\n".join(f"  {pid}: {text}" for pid, text in translation.items())
    bible_block = bible_text if bible_text else ""
    # V4 Efficiency A1.2 (provider cache): static blocks (instructions, the
    # full bible) come first as a common prefix across chunks; the dynamic
    # CHUNK/SOURCE/TRANSLATION blocks follow. Content unchanged.
    # A1.2 review fix (LOW): a valid non-empty ``bible_text`` may lack a
    # trailing newline; add an explicit delimiter so the CHUNK block never
    # glues onto the bible's last line (reproduced "...maleCHUNK: ...").
    bible_sep = "\n" if bible_block and not bible_block.endswith("\n") else ""
    return (
        f"{template.instructions}\n\n"
        f"{bible_block}{bible_sep}"
        f"CHUNK: {chunk_id}\n\n"
        f"SOURCE (PID -> English text):\n{src_lines}\n\n"
        f"TRANSLATION (PID -> Russian text, same PIDs in the same order):\n{tr_lines}\n"
    )


def render_gemma_audit_prompt(
    *,
    chunk_id: str,
    translation: dict[str, str],
    template: ReviewerPrompt = GEMMA_AUDIT_V1,
    bible_text: str = "",
) -> str:
    """Render the Gemma Russian-only Step 6 review as one user message."""
    tr_lines = "\n".join(f"  {pid}: {text}" for pid, text in translation.items())
    bible_block = bible_text if bible_text else ""
    # V4 Efficiency A1.2 (provider cache): static blocks (instructions, the
    # full bible) come first as a common prefix across chunks; the dynamic
    # CHUNK/TRANSLATION blocks follow. Content unchanged.
    # A1.2 review fix (LOW): a valid non-empty ``bible_text`` may lack a
    # trailing newline; add an explicit delimiter so the CHUNK block never
    # glues onto the bible's last line (reproduced "...maleCHUNK: ...").
    bible_sep = "\n" if bible_block and not bible_block.endswith("\n") else ""
    return (
        f"{template.instructions}\n\n"
        f"{bible_block}{bible_sep}"
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


def render_region_fidelity_gate_prompt(
    *,
    source_text: str,
    repaired_text: str,
    region: Any,
    template: ReviewerPrompt = REGION_FIDELITY_GATE_V1,
) -> str:
    """Render the L2b narrow Qwen re-gate request as one user message.

    Only the edited PID's source text, its repaired Russian text and the
    located ``region`` (``pid``/``start``/``end``) are rendered — unedited
    PIDs are covered by the convergence re-audit, never shown here.
    """
    region_line = f"pid={region.pid} span=[{region.start}, {region.end})"
    return (
        f"{template.instructions}\n\n"
        f"SOURCE (PID -> English text):\n  {region.pid}: {source_text}\n\n"
        f"REPAIRED TRANSLATION (same PID):\n  {region.pid}: {repaired_text}\n\n"
        f"REGION (the located repaired span):\n  {region_line}\n"
    )


def render_region_fidelity_gate_batch_prompt(
    *,
    items: list[dict[str, Any]],
    template: ReviewerPrompt = REGION_FIDELITY_GATE_BATCH_V1,
) -> str:
    """Render a multi-region L2b narrow Qwen re-gate request (B12 batching).

    ``items`` is a list of per-region payloads (``source_text`` /
    ``repaired_text`` / ``region``); each region is rendered as its own flat
    ``REGION <index>:`` block with the same SOURCE / REPAIRED TRANSLATION /
    REGION fields the single-region renderer uses, so the model returns one
    verdict object per region in order.
    """
    blocks = []
    for index, item in enumerate(items, start=1):
        region = item["region"]
        region_line = f"pid={region.pid} span=[{region.start}, {region.end})"
        blocks.append(
            f"REGION {index}:\n"
            f"SOURCE (PID -> English text):\n  {region.pid}: {item['source_text']}\n\n"
            f"REPAIRED TRANSLATION (same PID):\n  {region.pid}: {item['repaired_text']}\n\n"
            f"REGION (the located repaired span):\n  {region_line}\n"
        )
    return f"{template.instructions}\n\n" + "\n\n".join(blocks)


def _render_pair_block(pid: str, source: str, translation: str) -> str:
    """One ``<PAIR>`` block in the v4.1 chunked-audit format (harness)."""
    return (
        f"<PAIR id=\"{pid}\">\n"
        f"<SOURCE>{source}</SOURCE>\n"
        f"<TRANSLATION>{translation}</TRANSLATION>\n"
        f"</PAIR>"
    )


def render_chunked_audit_prompt(
    *,
    chunk_id: str,
    audit_pairs: Sequence[Any],
    context_pairs: Sequence[Any] = (),
    narrator_context: str = "",
    entity_context: str = "",
    chunk_index: int = 0,
    chunk_total: int = 1,
    template: ReviewerPrompt = QWEN_AUDIT_V4_1,
) -> str:
    """Render the v4.1 chunked Qwen audit request (B1, port of
    ``audit_v4.ps1``'s ``Build-ChunkPrompt``).

    ``audit_pairs``/``context_pairs`` are sequences of objects exposing
    ``pid``/``source``/``translation`` attributes (``pact_v4.audit.
    chunked_audit.AuditPair``). Blocks are appended in the harness order:

    1. fixed instructions (``template.instructions``);
    2. ``BOOK CONTEXT - FALLBACK ONLY`` when ``narrator_context`` is non-empty;
    3. ``CHAPTER ENTITY FACTS - SOURCE-DERIVED`` when ``entity_context`` is
       non-empty;
    4. ``CONTEXT_ONLY`` pairs (model must never report issues for them);
    5. ``AUDIT_PAIRS (chunk X of Y):`` + the audited pairs.

    Static blocks (instructions + narrator + entity) come first so the common
    prefix is stable across chunks (V4 Efficiency A1.2 provider-cache
    principle); the dynamic CONTEXT_ONLY/AUDIT_PAIRS follow.
    """
    ctx_block = ""
    if narrator_context.strip():
        ctx_block = (
            "\n\nBOOK CONTEXT - FALLBACK ONLY\n\n"
            f"{narrator_context}"
        )
    ent_block = ""
    if entity_context.strip():
        ent_block = (
            "\n\nCHAPTER ENTITY FACTS - SOURCE-DERIVED\n\n"
            f"{entity_context}"
        )
    ctx_pairs_block = ""
    if context_pairs:
        rendered_ctx = "\n".join(
            _render_pair_block(p.pid, p.source, p.translation) for p in context_pairs
        )
        ctx_pairs_block = (
            "\n\nCONTEXT_ONLY (for resolving speakers, referents, ellipsis, "
            "continuity).\nNEVER report an issue for a CONTEXT_ONLY pair.\n\n"
            f"{rendered_ctx}"
        )
    header = (
        f"AUDIT_PAIRS (chunk {chunk_index} of {chunk_total}):"
        if chunk_total > 1 else "AUDIT_PAIRS:"
    )
    rendered_audit = "\n".join(
        _render_pair_block(p.pid, p.source, p.translation) for p in audit_pairs
    )
    return (
        f"{template.instructions}"
        f"{ctx_block}{ent_block}{ctx_pairs_block}\n\n"
        f"{header}\n{rendered_audit}"
    )


# REPAIR-CTX (card t_97b31f81, owner decision 2026-08-12): local context for
# repair batches. A repair batch used to carry the FULL chapter SOURCE +
# TRANSLATION maps (~33.7k tokens for 400 PIDs, run_012) for 3-4 findings
# (findings were 1.5% of the prompt; prompt eval 93-114s). The fix renders
# ONLY the repairable PIDs (findings) plus their ±N neighbour pairs
# (CONTEXT_ONLY — the model may use them for referents/continuity but must
# NEVER edit them). Owner: окрестность = ±3 PID (3 назад + 3 вперёд).
DEFAULT_REPAIR_CONTEXT_WINDOW = 3


def render_selective_repair_prompt(
    *,
    chapter_id: str,
    source: dict[str, str],
    translation: dict[str, str],
    findings: Sequence[Any],
    template: ReviewerPrompt = REPAIR_AS_VERIFIER_V1,
    repair_context_window: int = DEFAULT_REPAIR_CONTEXT_WINDOW,
) -> str:
    """Render a B2 selective-repair batch request as one user message.

    ``findings`` is a sequence of objects exposing ``index`` (explicit
    ``[index]`` identifier), ``pid``, ``tier`` (``"A"`` for CONFIRMED /
    ``"B"`` for CANDIDATE), ``category``, ``severity``, ``confidence``,
    ``note``, ``excerpt`` (``pact_v4.repair.selective_repair.EligibleFinding``
    or any object with those attributes).

    REPAIR-CTX (t_97b31f81): the batch prompt carries ONLY the repairable
    PIDs (the findings) plus their ±``repair_context_window`` neighbourhood
    in source order (CONTEXT_ONLY), NOT the full chapter maps — run_012
    sent 33.7k tokens for 3-4 findings, of which the findings were 1.5%.
    The CONTEXT_ONLY block names the neighbour PIDs so the model can resolve
    speakers/referents/ellipsis/continuity but must NEVER propose an edit
    for them. Fail-loud: a finding PID missing from the maps raises
    ``ValueError`` — the model cannot repair a PID it cannot see.

    The model answers per ``[index]`` (repair-as-verifier, §5.2/§10 B2).
    """
    # Fail-loud: the model cannot repair a PID it cannot see.
    missing = [
        f.pid for f in findings
        if f.pid not in source or f.pid not in translation
    ]
    if missing:
        raise ValueError(
            "repair finding PID(s) not present in source/translation maps: "
            f"{sorted(set(missing))} — the model cannot repair what it cannot see"
        )
    order = list(source.keys())
    positions = {pid: i for i, pid in enumerate(order)}
    scope: set = set()
    for pid in {f.pid for f in findings}:
        scope.add(pid)
        i = positions[pid]
        for j in range(
            max(0, i - repair_context_window),
            min(len(order), i + repair_context_window + 1),
        ):
            scope.add(order[j])
    # Local window in source order; context-only = in-window, non-finding PIDs.
    local_pids = [pid for pid in order if pid in scope]
    finding_pids = {f.pid for f in findings}
    context_pids = [pid for pid in local_pids if pid not in finding_pids]
    src_lines = "\n".join(f"  {pid}: {source[pid]}" for pid in local_pids)
    tr_lines = "\n".join(f"  {pid}: {translation[pid]}" for pid in local_pids)
    ctx_block = ""
    if context_pids:
        ctx_block = (
            "\n\nCONTEXT_ONLY (neighbour PIDs in the maps above: "
            f"{', '.join(context_pids)} — for resolving speakers, referents, "
            "ellipsis, continuity).\nNEVER propose an edit for a CONTEXT_ONLY pid."
        )
    # The instructions label findings [CONFIRMED] (Tier A) / [CANDIDATE]
    # (Tier B); the FINDINGS block uses the same labels so the model can
    # apply the right per-finding contract.
    finding_lines = "\n".join(
        f"  [{f.index}] {f.pid} | {'CONFIRMED' if f.tier == 'A' else 'CANDIDATE'} | "
        f"{f.category} | "
        f"severity={f.severity} | confidence={f.confidence}\n"
        f"      note: {f.note}\n"
        f"      excerpt: {f.excerpt or '(none)'}"
        for f in findings
    ) or "  (none)"
    return (
        f"{template.instructions}\n\n"
        f"CHUNK: {chapter_id}\n\n"
        f"SOURCE (PID -> English text):\n{src_lines}\n\n"
        f"TRANSLATION (PID -> Russian text, same PIDs in the same order):\n{tr_lines}\n"
        f"{ctx_block}\n\n"
        f"FINDINGS (verify, then repair only confirmed issues):\n{finding_lines}\n"
    )


def render_reaudit_prompt(
    *,
    chapter_id: str,
    audit_pairs: Sequence[Any],
    context_pairs: Sequence[Any] = (),
    repaired_changes: Sequence[Any] = (),
    narrator_context: str = "",
    entity_context: str = "",
    chunk_index: int = 1,
    chunk_total: int = 1,
    template: ReviewerPrompt = QWEN_AUDIT_V4_1,
) -> str:
    """Render one re-audit chunk request (REPAIR-CTX, t_97b31f81).

    REPAIR-CTX (owner decision 2026-08-12): the post-repair re-audit is a
    CHUNKED audit over the affected region (changed PIDs + neighbour window),
    reusing the audit's chunking/overlap mechanisms — NEVER the whole chapter
    (run_012 re-audit input was 41.5k tokens and truncated the 49k context).
    ``audit_pairs`` is ONE chunk of the region (reportable); ``context_pairs``
    is the chunk's preceding CONTEXT_ONLY overlap (the audit's
    ``get_overlap_context`` mechanism — the model must NEVER report an issue
    for them); ``repaired_changes`` carries the repair delta ``{pid, before,
    after}`` so the auditor verifies the CORRECTNESS of each repair instead
    of just re-reading the text. The caller's JSON validation rejects any
    issue id outside the chunk (fail-closed scope, like the audit).
    Reuses the frozen v4.1 audit template and block layout.
    """
    ctx_block = ""
    if narrator_context.strip():
        ctx_block = (
            "\n\nBOOK CONTEXT - FALLBACK ONLY\n\n"
            f"{narrator_context}"
        )
    ent_block = ""
    if entity_context.strip():
        ent_block = (
            "\n\nCHAPTER ENTITY FACTS - SOURCE-DERIVED\n\n"
            f"{entity_context}"
        )
    delta_block = ""
    if repaired_changes:
        rendered_delta = "\n".join(
            f"  {c.pid}:\n"
            f"    before: {c.before}\n"
            f"    after: {c.after}"
            for c in repaired_changes
        )
        delta_block = (
            "\n\nREPAIRED CHANGES (the repair editor changed these PIDs after "
            "the last audit; verify each repair is CORRECT — the after text "
            "must fix the reported issue without introducing a new defect):\n"
            f"{rendered_delta}"
        )
    ctx_pairs_block = ""
    if context_pairs:
        rendered_ctx = "\n".join(
            _render_pair_block(p.pid, p.source, p.translation) for p in context_pairs
        )
        ctx_pairs_block = (
            "\n\nCONTEXT_ONLY (preceding overlap pairs for resolving "
            "speakers, referents, ellipsis, continuity, and "
            "cross-references).\nNEVER report an issue for a CONTEXT_ONLY pair.\n\n"
            f"{rendered_ctx}"
        )
    rendered_audit = "\n".join(
        _render_pair_block(p.pid, p.source, p.translation) for p in audit_pairs
    )
    header = (
        f"RE-AUDIT PAIRS (chunk {chunk_index} of {chunk_total}, "
        "changed PIDs + neighbours):"
        if chunk_total > 1 else "RE-AUDIT PAIRS (changed PIDs + neighbours):"
    )
    return (
        f"{template.instructions}"
        f"{ctx_block}{ent_block}{delta_block}{ctx_pairs_block}\n\n"
        f"{header}\n{rendered_audit}"
    )
