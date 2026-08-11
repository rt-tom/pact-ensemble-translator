"""BM integration tests: deterministic book_memory candidate accumulation.

Covers the BM card (V4.1 §15 of ``docs/plans/V4_1_AUDIT_B1_RU.md``; owner
decision 2026-08-08 — 0 model calls) in the book run:

* after each accepted chapter (``complete`` / ``accepted_degraded``) the run
  derives book_memory candidates from the chapter SOURCE (proper-name
  characters; NEVER from translations or LLM inference), appends them to the
  append-only ledger (``book_memory_candidates.json``) and auto-promotes the
  ones meeting the v3-style thresholds (total occurrences >= N OR distinct
  chapters >= M) via ``MemoryManager.add_observation("book_memory", ...)`` ->
  the existing ``promote`` path (B7 conflict resolution + quarantined filter);
* ``gender`` is included ONLY when the source explicitly shows exclusively
  one gendered pronoun set (he/him/his vs she/her/hers) in the name's PIDs
  and their immediate neighbours (fail-closed: ambiguous or disagreeing
  chapters => no gender, no gender fact);
* key-bound facts are promoted alongside a character: a presence fact
  (``<name> appears in chapters ...``) and, when gender is source-confirmed,
  a gender fact — both carry explicit ``keys`` so ``build_chapter_index``
  binds them;
* established/locked book_memory entries are never overwritten; a candidate
  without explicit source confirmation is never promoted; ``book_memory.json``
  is only rewritten when a value really changes (byte preservation,
  B9-RV9 pattern) so ``book_memory_hash`` moves only on a real promotion;
* quarantined-chunk evidence is excluded BEFORE generation (B9-RV3 pattern),
  with BM-F5/F6 fail-closed on missing/corrupt/empty/ambiguous PID->chunk
  provenance.

The strict driver is faked (``_run_one_chapter`` monkeypatched); per-chapter
out_dir artifacts are pre-populated on disk exactly like the B7/B9 tests.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from pact_v4.phase1.book_memory_candidates import BookMemoryCandidateLedger

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


def _setup_memory(tmp_path: Path, book_memory: dict | None = None) -> Path:
    memory = tmp_path / "memory"
    memory.mkdir(parents=True, exist_ok=True)
    (memory / "glossary.json").write_text("{}", encoding="utf-8")
    if book_memory is None:
        book_memory = {
            "pov": {"gender": "male", "source_name": "Blake Thorburn"},
            "characters": {
                "Blake Thorburn": {
                    "type": "character", "gender": "male",
                    "chapters": ["0001"], "variants": {"Blake": 1},
                    "forbidden_targets": [],
                },
            },
        }
    (memory / "book_memory.json").write_text(
        json.dumps(book_memory, ensure_ascii=False), encoding="utf-8",
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
    chunk_plan: dict = _PLAN,
) -> None:
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


class TestBookMemoryAccumulation:
    def _run(self, tmp_path, monkeypatch, chapter_specs,
             book_memory=None, **run_kwargs):
        from pact_full_pipeline_runner_v1 import v4_book_run

        memory = _setup_memory(tmp_path, book_memory=book_memory)
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
                translations=translations,
            )

        result = v4_book_run.run_book(
            memory_dir=memory,
            chapter_ids=list(chapter_specs),
            chapter_html_pattern=str(src_dir / "{chapter_id}.html"),
            out_base=out_base,
            **run_kwargs,
        )
        return memory, out_base, result

    # ------------------------------------------------------------------
    # Two-chapter accumulation + promotion (acceptance: 2 главы)
    # ------------------------------------------------------------------

    def test_two_chapters_promote_source_confirmed_character_and_facts(
        self, tmp_path, monkeypatch,
    ):
        """A recurring proper name promotes into book_memory.characters with
        source-confirmed gender + key-bound presence/gender facts."""
        ch1 = (
            "<p>Blake met Rose at the gate.</p>\n"
            "<p>She was running late that day.</p>"
        )
        ch2 = (
            "<p>Blake saw Rose smile.</p>\n"
            "<p>She opened the heavy door.</p>"
        )
        translations = {
            "p00001": "Блэйк встретил Роуз у ворот.",
            "p00002": "Она опаздывала в тот день.",
            "p00003": "Блэйк видел, как Роуз улыбнулась.",
            "p00004": "Она открыла тяжёлую дверь.",
        }
        memory, out_base, result = self._run(tmp_path, monkeypatch, {
            "0001": (ch1, "complete", [], translations),
            "0002": (ch2, "complete", [], translations),
        })

        # Both chapters accepted; BM ran both times.
        assert [r["terminal_status"] for r in result["chapters"]] == [
            "complete", "complete",
        ]

        # Rose promoted in book_memory.characters with source-confirmed
        # gender; the established narrator (Blake Thorburn) is untouched.
        bm = json.loads((memory / "book_memory.json").read_text(encoding="utf-8"))
        assert "Rose" in bm["characters"]
        rose = bm["characters"]["Rose"]
        assert rose["type"] == "character"
        assert rose["gender"] == "female"
        assert rose["chapters"] == ["0001", "0002"]
        assert rose["forbidden_targets"] == []
        # The observation-only chunk_id field was stripped after promote.
        assert "chunk_id" not in rose
        # Established narrator entry unchanged.
        assert bm["characters"]["Blake Thorburn"]["gender"] == "male"
        assert bm["pov"]["gender"] == "male"

        # Key-bound facts: presence + gender (both with explicit keys).
        fact_texts = [f.get("fact") for f in bm.get("facts", [])]
        assert any("Rose appears in chapters 0001, 0002." in t for t in fact_texts)
        assert any("female pronouns" in t for t in fact_texts)
        rose_facts = [f for f in bm.get("facts", []) if f.get("keys") == ["Rose"]]
        assert len(rose_facts) == 2
        assert all("chunk_id" not in f for f in rose_facts)

        # book_run.json records the BM block and the promotion events with
        # evidence PIDs (review/rollback artifact).
        ch2_rec = result["chapters"][1]
        assert ch2_rec["book_memory_candidates"]["generated"] >= 1
        assert ch2_rec["book_memory_candidates"]["committed"] == 1
        assert ch2_rec["book_memory_candidates"]["conflicts"] == 0
        assert ch2_rec["book_memory_promotions"]
        promo = ch2_rec["book_memory_promotions"][0]
        assert promo["source"] == "Rose"
        assert promo["gender"] == "female"
        assert promo["chapters"] == ["0001", "0002"]
        assert promo["evidence_pids"]

        # The ledger accumulated Rose across both chapters.
        ledger = BookMemoryCandidateLedger(
            str(out_base / "book_memory_candidates.json")
        ).load()
        rose_rec = ledger["character|rose"]
        assert rose_rec["total_occurrences"] >= 2
        assert {c["chapter_id"] for c in rose_rec["chapters"]} == {"0001", "0002"}
        assert rose_rec["gender"] == "female"

    def test_gender_fail_closed_without_explicit_source_confirmation(
        self, tmp_path, monkeypatch,
    ):
        """No he/she/him/her evidence near the name => no gender field and no
        gender fact (fail-closed), but the character still promotes (the name
        itself IS source-confirmed)."""
        html = (
            "<p>Blake met Rose at the gate, and Rose knew the way.</p>\n"
            "<p>Rose waited outside for a moment.</p>"
        )
        translations = {
            "p00001": "Блэйк встретил Роуз у ворот, и Роуз знала дорогу.",
            "p00002": "Роуз ждала снаружи мгновение.",
        }
        memory, out_base, result = self._run(tmp_path, monkeypatch, {
            "0001": (html, "complete", [], translations),
        })

        bm = json.loads((memory / "book_memory.json").read_text(encoding="utf-8"))
        assert "Rose" in bm["characters"]
        assert "gender" not in bm["characters"]["Rose"]
        # No gender fact — only the presence fact.
        rose_facts = [f for f in bm.get("facts", []) if f.get("keys") == ["Rose"]]
        assert len(rose_facts) == 1
        assert "pronouns" not in rose_facts[0]["fact"]

        ledger = BookMemoryCandidateLedger(
            str(out_base / "book_memory_candidates.json")
        ).load()
        assert ledger["character|rose"]["gender"] is None

    def test_gender_ambiguous_both_pronoun_sets_never_promotes(
        self, tmp_path, monkeypatch,
    ):
        """Both he/him and she/her near the name => ambiguous => no gender."""
        html = (
            "<p>Blake met Rose at the gate, and Rose knew the way.</p>\n"
            "<p>He waited while she talked to Rose.</p>"
        )
        translations = {
            "p00001": "Блэйк встретил Роуз у ворот, и Роуз знала дорогу.",
            "p00002": "Он ждал, пока она говорила с Роуз.",
        }
        memory, out_base, result = self._run(tmp_path, monkeypatch, {
            "0001": (html, "complete", [], translations),
        })

        bm = json.loads((memory / "book_memory.json").read_text(encoding="utf-8"))
        assert "gender" not in bm["characters"]["Rose"]
        rose_facts = [f for f in bm.get("facts", []) if f.get("keys") == ["Rose"]]
        assert len(rose_facts) == 1  # presence only, no gender fact

    def test_cross_chapter_gender_disagreement_never_promotes(
        self, tmp_path, monkeypatch,
    ):
        """Ch1 says male, ch2 says female => merged gender is None forever
        (fail-closed, like the B9 glossary target irreversibility)."""
        ch1 = "<p>Blake met Rose at the gate. He waved.</p>"
        ch2 = "<p>Blake saw Rose return home. She opened the door.</p>"
        translations = {
            "p00001": "Блэйк встретил Роуз у ворот. Он помахал.",
            "p00002": "Блэйк видел, как Роуз вернулась домой. Она открыла дверь.",
        }
        memory, out_base, result = self._run(tmp_path, monkeypatch, {
            "0001": (ch1, "complete", [], translations),
            "0002": (ch2, "complete", [], translations),
        })

        bm = json.loads((memory / "book_memory.json").read_text(encoding="utf-8"))
        assert "gender" not in bm["characters"]["Rose"]
        rose_facts = [f for f in bm.get("facts", []) if f.get("keys") == ["Rose"]]
        assert all("pronouns" not in f["fact"] for f in rose_facts)

        ledger = BookMemoryCandidateLedger(
            str(out_base / "book_memory_candidates.json")
        ).load()
        rose_rec = ledger["character|rose"]
        assert rose_rec["gender"] is None
        assert set(rose_rec["gender_conflicts"]) == {"male", "female"}

    def test_established_and_locked_never_overwritten(
        self, tmp_path, monkeypatch,
    ):
        """book_memory entries with status established/locked are never
        overwritten by promotion; the generator also excludes known names."""
        book_memory = {
            "pov": {"gender": "male", "source_name": "Blake Thorburn"},
            "characters": {
                "Blake Thorburn": {
                    "type": "character", "gender": "male", "status": "established",
                    "chapters": ["0001"], "variants": {"Blake": 1},
                    "forbidden_targets": [],
                },
                "Locked Marian": {
                    "type": "character", "gender": "female", "status": "locked",
                    "chapters": [], "variants": {}, "forbidden_targets": [],
                },
            },
        }
        html = (
            "<p>Blake met Rose at the gate, and Rose knew the way.</p>\n"
            "<p>Rose waited outside. She was running late.</p>"
        )
        translations = {
            "p00001": "Блэйк встретил Роуз у ворот, и Роуз знала дорогу.",
            "p00002": "Роуз ждала снаружи. Она опаздывала.",
        }
        before_bytes = json.dumps(book_memory, ensure_ascii=False).encode("utf-8")
        memory, out_base, result = self._run(
            tmp_path, monkeypatch,
            {"0001": (html, "complete", [], translations)},
            book_memory=book_memory,
        )

        bm = json.loads((memory / "book_memory.json").read_text(encoding="utf-8"))
        # Locked entry untouched.
        assert bm["characters"]["Locked Marian"]["gender"] == "female"
        # Established narrator untouched.
        assert bm["characters"]["Blake Thorburn"]["status"] == "established"
        # Rose is new and promoted; Blake (established) was never proposed.
        assert "Rose" in bm["characters"]
        # A real promotion happened, so bytes legitimately changed — but the
        # established/locked entries above prove conflict resolution held.
        assert (memory / "book_memory.json").read_bytes() != before_bytes

    def test_below_threshold_candidate_not_promoted_hash_unchanged(
        self, tmp_path, monkeypatch,
    ):
        """A single one-off proper name (1 occurrence, 1 chapter) is below
        both thresholds: nothing is proposed, book_memory.json bytes and
        book_memory_hash are unchanged (byte preservation, B9-RV9 pattern)."""
        html = "<p>Rose met Blake at the gate.</p>"
        translations = {"p00001": "Роуз встретила Блэйка у ворот."}
        book_memory = {
            "pov": {"gender": "male", "source_name": "Blake Thorburn"},
            "characters": {
                "Blake Thorburn": {
                    "type": "character", "gender": "male",
                    "chapters": ["0001"], "variants": {"Blake": 1},
                    "forbidden_targets": [],
                },
            },
        }
        before_bytes = json.dumps(book_memory, ensure_ascii=False).encode("utf-8")
        memory, out_base, result = self._run(
            tmp_path, monkeypatch,
            {"0001": (html, "complete", [], translations)},
            book_memory=book_memory,
        )

        assert result["chapters"][0]["book_memory_candidates"]["proposed"] == 0
        assert result["chapters"][0]["book_memory_candidates"]["committed"] == 0
        bm = json.loads((memory / "book_memory.json").read_text(encoding="utf-8"))
        assert "Rose" not in bm["characters"]
        assert (memory / "book_memory.json").read_bytes() == before_bytes
        rec = result["chapters"][0]
        assert rec["book_memory_hash_before"] == rec["book_memory_hash_after"]
        assert rec["book_memory_hash_after"] == hashlib.sha256(
            before_bytes
        ).hexdigest()

    # ------------------------------------------------------------------
    # Terminal policy / quarantine
    # ------------------------------------------------------------------

    def test_failed_chapter_never_enters_bm_ledger(self, tmp_path, monkeypatch):
        """A failed chapter contributes no BM candidates, no ledger line, no
        observation (review F1 pattern: accepted text only)."""
        html = (
            "<p>Rose met Blake at the gate. Rose knew the way.</p>\n"
            "<p>Rose waited outside. She was running late.</p>"
        )
        translations = {
            "p00001": "Роуз встретила Блэйка у ворот. Роуз знала дорогу.",
            "p00002": "Роуз ждала снаружи. Она опаздывала.",
        }
        memory, out_base, result = self._run(tmp_path, monkeypatch, {
            "0001": (html, "failed", [], translations),
        })

        assert result["chapters"][0]["book_memory_candidates"] == {
            "generated": 0, "proposed": 0, "committed": 0, "conflicts": 0,
        }
        assert result["chapters"][0]["book_memory_promotions"] == []
        ledger = BookMemoryCandidateLedger(
            str(out_base / "book_memory_candidates.json")
        ).load()
        assert "character|rose" not in ledger
        bm = json.loads((memory / "book_memory.json").read_text(encoding="utf-8"))
        assert "Rose" not in bm["characters"]

    def test_quarantined_evidence_excluded_before_generation(
        self, tmp_path, monkeypatch,
    ):
        """accepted_degraded + quarantined chunk: a candidate whose evidence
        lives wholly in a quarantined chunk never generates (B9-RV3 pattern),
        so it never enters the ledger and never promotes."""
        html = (
            "<p>Rose met Blake at the gate. Rose knew the way.</p>\n"
            "<p>Rose waited outside. She was running late.</p>\n"
            "<p>Rose returned home later.</p>\n"
            "<p>The pact held firm against time.</p>\n"
            "<p>The pact was old and strong.</p>\n"
            "<p>The others watched from afar.</p>"
        )
        translations = {
            "p00001": "Роуз встретила Блэйка у ворот. Роуз знала дорогу.",
            "p00002": "Роуз ждала снаружи. Она опаздывала.",
            "p00003": "Роуз вернулась домой позже.",
            "p00004": "Пакт держался стойко против времени.",
            "p00005": "Пакт был старым и сильным.",
            "p00006": "Остальные наблюдали издалека.",
        }
        # chunk0001 (p00001-p00003, all Rose evidence) is quarantined.
        memory, out_base, result = self._run(tmp_path, monkeypatch, {
            "0001": (html, "accepted_degraded", ["chunk0001"], translations),
        })

        assert result["chapters"][0]["book_memory_candidates"]["generated"] == 0
        assert result["chapters"][0]["book_memory_candidates"]["committed"] == 0
        ledger = BookMemoryCandidateLedger(
            str(out_base / "book_memory_candidates.json")
        ).load()
        assert "character|rose" not in ledger
        bm = json.loads((memory / "book_memory.json").read_text(encoding="utf-8"))
        assert "Rose" not in bm["characters"]

    def test_mixed_candidate_counts_only_accepted_chunks(
        self, tmp_path, monkeypatch,
    ):
        """A candidate spanning accepted + quarantined chunks keeps only its
        accepted-chunk evidence (occurrences, chunk_ids, promotion)."""
        html = (
            "<p>Blake met Rose at the gate, and Rose knew the way.</p>\n"      # p00001 (Q)
            "<p>Rose waited outside. She was running late.</p>\n"              # p00002 (Q)
            "<p>Rose returned home later.</p>\n"                               # p00003 (Q)
            "<p>Blake saw Rose smile, and Rose waved once more.</p>\n"         # p00004
            "<p>The pact held firm against time.</p>\n"                        # p00005
            "<p>The others watched from afar.</p>"                             # p00006
        )
        translations = {
            "p00001": "Роуз встретила Блэйка у ворот. Роуз знала дорогу.",
            "p00002": "Роуз ждала снаружи. Она опаздывала.",
            "p00003": "Роуз вернулась домой позже.",
            "p00004": "Роуз улыбнулась Блэйку. Роуз помахала ещё раз.",
            "p00005": "Пакт держался стойко против времени.",
            "p00006": "Остальные наблюдали издалека.",
        }
        memory, out_base, result = self._run(tmp_path, monkeypatch, {
            "0001": (html, "accepted_degraded", ["chunk0001"], translations),
        })

        # Rose evidence is now only from chunk0002 (2 occurrences >= 2) -> promoted
        # with accepted provenance; the quarantined chunk's 4 occurrences never
        # count toward the threshold (without exclusion it would be 6 >= 2 too,
        # but the chunk_ids must be accepted-only and committed must succeed).
        rec = result["chapters"][0]
        assert rec["book_memory_candidates"]["committed"] == 1
        ledger = BookMemoryCandidateLedger(
            str(out_base / "book_memory_candidates.json")
        ).load()
        rose_rec = ledger["character|rose"]
        assert rose_rec["total_occurrences"] == 2  # not 6
        assert rose_rec["chapters"][0]["chunk_ids"] == ["chunk0002"]
        bm = json.loads((memory / "book_memory.json").read_text(encoding="utf-8"))
        assert "Rose" in bm["characters"]

    def test_accepted_degraded_uses_current_chapter_chunk_provenance(
        self, tmp_path, monkeypatch,
    ):
        """RV fix: observation chunk provenance comes from the CURRENT chapter
        candidate only, never from the cumulative-ledger union.

        ch1: Rose in chunk0001, chapter complete, but below both thresholds
        (1 occurrence / 1 chapter) so nothing promotes. ch2: Rose in accepted
        chunk0002 while chunk0001 is quarantined in THIS chapter; the
        cumulative ledger then reaches the thresholds (2 occurrences OR 2
        chapters). The per-chapter chunk IDs repeat (chunk0001/chunk0002 in
        both chapters), so a union over the cumulative ledger would pick
        chunk0001 — quarantined in ch2 — and the B7 filter would drop the
        valid promotion (proposed=1, committed=0). Expected: committed=1 and
        Rose present in book_memory.
        """
        ch1 = (
            "<p>Blake met Rose at the gate.</p>\n"        # p00001 (chunk0001)
            "<p>The pact held firm against time.</p>\n"   # p00002 (chunk0001)
            "<p>The others watched from afar.</p>"       # p00003 (chunk0001)
        )
        # ch2: first three paragraphs live in chunk0001 (quarantined here);
        # Rose's ch2 evidence lives in p00004/p00005 (accepted chunk0002).
        ch2 = (
            "<p>The pact held firm against time.</p>\n"   # p00001 (chunk0001, Q)
            "<p>The others watched from afar.</p>\n"      # p00002 (chunk0001, Q)
            "<p>The door was old and strong.</p>\n"       # p00003 (chunk0001, Q)
            "<p>Blake saw Rose smile.</p>\n"              # p00004 (chunk0002)
            "<p>Rose waited outside.</p>\n"               # p00005 (chunk0002)
            "<p>The night was quiet.</p>"                 # p00006 (chunk0002)
        )
        translations = {
            "p00001": "Блэйк встретил Роуз у ворот.",
            "p00002": "Пакт держался стойко против времени.",
            "p00003": "Остальные наблюдали издалека.",
            "p00004": "Блэйк видел, как Роуз улыбнулась.",
            "p00005": "Роуз ждала снаружи.",
            "p00006": "Ночь была тихой.",
        }
        # ch2 is accepted_degraded with chunk0001 quarantined, but all of
        # ch2's Rose evidence lives in chunk0002 (accepted).
        memory, out_base, result = self._run(tmp_path, monkeypatch, {
            "0001": (ch1, "complete", [], translations),
            "0002": (ch2, "accepted_degraded", ["chunk0001"], translations),
        })

        assert result["chapters"][1]["terminal_status"] == "accepted_degraded"
        assert result["chapters"][1]["book_memory_candidates"]["committed"] == 1
        bm = json.loads((memory / "book_memory.json").read_text(encoding="utf-8"))
        assert "Rose" in bm["characters"]
        # The observation chunk_id must be the CURRENT chapter's accepted
        # chunk (chunk0002), not the union-picked chunk0001 that is
        # quarantined here (regression for 97571d3 finding).
        promo = result["chapters"][1]["book_memory_promotions"][0]
        assert promo["source"] == "Rose"
        assert promo["chunk_ids"] == ["chunk0002"]

    def test_fail_closed_when_current_chapter_provenance_missing(
        self, tmp_path, monkeypatch,
    ):
        """RV fix, fail-closed branch: a candidate whose current-chapter
        accepted provenance is missing/invalid is NEVER promoted, even when
        the cumulative ledger satisfies the thresholds. A prior chapter's
        chunk_ids must not substitute for the current chapter's accepted
        provenance."""
        html = (
            "<p>Rose met Blake at the gate.</p>\n"
            "<p>Rose knew the way.</p>\n"
        )
        translations = {
            "p00001": "Роуз встретила Блэйка у ворот.",
            "p00002": "Роуз знала дорогу.",
        }
        from pact_full_pipeline_runner_v1 import v4_book_run

        memory = _setup_memory(tmp_path)
        out_base = tmp_path / "out"
        src_dir = tmp_path / "src"

        def fake_run_one(*args, **kwargs):
            return {"status": "ok"}

        monkeypatch.setattr(v4_book_run, "_run_one_chapter", fake_run_one)

        _write_chapter_html(src_dir, "0001", html)
        # No chunk_plan.json at all: _pid_to_chunk returns None, so the
        # candidate carries NO chunk provenance for the current chapter.
        _make_chapter_artifacts(
            out_base / "chapter_0001", "0001",
            terminal_status="accepted_degraded", quarantined=["chunk0001"],
            translations=translations,
        )
        (out_base / "chapter_0001" / "chunk_plan.json").unlink()

        result = v4_book_run.run_book(
            memory_dir=memory,
            chapter_ids=["0001"],
            chapter_html_pattern=str(src_dir / "{chapter_id}.html"),
            out_base=out_base,
        )

        rec = result["chapters"][0]
        assert rec["book_memory_candidates"]["proposed"] == 0
        assert rec["book_memory_candidates"]["committed"] == 0
        bm = json.loads((memory / "book_memory.json").read_text(encoding="utf-8"))
        assert "Rose" not in bm["characters"]

    def test_fail_closed_on_unavailable_quarantine_provenance(
        self, tmp_path, monkeypatch,
    ):
        """accepted_degraded + quarantined chunk + missing chunk_plan.json:
        BM fails closed — no candidates, no ledger line, no observation, no
        book_memory change (BM-F5/F6, mirror of B9-F5/F6)."""
        from pact_full_pipeline_runner_v1 import v4_book_run

        memory = _setup_memory(tmp_path)
        out_base = tmp_path / "out"
        src_dir = tmp_path / "src"

        def fake_run_one(*args, **kwargs):
            return {"status": "ok"}

        monkeypatch.setattr(v4_book_run, "_run_one_chapter", fake_run_one)

        html = "<p>Rose met Blake at the gate. Rose knew the way.</p>"
        translations = {"p00001": "Роуз встретила Блэйка у ворот. Роуз знала дорогу."}
        _write_chapter_html(src_dir, "0001", html)
        _make_chapter_artifacts(
            out_base / "chapter_0001", "0001",
            terminal_status="accepted_degraded", quarantined=["chunk0001"],
            translations=translations,
        )
        # Break the plan that backs the quarantine exclusion.
        (out_base / "chapter_0001" / "chunk_plan.json").unlink()

        result = v4_book_run.run_book(
            memory_dir=memory,
            chapter_ids=["0001"],
            chapter_html_pattern=str(src_dir / "{chapter_id}.html"),
            out_base=out_base,
        )

        assert result["chapters"][0]["terminal_status"] == "accepted_degraded"
        assert result["chapters"][0]["book_memory_candidates"] == {
            "generated": 0, "proposed": 0, "committed": 0, "conflicts": 0,
        }
        ledger = BookMemoryCandidateLedger(
            str(out_base / "book_memory_candidates.json")
        ).load()
        assert ledger == {}
        bm = json.loads((memory / "book_memory.json").read_text(encoding="utf-8"))
        assert "Rose" not in bm["characters"]
        observations = json.loads(
            (memory / "observations.json").read_text(encoding="utf-8")
        )
        assert observations.get("book_memory", {}) == {}


class _RecordingManager:
    """Minimal MemoryManager stand-in recording add_observation calls."""

    def __init__(self):
        self.calls: list = []

    def add_observation(self, category, key, payload):
        self.calls.append((category, key, dict(payload)))


class TestAutoPromoteBookMemoryProvenance:
    """Direct unit coverage of _auto_promote_book_memory provenance rules."""

    def _ledger(self, chapter_chunk_ids):
        """Cumulative ledger record for Rose with per-chapter chunk_ids."""
        return {
            "character|rose": {
                "source": "Rose", "kind": "character",
                "total_occurrences": 4,
                "chapters": [
                    {"chapter_id": f"{i + 1:04d}", "chunk_ids": chunk_ids,
                     "count": 2, "evidence_pids": [f"p{i + 1:05d}"],
                     "gender": None, "gender_evidence_pids": []}
                    for i, chunk_ids in enumerate(chapter_chunk_ids)
                ],
                "gender": None,
            },
        }

    def test_promotion_uses_current_chapter_chunk_id_only(self):
        """ch1 chunk0001 + ch2 chunk0002: the observation chunk_id is the
        CURRENT chapter's chunk0002, never the sorted union chunk0001
        (regression for the 97571d3 review finding)."""
        from pact_full_pipeline_runner_v1 import v4_book_run

        manager = _RecordingManager()
        cand = {
            "source": "Rose", "kind": "character", "occurrences": 2,
            "chunk_ids": ["chunk0002"],
            "evidence_pids": ["p00002"], "gender": None,
        }
        proposed, conflicts = v4_book_run._auto_promote_book_memory(
            manager,
            [cand],
            self._ledger([["chunk0001"], ["chunk0002"]]),
            {},
            min_name_occurrences=2, min_name_chapters=2,
        )
        assert len(proposed) == 1
        assert conflicts == []
        char_calls = [c for c in manager.calls
                      if c[0] == "book_memory" and c[1].startswith("characters:")]
        assert len(char_calls) == 1
        assert char_calls[0][2]["chunk_id"] == "chunk0002"

    def test_missing_current_chunk_provenance_fails_closed(self):
        """The candidate carries NO chunk_ids for the current chapter: even
        though the cumulative ledger satisfies the thresholds, promotion is
        skipped (fail-closed) — a prior chapter's chunk_id must not stand in
        for the current chapter's accepted provenance."""
        from pact_full_pipeline_runner_v1 import v4_book_run

        manager = _RecordingManager()
        cand = {
            "source": "Rose", "kind": "character", "occurrences": 2,
            "chunk_ids": [],  # missing/invalid current-chapter provenance
            "evidence_pids": ["p00002"], "gender": None,
        }
        proposed, conflicts = v4_book_run._auto_promote_book_memory(
            manager,
            [cand],
            self._ledger([["chunk0001"], ["chunk0002"]]),
            {},
            min_name_occurrences=2, min_name_chapters=2,
        )
        assert proposed == []
        assert conflicts == []
        assert manager.calls == []

    def test_empty_current_chunk_ids_not_substituted_by_ledger(self):
        """Same as above with an explicit empty chunk_ids list (a plan that
        maps nothing): still fail-closed, no observation."""
        from pact_full_pipeline_runner_v1 import v4_book_run

        manager = _RecordingManager()
        cand = {
            "source": "Rose", "kind": "character", "occurrences": 2,
            "chunk_ids": [],
            "evidence_pids": ["p00002"], "gender": None,
        }
        proposed, _conflicts = v4_book_run._auto_promote_book_memory(
            manager,
            [cand],
            self._ledger([["chunk0001"]]),
            {},
            min_name_occurrences=2, min_name_chapters=1,
        )
        assert proposed == []
        assert manager.calls == []


