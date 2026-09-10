"""Book resume regressions (owner-approved change ``book-chapter-retry``).

``run_book`` with the strict-chapter seam faked by the real stub driver, so
chapter records/artifacts are genuine while no model is ever called:

  * ``book --resume`` skips ready chapters (strict stages not run, promotion
    only) and reruns failed chapters from their own failed stage;
  * retrying chapter 2 after chapters 3-4 completed keeps 3-4 and their
    observations intact and only adds chapter 2's state (additive memory);
  * repeated promotion is idempotent (ledgers/memory/index gain no dupes);
  * ``--force-rerun-chapter`` reruns even a ready chapter;
  * ``--resume`` without an existing ``--out-base`` fails; changed
    chapter-run flags or a changed chapter source fail closed;
  * ``--retry-incomplete`` is never forwarded book-wide;
  * without ``--resume`` every chapter runs unconditionally (unchanged).
"""
from __future__ import annotations

import dataclasses
import json
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest

import pact_full_pipeline_runner_v1.v4_book_run as book_run_mod
from pact_full_pipeline_runner_v1.v4_book_run import (
    _book_resume_identity,
    run_book,
)
from pact_v4.pipeline.v4_phase12_strict_runner import (
    StrictRunConfig,
    run_chapter_strict,
)
from tests.pact_v4.pipeline.test_v4_phase12_strict_runner import (
    StubGemma,
    StubGemmaAudit,
    StubModelCaller,
    StubQwen,
    StubQwenAudit,
    StubRegionGate,
    _LifecycleAwareGemmaAudit,
    _LifecycleAwareGemmaSelector,
    _LifecycleAwareModelCaller,
    _LifecycleAwareQwen,
    _LifecycleAwareQwenAudit,
    _make_backend,
    _make_router,
)
from tests.pact_v4.pipeline.test_book_chapter_retry import _StubRepairCaller
from tests.pact_v4.pipeline.test_v4_phase12_strict_runner_repair import (
    _flagging_audit,
)


def _write_html(path: Path, n_paragraphs: int = 8) -> None:
    paragraph = " ".join(f"word{i}" for i in range(35))
    body = "\n".join(f"<p>{paragraph}</p>" for _ in range(n_paragraphs))
    path.write_text("<html><body>" + body + "</body></html>", encoding="utf-8")


def _write_memory(memory_dir: Path) -> None:
    memory_dir.mkdir(parents=True, exist_ok=True)
    (memory_dir / "glossary.json").write_text("{}", encoding="utf-8")
    (memory_dir / "book_memory.json").write_text("{}", encoding="utf-8")


class _BookFixture:
    """Three-chapter book layout with a recording fake strict driver."""

    def __init__(self, tmp_path: Path) -> None:
        self.root = tmp_path
        self.memory_dir = tmp_path / "memory"
        self.out_base = tmp_path / "book"
        _write_memory(self.memory_dir)
        for chapter_id in ("0001", "0002", "0003"):
            _write_html(tmp_path / f"{chapter_id}.html")
        self.calls: List[Dict[str, Any]] = []

    @property
    def pattern(self) -> str:
        return str(self.root / "{chapter_id}.html")

    def fake_run_one_chapter(
        self, chapter_id: str, *, memory_dir: Path,
        chapter_html_path: Path, out_dir: Path,
        extra_args=(),
    ) -> Dict[str, Any]:
        from tests.pact_v4.pipeline.test_book_chapter_retry import (
            _FakeB3,
            _run_wc_chapter,
        )
        extra = list(extra_args)
        assert "--retry-incomplete" not in extra, \
            "book must never forward --retry-incomplete book-wide"
        if "--whole-chapter" in extra:
            # Whole-chapter chapters rerun through the real WC driver
            # with a zero-model-call fake B3 (mirrors production, where
            # the book always forwards --whole-chapter for WC chapters).
            from pact_v4.pipeline.v4_phase12_strict_runner import (
                StrictRunConfig as _StrictRunConfig,
            )
            from tests.pact_v4.pipeline.test_v4_phase12_strict_runner import (
                _make_backend as _make_stub_backend,
            )
            wc_cfg = _StrictRunConfig(
                chapter_id=chapter_id,
                chapter_html_path=Path(chapter_html_path),
                memory_dir=Path(memory_dir), out_dir=Path(out_dir),
                backend=_make_stub_backend(), whole_chapter=True,
                resume="--resume" in extra,
                force_rerun="--force-rerun" in extra,
            )
            wc_result, _, _ = _run_wc_chapter(wc_cfg, b3=_FakeB3())
            self.calls.append({
                "chapter_id": chapter_id,
                "extra_args": extra,
                "terminal": wc_result.step8.get("status"),
            })
            return {"status": "ok"}
        gen_caller = StubModelCaller()
        repair_caller = _StubRepairCaller()
        # Flagged audit finding on p00001 forces the repair stage to execute
        # (repair caller invoked) on every full run, so resume behavior is
        # observable at the repair stage instead of cache-skipped.
        audit_inner = _flagging_audit("p00001")
        router = _make_router()
        cfg = StrictRunConfig(
            chapter_id=chapter_id, chapter_html_path=Path(chapter_html_path),
            memory_dir=Path(memory_dir), out_dir=Path(out_dir),
            backend=_make_backend(),
            resume="--resume" in extra,
            force_rerun="--force-rerun" in extra,
        )
        result = run_chapter_strict(
            cfg, router=router,
            model_caller=_LifecycleAwareModelCaller(router, gen_caller),
            qwen_evaluator=_LifecycleAwareQwen(router, StubQwen()),
            gemma_selector=_LifecycleAwareGemmaSelector(router, StubGemma()),
            qwen_audit_evaluator=_LifecycleAwareQwenAudit(router, audit_inner),
            gemma_audit_evaluator=_LifecycleAwareGemmaAudit(router, StubGemmaAudit()),
            repair_adapters=(
                repair_caller,
                StubRegionGate(passed=True, reason="regate"),
                StubQwenAudit(),
                StubGemmaAudit(),
            ),
        )
        self.calls.append({
            "chapter_id": chapter_id,
            "extra_args": extra,
            "gen_calls": len(gen_caller.calls),
            "repair_calls": len(repair_caller.calls),
            "terminal": result.step8.get("status"),
        })
        return {"status": "ok"}


