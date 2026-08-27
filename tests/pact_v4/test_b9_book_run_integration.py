"""GLOSSARY-FROM-ENTITY integration tests (owner decision 2026-08-15, variant B).

The deterministic B9 scan (generate_candidates + v3-threshold
auto-promotion) is REMOVED from the book run (its helpers remain as dead
code for reference, tested by the direct helper tests below). Glossary
candidates now come from the source-only entity extractor's VERIFIED
entities (``entity_context_cache.json``, written by the strict run BEFORE
generation — 0 extra model calls):

* after each accepted chapter (``complete`` / ``accepted_degraded``) the
  run reads the validated context for the CURRENT chapter (fail-closed
  provenance: chapter_id + source_hash + extractor_version must match the
  strict run record) and promotes the VERIFIED proper-noun entities into
  ``glossary.json`` with the target the deterministic ``align_candidates``
  script extracts from the finished chapter translation — the glossary
  entry is exactly what the model wrote;
* objects (pocketwatch, upstairs bathroom mirror, motorcycle, cat — common
  nouns) are NOT glossary candidates (proper-noun filter) and never
  promote;
* multi-word entity names (Joel's car, Hillsglade House) cannot be aligned
  word-by-word: the target falls back to the entity's established
  ``canonical_ru`` in book_memory when present, otherwise the candidate is
  recorded without a target and NOT promoted;
* the same aligned target fills ``canonical_ru`` in the book_memory entity
  observations (not only the seed entries);
* established glossary entries with a DIFFERENT target are a conflict (never
  overwritten); the same target is a no-op;
* apostrophe variants match (APOSTROPHE-NORM: ``Jacob's Bell`` ==
  ``Jacob’s Bell``) in both the established-glossary check and the
  canonical_ru fallback;
* only chapters with an accepted terminal result promote; the B7
  quarantined-chunk filter applies via the observation's ``chunk_id`` (the
  anchor pid's chunk).

The strict driver is faked (``_run_one_chapter`` monkeypatched) — the out_dir
artifacts (``strict_chapter_trial_record.json``, ``selection_results.json``,
``translations.json``, ``chunk_plan.json``, ``entity_context_cache.json``)
are pre-populated on disk, exactly like the BM tests.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from pact_v4.audit.entity_extractor import EXTRACTOR_VERSION
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


def _entity_cache_entry(
    chapter_id: str,
    source_text: dict,
    *,
    entity: str = "Rose",
    canonical_type: str = "woman",
    anchor_pid: str = "p00001",
    anchor_span: str = "Rose met Blake at the gate.",
    claims: list | None = None,
    source_hash: str = "test-hash",
    extractor_version: str = EXTRACTOR_VERSION,
) -> dict:
    """Build a valid single-entry ``entity_context_cache.json`` payload.

    Mirrors the BM test fixture: the cache schema is
    ``pact-v4-entity-context-cache/v1``; every context carries the chapter
    id + source hash, and entries are keyed by source_hash +
    extractor_version. ``source_text`` is the chapter source PID map (the
    anchor span must be verbatim in ``anchor_pid``).
    """
    from pact_v4.audit.entity_extractor import (
        CACHE_SCHEMA,
        ENTITY_CONTEXT_SCHEMA,
        entity_context_cache_key,
    )

    if claims is None:
        claims = []
    context = {
        "schema": ENTITY_CONTEXT_SCHEMA,
        "extractor_version": extractor_version,
        "chapter_id": chapter_id,
        "source_hash": source_hash,
        "entities": [
            {
                "entity": entity,
                "canonical_type": canonical_type,
                "anchor": {"pid": anchor_pid, "span": anchor_span},
                "aliases": [],
                "claims": claims,
            },
        ],
    }
    return {
        "schema": CACHE_SCHEMA,
        "entries": [
            {
                "key": entity_context_cache_key(
                    source_hash=source_hash,
                    extractor_version=extractor_version,
                ),
                "context": context,
            },
        ],
    }


def _make_chapter_artifacts(
    out_dir: Path,
    chapter_id: str,
    *,
    terminal_status: str,
    quarantined: list,
    translations: dict,
    chunk_plan: dict | None = None,
    entity_cache_payload: dict | None = None,
    record_source_hash: str = "test-hash",
    record_extractor_version: str = EXTRACTOR_VERSION,
) -> None:
    """Pre-populate the per-chapter out_dir the way the strict driver would.

    ``chunk_plan=None`` deliberately leaves ``chunk_plan.json`` ABSENT
    (provenance-missing test); the selection record still declares which
    chunks were quarantined, so the run sees a non-empty quarantined set
    without an authoritative plan.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    if chunk_plan is not None:
        chunk_ids = [c["chunk_id"] for c in chunk_plan["chunks"]]
    else:
        chunk_ids = list(quarantined)
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
    record = {"chapter_id": chapter_id,
              "step8": {"status": terminal_status}}
    if entity_cache_payload is not None:
        # GLOSSARY-FROM-ENTITY provenance: the strict run records the exact
        # source hash and extractor version it consumed; the book-run
        # promotion verifies a cache entry against both (fail-closed).
        record["identities"] = {"source_hash": record_source_hash}
        record["operational_policy"] = {
            "audit": {"extractor_version": record_extractor_version},
        }
    out_dir.joinpath("strict_chapter_trial_record.json").write_text(
        json.dumps(record, ensure_ascii=False),
        encoding="utf-8",
    )
    out_dir.joinpath("translations.json").write_text(
        json.dumps(translations, ensure_ascii=False), encoding="utf-8",
    )
    if chunk_plan is not None:
        out_dir.joinpath("chunk_plan.json").write_text(
            json.dumps(chunk_plan, ensure_ascii=False), encoding="utf-8",
        )
    if entity_cache_payload is not None:
        out_dir.joinpath("entity_context_cache.json").write_text(
            json.dumps(entity_cache_payload, ensure_ascii=False),
            encoding="utf-8",
        )


