"""B9-I2 integration tests: candidate generation/ledger in the book run (Variant A).

Covers the B9-I2 card requirements under the owner decision recorded in
DECISIONS.md (Variant A — shadow-only; B9-RV2 review of PR #128):

* ``run_book`` calls the generator + consensus alignment after each chapter
  (source = chapter HTML, translation = ``out_dir/translations.json``) and
  appends to the ledger (default ``<out_base>/glossary_candidates.json``)
  BEFORE ``MemoryManager.promote``;
* ONLY chapters with an accepted terminal result (``complete`` /
  ``accepted_degraded``) contribute to the ledger — a failed chapter's
  observations must never satisfy later thresholds (review F1);
* the B9 loop NEVER auto-promotes: ``MemoryManager.add_observation`` is not
  called, ``glossary.json`` stays untouched, and glossary growth remains a
  human decision (Variant A; the B7 ``promote`` still moves manual
  observations);
* the B5 combined mixed-script allowlist (bible + glossary + manual +
  source-derived) is threaded into candidate generation — an allowlisted
  token is never recorded (review F3);
* the promoted glossary entries are FLAT ``{source: target}`` on disk when
  a manual observation is promoted.

The strict driver is faked (``_run_one_chapter`` monkeypatched) — the out_dir
artifacts (``strict_chapter_trial_record.json``, ``selection_results.json``,
``translations.json``, ``chunk_plan.json``) are pre-populated on disk, exactly
like the existing B7 wrapper tests.
"""
from __future__ import annotations

import json
from pathlib import Path

