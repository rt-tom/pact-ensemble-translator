"""Offline contract tests for the unified v4 dispatcher (book-first).

No pipeline, model server, provider, filesystem artifact (beyond tmp), or
network side effects are exercised — entrypoints are patched.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from unittest.mock import patch

import pytest

from pact_v4.runtime.runtime_config import PreflightCheck, PreflightReport


def _ok_report(kind="local_llama"):
    return PreflightReport(
        ok=True,
        kind=kind,
        identity_hash="abc123",
        public_record={"kind": kind},
        model_bindings={},
        effective_options={},
        checks=(PreflightCheck(name="exe", ok=True, detail="present"),),
        errors=(),
    )


def _fail_report():
    return PreflightReport(
        ok=False,
        kind="local_llama",
        identity_hash="bad",
        public_record={},
        model_bindings={},
        effective_options={},
        checks=(PreflightCheck(name="exe", ok=False, detail="missing"),),
        errors=("exe missing",),
    )


# ---------------------------------------------------------------------------
# Help — offline, no artifacts, required terms
# ---------------------------------------------------------------------------

def test_top_level_help_contains_required_terms_and_no_artifacts(tmp_path, capsys):
    from pact_full_pipeline_runner_v1.v4_run import main
    out_root = tmp_path / "gate_bench_runs"
    os.environ["PACT_V4_OUT_ROOT"] = str(out_root)
    try:
        rc = main(["--help"])
        assert rc == 0
        captured = capsys.readouterr()
        text = captured.out
        for term in [
            "book",
            "chapter",
            "runtime profile",
            "D:\\pact\\gate_bench_runs",
            "profile defaults",
            "translator",
            "reviewer",
            "reasoning",
            "preflight",
            "JSON",
            "owner-started on RT",
            "--markup preserve",
            "source-pattern",
            "automatic output",
        ]:
            assert term.lower() in text.lower(), f"missing term {term!r} in top-level help"
        if out_root.exists():
            assert not list(out_root.iterdir())
    finally:
        os.environ.pop("PACT_V4_OUT_ROOT", None)


def test_book_help_and_chapter_help_offline(capsys):
    from pact_full_pipeline_runner_v1.v4_run import main
    rc = main(["book", "--help"])
    assert rc == 0
    text = capsys.readouterr().out
    assert "--chapters" in text
    assert "--runtime-config" in text
    assert "--markup preserve" in text.lower()
    # OpenSpec mode-help contract: book help must expose forwarded operational groups
    low = text.lower()
    assert "topology" in low and "resume" in low, "book --help must mention topology/resume"
    assert "audit" in low and "formatting" in low, "book --help must mention audit/formatting"
    assert "whole-chapter" in low or "whole_chapter" in low, "book --help must mention whole-chapter"
    for term in ["--managed-server", "--providers-config", "--run-audit", "--skip-audit", "--whole-chapter", "--stop-after-generation", "--lazy-balanced"]:
        assert term in text, f"book --help must include forwarded option {term}"
    rc = main(["chapter", "--help"])
    assert rc == 0
    text = capsys.readouterr().out
    assert "--chapter-id" in text


def test_readme_documents_valid_alias_and_no_invalid_example():
    """README must use opencode-go/musefree (provider key opencode-go) and not opencode/musefree."""
    readme = Path("README.md").read_text(encoding="utf-8")
    assert "opencode-go/musefree" in readme, "README must document valid alias opencode-go/musefree"
    # Invalid bare opencode/musefree example should not appear as translator example
    # Use strict check: the exact string '--translator opencode/musefree' must be absent
    assert "--translator opencode/musefree" not in readme
    # Providers registry defines opencode-go/musefree correctly
    providers = Path("configs/providers.yaml").read_text(encoding="utf-8")
    assert "opencode-go:" in providers
    # Ensure help example alias is valid via dispatcher preflight path (no pipeline)
    from pact_full_pipeline_runner_v1.v4_run import main
    import tempfile, os
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        os.environ["PACT_V4_OUT_ROOT"] = str(td / "out")
        try:
            # Use valid alias from README – should not fail on alias resolution
            from unittest.mock import patch
            from pact_v4.runtime.runtime_config import PreflightReport, PreflightCheck
            ok = PreflightReport(ok=True, kind="opencode_server", identity_hash="x", public_record={}, model_bindings={}, effective_options={}, checks=(PreflightCheck(name="ok", ok=True, detail=""),), errors=())
            with patch("pact_v4.runtime.runtime_config.run_runtime_preflight", return_value=ok):
                with patch("pact_full_pipeline_runner_v1.v4_book_run.main", return_value=0):
                    rc = main(["book", "--chapters", "27-32", "--runtime-config", "configs/runtime_remote.example.yaml", "--translator", "opencode-go/musefree", "--preflight"])
                    assert rc == 0
        finally:
            os.environ.pop("PACT_V4_OUT_ROOT", None)


# ---------------------------------------------------------------------------
# Range validation
# ---------------------------------------------------------------------------

def test_book_invalid_ranges_exit_before_pipeline(tmp_path):
    from pact_full_pipeline_runner_v1.v4_run import main
    for bad in ["32-27", "abc-32", "27-", "0", "0-5", "27-5000"]:
        with pytest.raises(SystemExit) as exc:
            main(["book", "--chapters", bad, "--runtime-config", "configs/runtime_local.example.yaml"])
        assert exc.value.code == 2
    # empty string case via missing value — argparse will error but we treat as invalid
    with pytest.raises(SystemExit):
        main(["book", "--chapters", "", "--runtime-config", "configs/runtime_local.example.yaml"])


def test_book_range_expansion_and_label(tmp_path, monkeypatch):
    """Valid range expands to zero-padded IDs and output dir uses descriptor label."""
    from pact_full_pipeline_runner_v1 import v4_run
    out_root = tmp_path / "gate_bench"
    monkeypatch.setenv("PACT_V4_OUT_ROOT", str(out_root))
    fake_cfg_path = tmp_path / "profile.yaml"
    # Minimal local profile that would pass if exe exists; we mock preflight anyway
    # Use real example but patch preflight to ok
    fake_cfg_path.write_text(Path("configs/runtime_local.example.yaml").read_text(), encoding="utf-8")

    ok = _ok_report(kind="local_llama")
    with patch("pact_v4.runtime.runtime_config.run_runtime_preflight", return_value=ok):
        # Patch derive to return local explicitly
        with patch.object(v4_run, "_derive_label", return_value="local"):
            # Patch book_main to capture delegated argv
            with patch("pact_full_pipeline_runner_v1.v4_book_run.main") as mock_book:
                mock_book.return_value = 0
                # Need chapter range 27-32 -> expect 6 IDs
                rc = v4_run.main([
                    "book", "--chapters", "27-32",
                    "--runtime-config", str(fake_cfg_path),
                    "--memory-dir", str(tmp_path / "mem"),
                    "--chapter-html-pattern", str(tmp_path / "{chapter_id}.html"),
                ])
                assert rc == 0
                assert mock_book.called
                delegated = mock_book.call_args[0][0]
                # Check expanded chapters present
                for cid in ["0027", "0028", "0029", "0030", "0031", "0032"]:
                    assert cid in delegated
                # --chapter-html-pattern forwarded
                assert "--chapter-html-pattern" in delegated
                # --out-base auto-created under out_root with correct prefix
                assert "--out-base" in delegated
                idx = delegated.index("--out-base")
                out_base = Path(delegated[idx + 1])
                assert out_base.parent == out_root
                assert out_base.name.startswith("book_0027-0032_local_")
                assert out_base.exists()


def test_book_remote_label(tmp_path, monkeypatch):
    from pact_full_pipeline_runner_v1 import v4_run
    out_root = tmp_path / "gate_bench"
    monkeypatch.setenv("PACT_V4_OUT_ROOT", str(out_root))
    fake_cfg = tmp_path / "remote.yaml"
    fake_cfg.write_text(Path("configs/runtime_remote.example.yaml").read_text(), encoding="utf-8")
    ok = _ok_report(kind="opencode_server")
    with patch("pact_v4.runtime.runtime_config.run_runtime_preflight", return_value=ok):
        with patch("pact_full_pipeline_runner_v1.v4_book_run.main") as mock_book:
            mock_book.return_value = 0
            rc = v4_run.main([
                "book", "--chapters", "27-32",
                "--runtime-config", str(fake_cfg),
            ])
            assert rc == 0
            delegated = mock_book.call_args[0][0]
            idx = delegated.index("--out-base")
            out_base = Path(delegated[idx + 1])
            assert "remote" in out_base.name
            assert out_base.name.startswith("book_0027-0032_remote_")


# ---------------------------------------------------------------------------
# Markup preserve validation
# ---------------------------------------------------------------------------

def test_book_markup_only_preserve(tmp_path):
    from pact_full_pipeline_runner_v1.v4_run import main
    fake_cfg = tmp_path / "profile.yaml"
    fake_cfg.write_text(Path("configs/runtime_local.example.yaml").read_text(), encoding="utf-8")
    with pytest.raises(SystemExit) as exc:
        main(["book", "--chapters", "27-32", "--runtime-config", str(fake_cfg), "--markup", "strip"])
    assert exc.value.code == 2
    with pytest.raises(SystemExit) as exc:
        main(["chapter", "--chapter-id", "0001", "--chapter-html", "a.html", "--memory-dir", "m", "--out-dir", "o", "--markup", "strip"])
    assert exc.value.code == 2


def test_book_markup_preserve_passes(tmp_path, monkeypatch):
    """--markup preserve is validated but NOT forwarded (strict entrypoint has no --markup)."""
    from pact_full_pipeline_runner_v1 import v4_run
    out_root = tmp_path / "gate"
    monkeypatch.setenv("PACT_V4_OUT_ROOT", str(out_root))
    fake_cfg = tmp_path / "p.yaml"
    fake_cfg.write_text(Path("configs/runtime_local.example.yaml").read_text(), encoding="utf-8")
    ok = _ok_report()
    with patch("pact_v4.runtime.runtime_config.run_runtime_preflight", return_value=ok):
        with patch("pact_full_pipeline_runner_v1.v4_book_run.main") as mock_book:
            mock_book.return_value = 0
            rc = v4_run.main(["book", "--chapters", "27-32", "--runtime-config", str(fake_cfg), "--markup", "preserve"])
            assert rc == 0
            delegated = mock_book.call_args[0][0]
            # Guard consumed — must not be forwarded as unsupported syntax
            assert "--markup" not in delegated


def test_book_markup_preserve_not_forwarded_book(tmp_path, monkeypatch):
    """Regression: book preserve guard must not produce unrecognized arguments in strict/book path."""
    from pact_full_pipeline_runner_v1 import v4_run
    out_root = tmp_path / "gate"
    monkeypatch.setenv("PACT_V4_OUT_ROOT", str(out_root))
    fake_cfg = tmp_path / "p.yaml"
    fake_cfg.write_text(Path("configs/runtime_local.example.yaml").read_text(), encoding="utf-8")
    ok = _ok_report()
    with patch("pact_v4.runtime.runtime_config.run_runtime_preflight", return_value=ok):
        with patch("pact_full_pipeline_runner_v1.v4_book_run.main") as mock_book:
            mock_book.return_value = 0
            # Also ensures strict would not receive --markup if book delegates further
            rc = v4_run.main(["book", "--chapters", "27-32", "--runtime-config", str(fake_cfg), "--markup", "preserve", "--whole-chapter"])
            assert rc == 0
            delegated = mock_book.call_args[0][0]
            assert "--markup" not in delegated


def test_chapter_markup_preserve_not_forwarded(tmp_path):
    """Regression: chapter preserve guard must not be forwarded to strict (no --markup flag there)."""
    from pact_full_pipeline_runner_v1.v4_run import main
    fake_cfg = tmp_path / "p.yaml"
    fake_cfg.write_text(Path("configs/runtime_remote.example.yaml").read_text(), encoding="utf-8")
    ok = _ok_report(kind="opencode_server")
    with patch("pact_v4.runtime.runtime_config.run_runtime_preflight", return_value=ok):
        with patch("pact_full_pipeline_runner_v1.v4_phase12_strict_run.main") as mock_strict:
            mock_strict.return_value = 0
            rc = main(["chapter", "--chapter-id", "0001", "--chapter-html", "a.html", "--memory-dir", "m", "--out-dir", "o", "--runtime-config", str(fake_cfg), "--markup", "preserve"])
            assert rc == 0
            delegated = mock_strict.call_args[0][0]
            assert "--markup" not in delegated


# ---------------------------------------------------------------------------
# Preflight — check-only is side-effect free
# ---------------------------------------------------------------------------

def test_preflight_check_only_no_output_dir(tmp_path, capsys):
    from pact_full_pipeline_runner_v1.v4_run import main
    out_root = tmp_path / "gate"
    os.environ["PACT_V4_OUT_ROOT"] = str(out_root)
    fake_cfg = tmp_path / "p.yaml"
    fake_cfg.write_text(Path("configs/runtime_remote.example.yaml").read_text(), encoding="utf-8")
    fail = _fail_report()
    with patch("pact_v4.runtime.runtime_config.run_runtime_preflight", return_value=fail):
        # Also patch book main to ensure it would not be called
        with patch("pact_full_pipeline_runner_v1.v4_book_run.main") as mock_book:
            rc = main(["book", "--chapters", "27-32", "--runtime-config", str(fake_cfg), "--preflight"])
            assert rc == 1
            assert not mock_book.called
            assert not list(out_root.iterdir()) if out_root.exists() else True
            # Should have printed report
            out = capsys.readouterr().out
            assert "Runtime preflight" in out
    os.environ.pop("PACT_V4_OUT_ROOT", None)


def test_preflight_json_mode(tmp_path, capsys):
    from pact_full_pipeline_runner_v1.v4_run import main
    out_root = tmp_path / "gate"
    os.environ["PACT_V4_OUT_ROOT"] = str(out_root)
    fake_cfg = tmp_path / "p.yaml"
    fake_cfg.write_text(Path("configs/runtime_remote.example.yaml").read_text(), encoding="utf-8")
    ok = _ok_report(kind="opencode_server")
    with patch("pact_v4.runtime.runtime_config.run_runtime_preflight", return_value=ok):
        with patch("pact_full_pipeline_runner_v1.v4_book_run.main") as mock_book:
            rc = main(["book", "--chapters", "27-32", "--runtime-config", str(fake_cfg), "--preflight", "--json"])
            assert rc == 0
            assert not mock_book.called
            out = capsys.readouterr().out
            data = json.loads(out)
            assert data["ok"] is True
            assert "identity_hash" in data
    # alias --preflight-json
    with patch("pact_v4.runtime.runtime_config.run_runtime_preflight", return_value=ok):
        with patch("pact_full_pipeline_runner_v1.v4_book_run.main") as mock_book:
            rc = main(["book", "--chapters", "27-32", "--runtime-config", str(fake_cfg), "--preflight-json"])
            assert rc == 0
            assert not mock_book.called
            out = capsys.readouterr().out
            data = json.loads(out)
            assert data["ok"] is True
    os.environ.pop("PACT_V4_OUT_ROOT", None)


def test_default_preflight_blocks_output_and_pipeline(tmp_path):
    from pact_full_pipeline_runner_v1.v4_run import main
    out_root = tmp_path / "gate"
    os.environ["PACT_V4_OUT_ROOT"] = str(out_root)
    fake_cfg = tmp_path / "p.yaml"
    fake_cfg.write_text(Path("configs/runtime_local.example.yaml").read_text(), encoding="utf-8")
    fail = _fail_report()
    with patch("pact_v4.runtime.runtime_config.run_runtime_preflight", return_value=fail):
        with patch("pact_full_pipeline_runner_v1.v4_book_run.main") as mock_book:
            with pytest.raises(SystemExit) as exc:
                main(["book", "--chapters", "27-32", "--runtime-config", str(fake_cfg)])
            assert exc.value.code == 3
            assert not mock_book.called
            assert not list(out_root.iterdir()) if out_root.exists() else True
    os.environ.pop("PACT_V4_OUT_ROOT", None)


# ---------------------------------------------------------------------------
# Profile defaults vs overrides
# ---------------------------------------------------------------------------

def test_profile_defaults_without_overrides_uses_profile(tmp_path, monkeypatch):
    from pact_full_pipeline_runner_v1 import v4_run
    out_root = tmp_path / "gate"
    monkeypatch.setenv("PACT_V4_OUT_ROOT", str(out_root))
    fake_cfg = tmp_path / "remote.yaml"
    fake_cfg.write_text(Path("configs/runtime_remote.example.yaml").read_text(), encoding="utf-8")
    ok = _ok_report(kind="opencode_server")
    # Track what load returns and what apply does
    with patch("pact_v4.runtime.runtime_config.run_runtime_preflight", return_value=ok):
        with patch("pact_full_pipeline_runner_v1.v4_book_run.main") as mock_book:
            mock_book.return_value = 0
            rc = v4_run.main(["book", "--chapters", "27-32", "--runtime-config", str(fake_cfg)])
            assert rc == 0
            delegated = mock_book.call_args[0][0]
            # No translator/reviewer/reasoning overrides forwarded when not supplied
            assert "--translator" not in delegated
            assert "--reviewer" not in delegated
            # Should have --runtime-config
            assert "--runtime-config" in delegated


def test_explicit_overrides_forwarded_and_affect_label(tmp_path, monkeypatch):
    from pact_full_pipeline_runner_v1 import v4_run
    out_root = tmp_path / "gate"
    monkeypatch.setenv("PACT_V4_OUT_ROOT", str(out_root))
    fake_cfg = tmp_path / "remote.yaml"
    fake_cfg.write_text(Path("configs/runtime_remote.example.yaml").read_text(), encoding="utf-8")
    ok = _ok_report(kind="opencode_server")
    with patch("pact_v4.runtime.runtime_config.run_runtime_preflight", return_value=ok):
        with patch("pact_full_pipeline_runner_v1.v4_book_run.main") as mock_book:
            mock_book.return_value = 0
            rc = v4_run.main([
                "book", "--chapters", "27-32",
                "--runtime-config", str(fake_cfg),
                "--translator", "opencode-go/musefree",
                "--reasoning", "2",
            ])
            assert rc == 0
            delegated = mock_book.call_args[0][0]
            assert "--translator" in delegated
            assert delegated[delegated.index("--translator")+1] == "opencode-go/musefree"
            assert "--reasoning" in delegated
            assert delegated[delegated.index("--reasoning")+1] == "2"


def test_preflight_uses_effective_providers_config(tmp_path, monkeypatch, capsys):
    """Regression: preflight must resolve the effective --providers-config registry.

    Default registry has alias deepseek4flash but not custom_alias. A custom registry
    defines custom_alias. Dispatcher preflight should validate against the effective
    file — not always the default — so --providers-config custom + --translator custom
    passes preflight, while using default would fail.
    """
    from pact_full_pipeline_runner_v1.v4_run import main
    out_root = tmp_path / "gate"
    monkeypatch.setenv("PACT_V4_OUT_ROOT", str(out_root))
    # Remote profile
    fake_cfg = tmp_path / "remote.yaml"
    fake_cfg.write_text(Path("configs/runtime_remote.example.yaml").read_text(), encoding="utf-8")
    # Custom providers with unique alias not in default
    custom_prov = tmp_path / "custom_providers.yaml"
    custom_prov.write_text(
        """
