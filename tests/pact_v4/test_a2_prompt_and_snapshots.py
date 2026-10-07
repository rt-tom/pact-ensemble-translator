"""V4.1 A2 contract tests: prompt v4 render + JSON contract, Gemma server
args per §3.4, whole-chapter glossary full-chapter filter, and the
translations_repaired / translation_diffs snapshots (§7).
"""
from __future__ import annotations

import json

from pact_full_pipeline_runner_v1 import v4_phase12_strict_run as cli
from pact_v4.phase2.prompts import BALANCED_LITERARY_V4, FIDELITY_FIRST_V1, render_prompt
from pact_v4.phase2.risk import GlossaryEntry

# ---------------------------------------------------------------------------
# Prompt v4: full §4 text + JSON contract
# ---------------------------------------------------------------------------


def test_balanced_literary_v4_version_and_contract():
    assert BALANCED_LITERARY_V4.version == "pact-v4-prompt-balanced-literary/v7"
    assert FIDELITY_FIRST_V1.version == "pact-v4-prompt-fidelity-first/v3"
    for template in (BALANCED_LITERARY_V4, FIDELITY_FIRST_V1):
        instructions = template.instructions
        # §4 core instructions present.
        if template.role == "balanced_literary":
            assert "Translate by EFFECT" in instructions
            assert "soften or intensify it" in instructions
            assert "Avoid calques" in instructions
            assert "Preserve exact details" in instructions
            # DIALOGUE-TYPOGRAPHY (t_41da17ec)
            assert "RUSSIAN DIALOGUE TYPOGRAPHY" in instructions
            assert "MUST begin with an em dash (—)" in instructions
            assert "MUST NOT be enclosed in «quotation marks»" in instructions
            assert "never for ordinary dialogue paragraphs" in instructions
            assert instructions.index("RUSSIAN DIALOGUE TYPOGRAPHY") > instructions.index(
                "Avoid calques"
            )
            assert instructions.index("Preserve exact details") > instructions.index(
                "RUSSIAN DIALOGUE TYPOGRAPHY"
            )
            assert "{book_context}" in instructions
            assert "{glossary_entries}" in instructions
        # JSON object contract — unambiguous, never an array.
        assert "exactly one top-level JSON object" in instructions
        assert "and nothing else" in instructions
        assert "Do not return an array" in instructions
        # Wrapper shapes explicitly forbidden.
        assert '"translations"' in instructions or "translations" in instructions
        # At least one wrapper key is named explicitly (case-insensitive: the
        # balanced literary tail continues after a comma with lowercase "do").
        assert "do not wrap the object in any outer key" in instructions.lower()
        assert "pid/text" in instructions or '"pid"' in instructions
        assert "do not nest the translations inside another object or array" in instructions.lower()
        # Every value is a Russian string, keys are exactly OWNED_SOURCE.
        assert ("Every top-level value must be a Russian string" in instructions
                or "Every top-level value must be a Russian-language string" in instructions)
        assert "no missing keys, no extra keys, no duplicate keys" in instructions
        assert "no keys outside OWNED_SOURCE" in instructions
        assert "in exactly the same order as OWNED_SOURCE" in instructions
        # Markdown fences and commentary forbidden.
        assert "Do not wrap the JSON in markdown fences" in instructions
        assert "do not add commentary" in instructions
        # OWNED_SOURCE is input, not output format.
        assert "OWNED_SOURCE is an input list/map" in instructions
        assert "it is not an output format" in instructions
        # Illustrative example is present but marked non-copyable.
        assert "Shape example (illustrative only" in instructions
        assert "do not output these placeholder" in instructions
        # HTML/markup is forbidden inside JSON string values, while JSON remains
        # mandatory for the outer response.
        if template.role == "balanced_literary":
            assert "Do not output any HTML or markup inside" in instructions
            assert "markup" in instructions
            assert "The complete response must be valid JSON" in instructions
            assert "plain Russian text only" not in instructions
            assert "TRANSLATION VALUE RULE" in instructions
            assert "OUTPUT CONTRACT — MANDATORY AND AUTHORITATIVE" in instructions
        # Legacy STRICT marker may still appear via ownership guard wording, but
        # the authoritative contract is the explicit object rule above.
        assert "OWNED_SOURCE" in instructions