class TestStripBookMemoryAtomicWrite:
    """The authoritative book_memory.json rewrite after a BM promotion must
    go through an atomic temp-file + os.replace path (crash-safe), not a
    direct Path.write_text (97571d3 review finding #2)."""

    def test_strip_uses_atomic_write_and_valid_json(self, tmp_path, monkeypatch):
        from pact_full_pipeline_runner_v1 import v4_book_run

        memory = _setup_memory(tmp_path)
        # Simulate a post-promote book_memory.json: a character entry that
        # still carries the BM-internal chunk_id field.
        bm = {
            "pov": {"gender": "male", "source_name": "Blake Thorburn"},
            "characters": {
                "Blake Thorburn": {
                    "type": "character", "gender": "male",
                    "chapters": ["0001"], "variants": {"Blake": 1},
                    "forbidden_targets": [],
                },
                "Rose": {
                    "type": "character", "gender": "female",
                    "chapters": ["0001", "0002"], "variants": {},
                    "forbidden_targets": [], "chunk_id": "chunk0002",
                },
            },
            "facts": [
                {"fact": "Rose appears in chapters 0001, 0002.",
                 "keys": ["Rose"], "chunk_id": "chunk0002"},
            ],
        }
        (memory / "book_memory.json").write_text(
            json.dumps(bm, ensure_ascii=False), encoding="utf-8",
        )
        # Make any direct Path.write_text to book_memory.json explode.
        import os as _os
        replace_calls: list = []
        orig_replace = _os.replace

        def spy_replace(src, dst):
            replace_calls.append(str(dst))
            return orig_replace(src, dst)

        monkeypatch.setattr(_os, "replace", spy_replace)
        # Also prove the strip does not use Path.write_text on the
        # authoritative file.
        orig_path_write = Path.write_text

        def guarded_write_text(path_self, *args, **kwargs):
            if str(path_self).endswith("book_memory.json"):
                raise AssertionError(
                    "authoritative book_memory.json must be rewritten via "
                    "atomic temp-file + os.replace, not Path.write_text"
                )
            return orig_path_write(path_self, *args, **kwargs)

        monkeypatch.setattr(Path, "write_text", guarded_write_text)

        v4_book_run._strip_book_memory_observation_fields(memory)

        # Atomic path used for the authoritative file.
        assert any(p.endswith("book_memory.json") for p in replace_calls)
        # Result is valid JSON and chunk_id is gone.
        out = json.loads((memory / "book_memory.json").read_text(encoding="utf-8"))
        assert "chunk_id" not in out["characters"]["Rose"]
        assert "chunk_id" not in out["facts"][0]
        # No leftover temp files in the memory dir.
        leftovers = [p for p in memory.iterdir()
                     if p.name not in ("book_memory.json", "glossary.json",
                                       "observations.json")]
        assert leftovers == []

    def test_strip_noop_preserves_bytes(self, tmp_path):
        """Byte preservation: a book_memory.json with no chunk_id fields is
        never rewritten (no write at all, exact bytes preserved)."""
        from pact_full_pipeline_runner_v1 import v4_book_run

        memory = _setup_memory(tmp_path)
        before = (memory / "book_memory.json").read_bytes()
        v4_book_run._strip_book_memory_observation_fields(memory)
        assert (memory / "book_memory.json").read_bytes() == before