def _stub_resolve_invocation(argv) -> Dict[str, Any]:
    """Stub-backend invocation identity mirroring the fake strict driver.

    Rebuilds the same ``StrictRunConfig`` the fake uses (pure defaults +
    chapter paths from argv) so the resolved identity matches fake-written
    records. ``--whole-chapter`` IS honored (whole-chapter mode is part of
    the config identity, and the book forwards it for whole-chapter
    chapters); other identity-bearing flags are NOT honored -- the fake
    driver ignores them too, so any such flag would (correctly) fail the
    gate; the book flag-identity gate covers flag changes separately.
    """
    items = list(argv)

    def _val(flag: str) -> Optional[str]:
        for index, token in enumerate(items):
            text = str(token)
            if text == flag and index + 1 < len(items):
                return str(items[index + 1])
            if text.startswith(flag + "="):
                return text.split("=", 1)[1]
        return None

    cfg = StrictRunConfig(
        chapter_id=_val("--chapter-id") or "",
        chapter_html_path=Path(_val("--chapter-html") or "."),
        memory_dir=Path(_val("--memory-dir") or "."),
        out_dir=Path(_val("--out-dir") or "."),
        backend=_make_backend(),
        whole_chapter=any(
            str(token) in ("--whole-chapter", "--whole_chapter")
            or str(token).startswith(("--whole-chapter=", "--whole_chapter="))
            for token in items),
    )
    artifact = cfg.to_config_artifact(
        model_profile=cfg.backend.config_profile_name())
    return {
        "config_identity": artifact.config_identity,
        "backend_identity_hashes": list(cfg.backend.acceptable_identity_hashes()),
    }


@pytest.fixture()
def book(monkeypatch, tmp_path):
    fixture = _BookFixture(tmp_path)
    monkeypatch.setattr(
        book_run_mod, "_run_one_chapter", fixture.fake_run_one_chapter)
    monkeypatch.setattr(
        book_run_mod, "_resolve_chapter_invocation", _stub_resolve_invocation)
    return fixture


def _chapter_ids() -> List[str]:
    return ["0001", "0002", "0003"]


def _snapshot_chapter(out_dir: Path) -> Dict[str, bytes]:
    return {
        name: (out_dir / name).read_bytes()
        for name in ("strict_chapter_trial_record.json", "translations.json")
        if (out_dir / name).exists()
    }


def test_ready_chapters_skip_strict_stages(book: _BookFixture):
    first = run_book(
        memory_dir=book.memory_dir, chapter_ids=_chapter_ids(),
        chapter_html_pattern=book.pattern, out_base=book.out_base, extra_args=[],
    )
    assert [c["terminal_status"] for c in first["chapters"]] == ["complete"] * 3
    assert len(book.calls) == 3
    snapshots = {
        chapter_id: _snapshot_chapter(book.out_base / f"chapter_{chapter_id}")
        for chapter_id in _chapter_ids()
    }
    book.calls.clear()
    second = run_book(
        memory_dir=book.memory_dir, chapter_ids=_chapter_ids(),
        chapter_html_pattern=book.pattern, out_base=book.out_base, extra_args=[],
        resume=True,
    )
    # Ready chapters: strict stages not run (promotion only), artifacts intact.
    assert book.calls == []
    assert [c["terminal_status"] for c in second["chapters"]] == ["complete"] * 3
    for chapter_id in _chapter_ids():
        assert _snapshot_chapter(book.out_base / f"chapter_{chapter_id}") == snapshots[chapter_id]
    assert (book.out_base / "book_run.json").exists()


