"""B9-I2 integration tests: candidate generation/ledger/auto-promotion in the book run.

Covers the B9-I2 card requirements:

* ``run_book`` calls the generator + consensus alignment after each chapter
  (source = chapter HTML, translation = ``out_dir/translations.json``),
  appends to the ledger (default ``<out_base>/glossary_candidates.json``)
  BEFORE ``MemoryManager.promote``;
* v3-threshold auto-promotion through the existing
  ``add_observation -> promote`` path: proper_name >= 2 occurrences with a
  single target promotes immediately; term needs >= 2 chapters AND >= 3
  total occurrences;
* the promoted glossary entries are FLAT ``{source: target}`` on disk;
* quarantined-chunk observations are dropped by the B7 filter (``chunk_id``);
* conflicts (competing variants / established different target) are reported
  in the per-chapter ``candidates`` block and never promoted.

The strict driver is faked (``_run_one_chapter`` monkeypatched) — the out_dir
artifacts (``strict_chapter_trial_record.json``, ``selection_results.json``,
``translations.json``, ``chunk_plan.json``) are pre-populated on disk, exactly
like the existing B7 wrapper tests.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from pact_v4.phase1.glossary_candidates import GlossaryCandidateLedger
from pact_v4.phase1.memory import MemoryManager


def _setup_memory(tmp_path: Path) -> Path:
    memory = tmp_path / "memory"
    memory.mkdir(parents=True, exist_ok=True)
    (memory / "glossary.json").write_text("{}", encoding="utf-8")
    (memory / "book_memory.json").write_text(
        json.dumps({"pov": {"gender": "male"}}, ensure_ascii=False),
        encoding="utf-8",
    )
    (memory / "observations.json").write_text("{}", encoding="utf-8")
    return memory


def _write_chapter_html(src_dir: Path, chapter_id: str, html: str) -> None:
    src_dir.mkdir(parents=True, exist_ok=True)
    (src_dir / f"{chapter_id}.html").write_text(html, encoding="utf-8")


def _make_chapter_artifacts(
    out_dir: Path,
    chapter_id: str,
    *,
    terminal_status: str,
    quarantined: list,
    translations: dict,
    chunk_plan: dict,
) -> None:
    """Pre-populate the per-chapter out_dir the way the strict driver would."""
    out_dir.mkdir(parents=True, exist_ok=True)
    chunk_ids = [c["chunk_id"] for c in chunk_plan["chunks"]]
    results = []
    for chunk_id in chunk_ids:
        is_q = chunk_id in quarantined
        results.append({
            "chunk_id": chunk_id,
            "status": "quarantined" if is_q else "selected",
            "quarantine_reason": "qwen_fidelity" if is_q else None,
        })
    out_dir.joinpath("selection_results.json").write_text(
        json.dumps({"chapter_id": chapter_id, "results": results},
                   ensure_ascii=False),
        encoding="utf-8",
    )
    out_dir.joinpath("strict_chapter_trial_record.json").write_text(
        json.dumps({"chapter_id": chapter_id,
                    "step8": {"status": terminal_status}},
                   ensure_ascii=False),
        encoding="utf-8",
    )
    out_dir.joinpath("translations.json").write_text(
        json.dumps(translations, ensure_ascii=False), encoding="utf-8",
    )
    out_dir.joinpath("chunk_plan.json").write_text(
        json.dumps(chunk_plan, ensure_ascii=False), encoding="utf-8",
    )


# ---------------------------------------------------------------------------
# Two-chapter accumulation + promotion (B9-I2 req 5)
# ---------------------------------------------------------------------------

_CH1_HTML = """<p>He met Blake at the gate. Blake knew the way.</p>
<p>Blake waited outside for Mary.</p>
<p>Blake returned home later.</p>
<p>The pact bound them all together.</p>
<p>The pact held firm against time.</p>
<p>The pact was old and strong.</p>"""

_CH1_TRANSLATIONS = {
    "p00001": "Блэйк встретил Блэйка у ворот. Блэйк знал дорогу.",
    "p00002": "Блэйк ждал снаружи Мэри.",
    "p00003": "Блэйк вернулся домой позже.",
    "p00004": "Пакт связывал их всех вместе.",
    "p00005": "Пакт держался крепко против времени.",
    "p00006": "Пакт был старым и сильным.",
}

_CH2_HTML = """<p>The pact grew stronger with time.</p>
<p>The pact never broke its bond.</p>
<p>The pact endured through the ages.</p>
<p>He walked home in the dark.</p>
<p>She opened the heavy door.</p>"""

_CH2_TRANSLATIONS = {
    "p00001": "Пакт становился сильнее со временем.",
    "p00002": "Пакт никогда не разрывал свою связь.",
    "p00003": "Пакт выстоял сквозь века.",
    "p00004": "Он пошёл домой в темноте.",
    "p00005": "Она открыла тяжёлую дверь.",
}

_PLAN = {
    "artifact": "pact-v4-chunk-plan/v1",
    "snapshot_hash": "test",
    "plan_hash": "test",
    "chunks": [
        {"chunk_id": "chunk0001", "snapshot_hash": "test",
         "pids": ["p00001", "p00002", "p00003"],
         "word_counts": [], "context": {"left_ru": "", "right_en": []},
         "undersized_exception": False},
        {"chunk_id": "chunk0002", "snapshot_hash": "test",
         "pids": ["p00004", "p00005", "p00006"],
         "word_counts": [], "context": {"left_ru": "", "right_en": []},
         "undersized_exception": False},
    ],
}


class TestBookRunCandidateIntegration:
    def _run(self, tmp_path, monkeypatch, chapter_specs):
        from pact_full_pipeline_runner_v1 import v4_book_run

        memory = _setup_memory(tmp_path)
        out_base = tmp_path / "out"
        src_dir = tmp_path / "src"

        def fake_run_one(*args, **kwargs):
            return {"status": "ok"}

        monkeypatch.setattr(v4_book_run, "_run_one_chapter", fake_run_one)

        for chapter_id, (html, terminal, quarantined, translations) in chapter_specs.items():
            _write_chapter_html(src_dir, chapter_id, html)
            _make_chapter_artifacts(
                out_base / f"chapter_{chapter_id}", chapter_id,
                terminal_status=terminal, quarantined=quarantined,
                translations=translations, chunk_plan=_PLAN,
            )

        result = v4_book_run.run_book(
            memory_dir=memory,
            chapter_ids=list(chapter_specs),
            chapter_html_pattern=str(src_dir / "{chapter_id}.html"),
            out_base=out_base,
        )
        return memory, out_base, result

    def test_two_chapters_generate_accumulate_promote_flat_glossary(
        self, tmp_path, monkeypatch,
    ):
        """proper_name promotes at ch1; term waits for 2 chapters; glossary flat."""
        memory, out_base, result = self._run(tmp_path, monkeypatch, {
            "0001": (_CH1_HTML, "complete", [], _CH1_TRANSLATIONS),
            "0002": (_CH2_HTML, "complete", [], _CH2_TRANSLATIONS),
        })

        # Both chapters reached complete and promoted observations.
        assert [r["terminal_status"] for r in result["chapters"]] == [
            "complete", "complete",
        ]

        # Per-chapter candidates blocks (B9-I2 req 4).
        ch1, ch2 = result["chapters"]
        assert ch1["candidates"] == {"generated": 2, "promoted": 1, "conflicts": 0}
        assert ch2["candidates"] == {"generated": 1, "promoted": 1, "conflicts": 0}

        # glossary.json updated with FLAT {source: target} pairs (req 5).
        glossary = json.loads((memory / "glossary.json").read_text(encoding="utf-8"))
        assert glossary == {"Blake": "Блэйк", "pact": "пакт"}
        assert all(isinstance(v, str) for v in glossary.values())

        # Ledger accumulated: pact spans both chapters with 6 total occurrences.
        ledger_path = out_base / "glossary_candidates.json"
        assert result["candidates_ledger"] == str(ledger_path)
        assert ledger_path.exists()
        records = GlossaryCandidateLedger(str(ledger_path)).load()
        pact = records["term|pact"]
        assert pact["total_occurrences"] == 6
        assert {c["chapter_id"] for c in pact["chapters"]} == {"0001", "0002"}
        blake = records["proper_name|blake"]
        assert blake["total_occurrences"] == 4
        assert [c["chapter_id"] for c in blake["chapters"]] == ["0001"]

    def test_quarantined_chunk_observation_not_promoted(self, tmp_path, monkeypatch):
        """accepted_degraded + quarantined chunk: B7 filter drops the observation."""
        html = """<p>He met Blake at the gate. Blake knew the way.</p>