# ---------------------------------------------------------------------------
# MemoryManager: section-scoped book_memory merge (characters:/entities:/facts:)
# ---------------------------------------------------------------------------


class TestMemoryManagerSectionMerge:
    def _manager(self, tmp_path, book_memory=None):
        from pact_v4.phase1.memory import MemoryManager
        memory = _setup_memory(tmp_path, book_memory=book_memory)
        return MemoryManager(str(memory)), memory

    def test_characters_section_scoped_observation_merges(self, tmp_path):
        """characters:<name> observations land inside book_memory.characters
        (not as flat top-level keys) and never replace the whole section."""
        manager, memory = self._manager(tmp_path)
        manager.add_observation("book_memory", "characters:Rose", {
            "type": "character", "gender": "female", "chapters": ["0001"],
            "variants": {}, "forbidden_targets": [],
        })
        manager.promote("complete")
        bm = json.loads((memory / "book_memory.json").read_text(encoding="utf-8"))
        assert "Rose" in bm["characters"]
        assert bm["characters"]["Rose"]["gender"] == "female"
        # The pre-existing section content survived (not replaced).
        assert "Blake Thorburn" in bm["characters"]

    def test_facts_section_scoped_observation_appends_deduped(self, tmp_path):
        manager, memory = self._manager(tmp_path)
        fact = {"fact": "Rose appears in chapters 0001.", "keys": ["Rose"]}
        manager.add_observation("book_memory", "facts:rose:presence", fact)
        manager.promote("complete")
        # promote clears observations; re-observe the same fact text -> dedup.
        manager.add_observation("book_memory", "facts:rose:presence", fact)
        manager.promote("complete")
        bm = json.loads((memory / "book_memory.json").read_text(encoding="utf-8"))
        texts = [f.get("fact") for f in bm["facts"]]
        assert texts.count("Rose appears in chapters 0001.") == 1

    def test_established_locked_section_entry_never_overwritten(self, tmp_path):
        book_memory = {
            "pov": {"gender": "male"},
            "characters": {
                "Rose": {"type": "character", "gender": "female",
                         "status": "locked", "chapters": []},
            },
        }
        manager, memory = self._manager(tmp_path, book_memory=book_memory)
        manager.add_observation("book_memory", "characters:Rose", {
            "type": "character", "gender": "male", "chapters": ["0001"],
        })
        manager.promote("complete")
        bm = json.loads((memory / "book_memory.json").read_text(encoding="utf-8"))
        assert bm["characters"]["Rose"]["gender"] == "female"
        assert bm["characters"]["Rose"]["status"] == "locked"

    def test_flat_legacy_keys_still_work(self, tmp_path):
        """Flat book_memory observation keys keep the legacy top-level
        contract (B7 behaviour unchanged)."""
        manager, memory = self._manager(tmp_path)
        manager.add_observation("book_memory", "char1", {"name": "John"})
        manager.promote("complete")
        bm = json.loads((memory / "book_memory.json").read_text(encoding="utf-8"))
        assert bm["char1"] == {"name": "John"}


