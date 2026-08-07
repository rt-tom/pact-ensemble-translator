"""V4 Efficiency A1.2 — prompt prefix ordering (provider cache) tests.

Static blocks (template instructions + full bible + style/policy constants)
must sit at the START of the generation / Qwen fidelity / Qwen audit /
Gemma audit prompts, with the dynamic blocks (CHUNK_ID, source/translation,
glossary, context) after them — so different chunks of one run share a
common static prefix (provider-side ``cached_input_tokens``). Content is
unchanged: the same set of blocks is rendered, only the order moves.
"""
from __future__ import annotations

from pact_v4.phase2.prompts import render_prompt
from pact_v4.runtime.prompts_runtime import (
    render_gemma_audit_prompt,
    render_qwen_audit_prompt,
    render_qwen_review_prompt,
)

INSTRUCTIONS = "You are a translator. Translate exactly."
BIBLE = "BIBLE:\n  - Narrator: male\n"
STYLE = (("narrator_voice", "formal"),)


class _FakeTemplate:
    role = "fidelity_first"
    version = "pact-v4-prompt-fidelity-first/v2"
    instructions = INSTRUCTIONS


class _FakeBundle:
    def __init__(
        self,
        *,
        chunk_id: str = "chunk0001",
        owned_source=(("p1", "Hello"), ("p2", "World")),
        left_context=(),
        right_context=(),
        glossary=(),
        style_constraints=STYLE,
        bible_text=BIBLE,
        required_risk_feature_codes=(),
        risk_band: str = "low",
    ) -> None:
        self.template = _FakeTemplate()
        self.chunk_id = chunk_id
        self.risk_band = risk_band
        self.owned_source = owned_source
        self.left_context = left_context
        self.right_context = right_context
        self.glossary = glossary
        self.style_constraints = style_constraints
        self.bible_text = bible_text
        self.required_risk_feature_codes = required_risk_feature_codes


def _static_prefix(prompt: str) -> str:
    """Everything before the first dynamic marker (CHUNK_ID)."""
    marker = "CHUNK_ID:"
    return prompt.split(marker, 1)[0]


# ---------------------------------------------------------------------------
# Generation prompt (render_prompt)
# ---------------------------------------------------------------------------


def test_generation_static_blocks_precede_dynamic_blocks() -> None:
    prompt = render_prompt(_FakeBundle())
    assert prompt.index("You are a translator.") < prompt.index("CHUNK_ID:")
    assert prompt.index("BIBLE:") < prompt.index("CHUNK_ID:")
    assert prompt.index("STYLE_VOICE_CONSTRAINTS:") < prompt.index("CHUNK_ID:")
    # Dynamic blocks follow the static prefix.
    assert prompt.index("CHUNK_ID:") < prompt.index("OWNED_SOURCE")
    assert prompt.index("CHUNK_ID:") < prompt.index("GLOSSARY:")


def test_generation_static_prefix_identical_across_chunks() -> None:
    """Different chunks of one run share the exact same static prefix."""
    prompt_a = render_prompt(_FakeBundle(chunk_id="chunk0001", owned_source=(("p1", "Hello"),)))
    prompt_b = render_prompt(_FakeBundle(chunk_id="chunk0002", owned_source=(("p9", "Other"), ("p10", "Text"))))
    assert _static_prefix(prompt_a) == _static_prefix(prompt_b)


def test_generation_content_equivalence_all_blocks_present() -> None:
    """A1.2 only reorders — every block that was rendered before is still
    rendered, exactly once, in the same words."""
    prompt = render_prompt(_FakeBundle(
        glossary=(("steward", ("стюард",)),),
        left_context=(("p0", "Предыдущий"),),
        right_context=(("p3", "Next"),),
        required_risk_feature_codes=("number_word",),
    ))
    for marker in (
        "You are a translator.",
        "BIBLE:",
        "STYLE_VOICE_CONSTRAINTS:",
        "CHUNK_ID: chunk0001",
        "RISK_BAND: low",
        "OWNED_SOURCE",
        "left_context",
        "right_context",
        "GLOSSARY:",
        "  steward -> стюард",
        "REQUIRED_CATEGORY_INSTRUCTIONS:",
    ):
        assert prompt.count(marker) == 1, f"missing or duplicated block: {marker!r}"