<p>Blake waited outside for Mary.</p>"""
        translations = {
            "p00001": "Блэйк встретил Блэйка у ворот. Блэйк знал дорогу.",
            "p00002": "Блэйк ждал снаружи Мэри.",
        }
        plan = {
            "artifact": "pact-v4-chunk-plan/v1",
            "snapshot_hash": "test",
            "plan_hash": "test",
            "chunks": [
                {"chunk_id": "chunk0001", "snapshot_hash": "test",
                 "pids": ["p00001", "p00002"],
                 "word_counts": [], "context": {"left_ru": "", "right_en": []},
                 "undersized_exception": False},
            ],
        }
        memory, out_base, result = self._run(tmp_path, monkeypatch, {
            "0001": (html, "accepted_degraded", ["chunk0001"], translations),
        })

        # The candidate was generated and auto-promoted (observation added)...
        assert result["chapters"][0]["candidates"] == {
            "generated": 1, "promoted": 1, "conflicts": 0,
        }
        # ...but the B7 promote filter dropped the quarantined-chunk
        # observation, so the glossary was NOT updated.
        glossary = json.loads((memory / "glossary.json").read_text(encoding="utf-8"))
        assert "Blake" not in glossary
        assert glossary == {}


# ---------------------------------------------------------------------------
# _auto_promote_glossary: thresholds and conflict paths (B9-I2 req 2)
# ---------------------------------------------------------------------------


class TestAutoPromoteGlossary:
    def _manager(self, tmp_path: Path) -> MemoryManager:
        memory = _setup_memory(tmp_path)
        return MemoryManager(str(memory))

    def _ledger(self, records) -> dict:
        """One-entry ledger map keyed like ``GlossaryCandidateLedger.load``."""
        from pact_v4.phase1.glossary_candidates import candidate_key
        return {
            candidate_key(r["source"], r["kind"]): r
            for r in records
        }

    def _observation(self, tmp_path: Path, source: str):
        observations = json.loads(
            (tmp_path / "memory" / "observations.json").read_text(encoding="utf-8")
        )
        return observations.get("glossary", {}).get(source)

    def test_proper_name_meets_threshold_is_observed(self, tmp_path):
        from pact_full_pipeline_runner_v1 import v4_book_run

        manager = self._manager(tmp_path)
        aligned = [{"source": "Blake", "kind": "proper_name",
                    "occurrences": 3, "chunk_ids": ["chunk0002"],
                    "target": "Блэйк", "conflicts": []}]
        merged = self._ledger([{
            "source": "Blake", "kind": "proper_name",
            "total_occurrences": 3,
            "chapters": [{"chapter_id": "0001", "chunk_ids": ["chunk0002"], "count": 3}],
        }])
        promoted, conflicts = v4_book_run._auto_promote_glossary(
            manager, aligned, merged, {},
            term_min_chapters=2, term_min_occurrences=3,
            proper_name_min_occurrences=2,
        )
        assert len(promoted) == 1
        assert conflicts == []
        obs = self._observation(tmp_path, "Blake")
        assert obs == {"target": "Блэйк", "type": "proper_name",
                       "chunk_id": "chunk0002"}

    def test_term_below_chapters_threshold_not_observed(self, tmp_path):
        from pact_full_pipeline_runner_v1 import v4_book_run

        manager = self._manager(tmp_path)
        aligned = [{"source": "pact", "kind": "term",
                    "occurrences": 3, "chunk_ids": [],
                    "target": "пакт", "conflicts": []}]
        merged = self._ledger([{
            "source": "pact", "kind": "term",
            "total_occurrences": 3,
            "chapters": [{"chapter_id": "0001", "chunk_ids": [], "count": 3}],
        }])
        promoted, conflicts = v4_book_run._auto_promote_glossary(
            manager, aligned, merged, {},
            term_min_chapters=2, term_min_occurrences=3,
            proper_name_min_occurrences=2,
        )
        assert promoted == []
        assert conflicts == []
        observations = json.loads(
            (tmp_path / "memory" / "observations.json").read_text(encoding="utf-8")
        )
        assert "pact" not in observations.get("glossary", {})

    def test_term_meets_two_chapters_is_observed(self, tmp_path):
        from pact_full_pipeline_runner_v1 import v4_book_run

        manager = self._manager(tmp_path)
        aligned = [{"source": "pact", "kind": "term",
                    "occurrences": 3, "chunk_ids": ["chunk0002"],
                    "target": "пакт", "conflicts": []}]
        merged = self._ledger([{
            "source": "pact", "kind": "term",
            "total_occurrences": 6,
            "chapters": [
                {"chapter_id": "0001", "chunk_ids": [], "count": 3},
                {"chapter_id": "0002", "chunk_ids": ["chunk0002"], "count": 3},
            ],
        }])
        promoted, conflicts = v4_book_run._auto_promote_glossary(
            manager, aligned, merged, {},
            term_min_chapters=2, term_min_occurrences=3,
            proper_name_min_occurrences=2,
        )
        assert len(promoted) == 1
        assert self._observation(tmp_path, "pact") == {
            "target": "пакт", "type": "term", "chunk_id": "chunk0002",
        }

    def test_established_different_target_is_conflict(self, tmp_path):
        from pact_full_pipeline_runner_v1 import v4_book_run

        manager = self._manager(tmp_path)
        aligned = [{"source": "Blake", "kind": "proper_name",
                    "occurrences": 3, "chunk_ids": [],
                    "target": "Блэйк", "conflicts": []}]
        merged = self._ledger([{
            "source": "Blake", "kind": "proper_name",
            "total_occurrences": 3,
            "chapters": [{"chapter_id": "0001", "chunk_ids": [], "count": 3}],
        }])
        promoted, conflicts = v4_book_run._auto_promote_glossary(
            manager, aligned, merged, {"Blake": "Блейк"},
            term_min_chapters=2, term_min_occurrences=3,
            proper_name_min_occurrences=2,
        )
        assert promoted == []
        assert len(conflicts) == 1
        assert conflicts[0]["established_target"] == "Блейк"
        observations = json.loads(
            (tmp_path / "memory" / "observations.json").read_text(encoding="utf-8")
        )
        assert "Blake" not in observations.get("glossary", {})

    def test_alignment_conflict_variants_never_observed(self, tmp_path):
        from pact_full_pipeline_runner_v1 import v4_book_run

        manager = self._manager(tmp_path)
        aligned = [{"source": "Duncan", "kind": "proper_name",
                    "occurrences": 4, "chunk_ids": [],
                    "target": None, "conflicts": ["Гордон", "Дункан"]}]
        merged = self._ledger([{
            "source": "Duncan", "kind": "proper_name",
            "total_occurrences": 4,
            "chapters": [{"chapter_id": "0001", "chunk_ids": [], "count": 4}],
        }])
        promoted, conflicts = v4_book_run._auto_promote_glossary(
            manager, aligned, merged, {},
            term_min_chapters=2, term_min_occurrences=3,
            proper_name_min_occurrences=2,
        )
        assert promoted == []
        assert len(conflicts) == 1
        assert conflicts[0]["conflicts"] == ["Гордон", "Дункан"]

    def test_already_established_same_target_is_noop(self, tmp_path):
        from pact_full_pipeline_runner_v1 import v4_book_run

        manager = self._manager(tmp_path)
        aligned = [{"source": "Blake", "kind": "proper_name",
                    "occurrences": 2, "chunk_ids": [],
                    "target": "Блэйк", "conflicts": []}]
        merged = self._ledger([{
            "source": "Blake", "kind": "proper_name",
            "total_occurrences": 2,
            "chapters": [{"chapter_id": "0002", "chunk_ids": [], "count": 2}],
        }])
        promoted, conflicts = v4_book_run._auto_promote_glossary(
            manager, aligned, merged, {"Blake": "Блэйк"},
            term_min_chapters=2, term_min_occurrences=3,
            proper_name_min_occurrences=2,
        )
        assert promoted == []
        assert conflicts == []

    def test_flat_target_accepts_string_and_target_dict(self):
        from pact_full_pipeline_runner_v1 import v4_book_run

        assert v4_book_run._flat_target("Блэйк") == "Блэйк"
        assert v4_book_run._flat_target({"target": "Блэйк"}) == "Блэйк"
        assert v4_book_run._flat_target({"target": 5}) is None
        assert v4_book_run._flat_target(None) is None
        assert v4_book_run._flat_target({"name": "John"}) is None


# ---------------------------------------------------------------------------
# CLI arg wiring (dest-name check — the B5 regression pattern)
# ---------------------------------------------------------------------------


class TestBookRunCliArgs:
    def test_b9_args_parse_with_correct_dests(self):
        """Real argparse parse: dest names must match run_book's kwargs."""
        from pact_full_pipeline_runner_v1 import v4_book_run

        args, extra = v4_book_run.build_argparser().parse_known_args([
            "--memory-dir", "mem", "--chapters", "0001", "0002",
            "--chapter-html-pattern", "ch/{chapter_id}.html",
            "--out-base", "out",
            "--candidates-ledger", "led.json",
            "--term-min-occurrences", "4",
            "--term-min-chapters", "3",
            "--proper-name-min-occurrences", "5",
            "--consensus-ratio", "0.9",
            "--run-label", "test-run",  # unknown to book_run -> strict driver
        ])
        assert args.candidates_ledger == Path("led.json")
        assert args.term_min_occurrences == 4
        assert args.term_min_chapters == 3
        assert args.proper_name_min_occurrences == 5
        assert args.consensus_ratio == 0.9
        # Unknown args still pass through to the strict driver.
        assert "--run-label" in extra and "test-run" in extra

    def test_b9_args_defaults(self):
        from pact_full_pipeline_runner_v1 import v4_book_run

        args, _extra = v4_book_run.build_argparser().parse_known_args([
            "--memory-dir", "mem", "--chapters", "0001",
            "--chapter-html-pattern", "ch/{chapter_id}.html",
            "--out-base", "out",
        ])
        assert args.candidates_ledger is None
        assert args.term_min_occurrences == 3
        assert args.term_min_chapters == 2
        assert args.proper_name_min_occurrences == 2
        assert args.consensus_ratio == 0.8
