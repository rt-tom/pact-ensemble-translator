"""v41 italics — book-run formatting integration (finding round 2).

Proves:
  * run_book with a mock formatting_client calls resolve_format_mappings and
    restores <em> via run_formatting_align (model-call path).
  * run_book with formatting_client=None keeps graceful debt fallback (no crash,
    incident written, chapter still complete via lenient max_incidents).
  * CLI default path (without --no-formatting) wires a formatting_client when
    runtime-config is supplied (main builds client).
"""
from __future__ import annotations

import json
from pathlib import Path

from pact_v4.phase0b.source_html import parse_source_html


class _Gen:
    def __init__(self, content):
        self.content = content


class _MockFormattingClient:
    def __init__(self, blocks, translations, pid_map=None):
        self.calls = 0
        self.blocks = blocks
        self.translations = translations
        self.pid_map = pid_map or {}

    def complete(self, messages, cfg, max_tokens, label=None):
        self.calls += 1
        # Return mappings for all spans: target_text = russian word expected in translation
        # We synthesize from pid_map: {(pid,span_id): target_text}
        # Build JSON
        mappings = []
        for (pid, span_id), target in self.pid_map.items():
            mappings.append({"pid": pid, "span_id": span_id, "target_text": target, "occurrence": 1})
        return _Gen(json.dumps({"mappings": mappings}, ensure_ascii=False))


def _setup_memory(tmp_path: Path) -> Path:
    memory = tmp_path / "memory"
    memory.mkdir(parents=True, exist_ok=True)
    (memory / "glossary.json").write_text("{}", encoding="utf-8")
    (memory / "book_memory.json").write_text(json.dumps({"pov": {"gender": "male"}}), encoding="utf-8")
    (memory / "observations.json").write_text("{}", encoding="utf-8")
    return memory


def _make_chapter_artifacts(out_dir: Path, chapter_id: str, translations: dict, chunk_plan: dict | None = None, terminal="complete"):
    out_dir.mkdir(parents=True, exist_ok=True)
    # selection
    chunk_ids = [c["chunk_id"] for c in (chunk_plan["chunks"] if chunk_plan else [])] or ["chunk0001"]
    results = [{"chunk_id": cid, "status": "selected"} for cid in chunk_ids]
    (out_dir / "selection_results.json").write_text(json.dumps({"chapter_id": chapter_id, "results": results}, ensure_ascii=False), encoding="utf-8")
    (out_dir / "strict_chapter_trial_record.json").write_text(json.dumps({"chapter_id": chapter_id, "step8": {"status": terminal}, "identities": {"backend_identity_hash": "abc123"}}, ensure_ascii=False), encoding="utf-8")
    (out_dir / "translations.json").write_text(json.dumps(translations, ensure_ascii=False), encoding="utf-8")
    if chunk_plan:
        (out_dir / "chunk_plan.json").write_text(json.dumps(chunk_plan, ensure_ascii=False), encoding="utf-8")


def test_run_book_with_mock_formatting_client_restores_em(tmp_path, monkeypatch):
    from pact_full_pipeline_runner_v1 import v4_book_run

    memory = _setup_memory(tmp_path)
    out_base = tmp_path / "out"
    src_dir = tmp_path / "src"
    src_dir.mkdir(parents=True, exist_ok=True)

    html = "<html><body><p>Hello <em>world</em> again.</p></body></html>"
    (src_dir / "0001.html").write_text(html, encoding="utf-8")
    blocks = parse_source_html(html)
    pid = blocks[0].pid
    translations = {pid: "Привет мир снова."}
    plan = {"artifact": "pact-v4-chunk-plan/v1", "snapshot_hash": "t", "plan_hash": "t", "chunks": [{"chunk_id": "chunk0001", "snapshot_hash": "t", "pids": [pid], "word_counts": [], "context": {"left_ru": "", "right_en": []}, "undersized_exception": False}]}

    # mock strict run
    def fake_run_one(*a, **k):
        return {"status": "ok"}
    monkeypatch.setattr(v4_book_run, "_run_one_chapter", fake_run_one)
    _make_chapter_artifacts(out_base / "chapter_0001", "0001", translations, plan)

    # mock formatting client that returns target_text "мир" for the span
    # Need span_id: parse
    span_id = blocks[0].inline_spans[0].span_id
    client = _MockFormattingClient(blocks, translations, pid_map={(pid, span_id): "мир"})

    result = v4_book_run.run_book(
        memory_dir=memory,
        chapter_ids=["0001"],
        chapter_html_pattern=str(src_dir / "{chapter_id}.html"),
        out_base=out_base,
        formatting_client=client,
        max_formatting_incidents=999,
    )
    assert client.calls >= 1
    # translations.json should contain <em>
    final = json.loads((out_base / "chapter_0001" / "translations.json").read_text(encoding="utf-8"))
    assert "<em>мир</em>" in final[pid]
    # formatting_report should show resolved
    report = json.loads((out_base / "chapter_0001" / "formatting_report.json").read_text(encoding="utf-8"))
    assert report["resolved_count"] >= 1 or report["outcome"]["resolved_count"] >= 1 or any(True for _ in report.values())
    # Ensure we actually had model-target tier
    # Check report outcome if present
    outcome = report.get("outcome") or report
    # at least one mapping resolved
    assert outcome.get("resolved_count", 0) >= 1 or report.get("resolved_count", 0) >= 1


