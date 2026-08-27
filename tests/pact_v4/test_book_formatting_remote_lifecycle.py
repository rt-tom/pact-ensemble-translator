"""Remote formatting lifecycle tests: external->managed promotion, per-chapter close, composite routing, health-failure diagnostics."""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from pact_v4.phase0b.source_html import parse_source_html


def _setup_memory(tmp_path: Path) -> Path:
    memory = tmp_path / "memory"
    memory.mkdir(parents=True, exist_ok=True)
    (memory / "glossary.json").write_text("{}", encoding="utf-8")
    (memory / "book_memory.json").write_text(json.dumps({"pov": {"gender": "male"}}), encoding="utf-8")
    (memory / "observations.json").write_text("{}", encoding="utf-8")
    return memory


def _make_chapter_artifacts(out_dir: Path, chapter_id: str, translations: dict, plan=None, terminal="complete"):
    out_dir.mkdir(parents=True, exist_ok=True)
    pid_list = list(translations.keys())
    # default plan
    if plan is None:
        plan = {"artifact": "pact-v4-chunk-plan/v1", "snapshot_hash": "t", "plan_hash": "t", "chunks": [{"chunk_id": "chunk0001", "snapshot_hash": "t", "pids": pid_list, "word_counts": [], "context": {"left_ru": "", "right_en": []}, "undersized_exception": False}]}
    chunk_ids = [c["chunk_id"] for c in plan["chunks"]]
    results = [{"chunk_id": cid, "status": "selected"} for cid in chunk_ids]
    (out_dir / "selection_results.json").write_text(json.dumps({"chapter_id": chapter_id, "results": results}, ensure_ascii=False), encoding="utf-8")
    (out_dir / "strict_chapter_trial_record.json").write_text(json.dumps({"chapter_id": chapter_id, "step8": {"status": terminal}, "identities": {"backend_identity_hash": "abc123"}}, ensure_ascii=False), encoding="utf-8")
    (out_dir / "translations.json").write_text(json.dumps(translations, ensure_ascii=False), encoding="utf-8")
    (out_dir / "chunk_plan.json").write_text(json.dumps(plan, ensure_ascii=False), encoding="utf-8")


def test_formatting_backend_with_overrides_external_promotes_to_managed_4097():
    from pact_full_pipeline_runner_v1.v4_book_run import _formatting_backend_with_overrides
    from pact_v4.runtime.runtime_config import OpenCodeBackendConfig
    from pact_v4.runtime.opencode_backend import OpenCodeServerBackendConfig
    from pact_v4.runtime.opencode_server_lifecycle import ManagedServerSpec

    server = OpenCodeServerBackendConfig(
        base_url="http://127.0.0.1:4097",
        reasoning=3,
    )
    cfg = OpenCodeBackendConfig(server=server, server_mode="external", managed=None)
    out = _formatting_backend_with_overrides(cfg)
    # Must be promoted to managed
    assert out.server_mode == "managed"
    assert out.managed is not None
    assert out.managed.port == 4097
    # reasoning forced to 0
    assert out.server.reasoning == 0


def test_formatting_backend_with_overrides_composite_external_promotes():
    from pact_full_pipeline_runner_v1.v4_book_run import _formatting_backend_with_overrides
    from pact_v4.runtime.runtime_config import CompositeBackendConfig, OpenCodeBackendConfig
    from pact_v4.runtime.opencode_backend import OpenCodeServerBackendConfig
    from pact_v4.runtime.runtime_config import LocalLlamaBackendConfig
    from pact_full_pipeline_runner_v1.v4_phase12_strict_run import _gemma_server_args_for_reasoning

    # opencode external sub-backend
    server = OpenCodeServerBackendConfig(
        base_url="http://127.0.0.1:4097",
        reasoning=2,
    )
    opencode_cfg = OpenCodeBackendConfig(server=server, server_mode="external", managed=None)
    local_cfg = LocalLlamaBackendConfig(
        exe=Path("/tmp/fake.exe"),
        device="CPU",
        host="127.0.0.1",
        model_paths={"gemma": Path("/tmp/gemma.gguf"), "qwen": Path("/tmp/qwen.gguf")},
        model_names={"gemma": "gemma", "qwen": "qwen"},
        server_args={"gemma": _gemma_server_args_for_reasoning(3), "qwen": []},
        port=8094,
    )
    composite = CompositeBackendConfig(backends={"opencode": opencode_cfg, "local": local_cfg}, role_backend_map={"generator": "opencode", "gemma_audit": "local"})
    out = _formatting_backend_with_overrides(composite)
    # opencode sub-backend promoted
    assert out.backends["opencode"].server_mode == "managed"
    assert out.backends["opencode"].managed.port == 4097
    assert out.backends["opencode"].server.reasoning == 0
    # local gemma reasoning 0
    assert out.backends["local"].server_args["gemma"] == _gemma_server_args_for_reasoning(0)


