"""Tests for ``pact_v4.runtime.reasoning_writer`` (REASONING-STREAM)."""

from __future__ import annotations

from pact_v4.runtime.reasoning_writer import append_error_marker, open_reasoning_writer


def test_open_reasoning_writer_none_returns_none():
    assert open_reasoning_writer(None) is None


def test_writer_appends_live(tmp_path):
    path = tmp_path / "b1.2_entity_reasoning.txt"
    writer = open_reasoning_writer(path)
    writer("live-")
    writer("part")
    assert path.read_text(encoding="utf-8") == "live-part"


def test_writer_rollback_truncates_to_precall_state(tmp_path):
    """RV2 t_a7c14251 HIGH: rollback() discards tentative chunks already
    appended by a failed stream attempt — the file returns to the empty
    pre-call state so a later batch delivery is the only content."""
    path = tmp_path / "b1.2_entity_reasoning.txt"
    writer = open_reasoning_writer(path)
    writer("tentative-")
    assert path.read_text(encoding="utf-8") == "tentative-"
    writer.rollback()
    assert path.read_text(encoding="utf-8") == ""
    # Writer still usable after rollback (batch fallback appends the full
    # reasoning once).
    writer("full-reasoning")
    assert path.read_text(encoding="utf-8") == "full-reasoning"


def test_append_error_marker_preserves_streamed_content(tmp_path):
    path = tmp_path / "b1.2_entity_reasoning.txt"
    writer = open_reasoning_writer(path)
    writer("partial-streamed-")
    append_error_marker(path, RuntimeError("connection reset"))
    text = path.read_text(encoding="utf-8")
    assert text.startswith("partial-streamed-")
    assert "TRANSPORT_ERROR: RuntimeError: connection reset" in text
