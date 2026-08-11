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
