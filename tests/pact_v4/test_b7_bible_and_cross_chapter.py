"""Tests for B7: bible rendering, narrator_gender check, promotion, book-run."""
from __future__ import annotations

import json
import tempfile
from pathlib import Path
from typing import Any, Dict

import pytest

from pact_v4._integrity_checks import check_narrator_gender
from pact_v4.phase1.memory import MemoryManager
from pact_v4.phase2.prompts import render_prompt
from pact_v4.runtime.bible_renderer import (
    extract_narrator_gender,
    render_bible_section,
)
from pact_v4.runtime.prompts_runtime import (
    render_gemma_audit_prompt,
    render_qwen_audit_prompt,
    render_qwen_review_prompt,
)


# ---------------------------------------------------------------------------
# Bible renderer
# ---------------------------------------------------------------------------


class TestRenderBibleSection:
    def test_empty_memory_returns_empty(self):
        assert render_bible_section({}) == ""
        assert render_bible_section(None) == ""

    def test_narrator_gender_only(self):
        memory = {"pov": {"gender": "male"}}
        result = render_bible_section(memory)
        assert "BIBLE:" in result
        assert "Narrator: male" in result

    def test_characters_dict(self):
        memory = {
            "characters": {
                "John": {"gender": "male", "role": "protagonist"},
                "Mary": {"gender": "female", "role": "sister"},
            },
        }
        result = render_bible_section(memory)
        assert "Characters:" in result
        assert "John" in result
        assert "Mary" in result

    def test_characters_list(self):
        memory = {
            "characters": [
                {"name": "John", "gender": "male", "role": "protagonist"},
            ],
        }
        result = render_bible_section(memory)
        assert "John" in result

    def test_facts_list(self):
        memory = {
            "facts": [
                {"text": "The story takes place in London."},
                "John inherited the estate.",
            ],
        }
        result = render_bible_section(memory)
        assert "Facts:" in result
        assert "London" in result
        assert "inherited" in result

    def test_address_register(self):
        memory = {
            "address_register": [
                {"text": 'Use "ты" for family members'},
            ],
        }
        result = render_bible_section(memory)
        assert "Address register:" in result

    def test_full_render(self):
        memory = {
            "pov": {"gender": "female"},
            "characters": {"Anna": {"gender": "female", "role": "heroine"}},
            "facts": [{"text": "Set in Moscow."}],
            "address_register": [{"text": "ты for close friends"}],
        }
        result = render_bible_section(memory)
        assert "Narrator: female" in result
        assert "Anna" in result
        assert "Moscow" in result
        assert "ты" in result

    def test_deterministic(self):
        memory = {
            "pov": {"gender": "male"},
            "characters": {"A": {"gender": "male"}, "B": {"gender": "female"}},
        }
        r1 = render_bible_section(memory)
        r2 = render_bible_section(memory)
        assert r1 == r2

    def test_truncation_suffix_on_characters(self):
        memory = {
            "characters": {f"c{i}": {"gender": "male"} for i in range(50)},
        }
        result = render_bible_section(memory)
        assert "(showing first 20 of 50)" in result

    def test_truncation_suffix_on_facts(self):
        memory = {
            "facts": [{"text": f"fact {i}"} for i in range(50)],
        }
        result = render_bible_section(memory)
        assert "(showing first 30 of 50)" in result

    def test_no_truncation_suffix_under_limit(self):
        memory = {
            "characters": {f"c{i}": {"gender": "male"} for i in range(5)},
        }
        result = render_bible_section(memory)
        assert "showing first" not in result


class TestExtractNarratorGender:
    def test_male_pov(self):
        assert extract_narrator_gender({"pov": {"gender": "male"}}) == "male"

    def test_female_pov(self):
        assert extract_narrator_gender({"pov": {"gender": "female"}}) == "female"

    def test_legacy_key(self):
        assert extract_narrator_gender({"narrator_gender": "male"}) == "male"

    def test_russian_values(self):
        assert extract_narrator_gender({"pov": {"gender": "мужской"}}) == "male"
        assert extract_narrator_gender({"pov": {"gender": "женский"}}) == "female"

    def test_absent(self):
        assert extract_narrator_gender({}) is None
        assert extract_narrator_gender({"pov": {}}) is None