def test_failed_chapter_resumes_own_stage_downstream_intact(book: _BookFixture):
    run_book(
        memory_dir=book.memory_dir, chapter_ids=_chapter_ids(),
        chapter_html_pattern=book.pattern, out_base=book.out_base, extra_args=[],
    )
    ch2 = book.out_base / "chapter_0002"
    # Simulate a repair failure after generation/audit completed.
    (ch2 / "repair_cache.json").unlink()
    (ch2 / "repair_report.json").unlink()
    record_path = ch2 / "strict_chapter_trial_record.json"
    record = json.loads(record_path.read_text(encoding="utf-8"))
    # A repair failure is never released as audited: both the repair step
    # and the terminal step record the failure.
    record["step7"] = {"status": "failed", "error": "simulated repair failure"}
    record["step8"] = {"status": "failed", "error": "simulated repair failure"}
    record_path.write_text(json.dumps(record, ensure_ascii=False, indent=2),
                           encoding="utf-8")
    # Downstream chapters already added shared-memory observations.
    glossary_path = book.memory_dir / "glossary.json"
    glossary = json.loads(glossary_path.read_text(encoding="utf-8"))
    glossary["DownstreamTerm"] = "НисходящийТермин"
    glossary_path.write_text(json.dumps(glossary, ensure_ascii=False, indent=2),
                             encoding="utf-8")
    downstream = {
        chapter_id: _snapshot_chapter(book.out_base / f"chapter_{chapter_id}")
        for chapter_id in ("0001", "0003")
    }
    book.calls.clear()
    resumed = run_book(
        memory_dir=book.memory_dir, chapter_ids=_chapter_ids(),
        chapter_html_pattern=book.pattern, out_base=book.out_base, extra_args=[],
        resume=True,
    )
    # Only chapter 2 reran, with automatic --resume (never --retry-incomplete).
    assert [call["chapter_id"] for call in book.calls] == ["0002"]
    assert book.calls[0]["extra_args"] == ["--resume"]
    assert book.calls[0]["gen_calls"] == 0  # generation/audit reused
    assert book.calls[0]["repair_calls"] > 0  # repair re-executed
    assert [c["terminal_status"] for c in resumed["chapters"]] == ["complete"] * 3
    # Ready chapters byte-intact; downstream observations never rolled back.
    for chapter_id in ("0001", "0003"):
        assert _snapshot_chapter(book.out_base / f"chapter_{chapter_id}") == downstream[chapter_id]
    assert json.loads(glossary_path.read_text(encoding="utf-8"))["DownstreamTerm"] == \
        "НисходящийТермин"


def test_repeated_resume_promotion_idempotent(book: _BookFixture):
    run_book(
        memory_dir=book.memory_dir, chapter_ids=_chapter_ids(),
        chapter_html_pattern=book.pattern, out_base=book.out_base, extra_args=[],
    )
    run_book(
        memory_dir=book.memory_dir, chapter_ids=_chapter_ids(),
        chapter_html_pattern=book.pattern, out_base=book.out_base, extra_args=[],
        resume=True,
    )
    ledgers = {}
    for name in ("glossary_candidates.json", "book_memory_candidates.json"):
        path = book.out_base / name
        ledgers[name] = path.read_bytes() if path.exists() else b""
    memory_files = {}
    for name in ("glossary.json", "book_memory.json", "chapter_index.json"):
        path = book.memory_dir / name
        memory_files[name] = path.read_bytes() if path.exists() else b""
    run_book(
        memory_dir=book.memory_dir, chapter_ids=_chapter_ids(),
        chapter_html_pattern=book.pattern, out_base=book.out_base, extra_args=[],
        resume=True,
    )
    for name, before in ledgers.items():
        path = book.out_base / name
        after = path.read_bytes() if path.exists() else b""
        assert after == before, f"{name} changed on repeated resume"
    for name, before in memory_files.items():
        path = book.memory_dir / name
        after = path.read_bytes() if path.exists() else b""
        assert after == before, f"{name} changed on repeated resume"


def test_force_rerun_chapter_wins_over_skip(book: _BookFixture):
    run_book(
        memory_dir=book.memory_dir, chapter_ids=_chapter_ids(),
        chapter_html_pattern=book.pattern, out_base=book.out_base, extra_args=[],
    )
    book.calls.clear()
    run_book(
        memory_dir=book.memory_dir, chapter_ids=_chapter_ids(),
        chapter_html_pattern=book.pattern, out_base=book.out_base, extra_args=[],
        resume=True, force_rerun_chapters=["1"],
    )
    assert [call["chapter_id"] for call in book.calls] == ["0001"]
    assert "--force-rerun" in book.calls[0]["extra_args"]


def test_resume_requires_existing_out_base(book: _BookFixture):
    with pytest.raises(ValueError, match="existing --out-base"):
        run_book(
            memory_dir=book.memory_dir, chapter_ids=_chapter_ids(),
            chapter_html_pattern=book.pattern,
            out_base=book.root / "no-such-book", extra_args=[], resume=True,
        )


def test_changed_flags_fail_closed(book: _BookFixture):
    run_book(
        memory_dir=book.memory_dir, chapter_ids=_chapter_ids(),
        chapter_html_pattern=book.pattern, out_base=book.out_base,
        extra_args=["--reasoning", "1"],
    )
    with pytest.raises(ValueError, match="flags changed"):
        run_book(
            memory_dir=book.memory_dir, chapter_ids=_chapter_ids(),
            chapter_html_pattern=book.pattern, out_base=book.out_base,
            extra_args=["--reasoning", "2"], resume=True,
        )


def test_changed_source_fails_closed(book: _BookFixture):
    run_book(
        memory_dir=book.memory_dir, chapter_ids=_chapter_ids(),
        chapter_html_pattern=book.pattern, out_base=book.out_base, extra_args=[],
    )
    with open(book.root / "0001.html", "a", encoding="utf-8") as handle:
        handle.write("<p>Changed source invalidates ready artifacts.</p>")
    with pytest.raises(ValueError, match="source identity"):
        run_book(
            memory_dir=book.memory_dir, chapter_ids=_chapter_ids(),
            chapter_html_pattern=book.pattern, out_base=book.out_base,
            extra_args=[], resume=True,
        )


def test_without_resume_every_chapter_runs(book: _BookFixture):
    run_book(
        memory_dir=book.memory_dir, chapter_ids=_chapter_ids(),
        chapter_html_pattern=book.pattern, out_base=book.out_base, extra_args=[],
    )
    book.calls.clear()
    run_book(
        memory_dir=book.memory_dir, chapter_ids=_chapter_ids(),
        chapter_html_pattern=book.pattern, out_base=book.out_base, extra_args=[],
    )
    assert [call["chapter_id"] for call in book.calls] == _chapter_ids()
    assert all(call["extra_args"] == [] for call in book.calls)


