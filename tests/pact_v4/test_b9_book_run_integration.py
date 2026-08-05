"""B9-I2 integration tests: candidate generation/ledger/auto-promotion in the book run.

Covers the B9-I2 card requirements under the owner decision recorded in
DECISIONS.md (Variant B — auto-promotion with strict conservative evidence;
B9-RV2/B9-RV3 reviews of PR #128):

* ``run_book`` calls the generator + consensus alignment after each chapter
  (source = chapter HTML, translation = ``out_dir/translations.json``),
  appends to the ledger (default ``<out_base>/glossary_candidates.json``)
  BEFORE ``MemoryManager.promote``;
* v3-threshold auto-promotion through the existing
  ``add_observation -> promote`` path (Variant B): proper_name >= 2
  occurrences with a single target promotes; term needs >= 2 chapters AND
  >= 3 total occurrences with a single target;
* ONLY chapters with an accepted terminal result (``complete`` /
  ``accepted_degraded``) contribute to the ledger — failed/unknown/error
  chapters' observations never enter the ledger or satisfy later thresholds
  (review F1);
* quarantined-chunk evidence is excluded BEFORE ledger accumulation and
  auto-promotion (B9-RV3): pids from quarantined chunks never generate
  candidates, a candidate wholly from a quarantined chunk has no ledger line
  and cannot promote, and a mixed candidate counts only its accepted-chunk
  occurrences;
* the B5 combined mixed-script allowlist (bible + glossary + manual +
  source-derived) comes from the real book-run ``--mixed-script-allow`` flag
  (same input as the strict driver, no divergent duplicate flag) — an
  allowlisted token is never recorded and cannot promote (reviews F3/RV3);
* the promoted glossary entries are FLAT ``{source: target}`` on disk;
* strict conservative term alignment: co-occurring unrelated terms (e.g.
  ``bound``/``together`` next to ``pact``) never share a target and never
  promote (B9-RV2/RV3);
* B9-F5 fail-closed: ``accepted_degraded`` + quarantined chunk + a
  missing/corrupt/empty/incomplete ``chunk_plan.json`` yields ZERO candidate
  generation, no ledger line, no observation and no glossary mutation —
  unavailable PID->chunk provenance never lets unproven evidence through;
* B9-F7 (review B9-RV6) fail-closed: the ``accepted_degraded`` evidence gate
  accepts ONLY the canonical ``ChunkPlanArtifact`` payload of the strict run
  (artifact tag, snapshot/plan identity bound to the run record,
  content-derived plan_hash, exact shape) whose ``selection_results.json``
  binds to the same run with plan-exact chunk membership. A foreign/forged
  plan, a plan-vs-selection membership mismatch, an identity mismatch, or
  malformed/missing selection records fail closed (zero candidates, no
  ledger, no observation, no glossary mutation); candidate evidence may
  originate ONLY in authoritatively ``selected`` chunks — quarantined /
  error / failed / unknown / needs_synthesis / incomplete_generation chunks
  contribute no pids; valid all-selected and mixed selected+quarantined
  chapters keep their normal and selected-only behavior;
* B9-F8 (review B9-RV7) fail-closed: the accepted_degraded gate is no longer
  a weaker shadow subset of the canonical contract — every plan chunk is
  reconstructed through the real ``ChunkPlan`` constructor (word-window
  invariants: total below ``ChunkPlan.MIN_WORDS`` without
  ``undersized_exception``, or above ``ChunkPlan.MAX_WORDS`` regardless of
  the flag, fails closed; legal undersized chunks with the flag are
  preserved) and plan PID ownership must be EXACTLY the current chapter
  source PID set (missing and extra pids fail closed). Positive fixtures are
  genuine constructible ``ChunkPlanArtifact`` payloads; an actual-style
  selected+quarantined mixed plan still promotes selected-only evidence.

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
from pact_v4.phase1.models import canonical_json_hash

# Valid-format sha256 identity shared by a chapter's artifacts (the strict
# driver derives it from content; the tests only need one consistent hash).
_SNAPSHOT_HASH = "1" * 64


def _canonical_word_counts(n: int) -> list:
    """Per-PID source word counts that make a chunk a GENUINE constructible
    canonical ``ChunkPlan``: the total sits inside
    ``[ChunkPlan.MIN_WORDS=280, ChunkPlan.MAX_WORDS=640]`` (300 words split
    evenly), so positive fixtures are real ``ChunkPlanArtifact`` chunks.

    B9-RV7 (review F1): the old ``[1]*n`` default produced structurally
    impossible payloads (total below the soft minimum with
    ``undersized_exception=False``) — ``ChunkPlan`` itself rejects them, so
    a positive fixture must use in-window totals (or an explicit
    ``undersized_exception`` override).
    """
    base, remainder = divmod(300, n)
    return [base + (1 if i < remainder else 0) for i in range(n)]


def _plan_payload(chunks):
    """Canonical ``ChunkPlanArtifact`` payload with a real content-derived
    ``plan_hash`` — the shape ``_validate_canonical_chunk_plan`` accepts.

    Per-chunk ``word_counts`` / ``context`` / ``undersized_exception`` may
    be overridden (e.g. to build a deliberately noncanonical plan for a
    B9-F8 fail-closed test); the defaults are genuine constructible
    ``ChunkPlan`` chunks (B9-RV7)."""
    identity = {
        "artifact": "pact-v4-chunk-plan/v1",
        "snapshot_hash": _SNAPSHOT_HASH,
        "chunks": [
            {
                "chunk_id": c["chunk_id"],
                "snapshot_hash": _SNAPSHOT_HASH,
                "pids": list(c["pids"]),
                "word_counts": list(
                    c.get("word_counts")
                    or _canonical_word_counts(len(c["pids"]))
                ),
                "context": c.get("context", {"left_ru": "", "right_en": []}),
                "undersized_exception": c.get("undersized_exception", False),
            }
            for c in chunks
        ],
    }
    return {**identity, "plan_hash": canonical_json_hash(identity)}


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
    statuses: dict = None,
    record_identities: dict = None,
    selection_identities: dict = None,
) -> None:
    """Pre-populate the per-chapter out_dir the way the strict driver would.

    ``statuses`` overrides the per-chunk selection status
    (``{chunk_id: status}``); default: quarantined chunks -> ``quarantined``,
    everything else -> ``selected``. ``record_identities`` /
    ``selection_identities`` override the identity fields written into the
    strict record / selection results (default: taken from the plan's
    ``snapshot_hash`` / ``plan_hash``).
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    snapshot_hash = chunk_plan.get("snapshot_hash")
    plan_hash = chunk_plan.get("plan_hash")
    chunk_ids = [c["chunk_id"] for c in chunk_plan["chunks"]]
    results = []
    for chunk_id in chunk_ids:
        if statuses is not None:
            status = statuses.get(chunk_id, "selected")
        else:
            status = "quarantined" if chunk_id in quarantined else "selected"
        results.append({
            "chunk_id": chunk_id,
            "status": status,
            "quarantine_reason": "qwen_fidelity" if status == "quarantined" else None,
        })
    selection = {"chapter_id": chapter_id, "results": results}
    if snapshot_hash is not None:
        selection["snapshot_hash"] = snapshot_hash
        selection["chunk_plan_hash"] = plan_hash
    if selection_identities is not None:
        selection.update(selection_identities)
    out_dir.joinpath("selection_results.json").write_text(
        json.dumps(selection, ensure_ascii=False),
        encoding="utf-8",
    )
    record = {"chapter_id": chapter_id,
              "step8": {"status": terminal_status}}
    if snapshot_hash is not None:
        record["identities"] = {
            "snapshot_hash": snapshot_hash,
            "chunk_plan_hash": plan_hash,
        }
    if record_identities is not None:
        record["identities"] = record_identities
    out_dir.joinpath("strict_chapter_trial_record.json").write_text(
        json.dumps(record, ensure_ascii=False),
        encoding="utf-8",
    )
    out_dir.joinpath("translations.json").write_text(
        json.dumps(translations, ensure_ascii=False), encoding="utf-8",
    )
    out_dir.joinpath("chunk_plan.json").write_text(
        json.dumps(chunk_plan, ensure_ascii=False), encoding="utf-8",
    )