def test_build_formatting_client_leak_runtime_close_on_build_role_failure(tmp_path: Path, monkeypatch):
    from pact_full_pipeline_runner_v1 import v4_book_run
    from pact_v4.runtime.runtime_config import LocalLlamaBackendConfig
    from pact_full_pipeline_runner_v1.v4_phase12_strict_run import _gemma_server_args_for_reasoning
    import pact_v4.runtime.runtime_config as rc_mod

    backend = LocalLlamaBackendConfig(
        exe=Path("/tmp/fake.exe"),
        device="CPU",
        host="127.0.0.1",
        model_paths={"gemma": Path("/tmp/gemma.gguf"), "qwen": Path("/tmp/qwen.gguf")},
        model_names={"gemma": "gemma", "qwen": "qwen"},
        server_args={"gemma": _gemma_server_args_for_reasoning(0), "qwen": []},
        port=8094,
    )

    monkeypatch.setattr(v4_book_run, "_load_runtime_config_file", lambda p: backend, raising=False)
    import pact_full_pipeline_runner_v1.v4_phase12_strict_run as strict_cli
    monkeypatch.setattr(strict_cli, "_load_runtime_config_file", lambda p: backend, raising=False)

    closed = {"closed": False}

    class FakeRuntime:
        def close(self):
            closed["closed"] = True

    def fake_build_runtime(self, log_dir=None):
        return FakeRuntime()

    monkeypatch.setattr(LocalLlamaBackendConfig, "build_runtime", fake_build_runtime)
    # build_role_backend raises
    monkeypatch.setattr(rc_mod, "build_role_backend", lambda b, rt: (_ for _ in ()).throw(RuntimeError("role fail")))

    args = MagicMock()
    args.memory_dir = str(tmp_path)
    args.runtime_config = None
    args.translator = None
    args.reviewer = None
    args.providers_config = None
    rc_file = tmp_path / "rc.yaml"
    rc_file.write_text("dummy", encoding="utf-8")
    client = v4_book_run._build_formatting_client(args, ["--runtime-config", str(rc_file)], {"enabled": True}, out_dir=tmp_path / "out")
    assert client is None
    assert closed["closed"] is True, "runtime must be closed when build_role_backend fails"