def _bundle(*, glossary=(), bible_text="bible", reasoning=0):
    from pact_v4.phase2.generation import GenerationParams, PromptBundle

    return PromptBundle(
        template=BALANCED_LITERARY_V4,
        role="balanced_literary",
        risk_band="low",
        risk_policy_version="pact-v4-risk-source-en/v1",
        required_risk_feature_codes=(),
        snapshot_hash="s" * 64,
        source_hash="h" * 64,
        chunk_id="whole_chapter",
        owned_pids=("p1", "p2"),
        owned_source=(("p1", "Hello."), ("p2", "World.")),
        left_context=(),
        right_context=(),
        glossary=glossary,
        style_constraints=(),
        bible_text=bible_text,
        config_identity="cfg",
        params=GenerationParams(
            temperature=0.2, seed=1, max_tokens=1000, reasoning=reasoning,
        ),
    )


def test_render_prompt_v4_substitutes_book_context_and_glossary():
    glossary = (("Blake", ("Блэйк",)),)
    rendered = render_prompt(_bundle(glossary=glossary, bible_text="BIBLE:\n  - Narrator: male"))
    assert "BOOK CONTEXT (locked, authoritative — do not contradict):" in rendered
    assert "BIBLE:" in rendered
    assert "Narrator: male" in rendered
    assert "LOCKED GLOSSARY (canonical lexical choices and spellings):" in rendered
    assert "Blake -> Блэйк" in rendered
    # The inline tokens were consumed; no duplicate trailing GLOSSARY block.
    assert "{book_context}" not in rendered
    assert "{glossary_entries}" not in rendered
    # The JSON contract survives into the rendered prompt.
    assert "exactly one top-level JSON object" in rendered
    assert "Do not return an array" in rendered
    assert "Do not wrap the JSON in markdown fences" in rendered


def test_render_prompt_v4_legacy_template_unaffected():
    # A template without the inline tokens keeps the append-only layout.
    from dataclasses import replace

    from pact_v4.phase2.prompts import PromptTemplate

    legacy = PromptTemplate(
        role="fidelity_first", version="x/v1",
        instructions="You are a translator. Translate exactly.",
    )
    bundle = replace(_bundle(), template=legacy, role="fidelity_first")
    rendered = render_prompt(bundle)
    assert "GLOSSARY:" in rendered
    assert "{book_context}" not in rendered


def test_render_prompt_contract_object_not_array_and_forbids_wrappers():
    """Rendered generation prompt unambiguously specifies one JSON object and
    explicitly rejects arrays and wrapper forms; OWNED_SOURCE input clarification
    and illustrative example are present without altering PID ownership."""
    rendered = render_prompt(_bundle())
    assert "exactly one top-level JSON object and nothing else" in rendered
    assert "Do not return an array" in rendered
    assert "do not wrap the object in any outer key" in rendered.lower()
    assert '"translations"' in rendered or "translations" in rendered
    assert '"items"' in rendered or "items" in rendered
    assert '"paragraphs"' in rendered or "paragraphs" in rendered
    assert "pid/text" in rendered or '"pid"' in rendered
    assert "do not nest the translations inside another object or array" in rendered.lower()
    assert ("Every top-level value must be a Russian string" in rendered
            or "Every top-level value must be a Russian-language string" in rendered)
    assert "Keys must be exactly the PIDs" in rendered or "keys must be exactly the PIDs" in rendered.lower() or "no missing keys" in rendered
    assert "in exactly the same order as OWNED_SOURCE" in rendered
    assert "Do not wrap the JSON in markdown fences" in rendered
    assert "OWNED_SOURCE is an input list/map" in rendered
    assert "Shape example (illustrative only" in rendered
    assert "do not output these placeholder PIDs" in rendered
    # Ownership still intact: OWNED_SOURCE is the source of truth.
    assert "OWNED_SOURCE" in rendered
    assert "left_context (read-only" in rendered
    assert "right_context (read-only" in rendered