# ---------------------------------------------------------------------------
# Two-chapter accumulation + promotion (B9-I2 req 5, Variant B)
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

_PLAN = _plan_payload([
    {"chunk_id": "chunk0001", "pids": ["p00001", "p00002", "p00003"]},
    {"chunk_id": "chunk0002", "pids": ["p00004", "p00005", "p00006"]},
])


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

    def test_two_chapters_accumulate_promote_flat_glossary(
        self, tmp_path, monkeypatch,
    ):
        """Variant B: Blake promotes at ch1, pact at ch2; glossary stays flat."""
        memory, out_base, result = self._run(tmp_path, monkeypatch, {
            "0001": (_CH1_HTML, "complete", [], _CH1_TRANSLATIONS),
            "0002": (_CH2_HTML, "complete", [], _CH2_TRANSLATIONS),
        })

        # Both chapters reached complete and promoted observations.
        assert [r["terminal_status"] for r in result["chapters"]] == [
            "complete", "complete",
        ]

        # Per-chapter candidates blocks (B9-I2 req 4): proper_name promotes
        # at ch1 (4 occurrences >= 2, single target); term waits for the
        # second chapter.
        ch1, ch2 = result["chapters"]
        assert ch1["candidates"] == {"generated": 2, "promoted": 1, "conflicts": 0}
        assert ch2["candidates"] == {"generated": 1, "promoted": 1, "conflicts": 0}

        # Variant B: glossary.json updated with FLAT {source: target} pairs.
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
        # so the 2-chapter term threshold can never be reached by failed text.
        assert pact["total_occurrences"] == 3
        assert [c["chapter_id"] for c in pact["chapters"]] == ["0002"]
        # The failed chapter's proper-name candidate never entered the ledger.
        assert "proper_name|blake" not in records

        # With a single accepted chapter, pact (term) is below the 2-chapter
        # threshold and Blake never appeared — nothing promoted.
        assert result["chapters"][1]["candidates"] == {
            "generated": 1, "promoted": 0, "conflicts": 0,
        }
        glossary = json.loads((memory / "glossary.json").read_text(encoding="utf-8"))
        assert glossary == {}

    def test_quarantined_candidate_never_enters_ledger_or_promotes(
        self, tmp_path, monkeypatch,
    ):
        """B9-RV3: accepted_degraded + quarantined chunk: a candidate wholly
        from a quarantined chunk has NO ledger line and NO promotion.

        The old (Variant A) test asserted the candidate WAS shadow-recorded;
        B9-RV3 requires quarantined-chunk evidence to be excluded BEFORE
        ledger accumulation and auto-promotion.
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
            "generated": 0, "promoted": 0, "conflicts": 0,
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
            "generated": 1, "promoted": 1, "conflicts": 0,
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
        # the conservative guard strips the shared target from every
        # competing candidate, so nothing can promote.
        assert result["chapters"][0]["candidates"] == {
            "generated": 6, "promoted": 0, "conflicts": 6,
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
        # promotes under Variant B (2 proper_name occurrences, single target).
        assert "proper_name|beasley" in records
        assert result["chapters"][0]["candidates"] == {
            "generated": 1, "promoted": 1, "conflicts": 0,
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
# B9-F5: fail closed on unavailable quarantined-chunk provenance
# ---------------------------------------------------------------------------


class TestQuarantinedProvenanceFailClosed:
    """B9-F5: accepted_degraded + quarantined chunks must fail closed when
    ``chunk_plan.json`` cannot authoritatively exclude the quarantined
    evidence.

    A missing, corrupt, empty, or incomplete PID->chunk plan leaves
    source/translation pids of unknown provenance — they could belong to a
    quarantined chunk — so the chapter must generate zero candidates, append
    no ledger line, create no observation and mutate no glossary (the book
    run never crashes).
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
            # A canonical, identity-bound plan that still omits some of the
            # chapter's pids (p00002/p00003 are not owned by any chunk) —
            # their provenance is unknown, so the candidate loop fails closed
            # (B9-F7 incomplete-provenance check inside the generator).
            plan_path.write_text(json.dumps(_plan_payload([
                {"chunk_id": "chunk0001", "pids": ["p00001"]},
                {"chunk_id": "chunk0002",
                 "pids": ["p00004", "p00005", "p00006"]},
            ])), encoding="utf-8")
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
            "generated": 0, "promoted": 0, "conflicts": 0,
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
            "generated": 1, "promoted": 0, "conflicts": 0,
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
        duplicate_plan = _plan_payload(chunks)
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
            "generated": 0, "promoted": 0, "conflicts": 0,
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
# B9-F7: accepted_degraded evidence bound to authoritative selected-chunk
# provenance (canonical identity-bound chunk plan + selection results)
# ---------------------------------------------------------------------------


