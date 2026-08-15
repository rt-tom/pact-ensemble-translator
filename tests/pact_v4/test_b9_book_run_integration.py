"""B9 integration tests: candidate generation/ledger/promotion in the book run.

Covers the B9 card requirements under the owner decision recorded in
DECISIONS.md (2026-08-04 — V-final: auto-promotion stays, with v3
thresholds and strict source->target evidence; B9-RV2/B9-RV3 reviews of
PR #128, B9-F2/F3/F5/F6 follow-ups):

* ``run_book`` calls the generator + consensus alignment after each chapter
  (source = chapter HTML, translation = ``out_dir/translations.json``) and
  appends to the ledger (default ``<out_base>/glossary_candidates.json``)
  BEFORE ``MemoryManager.promote``;
* ONLY chapters with an accepted terminal result (``complete`` /
  ``accepted_degraded``) contribute to the ledger or to promotion — a failed
  chapter's observations must never satisfy later thresholds (review F1);
* candidates that meet the v3 thresholds with a single aligned target are
  auto-promoted through ``MemoryManager.add_observation`` -> the B7
  ``promote`` path (V-final); the per-chapter ``candidates`` block records
  ``{generated, proposed, committed, conflicts}``:
    - ``proposed`` — aligned records sent to ``add_observation`` this
      chapter (thresholds met, single target, no established conflict);
    - ``committed`` — how many of those actually landed in ``glossary.json``
      after ``promote``. For B9-generated observations ``committed ==
      proposed`` for ``complete`` AND ``accepted_degraded`` (valid plan):
      quarantined pids are excluded before generation (B9-RV3, F5/F6
      fail-closed), so proposed observations carry only accepted
      ``chunk_id``s that the B7 filter keeps; the B7 quarantined filter stays
      defense-in-depth and can lower ``committed`` only for independent
      (e.g. manual) observations carrying a quarantined ``chunk_id``);
    - ``conflicts`` — aligned records NOT proposed because of an alignment
      conflict, a cumulative ledger target conflict (chapters resolved the
      source to different targets, B9-F3), or an established glossary entry
      with a different target.
* quarantined-chunk evidence is excluded BEFORE ledger accumulation and
  auto-promotion (B9-RV3): pids from quarantined chunks never generate
  candidates, a candidate wholly from a quarantined chunk has no ledger line
  and cannot promote, and a mixed candidate counts only its accepted-chunk
  occurrences;
* B9-F5/F6 fail-closed: ``accepted_degraded`` + quarantined chunk + a
  missing/corrupt/empty/incomplete/ambiguous ``chunk_plan.json`` yields ZERO
  candidate generation, no ledger line, no observation and no glossary
  mutation — unavailable or non-authoritative PID->chunk provenance never
  lets unproven evidence through (the run never crashes);
* the B5 combined mixed-script allowlist (bible + glossary + manual +
  source-derived) comes from the real book-run ``--mixed-script-allow`` flag
  (same input as the strict driver, no divergent duplicate flag) — an
  allowlisted token is never recorded and cannot promote (reviews F3/RV3);
* the promoted glossary entries are FLAT ``{source: target}`` on disk;
* strict conservative term alignment: co-occurring unrelated terms (e.g.
  ``bound``/``together`` next to ``pact``) never share a target and never
  promote (B9-RV2/RV3).

The strict driver is faked (``_run_one_chapter`` monkeypatched) — the out_dir
artifacts (``strict_chapter_trial_record.json``, ``selection_results.json``,
``translations.json``, ``chunk_plan.json``) are pre-populated on disk, exactly
like the existing B7 wrapper tests.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from pact_v4.phase1.glossary_candidates import GlossaryCandidateLedger


def _setup_memory(tmp_path: Path, book_memory_bytes: bytes | None = None) -> Path:
    memory = tmp_path / "memory"
    memory.mkdir(parents=True, exist_ok=True)
    (memory / "glossary.json").write_text("{}", encoding="utf-8")
    if book_memory_bytes is None:
        (memory / "book_memory.json").write_text(
            json.dumps({"pov": {"gender": "male"}}, ensure_ascii=False),
            encoding="utf-8",
        )
    else:
        # Deliberately noncanonical formatting (compact, no indent) so the
        # regression can detect any read-modify-write reformatting by bytes.
        (memory / "book_memory.json").write_bytes(book_memory_bytes)
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
# Two-chapter accumulation + promotion (B9 V-final)
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
             mixed_script_allow=(), book_memory_bytes=None, **run_kwargs):
        from pact_full_pipeline_runner_v1 import v4_book_run

        memory = _setup_memory(tmp_path, book_memory_bytes=book_memory_bytes)
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
            **run_kwargs,
        )
        return memory, out_base, result

    def test_two_chapters_accumulate_and_auto_promote(
        self, tmp_path, monkeypatch,
    ):
        """Candidates accumulate in the ledger and auto-promote (V-final).

        Both chapters are accepted, so the ledger accumulates pact across two
        chapters (6 occurrences) and Blake in chapter 0001. Blake meets the
        proper_name threshold in chapter 0001 (4 occurrences >= 2, single
        target) and is proposed+committed there; pact is a generic TERM and
        NEVER auto-promotes (owner decision 2026-08-14: frequency+stability
        is not terminology) — it stays in the ledger only.
        ``glossary.json`` ends up with only the Blake entry;
        ``observations.json`` is cleared by ``promote``.
        """
        memory, out_base, result = self._run(tmp_path, monkeypatch, {
            "0001": (_CH1_HTML, "complete", [], _CH1_TRANSLATIONS),
            "0002": (_CH2_HTML, "complete", [], _CH2_TRANSLATIONS),
        })

        # Both chapters reached complete (B7 promote runs each time).
        assert [r["terminal_status"] for r in result["chapters"]] == [
            "complete", "complete",
        ]

        # Per-chapter candidates blocks (P0 term-promotion-OFF semantics):
        #   ch1: Blake proposed+committed; pact generated (term) but NEVER
        #        proposed.
        #   ch2: pact generated again; Blake already in glossary, so it is
        #        excluded from generation (glossary exclusions). pact still
        #        never proposes.
        ch1, ch2 = result["chapters"]
        assert ch1["candidates"] == {
            "generated": 2, "proposed": 1, "committed": 1, "conflicts": 0,
        }
        assert ch2["candidates"] == {
            "generated": 1, "proposed": 0, "committed": 0, "conflicts": 0,
        }

        # V-final: only the proper_name promoted into the flat glossary.
        glossary = json.loads((memory / "glossary.json").read_text(encoding="utf-8"))
        assert glossary == {"Blake": "Блэйк"}

        # observations.json is emptied by promote after each chapter.
        observations = json.loads(
            (memory / "observations.json").read_text(encoding="utf-8")
        )
        assert observations.get("glossary", {}) == {}

        # Ledger accumulated: pact spans both chapters with 6 total
        # occurrences; Blake only in 0001. The ledger is the observation
        # store — pact lives there even though it never auto-promotes.
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

    def test_complete_glossary_promotion_preserves_book_memory_bytes(
        self, tmp_path, monkeypatch,
    ):
        """B9-RV9 HIGH regression: a glossary-only promotion must NOT
        read-modify-write ``book_memory.json``.

        The fixture uses a PROPER_NAME-only chapter (``Beasley``,
        capitalized — a proper name never becomes a book_memory character
        via the deterministic script, which is OFF; the entity extractor is
        the only character source, and the mocked chapter has no
        entity_context_cache.json) promoted with the 2-occurrence
        proper_name threshold, to keep the byte-preservation invariant
        testable: the glossary category changes while book_memory has NO
        observations, so its bytes (and the recorded per-chapter
        ``_book_memory_hash``) must stay exactly the same.
        """
        term_html = (
            "<p>He met Beasley at the office. Beasley knew the law.</p>\n"
            "<p>Beasley signed the papers.</p>\n"
            "<p>Beasley left at noon.</p>"
        )
        term_translations = {
            "p00001": "Он встретил Бизли в офисе. Бизли знал закон.",
            "p00002": "Бизли подписал бумаги.",
            "p00003": "Бизли ушёл в полдень.",
        }
        book_memory_bytes = b'{"pov":{"gender":"male"}}'
        memory, out_base, result = self._run(
            tmp_path, monkeypatch, {
                "0001": (term_html, "complete", [], term_translations),
            },
            book_memory_bytes=book_memory_bytes,
        )

        # Sanity: this IS a real glossary promotion (Beasley proper_name, 3
        # occurrences >= 2, single target) and book_memory got no entity
        # observations (no entity_context_cache.json in the mocked chapter).
        glossary = json.loads((memory / "glossary.json").read_text(encoding="utf-8"))
        assert glossary == {"Beasley": "Бизли"}
        assert result["chapters"][0]["book_memory_candidates"] == {
            "generated": 0, "proposed": 0, "committed": 0, "conflicts": 0,
        }

        # Exact bytes preserved — no read-modify-write reformatting.
        assert (memory / "book_memory.json").read_bytes() == book_memory_bytes

        # The recorded per-chapter hashes are unchanged and still match the
        # original raw bytes.
        rec = result["chapters"][0]
        assert rec["book_memory_hash_before"] == rec["book_memory_hash_after"]
        assert rec["book_memory_hash_after"] == hashlib.sha256(
            book_memory_bytes
        ).hexdigest()

    def test_accepted_degraded_glossary_promotion_preserves_book_memory_bytes(
        self, tmp_path, monkeypatch,
    ):
        """B9-RV9 HIGH regression, accepted_degraded valid-plan path.

        An ``accepted_degraded`` chapter WITHOUT quarantined chunks (valid
        PID->chunk plan, no exclusion needed — the F5/F6 fail-closed branch
        does not engage) promotes its glossary proper_name candidate while
        leaving ``book_memory.json`` bytes and ``_book_memory_hash``
        untouched (same proper_name-only fixture as the complete case — see
        the sibling test).
        """
        term_html = (
            "<p>He met Beasley at the office. Beasley knew the law.</p>\n"
            "<p>Beasley signed the papers.</p>\n"
            "<p>Beasley left at noon.</p>"
        )
        term_translations = {
            "p00001": "Он встретил Бизли в офисе. Бизли знал закон.",
            "p00002": "Бизли подписал бумаги.",
            "p00003": "Бизли ушёл в полдень.",
        }
        book_memory_bytes = b'{"pov":{"gender":"male"}}'
        memory, out_base, result = self._run(
            tmp_path, monkeypatch, {
                "0001": (term_html, "accepted_degraded", [], term_translations),
            },
            book_memory_bytes=book_memory_bytes,
        )

        # Sanity: the accepted_degraded valid-plan chapter still promotes.
        glossary = json.loads((memory / "glossary.json").read_text(encoding="utf-8"))
        assert glossary == {"Beasley": "Бизли"}

        # Exact bytes preserved — no read-modify-write reformatting.
        assert (memory / "book_memory.json").read_bytes() == book_memory_bytes

        rec = result["chapters"][0]
        assert rec["book_memory_hash_before"] == rec["book_memory_hash_after"]
        assert rec["book_memory_hash_after"] == hashlib.sha256(
            book_memory_bytes
        ).hexdigest()

    def test_cross_chapter_target_disagreement_never_promotes(
        self, tmp_path, monkeypatch,
    ):
        """B9-F3 regression (review finding, HIGH): two accepted chapters
        that resolve the SAME term to DIFFERENT targets must leave the
        ledger in conflict and never auto-promote.

        ch0001 aligns pact -> договор, ch0002 aligns pact -> пакт. The
        cumulative ledger then has ``targets_seen`` [договор, пакт] and a
        merged ``target`` of None (irreversible). P0 owner decision
        2026-08-14 also turns generic-term auto-promotion OFF entirely, so
        pact is NEVER proposed or committed in either chapter (it is not
        even evaluated as a conflict — the term branch is skipped before
        the conflict check); glossary.json stays unchanged and the ledger
        retains the disagreement for human review.
        """
        memory, out_base, result = self._run(tmp_path, monkeypatch, {
            "0001": (_CH1_DISAGREE_HTML, "complete", [],
                     _CH1_DISAGREE_TRANSLATIONS),
            "0002": (_CH2_DISAGREE_HTML, "complete", [],
                     _CH2_DISAGREE_TRANSLATIONS),
        })

        ch1, ch2 = result["chapters"]
        # pact is a generic term — auto-promotion is OFF, so it is never
        # proposed nor reported as a conflict in either chapter.
        assert ch1["candidates"] == {
            "generated": 1, "proposed": 0, "committed": 0, "conflicts": 0,
        }
        assert ch2["candidates"] == {
            "generated": 1, "proposed": 0, "committed": 0, "conflicts": 0,
        }

        # The ambiguous mapping never reached glossary.json.
        glossary = json.loads((memory / "glossary.json").read_text(encoding="utf-8"))
        assert glossary == {}

        # The ledger retains the disagreement for human review: no merged
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

    def test_quarantined_candidate_never_enters_ledger_or_promotes(
        self, tmp_path, monkeypatch,
    ):
        """B9-RV3: accepted_degraded + quarantined chunk: a candidate wholly
        from a quarantined chunk has NO ledger line and NO promotion.

        The old (cancelled Variant A shadow-only policy) test asserted the
        candidate WAS shadow-recorded; B9-RV3 requires quarantined-chunk
        evidence to be excluded BEFORE ledger accumulation and
        auto-promotion.
        """
        html = """<p>He met Blake at the gate. Blake knew the way.</p>