def test_prompt_cache_identity_changes_with_version_and_instructions():
    """Prompt version/instructions are part of PromptBundle identity: bumping
    either changes bundle_hash so old caches cannot be silently reused."""
    from pact_v4.phase2.generation import GenerationParams, PromptBundle
    from pact_v4.phase2.prompts import PromptTemplate

    common = dict(
        role="fidelity_first",
        risk_band="low",
        risk_policy_version="pact-v4-risk-source-en/v1",
        required_risk_feature_codes=(),
        snapshot_hash="s" * 64,
        source_hash="h" * 64,
        chunk_id="c1",
        owned_pids=("p1", "p2"),
        owned_source=(("p1", "Hello."), ("p2", "World.")),
        left_context=(),
        right_context=(),
        glossary=(),
        style_constraints=(),
        bible_text="",
        config_identity="cfg",
        params=GenerationParams(temperature=0.2, seed=1, max_tokens=1000, reasoning=0),
    )
    template_v2 = PromptTemplate(role="fidelity_first", version="pact-v4-prompt-fidelity-first/v2", instructions="Do X. exactly one top-level JSON object")
    template_v3 = PromptTemplate(role="fidelity_first", version="pact-v4-prompt-fidelity-first/v3", instructions="Do X. exactly one top-level JSON object")
    template_v3_reworded = PromptTemplate(role="fidelity_first", version="pact-v4-prompt-fidelity-first/v3", instructions="Do Y. exactly one top-level JSON object")
    bundle_v2 = PromptBundle(template=template_v2, **common)
    bundle_v3 = PromptBundle(template=template_v3, **common)
    bundle_reworded = PromptBundle(template=template_v3_reworded, **common)
    assert bundle_v2.bundle_hash != bundle_v3.bundle_hash
    assert bundle_v3.bundle_hash != bundle_reworded.bundle_hash
    # Real templates also participate: their current versions are v3/v7.
    assert FIDELITY_FIRST_V1.version == "pact-v4-prompt-fidelity-first/v3"
    assert BALANCED_LITERARY_V4.version == "pact-v4-prompt-balanced-literary/v7"

# ---------------------------------------------------------------------------
# Gemma server args per §3.4
# ---------------------------------------------------------------------------


def test_gemma_server_args_match_plan_34():
    args = cli.GEMMA_SERVER_ARGS
    assert args == [
        "-ngl", "all",
        "-ncmoe", "18",
        "--load-mode", "mmap",
        "--reasoning-budget", "2048",
        "-np", "1",
        "-c", str(cli.GEMMA_CONTEXT_SIZE),
        "-fa", "on",
        "--jinja",
        "-ctk", "q8_0",
        "-ctv", "q4_0",
        "--cache-ram", "0",
        "--ctx-checkpoints", "0",
    ]
    # §3.4: MTP draft is OFF in v4.1; context is 49k.
    assert "--model-draft" not in args
    assert cli.GEMMA_CONTEXT_SIZE == 49152
    # F3 (B3 review): the default local Qwen AUDIT server profile is the B3
    # contract — MTP draft, reasoning 8192, context 49152 — so the Qwen
    # context is no longer the historical 32768 (the constant is kept for
    # backward-compat, the server args carry the B3 profile).
    assert cli.CONTEXT_SIZE == 32768
    assert cli.QWEN_SERVER_ARGS[cli.QWEN_SERVER_ARGS.index("-c") + 1] == "49152"

# ---------------------------------------------------------------------------
# Whole-chapter glossary full-chapter filter (§5.3)
# ---------------------------------------------------------------------------