# ---------------------------------------------------------------------------
# Narrator gender check
# ---------------------------------------------------------------------------


class TestCheckNarratorGender:
    def test_male_expected_female_found(self):
        text = "Я вошёл в комнату. Я увидел стол."
        mismatches = check_narrator_gender(text, "male")
        assert mismatches == []

    def test_male_expected_female_mismatch(self):
        text = "Я вошёл в комнату. Я увидела стол."
        mismatches = check_narrator_gender(text, "male")
        assert len(mismatches) >= 1
        assert any("увидела" in m["form"] for m in mismatches)

    def test_female_expected_male_mismatch(self):
        text = "Я вошла в комнату. Я увидел стол."
        mismatches = check_narrator_gender(text, "female")
        assert len(mismatches) >= 1
        assert any("увидел" in m["form"] for m in mismatches)

    def test_female_expected_correct(self):
        text = "Я вошла в комнату. Я увидела стол."
        mismatches = check_narrator_gender(text, "female")
        assert mismatches == []

    def test_empty_text(self):
        assert check_narrator_gender("", "male") == []

    def test_unknown_gender(self):
        assert check_narrator_gender("Я увидел", "unknown") == []

    def test_no_self_references(self):
        text = "Он вошёл в комнату. Она увидела стол."
        assert check_narrator_gender(text, "male") == []

    def test_male_pattern_cleaned_of_typos(self):
        """Guard against reintroduction of dead branches like шёлкошел or мо[гg]."""
        from pact_v4._integrity_checks import _NARRATOR_MALE_RE, _NARRATOR_FEMALE_RE
        assert "шёлкошел" not in _NARRATOR_MALE_RE.pattern
        assert "мо[гg]" not in _NARRATOR_MALE_RE.pattern
        assert "зна[лc]" not in _NARRATOR_MALE_RE.pattern
        assert "реши[лc]" not in _NARRATOR_MALE_RE.pattern
        # female pattern stays closed-form female-only verbs
        assert "была" in _NARRATOR_FEMALE_RE.pattern
        assert "увидела" in _NARRATOR_FEMALE_RE.pattern


# ---------------------------------------------------------------------------
# Prompt rendering with bible_text
# ---------------------------------------------------------------------------


class TestPromptRenderingWithBible:
    def test_generation_prompt_includes_bible(self):
        class FakeBundle:
            template = type("T", (), {"instructions": "INSTRUCT"})()
            chunk_id = "chunk0001"
            risk_band = "low"
            owned_source = (("p1", "Hello"),)
            left_context = ()
            right_context = ()
            glossary = ()
            style_constraints = ()
            bible_text = "BIBLE:\n  - Narrator: male\n"
            required_risk_feature_codes = ()

        prompt = render_prompt(FakeBundle())
        assert "BIBLE:" in prompt
        assert "Narrator: male" in prompt

    def test_generation_prompt_no_bible(self):
        class FakeBundle:
            template = type("T", (), {"instructions": "INSTRUCT"})()
            chunk_id = "chunk0001"
            risk_band = "low"
            owned_source = (("p1", "Hello"),)
            left_context = ()
            right_context = ()
            glossary = ()
            style_constraints = ()
            bible_text = ""
            required_risk_feature_codes = ()

        prompt = render_prompt(FakeBundle())
        assert "BIBLE:" not in prompt

    def test_qwen_review_prompt_with_bible(self):
        prompt = render_qwen_review_prompt(
            source={"p1": "Hello"},
            translation={"p1": "Привет"},
            bible_text="BIBLE:\n  - Narrator: male\n",
        )
        assert "BIBLE:" in prompt
        assert "Narrator: male" in prompt

    def test_qwen_audit_prompt_with_bible(self):
        prompt = render_qwen_audit_prompt(
            chunk_id="chunk0001",
            source={"p1": "Hello"},
            translation={"p1": "Привет"},
            bible_text="BIBLE:\n  - Narrator: female\n",
        )
        assert "BIBLE:" in prompt
        assert "Narrator: female" in prompt

    def test_gemma_audit_prompt_with_bible(self):
        prompt = render_gemma_audit_prompt(
            chunk_id="chunk0001",
            translation={"p1": "Привет"},
            bible_text="BIBLE:\n  - Characters:\n  * John\n",
        )
        assert "BIBLE:" in prompt
        assert "John" in prompt