<p>Blake waited outside for Mary.</p>"""
        translations = {
            "p00001": "Блэйк встретил Блэйка у ворот. Блэйк знал дорогу.",
            "p00002": "Блэйк ждал снаружи Мэри.",
        }
        memory, out_base, result = self._run(tmp_path, monkeypatch, {
            "0001": (html, "accepted_degraded", ["chunk0001"], translations),
        })

        # The whole chapter is one quarantined chunk — the candidate's
        # evidence is entirely quarantined, so nothing is generated, nothing
        # enters the ledger, nothing promotes.
        assert result["chapters"][0]["candidates"] == {
            "generated": 0, "proposed": 0, "committed": 0, "conflicts": 0,
        }
        glossary = json.loads((memory / "glossary.json").read_text(encoding="utf-8"))
        assert glossary == {}
        observations = json.loads(
            (memory / "observations.json").read_text(encoding="utf-8")
        )
        assert observations.get("glossary", {}) == {}

        records = GlossaryCandidateLedger(
            str(out_base / "glossary_candidates.json")
        ).load()
        assert "proper_name|blake" not in records

    def test_mixed_candidate_counts_only_accepted_chunks(
        self, tmp_path, monkeypatch,
    ):
        """B9-RV3: a candidate spanning accepted + quarantined chunks counts
        only its accepted-chunk occurrences; its ledger entry and promotion
        provenance carry only accepted chunks."""
        html = ("<p>He met Blake at the gate. Blake knew the way.</p>\n"
                "<p>Blake waited outside for Mary.</p>\n"
                "<p>Blake returned home later.</p>\n"
                "<p>He saw Blake return home. Blake waved once more.</p>\n"
                "<p>He met Blake again.</p>\n"
                "<p>Blake left at noon.</p>")
        translations = {
            "p00001": "Блэйк встретил Блэйка у ворот. Блэйк знал дорогу.",
            "p00002": "Блэйк ждал снаружи Мэри.",
            "p00003": "Блэйк вернулся домой позже.",
            "p00004": "Он видел, как Блэйк вернулся домой. Блэйк помахал ещё раз.",
            "p00005": "Он снова встретил Блэйка.",
            "p00006": "Блэйк ушёл в полдень.",
        }
        # _PLAN: chunk0001 (p00001-p00003) is quarantined; chunk0002
        # (p00004-p00006) is accepted. Blake has 8 raw occurrences, 4 in each
        # chunk. Only the accepted chunk's 4 occurrences (with mid-sentence
        # capitals) count.
        memory, out_base, result = self._run(tmp_path, monkeypatch, {
            "0001": (html, "accepted_degraded", ["chunk0001"], translations),
        })

        # Only the accepted chunk's evidence counts: 4 occurrences, so Blake
        # still meets the proper_name threshold (2) and promotes with accepted
        # provenance only.
        assert result["chapters"][0]["candidates"] == {
            "generated": 1, "proposed": 1, "committed": 1, "conflicts": 0,
        }
        records = GlossaryCandidateLedger(
            str(out_base / "glossary_candidates.json")
        ).load()
        blake = records["proper_name|blake"]
        assert blake["total_occurrences"] == 4  # not 8
        assert blake["chapters"][0]["chunk_ids"] == ["chunk0002"]
        # Promoted into the flat glossary (chunk_id = chunk0002 is accepted,
        # so the B7 quarantined filter keeps it).
        glossary = json.loads((memory / "glossary.json").read_text(encoding="utf-8"))
        assert glossary == {"Blake": "Блэйк"}

    def test_co_occurring_terms_never_promote_false_pair(
        self, tmp_path, monkeypatch,
    ):
        """B9-RV2/RV3: bound/together next to pact never share 'пакт'.

        The frequency-contrast heuristic cannot tell "the candidate's
        translation" from "a word that merely co-occurs" — when all term
        candidates dominate on the same Russian variant, EVERY candidate
        loses the target. None of pact/bound/together may promote as «пакт».
        """
        html = ("<p>The pact bound them all together.</p>\n"
                "<p>The pact bound them all together.</p>\n"
                "<p>The pact bound them all together.</p>\n"
                "<p>The others watched from afar.</p>\n"
                "<p>The others watched from afar.</p>\n"
                "<p>The others watched from afar.</p>")
        translations = {
            "p00001": "Пакт связывал их всех вместе.",
            "p00002": "Пакт связывал их всех вместе.",
            "p00003": "Пакт связывал их всех вместе.",
            "p00004": "Остальные наблюдали издалека.",
            "p00005": "Остальные наблюдали издалека.",
            "p00006": "Остальные наблюдали издалека.",
        }
        memory, out_base, result = self._run(tmp_path, monkeypatch, {
            "0001": (html, "complete", [], translations),
        })

        # pact/bound/together (and the contrasting others/watched/afar) all
        # co-occur in the same pids and all dominate on a shared variant —
        # they are generic TERMS, and P0 (2026-08-14) turns term
        # auto-promotion OFF entirely: nothing can promote, and terms are
        # not even evaluated as conflicts (the term branch is skipped before
        # the conflict check).
        assert result["chapters"][0]["candidates"] == {
            "generated": 6, "proposed": 0, "committed": 0, "conflicts": 0,
        }
        glossary = json.loads((memory / "glossary.json").read_text(encoding="utf-8"))
        assert glossary == {}
        # The ledger records the evidence (for the human reviewer) but every
        # candidate keeps target=None and «пакт» in conflicts.
        records = GlossaryCandidateLedger(
            str(out_base / "glossary_candidates.json")
        ).load()
        for key in ("term|pact", "term|bound", "term|together"):
            record = records[key]
            assert record["target"] is None
            assert "пакт" in record["conflicts"]

    def test_mixed_script_allowlist_token_never_recorded(
        self, tmp_path, monkeypatch,
    ):
        """Reviews F3/RV3: an allowlisted token is excluded from the candidate
        scan and never recorded or promoted; a non-allowlisted control token
        is."""
        html = ("<p>The lawyer Beasley handled the case. Beasley knew the law.</p>"
                "<p>Corvidae handled the papers. Corvidae signed them. Corvidae left.</p>")
        translations = {
            "p00001": "Бизли вёл дело. Бизли знал закон.",
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
        # promotes under V-final (2 proper_name occurrences, single target).
        assert "proper_name|beasley" in records
        assert result["chapters"][0]["candidates"] == {
            "generated": 1, "proposed": 1, "committed": 1, "conflicts": 0,
        }
        glossary = json.loads((memory / "glossary.json").read_text(encoding="utf-8"))
        assert glossary == {"Beasley": "Бизли"}

    def test_b5_mixed_script_flag_excludes_candidate_and_reaches_strict_driver(
        self, tmp_path, monkeypatch,
    ):
        """B9-RV3: the REAL B5 book-run flag --mixed-script-allow excludes a
        token from the ledger/promotion AND is re-forwarded to the strict
        driver — no divergent duplicate candidate-only flag."""
        from pact_full_pipeline_runner_v1 import v4_book_run

        memory = _setup_memory(tmp_path)
        out_base = tmp_path / "out"
        src_dir = tmp_path / "src"
        html = ("<p>The lawyer Beasley handled the case. Beasley knew the law.</p>"
                "<p>Corvidae handled the papers. Corvidae signed them. Corvidae left.</p>")
        translations = {
            "p00001": "Бизли вёл дело. Бизли знал закон.",
            "p00002": "Корвиды разбирали бумаги. Корвиды подписали их. Корвиды ушли.",
        }
        _write_chapter_html(src_dir, "0001", html)
        _make_chapter_artifacts(
            out_base / "chapter_0001", "0001",
            terminal_status="complete", quarantined=[],
            translations=translations, chunk_plan=_PLAN,
        )

        captured = {}

        def fake_run_one(chapter_id, *, memory_dir, chapter_html_path,
                         out_dir, extra_args=()):
            captured["extra_args"] = list(extra_args)
            return {"status": "ok"}

        monkeypatch.setattr(v4_book_run, "_run_one_chapter", fake_run_one)

        rc = v4_book_run.main([
            "--memory-dir", str(memory),
            "--chapters", "0001",
            "--chapter-html-pattern", str(src_dir / "{chapter_id}.html"),
            "--out-base", str(out_base),
            "--mixed-script-allow", "corvidae",
        ])
        assert rc == 0
        # The B5 manual flag is forwarded verbatim to the strict driver.
        assert captured["extra_args"] == ["--mixed-script-allow", "corvidae"]
        # And it excluded the token from the B9 candidate ledger.
        records = GlossaryCandidateLedger(
            str(out_base / "glossary_candidates.json")
        ).load()
        assert "term|corvidae" not in records
        assert "proper_name|beasley" in records


# ---------------------------------------------------------------------------
# B9-F5/F6: fail closed on unavailable / ambiguous quarantined-chunk provenance
# ---------------------------------------------------------------------------


class TestQuarantinedProvenanceFailClosed:
    """B9-F5/F6: accepted_degraded + quarantined chunks must fail closed when
    ``chunk_plan.json`` cannot authoritatively exclude the quarantined
    evidence.

    A missing, corrupt, empty, incomplete, or ambiguous (duplicate
    PID/chunk ownership) PID->chunk plan leaves source/translation pids of
    unknown provenance — they could belong to a quarantined chunk — so the
    chapter must generate zero candidates, append no ledger line, create no
    observation and mutate no glossary (the book run never crashes).
    """

    def _run_plan_mode(self, tmp_path, monkeypatch, plan_mode):
        from pact_full_pipeline_runner_v1 import v4_book_run

        memory = _setup_memory(tmp_path)
        out_base = tmp_path / "out"
        src_dir = tmp_path / "src"

        def fake_run_one(*args, **kwargs):
            return {"status": "ok"}

        monkeypatch.setattr(v4_book_run, "_run_one_chapter", fake_run_one)

        _write_chapter_html(src_dir, "0001", _CH1_HTML)
        out_dir = out_base / "chapter_0001"
        _make_chapter_artifacts(
            out_dir, "0001",
            terminal_status="accepted_degraded", quarantined=["chunk0001"],
            translations=_CH1_TRANSLATIONS, chunk_plan=_PLAN,
        )
        # Now break / keep the plan that backs the quarantine exclusion.
        plan_path = out_dir / "chunk_plan.json"
        if plan_mode == "valid":
            pass  # the _PLAN written above stays intact
        elif plan_mode == "missing":
            plan_path.unlink()
        elif plan_mode == "corrupt":
            plan_path.write_text(
                '{"chunks": [{"chunk_id": "chunk0001", "pids": [',
                encoding="utf-8",
            )
        elif plan_mode == "empty":
            plan_path.write_text('{"chunks": []}', encoding="utf-8")
        elif plan_mode == "partial":
            # chunk0002's pids (p00004-p00006) are missing from the plan —
            # their provenance is unknown.
            plan_path.write_text(json.dumps({
                "artifact": "pact-v4-chunk-plan/v1",
                "chunks": [
                    {"chunk_id": "chunk0001",
                     "pids": ["p00001", "p00002", "p00003"]},
                ],
            }), encoding="utf-8")
        else:  # pragma: no cover
            raise AssertionError(f"unknown plan_mode {plan_mode!r}")

        result = v4_book_run.run_book(
            memory_dir=memory,
            chapter_ids=["0001"],
            chapter_html_pattern=str(src_dir / "{chapter_id}.html"),
            out_base=out_base,
        )
        return memory, out_base, result

    @pytest.mark.parametrize("plan_mode", ["missing", "corrupt", "empty", "partial"])
    def test_fail_closed_on_unavailable_quarantine_provenance(
        self, tmp_path, monkeypatch, plan_mode,
    ):
        """accepted_degraded + quarantined chunk + broken chunk_plan.json:
        zero candidates, no ledger line, no observation, no glossary change."""
        memory, out_base, result = self._run_plan_mode(
            tmp_path, monkeypatch, plan_mode,
        )

        assert result["chapters"][0]["terminal_status"] == "accepted_degraded"
        assert result["chapters"][0]["candidates"] == {
            "generated": 0, "proposed": 0, "committed": 0, "conflicts": 0,
        }

        records = GlossaryCandidateLedger(
            str(out_base / "glossary_candidates.json")
        ).load()
        assert records == {}

        glossary = json.loads((memory / "glossary.json").read_text(encoding="utf-8"))
        assert glossary == {}
        observations = json.loads(
            (memory / "observations.json").read_text(encoding="utf-8")
        )
        assert observations.get("glossary", {}) == {}

    def test_valid_plan_same_data_still_generates(self, tmp_path, monkeypatch):
        """Positive control: the SAME chapter data with a VALID plan still
        generates pact from the accepted chunk — the zero above is caused by
        the broken plan, not by accepted_degraded + quarantined itself."""
        memory, out_base, result = self._run_plan_mode(
            tmp_path, monkeypatch, "valid",
        )

        # chunk0001 quarantined -> Blake's pids (p00001-p00003) are dropped;
        # pact (chunk0002, accepted) generates but needs 2 chapters to promote.
        assert result["chapters"][0]["candidates"] == {
            "generated": 1, "proposed": 0, "committed": 0, "conflicts": 0,
        }
        records = GlossaryCandidateLedger(
            str(out_base / "glossary_candidates.json")
        ).load()
        assert "proper_name|blake" not in records
        pact = records["term|pact"]
        assert pact["total_occurrences"] == 3
        assert pact["chapters"][0]["chunk_ids"] == ["chunk0002"]
        glossary = json.loads((memory / "glossary.json").read_text(encoding="utf-8"))
        assert glossary == {}

    @pytest.mark.parametrize("plan_mode", ["across_chunks_accepted_wins",
                                           "within_chunk_duplicate"])
    def test_fail_closed_on_duplicate_pid_ownership(
        self, tmp_path, monkeypatch, plan_mode,
    ):
        """B9-F6: a corrupt ``chunk_plan.json`` that maps a PID to more than
        one chunk (or twice within one chunk) is non-authoritative and fails
        closed — the old dict-assignment silently let the LAST chunk win, so
        a later accepted chunk masked the same PID's earlier quarantined
        ownership and quarantined evidence generated a candidate.

        Reproduction from B9-RV5 on the reviewed tip: quarantined chunk owns
        p00001/p00002 first, accepted chunk owns the SAME pids later ->
        old code emitted a Blake->Блэйк candidate with
        ``chunk_ids=['accepted']``. Here the same corrupt plan must yield
        zero candidates, no ledger line, no observation, no glossary
        mutation, and the terminal stays ``accepted_degraded``.
        """
        from pact_full_pipeline_runner_v1 import v4_book_run

        memory = _setup_memory(tmp_path)
        out_base = tmp_path / "out"
        src_dir = tmp_path / "src"

        def fake_run_one(*args, **kwargs):
            return {"status": "ok"}

        monkeypatch.setattr(v4_book_run, "_run_one_chapter", fake_run_one)

        html = ("<p>He met Blake at the gate. Blake knew the way.</p>\n"
                "<p>Blake waited outside for Mary.</p>")
        translations = {
            "p00001": "Блэйк встретил Блэйка у ворот. Блэйк знал дорогу.",
            "p00002": "Блэйк ждал снаружи Мэри.",
        }
        if plan_mode == "across_chunks_accepted_wins":
            # Quarantined chunk owns p00001/p00002 FIRST, accepted chunk
            # claims the SAME pids SECOND — the old ``mapping[pid] =
            # chunk_id`` overwrite made the accepted chunk win, hiding the
            # quarantined ownership from the drop filter.
            chunks = [
                {"chunk_id": "chunk_q", "snapshot_hash": "test",
                 "pids": ["p00001", "p00002"],
                 "word_counts": [], "context": {"left_ru": "", "right_en": []},
                 "undersized_exception": False},
                {"chunk_id": "chunk_a", "snapshot_hash": "test",
                 "pids": ["p00001", "p00002"],
                 "word_counts": [], "context": {"left_ru": "", "right_en": []},
                 "undersized_exception": False},
            ]
            quarantined = ["chunk_q"]
        elif plan_mode == "within_chunk_duplicate":
            # One chunk lists the same pid twice; the accepted chunk also
            # claims p00001 twice. Old code normalized the duplicate away.
            chunks = [
                {"chunk_id": "chunk_q", "snapshot_hash": "test",
                 "pids": ["p00003"],
                 "word_counts": [], "context": {"left_ru": "", "right_en": []},
                 "undersized_exception": False},
                {"chunk_id": "chunk_a", "snapshot_hash": "test",
                 "pids": ["p00001", "p00001", "p00002"],
                 "word_counts": [], "context": {"left_ru": "", "right_en": []},
                 "undersized_exception": False},
            ]
            quarantined = ["chunk_q"]
        else:  # pragma: no cover
            raise AssertionError(f"unknown plan_mode {plan_mode!r}")
        duplicate_plan = {
            "artifact": "pact-v4-chunk-plan/v1",
            "snapshot_hash": "test",
            "plan_hash": "test",
            "chunks": chunks,
        }
        _write_chapter_html(src_dir, "0001", html)
        _make_chapter_artifacts(
            out_base / "chapter_0001", "0001",
            terminal_status="accepted_degraded", quarantined=quarantined,
            translations=translations, chunk_plan=duplicate_plan,
        )

        result = v4_book_run.run_book(
            memory_dir=memory,
            chapter_ids=["0001"],
            chapter_html_pattern=str(src_dir / "{chapter_id}.html"),
            out_base=out_base,
        )

        # Terminal stays accepted_degraded (the run itself succeeded); the
        # B9 loop failed closed on the corrupt provenance.
        assert result["chapters"][0]["terminal_status"] == "accepted_degraded"
        assert result["chapters"][0]["candidates"] == {
            "generated": 0, "proposed": 0, "committed": 0, "conflicts": 0,
        }

        records = GlossaryCandidateLedger(
            str(out_base / "glossary_candidates.json")
        ).load()
        assert records == {}

        glossary = json.loads((memory / "glossary.json").read_text(encoding="utf-8"))
        assert glossary == {}
        observations = json.loads(
            (memory / "observations.json").read_text(encoding="utf-8")
        )
        assert observations.get("glossary", {}) == {}


# ---------------------------------------------------------------------------
# _auto_promote_glossary: thresholds and conflict paths (B9-I2 req 2)
# ---------------------------------------------------------------------------


class TestAutoPromoteGlossary:
    def _manager(self, tmp_path):
        from pact_v4.phase1.memory import MemoryManager
        memory = _setup_memory(tmp_path)
        return MemoryManager(str(memory))

    def _ledger(self, records) -> dict:
        """One-entry ledger map keyed like ``GlossaryCandidateLedger.load``."""
        from pact_v4.phase1.glossary_candidates import candidate_key
        return {
            candidate_key(r["source"], r["kind"]): r
            for r in records
        }

    def _observation(self, tmp_path, source: str):
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
            "target": "Блэйк", "targets_seen": ["Блэйк"], "conflicts": [],
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
            "target": "пакт", "targets_seen": ["пакт"], "conflicts": [],
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

    def test_term_never_promoted_even_above_threshold(self, tmp_path):
        # P0 owner decision 2026-08-14 (term auto-promotion OFF): a generic
        # term that meets BOTH v3 thresholds (2 chapters, N occurrences) and
        # has an unambiguous target is STILL never promoted — frequency +
        # stability does not detect terminology (door → дверь is stable but
        # not a term). It stays in the ledger only, never in the prompt.
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
            "target": "пакт", "targets_seen": ["пакт"], "conflicts": [],
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
            "target": "Блэйк", "targets_seen": ["Блэйк"], "conflicts": [],
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
            "target": "Блэйк", "targets_seen": ["Блэйк"], "conflicts": [],
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
# B9-F3: _auto_promote_glossary cumulative-ledger conflict guard
# (direct helper tests, no book run)
# ---------------------------------------------------------------------------


class _RecordingManager:
    """MemoryManager stand-in that records ``add_observation`` calls."""

    def __init__(self):
        self.calls = []

    def add_observation(self, category, key, value):
        self.calls.append((category, key, value))


class TestAutoPromoteGlossaryCumulativeGuard:
    def _aligned_pact(self, target, conflicts=()):
        return [{
            "source": "pact", "kind": "proper_name", "occurrences": 3,
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
            v4_book_run.candidate_key("pact", "proper_name"): {
                "source": "pact", "kind": "proper_name", "total_occurrences": 6,
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
            v4_book_run.candidate_key("pact", "proper_name"): {
                "source": "pact", "kind": "proper_name", "total_occurrences": 6,
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
            v4_book_run.candidate_key("pact", "proper_name"): {
                "source": "pact", "kind": "proper_name", "total_occurrences": 6,
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
             {"target": "пакт", "type": "proper_name", "chunk_id": "chunk0002"}),
        ]


# ---------------------------------------------------------------------------
# CLI arg wiring (dest-name check — the B5 regression pattern)
# ---------------------------------------------------------------------------


class TestBookRunCliArgs:
    def test_b9_args_parse_with_correct_dests(self):
        """Real argparse parse: dest names must match run_book's kwargs and
        the B5 manual allowlist must come from --mixed-script-allow (the same
        flag the strict driver uses)."""
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
            "--mixed-script-allow", "corvidae",
            "--mixed-script-allow", "R.D.T.",
            "--run-label", "test-run",  # unknown to book_run -> strict driver
        ])
        assert args.candidates_ledger == Path("led.json")
        assert args.term_min_occurrences == 4
        assert args.term_min_chapters == 3
        assert args.proper_name_min_occurrences == 5
        assert args.consensus_ratio == 0.9
        assert args.mixed_script_allow == ["corvidae", "R.D.T."]
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
        assert args.mixed_script_allow is None