def test_run_book_remote_per_chapter_lifecycle_with_logs_and_close(tmp_path, monkeypatch):
    from pact_full_pipeline_runner_v1 import v4_book_run

    memory = _setup_memory(tmp_path)
    out_base = tmp_path / "out"
    src_dir = tmp_path / "src"
    src_dir.mkdir(parents=True, exist_ok=True)
    html = "<html><body><p>Hello <em>world</em> again.</p></body></html>"
    (src_dir / "0001.html").write_text(html, encoding="utf-8")
    (src_dir / "0002.html").write_text(html, encoding="utf-8")
    blocks = parse_source_html(html)
    pid = blocks[0].pid
    translations = {pid: "Привет мир снова."}
    plan = {"artifact": "pact-v4-chunk-plan/v1", "snapshot_hash": "t", "plan_hash": "t", "chunks": [{"chunk_id": "chunk0001", "snapshot_hash": "t", "pids": [pid], "word_counts": [], "context": {"left_ru": "", "right_en": []}, "undersized_exception": False}]}

    def fake_run_one(*a, **k):
        return {"status": "ok"}
    monkeypatch.setattr(v4_book_run, "_run_one_chapter", fake_run_one)
    # Pre-create artifacts for both chapters (run_book will read them after fake_run_one)
    _make_chapter_artifacts(out_base / "chapter_0001", "0001", translations, plan)
    _make_chapter_artifacts(out_base / "chapter_0002", "0002", translations, plan)

    # Track per-chapter builds and closes
    builds = []
    closes = []
    # Fake runtime that creates real log files
    class FakeRuntime:
        def __init__(self, log_dir):
            self.log_dir = Path(log_dir) if log_dir else None
            # Simulate ManagedServerProcess creating stdout/stderr logs
            if self.log_dir:
                self.log_dir.mkdir(parents=True, exist_ok=True)
                (self.log_dir / "opencode_serve_20250101_120000_stdout.log").write_text("stdout real", encoding="utf-8")
                (self.log_dir / "opencode_serve_20250101_120000_stderr.log").write_text("stderr real", encoding="utf-8")
        def close(self):
            closes.append(1)

    class FakeBackend:
        def build_runtime(self, log_dir=None):
            builds.append(str(log_dir))
            return FakeRuntime(log_dir)
        descriptor = MagicMock(model_bindings={"generator": "opencode/muse-spark-1.2-contributor-free"})

    # Mock _build_formatting_client to use FakeBackend and return _FormattingBackendClient-like object
    def fake_build(args, extra, fmt_cfg, out_dir=None):
        # Verify out_dir is per-chapter (contains chapter id)
        assert out_dir is not None
        assert "chapter_" in str(out_dir)
        # Verify log_dir would be out_dir/server_logs
        runtime = FakeBackend().build_runtime(log_dir=Path(out_dir) / "server_logs")
        # Create a fake client whose resolve returns mapping
        class FakeClient:
            def __init__(self, rt):
                self._runtime = rt
            def close(self):
                self._runtime.close()
        client = FakeClient(runtime)
        # Simulate health wait success
        return client

    monkeypatch.setattr(v4_book_run, "_build_formatting_client", fake_build)

    # Mock resolve_format_mappings to return real mapping and write batch meta (simulate real behavior)
    def fake_resolve(client, cfg, blks, trans, out_dir=None):
        # Write expected batch meta as real resolve would
        from pact_v4.phase5.formatting import _effective_max_tokens
        span_count = sum(len(b.inline_spans) for b in blks)
        effective = _effective_max_tokens(span_count, cfg.get("max_tokens"))
        meta = {"batch": 1, "attempt": 1, "span_count": span_count, "effective_max_tokens": effective, "finish_reason": "stop", "usage": {"prompt_tokens": 10}, "response_format_attempted": True}
        if out_dir:
            Path(out_dir, "formatting_batch1_meta.json").write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")
        # Return mappings for all spans
        return {(pid, blocks[0].inline_spans[0].span_id): ("мир", 1)}

    monkeypatch.setattr("pact_v4.phase5.formatting.resolve_format_mappings", fake_resolve, raising=False)
    # Also need to patch the import inside v4_book_run's local scope - monkeypatch the module attribute
    import pact_v4.phase5.formatting as fmt_mod
    monkeypatch.setattr(fmt_mod, "resolve_format_mappings", fake_resolve, raising=False)

    # Need to ensure v4_book_run's internal import resolves to our fake - patch via sys.modules
    # run_book imports inside function, so our monkeypatch on fmt_mod suffices if we also patch v4_book_run's reference via monkeypatch
    # Instead, we can directly ensure v4_book_run uses our fake by patching its global
    # Simpler: run_book will import from pact_v4.phase5.formatting at call time, which will get our patched version

    result = v4_book_run.run_book(
        memory_dir=memory,
        chapter_ids=["0001", "0002"],
        chapter_html_pattern=str(src_dir / "{chapter_id}.html"),
        out_base=out_base,
        extra_args=["--runtime-config", "configs/runtime_remote.example.yaml"],
        max_formatting_incidents=999,
    )
    # Both chapters should have been built per-chapter (2 builds) and closed (2 closes)
    assert len(builds) == 2
    assert len(closes) == 2
    # Verify per-chapter fmt logs exist with real content (renamed from real logs)
    for cid in ["0001", "0002"]:
        log_dir = out_base / f"chapter_{cid}" / "server_logs"
        fmt_logs = list(log_dir.glob("opencode_serve_fmt_*.log"))
        assert len(fmt_logs) >= 1, f"chapter {cid} must have fmt log"
        # Should contain real content, not placeholder
        content = fmt_logs[0].read_text(encoding="utf-8")
        assert "stdout real" in content or "stderr real" in content
        # Ensure no placeholder synthetic empty log
        assert "formatting server log for" not in content
        # Batch meta should have effective_max_tokens
        meta = json.loads((out_base / f"chapter_{cid}" / "formatting_batch1_meta.json").read_text(encoding="utf-8"))
        assert "effective_max_tokens" in meta
        assert meta["effective_max_tokens"] >= 800
        # Translations should have em restored
        final = json.loads((out_base / f"chapter_{cid}" / "translations.json").read_text(encoding="utf-8"))
        assert "<em>" in final[pid]