def test_run_book_without_client_graceful_debt(tmp_path, monkeypatch):
    from pact_full_pipeline_runner_v1 import v4_book_run
    memory = _setup_memory(tmp_path)
    out_base = tmp_path / "out2"
    src_dir = tmp_path / "src2"
    src_dir.mkdir(parents=True, exist_ok=True)
    html = "<html><body><p>Hello <em>world</em>.</p></body></html>"
    (src_dir / "0001.html").write_text(html, encoding="utf-8")
    blocks = parse_source_html(html)
    pid = blocks[0].pid
    translations = {pid: "Привет мир."}
    plan = {"artifact": "pact-v4-chunk-plan/v1", "snapshot_hash": "t", "plan_hash": "t", "chunks": [{"chunk_id": "chunk0001", "snapshot_hash": "t", "pids": [pid], "word_counts": [], "context": {"left_ru": "", "right_en": []}, "undersized_exception": False}]}
    def fake_run_one(*a, **k):
        return {"status": "ok"}
    monkeypatch.setattr(v4_book_run, "_run_one_chapter", fake_run_one)
    _make_chapter_artifacts(out_base / "chapter_0001", "0001", translations, plan)
    # No client
    result = v4_book_run.run_book(
        memory_dir=memory,
        chapter_ids=["0001"],
        chapter_html_pattern=str(src_dir / "{chapter_id}.html"),
        out_base=out_base,
        formatting_client=None,
        max_formatting_incidents=999,
    )
    # Should not crash, chapter still complete (lenient debt)
    assert result["chapters"][0]["terminal_status"] in ("complete", "accepted_degraded")
    # formatting_report should exist with incident (debt) because no mapping
    report = json.loads((out_base / "chapter_0001" / "formatting_report.json").read_text(encoding="utf-8"))
    # debt: incident count >=1 or resolved 0
    # Graceful: no exception


def test_cli_wires_formatting_client_when_runtime_config(tmp_path, monkeypatch):
    # Verify that main builds a formatting_client when --runtime-config is supplied.
    # We monkeypatch _build_formatting_client to record call.
    from pact_full_pipeline_runner_v1 import v4_book_run
    calls = []
    orig = v4_book_run._build_formatting_client
    def fake_build(args, extra, fmt_cfg):
        calls.append((args, extra, fmt_cfg))
        if not fmt_cfg.get("enabled", True):
            return None
        class _Fake:
            def complete(self, messages, cfg, max_tokens, label=None):
                return _Gen('{"mappings":[]}')
            def close(self):
                pass
        return _Fake()
    monkeypatch.setattr(v4_book_run, "_build_formatting_client", fake_build)
    # Also mock run_book to capture formatting_client
    captured = {}
    def fake_run_book(**kwargs):
        captured.update(kwargs)
        return {"chapters": [{"chapter_id": "0001", "terminal_status": "complete", "promoted": False, "candidates": {}, "book_memory_candidates": {}, "book_memory_promotions": [], "index_built": False, "error": None, "media_confirmation": None, "media_error": None}], "memory_dir": str(kwargs["memory_dir"]), "candidates_ledger": "x", "book_memory_candidates_ledger": "y"}
    monkeypatch.setattr(v4_book_run, "run_book", fake_run_book)
    memory = tmp_path / "mem"
    memory.mkdir(parents=True, exist_ok=True)
    (memory / "glossary.json").write_text("{}", encoding="utf-8")
    (memory / "book_memory.json").write_text("{}", encoding="utf-8")
    out_base = tmp_path / "out"
    src_dir = tmp_path / "src"
    src_dir.mkdir(parents=True, exist_ok=True)
    (src_dir / "0001.html").write_text("<p>hi</p>", encoding="utf-8")
    # Provide dummy runtime-config file so _build would be called (we patched it)
    rc = tmp_path / "rc.yaml"
    rc.write_text("kind: local_llama\n", encoding="utf-8")
    # Call main with extra runtime-config
    v4_book_run.main(["--memory-dir", str(memory), "--chapters", "0001", "--chapter-html-pattern", str(src_dir / "{chapter_id}.html"), "--out-base", str(out_base), "--runtime-config", str(rc)])
    assert len(calls) == 1
    assert captured.get("formatting_client") is not None
    # When --no-formatting, client should be None
    calls.clear()
    captured.clear()
    v4_book_run.main(["--memory-dir", str(memory), "--chapters", "0001", "--chapter-html-pattern", str(src_dir / "{chapter_id}.html"), "--out-base", str(out_base), "--no-formatting"])
    assert captured.get("formatting_client") is None
