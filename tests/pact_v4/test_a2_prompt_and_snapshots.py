"""V4.1 A2 contract tests: prompt v4 render + JSON contract, Gemma server
args per §3.4, whole-chapter glossary full-chapter filter, and the
translations_repaired / translation_diffs snapshots (§7).
"""
from __future__ import annotations

import json

from pact_full_pipeline_runner_v1 import v4_phase12_strict_run as cli
from pact_v4.phase2.prompts import BALANCED_LITERARY_V4, render_prompt
from pact_v4.phase2.risk import GlossaryEntry

# ---------------------------------------------------------------------------
# Prompt v4: full §4 text + JSON contract
# ---------------------------------------------------------------------------


def test_balanced_literary_v4_version_and_contract():
    assert BALANCED_LITERARY_V4.version == "pact-v4-prompt-balanced-literary/v4"
    instructions = BALANCED_LITERARY_V4.instructions
    # §4 core instructions present.
    assert "Translate by EFFECT" in instructions
    assert "soften or intensify it" in instructions
    assert "Avoid calques" in instructions
    assert "Preserve exact details" in instructions
    # DIALOGUE-TYPOGRAPHY (t_41da17ec): the Russian literary dialogue
    # typography rule is part of the v4 instructions — a spoken replica
    # paragraph must open with an em dash, never with guillemets; «...» is
    # reserved for actual quotations/words/titles/nested speech.
    assert "RUSSIAN DIALOGUE TYPOGRAPHY" in instructions
    assert "MUST begin with an em dash (—)" in instructions
    assert "MUST NOT be enclosed in «quotation marks»" in instructions
    assert "never for ordinary dialogue paragraphs" in instructions
    # The block sits between the calque paragraph and "Preserve exact details".
    assert instructions.index("RUSSIAN DIALOGUE TYPOGRAPHY") > instructions.index(
        "Avoid calques"
    )
    assert instructions.index("Preserve exact details") > instructions.index(
        "RUSSIAN DIALOGUE TYPOGRAPHY"
    )
    # Inline BOOK CONTEXT / LOCKED GLOSSARY tokens (render_prompt fills them).
    assert "{book_context}" in instructions
    assert "{glossary_entries}" in instructions
    # Strict JSON contract, no HTML/markup.
    assert "Return STRICT JSON" in instructions
    assert "no missing keys, no extra keys, no duplicate keys" in instructions
    assert "Do not output any HTML or" in instructions
    assert "markup" in instructions
    assert "in markdown fences or add commentary" in instructions


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
    assert "LOCKED GLOSSARY (use these translations consistently, do not vary):" in rendered
    assert "Blake -> Блэйк" in rendered
    # The inline tokens were consumed; no duplicate trailing GLOSSARY block.
    assert "{book_context}" not in rendered
    assert "{glossary_entries}" not in rendered
    # The JSON contract survives into the rendered prompt.
    assert "Return STRICT JSON" in rendered
    assert "in markdown fences or add commentary" in rendered


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


# ---------------------------------------------------------------------------
# Gemma server args per §3.4
# ---------------------------------------------------------------------------


def test_gemma_server_args_match_plan_34():
    args = cli.GEMMA_SERVER_ARGS
    assert args == [
        "-ngl", "99",
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
# Snapshots: translations_repaired.json + translation_diffs.json (§7)
#
# Covered in tests/pact_v4/pipeline/test_v4_phase12_strict_runner_whole_chapter.py
# (test_whole_chapter_writes_snapshots_with_identity_and_empty_diffs), where
# the whole-chapter runner fixtures live.
# ---------------------------------------------------------------------------