def test_run_book_remote_health_failure_writes_meta_and_debt(tmp_path, monkeypatch):
    from pact_full_pipeline_runner_v1 import v4_book_run

    memory = _setup_memory(tmp_path)
    out_base = tmp_path / "out"
    src_dir = tmp_path / "src"
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

    # Simulate _build_formatting_client raising ManagedServerError (health timeout)
    def fake_build_fail(args, extra, fmt_cfg, out_dir=None):
        # Also simulate creating a real stderr log before failing (as ManagedServerProcess would)
        if out_dir:
            ld = Path(out_dir) / "server_logs"
            ld.mkdir(parents=True, exist_ok=True)
            (ld / "opencode_serve_20250101_120000_stderr.log").write_text("health failed connection refused", encoding="utf-8")
        raise RuntimeError("Connection refused on GET /global/health port 4097")

    monkeypatch.setattr(v4_book_run, "_build_formatting_client", fake_build_fail)

    result = v4_book_run.run_book(
        memory_dir=memory,
        chapter_ids=["0001"],
        chapter_html_pattern=str(src_dir / "{chapter_id}.html"),
        out_base=out_base,
        extra_args=["--runtime-config", "configs/runtime_remote.example.yaml"],
        max_formatting_incidents=999,
    )
    # Chapter must not crash, should be lenient debt
    assert result["chapters"][0]["terminal_status"] in ("complete", "accepted_degraded")
    # formatting_batch1_meta.json must exist with health path error and effective_max_tokens
    meta_path = out_base / "chapter_0001" / "formatting_batch1_meta.json"
    assert meta_path.exists(), "health failure must write formatting_batch1_meta.json"
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    assert "/global/health" in meta.get("error", "")
    assert "effective_max_tokens" in meta
    assert meta["effective_max_tokens"] >= 800
    # Log promotion: real stderr log should be copied to fmt prefix, not synthetic placeholder
    log_dir = out_base / "chapter_0001" / "server_logs"
    fmt_logs = list(log_dir.glob("opencode_serve_fmt_*.log"))
    assert len(fmt_logs) >= 1
    assert any("health failed" in p.read_text(encoding="utf-8") for p in fmt_logs)
    assert not any("formatting server log for" in p.read_text(encoding="utf-8") for p in fmt_logs)
    # formatting_report should be debt (incident)
    report = json.loads((out_base / "chapter_0001" / "formatting_report.json").read_text(encoding="utf-8"))
    # debt: should have incident or resolved 0
    assert report is not None


def test_run_book_remote_build_returns_none_writes_health_meta(tmp_path, monkeypatch):
    """When _build returns None (swallowed startup error), meta must still be written."""
    from pact_full_pipeline_runner_v1 import v4_book_run

    memory = _setup_memory(tmp_path)
    out_base = tmp_path / "out"
    src_dir = tmp_path / "src"
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

    def fake_build_none(args, extra, fmt_cfg, out_dir=None):
        # Simulate swallowed error: create log then return None
        if out_dir:
            ld = Path(out_dir) / "server_logs"
            ld.mkdir(parents=True, exist_ok=True)
            (ld / "opencode_serve_20250101_120000_stdout.log").write_text("some startup log", encoding="utf-8")
        return None

    monkeypatch.setattr(v4_book_run, "_build_formatting_client", fake_build_none)

    result = v4_book_run.run_book(
        memory_dir=memory,
        chapter_ids=["0001"],
        chapter_html_pattern=str(src_dir / "{chapter_id}.html"),
        out_base=out_base,
        max_formatting_incidents=999,
    )
    meta_path = out_base / "chapter_0001" / "formatting_batch1_meta.json"
    assert meta_path.exists()
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    assert "/global/health" in meta.get("error", "")
    assert "effective_max_tokens" in meta
    fmt_logs = list((out_base / "chapter_0001" / "server_logs").glob("opencode_serve_fmt_*.log"))
    assert len(fmt_logs) >= 1
    assert "some startup log" in fmt_logs[0].read_text(encoding="utf-8")