# ---------------------------------------------------------------------------
# Memory promotion with accepted_degraded
# ---------------------------------------------------------------------------


class TestMemoryPromotion:
    def _setup_memory(self, tmp_path: Path) -> MemoryManager:
        tmp_path.mkdir(parents=True, exist_ok=True)
        (tmp_path / "glossary.json").write_text("{}", encoding="utf-8")
        (tmp_path / "book_memory.json").write_text("{}", encoding="utf-8")
        (tmp_path / "observations.json").write_text("{}", encoding="utf-8")
        return MemoryManager(str(tmp_path))

    def test_complete_promotes_all(self, tmp_path):
        manager = self._setup_memory(tmp_path)
        manager.add_observation("book_memory", "char1", {"name": "John"})
        manager.promote("complete")
        bm = json.loads((tmp_path / "book_memory.json").read_text(encoding="utf-8"))
        assert "char1" in bm

    def test_accepted_degraded_promotes_non_quarantined(self, tmp_path):
        manager = self._setup_memory(tmp_path)
        manager.add_observation("book_memory", "char1", {
            "name": "John", "chunk_id": "chunk0001",
        })
        manager.add_observation("book_memory", "char2", {
            "name": "Mary", "chunk_id": "chunk0002",
        })
        manager.promote("accepted_degraded", quarantined_chunks={"chunk0002"})
        bm = json.loads((tmp_path / "book_memory.json").read_text(encoding="utf-8"))
        assert "char1" in bm
        assert "char2" not in bm

    def test_accepted_degraded_promotes_chunkless(self, tmp_path):
        manager = self._setup_memory(tmp_path)
        manager.add_observation("book_memory", "fact1", {
            "text": "London setting",
        })
        manager.promote("accepted_degraded", quarantined_chunks={"chunk0001"})
        bm = json.loads((tmp_path / "book_memory.json").read_text(encoding="utf-8"))
        assert "fact1" in bm

    def test_failed_does_not_promote(self, tmp_path):
        manager = self._setup_memory(tmp_path)
        manager.add_observation("book_memory", "char1", {"name": "John"})
        manager.promote("failed")
        bm = json.loads((tmp_path / "book_memory.json").read_text(encoding="utf-8"))
        assert "char1" not in bm

    def test_quarantined_does_not_promote(self, tmp_path):
        manager = self._setup_memory(tmp_path)
        manager.add_observation("book_memory", "char1", {"name": "John"})
        manager.promote("quarantined")
        bm = json.loads((tmp_path / "book_memory.json").read_text(encoding="utf-8"))
        assert "char1" not in bm


# ---------------------------------------------------------------------------
# Book-run wrapper (B7)
# ---------------------------------------------------------------------------