def test_whole_chapter_glossary_filter_uses_full_chapter_text(tmp_path):
    from pact_v4.pipeline._shared_runner_helpers import _glossary_entries_for_chunk
    from pact_v4.phase2.risk import GlossaryEntry

    glossary = (
        GlossaryEntry(source_term="Blake", target_terms=("Блэйк",)),
        GlossaryEntry(source_term="Paige", target_terms=("Пэйдж",)),
        GlossaryEntry(source_term="hundred", target_terms=("сто",)),
    )
    chapter_text = "Blake walked past the hundred-year-old oak."
    kept, dropped = _glossary_entries_for_chunk(
        glossary,
        chunk_text=chapter_text,
        risk_feature_codes=(),
        narrator_gender=None,
        narrator_source_terms=(),
    )
    kept_terms = {entry.source_term for entry in kept}
    assert "Blake" in kept_terms
    assert "hundred" in kept_terms  # number_word category -> always_include
    assert "Paige" not in kept_terms
    assert "Paige" in dropped

# ---------------------------------------------------------------------------
# translator-line-output: whole-chapter line template + scoped rendering
# ---------------------------------------------------------------------------


def test_whole_chapter_line_template_carries_verbatim_contract():
    from pact_v4.phase2.prompts import BALANCED_LITERARY_WHOLE_CHAPTER_LINE_V1

    line = BALANCED_LITERARY_WHOLE_CHAPTER_LINE_V1
    assert line.role == "balanced_literary"
    assert line.version == "pact-v4-prompt-balanced-literary-wc-line/v1"
    assert line.version != BALANCED_LITERARY_V4.version
    # The approved verbatim block is present exactly (TARGET wording kept).
    verbatim = (
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
    assert verbatim in line.instructions
    # The retired JSON contract block is gone: no second contract remains.
    assert "OUTPUT CONTRACT — MANDATORY AND AUTHORITATIVE" not in line.instructions
    assert "exactly one top-level JSON object" not in line.instructions
    assert "The complete response must be valid JSON" not in line.instructions
    # Everything before the contract is byte-identical to the shared template.
    base = BALANCED_LITERARY_V4.instructions
    marker = "OUTPUT CONTRACT — MANDATORY AND AUTHORITATIVE:"
    assert line.instructions.startswith(base[: base.index(marker)])


def test_whole_chapter_line_template_leaves_shared_template_untouched():
    # Chunked JSON generation keeps its exact contract: the shared template
    # still mandates one JSON object and knows nothing about line output.
    instructions = BALANCED_LITERARY_V4.instructions
    assert "OUTPUT CONTRACT — MANDATORY AND AUTHORITATIVE" in instructions
    assert "exactly one top-level JSON object" in instructions
    assert "PID: translation" not in instructions


def _wc_bundle():
    import dataclasses

    from pact_v4.phase2.prompts import BALANCED_LITERARY_WHOLE_CHAPTER_LINE_V1

    return dataclasses.replace(
        _bundle(), template=BALANCED_LITERARY_WHOLE_CHAPTER_LINE_V1
    )


def test_render_prompt_whole_chapter_labels_target_and_line_checks():
    rendered = render_prompt(_wc_bundle())
    # Architect decision: the source block carries the TARGET alias so the
    # verbatim contract's TARGET boundary is unambiguous.
    assert "TARGET (OWNED_SOURCE" in rendered
    assert "TARGET PID" in rendered
    # Line-protocol output checks, no JSON mandate.
    assert "`PID: translation` line per TARGET PID" in rendered
    assert "exactly one valid JSON object" not in rendered
    assert "Before emitting the translation," in rendered
    # All fidelity/risk checks retained verbatim.
    assert "locked glossary spelling/transliteration" in rendered
    assert "narrator/character gender, speakers, and referents" in rendered
    assert "one source PID = one output PID" in rendered
    assert "no missing, extra, duplicate, split, merged, or reordered PIDs" in rendered


def test_render_prompt_chunked_mode_keeps_json_contract_byte_identical():
    # Non-whole-chapter bundles render exactly as before: original header +
    # JSON final check, so the chunked path is contract-compatible.
    import dataclasses

    rendered = render_prompt(dataclasses.replace(_bundle(), chunk_id="chunk0001"))
    assert "OWNED_SOURCE (translate exactly these PIDs, in this order):" in rendered
    assert "TARGET (OWNED_SOURCE" not in rendered
    assert "exactly one valid JSON object and nothing else" in rendered
    assert "Before emitting the JSON," in rendered
    assert "`PID: translation` line per TARGET PID" not in rendered