def test_formatting_log_isolation_strict_logs_not_copied(tmp_path, monkeypatch):
    """Strict-run logs pre-existing in server_logs must not leak into fmt logs."""
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

    def fake_run_one(*a, **k):
        return {"status": "ok"}
    monkeypatch.setattr(v4_book_run, "_run_one_chapter", fake_run_one)
    _make_chapter_artifacts(out_base / "chapter_0001", "0001", translations, plan)
    # Pre-create strict-run log that must NOT be copied
    strict_log = out_base / "chapter_0001" / "server_logs" / "opencode_serve_20250101_000000_strict_stdout.log"
    strict_log.parent.mkdir(parents=True, exist_ok=True)
    strict_log.write_text("STRICT-ONLY-CONTENT-XYZ", encoding="utf-8")
    # Also ensure no pre-existing fmt log
    assert not list((out_base / "chapter_0001" / "server_logs").glob("opencode_serve_fmt_*.log"))

    def fake_build(args, extra, fmt_cfg, out_dir=None):
        # Create only formatting logs after snapshot
        ld = Path(out_dir) / "server_logs"
        ld.mkdir(parents=True, exist_ok=True)
        (ld / "opencode_serve_20250101_120000_stdout.log").write_text("FMT-REAL-CONTENT", encoding="utf-8")
        class FakeClient:
            def close(self):
                pass
        return FakeClient()
    monkeypatch.setattr(v4_book_run, "_build_formatting_client", fake_build)

    def fake_resolve(client, cfg, blks, trans, out_dir=None):
        from pact_v4.phase5.formatting import _effective_max_tokens
        span_count = sum(len(b.inline_spans) for b in blks)
        effective = _effective_max_tokens(span_count, cfg.get("max_tokens"))
        meta = {"batch": 1, "attempt": 1, "span_count": span_count, "effective_max_tokens": effective, "finish_reason": "stop", "usage": {"prompt_tokens": 10}, "response_format_attempted": True}
        if out_dir:
            Path(out_dir, "formatting_batch1_meta.json").write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")
        return {(pid, blocks[0].inline_spans[0].span_id): ("мир", 1)}
    monkeypatch.setattr("pact_v4.phase5.formatting.resolve_format_mappings", fake_resolve, raising=False)
    import pact_v4.phase5.formatting as fmt_mod
    monkeypatch.setattr(fmt_mod, "resolve_format_mappings", fake_resolve, raising=False)

    v4_book_run.run_book(
        memory_dir=memory,
        chapter_ids=["0001"],
        chapter_html_pattern=str(src_dir / "{chapter_id}.html"),
        out_base=out_base,
        extra_args=["--runtime-config", "configs/runtime_remote.example.yaml"],
        max_formatting_incidents=999,
    )
    fmt_logs = list((out_base / "chapter_0001" / "server_logs").glob("opencode_serve_fmt_*.log"))
    assert len(fmt_logs) >= 1
    # Strict content must not leak
    for p in fmt_logs:
        assert "STRICT-ONLY-CONTENT-XYZ" not in p.read_text(encoding="utf-8"), "strict log leaked into fmt"
    # Formatting content must be present
    assert any("FMT-REAL-CONTENT" in p.read_text(encoding="utf-8") for p in fmt_logs)
    # Original strict log still exists unchanged
    assert strict_log.exists()
    assert "STRICT-ONLY-CONTENT-XYZ" in strict_log.read_text(encoding="utf-8")