from pact_v4.phase1.glossary_candidates import GlossaryCandidateLedger


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
# Two-chapter accumulation (B9-I2 req 5, Variant A)
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
    def _run(self, tmp_path, monkeypatch, chapter_specs,
             mixed_script_allow=()):
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
            mixed_script_allow=mixed_script_allow,
        )
        return memory, out_base, result

    def test_two_chapters_accumulate_in_ledger_shadow_only(
        self, tmp_path, monkeypatch,
    ):
        """Candidates accumulate in the ledger; nothing is auto-promoted.

        Variant A (shadow-only): both chapters are accepted, so the ledger
        accumulates pact across two chapters (6 occurrences) and Blake in
        chapter 0001 — but ``glossary.json`` is NOT mutated by the B9 loop
        (no ``add_observation`` call); glossary growth stays manual.
        """
        memory, out_base, result = self._run(tmp_path, monkeypatch, {
            "0001": (_CH1_HTML, "complete", [], _CH1_TRANSLATIONS),
            "0002": (_CH2_HTML, "complete", [], _CH2_TRANSLATIONS),
        })

        # Both chapters reached complete (B7 promote still runs and moves
        # only manual observations — here there are none).
        assert [r["terminal_status"] for r in result["chapters"]] == [
            "complete", "complete",
        ]

        # Per-chapter candidates blocks (B9-I2 req 4); Variant A never
        # reports promoted candidates.
        ch1, ch2 = result["chapters"]
        assert ch1["candidates"] == {"generated": 2, "promoted": 0, "conflicts": 0}
        assert ch2["candidates"] == {"generated": 1, "promoted": 0, "conflicts": 0}

        # Variant A: the B9 loop never writes to glossary.json.
        glossary = json.loads((memory / "glossary.json").read_text(encoding="utf-8"))
        assert glossary == {}

        # Ledger accumulated: pact spans both chapters with 6 total
        # occurrences; Blake only in 0001.
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

    def test_failed_chapter_never_enters_ledger(self, tmp_path, monkeypatch):
        """Review F1: failed 0001 + complete 0002 -> ledger counts 0002 only.

        Before the fix the ledger accumulated the failed chapter's aligned
        candidates, so a later complete chapter could hit the term
        chapter/occurrence thresholds on text that was never accepted.
        """
        memory, out_base, result = self._run(tmp_path, monkeypatch, {
            # Same rich text as the happy path (pact + Blake would be
            # generated), but the chapter FAILED and must contribute nothing.
            "0001": (_CH1_HTML, "failed", [], _CH1_TRANSLATIONS),
            "0002": (_CH2_HTML, "complete", [], _CH2_TRANSLATIONS),
        })

        assert [r["terminal_status"] for r in result["chapters"]] == [
            "failed", "complete",
        ]
        # The failed chapter generated no candidates at all.
        assert result["chapters"][0]["candidates"] == {
            "generated": 0, "promoted": 0, "conflicts": 0,
        }

        records = GlossaryCandidateLedger(
            str(out_base / "glossary_candidates.json")
        ).load()
        pact = records["term|pact"]
        # Only the accepted chapter is counted — 3 occurrences, one chapter,
        # so the 2-chapter/6-occurrence threshold of the old auto-promotion
        # can never be reached by failed text.
        assert pact["total_occurrences"] == 3
        assert [c["chapter_id"] for c in pact["chapters"]] == ["0002"]
        # The failed chapter's proper-name candidate never entered the ledger.
        assert "proper_name|blake" not in records

        # Variant A: nothing promoted, glossary untouched.
        glossary = json.loads((memory / "glossary.json").read_text(encoding="utf-8"))
        assert glossary == {}

    def test_quarantined_chapter_candidates_shadow_only(
        self, tmp_path, monkeypatch,
    ):
        """accepted_degraded + quarantined chunk: candidate is shadow-recorded
        with its chunk_ids; no observation, so nothing is promoted."""
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

        # The candidate was generated (shadow) but NEVER observed/promoted —
        # Variant A does not call add_observation.
        assert result["chapters"][0]["candidates"] == {
            "generated": 1, "promoted": 0, "conflicts": 0,
        }
        glossary = json.loads((memory / "glossary.json").read_text(encoding="utf-8"))
        assert glossary == {}
        observations = json.loads(
            (memory / "observations.json").read_text(encoding="utf-8")
        )
        assert observations.get("glossary", {}) == {}

        # The shadow ledger carries the candidate with its chunk provenance.
        records = GlossaryCandidateLedger(
            str(out_base / "glossary_candidates.json")
        ).load()
        assert records["proper_name|blake"]["chapters"][0]["chunk_ids"] == [
            "chunk0001",
        ]

    def test_mixed_script_allowlist_token_never_recorded(
        self, tmp_path, monkeypatch,
    ):
        """Review F3: an allowlisted token is excluded from the candidate scan
        and never recorded in the ledger; a non-allowlisted control token is."""
        html = ("<p>The lawyer Beasley handled the case. Beasley knew the law.</p>"
                "<p>Corvidae handled the papers. Corvidae signed them. Corvidae left.</p>")
        translations = {
            "p00001": "Адвокат Бизли вёл дело. Бизли знал закон.",
            "p00002": "Корвиды разбирали бумаги. Корвиды подписали их. Корвиды ушли.",
        }
        memory, out_base, result = self._run(tmp_path, monkeypatch, {
            "0001": (html, "complete", [], translations),
        }, mixed_script_allow=("corvidae",))

        records = GlossaryCandidateLedger(
            str(out_base / "glossary_candidates.json")
        ).load()
        # Corvidae (3 occurrences) would be a term candidate without the
        # allowlist; with the B5 allowlist wired through it is excluded.
        assert "term|corvidae" not in records
        # The control candidate (Beasley, not allowlisted) IS recorded.
        assert "proper_name|beasley" in records
        # Nothing auto-promoted either way.
        assert result["chapters"][0]["candidates"] == {
            "generated": 1, "promoted": 0, "conflicts": 0,
        }
        glossary = json.loads((memory / "glossary.json").read_text(encoding="utf-8"))
        assert glossary == {}


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
            "--candidate-mixed-script-allow", "corvidae",
            "--candidate-mixed-script-allow", "R.D.T.",
            "--run-label", "test-run",  # unknown to book_run -> strict driver
        ])
        assert args.candidates_ledger == Path("led.json")
        assert args.term_min_occurrences == 4
        assert args.term_min_chapters == 3
        assert args.proper_name_min_occurrences == 5
        assert args.consensus_ratio == 0.9
        assert args.candidate_mixed_script_allow == ["corvidae", "R.D.T."]
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
        assert args.candidate_mixed_script_allow is None
