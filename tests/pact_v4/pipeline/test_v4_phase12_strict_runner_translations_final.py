"""B13 contract tests: translations.json is the chapter's single final
translation source, rewritten from repair_report.final_translation.

Owner decision 2026-08-05 (DECISIONS.md): translations.json — read by
book_run and B9 — must carry the FINAL translation (committed repairs,
Phase 5 formatting, healed quarantined-retry winners), not just the
original selected candidates. Contract:

  (a) a chapter with a committed repair AND a healed quarantined-retry
      writes translations.json with every PID of the chapter (400/400 in
      run_005 terms; here: every PID of the test chapter), the repaired /
      healed PIDs equal repair_report.final_translation, and the format is
      {pid: text};
  (б) without repair adapters the historical fallback is kept: the original
      selected candidates;
  (в) resume-incrementality is not broken: a resumed run still reuses its
      caches and the final write still produces the full final map.

B13 also normalizes HTML entities in the merged markup (&lt;em&gt; -> <em>)
so the original's italics survive into the book (owner decision 2026-08-05;
full markup normalization + mixed_script tag exemption is card B14).
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict

from pact_v4.phase1.models import GateResult
from pact_v4.pipeline.v4_phase12_strict_runner import (
    StrictBackendConfig,
    StrictRunConfig,
    _load_repair_report_final_translation,
    _normalize_final_markup,
    run_chapter_strict,
)
from tests.pact_v4.pipeline.test_v4_phase12_strict_runner import (
    StubGemma,
    StubGemmaAudit,
    StubQwenAudit,
    StubRegionGate,
    _LifecycleAwareGemmaAudit,
    _LifecycleAwareGemmaSelector,
    _LifecycleAwareModelCaller,
    _LifecycleAwareQwenAudit,
    _make_router,
)
from tests.pact_v4.pipeline.test_v4_phase12_strict_runner_retry import (
    _make_cfg,
    _run_with_retry,
    ContentAudit,
    ContentQwen,
    LookaheadChunkCaller,
    StubRepairCaller,
)

WORDS_PER_PARAGRAPH = 35


def _write_chapter_html(path: Path, n_paragraphs: int) -> None:
    paragraph_text = " ".join(f"word{i}" for i in range(WORDS_PER_PARAGRAPH))
    body = "\n".join(f"<p>{paragraph_text}</p>" for _ in range(n_paragraphs))
    path.write_text("<html><body>" + body + "</body></html>", encoding="utf-8")


def _write_empty_memory(dir_path: Path) -> None:
    dir_path.mkdir(parents=True, exist_ok=True)
    (dir_path / "glossary.json").write_text("{}", encoding="utf-8")
    (dir_path / "book_memory.json").write_text("{}", encoding="utf-8")


def _make_backend() -> StrictBackendConfig:
    return StrictBackendConfig(
        exe=Path("C:/fake/llama-server.exe"), device="FAKE0", host="127.0.0.1",
        model_paths={"gemma": Path("C:/fake/gemma.gguf"), "qwen": Path("C:/fake/qwen.gguf")},
        model_names={"gemma": "gemma-fake", "qwen": "qwen-fake"},
        server_args={"gemma": [], "qwen": []}, port=0,
    )


def _make_chapter_cfg(
    tmp_path: Path, *, n_paragraphs: int = 24,
    mixed_script_allow: tuple = (),
) -> StrictRunConfig:
    chapter_html = tmp_path / "046.html"
    memory_dir = tmp_path / "memory"
    _write_chapter_html(chapter_html, n_paragraphs)
    _write_empty_memory(memory_dir)
    return StrictRunConfig(
        chapter_id="046", chapter_html_path=chapter_html, memory_dir=memory_dir,
        out_dir=tmp_path / "out", backend=_make_backend(),
        max_consecutive_terminal_nonselections=3,
        deterministic_mixed_script_allow=mixed_script_allow,
    )


def _render_translation(bundle, *, marker: str) -> Dict[str, str]:
    """Translate like StubModelCaller but with a per-chunk marker prefix."""
    out: Dict[str, str] = {}
    for index, (pid, text) in enumerate(bundle.owned_source, start=1):
        digits = "".join(ch for ch in text if ch.isdigit())
        digit_part = f" ({digits})" if digits else ""
        out[pid] = f"{marker} номер{index}{digit_part}"
    return out


class CombinedChunkCaller:
    """chunk0001 bad without look-ahead (quarantined), good with look-ahead
    (healed by retry); chunk0002 always good but carries an ``&lt;em&gt;``
    entity in one PID so the committed-repair text exercises the B13
    entity normalization path (the repair caller fixes that PID)."""

    def __init__(self) -> None:
        self.calls: list = []

    def __call__(self, bundle) -> str:
        self.calls.append(bundle)
        if bundle.chunk_id == "chunk0001" and not bundle.right_context:
            out = _render_translation(bundle, marker="Плохой перевод")
        else:
            out = _render_translation(bundle, marker="Хороший перевод")
        return json.dumps(out, ensure_ascii=False)


class CombinedAudit(ContentAudit):
    """Step 6 / re-audit Qwen: flag the bad chunk0001 text AND one PID of
    chunk0002 (p00017) so a committed repair happens on a selected chunk."""

    def __call__(self, *, chunk_id, source, translation) -> str:
        self.calls.append((chunk_id, dict(source), dict(translation)))
        text = " ".join(translation.values())
        if "Плохой" in text:
            pid = next(iter(translation))
            return json.dumps({"issues": [
                {"pid": pid, "category": "omission", "note": "dropped clause"}
            ]})
        if chunk_id == "chunk0002":
            pid = next(iter(translation))
            return json.dumps({"issues": [
                {"pid": pid, "category": "omission", "note": "dropped clause"}
            ]})
        return json.dumps({"issues": []})


class CombinedRegionGate(StubRegionGate):
    """Per-PID re-gate: chunk0002's repair commits (p00017+), chunk0001's
    repair keeps debt (p00001..p00016) so the retry cycle fires and heals
    it — the chapter exercises BOTH a committed repair and a healed
    quarantined-retry in one run."""

    def __init__(self) -> None:
        self.calls: list = []

    def __call__(self, *, source_text, repaired_text, region) -> GateResult:
        self.calls.append(region.pid)
        pid_index = int(region.pid[1:])
        if pid_index >= 17:  # chunk0002 pids are p00017..p00024
            return GateResult(gate="region_fidelity", passed=True, detail="ok")
        return GateResult(gate="region_fidelity", passed=False, detail="re-gate fails")


class EntityRepairCaller(StubRepairCaller):
    """Committed-repair caller whose repaired text carries an HTML entity
    (&lt;em&gt;) — the run_005-style formatting/model defect — so the final
    write must normalize it back to a clean <em> tag in translations.json."""

    def __call__(self, *, chunk_id, source, translation, region, findings) -> str:
        self.calls.append((chunk_id, region.pid))
        pid = region.pid
        digits = "".join(ch for ch in source.get(pid, "") if ch.isdigit())
        digit_part = f" ({digits})" if digits else ""
        return json.dumps(
            {"repaired": {pid: f"&lt;em&gt;Исправленный перевод{digit_part}&lt;/em&gt;"},
             "reason": "scripted"},
            ensure_ascii=False,
        )


def _run_combined(
    cfg: StrictRunConfig,
    *,
    repair_text_entities: bool = False,
):
    """Run the strict driver with Phase 4 repair adapters + the B6 stubs.

    chunk0001 is quarantined (bad text) and healed by the retry cycle;
    chunk0002 is selected and one of its PIDs (p00017) gets a committed
    repair (the per-PID re-gate commits chunk0002 but keeps chunk0001's
    repair debt so the retry fires).
    """
    router = _make_router()
    inner = CombinedChunkCaller()
    model_caller = _LifecycleAwareModelCaller(router, inner)
    qwen_audit = CombinedAudit()
    repair_caller = EntityRepairCaller() if repair_text_entities else StubRepairCaller()
    result = run_chapter_strict(
        cfg, router=router,
        model_caller=model_caller,
        qwen_evaluator=ContentQwen(),
        gemma_selector=_LifecycleAwareGemmaSelector(router, StubGemma()),
        qwen_audit_evaluator=_LifecycleAwareQwenAudit(router, qwen_audit),
        gemma_audit_evaluator=_LifecycleAwareGemmaAudit(router, StubGemmaAudit()),
        repair_adapters=(
            repair_caller,
            CombinedRegionGate(),
            _LifecycleAwareQwenAudit(router, StubQwenAudit()),
            _LifecycleAwareGemmaAudit(router, StubGemmaAudit()),
        ),
    )
    return result, router, inner, qwen_audit, repair_caller


def _load_report(cfg: StrictRunConfig) -> Dict[str, Any]:
    return json.loads(
        (cfg.out_dir / "repair_report.json").read_text(encoding="utf-8")
    )


def _pids_of(cfg: StrictRunConfig, chunk_id: str):
    plan = json.loads((cfg.out_dir / "chunk_plan.json").read_text(encoding="utf-8"))
    return next(c["pids"] for c in plan["chunks"] if c["chunk_id"] == chunk_id)


class AlwaysGoodCaller:
    """Every chunk translates to good text (all chunks selected)."""

    def __init__(self) -> None:
        self.calls: list = []

    def __call__(self, bundle) -> str:
        self.calls.append(bundle)
        return json.dumps(_render_translation(bundle, marker="Хороший перевод"),
                          ensure_ascii=False)


# ---------------------------------------------------------------------------
# (а) committed repair + healed quarantined-retry -> full final map
# ---------------------------------------------------------------------------


def test_translations_json_is_full_final_map_with_repair_and_retry(tmp_path: Path):
    # Chapter: chunk0001 quarantined (bad text) -> healed by retry;
    # chunk0002 selected with one committed repair (p00017). The re-gate
    # passes so the chunk0002 repair commits; chunk0001 keeps debt until
    # the retry heals it.
    cfg = _make_chapter_cfg(tmp_path, n_paragraphs=24)
    result, _router, caller, audit, repair_caller = _run_combined(cfg)
    assert result.step7["quarantined_retry"]["status"] == "ran"
    assert result.step7["quarantined_retry"]["selected_chunk_ids"] == ["chunk0001"]
    assert repair_caller.calls  # a committed repair actually ran

    translations = json.loads(result.translations_path.read_text(encoding="utf-8"))
    report = _load_report(cfg)
    report_final = dict(report["final_translation"])

    # 400/400-equivalent: every PID of the chapter is present.
    assert set(translations) == set(report_final)
    assert len(translations) == len(report_final)
    # Format {pid: text}.
    assert all(isinstance(pid, str) and isinstance(text, str)
               for pid, text in translations.items())
    # Repaired / healed PIDs equal final_translation (normalized).
    assert translations == {
        pid: _normalize_final_markup(text) for pid, text in report_final.items()
    }
    # The committed-repair PID (first of chunk0002) carries the repaired text.
    chunk0002_first = _pids_of(cfg, "chunk0002")[0]
    assert "Исправленный перевод" in translations[chunk0002_first]
    assert translations[chunk0002_first] == _normalize_final_markup(
        report_final[chunk0002_first]
    )
    # The healed quarantined chunk's PIDs carry the retry winner text, not
    # the bad original candidate.
    chunk0001_pids = _pids_of(cfg, "chunk0001")
    chunk0001_texts = [translations[pid] for pid in chunk0001_pids]
    assert all("Хороший перевод" in text for text in chunk0001_texts)
    assert all("Плохой перевод" not in text for text in chunk0001_texts)


def test_translations_json_normalizes_html_entities_from_repair(tmp_path: Path):
    # run_005 defect: the final translation can carry double-escaped markup
    # (&lt;em&gt;). B13 keeps the original's italics by unescaping entities
    # to clean tags when merging into translations.json; the visible text is
    # otherwise unchanged. B14 completes the fix: the mixed_script detector
    # ignores the tag tokens itself, so no config allowlist is needed for
    # the entity-carrying repair text to commit.
    cfg = _make_chapter_cfg(tmp_path, n_paragraphs=24)
    result, _router, _caller, _audit, repair_caller = _run_combined(
        cfg, repair_text_entities=True,
    )
    assert repair_caller.calls

    translations = json.loads(result.translations_path.read_text(encoding="utf-8"))
    report = _load_report(cfg)
    report_final = dict(report["final_translation"])
    chunk0002_first = _pids_of(cfg, "chunk0002")[0]

    # The report keeps the escaped form (diagnostic artifact unchanged)...
    assert "&lt;em&gt;" in report_final[chunk0002_first]
    # ...while translations.json carries the clean tag.
    repaired_text = translations[chunk0002_first]
    assert "<em>Исправленный перевод" in repaired_text
    assert "&lt;em&gt;" not in repaired_text
    # Only entities were unescaped — the text is otherwise identical.
    assert repaired_text == _normalize_final_markup(report_final[chunk0002_first])


def test_load_repair_report_final_translation_reads_on_disk_report(tmp_path: Path):
    # The authoritative source for the final write is the on-disk
    # repair_report.json (the retry cycle re-writes it with the merged map;
    # the frozen in-memory RepairPhaseResult is pre-retry). Verify the
    # loader returns the merged map and is safe on missing/corrupt files.
    cfg = _make_chapter_cfg(tmp_path, n_paragraphs=24)
    _run_combined(cfg)

    loaded = _load_repair_report_final_translation(cfg.out_dir)
    report = _load_report(cfg)
    assert loaded == dict(report["final_translation"])
    assert loaded  # non-empty

    # Missing file -> None (caller falls back to selected candidates).
    assert _load_repair_report_final_translation(tmp_path / "nope") is None
    # Corrupt JSON -> None.
    bad = tmp_path / "bad"
    bad.mkdir(exist_ok=True)
    (bad / "repair_report.json").write_text("{not json", encoding="utf-8")
    assert _load_repair_report_final_translation(bad) is None


def test_normalize_final_markup_only_unescapes_entities(tmp_path: Path):
    assert _normalize_final_markup("&lt;em&gt;курсив&lt;/em&gt;") == "<em>курсив</em>"
    assert _normalize_final_markup("обычный текст") == "обычный текст"
    # B14 contract: only INLINE-TAG entities are converted to tags; other
    # entities and the visible text are left byte-identical ("текст не
    # меняется" — a literal &amp; stays &amp;).
    assert _normalize_final_markup("&amp; &quot; &#39;") == "&amp; &quot; &#39;"


# ---------------------------------------------------------------------------
# (б) no repair adapters -> historical fallback to selected candidates
# ---------------------------------------------------------------------------


def test_translations_json_falls_back_without_repair_adapters(tmp_path: Path):
    cfg = _make_chapter_cfg(tmp_path, n_paragraphs=8)
    router = _make_router()
    model_caller = _LifecycleAwareModelCaller(router, AlwaysGoodCaller())
    result = run_chapter_strict(
        cfg, router=router, model_caller=model_caller,
        qwen_evaluator=ContentQwen(),
        gemma_selector=_LifecycleAwareGemmaSelector(router, StubGemma()),
        qwen_audit_evaluator=_LifecycleAwareQwenAudit(router, StubQwenAudit()),
        gemma_audit_evaluator=_LifecycleAwareGemmaAudit(router, StubGemmaAudit()),
    )
    assert result.step7["status"] == "skipped"
    assert result.step7["reason"] == "repair_adapters_not_configured"
    translations = json.loads(result.translations_path.read_text(encoding="utf-8"))
    # Historical fallback: the original selected candidates (no repair text).
    assert translations
    assert all("Хороший перевод" in text for text in translations.values())
    assert not (cfg.out_dir / "repair_report.json").exists()


# ---------------------------------------------------------------------------
# (в) resume-incrementality is not broken
# ---------------------------------------------------------------------------


def test_translations_json_resume_keeps_caches_and_full_final_map(tmp_path: Path):
    cfg = _make_chapter_cfg(tmp_path, n_paragraphs=24)
    first, _r1, _c1, _a1, repair_caller1 = _run_combined(cfg)
    assert repair_caller1.calls

    resumed_cfg = StrictRunConfig(
        chapter_id=cfg.chapter_id, chapter_html_path=cfg.chapter_html_path,
        memory_dir=cfg.memory_dir, out_dir=cfg.out_dir, backend=cfg.backend,
    )
    second, _r2, caller2, _a2, repair_caller2 = _run_combined(resumed_cfg)
    assert second.resumed_from_index > 0
    # Resume-incrementality: journaled chunks are never regenerated — the
    # chunk model caller is not re-invoked on a clean resume.
    assert caller2.calls == []
    # NOTE: the repair caller MAY legitimately re-fire on this resume. The
    # retry cycle merges the healed chunk's generation record (overwriting
    # the pre-retry best-variant), so the resumed assembled chapter differs
    # from the first session's Step 6 chapter and the audit-cache identity
    # misses — re-audit -> re-repair. This is identical pre-B13 behavior
    # (verified on main) and outside B13's scope; B13's own guarantee is the
    # final map below.
    # The final write still produces the full final map after resume.
    translations = json.loads(second.translations_path.read_text(encoding="utf-8"))
    report = _load_report(resumed_cfg)
    report_final = dict(report["final_translation"])
    assert set(translations) == set(report_final)
    assert translations == {
        pid: _normalize_final_markup(text) for pid, text in report_final.items()
    }


def test_translations_json_retry_resume_reuses_prior_attempt_and_final_map(tmp_path: Path):
    # The B6 resume gate (owner decision 2026-08-04) reuses the prior retry
    # attempt; the final translations.json must still be the full final map.
    cfg = _make_chapter_cfg(tmp_path, n_paragraphs=24)
    _run_with_retry(cfg)

    resumed_cfg = StrictRunConfig(
        chapter_id=cfg.chapter_id, chapter_html_path=cfg.chapter_html_path,
        memory_dir=cfg.memory_dir, out_dir=cfg.out_dir, backend=cfg.backend,
    )
    second, _r2, caller2, _audit2, _reaudit2 = _run_with_retry(resumed_cfg)
    assert second.resumed_from_index > 0
    assert caller2.calls == []  # prior retry attempt reused, no re-generation
    translations = json.loads(second.translations_path.read_text(encoding="utf-8"))
    report = _load_report(resumed_cfg)
    report_final = dict(report["final_translation"])
    assert set(translations) == set(report_final)
    assert translations == {
        pid: _normalize_final_markup(text) for pid, text in report_final.items()
    }