def test_generation_no_bible_no_empty_block() -> None:
    prompt = render_prompt(_FakeBundle(bible_text=""))
    assert "BIBLE:" not in prompt
    assert prompt.startswith("You are a translator.")


# ---------------------------------------------------------------------------
# Qwen fidelity review prompt
# ---------------------------------------------------------------------------


def test_qwen_review_bible_precedes_source() -> None:
    prompt = render_qwen_review_prompt(
        source={"p1": "Hello"}, translation={"p1": "Привет"}, bible_text=BIBLE,
    )
    assert prompt.index(BIBLE) < prompt.index("SOURCE (PID -> English text, source order):")
    assert prompt.index("SOURCE") < prompt.index("TRANSLATION")


def test_qwen_review_static_prefix_identical_across_chunks() -> None:
    prompt_a = render_qwen_review_prompt(
        source={"p1": "Hello"}, translation={"p1": "Привет"}, bible_text=BIBLE,
    )
    prompt_b = render_qwen_review_prompt(
        source={"p7": "Farewell"}, translation={"p7": "Прощай"}, bible_text=BIBLE,
    )
    prefix_a = prompt_a.split("SOURCE (PID -> English text, source order):", 1)[0]
    prefix_b = prompt_b.split("SOURCE (PID -> English text, source order):", 1)[0]
    assert prefix_a == prefix_b


# ---------------------------------------------------------------------------
# Qwen audit prompt (Step 6)
# ---------------------------------------------------------------------------


def test_qwen_audit_bible_precedes_chunk() -> None:
    prompt = render_qwen_audit_prompt(
        chunk_id="chunk0001", source={"p1": "Hello"}, translation={"p1": "Привет"},
        bible_text=BIBLE,
    )
    source_block = "SOURCE (PID -> English text):\n"
    assert prompt.index(BIBLE) < prompt.index("CHUNK: chunk0001")
    assert prompt.index("CHUNK: chunk0001") < prompt.index(source_block)


def test_qwen_audit_static_prefix_identical_across_chunks() -> None:
    prompt_a = render_qwen_audit_prompt(
        chunk_id="chunk0001", source={"p1": "Hello"}, translation={"p1": "Привет"},
        bible_text=BIBLE,
    )
    prompt_b = render_qwen_audit_prompt(
        chunk_id="chunk0002", source={"p9": "Farewell"}, translation={"p9": "Прощай"},
        bible_text=BIBLE,
    )
    prefix_a = prompt_a.split("CHUNK: chunk0001", 1)[0]
    prefix_b = prompt_b.split("CHUNK: chunk0002", 1)[0]
    assert prefix_a == prefix_b


# ---------------------------------------------------------------------------
# Gemma audit prompt (Step 6, Russian-only)
# ---------------------------------------------------------------------------


def test_gemma_audit_bible_precedes_chunk() -> None:
    prompt = render_gemma_audit_prompt(
        chunk_id="chunk0001", translation={"p1": "Привет"}, bible_text=BIBLE,
    )
    translation_block = "TRANSLATION (PID -> Russian text):\n"
    assert prompt.index(BIBLE) < prompt.index("CHUNK: chunk0001")
    assert prompt.index("CHUNK: chunk0001") < prompt.index(translation_block)


def test_gemma_audit_static_prefix_identical_across_chunks() -> None:
    prompt_a = render_gemma_audit_prompt(
        chunk_id="chunk0001", translation={"p1": "Привет"}, bible_text=BIBLE,
    )
    prompt_b = render_gemma_audit_prompt(
        chunk_id="chunk0002", translation={"p9": "Прощай"}, bible_text=BIBLE,
    )
    prefix_a = prompt_a.split("CHUNK: chunk0001", 1)[0]
    prefix_b = prompt_b.split("CHUNK: chunk0002", 1)[0]
    assert prefix_a == prefix_b