class TestBookRunWrapper:
    def _setup_memory(self, tmp_path: Path) -> Path:
        memory = tmp_path / "memory"
        memory.mkdir(parents=True, exist_ok=True)
        (memory / "glossary.json").write_text("{}", encoding="utf-8")
        (memory / "book_memory.json").write_text(
            json.dumps({"pov": {"gender": "male"}}, ensure_ascii=False),
            encoding="utf-8",
        )
        (memory / "observations.json").write_text("{}", encoding="utf-8")
        return memory

    def _make_chapter_artifacts(self, out_dir: Path, chapter_id: str, *, terminal_status: str, quarantined: list) -> None:
        """Pre-populate the per-chapter out_dir so run_book reads status from disk
        without actually running the strict driver (tests for the wrapper's
        promotion policy only — not the strict driver itself)."""
        out_dir.mkdir(parents=True, exist_ok=True)
        results = []
        for i in range(3):
            chunk_id = f"chunk{i:04d}"
            is_q = chunk_id in quarantined
            results.append({
                "chunk_id": chunk_id,
                "status": "quarantined" if is_q else "selected",
                "quarantine_reason": "qwen_fidelity" if is_q else None,
            })
        out_dir.joinpath("selection_results.json").write_text(
            json.dumps({
                "chapter_id": chapter_id,
                "results": results,
            }, ensure_ascii=False),
            encoding="utf-8",
        )
        out_dir.joinpath("strict_chapter_trial_record.json").write_text(
            json.dumps({
                "chapter_id": chapter_id,
                "step8": {"status": terminal_status},
            }, ensure_ascii=False),
            encoding="utf-8",
        )

    def test_promotion_invariants_for_complete(self, tmp_path, monkeypatch):
        """Complete: all observations promoted, no quarantined chunks allowed."""
        from pact_full_pipeline_runner_v1 import v4_book_run

        memory = self._setup_memory(tmp_path)
        out_base = tmp_path / "out"
        chapter_dir = out_base / "chapter_0001"
        self._make_chapter_artifacts(chapter_dir, "0001", terminal_status="complete", quarantined=[])

        from pact_v4.phase1.memory import MemoryManager
        mgr = MemoryManager(str(memory))
        mgr.add_observation("book_memory", "char1", {"name": "John"})
        mgr.add_observation("book_memory", "char2", {
            "name": "Mary", "chunk_id": "chunk0001",
        })

        def fake_run_one(*args, **kwargs):
            return {"status": "ok"}

        monkeypatch.setattr(v4_book_run, "_run_one_chapter", fake_run_one)

        v4_book_run.run_book(
            memory_dir=memory,
            chapter_ids=["0001"],
            chapter_html_pattern=str(tmp_path / "src" / "{chapter_id}.html"),
            out_base=out_base,
        )
        bm = json.loads((memory / "book_memory.json").read_text(encoding="utf-8"))
        assert "char1" in bm
        assert "char2" in bm

        book_run = json.loads((out_base / "book_run.json").read_text(encoding="utf-8"))
        assert book_run["chapters"][0]["terminal_status"] == "complete"
        assert book_run["chapters"][0]["promoted"] is True

    def test_accepted_degraded_excludes_quarantined(self, tmp_path, monkeypatch):
        """accepted_degraded: promote all except quarantined chunk observations."""
        from pact_full_pipeline_runner_v1 import v4_book_run

        memory = self._setup_memory(tmp_path)
        out_base = tmp_path / "out"
        chapter_dir = out_base / "chapter_0001"
        self._make_chapter_artifacts(chapter_dir, "0001", terminal_status="accepted_degraded", quarantined=["chunk0001"])

        from pact_v4.phase1.memory import MemoryManager
        mgr = MemoryManager(str(memory))
        mgr.add_observation("book_memory", "kept1", {
            "name": "John", "chunk_id": "chunk0002",
        })
        mgr.add_observation("book_memory", "excluded1", {
            "name": "Mary", "chunk_id": "chunk0001",
        })

        def fake_run_one(*args, **kwargs):
            return {"status": "ok"}

        monkeypatch.setattr(v4_book_run, "_run_one_chapter", fake_run_one)

        v4_book_run.run_book(
            memory_dir=memory,
            chapter_ids=["0001"],
            chapter_html_pattern=str(tmp_path / "src" / "{chapter_id}.html"),
            out_base=out_base,
        )
        bm = json.loads((memory / "book_memory.json").read_text(encoding="utf-8"))
        assert "kept1" in bm
        assert "excluded1" not in bm

    def test_failed_does_not_promote(self, tmp_path, monkeypatch):
        from pact_full_pipeline_runner_v1 import v4_book_run

        memory = self._setup_memory(tmp_path)
        out_base = tmp_path / "out"
        chapter_dir = out_base / "chapter_0001"
        self._make_chapter_artifacts(chapter_dir, "0001", terminal_status="failed", quarantined=[])

        from pact_v4.phase1.memory import MemoryManager
        mgr = MemoryManager(str(memory))
        mgr.add_observation("book_memory", "char1", {"name": "John"})

        def fake_run_one(*args, **kwargs):
            return {"status": "ok"}

        monkeypatch.setattr(v4_book_run, "_run_one_chapter", fake_run_one)

        v4_book_run.run_book(
            memory_dir=memory,
            chapter_ids=["0001"],
            chapter_html_pattern=str(tmp_path / "src" / "{chapter_id}.html"),
            out_base=out_base,
        )
        bm = json.loads((memory / "book_memory.json").read_text(encoding="utf-8"))
        assert "char1" not in bm
        book_run = json.loads((out_base / "book_run.json").read_text(encoding="utf-8"))
        assert book_run["chapters"][0]["promoted"] is False

    def test_main_exit_code_on_failure(self, tmp_path, monkeypatch, capsys):
        from pact_full_pipeline_runner_v1 import v4_book_run

        memory = self._setup_memory(tmp_path)
        out_base = tmp_path / "out"
        chapter_dir = out_base / "chapter_0001"
        self._make_chapter_artifacts(chapter_dir, "0001", terminal_status="failed", quarantined=[])

        def fake_run_one(*args, **kwargs):
            return {"status": "ok"}

        monkeypatch.setattr(v4_book_run, "_run_one_chapter", fake_run_one)

        rc = v4_book_run.main([
            "--memory-dir", str(memory),
            "--chapters", "0001",
            "--chapter-html-pattern", str(tmp_path / "src" / "{chapter_id}.html"),
            "--out-base", str(out_base),
        ])
        assert rc == 1
        captured = capsys.readouterr()
        assert "FAILED" in captured.out
        assert "did not reach" in captured.err

    def test_main_exit_zero_on_complete(self, tmp_path, monkeypatch):
        from pact_full_pipeline_runner_v1 import v4_book_run

        memory = self._setup_memory(tmp_path)
        out_base = tmp_path / "out"
        chapter_dir = out_base / "chapter_0001"
        self._make_chapter_artifacts(chapter_dir, "0001", terminal_status="complete", quarantined=[])

        def fake_run_one(*args, **kwargs):
            return {"status": "ok"}

        monkeypatch.setattr(v4_book_run, "_run_one_chapter", fake_run_one)

        rc = v4_book_run.main([
            "--memory-dir", str(memory),
            "--chapters", "0001",
            "--chapter-html-pattern", str(tmp_path / "src" / "{chapter_id}.html"),
            "--out-base", str(out_base),
        ])
        assert rc == 0