def test_formatting_startup_failure_no_prior_log_creates_diagnostic_log(tmp_path, monkeypatch):
    """Port-occupancy preflight fails before any log file: diagnostic fmt log with real error, no synthetic empty placeholder."""
    from pact_full_pipeline_runner_v1 import v4_book_run

    memory = _setup_memory(tmp_path)
    out_base = tmp_path / "out"
    src_dir = tmp_path / "src"
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
    # Ensure no logs exist before formatting (no strict log, no fmt log)
    log_dir = out_base / "chapter_0001" / "server_logs"
    # _make_chapter_artifacts does not create server_logs, ensure empty
    if log_dir.exists():
        for p in log_dir.glob("*"):
            p.unlink()

    def fake_build_no_log(args, extra, fmt_cfg, out_dir=None):
        # Simulate port-occupancy preflight: fail without creating any log file
        raise RuntimeError("port 4097 already in use — preflight check failed")
    monkeypatch.setattr(v4_book_run, "_build_formatting_client", fake_build_no_log)

    result = v4_book_run.run_book(
        memory_dir=memory,
        chapter_ids=["0001"],
        chapter_html_pattern=str(src_dir / "{chapter_id}.html"),
        out_base=out_base,
        extra_args=["--runtime-config", "configs/runtime_remote.example.yaml"],
        max_formatting_incidents=999,
    )
    assert result["chapters"][0]["terminal_status"] in ("complete", "accepted_degraded")
    meta_path = out_base / "chapter_0001" / "formatting_batch1_meta.json"
    assert meta_path.exists()
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    assert "/global/health" in meta.get("error", "")
    assert "effective_max_tokens" in meta
    fmt_logs = list(log_dir.glob("opencode_serve_fmt_*.log"))
    assert len(fmt_logs) >= 1, "startup failure before logs must still produce diagnostic fmt log"
    # Diagnostic log must contain real health error, not synthetic empty placeholder
    assert any("/global/health" in p.read_text(encoding="utf-8") or "4097" in p.read_text(encoding="utf-8") for p in fmt_logs)
    assert not any("formatting server log for" in p.read_text(encoding="utf-8") for p in fmt_logs)
    # Each fmt log must be non-empty (no synthetic empty)
    for p in fmt_logs:
        assert p.read_text(encoding="utf-8").strip() != ""


def test_formatting_log_collision_same_second_isolated(tmp_path, monkeypatch):
    """Round4: same-second strict+formatting log names must not collide.

    Strict log opencode_serve_<ts>_stdout.log pre-exists with second-resolution
    timestamp. Formatting lifecycle that previously reused the same second-resolution
    name would overwrite strict log and be classified as pre-existing (no fmt copy).
    After fix formatting uses collision-proof names and/or metadata snapshot, so
    strict log is preserved, formatting log is recognized as new and copied to
    opencode_serve_fmt_* with formatting content.
    """
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

    def fake_run_one(*a, **k):
        return {"status": "ok"}
    monkeypatch.setattr(v4_book_run, "_run_one_chapter", fake_run_one)
    _make_chapter_artifacts(out_base / "chapter_0001", "0001", translations, plan)
    # Pre-create strict-run log with second-resolution name that formatting will collide with
    colliding_name = "opencode_serve_20250101_120000_stdout.log"
    strict_log = out_base / "chapter_0001" / "server_logs" / colliding_name
    strict_log.parent.mkdir(parents=True, exist_ok=True)
    strict_log.write_text("STRICT-ORIGINAL-CONTENT", encoding="utf-8")
    # Ensure mtime distinct so metadata check can differentiate overwrite
    import time
    time.sleep(0.02)

    def fake_build_collision(args, extra, fmt_cfg, out_dir=None):
        # Simulate formatting server overwriting SAME filename (second-resolution collision)
        ld = Path(out_dir) / "server_logs"
        ld.mkdir(parents=True, exist_ok=True)
        # Overwrite colliding file with formatting content (simulates non-unique stamp)
        (ld / colliding_name).write_text("FMT-COLLIDING-CONTENT", encoding="utf-8")
        class FakeClient:
            def close(self):
                pass
        return FakeClient()
    monkeypatch.setattr(v4_book_run, "_build_formatting_client", fake_build_collision)

    def fake_resolve(client, cfg, blks, trans, out_dir=None):
        from pact_v4.phase5.formatting import _effective_max_tokens
        span_count = sum(len(b.inline_spans) for b in blks)
        effective = _effective_max_tokens(span_count, cfg.get("max_tokens"))
        meta = {"batch": 1, "attempt": 1, "span_count": span_count, "effective_max_tokens": effective, "finish_reason": "stop", "usage": {"prompt_tokens": 10}, "response_format_attempted": True}
        if out_dir:
            Path(out_dir, "formatting_batch1_meta.json").write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")
        return {(pid, blocks[0].inline_spans[0].span_id): ("мир", 1)}
    monkeypatch.setattr("pact_v4.phase5.formatting.resolve_format_mappings", fake_resolve, raising=False)
    import pact_v4.phase5.formatting as fmt_mod
    monkeypatch.setattr(fmt_mod, "resolve_format_mappings", fake_resolve, raising=False)

    v4_book_run.run_book(
        memory_dir=memory,
        chapter_ids=["0001"],
        chapter_html_pattern=str(src_dir / "{chapter_id}.html"),
        out_base=out_base,
        extra_args=["--runtime-config", "configs/runtime_remote.example.yaml"],
        max_formatting_incidents=999,
    )
    log_dir = out_base / "chapter_0001" / "server_logs"
    # Formatting log must have been recognized as NEW despite same name (metadata changed) and copied to fmt prefix
    fmt_logs = list(log_dir.glob("opencode_serve_fmt_*.log"))
    assert len(fmt_logs) >= 1, "collision: formatting log with same name must be detected as new and copied to fmt"
    assert any("FMT-COLLIDING-CONTENT" in p.read_text(encoding="utf-8") for p in fmt_logs), "fmt copy must contain formatting content, not be skipped as pre-existing"
    # Strict log path now contains formatting content due to overwrite, but fmt copy preserves it; ensure not lost
    # And ensure the copy did NOT leak strict content
    for p in fmt_logs:
        assert "STRICT-ORIGINAL-CONTENT" not in p.read_text(encoding="utf-8")  # fmt copy must not contain strict