def test_book_resume_identity_normalization():
    assert _book_resume_identity(
        ["--resume", "--translator", "a/b", "--force-rerun-chapter", "0001"]
    ) == ["--translator", "a/b"]
    assert _book_resume_identity(
        ["--force-rerun-chapter=0002", "--reasoning", "1"]
    ) == ["--reasoning", "1"]
    assert _book_resume_identity(["--retry-incomplete"]) == []


# ---------------------------------------------------------------------------
# HIGH finding: ready skip must compare saved config/backend identity to the
# CURRENT resolved invocation (same CLI flags can resolve differently when
# the runtime/backend config changed: registry edit, model file swap,
# server-profile change). Mismatch hard-fails instead of silently promoting
# foreign artifacts.
# ---------------------------------------------------------------------------


def _write_matching_stub_invocation(monkeypatch, *, backend_hashes=None,
                                    config_identity=None, raises=None):
    """Re-patch the invocation resolver: same-flags, changed resolution."""
    probe = _stub_resolve_invocation(
        ["--chapter-id", "0001", "--chapter-html", "c.html",
         "--memory-dir", "m", "--out-dir", "o"])
    if config_identity is None:
        config_identity = probe["config_identity"]
    if backend_hashes is None:
        backend_hashes = probe["backend_identity_hashes"]

    def _evil(_argv):
        if raises is not None:
            raise raises
        return {"config_identity": config_identity,
                "backend_identity_hashes": list(backend_hashes)}

    monkeypatch.setattr(book_run_mod, "_resolve_chapter_invocation", _evil)


def test_invocation_gate_same_flags_changed_backend_fails(book, monkeypatch):
    run_book(
        memory_dir=book.memory_dir, chapter_ids=["0001"],
        chapter_html_pattern=book.pattern, out_base=book.out_base, extra_args=[],
    )
    # Same CLI flags, but the backend now resolves differently (registry /
    # model / server-profile change with identical argv).
    _write_matching_stub_invocation(
        monkeypatch, backend_hashes=["deadbeef-changed-backend"])
    book.calls.clear()
    with pytest.raises(ValueError, match="config/backend identity"):
        run_book(
            memory_dir=book.memory_dir, chapter_ids=["0001"],
            chapter_html_pattern=book.pattern, out_base=book.out_base,
            extra_args=[], resume=True,
        )
    assert book.calls == []  # no strict run, no silent promotion


def test_invocation_gate_same_flags_changed_config_fails(book, monkeypatch):
    run_book(
        memory_dir=book.memory_dir, chapter_ids=["0001"],
        chapter_html_pattern=book.pattern, out_base=book.out_base, extra_args=[],
    )
    _write_matching_stub_invocation(
        monkeypatch, config_identity="changed-config-identity")
    with pytest.raises(ValueError, match="config/backend identity"):
        run_book(
            memory_dir=book.memory_dir, chapter_ids=["0001"],
            chapter_html_pattern=book.pattern, out_base=book.out_base,
            extra_args=[], resume=True,
        )


def test_invocation_resolution_failure_fails_closed(book, monkeypatch):
    run_book(
        memory_dir=book.memory_dir, chapter_ids=["0001"],
        chapter_html_pattern=book.pattern, out_base=book.out_base, extra_args=[],
    )
    _write_matching_stub_invocation(
        monkeypatch, raises=RuntimeError("providers.yaml unreadable"))
    with pytest.raises(ValueError, match="does not resolve"):
        run_book(
            memory_dir=book.memory_dir, chapter_ids=["0001"],
            chapter_html_pattern=book.pattern, out_base=book.out_base,
            extra_args=[], resume=True,
        )


def test_chapter_invocation_matches_unit(tmp_path):
    from pact_v4.pipeline.v4_retry import chapter_invocation_matches
    out_dir = tmp_path / "chapter_0001"
    out_dir.mkdir()
    record = {
        "step8": {"status": "complete"},
        "step7": {"status": "skipped", "reason": "x"},
        "identities": {
            "snapshot_hash": "snap", "chunk_plan_hash": "plan",
            "config_identity": "cfg-A",
        },
        "backend": {"identity_hash": "be-A"},
    }
    (out_dir / "strict_chapter_trial_record.json").write_text(
        json.dumps(record), encoding="utf-8")
    manifest = {
        "schema": "pact-v4-stage-manifest/v1",
        "stages": {
            "generation": {
                "status": "complete", "attempt": 0,
                "inputs": {"config_identity": "cfg-A",
                           "backend_identity_hash": "be-A"},
                "artifacts": [], "integrity": {},
            }
        },
    }
    (out_dir / "stage_manifest.json").write_text(
        json.dumps(manifest), encoding="utf-8")
    ok, _reason = chapter_invocation_matches(
        out_dir, config_identity="cfg-A", acceptable_backend_hashes=["be-A"])
    assert ok is True
    # Same flags, changed backend resolution.
    ok, reason = chapter_invocation_matches(
        out_dir, config_identity="cfg-A", acceptable_backend_hashes=["be-B"])
    assert ok is False
    assert "backend" in reason
    # Same flags, changed config resolution.
    ok, reason = chapter_invocation_matches(
        out_dir, config_identity="cfg-B", acceptable_backend_hashes=["be-A"])
    assert ok is False
    assert "config_identity" in reason
    # Manifest leg disagrees even when the record matches.
    manifest["stages"]["generation"]["inputs"]["backend_identity_hash"] = "be-X"
    (out_dir / "stage_manifest.json").write_text(
        json.dumps(manifest), encoding="utf-8")
    ok, reason = chapter_invocation_matches(
        out_dir, config_identity="cfg-A", acceptable_backend_hashes=["be-A"])
    assert ok is False
    assert "generation" in reason
    # Missing record never matches.
    (out_dir / "strict_chapter_trial_record.json").unlink()
    ok, _reason = chapter_invocation_matches(
        out_dir, config_identity="cfg-A", acceptable_backend_hashes=["be-A"])
    assert ok is False