# ---------------------------------------------------------------------------
# build_role_adapters / build_strict_lifecycle / build_repair_adapters
# thread bible_text into all Phase 2C/3B/4A adapters.
# ---------------------------------------------------------------------------


class TestBiblePlumbing:
    def test_build_role_adapters_threads_bible_text(self):
        """B7 critical: bible_text must reach every Phase 2C/3B adapter.

        A local Llama backend needs a real exe/model_paths, so the full
        ``build_role_adapters`` path is exercised by inspecting each
        adapter's config (the only B7-relevant surface). The adapters
        themselves are wrapped BackendX — we verify the config field
        instead of mocking network calls.
        """
        from unittest.mock import MagicMock
        from pact_v4.runtime.runtime_config import build_role_adapters
        from pact_v4.runtime.backend_role_adapters import (
            BackendQwenAuditEvaluatorConfig,
            BackendGemmaAuditEvaluatorConfig,
            BackendQwenEvaluatorConfig,
        )

        fake_cfg = MagicMock()
        fake_runtime = MagicMock()
        fake_runtime.backend = MagicMock()

        bible = "BIBLE:\n  - Narrator: male\n"
        with MagicMock() as fake_backend_factory:
            fake_backend_factory.return_value = MagicMock()
            import pact_v4.runtime.runtime_config as rc
            original = rc.build_role_backend
            rc.build_role_backend = lambda cfg, runtime: MagicMock()
            try:
                model_caller, qwen_evaluator, gemma_selector, \
                    qwen_audit, gemma_audit = build_role_adapters(
                        fake_cfg, fake_runtime, bible_text=bible,
                    )
            finally:
                rc.build_role_backend = original

        assert qwen_evaluator._config.bible_text == bible
        assert qwen_audit._config.bible_text == bible
        assert gemma_audit._config.bible_text == bible

    def test_b6_quarantined_retry_threads_bible_text(self):
        """The B6 retry cycle must generate with the same bible as main loop."""
        import inspect
        from pact_v4.phase4 import quarantined_retry

        run_sig = inspect.signature(quarantined_retry.run_quarantined_retry)
        assert "bible_text" in run_sig.parameters
        assert run_sig.parameters["bible_text"].default == ""

        retry_sig = inspect.signature(quarantined_retry._retry_one_chunk)
        assert "bible_text" in retry_sig.parameters
