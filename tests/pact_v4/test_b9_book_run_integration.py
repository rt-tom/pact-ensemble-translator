"""B9 integration tests: candidate generation/ledger/promotion in the book run.

Covers the B9 card requirements under the owner decision recorded in
DECISIONS.md (2026-08-04 — Variant B: auto-promotion stays, with v3
thresholds and strict source->target evidence; B9-RV2 review of PR #128):

* ``run_book`` calls the generator + consensus alignment after each chapter
  (source = chapter HTML, translation = ``out_dir/translations.json``) and
  appends to the ledger (default ``<out_base>/glossary_candidates.json``)
  BEFORE ``MemoryManager.promote``;
* ONLY chapters with an accepted terminal result (``complete`` /
  ``accepted_degraded``) contribute to the ledger or to promotion — a failed
  chapter's observations must never satisfy later thresholds (review F1);
* candidates that meet the v3 thresholds with a single aligned target are
  auto-promoted through ``MemoryManager.add_observation`` -> the B7
  ``promote`` path (Variant B); the per-chapter ``candidates`` block records
  ``{generated, proposed, committed, conflicts}``:
    - ``proposed`` — aligned records sent to ``add_observation`` this
      chapter (thresholds met, single target, no established conflict);
    - ``committed`` — how many of those actually landed in ``glossary.json``
      after ``promote`` (the B7 quarantined-chunk filter can drop proposed
      observations on ``accepted_degraded``, so ``committed`` may be smaller
      than ``proposed``; ``complete`` promotes all observations);
    - ``conflicts`` — aligned records NOT proposed because of an alignment
      conflict, a cumulative ledger target conflict (chapters resolved the
      source to different targets), or an established glossary entry with a
      different target.
* the B5 combined mixed-script allowlist (bible + glossary + manual +
  source-derived) is threaded into candidate generation — an allowlisted
  token is never recorded (review F3);
* the promoted glossary entries are FLAT ``{source: target}`` on disk.

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
    (memory / "glossary.json").write_text("{}\n", encoding="utf-8")
    (memory / "book_memory.json").write_text(
        json.dumps({"pov": {"gender": "male"}}, ensure_ascii=False),
        encoding="utf-8",
    )
    (memory / "observations.json").write_text("{}\n", encoding="utf-8")
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
# Two-chapter accumulation + promotion (B9 Variant B)
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

# B9-F3 regression fixtures: two accepted chapters that resolve the SAME
# term ("pact") to DIFFERENT targets (договор in 0001, пакт in 0002). pact
# is the only candidate in each chapter (all other tokens occur once).

_CH1_DISAGREE_HTML = """<p>The pact was sealed with blood.</p>
<p>The pact bound them all.</p>
<p>The pact held for years.</p>
<p>He walked home alone.</p>
<p>She closed the heavy door.</p>"""

_CH1_DISAGREE_TRANSLATIONS = {
    "p00001": "Договор был скреплён кровью.",
    "p00002": "Договор связывал их всех.",
    "p00003": "Договор держался годами.",
    "p00004": "Он пошёл домой один.",
    "p00005": "Она закрыла тяжёлую дверь.",
}

_CH2_DISAGREE_HTML = """<p>The pact grew stronger with time.</p>
<p>The pact never broke its bond.</p>
<p>The pact endured through the ages.</p>
<p>He walked home in the dark.</p>
<p>She opened the heavy door.</p>"""

_CH2_DISAGREE_TRANSLATIONS = {
    "p00001": "Пакт становился сильнее со временем.",
    "p00002": "Пакт никогда не разрывал свою связь.",
    "p00003": "Пакт выстоял сквозь века.",
    "p00004": "Он пошёл домой в темноте.",
    "p00005": "Она открыла тяжёлую дверь.",
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

    def test_two_chapters_accumulate_and_auto_promote(
        self, tmp_path, monkeypatch,
    ):
        """Candidates accumulate in the ledger and auto-promote (Variant B).

        Both chapters are accepted, so the ledger accumulates pact across two
        chapters (6 occurrences) and Blake in chapter 0001. Blake meets the
        proper_name threshold in chapter 0001 (4 occurrences >= 2, single
        target) and is proposed+committed there; pact meets the term
        threshold (2 chapters AND 6 occurrences) only in chapter 0002 and is
        proposed+committed there. ``glossary.json`` ends up with both flat
        ``{source: target}`` entries; ``observations.json`` is cleared by
        ``promote``.
        """
        memory, out_base, result = self._run(tmp_path, monkeypatch, {
            "0001": (_CH1_HTML, "complete", [], _CH1_TRANSLATIONS),
            "0002": (_CH2_HTML, "complete", [], _CH2_TRANSLATIONS),
        })

        # Both chapters reached complete (B7 promote runs each time).
        assert [r["terminal_status"] for r in result["chapters"]] == [
            "complete", "complete",
        ]

        # Per-chapter candidates blocks (Variant B semantics):
        #   ch1: Blake proposed+committed; pact is term with 1 chapter < 2.
        #   ch2: pact proposed+committed; Blake already in glossary, so it is
        #        excluded from generation (glossary exclusions).
        ch1, ch2 = result["chapters"]
        assert ch1["candidates"] == {
            "generated": 2, "proposed": 1, "committed": 1, "conflicts": 0,
        }
        assert ch2["candidates"] == {
            "generated": 1, "proposed": 1, "committed": 1, "conflicts": 0,
        }

        # Variant B: the B9 loop promoted both candidates into glossary.json,
        # stored flat {source: target} after _flatten_promoted_glossary.
        glossary = json.loads((memory / "glossary.json").read_text(encoding="utf-8"))
        assert glossary == {"Blake": "Блэйк", "pact": "пакт"}

        # observations.json is emptied by promote after each chapter.
        observations = json.loads(
            (memory / "observations.json").read_text(encoding="utf-8")
        )
        assert observations.get("glossary", {}) == {}

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

    def test_cross_chapter_target_disagreement_never_promotes(
        self, tmp_path, monkeypatch,
    ):
        """B9-F3 regression (review finding, HIGH): two accepted chapters
        that resolve the SAME term to DIFFERENT targets must leave the
        ledger in conflict and never auto-promote.

        ch0001 aligns pact -> договор, ch0002 aligns pact -> пакт. The
        cumulative ledger then has ``targets_seen`` [договор, пакт] and a
        merged ``target`` of None (irreversible), so pact's thresholds are
        met (2 chapters, 6 occurrences) but the cross-chapter disagreement
        must surface as a conflict: no proposal, no commit, glossary.json
        unchanged. Before the fix ``_auto_promote_glossary`` decided
        promotion from the current chapter's aligned record alone and ch0002
        proposed+committed pact -> пакт, persisting an ambiguous mapping.
        """
        memory, out_base, result = self._run(tmp_path, monkeypatch, {
            "0001": (_CH1_DISAGREE_HTML, "complete", [],
                     _CH1_DISAGREE_TRANSLATIONS),
            "0002": (_CH2_DISAGREE_HTML, "complete", [],
                     _CH2_DISAGREE_TRANSLATIONS),
        })

        ch1, ch2 = result["chapters"]
        # ch0001: pact is a single-chapter term (< 2 chapters) -> below the
        # term threshold, neither proposed nor conflicting.
        assert ch1["candidates"] == {
            "generated": 1, "proposed": 0, "committed": 0, "conflicts": 0,
        }
        # ch0002: pact meets the thresholds, but the cumulative ledger
        # records the cross-chapter target disagreement -> conflict, never
        # proposed, never committed.
        assert ch2["candidates"] == {
            "generated": 1, "proposed": 0, "committed": 0, "conflicts": 1,
        }

        # The ambiguous mapping never reached glossary.json.
        glossary = json.loads((memory / "glossary.json").read_text(encoding="utf-8"))
        assert glossary == {}

        # The ledger retains the conflict for human review: no merged
        # target, both distinct chapter targets in conflicts.
        records = GlossaryCandidateLedger(
            str(out_base / "glossary_candidates.json")
        ).load()
        pact = records["term|pact"]
        assert pact["target"] is None
        assert pact["total_occurrences"] == 6
        assert {c["chapter_id"] for c in pact["chapters"]} == {"0001", "0002"}
        assert set(pact["conflicts"]) == {"договор", "пакт"}

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
            "generated": 0, "proposed": 0, "committed": 0, "conflicts": 0,
        }
        # pact is generated in 0002 but with a single chapter it does not
        # meet the term threshold (>= 2 chapters), so it is neither proposed
        # nor committed.
        assert result["chapters"][1]["candidates"] == {
            "generated": 1, "proposed": 0, "committed": 0, "conflicts": 0,
        }

        records = GlossaryCandidateLedger(
            str(out_base / "glossary_candidates.json")
        ).load()
        pact = records["term|pact"]
        # Only the accepted chapter is counted — 3 occurrences, one chapter,
        # so the 2-chapter/6-occurrence threshold can never be reached by
        # failed text.
        assert pact["total_occurrences"] == 3
        assert [c["chapter_id"] for c in pact["chapters"]] == ["0002"]
        # The failed chapter's proper-name candidate never entered the ledger.
        assert "proper_name|blake" not in records

        # Nothing proposed -> glossary untouched.
        glossary = json.loads((memory / "glossary.json").read_text(encoding="utf-8"))
        assert glossary == {}

    def test_quarantined_chunk_proposed_gt_committed(
        self, tmp_path, monkeypatch,
    ):
        """accepted_degraded + quarantined chunk: proposed > committed.

        The candidate is wholly in chunk0001, which is quarantined. It meets
        the proper_name threshold and is PROPOSED (sent to
        ``add_observation`` with chunk_id=chunk0001), but the B7
        ``promote(accepted_degraded, quarantined_chunks=[chunk0001])`` filter
        drops the observation, so it is NOT committed to ``glossary.json``:
        ``proposed == 1 > committed == 0`` (B9 card semantics).
        """
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

        candidates = result["chapters"][0]["candidates"]
        assert candidates == {
            "generated": 1, "proposed": 1, "committed": 0, "conflicts": 0,
        }
        # The whole point of the split semantics: proposed > committed.
        assert candidates["proposed"] > candidates["committed"]

        # The quarantined observation never reached glossary.json.
        glossary = json.loads((memory / "glossary.json").read_text(encoding="utf-8"))
        assert glossary == {}
        # observations.json was cleared by promote.
        observations = json.loads(
            (memory / "observations.json").read_text(encoding="utf-8")
        )
        assert observations.get("glossary", {}) == {}

        # The shadow ledger still carries the candidate with its chunk
        # provenance (shadow data for manual review).
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
        and never recorded in the ledger; a non-allowlisted control token is.
        """
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
        # The control candidate (Beasley, not allowlisted) IS recorded and
        # meets the proper_name threshold, so it is proposed and committed.
        assert "proper_name|beasley" in records
        assert result["chapters"][0]["candidates"] == {
            "generated": 1, "proposed": 1, "committed": 1, "conflicts": 0,
        }
        glossary = json.loads((memory / "glossary.json").read_text(encoding="utf-8"))
        assert glossary == {"Beasley": "Адвокат"}