def test_real_resolver_same_argv_changed_backend(tmp_path):
    """Grounding: identical chapter argv resolves different identities when
    the runtime config content changes (the HIGH finding's premise)."""
    from pact_full_pipeline_runner_v1.v4_phase12_strict_run import (
        resolve_invocation_identities,
    )
    html = tmp_path / "c.html"
    html.write_text("<html><body><p>Hello world one two three.</p></body></html>",
                    encoding="utf-8")
    memory_dir = tmp_path / "memory"
    memory_dir.mkdir()
    rt_config = tmp_path / "rt.yaml"
    rt_config.write_text(
        "kind: opencode_server\nserver_mode: external\n"
        "base_url: http://127.0.0.1:4097\nmodel_bindings:\n"
        "  generator: opencode/model-a\n  repair: opencode/model-a\n"
        "  qwen_audit: openai/model-b\n",
        encoding="utf-8",
    )
    argv = ["--chapter-id", "0001", "--chapter-html", str(html),
            "--memory-dir", str(memory_dir), "--out-dir", str(tmp_path / "o"),
            "--runtime-config", str(rt_config)]
    before = resolve_invocation_identities(argv)
    rt_config.write_text(
        rt_config.read_text(encoding="utf-8").replace(
            "opencode/model-a", "opencode/model-A-CHANGED"),
        encoding="utf-8",
    )
    after = resolve_invocation_identities(argv)
    assert (before["backend_identity_hashes"] != after["backend_identity_hashes"]
            or before["config_identity"] != after["config_identity"])


# ---------------------------------------------------------------------------
# Round-4 HIGH-1: bad-manifest chapters heal without promoting in the same
# book invocation; promotion waits for a later all-gates-fresh invocation.
# ---------------------------------------------------------------------------


def test_bad_manifest_blocks_promotion_this_invocation(book: _BookFixture):
    run_book(
        memory_dir=book.memory_dir, chapter_ids=_chapter_ids(),
        chapter_html_pattern=book.pattern, out_base=book.out_base, extra_args=[],
    )
    ch2 = book.out_base / "chapter_0002"
    (ch2 / "stage_manifest.json").write_text("{broken", encoding="utf-8")
    book.calls.clear()
    resumed = run_book(
        memory_dir=book.memory_dir, chapter_ids=_chapter_ids(),
        chapter_html_pattern=book.pattern, out_base=book.out_base, extra_args=[],
        resume=True,
    )
    # Chapter 2 reran (safe self-heal) but did NOT promote in this
    # invocation; the block is loud in the record.
    assert [call["chapter_id"] for call in book.calls] == ["0002"]
    ch2rec = next(c for c in resumed["chapters"] if c["chapter_id"] == "0002")
    assert ch2rec["terminal_status"] == "complete"
    assert ch2rec["promoted"] is False
    assert "blocked" in (ch2rec["promotion_error"] or ""), ch2rec
    # ...while ready chapters kept their normal (non-blocked) outcome.
    for chapter_id in ("0001", "0003"):
        rec = next(c for c in resumed["chapters"] if c["chapter_id"] == chapter_id)
        assert "blocked" not in (rec["promotion_error"] or ""), rec
    # The heal rewrote a valid manifest: a later invocation passes all
    # gates fresh (skip, no strict rerun) and the block is lifted.
    from pact_v4.pipeline.v4_retry import chapter_readiness
    assert chapter_readiness(ch2)["ready"] is True
    book.calls.clear()
    second = run_book(
        memory_dir=book.memory_dir, chapter_ids=_chapter_ids(),
        chapter_html_pattern=book.pattern, out_base=book.out_base, extra_args=[],
        resume=True,
    )
    assert book.calls == []
    ch2again = next(c for c in second["chapters"] if c["chapter_id"] == "0002")
    assert "blocked" not in (ch2again["promotion_error"] or ""), ch2again


# ---------------------------------------------------------------------------
# Round-5 HIGH: nonempty-but-malformed manifests block same-invocation
# promotion too (not just corrupt files). Chapters heal via stage-aware
# resume and promote on a later all-gates-fresh invocation.
# ---------------------------------------------------------------------------


def _truncate_audit_manifest(out_dir: Path) -> None:
    manifest_path = out_dir / "stage_manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["stages"]["audit"]["artifacts"].remove("b2_handoff.json")
    del manifest["stages"]["audit"]["integrity"]["b2_handoff.json"]
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")


def _unknown_only_manifest(out_dir: Path) -> None:
    (out_dir / "stage_manifest.json").write_text(
        json.dumps({"schema": "pact-v4-stage-manifest/v1",
                    "stages": {"exotic": {"status": "complete"}}}),
        encoding="utf-8")