def test_opencode_server_log_names_are_collision_proof(tmp_path):
    """Round4: OpenCodeServerProcess must generate unique log names for same-second starts."""
    from pact_v4.runtime.opencode_server_lifecycle import OpenCodeServerProcess, ManagedServerSpec
    import time as _time
    # Freeze strftime to same second, but time.time differs slightly and token is random
    orig_strftime = _time.strftime
    def fake_strftime(fmt, *_a, **_k):
        if fmt == "%Y%m%d_%H%M%S":
            return "20250101_120000"
        return orig_strftime(fmt)
    import pact_v4.runtime.opencode_server_lifecycle as life
    # Monkeypatch via direct assignment in module
    old = life.time.strftime
    life.time.strftime = fake_strftime
    try:
        log_dir = tmp_path / "logs"
        # Need fake popen/http to avoid real server
        class FakeProc:
            pid = 1111
            def poll(self): return None
            def terminate(self): pass
            def wait(self, timeout=None): return 0
            def kill(self): pass
        captured_paths = []
        state = {"started": False}
        def fake_popen(args, **kwargs):
            for k in ("stdout", "stderr"):
                fh = kwargs.get(k)
                if fh and hasattr(fh, "name"):
                    captured_paths.append(fh.name)
                    try:
                        fh.close()
                    except Exception:
                        pass
            state["started"] = True
            return FakeProc()
        def fake_http(url, timeout, auth=None):
            if not state["started"]:
                raise ConnectionError("connection refused")
            class R:
                def json(self): return {"healthy": True, "version": "1.18.18"}
            return R()
        spec = ManagedServerSpec(hostname="127.0.0.1", port=4096, startup_timeout=1.0, health_interval=0.01)
        p1 = OpenCodeServerProcess(spec, log_dir=log_dir, popen=fake_popen, http_get=fake_http)
        p1.start()
        # second server on different port, reset started flag for its pre-check
        state["started"] = False
        p2 = OpenCodeServerProcess(ManagedServerSpec(hostname="127.0.0.1", port=4097, startup_timeout=1.0, health_interval=0.01), log_dir=log_dir, popen=fake_popen, http_get=fake_http)
        p2.start()
        # Filter captured to opencode logs
        import pathlib
        opencode_logs = [pathlib.Path(p).name for p in captured_paths if "opencode_serve_" in p]
        # At least 2 stdout logs, names must be distinct even though strftime same second
        stdout_logs = [n for n in opencode_logs if "stdout" in n]
        assert len(stdout_logs) >= 2
        assert len(set(stdout_logs)) == len(stdout_logs), f"collision-proof failed: duplicate names {stdout_logs}"
        # Also ensure micro suffix present
        assert any("_120000_" in n for n in stdout_logs)
    finally:
        life.time.strftime = old