# ---------------------------------------------------------------------------
# B9-F3: _auto_promote_glossary guards (direct helper tests)
# ---------------------------------------------------------------------------


class _RecordingManager:
    """MemoryManager stand-in that records ``add_observation`` calls."""

    def __init__(self):
        self.calls = []

    def add_observation(self, category, key, value):
        self.calls.append((category, key, value))


class TestAutoPromoteGlossary:
    def _aligned_pact(self, target, conflicts=()):
        return [{
            "source": "pact", "kind": "term", "occurrences": 3,
            "chunk_ids": ["chunk0002"], "context": "The pact held.",
            "variants": {target: 3}, "target": target,
            "consensus_share": 1.0, "conflicts": list(conflicts),
        }]

    def test_cumulative_target_conflict_never_proposed(self):
        """Direct reproduction of the B9-RV2 finding: a cumulative ledger
        record with ``target`` None (targets_seen disagreement) plus a
        current aligned target must be reported as a conflict and never sent
        to ``add_observation`` — the record's ``total_occurrences`` and
        ``chapters`` alone must not drive promotion.
        """
        from pact_full_pipeline_runner_v1 import v4_book_run

        manager = _RecordingManager()
        merged_ledger = {
            v4_book_run.candidate_key("pact", "term"): {
                "source": "pact", "kind": "term", "total_occurrences": 6,
                "chapters": [
                    {"chapter_id": "0001", "chunk_ids": [], "count": 3},
                    {"chapter_id": "0002", "chunk_ids": [], "count": 3},
                ],
                "variants": {"договор": 3, "пакт": 3},
                "target": None,
                "targets_seen": ["договор", "пакт"],
                "conflicts": ["договор", "пакт"],
            },
        }

        proposed, conflicts = v4_book_run._auto_promote_glossary(
            manager, self._aligned_pact("пакт"), merged_ledger, {},
            term_min_chapters=2, term_min_occurrences=3,
            proper_name_min_occurrences=2,
        )
        assert proposed == []
        assert len(conflicts) == 1
        assert set(conflicts[0]["cumulative_targets"]) == {"договор", "пакт"}
        assert manager.calls == []

    def test_established_glossary_conflict_never_proposed(self):
        """The established-glossary guard: a candidate whose source already
        has a DIFFERENT target in glossary.json is a conflict, never
        proposed (defensive — ``run_book`` generation excludes glossary keys
        upstream, so this branch is reachable only through direct use).
        """
        from pact_full_pipeline_runner_v1 import v4_book_run

        manager = _RecordingManager()
        merged_ledger = {
            v4_book_run.candidate_key("pact", "term"): {
                "source": "pact", "kind": "term", "total_occurrences": 6,
                "chapters": [
                    {"chapter_id": "0001", "chunk_ids": [], "count": 3},
                    {"chapter_id": "0002", "chunk_ids": [], "count": 3},
                ],
                "variants": {"пакт": 6},
                "target": "пакт",
                "targets_seen": ["пакт"],
                "conflicts": [],
            },
        }

        proposed, conflicts = v4_book_run._auto_promote_glossary(
            manager, self._aligned_pact("пакт"), merged_ledger,
            {"pact": "договор"},
            term_min_chapters=2, term_min_occurrences=3,
            proper_name_min_occurrences=2,
        )
        assert proposed == []
        assert len(conflicts) == 1
        assert conflicts[0]["established_target"] == "договор"
        assert manager.calls == []

    def test_unambiguous_cumulative_target_still_proposes(self):
        """The guard must not block the happy path: a ledger record with a
        single distinct target consistent with the current chapter's aligned
        target still proposes (v3 thresholds met).
        """
        from pact_full_pipeline_runner_v1 import v4_book_run

        manager = _RecordingManager()
        merged_ledger = {
            v4_book_run.candidate_key("pact", "term"): {
                "source": "pact", "kind": "term", "total_occurrences": 6,
                "chapters": [
                    {"chapter_id": "0001", "chunk_ids": [], "count": 3},
                    {"chapter_id": "0002", "chunk_ids": [], "count": 3},
                ],
                "variants": {"пакт": 6},
                "target": "пакт",
                "targets_seen": ["пакт"],
                "conflicts": [],
            },
        }

        proposed, conflicts = v4_book_run._auto_promote_glossary(
            manager, self._aligned_pact("пакт"), merged_ledger, {},
            term_min_chapters=2, term_min_occurrences=3,
            proper_name_min_occurrences=2,
        )
        assert len(proposed) == 1
        assert conflicts == []
        assert manager.calls == [
            ("glossary", "pact",
             {"target": "пакт", "type": "term", "chunk_id": "chunk0002"}),
        ]


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