def test_nonempty_malformed_manifests_block_promotion(book: _BookFixture):
    run_book(
        memory_dir=book.memory_dir, chapter_ids=_chapter_ids(),
        chapter_html_pattern=book.pattern, out_base=book.out_base, extra_args=[],
    )
    ch2 = book.out_base / "chapter_0002"
    ch3 = book.out_base / "chapter_0003"
    _truncate_audit_manifest(ch2)
    _unknown_only_manifest(ch3)
    book.calls.clear()
    resumed = run_book(
        memory_dir=book.memory_dir, chapter_ids=_chapter_ids(),
        chapter_html_pattern=book.pattern, out_base=book.out_base, extra_args=[],
        resume=True,
    )
    # Both chapters reran (safe self-heal) but neither promoted: the block
    # applies to every present manifest that is not fully valid.
    assert sorted(call["chapter_id"] for call in book.calls) == ["0002", "0003"]
    for chapter_id in ("0002", "0003"):
        rec = next(c for c in resumed["chapters"] if c["chapter_id"] == chapter_id)
        assert rec["terminal_status"] == "complete", rec
        assert rec["promoted"] is False, rec
        assert "blocked" in (rec["promotion_error"] or ""), rec
    # The control chapter skipped normally with no block note.
    rec1 = next(c for c in resumed["chapters"] if c["chapter_id"] == "0001")
    assert "blocked" not in (rec1["promotion_error"] or ""), rec1
    # Healed manifests validate again: a later invocation passes all gates
    # fresh (skip, no strict rerun) and the block is lifted for both.
    from pact_v4.pipeline.v4_retry import chapter_readiness
    assert chapter_readiness(ch2)["ready"] is True
    assert chapter_readiness(ch3)["ready"] is True
    book.calls.clear()
    second = run_book(
        memory_dir=book.memory_dir, chapter_ids=_chapter_ids(),
        chapter_html_pattern=book.pattern, out_base=book.out_base, extra_args=[],
        resume=True,
    )
    assert book.calls == []
    for chapter_id in ("0002", "0003"):
        rec = next(c for c in second["chapters"] if c["chapter_id"] == chapter_id)
        assert "blocked" not in (rec["promotion_error"] or ""), rec


# ---------------------------------------------------------------------------
# Final review HIGH: accepted_degraded chapters get promotion-only handling
# (skip, never rerun) with their degraded status preserved verbatim.
# ---------------------------------------------------------------------------


def test_degraded_chapter_promotion_only_skip(book: _BookFixture):
    from pact_v4.pipeline.v4_phase12_strict_runner import StrictRunConfig
    from tests.pact_v4.pipeline.test_book_chapter_retry import (
        _SelectiveFailCaller,
        _quarantine_chunk1_gate,
        _run_chapter,
    )
    from tests.pact_v4.pipeline.test_v4_phase12_strict_runner import (
        _build_artifacts,
        _make_backend,
    )
    from tests.pact_v4.pipeline.test_v4_phase12_strict_runner_repair import (
        _flagging_audit,
    )
    # Chapters 1 and 3 complete normally through the book.
    run_book(
        memory_dir=book.memory_dir, chapter_ids=["0001", "0003"],
        chapter_html_pattern=book.pattern, out_base=book.out_base, extra_args=[],
    )
    # Chapter 2 converges degraded (quarantined chunk, unclosable debt).
    ch2 = book.out_base / "chapter_0002"
    ch2_html = book.root / "0002.html"
    cfg2 = StrictRunConfig(
        chapter_id="0002", chapter_html_path=ch2_html,
        memory_dir=book.memory_dir, out_dir=ch2, backend=_make_backend())
    _, _, chunk_plan2, _ = _build_artifacts(cfg2)
    chunk1_pids = set(chunk_plan2.chunks[0].pids)
    res2, _, _ = _run_chapter(
        cfg2, qwen=_quarantine_chunk1_gate(chunk1_pids),
        qwen_audit=_flagging_audit(sorted(chunk1_pids)[0]),
        with_repair=True, regate_passed=False)
    assert res2.step8["status"] == "accepted_degraded"
    record_before = (ch2 / "strict_chapter_trial_record.json").read_bytes()
    translations_before = (ch2 / "translations.json").read_bytes()
    # Book resume must skip (never rerun) the degraded chapter and keep
    # its degraded terminal status verbatim in the promotion record.
    book.calls.clear()
    resumed = run_book(
        memory_dir=book.memory_dir, chapter_ids=_chapter_ids(),
        chapter_html_pattern=book.pattern, out_base=book.out_base, extra_args=[],
        resume=True,
    )
    assert book.calls == []
    ch2rec = next(c for c in resumed["chapters"] if c["chapter_id"] == "0002")
    assert ch2rec["terminal_status"] == "accepted_degraded", ch2rec
    assert "blocked" not in (ch2rec["promotion_error"] or ""), ch2rec
    assert (ch2 / "strict_chapter_trial_record.json").read_bytes() == record_before
    assert (ch2 / "translations.json").read_bytes() == translations_before


# ---------------------------------------------------------------------------
# Whole-chapter B3 binding: a degraded WC chapter skips models and keeps
# its terminal status only when its B3 artifacts validate.
# ---------------------------------------------------------------------------