providers:
  opencode-go:
    kind: opencode_server
    models:
      customalias:
        ref: opencode-go/custom-model-1
        reasoning_contract:
          variants: [low, medium, high]
""",
        encoding="utf-8",
    )
    # Without custom config, translator customalias should be rejected (preflight would fail)
    # But we test with custom config — preflight should succeed and not fall back to default
    # Mock run_runtime_preflight to verify it was called with cfg that already has custom binding.
    # Instead we observe end-to-end: dispatcher should not error about unknown provider when
    # providers-config is effective; if it incorrectly used default, it would exit 2.
    ok = _ok_report(kind="opencode_server")
    # Patch preflight to ok, but also patch apply_provider_flags indirectly via dispatcher
    # To verify effective registry usage, we assert that calling with custom succeeds
    with patch("pact_v4.runtime.runtime_config.run_runtime_preflight", return_value=ok) as mock_preflight:
        with patch("pact_full_pipeline_runner_v1.v4_book_run.main") as mock_book:
            mock_book.return_value = 0
            rc = main([
                "book", "--chapters", "27-32",
                "--runtime-config", str(fake_cfg),
                "--providers-config", str(custom_prov),
                "--translator", "opencode-go/customalias",
            ])
            assert rc == 0
            # Ensure preflight was invoked with cfg whose bindings contain custom model
            assert mock_preflight.called
            called_cfg = mock_preflight.call_args[0][0]
            # The custom alias should be bound to generator role
            bindings = called_cfg.build_descriptor().model_bindings
            assert bindings.get("generator") == "opencode-go/custom-model-1"
            # Delegated must forward providers-config and translator for strict path
            delegated = mock_book.call_args[0][0]
            assert "--providers-config" in delegated
            assert "--translator" in delegated
    # Also check that chapter mode propagates the same effective registry to preflight
    ok2 = _ok_report(kind="opencode_server")
    with patch("pact_v4.runtime.runtime_config.run_runtime_preflight", return_value=ok2) as mock_preflight:
        with patch("pact_full_pipeline_runner_v1.v4_phase12_strict_run.main") as mock_strict:
            mock_strict.return_value = 0
            rc = main([
                "chapter", "--chapter-id", "0001", "--chapter-html", "a.html",
                "--memory-dir", "m", "--out-dir", "o",
                "--runtime-config", str(fake_cfg),
                "--providers-config", str(custom_prov),
                "--translator", "opencode-go/customalias",
                "--preflight",
            ])
            assert rc == 0
            assert mock_preflight.called
            called_cfg = mock_preflight.call_args[0][0]
            assert called_cfg.build_descriptor().model_bindings.get("generator") == "opencode-go/custom-model-1"
            out = capsys.readouterr().out
            assert "Runtime preflight" in out


# ---------------------------------------------------------------------------
# Chapter mode — no-config compatibility and forwarding
# ---------------------------------------------------------------------------

def test_chapter_no_config_forwarding(tmp_path):
    from pact_full_pipeline_runner_v1.v4_run import main
    with patch("pact_full_pipeline_runner_v1.v4_phase12_strict_run.main") as mock_strict:
        mock_strict.return_value = 0
        rc = main(["chapter", "--chapter-id", "0001", "--chapter-html", "a.html", "--memory-dir", "m", "--out-dir", "o"])
        assert rc == 0
        assert mock_strict.called
        delegated = mock_strict.call_args[0][0]
        assert "--chapter-id" in delegated


def test_chapter_with_profile_preflight_and_forwarding(tmp_path):
    from pact_full_pipeline_runner_v1.v4_run import main
    fake_cfg = tmp_path / "p.yaml"
    fake_cfg.write_text(Path("configs/runtime_remote.example.yaml").read_text(), encoding="utf-8")
    ok = _ok_report(kind="opencode_server")
    with patch("pact_v4.runtime.runtime_config.run_runtime_preflight", return_value=ok):
        with patch("pact_full_pipeline_runner_v1.v4_phase12_strict_run.main") as mock_strict:
            mock_strict.return_value = 0
            rc = main([
                "chapter", "--chapter-id", "0001", "--chapter-html", "a.html",
                "--memory-dir", "m", "--out-dir", "o",
                "--runtime-config", str(fake_cfg),
                "--translator", "opencode-go/musefree",
            ])
            assert rc == 0
            delegated = mock_strict.call_args[0][0]
            assert "--runtime-config" in delegated
            assert "--translator" in delegated


# ---------------------------------------------------------------------------
# Managed-server override must be applied before preflight/descriptor (BLOCKER)
# ---------------------------------------------------------------------------

def test_book_managed_server_local_fails_before_artifacts(tmp_path, monkeypatch):
    """Local profile with --managed-server must fail via dispatcher preflight before output creation."""
    from pact_full_pipeline_runner_v1.v4_run import main
    out_root = tmp_path / "gate"
    monkeypatch.setenv("PACT_V4_OUT_ROOT", str(out_root))
    fake_cfg = tmp_path / "local.yaml"
    fake_cfg.write_text(Path("configs/runtime_local.example.yaml").read_text(), encoding="utf-8")
    # Even if runtime preflight would report ok, force_managed should raise before it
    with patch("pact_full_pipeline_runner_v1.v4_book_run.main") as mock_book:
        with pytest.raises(SystemExit) as exc:
            main(["book", "--chapters", "27-32", "--runtime-config", str(fake_cfg), "--managed-server"])
        # Dispatcher should fail with validation error, not create output
        assert exc.value.code == 2
        assert not mock_book.called
        assert not list(out_root.iterdir()) if out_root.exists() else True


def test_book_managed_server_local_check_only_fails_without_artifacts(tmp_path, monkeypatch, capsys):
    """Check-only --preflight with --managed-server on local must report error and not create dir."""
    from pact_full_pipeline_runner_v1.v4_run import main
    out_root = tmp_path / "gate"
    monkeypatch.setenv("PACT_V4_OUT_ROOT", str(out_root))
    fake_cfg = tmp_path / "local.yaml"
    fake_cfg.write_text(Path("configs/runtime_local.example.yaml").read_text(), encoding="utf-8")
    with patch("pact_full_pipeline_runner_v1.v4_book_run.main") as mock_book:
        with pytest.raises(SystemExit) as exc:
            main(["book", "--chapters", "27-32", "--runtime-config", str(fake_cfg), "--managed-server", "--preflight"])
        assert exc.value.code == 2
        assert not mock_book.called
        assert not list(out_root.iterdir()) if out_root.exists() else True


def test_chapter_managed_server_local_fails_before_strict(tmp_path):
    """Chapter local profile with --managed-server must fail before delegating to strict."""
    from pact_full_pipeline_runner_v1.v4_run import main
    fake_cfg = tmp_path / "local.yaml"
    fake_cfg.write_text(Path("configs/runtime_local.example.yaml").read_text(), encoding="utf-8")
    with patch("pact_full_pipeline_runner_v1.v4_phase12_strict_run.main") as mock_strict:
        with pytest.raises(SystemExit) as exc:
            main(["chapter", "--chapter-id", "0001", "--chapter-html", "a.html", "--memory-dir", "m", "--out-dir", "o", "--runtime-config", str(fake_cfg), "--managed-server"])
        assert exc.value.code == 2
        assert not mock_strict.called


def test_book_managed_server_remote_uses_effective_descriptor_and_preflight(tmp_path, monkeypatch):
    """Remote profile with --managed-server must apply force_managed before preflight/descriptor derivation."""
    from pact_full_pipeline_runner_v1 import v4_run
    out_root = tmp_path / "gate"
    monkeypatch.setenv("PACT_V4_OUT_ROOT", str(out_root))
    fake_cfg = tmp_path / "remote.yaml"
    fake_cfg.write_text(Path("configs/runtime_remote.example.yaml").read_text(), encoding="utf-8")
    ok = _ok_report(kind="opencode_server")
    with patch("pact_v4.runtime.runtime_config.run_runtime_preflight", return_value=ok) as mock_preflight:
        with patch("pact_full_pipeline_runner_v1.v4_book_run.main") as mock_book:
            mock_book.return_value = 0
            rc = v4_run.main(["book", "--chapters", "27-32", "--runtime-config", str(fake_cfg), "--managed-server"])
            assert rc == 0
            assert mock_preflight.called
            called_cfg = mock_preflight.call_args[0][0]
            # Effective config must be managed
            assert getattr(called_cfg, "server_mode", None) == "managed"
            delegated = mock_book.call_args[0][0]
            assert "--managed-server" in delegated


# ---------------------------------------------------------------------------
# Collision-safe automatic output naming (MAJOR)
# ---------------------------------------------------------------------------

def test_book_auto_output_collision_safe(tmp_path, monkeypatch):
    """Same-second runs must allocate distinct output directories (no silent reuse)."""
    from pact_full_pipeline_runner_v1 import v4_run
    out_root = tmp_path / "gate"
    monkeypatch.setenv("PACT_V4_OUT_ROOT", str(out_root))
    fake_cfg = tmp_path / "remote.yaml"
    fake_cfg.write_text(Path("configs/runtime_remote.example.yaml").read_text(), encoding="utf-8")
    ok = _ok_report(kind="opencode_server")
    # Force timestamp collision by patching _timestamp to constant
    with patch.object(v4_run, "_timestamp", return_value="20260101_120000_000000"):
        with patch("pact_v4.runtime.runtime_config.run_runtime_preflight", return_value=ok):
            with patch("pact_full_pipeline_runner_v1.v4_book_run.main") as mock_book:
                mock_book.return_value = 0
                rc1 = v4_run.main(["book", "--chapters", "27-32", "--runtime-config", str(fake_cfg)])
                assert rc1 == 0
                dir1 = Path(mock_book.call_args[0][0][mock_book.call_args[0][0].index("--out-base") + 1])
                rc2 = v4_run.main(["book", "--chapters", "27-32", "--runtime-config", str(fake_cfg)])
                assert rc2 == 0
                dir2 = Path(mock_book.call_args[0][0][mock_book.call_args[0][0].index("--out-base") + 1])
                assert dir1 != dir2
                assert dir1.exists() and dir2.exists()
                # Both share prefix but second has suffix for uniqueness
                assert dir1.name.startswith("book_0027-0032_remote_20260101_120000")
                assert dir2.name.startswith("book_0027-0032_remote_20260101_120000")


# ---------------------------------------------------------------------------
# Bare --json must be rejected unless paired with --preflight (HIGH)
# ---------------------------------------------------------------------------

def test_book_bare_json_rejected_before_pipeline(tmp_path, monkeypatch):
    """Bare --json without --preflight must be rejected and not start pipeline or create output."""
    from pact_full_pipeline_runner_v1.v4_run import main
    out_root = tmp_path / "gate"
    monkeypatch.setenv("PACT_V4_OUT_ROOT", str(out_root))
    fake_cfg = tmp_path / "p.yaml"
    fake_cfg.write_text(Path("configs/runtime_remote.example.yaml").read_text(), encoding="utf-8")
    with patch("pact_full_pipeline_runner_v1.v4_book_run.main") as mock_book:
        with pytest.raises(SystemExit) as exc:
            main(["book", "--chapters", "27-32", "--runtime-config", str(fake_cfg), "--json"])
        assert exc.value.code == 2
        assert not mock_book.called
        assert not list(out_root.iterdir()) if out_root.exists() else True
    # Chapter bare --json also rejected
    with patch("pact_full_pipeline_runner_v1.v4_phase12_strict_run.main") as mock_strict:
        with pytest.raises(SystemExit) as exc:
            main(["chapter", "--chapter-id", "0001", "--chapter-html", "a.html", "--memory-dir", "m", "--out-dir", "o", "--json"])
        assert exc.value.code == 2
        assert not mock_strict.called


def test_book_valid_preflight_json_aliases_pass(tmp_path, monkeypatch, capsys):
    """Valid check-only aliases --preflight --json and --preflight-json must succeed and not be rejected."""
    from pact_full_pipeline_runner_v1.v4_run import main
    out_root = tmp_path / "gate"
    monkeypatch.setenv("PACT_V4_OUT_ROOT", str(out_root))
    fake_cfg = tmp_path / "p.yaml"
    fake_cfg.write_text(Path("configs/runtime_remote.example.yaml").read_text(), encoding="utf-8")
    ok = _ok_report(kind="opencode_server")
    with patch("pact_v4.runtime.runtime_config.run_runtime_preflight", return_value=ok):
        with patch("pact_full_pipeline_runner_v1.v4_book_run.main") as mock_book:
            # --preflight --json
            rc = main(["book", "--chapters", "27-32", "--runtime-config", str(fake_cfg), "--preflight", "--json"])
            assert rc == 0
            assert not mock_book.called
            data = json.loads(capsys.readouterr().out)
            assert data["ok"] is True
            # --preflight-json
            rc = main(["book", "--chapters", "27-32", "--runtime-config", str(fake_cfg), "--preflight-json"])
            assert rc == 0
            data = json.loads(capsys.readouterr().out)
            assert data["ok"] is True
            # --preflight-json --json (redundant) also valid
            rc = main(["book", "--chapters", "27-32", "--runtime-config", str(fake_cfg), "--preflight-json", "--json"])
            assert rc == 0
    # Chapter valid aliases
    with patch("pact_v4.runtime.runtime_config.run_runtime_preflight", return_value=ok):
        with patch("pact_full_pipeline_runner_v1.v4_phase12_strict_run.main") as mock_strict:
            rc = main(["chapter", "--chapter-id", "0001", "--chapter-html", "a.html", "--memory-dir", "m", "--out-dir", "o", "--runtime-config", str(fake_cfg), "--preflight", "--json"])
            assert rc == 0
            assert not mock_strict.called


# ---------------------------------------------------------------------------
# Chapter no-config: reasoning/translator/reviewer forwarding (HIGH)
# ---------------------------------------------------------------------------

def test_chapter_no_config_forwards_reasoning(tmp_path):
    """No-config chapter must forward --reasoning unchanged and not drop it."""
    from pact_full_pipeline_runner_v1.v4_run import main
    with patch("pact_full_pipeline_runner_v1.v4_phase12_strict_run.main") as mock_strict:
        mock_strict.return_value = 0
        rc = main([
            "chapter", "--chapter-id", "0001", "--chapter-html", "a.html",
            "--memory-dir", "m", "--out-dir", "o", "--reasoning", "2",
        ])
        assert rc == 0
        delegated = mock_strict.call_args[0][0]
        assert "--reasoning" in delegated
        assert delegated[delegated.index("--reasoning") + 1] == "2"


def test_chapter_no_config_forwards_translator_reviewer(tmp_path):
    """No-config chapter must forward translator/reviewer so strict fails closed on invalid overrides."""
    from pact_full_pipeline_runner_v1.v4_run import main
    # No-config with translator should be forwarded and fail closed in strict
    with patch("pact_full_pipeline_runner_v1.v4_phase12_strict_run.main") as mock_strict:
        def _strict_fail(argv=None):
            # Simulate strict validation: translator/reviewer require --runtime-config
            if argv and ("--translator" in argv or "--reviewer" in argv):
                # strict checks this before any other work
                has_runtime = "--runtime-config" in argv
                if not has_runtime:
                    raise ValueError("--translator/--reviewer require --runtime-config")
            return 0
        mock_strict.side_effect = _strict_fail
        with pytest.raises(ValueError, match="--translator/--reviewer require"):
            main([
                "chapter", "--chapter-id", "0001", "--chapter-html", "a.html",
                "--memory-dir", "m", "--out-dir", "o",
                "--translator", "opencode-go/musefree",
            ])
        # Verify forwarding happened (call was attempted)
        assert mock_strict.called
        delegated = mock_strict.call_args[0][0]
        assert "--translator" in delegated
        assert "opencode-go/musefree" in delegated
    # Reviewer similarly
    with patch("pact_full_pipeline_runner_v1.v4_phase12_strict_run.main") as mock_strict:
        mock_strict.side_effect = _strict_fail
        with pytest.raises(ValueError):
            main([
                "chapter", "--chapter-id", "0001", "--chapter-html", "a.html",
                "--memory-dir", "m", "--out-dir", "o",
                "--reviewer", "openai/luna",
            ])
        assert "--reviewer" in mock_strict.call_args[0][0]


def test_chapter_no_config_invalid_translator_fails_closed(tmp_path):
    """Invalid provider alias in no-config mode must not be silently dropped."""
    from pact_full_pipeline_runner_v1.v4_run import main
    # Use real strict main but with invalid alias – it should error via dispatcher forwarding
    # Mock strict to raise on unknown alias to simulate fail-closed
    with patch("pact_full_pipeline_runner_v1.v4_phase12_strict_run.main") as mock_strict:
        mock_strict.side_effect = ValueError("providers registry: unknown alias")
        with pytest.raises(ValueError, match="unknown alias"):
            main([
                "chapter", "--chapter-id", "0001", "--chapter-html", "a.html",
                "--memory-dir", "m", "--out-dir", "o",
                "--translator", "opencode-go/unknown_alias_xyz",
            ])
        # Ensure the invalid alias was actually forwarded, not dropped
        assert "unknown_alias_xyz" in str(mock_strict.call_args[0][0])