class TestAcceptedDegradedAuthoritativeProvenance:
    """B9-F7 (review B9-RV6): the accepted_degraded candidate loop accepts
    evidence ONLY from chunks authoritatively ``selected`` in
    ``selection_results.json``, and only when ``chunk_plan.json`` is the
    canonical ``ChunkPlanArtifact`` payload of THIS strict run (artifact tag,
    snapshot/plan identity bound to the run record, content-derived
    plan_hash, exact contract shape) with plan-vs-selection membership
    agreement. Foreign/missing/corrupt/inconsistent provenance fails closed:
    zero candidates, no ledger line, no observation, no glossary mutation
    (warning logged, no crash).

    B9-F8 (review B9-RV7) extends the same gate to the FULL canonical
    contract: chunk word-window invariants (via the real ``ChunkPlan``
    constructor) and EXACT source-PID ownership; the positive fixtures are
    genuine constructible ``ChunkPlanArtifact`` payloads."""

    def _run(self, tmp_path, monkeypatch, html, translations,
             chunk_plan=_PLAN, *, statuses=None, record_identities=None,
             selection_identities=None, terminal_status="accepted_degraded",
             selection_results=None):
        from pact_full_pipeline_runner_v1 import v4_book_run

        memory = _setup_memory(tmp_path)
        out_base = tmp_path / "out"
        src_dir = tmp_path / "src"

        def fake_run_one(*args, **kwargs):
            return {"status": "ok"}

        monkeypatch.setattr(v4_book_run, "_run_one_chapter", fake_run_one)

        _write_chapter_html(src_dir, "0001", html)
        out_dir = out_base / "chapter_0001"
        _make_chapter_artifacts(
            out_dir, "0001",
            terminal_status=terminal_status, quarantined=[],
            translations=translations, chunk_plan=chunk_plan,
            statuses=statuses, record_identities=record_identities,
            selection_identities=selection_identities,
        )
        if selection_results is not None:
            # Full override of selection_results.json (e.g. a selection
            # listing chunks the plan does not own).
            out_dir.joinpath("selection_results.json").write_text(
                json.dumps(selection_results, ensure_ascii=False),
                encoding="utf-8",
            )

        result = v4_book_run.run_book(
            memory_dir=memory,
            chapter_ids=["0001"],
            chapter_html_pattern=str(src_dir / "{chapter_id}.html"),
            out_base=out_base,
        )
        return memory, out_base, result

    def test_forged_plan_selection_quarantined_mismatch_fails_closed(
        self, tmp_path, monkeypatch,
    ):
        """Finding 1 (B9-RV6 HIGH): strict record accepted_degraded, selection
        says chunk_q is quarantined, but the plan maps the Blake pids to an
        unrelated forged_accepted chunk. The foreign plan is not bound to the
        run's selection: fail closed — no Blake candidate, no ledger, no
        observation, no glossary mutation."""
        html = ("<p>He met Blake at the gate. Blake knew the way.</p>\n"
                "<p>Blake waited outside for Mary.</p>")
        translations = {
            "p00001": "Блэйк встретил Блэйка у ворот. Блэйк знал дорогу.",
            "p00002": "Блэйк ждал снаружи Мэри.",
        }
        forged_plan = _plan_payload([
            {"chunk_id": "forged_accepted", "pids": ["p00001", "p00002"]},
        ])
        # The selection results of the run name the REAL quarantined chunk
        # (chunk_q), which the forged plan never owns -> plan-vs-selection
        # membership disagreement.
        selection_results = {
            "chapter_id": "0001",
            "snapshot_hash": forged_plan["snapshot_hash"],
            "chunk_plan_hash": forged_plan["plan_hash"],
            "results": [{"chunk_id": "chunk_q", "status": "quarantined"}],
        }
        memory, out_base, result = self._run(
            tmp_path, monkeypatch, html, translations,
            chunk_plan=forged_plan, selection_results=selection_results,
        )

        assert result["chapters"][0]["terminal_status"] == "accepted_degraded"
        assert result["chapters"][0]["candidates"] == {
            "generated": 0, "promoted": 0, "conflicts": 0,
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

    def test_nonselected_error_status_evidence_excluded(self, tmp_path, monkeypatch):
        """Finding 2 (B9-RV6 HIGH): a fully mapped accepted_degraded chapter
        whose chunk has selection status ``error`` (no quarantined chunk) must
        NOT promote from that non-accepted chunk. Only proven ``selected``
        evidence may generate candidates."""
        html = ("<p>He met Blake at the gate. Blake knew the way.</p>\n"
                "<p>Blake waited outside for Mary.</p>")
        translations = {
            "p00001": "Блэйк встретил Блэйка у ворот. Блэйк знал дорогу.",
            "p00002": "Блэйк ждал снаружи Мэри.",
        }
        plan = _plan_payload([
            {"chunk_id": "chunk0001", "pids": ["p00001", "p00002"]},
        ])
        memory, out_base, result = self._run(
            tmp_path, monkeypatch, html, translations,
            chunk_plan=plan, statuses={"chunk0001": "error"},
        )

        assert result["chapters"][0]["candidates"] == {
            "generated": 0, "promoted": 0, "conflicts": 0,
        }
        records = GlossaryCandidateLedger(
            str(out_base / "glossary_candidates.json")
        ).load()
        assert "proper_name|blake" not in records
        assert records == {}
        glossary = json.loads((memory / "glossary.json").read_text(encoding="utf-8"))
        assert glossary == {}
        observations = json.loads(
            (memory / "observations.json").read_text(encoding="utf-8")
        )
        assert observations.get("glossary", {}) == {}

    def test_mixed_selected_quarantined_keeps_only_selected_evidence(
        self, tmp_path, monkeypatch,
    ):
        """Requirement 3: a mixed accepted_degraded chapter retains only the
        selected chunk's PID/chunk evidence; the wholly-quarantined candidate
        is absent and the mixed candidate's ledger/promotion provenance
        carries only selected chunks."""
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
        memory, out_base, result = self._run(
            tmp_path, monkeypatch, html, translations,
            statuses={"chunk0001": "quarantined", "chunk0002": "selected"},
        )

        # Blake's 4 accepted-chunk occurrences promote with accepted
        # provenance; the quarantined chunk's occurrences never count.
        assert result["chapters"][0]["candidates"] == {
            "generated": 1, "promoted": 1, "conflicts": 0,
        }
        records = GlossaryCandidateLedger(
            str(out_base / "glossary_candidates.json")
        ).load()
        blake = records["proper_name|blake"]
        assert blake["total_occurrences"] == 4  # not 8
        assert blake["chapters"][0]["chunk_ids"] == ["chunk0002"]
        assert set(blake["chapters"][0]["chunk_ids"]) <= {"chunk0002"}
        glossary = json.loads((memory / "glossary.json").read_text(encoding="utf-8"))
        assert glossary == {"Blake": "Блэйк"}

    def test_all_selected_accepted_degraded_generates_normally(
        self, tmp_path, monkeypatch,
    ):
        """Requirement 3/5(d): a fully mapped accepted_degraded chapter with
        every chunk authoritatively selected behaves like normal generation —
        candidates, ledger and promotion all work."""
        memory, out_base, result = self._run(
            tmp_path, monkeypatch, _CH1_HTML, _CH1_TRANSLATIONS,
        )
        assert result["chapters"][0]["candidates"] == {
            "generated": 2, "promoted": 1, "conflicts": 0,
        }
        records = GlossaryCandidateLedger(
            str(out_base / "glossary_candidates.json")
        ).load()
        # Blake lives in chunk0001 (p00001-p00003); all chunks are selected
        # so the full chapter evidence is used and promotion is normal.
        assert records["proper_name|blake"]["chapters"][0]["chunk_ids"] == [
            "chunk0001",
        ]
        glossary = json.loads((memory / "glossary.json").read_text(encoding="utf-8"))
        assert glossary == {"Blake": "Блэйк"}

    def test_foreign_record_plan_identity_fails_closed(self, tmp_path, monkeypatch):
        """Artifact/snapshot/plan identity mismatch: the plan is canonical but
        the strict record's identities point at a different snapshot/plan —
        the plan is not the artifact of THIS run -> fail closed."""
        html = ("<p>He met Blake at the gate. Blake knew the way.</p>\n"
                "<p>Blake waited outside for Mary.</p>")
        translations = {
            "p00001": "Блэйк встретил Блэйка у ворот. Блэйк знал дорогу.",
            "p00002": "Блэйк ждал снаружи Мэри.",
        }
        memory, out_base, result = self._run(
            tmp_path, monkeypatch, html, translations,
            record_identities={
                "snapshot_hash": "2" * 64, "chunk_plan_hash": "3" * 64,
            },
        )
        assert result["chapters"][0]["candidates"] == {
            "generated": 0, "promoted": 0, "conflicts": 0,
        }
        records = GlossaryCandidateLedger(
            str(out_base / "glossary_candidates.json")
        ).load()
        assert records == {}
        glossary = json.loads((memory / "glossary.json").read_text(encoding="utf-8"))
        assert glossary == {}

    def test_foreign_selection_identity_fails_closed(self, tmp_path, monkeypatch):
        """Selection results bound to a different run (snapshot/plan identity
        mismatch) are not authoritative -> fail closed."""
        html = ("<p>He met Blake at the gate. Blake knew the way.</p>\n"
                "<p>Blake waited outside for Mary.</p>")
        translations = {
            "p00001": "Блэйк встретил Блэйка у ворот. Блэйк знал дорогу.",
            "p00002": "Блэйк ждал снаружи Мэри.",
        }
        memory, out_base, result = self._run(
            tmp_path, monkeypatch, html, translations,
            selection_identities={"snapshot_hash": "4" * 64},
        )
        assert result["chapters"][0]["candidates"] == {
            "generated": 0, "promoted": 0, "conflicts": 0,
        }
        records = GlossaryCandidateLedger(
            str(out_base / "glossary_candidates.json")
        ).load()
        assert records == {}

    def test_malformed_selection_results_fails_closed(self, tmp_path, monkeypatch):
        """A selection record missing its status is malformed -> fail closed
        (no evidence can be proven selected)."""
        html = ("<p>He met Blake at the gate. Blake knew the way.</p>\n"
                "<p>Blake waited outside for Mary.</p>")
        translations = {
            "p00001": "Блэйк встретил Блэйка у ворот. Блэйк знал дорогу.",
            "p00002": "Блэйк ждал снаружи Мэри.",
        }
        selection_results = {
            "chapter_id": "0001",
            "snapshot_hash": _PLAN["snapshot_hash"],
            "chunk_plan_hash": _PLAN["plan_hash"],
            "results": [{"chunk_id": "chunk0001"}],  # missing status
        }
        memory, out_base, result = self._run(
            tmp_path, monkeypatch, html, translations,
            selection_results=selection_results,
        )
        assert result["chapters"][0]["candidates"] == {
            "generated": 0, "promoted": 0, "conflicts": 0,
        }
        records = GlossaryCandidateLedger(
            str(out_base / "glossary_candidates.json")
        ).load()
        assert records == {}
        glossary = json.loads((memory / "glossary.json").read_text(encoding="utf-8"))
        assert glossary == {}

    def test_missing_selection_results_fails_closed(self, tmp_path, monkeypatch):
        """accepted_degraded with NO selection_results.json: provenance is
        unavailable — fail closed (the old code read it as 'no quarantined
        chunks' and generated from all evidence)."""
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
            out_dir, "0001", terminal_status="accepted_degraded",
            quarantined=[], translations=_CH1_TRANSLATIONS, chunk_plan=_PLAN,
        )
        (out_dir / "selection_results.json").unlink()

        result = v4_book_run.run_book(
            memory_dir=memory, chapter_ids=["0001"],
            chapter_html_pattern=str(src_dir / "{chapter_id}.html"),
            out_base=out_base,
        )
        assert result["chapters"][0]["candidates"] == {
            "generated": 0, "promoted": 0, "conflicts": 0,
        }
        records = GlossaryCandidateLedger(
            str(out_base / "glossary_candidates.json")
        ).load()
        assert records == {}
        glossary = json.loads((memory / "glossary.json").read_text(encoding="utf-8"))
        assert glossary == {}

    # ------------------------------------------------------------------
    # B9-F8 (review B9-RV7): the accepted_degraded gate enforces the FULL
    # canonical ChunkPlanArtifact contract — ChunkPlan word-window
    # invariants and EXACT source-PID ownership — not a weaker shadow
    # subset. Every invalid condition fails closed (zero candidates, no
    # ledger, no observation, no glossary mutation, warning logged, no
    # crash); legal undersized chunks (undersized_exception=True) and an
    # actual-style selected+quarantined mix keep working.
    # ------------------------------------------------------------------

    def _assert_fail_closed(self, memory, out_base, result):
        assert result["chapters"][0]["terminal_status"] == "accepted_degraded"
        assert result["chapters"][0]["candidates"] == {
            "generated": 0, "promoted": 0, "conflicts": 0,
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

    def test_undersized_false_flag_fails_closed(self, tmp_path, monkeypatch, caplog):
        """B9-RV7 repro: a plan whose chunk total words are below
        ``ChunkPlan.MIN_WORDS`` with ``undersized_exception=False`` is
        structurally impossible as a canonical ``ChunkPlanArtifact`` (the
        real ``ChunkPlan`` constructor rejects it) — fail closed. The old
        gate accepted this payload and promoted Blake from it."""
        plan = _plan_payload([
            {"chunk_id": "chunk0001",
             "pids": ["p00001", "p00002", "p00003",
                      "p00004", "p00005", "p00006"],
             "word_counts": [1, 1, 1, 1, 1, 1]},
        ])
        memory, out_base, result = self._run(
            tmp_path, monkeypatch, _CH1_HTML, _CH1_TRANSLATIONS,
            chunk_plan=plan,
        )
        self._assert_fail_closed(memory, out_base, result)
        assert any("B9-F7/B9-F8" in r.message for r in caplog.records)

    def test_oversized_chunk_fails_closed(self, tmp_path, monkeypatch, caplog):
        """Total words above ``ChunkPlan.MAX_WORDS`` are rejected even with
        ``undersized_exception=True`` — the flag never relaxes the hard
        maximum (ChunkPlan enforces it unconditionally)."""
        plan = _plan_payload([
            {"chunk_id": "chunk0001",
             "pids": ["p00001", "p00002", "p00003",
                      "p00004", "p00005", "p00006"],
             "word_counts": [200, 200, 200, 200, 200, 200],
             "undersized_exception": True},
        ])
        memory, out_base, result = self._run(
            tmp_path, monkeypatch, _CH1_HTML, _CH1_TRANSLATIONS,
            chunk_plan=plan,
        )
        self._assert_fail_closed(memory, out_base, result)
        assert any("B9-F7/B9-F8" in r.message for r in caplog.records)

    def test_missing_source_pid_fails_closed(self, tmp_path, monkeypatch, caplog):
        """A canonical-valid plan that does not own EVERY chapter source
        pid (p00006 is missing) fails closed — ownership must be exact, not
        a subset."""
        plan = _plan_payload([
            {"chunk_id": "chunk0001",
             "pids": ["p00001", "p00002", "p00003",
                      "p00004", "p00005"]},
        ])
        memory, out_base, result = self._run(
            tmp_path, monkeypatch, _CH1_HTML, _CH1_TRANSLATIONS,
            chunk_plan=plan,
        )
        self._assert_fail_closed(memory, out_base, result)
        assert any("B9-F7/B9-F8" in r.message for r in caplog.records)

    def test_extra_pid_ownership_fails_closed(self, tmp_path, monkeypatch, caplog):
        """B9-RV7 repro: a plan that owns PIDs OUTSIDE the chapter source
        (extra p00007) passed the old ``present_pids <= plan_pids`` check
        and generated/promoted evidence — exact ownership fails closed."""
        plan = _plan_payload([
            {"chunk_id": "chunk0001",
             "pids": ["p00001", "p00002", "p00003"]},
            {"chunk_id": "chunk0002",
             "pids": ["p00004", "p00005", "p00006", "p00007"]},
        ])
        memory, out_base, result = self._run(
            tmp_path, monkeypatch, _CH1_HTML, _CH1_TRANSLATIONS,
            chunk_plan=plan,
        )
        self._assert_fail_closed(memory, out_base, result)
        assert any("B9-F7/B9-F8" in r.message for r in caplog.records)

    def test_legal_undersized_exception_still_generates(self, tmp_path, monkeypatch):
        """Legal undersized chunk: total below ``ChunkPlan.MIN_WORDS`` WITH
        ``undersized_exception=True`` is a constructible canonical
        ``ChunkPlan`` (the canonical model permits it) — the gate must NOT
        over-reject: generation, ledger and promotion keep working."""
        plan = _plan_payload([
            {"chunk_id": "chunk0001",
             "pids": ["p00001", "p00002", "p00003",
                      "p00004", "p00005", "p00006"],
             "word_counts": [10, 10, 10, 10, 10, 10],
             "undersized_exception": True},
        ])
        memory, out_base, result = self._run(
            tmp_path, monkeypatch, _CH1_HTML, _CH1_TRANSLATIONS,
            chunk_plan=plan,
        )
        # Same normal behavior as test_all_selected_accepted_degraded_generates_normally.
        assert result["chapters"][0]["candidates"] == {
            "generated": 2, "promoted": 1, "conflicts": 0,
        }
        records = GlossaryCandidateLedger(
            str(out_base / "glossary_candidates.json")
        ).load()
        assert records["proper_name|blake"]["chapters"][0]["chunk_ids"] == [
            "chunk0001",
        ]
        glossary = json.loads((memory / "glossary.json").read_text(encoding="utf-8"))
        assert glossary == {"Blake": "Блэйк"}

    def test_actual_style_mixed_selected_quarantined_promotes(
        self, tmp_path, monkeypatch,
    ):
        """B9-RV7 positive: an ACTUAL-style canonical plan — many pids per
        chunk, per-chunk totals inside the 280-640 word window — with a
        selected + quarantined mix proves selected-only evidence still
        promotes correctly (the quarantined chunk's Blake occurrences never
        count)."""
        quarantined_paras = [
            "<p>Blake walked to the gate and Blake met the guard.</p>",
            "<p>Blake knew the old road well.</p>",
            "<p>Blake waited outside for Mary.</p>",
            "<p>Blake returned home very late.</p>",
            "<p>Blake heard a distant bell.</p>",
            "<p>Blake nodded to the keeper.</p>",
            "<p>Blake left before the dawn.</p>",
            "<p>Blake closed the heavy door.</p>",
        ]
        selected_paras = [
            "<p>He met Blake at the gate. Blake knew the way.</p>",
            "<p>Blake waited outside for Mary.</p>",
            "<p>Blake returned home later.</p>",
            "<p>He saw Blake return home. Blake waved once more.</p>",
            "<p>He met Blake again.</p>",
            "<p>Blake left at noon.</p>",
            "<p>The pact bound them all together.</p>",
            "<p>The pact held firm against time.</p>",
            "<p>The pact was old and strong.</p>",
        ]
        html = "\n".join(quarantined_paras + selected_paras)
        translations = {
            f"p{i:05d}": "Блэйк снова вышел к воротам." for i in range(1, 15)
        }
        translations["p00015"] = "Пакт связывал их всех вместе."
        translations["p00016"] = "Пакт держался крепко против времени."
        translations["p00017"] = "Пакт был старым и сильным."
        plan = _plan_payload([
            {"chunk_id": "chunk_q",
             "pids": [f"p{i:05d}" for i in range(1, 9)],
             "word_counts": [38] * 8},   # 304 words — inside the window
            {"chunk_id": "chunk_a",
             "pids": [f"p{i:05d}" for i in range(9, 18)],
             "word_counts": [34] * 9},   # 306 words — inside the window
        ])
        memory, out_base, result = self._run(
            tmp_path, monkeypatch, html, translations,
            chunk_plan=plan,
            statuses={"chunk_q": "quarantined", "chunk_a": "selected"},
        )
        assert result["chapters"][0]["candidates"] == {
            "generated": 2, "promoted": 1, "conflicts": 0,
        }
        records = GlossaryCandidateLedger(
            str(out_base / "glossary_candidates.json")
        ).load()
        blake = records["proper_name|blake"]
        # Only the SELECTED chunk's 8 Blake occurrences count — the
        # quarantined chunk's Blake text never contributed evidence.
        assert blake["total_occurrences"] == 8
        assert blake["chapters"][0]["chunk_ids"] == ["chunk_a"]
        glossary = json.loads((memory / "glossary.json").read_text(encoding="utf-8"))
        assert glossary == {"Blake": "Блэйк"}


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