def _run_wc_book_chapter(book: _BookFixture, chapter_id: str, *, degraded=False):
    """Build one book chapter dir with a real whole-chapter + FakeB3 run."""
    from pact_v4.pipeline.v4_phase12_strict_runner import StrictRunConfig
    from tests.pact_v4.pipeline.test_book_chapter_retry import (
        _FakeB3,
        _run_wc_chapter,
    )
    out_dir = book.out_base / f"chapter_{chapter_id}"
    cfg = StrictRunConfig(
        chapter_id=chapter_id,
        chapter_html_path=book.root / f"{chapter_id}.html",
        memory_dir=book.memory_dir, out_dir=out_dir,
        backend=_make_backend(), whole_chapter=True)
    result, _, b3 = _run_wc_chapter(cfg, b3=_FakeB3(degraded=degraded))
    assert b3.calls == [chapter_id]
    return result


def test_wc_degraded_promotion_only_skip(book: _BookFixture):
    _run_wc_book_chapter(book, "0001")
    _run_wc_book_chapter(book, "0003")
    _run_wc_book_chapter(book, "0002", degraded=True)
    ch2 = book.out_base / "chapter_0002"
    record_before = (ch2 / "strict_chapter_trial_record.json").read_bytes()
    translations_before = (ch2 / "translations.json").read_bytes()
    # Book resume with the whole-chapter flag skips all model stages for
    # every chapter -- including the degraded one -- and preserves each
    # terminal status byte-intact.
    book.calls.clear()
    resumed = run_book(
        memory_dir=book.memory_dir, chapter_ids=_chapter_ids(),
        chapter_html_pattern=book.pattern, out_base=book.out_base,
        extra_args=["--whole-chapter"], resume=True,
    )
    assert book.calls == []
    expected = {"0001": "complete", "0002": "accepted_degraded",
                "0003": "complete"}
    for chapter_id, terminal in expected.items():
        rec = next(c for c in resumed["chapters"] if c["chapter_id"] == chapter_id)
        assert rec["terminal_status"] == terminal, rec
        assert "blocked" not in (rec["promotion_error"] or ""), rec
    assert (ch2 / "strict_chapter_trial_record.json").read_bytes() == record_before
    assert (ch2 / "translations.json").read_bytes() == translations_before
    # ...and a deleted B3 artifact forces a real rerun (no silent skip).
    (ch2 / "audit_cache_b3.json").unlink()
    from pact_v4.pipeline.v4_retry import chapter_readiness as _readiness
    assert _readiness(ch2)["ready"] is False
    book.calls.clear()
    rerun = run_book(
        memory_dir=book.memory_dir, chapter_ids=_chapter_ids(),
        chapter_html_pattern=book.pattern, out_base=book.out_base,
        extra_args=["--whole-chapter"], resume=True,
    )
    assert [call["chapter_id"] for call in book.calls] == ["0002"]
    assert book.calls[0]["extra_args"] == ["--whole-chapter", "--resume"]
    ch2rerun = next(c for c in rerun["chapters"] if c["chapter_id"] == "0002")
    assert ch2rerun["terminal_status"] == "complete"
    assert (ch2 / "audit_cache_b3.json").exists()
    assert _readiness(ch2)["ready"] is True


def test_drifted_repair_retry_promotion_only_on_second_resume(book: _BookFixture):
    """End-to-end second-resume regression for additive-memory lineage.

    Initial complete run + downstream glossary addition + simulated repair
    failure + ``--resume`` (heal) must leave chapter 2 *ready* -- the
    retained journal entries keep their original pair while the rewritten
    record carries the live one, bridged by the recorded ``resumed_from``
    lineage. A second ``--resume`` then skips chapter 2 (promotion-only)
    instead of rerunning it. Pre-fix, readiness flipped true -> false
    after the heal and the second resume needlessly reran the chapter.
    """
    from pact_v4.pipeline.v4_retry import chapter_readiness
    run_book(
        memory_dir=book.memory_dir, chapter_ids=_chapter_ids(),
        chapter_html_pattern=book.pattern, out_base=book.out_base, extra_args=[],
    )
    ch2 = book.out_base / "chapter_0002"
    # Simulate a repair failure after generation/audit completed.
    (ch2 / "repair_cache.json").unlink()
    (ch2 / "repair_report.json").unlink()
    record_path = ch2 / "strict_chapter_trial_record.json"
    record = json.loads(record_path.read_text(encoding="utf-8"))
    record["step7"] = {"status": "failed", "error": "simulated repair failure"}
    record["step8"] = {"status": "failed", "error": "simulated repair failure"}
    record_path.write_text(json.dumps(record, ensure_ascii=False, indent=2),
                           encoding="utf-8")
    # Downstream chapters already added shared-memory observations.
    glossary_path = book.memory_dir / "glossary.json"
    glossary = json.loads(glossary_path.read_text(encoding="utf-8"))
    glossary["DownstreamTerm"] = "НисходящийТермин"
    glossary_path.write_text(json.dumps(glossary, ensure_ascii=False, indent=2),
                             encoding="utf-8")
    downstream = {
        chapter_id: _snapshot_chapter(book.out_base / f"chapter_{chapter_id}")
        for chapter_id in ("0001", "0003")
    }
    book.calls.clear()
    resumed = run_book(
        memory_dir=book.memory_dir, chapter_ids=_chapter_ids(),
        chapter_html_pattern=book.pattern, out_base=book.out_base, extra_args=[],
        resume=True,
    )
    # Only chapter 2 reran (heal); generation reused, repair re-executed.
    assert [call["chapter_id"] for call in book.calls] == ["0002"]
    assert book.calls[0]["gen_calls"] == 0
    assert book.calls[0]["repair_calls"] > 0
    assert [c["terminal_status"] for c in resumed["chapters"]] == ["complete"] * 3
    # The healed chapter is ready despite retained original-pair journal
    # entries: the recorded lineage pair bridges them to the live record.
    readiness = chapter_readiness(ch2)
    assert readiness["ready"] is True, readiness
    assert readiness["resume_stage"] is None
    # Second resume: chapter 2 is skipped (promotion-only); nothing reruns
    # and downstream chapters plus shared observations stay intact.
    book.calls.clear()
    second = run_book(
        memory_dir=book.memory_dir, chapter_ids=_chapter_ids(),
        chapter_html_pattern=book.pattern, out_base=book.out_base, extra_args=[],
        resume=True,
    )
    assert book.calls == []
    assert [c["terminal_status"] for c in second["chapters"]] == ["complete"] * 3
    for chapter_id in ("0001", "0003"):
        assert _snapshot_chapter(book.out_base / f"chapter_{chapter_id}") == downstream[chapter_id]
    assert json.loads(glossary_path.read_text(encoding="utf-8"))["DownstreamTerm"] == \
        "НисходящийТермин"