# ---------------------------------------------------------------------------
# CLI arg wiring (dest-name check — the B5 regression pattern)
# ---------------------------------------------------------------------------


class TestBookMemoryCliArgs:
    def test_bm_args_parse_with_correct_dests(self):
        from pact_full_pipeline_runner_v1 import v4_book_run

        args, extra = v4_book_run.build_argparser().parse_known_args([
            "--memory-dir", "mem", "--chapters", "0001", "0002",
            "--chapter-html-pattern", "ch/{chapter_id}.html",
            "--out-base", "out",
            "--bm-candidates-ledger", "bm_led.json",
            "--bm-min-name-occurrences", "4",
            "--bm-min-name-chapters", "3",
            "--term-min-chapters", "2",  # B9 flag coexists
        ])
        assert args.bm_candidates_ledger == Path("bm_led.json")
        assert args.bm_min_name_occurrences == 4
        assert args.bm_min_name_chapters == 3
        assert args.term_min_chapters == 2

    def test_bm_args_defaults(self):
        from pact_full_pipeline_runner_v1 import v4_book_run

        args, _extra = v4_book_run.build_argparser().parse_known_args([
            "--memory-dir", "mem", "--chapters", "0001",
            "--chapter-html-pattern", "ch/{chapter_id}.html",
            "--out-base", "out",
        ])
        assert args.bm_candidates_ledger is None
        assert args.bm_min_name_occurrences == 2
        assert args.bm_min_name_chapters == 2