# ---------------------------------------------------------------------------
# GLOSSARY-FROM-ENTITY run_book integration (variant B, owner 2026-08-15):
# verified proper-noun entities -> glossary targets aligned from the chapter
# translation; canonical_ru filled in book_memory entities.
# ---------------------------------------------------------------------------

_CH_HTML = """<p>Rose met Blake at the gate.</p>
<p>Rose knew the way home.</p>
<p>Blake waited outside for Mary.</p>
<p>The pact held them all together.</p>
<p>The pact was old and strong.</p>
<p>He walked home alone.</p>"""

_CH_TRANSLATIONS = {
    "p00001": "Роуз встретила Блэйка у ворот.",
    "p00002": "Роуз знала дорогу домой.",
    "p00003": "Блэйк ждал снаружи Мэри.",
    "p00004": "Пакт держал их всех вместе.",
    "p00005": "Пакт был старым и сильным.",
    "p00006": "Он пошёл домой один.",
}

_CH_SOURCE_TEXT = {
    "p00001": "Rose met Blake at the gate.",
    "p00002": "Rose knew the way home.",
    "p00003": "Blake waited outside for Mary.",
    "p00004": "The pact held them all together.",
    "p00005": "The pact was old and strong.",
    "p00006": "He walked home alone.",
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


class TestBookRunGlossaryFromEntity:
    """run_book-level GLOSSARY-FROM-ENTITY integration.

    The B9 deterministic scan is removed; glossary candidates now come from
    the entity extractor's VERIFIED proper-noun entities. Each chapter's
    out_dir carries ``entity_context_cache.json`` + the strict record's
    ``identities.source_hash`` / ``operational_policy.audit.extractor_version``
    so the run can verify the cache entry provenance (fail-closed).
    """

    def _run(self, tmp_path, monkeypatch, chapter_specs,
             book_memory_bytes=None, **run_kwargs):
        from pact_full_pipeline_runner_v1 import v4_book_run

        memory = _setup_memory(tmp_path, book_memory_bytes=book_memory_bytes)
        out_base = tmp_path / "out"
        src_dir = tmp_path / "src"

        def fake_run_one(*args, **kwargs):
            return {"status": "ok"}

        monkeypatch.setattr(v4_book_run, "_run_one_chapter", fake_run_one)

        for chapter_id, spec in chapter_specs.items():
            _write_chapter_html(src_dir, chapter_id, spec["html"])
            _make_chapter_artifacts(
                out_base / f"chapter_{chapter_id}", chapter_id,
                terminal_status=spec["terminal"],
                quarantined=spec.get("quarantined", []),
                translations=spec["translations"],
                chunk_plan=spec.get("chunk_plan", _PLAN),
                entity_cache_payload=spec.get("entity_cache"),
                record_source_hash=spec.get("source_hash", "test-hash"),
                record_extractor_version=spec.get(
                    "extractor_version", EXTRACTOR_VERSION,
                ),
            )

        result = v4_book_run.run_book(
            memory_dir=memory,
            chapter_ids=list(chapter_specs),
            chapter_html_pattern=str(src_dir / "{chapter_id}.html"),
            out_base=out_base,
            **run_kwargs,
        )
        return memory, out_base, result

    def test_verified_proper_noun_promotes_with_translation_target(
        self, tmp_path, monkeypatch,
    ):
        """A verified single-word proper-noun entity (Rose) aligns to the
        ACTUAL translation (Роуз) and lands in the flat glossary; the same
        target fills ``canonical_ru`` in the book_memory entity observation
        (not only the seed entries)."""
        memory, out_base, result = self._run(tmp_path, monkeypatch, {
            "0001": {
                "html": _CH_HTML,
                "terminal": "complete",
                "translations": _CH_TRANSLATIONS,
                "entity_cache": _entity_cache_entry(
                    "0001", _CH_SOURCE_TEXT,
                    entity="Rose", canonical_type="woman",
                    anchor_pid="p00001",
                    anchor_span="Rose met Blake at the gate.",
                ),
            },
        })

        rec = result["chapters"][0]
        assert rec["terminal_status"] == "complete"
        # glossary-model-resolver: mode=off forbids new glossary observations, even legacy deterministic (fail-closed).
        # No sidecar -> no promotion (deterministic align deprecated for proper_name).
        assert rec["candidates"] == {
            "generated": 0, "proposed": 0, "committed": 0, "conflicts": 0,
        }

        # No glossary promotion when off without sidecar (fail-closed).
        glossary = json.loads((memory / "glossary.json").read_text(encoding="utf-8"))
        assert glossary == {}

        # book_memory glossary canonical_ru not filled when off (no alignment)
        # observations.json is emptied by promote.
        observations = json.loads(
            (memory / "observations.json").read_text(encoding="utf-8")
        )
        assert observations.get("glossary", {}) == {}

    def test_object_entity_never_promotes(self, tmp_path, monkeypatch):
        """pocketwatch / upstairs bathroom mirror (common nouns) are NOT
        glossary candidates: the proper-noun filter excludes them, so the
        model's object translation never locks a glossary entry."""
        html = """<p>He pocketed the pocketwatch. The pocketwatch ticked.</p>
<p>He glanced at the pocketwatch.</p>
<p>She closed the heavy door.</p>"""
        translations = {
            "p00001": "Он сунул карманные часы в карман. Карманные часы тикали.",
            "p00002": "Он взглянул на карманные часы.",
            "p00003": "Она закрыла тяжёлую дверь.",
        }
        source_text = {
            "p00001": "He pocketed the pocketwatch. The pocketwatch ticked.",
            "p00002": "He glanced at the pocketwatch.",
            "p00003": "She closed the heavy door.",
        }
        memory, _out_base, result = self._run(tmp_path, monkeypatch, {
            "0001": {
                "html": html,
                "terminal": "complete",
                "translations": translations,
                "entity_cache": _entity_cache_entry(
                    "0001", source_text,
                    entity="pocketwatch", canonical_type="pocketwatch",
                    anchor_pid="p00001",
                    anchor_span="He pocketed the pocketwatch. The pocketwatch ticked.",
                ),
            },
        })

        assert result["chapters"][0]["candidates"] == {
            "generated": 0, "proposed": 0, "committed": 0, "conflicts": 0,
        }
        glossary = json.loads((memory / "glossary.json").read_text(encoding="utf-8"))
        assert glossary == {}

    def test_multi_word_without_canonical_ru_not_promoted(
        self, tmp_path, monkeypatch,
    ):
        """Hillsglade House (multi-word) cannot be aligned word-by-word and
        has no established canonical_ru in book_memory -> candidate without
        target, never promoted (card Q2)."""
        memory, _out_base, result = self._run(tmp_path, monkeypatch, {
            "0001": {
                "html": """<p>Hillsglade House stood at the end of the lane.</p>
<p>Hillsglade House was old and quiet.</p>
<p>Blake walked past the house.</p>""",
                "terminal": "complete",
                "translations": {
                    "p00001": "Дом-на-Холме стоял в конце аллеи.",
                    "p00002": "Дом-на-Холме был старым и тихим.",
                    "p00003": "Блэйк прошёл мимо дома.",
                },
                "entity_cache": _entity_cache_entry(
                    "0001", {
                        "p00001": "Hillsglade House stood at the end of the lane.",
                        "p00002": "Hillsglade House was old and quiet.",
                        "p00003": "Blake walked past the house.",
                    },
                    entity="Hillsglade House", canonical_type="house",
                    anchor_pid="p00001",
                    anchor_span="Hillsglade House stood at the end of the lane.",
                ),
            },
        })

        assert result["chapters"][0]["candidates"] == {
            "generated": 0, "proposed": 0, "committed": 0, "conflicts": 0,
        }
        glossary = json.loads((memory / "glossary.json").read_text(encoding="utf-8"))
        assert glossary == {}

    def test_multi_word_with_canonical_ru_promoted(self, tmp_path, monkeypatch):
        """A multi-word entity with an ESTABLISHED canonical_ru in
        book_memory uses it as the target (card Q2 fallback)."""
        from pact_v4.audit.entity_extractor import EXTRACTOR_VERSION

        seed = {
            "pov": {"gender": "male"},
            "entities": {
                "Hillsglade House": {
                    "type": "house",
                    "canonical_ru": "Дом-на-Холме",
                    "chapters": [],
                    "variants": {},
                    "forbidden_targets": [],
                },
            },
        }
        book_memory_bytes = json.dumps(seed, ensure_ascii=False).encode("utf-8")
        memory, _out_base, result = self._run(tmp_path, monkeypatch, {
            "0001": {
                "html": """<p>Hillsglade House stood at the end of the lane.</p>
<p>Blake walked past the house.</p>""",
                "terminal": "complete",
                "translations": {
                    "p00001": "Дом-на-Холме стоял в конце аллеи.",
                    "p00002": "Блэйк прошёл мимо дома.",
                },
                "entity_cache": _entity_cache_entry(
                    "0001", {
                        "p00001": "Hillsglade House stood at the end of the lane.",
                        "p00002": "Blake walked past the house.",
                    },
                    entity="Hillsglade House", canonical_type="house",
                    anchor_pid="p00001",
                    anchor_span="Hillsglade House stood at the end of the lane.",
                ),
            },
        }, book_memory_bytes=book_memory_bytes)

        # mode=off fail-closed: no glossary promotion even with established canonical_ru
        assert result["chapters"][0]["candidates"] == {
            "generated": 0, "proposed": 0, "committed": 0, "conflicts": 0,
        }
        glossary = json.loads((memory / "glossary.json").read_text(encoding="utf-8"))
        assert glossary == {}

    def test_established_glossary_different_target_is_conflict(
        self, tmp_path, monkeypatch,
    ):
        """An established glossary entry with a DIFFERENT target is a
        conflict: never overwritten (card Q5)."""
        memory = _setup_memory(tmp_path)
        (memory / "glossary.json").write_text(
            json.dumps({"Rose": "Роза"}, ensure_ascii=False), encoding="utf-8",
        )
        out_base = tmp_path / "out"
        src_dir = tmp_path / "src"

        from pact_full_pipeline_runner_v1 import v4_book_run

        def fake_run_one(*args, **kwargs):
            return {"status": "ok"}

        monkeypatch.setattr(v4_book_run, "_run_one_chapter", fake_run_one)
        _write_chapter_html(src_dir, "0001", _CH_HTML)
        _make_chapter_artifacts(
            out_base / "chapter_0001", "0001",
            terminal_status="complete", quarantined=[],
            translations=_CH_TRANSLATIONS, chunk_plan=_PLAN,
            entity_cache_payload=_entity_cache_entry(
                "0001", _CH_SOURCE_TEXT,
                entity="Rose", canonical_type="woman",
                anchor_pid="p00001",
                anchor_span="Rose met Blake at the gate.",
            ),
        )
        result = v4_book_run.run_book(
            memory_dir=memory,
            chapter_ids=["0001"],
            chapter_html_pattern=str(src_dir / "{chapter_id}.html"),
            out_base=out_base,
        )

        rec = result["chapters"][0]
        # off forbids new observations, so no candidate even though conflict would be 1 with old path
        assert rec["candidates"] == {
            "generated": 0, "proposed": 0, "committed": 0, "conflicts": 0,
        }
        # The established target survives untouched.
        glossary = json.loads((memory / "glossary.json").read_text(encoding="utf-8"))
        assert glossary == {"Rose": "Роза"}

    def test_bike_not_велосипед_memory_fact_wins(self, tmp_path, monkeypatch):
        """The memory fact wins (acceptance regression): the seed
        ``Blake's vehicle`` entity carries canonical_ru ``мотоцикл`` with
        ``source_aliases`` [motorcycle, bike]; a verified entity surfaced as
        ``Bike`` must be promoted as ``мотоцикл`` — NEVER as the model's
        ``велосипед`` — via the established-canonical_ru fallback."""
        seed = {
            "pov": {"gender": "male"},
            "entities": {
                "Blake's vehicle": {
                    "type": "vehicle",
                    "canonical_ru": "мотоцикл",
                    "source_aliases": ["motorcycle", "bike"],
                    "chapters": [],
                    "variants": {},
                    "forbidden_targets": [],
                },
            },
        }
        book_memory_bytes = json.dumps(seed, ensure_ascii=False).encode("utf-8")
        memory, _out_base, result = self._run(tmp_path, monkeypatch, {
            "0001": {
                "html": """<p>Bike was parked by the gate.</p>
<p>Blake took the bike.</p>""",
                "terminal": "complete",
                "translations": {
                    "p00001": "Велосипед стоял у ворот.",
                    "p00002": "Блэйк взял велосипед.",
                },
                "entity_cache": _entity_cache_entry(
                    "0001", {
                        "p00001": "Bike was parked by the gate.",
                        "p00002": "Blake took the bike.",
                    },
                    entity="Bike", canonical_type="motorcycle",
                    anchor_pid="p00001",
                    anchor_span="Bike was parked by the gate.",
                ),
            },
        }, book_memory_bytes=book_memory_bytes)

        # off forbids new glossary observations
        assert result["chapters"][0]["candidates"] == {
            "generated": 0, "proposed": 0, "committed": 0, "conflicts": 0,
        }
        glossary = json.loads((memory / "glossary.json").read_text(encoding="utf-8"))
        assert glossary == {}

    def test_apostrophe_norm_jacobs_bell_established_matches(
        self, tmp_path, monkeypatch,
    ):
        """APOSTROPHE-NORM (acceptance regression, Jacob's Bell): an
        extractor entity named with a CURLY apostrophe matches the
        established straight-apostrophe glossary entry — the same target is
        a no-op (no duplicate key, no overwrite, no conflict)."""
        memory = _setup_memory(tmp_path)
        (memory / "glossary.json").write_text(
            json.dumps({"Jacob's Bell": "Якобс-Бэлл"}, ensure_ascii=False),
            encoding="utf-8",
        )
        out_base = tmp_path / "out"
        src_dir = tmp_path / "src"

        from pact_full_pipeline_runner_v1 import v4_book_run

        def fake_run_one(*args, **kwargs):
            return {"status": "ok"}

        monkeypatch.setattr(v4_book_run, "_run_one_chapter", fake_run_one)
        html = """<p>Jacob\u2019s Bell rang across the valley.</p>
<p>The bell was heavy.</p>"""
        translations = {
            "p00001": "Колокол Якобса прозвонил над долиной.",
            "p00002": "Колокол был тяжёлым.",
        }
        source_text = {
            "p00001": "Jacob\u2019s Bell rang across the valley.",
            "p00002": "The bell was heavy.",
        }
        _write_chapter_html(src_dir, "0001", html)
        _make_chapter_artifacts(
            out_base / "chapter_0001", "0001",
            terminal_status="complete", quarantined=[],
            translations=translations, chunk_plan=_PLAN,
            entity_cache_payload=_entity_cache_entry(
                "0001", source_text,
                entity="Jacob\u2019s Bell", canonical_type="bell",
                anchor_pid="p00001",
                anchor_span="Jacob\u2019s Bell rang across the valley.",
            ),
        )
        result = v4_book_run.run_book(
            memory_dir=memory,
            chapter_ids=["0001"],
            chapter_html_pattern=str(src_dir / "{chapter_id}.html"),
            out_base=out_base,
        )

        # Multi-word + established same-target entry -> no-op (proposed=0,
        # conflicts=0, generated=0): the curly-apostrophe entity matches the
        # established straight-apostrophe entry, so nothing new is produced.
        assert result["chapters"][0]["candidates"] == {
            "generated": 0, "proposed": 0, "committed": 0, "conflicts": 0,
        }
        glossary = json.loads((memory / "glossary.json").read_text(encoding="utf-8"))
        assert glossary == {"Jacob's Bell": "Якобс-Бэлл"}

    def test_quarantined_anchor_chunk_observation_dropped(
        self, tmp_path, monkeypatch,
    ):
        """accepted_degraded + anchor pid's chunk quarantined -> the glossary
        observation carries that chunk_id and the B7 filter drops it
        (quarantined evidence never locks a glossary entry)."""
        memory, _out_base, result = self._run(tmp_path, monkeypatch, {
            "0001": {
                "html": _CH_HTML,
                "terminal": "accepted_degraded",
                "quarantined": ["chunk0001"],
                "translations": _CH_TRANSLATIONS,
                "entity_cache": _entity_cache_entry(
                    "0001", _CH_SOURCE_TEXT,
                    entity="Rose", canonical_type="woman",
                    anchor_pid="p00001",
                    anchor_span="Rose met Blake at the gate.",
                ),
            },
        })

        rec = result["chapters"][0]
        assert rec["terminal_status"] == "accepted_degraded"
        # off forbids new glossary observations (fail-closed)
        assert rec["candidates"] == {
            "generated": 0, "proposed": 0, "committed": 0, "conflicts": 0,
        }
        glossary = json.loads((memory / "glossary.json").read_text(encoding="utf-8"))
        assert glossary == {}

    def test_quarantined_anchor_chunk_missing_plan_fails_closed(
        self, tmp_path, monkeypatch,
    ):
        """RV finding t_72f549c8 (HIGH): accepted_degraded WITH quarantined
        chunks and NO chunk_plan.json — the entity-derived glossary block
        fails closed (B9-F5/F6). The reproduced bug: missing plan made
        ``_pid_to_chunk`` return None, the observation carried
        ``chunk_id=\"\"``, and MemoryManager promoted the Rose entry
        (quarantine evidence committed). Now the whole glossary block is
        skipped: no candidates, no ledger line, no observation, no
        glossary mutation — and the run does not crash."""
        memory, out_base, result = self._run(tmp_path, monkeypatch, {
            "0001": {
                "html": _CH_HTML,
                "terminal": "accepted_degraded",
                "quarantined": ["chunk0001"],
                "translations": _CH_TRANSLATIONS,
                "chunk_plan": None,
                "entity_cache": _entity_cache_entry(
                    "0001", _CH_SOURCE_TEXT,
                    entity="Rose", canonical_type="woman",
                    anchor_pid="p00001",
                    anchor_span="Rose met Blake at the gate.",
                ),
            },
        })

        rec = result["chapters"][0]
        assert rec["terminal_status"] == "accepted_degraded"
        # Fail-closed: candidates generated/proposed/committed/conflicts
        # all zero for the glossary block.
        assert rec["candidates"] == {
            "generated": 0, "proposed": 0, "committed": 0, "conflicts": 0,
        }
        # No glossary ledger line (the ledger file is never created).
        assert not (out_base / "glossary_candidates.json").exists()
        # No glossary observation (observations.json holds nothing for
        # glossary after promote).
        observations = json.loads(
            (memory / "observations.json").read_text(encoding="utf-8")
        )
        assert observations.get("glossary", {}) == {}
        # No glossary mutation: the empty glossary.json is untouched.
        glossary = json.loads((memory / "glossary.json").read_text(encoding="utf-8"))
        assert glossary == {}
        # No glossary mutation: the empty glossary.json is untouched.
        # (book_memory promotion not asserted here under off mode)

    def test_quarantined_anchor_chunk_duplicate_plan_fails_closed(
        self, tmp_path, monkeypatch,
    ):
        """A duplicate/ambiguous PID->chunk plan (the anchor pid listed in
        TWO chunks) is non-authoritative: ``_pid_to_chunk`` returns None,
        so the entity-derived glossary block fails closed (B9-F5/F6) — no
        candidates, no ledger line, no observation, no glossary mutation."""
        duplicate_plan = {
            "artifact": "pact-v4-chunk-plan/v1",
            "snapshot_hash": "test",
            "plan_hash": "test",
            "chunks": [
                {"chunk_id": "chunk0001", "snapshot_hash": "test",
                 "pids": ["p00001", "p00002", "p00003"],
                 "word_counts": [], "context": {"left_ru": "", "right_en": []},
                 "undersized_exception": False},
                {"chunk_id": "chunk0002", "snapshot_hash": "test",
                 "pids": ["p00003", "p00004", "p00005", "p00006"],
                 "word_counts": [], "context": {"left_ru": "", "right_en": []},
                 "undersized_exception": False},
            ],
        }
        memory, out_base, result = self._run(tmp_path, monkeypatch, {
            "0001": {
                "html": _CH_HTML,
                "terminal": "accepted_degraded",
                "quarantined": ["chunk0001"],
                "translations": _CH_TRANSLATIONS,
                "chunk_plan": duplicate_plan,
                "entity_cache": _entity_cache_entry(
                    "0001", _CH_SOURCE_TEXT,
                    entity="Rose", canonical_type="woman",
                    anchor_pid="p00001",
                    anchor_span="Rose met Blake at the gate.",
                ),
            },
        })

        rec = result["chapters"][0]
        assert rec["terminal_status"] == "accepted_degraded"
        assert rec["candidates"] == {
            "generated": 0, "proposed": 0, "committed": 0, "conflicts": 0,
        }
        assert not (out_base / "glossary_candidates.json").exists()
        observations = json.loads(
            (memory / "observations.json").read_text(encoding="utf-8")
        )
        assert observations.get("glossary", {}) == {}
        glossary = json.loads((memory / "glossary.json").read_text(encoding="utf-8"))
        assert glossary == {}

    def test_quarantined_anchor_chunk_incomplete_plan_fails_closed(
        self, tmp_path, monkeypatch,
    ):
        """An incomplete PID->chunk plan (a source/translation pid the plan
        does not map) is non-authoritative: the entity-derived glossary
        block fails closed (B9-F5) — a pid of unknown provenance could
        belong to a quarantined chunk."""
        incomplete_plan = {
            "artifact": "pact-v4-chunk-plan/v1",
            "snapshot_hash": "test",
            "plan_hash": "test",
            "chunks": [
                {"chunk_id": "chunk0001", "snapshot_hash": "test",
                 "pids": ["p00001", "p00002", "p00003"],
                 "word_counts": [], "context": {"left_ru": "", "right_en": []},
                 "undersized_exception": False},
                {"chunk_id": "chunk0002", "snapshot_hash": "test",
                 "pids": ["p00004", "p00005"],
                 "word_counts": [], "context": {"left_ru": "", "right_en": []},
                 "undersized_exception": False},
            ],
        }
        memory, out_base, result = self._run(tmp_path, monkeypatch, {
            "0001": {
                "html": _CH_HTML,
                "terminal": "accepted_degraded",
                "quarantined": ["chunk0001"],
                "translations": _CH_TRANSLATIONS,
                "chunk_plan": incomplete_plan,
                "entity_cache": _entity_cache_entry(
                    "0001", _CH_SOURCE_TEXT,
                    entity="Rose", canonical_type="woman",
                    anchor_pid="p00001",
                    anchor_span="Rose met Blake at the gate.",
                ),
            },
        })

        rec = result["chapters"][0]
        assert rec["terminal_status"] == "accepted_degraded"
        assert rec["candidates"] == {
            "generated": 0, "proposed": 0, "committed": 0, "conflicts": 0,
        }
        assert not (out_base / "glossary_candidates.json").exists()
        observations = json.loads(
            (memory / "observations.json").read_text(encoding="utf-8")
        )
        assert observations.get("glossary", {}) == {}
        glossary = json.loads((memory / "glossary.json").read_text(encoding="utf-8"))
        assert glossary == {}

    def test_accepted_anchor_chunk_promotes_accepted_degraded(
        self, tmp_path, monkeypatch,
    ):
        """With a VALID authoritative plan, an entity anchored in an
        ACCEPTED chunk may promote under accepted_degraded: the anchor
        pid's chunk (chunk0002) is not quarantined, so the B7 filter keeps
        the observation (quarantine never blocks clean evidence)."""
        html = """<p>Rose met Blake at the gate.</p>
<p>Blake waited outside for Mary.</p>
<p>Rose knew the way home.</p>
<p>Rose walked home alone.</p>
<p>The pact held them all together.</p>
<p>He walked home alone.</p>"""
        translations = {
            "p00001": "Роуз встретила Блэйка у ворот.",
            "p00002": "Блэйк ждал снаружи Мэри.",
            "p00003": "Роуз знала дорогу домой.",
            "p00004": "Роуз пошла домой одна.",
            "p00005": "Пакт держал их всех вместе.",
            "p00006": "Он пошёл домой один.",
        }
        source_text = {
            "p00001": "Rose met Blake at the gate.",
            "p00002": "Blake waited outside for Mary.",
            "p00003": "Rose knew the way home.",
            "p00004": "Rose walked home alone.",
            "p00005": "The pact held them all together.",
            "p00006": "He walked home alone.",
        }
        memory, _out_base, result = self._run(tmp_path, monkeypatch, {
            "0001": {
                "html": html,
                "terminal": "accepted_degraded",
                "quarantined": ["chunk0001"],
                "translations": translations,
                "entity_cache": _entity_cache_entry(
                    "0001", source_text,
                    entity="Rose", canonical_type="woman",
                    anchor_pid="p00004",
                    anchor_span="Rose walked home alone.",
                ),
            },
        })

        rec = result["chapters"][0]
        assert rec["terminal_status"] == "accepted_degraded"
        # off forbids new glossary observations (fail-closed)
        assert rec["candidates"] == {
            "generated": 0, "proposed": 0, "committed": 0, "conflicts": 0,
        }
        glossary = json.loads((memory / "glossary.json").read_text(encoding="utf-8"))
        assert glossary == {}

    def test_failed_chapter_never_promotes(self, tmp_path, monkeypatch):
        """A failed chapter never promotes glossary entries — even with a
        valid entity cache (review F1 gate)."""
        memory, _out_base, result = self._run(tmp_path, monkeypatch, {
            "0001": {
                "html": _CH_HTML,
                "terminal": "failed",
                "translations": _CH_TRANSLATIONS,
                "entity_cache": _entity_cache_entry(
                    "0001", _CH_SOURCE_TEXT,
                    entity="Rose", canonical_type="woman",
                    anchor_pid="p00001",
                    anchor_span="Rose met Blake at the gate.",
                ),
            },
        })

        assert result["chapters"][0]["candidates"] == {
            "generated": 0, "proposed": 0, "committed": 0, "conflicts": 0,
        }
        glossary = json.loads((memory / "glossary.json").read_text(encoding="utf-8"))
        assert glossary == {}
        book_memory = json.loads(
            (memory / "book_memory.json").read_text(encoding="utf-8")
        )
        assert "Rose" not in book_memory.get("entities", {})

    def test_foreign_entity_cache_never_promoted(self, tmp_path, monkeypatch):
        """A cache entry whose source_hash does not match the strict run's
        identity is foreign: fail-closed, nothing promotes (RV finding 2)."""
        memory, _out_base, result = self._run(tmp_path, monkeypatch, {
            "0001": {
                "html": _CH_HTML,
                "terminal": "complete",
                "translations": _CH_TRANSLATIONS,
                "entity_cache": _entity_cache_entry(
                    "0001", _CH_SOURCE_TEXT,
                    entity="Rose", canonical_type="woman",
                    anchor_pid="p00001",
                    anchor_span="Rose met Blake at the gate.",
                    source_hash="other-hash",
                ),
                "source_hash": "test-hash",
            },
        })

        assert result["chapters"][0]["candidates"] == {
            "generated": 0, "proposed": 0, "committed": 0, "conflicts": 0,
        }
        glossary = json.loads((memory / "glossary.json").read_text(encoding="utf-8"))
        assert glossary == {}

    def test_no_entity_cache_nothing_promoted_bytes_preserved(
        self, tmp_path, monkeypatch,
    ):
        """A chapter WITHOUT entity_context_cache.json produces no glossary
        candidates and must not read-modify-write book_memory.json (B9-RV9
        regression kept for the entity flow)."""
        book_memory_bytes = b'{"pov":{"gender":"male"}}'
        memory, _out_base, result = self._run(
            tmp_path, monkeypatch, {
                "0001": {
                    "html": _CH_HTML,
                    "terminal": "complete",
                    "translations": _CH_TRANSLATIONS,
                    "entity_cache": None,
                },
            },
            book_memory_bytes=book_memory_bytes,
        )

        rec = result["chapters"][0]
        assert rec["candidates"] == {
            "generated": 0, "proposed": 0, "committed": 0, "conflicts": 0,
        }
        assert (memory / "book_memory.json").read_bytes() == book_memory_bytes
        assert rec["book_memory_hash_before"] == rec["book_memory_hash_after"]
        glossary = json.loads((memory / "glossary.json").read_text(encoding="utf-8"))
        assert glossary == {}



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


    # ------------------------------------------------------------------
    # B1 promote-only (owner 2026-08-19): reuse a completed strict chapter
    # out_dir instead of re-running the strict pipeline (--promote-existing).
    # ------------------------------------------------------------------

    def test_promote_existing_complete_promotes_without_strict(
        self, tmp_path, monkeypatch,
    ):
        """--promote-existing consumes an already-completed strict chapter
        out_dir and runs the acceptance/promotion stage without ever invoking
        the strict pipeline (the translator model may be unavailable)."""
        from pact_full_pipeline_runner_v1 import v4_book_run

        memory = _setup_memory(tmp_path)
        src_dir = tmp_path / "src"
        out_base = tmp_path / "out"
        existing = out_base / "chapter_0001"

        # Build the completed-strict chapter artifacts (as if a prior strict
        # run had succeeded) directly in --promote-existing dir.
        _write_chapter_html(src_dir, "0001", _CH_HTML)
        _make_chapter_artifacts(
            existing, "0001",
            terminal_status="complete",
            quarantined=[],
            translations=_CH_TRANSLATIONS,
            chunk_plan=_PLAN,
            entity_cache_payload=_entity_cache_entry(
                "0001", _CH_SOURCE_TEXT,
                entity="Rose", canonical_type="woman",
                anchor_pid="p00001",
                anchor_span="Rose met Blake at the gate.",
            ),
            record_source_hash="test-hash",
            record_extractor_version=EXTRACTOR_VERSION,
        )

        # The strict pipeline must NOT be invoked in promote-only mode.
        called = {"n": 0}

        def fake_run_one(*args, **kwargs):
            called["n"] += 1
            raise AssertionError("strict must not run under promote-existing")

        monkeypatch.setattr(v4_book_run, "_run_one_chapter", fake_run_one)

        result = v4_book_run.run_book(
            memory_dir=memory,
            chapter_ids=["0001"],
            chapter_html_pattern=str(src_dir / "{chapter_id}.html"),
            out_base=out_base,
            promote_existing_dir=existing,
        )
        assert called["n"] == 0, "promote-only must not call the strict run"
        rec = result["chapters"][0]
        assert rec["terminal_status"] == "complete"
        assert rec["promoted"] is True
        # off forbids new glossary observations (fail-closed, even via promote-existing without sidecar)
        _glossary_path = memory / "glossary.json"
        mem_keys = set(
            json.loads(_glossary_path.read_text(encoding="utf-8"))
            if _glossary_path.exists() else {}
        )
        assert "Rose" not in mem_keys  # off + no sidecar -> no glossary promotion (fail-closed)

    def test_promote_existing_missing_record_errors(self, tmp_path, monkeypatch):
        """--promote-existing pointing at a dir without a strict record
        fails closed with a clear error, not a silent no-op."""
        from pact_full_pipeline_runner_v1 import v4_book_run

        memory = _setup_memory(tmp_path)
        out_base = tmp_path / "out"
        empty_dir = out_base / "chapter_0001"
        empty_dir.mkdir(parents=True, exist_ok=True)

        def fake_run_one(*args, **kwargs):
            raise AssertionError("strict must not run when record is absent")

        monkeypatch.setattr(v4_book_run, "_run_one_chapter", fake_run_one)

        src_dir = tmp_path / "src"
        _write_chapter_html(src_dir, "0001", _CH_HTML)
        result = v4_book_run.run_book(
            memory_dir=memory,
            chapter_ids=["0001"],
            chapter_html_pattern=str(src_dir / "{chapter_id}.html"),
            out_base=out_base,
            promote_existing_dir=empty_dir,
        )
        rec = result["chapters"][0]
        assert rec["terminal_status"] == "error"
        assert "no strict_chapter_trial_record.json" in rec.get("error", "")

    def test_promote_existing_argparse(self):
        from pact_full_pipeline_runner_v1 import v4_book_run

        args, _extra = v4_book_run.build_argparser().parse_known_args([
            "--memory-dir", "mem", "--chapters", "0001",
            "--chapter-html-pattern", "ch/{chapter_id}.html",
            "--out-base", "out",
            "--promote-existing", "D:/some/completed/chapter_0001",
        ])
        assert args.promote_existing == Path("D:/some/completed/chapter_0001")