def test_multidrift_force_regen_promotion_only_on_final_resume(book: _BookFixture):
    """Book end-to-end for accumulated lineage across two drifts.

    Full run (S1) -> drift M2 + repair failure -> resume heals (record
    S2 + [S1]) -> drift M3 -> force-rerun chapter 2 (regenerates under S3;
    record S3 + [S1, S2]) -> final resume skips chapter 2
    (promotion-only) instead of rerunning it. Pre-fix, the multi-pair
    record omitted lineage and the final resume needlessly reran.
    """
    from pact_v4.pipeline.v4_retry import chapter_readiness
    run_book(
        memory_dir=book.memory_dir, chapter_ids=_chapter_ids(),
        chapter_html_pattern=book.pattern, out_base=book.out_base, extra_args=[],
    )
    ch2 = book.out_base / "chapter_0002"

    def _record_pair():
        identities = json.loads(
            (ch2 / "strict_chapter_trial_record.json").read_text(
                encoding="utf-8"))["identities"]
        return (identities["snapshot_hash"], identities["chunk_plan_hash"])

    def _drift(term):
        glossary_path = book.memory_dir / "glossary.json"
        glossary = json.loads(glossary_path.read_text(encoding="utf-8"))
        glossary[term] = "НисходящийТермин"
        glossary_path.write_text(
            json.dumps(glossary, ensure_ascii=False, indent=2), encoding="utf-8")

    s1 = _record_pair()
    _drift("DownstreamTermOne")
    (ch2 / "repair_cache.json").unlink()
    (ch2 / "repair_report.json").unlink()
    record_path = ch2 / "strict_chapter_trial_record.json"
    record = json.loads(record_path.read_text(encoding="utf-8"))
    record["step7"] = {"status": "failed", "error": "simulated repair failure"}
    record["step8"] = {"status": "failed", "error": "simulated repair failure"}
    record_path.write_text(json.dumps(record, ensure_ascii=False, indent=2),
                           encoding="utf-8")
    downstream = {
        chapter_id: _snapshot_chapter(book.out_base / f"chapter_{chapter_id}")
        for chapter_id in ("0001", "0003")
    }
    book.calls.clear()
    healed = run_book(
        memory_dir=book.memory_dir, chapter_ids=_chapter_ids(),
        chapter_html_pattern=book.pattern, out_base=book.out_base, extra_args=[],
        resume=True,
    )
    assert [call["chapter_id"] for call in book.calls] == ["0002"]
    assert [c["terminal_status"] for c in healed["chapters"]] == ["complete"] * 3
    s2 = _record_pair()
    assert s2 != s1
    assert chapter_readiness(ch2)["ready"] is True
    # Second drift, then a regenerating retry under the new memory.
    _drift("DownstreamTermTwo")
    book.calls.clear()
    forced = run_book(
        memory_dir=book.memory_dir, chapter_ids=_chapter_ids(),
        chapter_html_pattern=book.pattern, out_base=book.out_base, extra_args=[],
        resume=True, force_rerun_chapters=["0002"],
    )
    assert [call["chapter_id"] for call in book.calls] == ["0002"]
    # Single-chunk fixture chapter: exactly one regeneration call proves
    # the force-rerun really regenerated under the new memory.
    assert book.calls[0]["gen_calls"] == 1
    assert [c["terminal_status"] for c in forced["chapters"]] == ["complete"] * 3
    s3 = _record_pair()
    assert len({s1, s2, s3}) == 3
    assert chapter_readiness(ch2)["ready"] is True
    # Final resume: chapter 2 is skipped (promotion-only); nothing reruns
    # and downstream chapters plus both shared observations stay intact.
    book.calls.clear()
    final = run_book(
        memory_dir=book.memory_dir, chapter_ids=_chapter_ids(),
        chapter_html_pattern=book.pattern, out_base=book.out_base, extra_args=[],
        resume=True,
    )
    assert book.calls == []
    assert [c["terminal_status"] for c in final["chapters"]] == ["complete"] * 3
    for chapter_id in ("0001", "0003"):
        assert _snapshot_chapter(book.out_base / f"chapter_{chapter_id}") == downstream[chapter_id]
    glossary_path = book.memory_dir / "glossary.json"
    assert json.loads(glossary_path.read_text(encoding="utf-8"))["DownstreamTermTwo"] == \
        "НисходящийТермин"
