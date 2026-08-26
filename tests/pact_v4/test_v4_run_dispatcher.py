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


# ---------------------------------------------------------------------------
# Host-aware layout, source discovery, media defaults, media verdict, preflight readiness (P1)
# ---------------------------------------------------------------------------

def test_validate_layout_forbidden_root_precedence(tmp_path):
    """_validate_layout must not always reject; forbidden check uses resolve-if-exists else absolute."""
    from pact_full_pipeline_runner_v1.v4_run import _validate_layout
    # Use tmp paths that do not exist under forbidden — should NOT raise
    layout = {"source": tmp_path / "src", "state": tmp_path / "state", "output": tmp_path / "out"}
    (layout["source"]).mkdir(); (layout["state"]).mkdir(); (layout["output"]).mkdir()
    _validate_layout(layout)  # should not raise
    # Forbidden exact should raise
    layout2 = {"source": tmp_path / "src2", "state": Path("/home/rt/pact_runs/books/1"), "output": tmp_path / "out2"}
    (layout2["source"]).mkdir(parents=True, exist_ok=True); (layout2["output"]).mkdir(parents=True, exist_ok=True)
    with pytest.raises(ValueError, match="snapshot"):
        _validate_layout(layout2)
    # State inside forbidden should also raise
    layout3 = {"source": tmp_path / "src3", "state": Path("/home/rt/pact_runs/books/1/sub"), "output": tmp_path / "out3"}
    (layout3["source"]).mkdir(parents=True, exist_ok=True); (layout3["output"]).mkdir(parents=True, exist_ok=True)
    with pytest.raises(ValueError, match="snapshot"):
        _validate_layout(layout3)


def test_validate_layout_symmetric_isolation(tmp_path):
    """Source/output must not be inside state (symmetric guard)."""
    from pact_full_pipeline_runner_v1.v4_run import _validate_layout
    # Legitimate layouts must still validate
    layout_rt = {"source": Path("D:/pact/pact_chapters"), "state": Path("D:/pact/book_state"), "output": Path("D:/pact/gate_bench_runs")}
    # Use tmp-based equivalent that mimics non-nesting
    legit = {"source": tmp_path / "csrc", "state": tmp_path / "cstate", "output": tmp_path / "cout"}
    for p in legit.values():
        p.mkdir(parents=True, exist_ok=True)
    _validate_layout(legit)  # should not raise
    # Source inside state must be rejected
    state = tmp_path / "state_iso"
    state.mkdir(exist_ok=True)
    src_inside = state / "chapters"
    src_inside.mkdir(exist_ok=True)
    out_ok = tmp_path / "out_iso_ok"
    out_ok.mkdir(exist_ok=True)
    with pytest.raises(ValueError, match="inside state"):
        _validate_layout({"source": src_inside, "state": state, "output": out_ok})
    # Output inside state must be rejected
    src_ok = tmp_path / "src_iso_ok2"
    src_ok.mkdir(exist_ok=True)
    out_inside = state / "runs"
    out_inside.mkdir(exist_ok=True)
    with pytest.raises(ValueError, match="inside state"):
        _validate_layout({"source": src_ok, "state": state, "output": out_inside})
    # Equality output==state also rejected
    with pytest.raises(ValueError, match="output and state"):
        _validate_layout({"source": src_ok, "state": state, "output": state})


def test_host_layout_env_overrides(tmp_path, monkeypatch):
    from pact_full_pipeline_runner_v1.v4_run import _host_layout
    monkeypatch.setenv("PACT_V4_SOURCE_ROOT", str(tmp_path / "csrc"))
    monkeypatch.setenv("PACT_V4_STATE_ROOT", str(tmp_path / "cstate"))
    monkeypatch.setenv("PACT_V4_OUT_ROOT", str(tmp_path / "cout"))
    layout = _host_layout()
    assert layout["source"] == Path(tmp_path / "csrc")
    assert layout["state"] == Path(tmp_path / "cstate")
    assert layout["output"] == Path(tmp_path / "cout")
    # Env PACT_V4_HOST hint
    monkeypatch.setenv("PACT_V4_HOST", "rt")
    monkeypatch.delenv("PACT_V4_SOURCE_ROOT", raising=False)
    monkeypatch.delenv("PACT_V4_STATE_ROOT", raising=False)
    monkeypatch.delenv("PACT_V4_OUT_ROOT", raising=False)
    layout_rt = _host_layout()
    assert layout_rt["source"] == Path("D:/pact/pact_chapters")
    monkeypatch.setenv("PACT_V4_HOST", "media")
    layout_media = _host_layout()
    assert layout_media["source"] == Path("/home/rt/pact_chapters")


def test_source_discovery_variable_suffix_and_single_shorthand(tmp_path, monkeypatch):
    from pact_full_pipeline_runner_v1.v4_run import _discover_chapter_sources
    src = tmp_path / "src"
    src.mkdir()
    (src / "0028_foo.html").write_text("<html>28</html>")
    (src / "0149_judgment-16-13.html").write_text("<html>149</html>")
    # Single chapter discovery
    res = _discover_chapter_sources(src, [28])
    assert 28 in res and res[28].name == "0028_foo.html"
    res2 = _discover_chapter_sources(src, [149])
    assert 149 in res2 and res2[149].name == "0149_judgment-16-13.html"
    # Missing should fail
    with pytest.raises(ValueError, match="no source"):
        _discover_chapter_sources(src, [9999])
    # Ambiguous should fail
    (src / "0028_bar.html").write_text("<html>dup</html>")
    with pytest.raises(ValueError, match="ambiguous"):
        _discover_chapter_sources(src, [28])


def test_source_discovery_rejects_symlink_and_fifo(tmp_path):
    from pact_full_pipeline_runner_v1.v4_run import _discover_chapter_sources
    import os
    src = tmp_path / "src2"
    src.mkdir()
    real = src / "0029_real.html"
    real.write_text("<html/>")
    link = src / "0029_link.html"
    try:
        link.symlink_to(real)
    except OSError:
        pytest.skip("symlink not supported")
    with pytest.raises(ValueError, match="symlink"):
        _discover_chapter_sources(src, [29])
    # Clean symlink and test FIFO
    link.unlink()
    fifo = src / "0030_fifo.html"
    try:
        os.mkfifo(fifo)
    except OSError:
        pytest.skip("fifo not supported")
    with pytest.raises(ValueError, match="not regular"):
        _discover_chapter_sources(src, [30])


def test_simple_local_injects_media_defaults_and_whole_chapter(tmp_path, monkeypatch):
    from pact_full_pipeline_runner_v1 import v4_run
    src = tmp_path / "src"
    state = tmp_path / "state"
    out = tmp_path / "out"
    src.mkdir(); state.mkdir(); out.mkdir()
    (src / "0028_alpha.html").write_text("<html/>")
    monkeypatch.setenv("PACT_V4_SOURCE_ROOT", str(src))
    monkeypatch.setenv("PACT_V4_STATE_ROOT", str(state))
    monkeypatch.setenv("PACT_V4_OUT_ROOT", str(out))
    ok = _ok_report(kind="local_llama")
    with patch("pact_v4.runtime.runtime_config.run_runtime_preflight", return_value=ok):
        with patch("pact_full_pipeline_runner_v1.v4_book_run.main") as mock_book:
            mock_book.return_value = 0
            rc = v4_run.main(["book", "--chapters", "28", "--local"])
            assert rc == 0
            delegated = mock_book.call_args[0][0]
            # Whole-chapter injected
            assert "--whole-chapter" in delegated
            # Media defaults
            assert "--media-book-id" in delegated
            assert delegated[delegated.index("--media-book-id")+1] == "1"
            assert "--media-target" in delegated and delegated[delegated.index("--media-target")+1] == "media-snap"
            assert "--media-root" in delegated and delegated[delegated.index("--media-root")+1] == "/home/rt/pact_runs"
            # Single shorthand 28 -> delegated contains full stem 0028_alpha
            assert "0028_alpha" in delegated
            # No duplicate --local leakage
            assert "--local" not in delegated


def test_simple_bare_remote_uses_profile_defaults(tmp_path, monkeypatch):
    from pact_full_pipeline_runner_v1 import v4_run
    src = tmp_path / "src"
    state = tmp_path / "state"
    out = tmp_path / "out"
    src.mkdir(); state.mkdir(); out.mkdir()
    (src / "0030_b.html").write_text("<html/>")
    monkeypatch.setenv("PACT_V4_SOURCE_ROOT", str(src))
    monkeypatch.setenv("PACT_V4_STATE_ROOT", str(state))
    monkeypatch.setenv("PACT_V4_OUT_ROOT", str(out))
    ok = _ok_report(kind="opencode_server")
    with patch("pact_v4.runtime.runtime_config.run_runtime_preflight", return_value=ok):
        with patch("pact_full_pipeline_runner_v1.v4_book_run.main") as mock_book:
            mock_book.return_value = 0
            rc = v4_run.main(["book", "--chapters", "30", "--remote"])
            assert rc == 0
            delegated = mock_book.call_args[0][0]
            assert "--managed-server" in delegated
            assert "--whole-chapter" in delegated
            assert "--media-book-id" in delegated


def test_simple_remote_with_alias_override(tmp_path, monkeypatch):
    from pact_full_pipeline_runner_v1 import v4_run
    src = tmp_path / "src"
    state = tmp_path / "state"
    out = tmp_path / "out"
    src.mkdir(); state.mkdir(); out.mkdir()
    (src / "0031_x.html").write_text("<html/>")
    monkeypatch.setenv("PACT_V4_SOURCE_ROOT", str(src))
    monkeypatch.setenv("PACT_V4_STATE_ROOT", str(state))
    monkeypatch.setenv("PACT_V4_OUT_ROOT", str(out))
    ok = _ok_report(kind="opencode_server")
    with patch("pact_v4.runtime.runtime_config.run_runtime_preflight", return_value=ok):
        with patch("pact_full_pipeline_runner_v1.v4_book_run.main") as mock_book:
            mock_book.return_value = 0
            rc = v4_run.main(["book", "--chapters", "31", "--remote", "musefree/luna"])
            assert rc == 0
            delegated = mock_book.call_args[0][0]
            assert "--translator" in delegated and "musefree" in delegated[delegated.index("--translator")+1]
            assert "--reviewer" in delegated and "luna" in delegated[delegated.index("--reviewer")+1]


def test_book_media_verdict_accepted(capsys, tmp_path, monkeypatch):
    from pact_full_pipeline_runner_v1 import v4_book_run
    # Mock run_book to return accepted with confirmation
    payload = {"schema": "pact-v4-book-run/v1", "memory_dir": str(tmp_path), "candidates_ledger": str(tmp_path / "gl.json"), "book_memory_candidates_ledger": str(tmp_path / "bm.json"), "chapters": [{"chapter_id": "0028", "terminal_status": "complete", "book_memory_hash_before": "a", "book_memory_hash_after": "b", "promoted": True, "promote_detail": "", "out_dir": str(tmp_path), "candidates": {}, "book_memory_candidates": {}, "book_memory_promotions": [], "index_built": False, "error": None, "media_confirmation": {"status": "ACCEPTED", "revision_id": "rev-0002"}, "media_error": None}]}
    out_base = tmp_path / "outbase"
    out_base.mkdir()
    (out_base / "book_run.json").write_text(json.dumps(payload))
    with patch("pact_full_pipeline_runner_v1.v4_book_run.run_book", return_value=payload):
        rc = v4_book_run.main(["--memory-dir", str(tmp_path), "--chapters", "0028", "--chapter-html-pattern", str(tmp_path / "{chapter_id}.html"), "--out-base", str(out_base), "--media-book-id", "1"])
        out = capsys.readouterr().out
        assert "MEDIA PUBLISH: ACCEPTED" in out
        assert "rev-0002" in out
        assert rc == 0
        data = json.loads((out_base / "book_run.json").read_text())
        assert data["media_publish"]["status"] == "ACCEPTED"


def test_book_media_verdict_rejected(capsys, tmp_path):
    from pact_full_pipeline_runner_v1 import v4_book_run
    payload = {"schema": "pact-v4-book-run/v1", "memory_dir": str(tmp_path), "candidates_ledger": str(tmp_path / "gl.json"), "book_memory_candidates_ledger": str(tmp_path / "bm.json"), "chapters": [{"chapter_id": "0028", "terminal_status": "complete", "book_memory_hash_before": "a", "book_memory_hash_after": "b", "promoted": True, "promote_detail": "", "out_dir": str(tmp_path), "candidates": {}, "book_memory_candidates": {}, "book_memory_promotions": [], "index_built": False, "error": None, "media_confirmation": {"status": "REJECTED", "reason": "STALE_PARENT"}, "media_error": None}]}
    out_base = tmp_path / "outbase2"
    out_base.mkdir()
    (out_base / "book_run.json").write_text(json.dumps(payload))
    with patch("pact_full_pipeline_runner_v1.v4_book_run.run_book", return_value=payload):
        rc = v4_book_run.main(["--memory-dir", str(tmp_path), "--chapters", "0028", "--chapter-html-pattern", str(tmp_path / "{chapter_id}.html"), "--out-base", str(out_base), "--media-book-id", "1"])
        out = capsys.readouterr().out
        assert "MEDIA PUBLISH: REJECTED" in out
        assert "STALE_PARENT" in out
        assert rc == 1


def test_book_media_verdict_transport_failure(capsys, tmp_path):
    from pact_full_pipeline_runner_v1 import v4_book_run
    payload = {"schema": "pact-v4-book-run/v1", "memory_dir": str(tmp_path), "candidates_ledger": str(tmp_path / "gl.json"), "book_memory_candidates_ledger": str(tmp_path / "bm.json"), "chapters": [{"chapter_id": "0028", "terminal_status": "complete", "book_memory_hash_before": "a", "book_memory_hash_after": "b", "promoted": True, "promote_detail": "", "out_dir": str(tmp_path), "candidates": {}, "book_memory_candidates": {}, "book_memory_promotions": [], "index_built": False, "error": None, "media_confirmation": None, "media_error": "ssh timeout"}]}
    out_base = tmp_path / "outbase3"
    out_base.mkdir()
    (out_base / "book_run.json").write_text(json.dumps(payload))
    with patch("pact_full_pipeline_runner_v1.v4_book_run.run_book", return_value=payload):
        rc = v4_book_run.main(["--memory-dir", str(tmp_path), "--chapters", "0028", "--chapter-html-pattern", str(tmp_path / "{chapter_id}.html"), "--out-base", str(out_base), "--media-book-id", "1"])
        out = capsys.readouterr().out
        assert "MEDIA PUBLISH: REJECTED" in out
        assert "ssh timeout" in out or "missing confirmation" in out
        assert rc == 1


def test_book_media_verdict_missing_confirmation(capsys, tmp_path):
    from pact_full_pipeline_runner_v1 import v4_book_run
    payload = {"schema": "pact-v4-book-run/v1", "memory_dir": str(tmp_path), "candidates_ledger": str(tmp_path / "gl.json"), "book_memory_candidates_ledger": str(tmp_path / "bm.json"), "chapters": [{"chapter_id": "0028", "terminal_status": "complete", "book_memory_hash_before": "a", "book_memory_hash_after": "b", "promoted": True, "promote_detail": "", "out_dir": str(tmp_path), "candidates": {}, "book_memory_candidates": {}, "book_memory_promotions": [], "index_built": False, "error": None, "media_confirmation": None, "media_error": None}]}
    out_base = tmp_path / "outbase4"
    out_base.mkdir()
    (out_base / "book_run.json").write_text(json.dumps(payload))
    with patch("pact_full_pipeline_runner_v1.v4_book_run.run_book", return_value=payload):
        rc = v4_book_run.main(["--memory-dir", str(tmp_path), "--chapters", "0028", "--chapter-html-pattern", str(tmp_path / "{chapter_id}.html"), "--out-base", str(out_base), "--media-book-id", "1"])
        out = capsys.readouterr().out
        assert "MEDIA PUBLISH: REJECTED" in out
        assert "missing confirmation" in out
        assert rc == 1


def test_output_rejected_inside_source_and_canonical_snapshot(tmp_path, monkeypatch):
    from pact_full_pipeline_runner_v1.v4_run import _validate_layout
    src = tmp_path / "src"
    src.mkdir()
    state = tmp_path / "state"
    state.mkdir()
    # Output inside source must be rejected
    out_inside_src = src / "runs"
    with pytest.raises(ValueError, match="inside source"):
        _validate_layout({"source": src, "state": state, "output": out_inside_src})
    # Output equal to source must be rejected
    with pytest.raises(ValueError, match="same as source"):
        _validate_layout({"source": src, "state": state, "output": src})
    # Output inside canonical snapshot root must be rejected
    out_inside_snap = Path("/home/rt/pact_runs/books/1") / "evil"
    with pytest.raises(ValueError, match="snapshot"):
        _validate_layout({"source": src, "state": state, "output": out_inside_snap})
    out_snap_exact = Path("/home/rt/pact_runs/books")
    with pytest.raises(ValueError, match="snapshot"):
        _validate_layout({"source": src, "state": state, "output": out_snap_exact})
    # Legitimate output outside both must still validate
    out_ok = tmp_path / "out_ok2"
    out_ok.mkdir()
    _validate_layout({"source": src, "state": state, "output": out_ok})


def test_book_out_base_inside_source_or_snapshot_fails_before_mkdir(tmp_path, monkeypatch):
    from pact_full_pipeline_runner_v1.v4_run import main
    src = tmp_path / "src"
    src.mkdir()
    (src / "0028_a.html").write_text("<html/>")
    state = tmp_path / "state"
    state.mkdir()
    monkeypatch.setenv("PACT_V4_SOURCE_ROOT", str(src))
    monkeypatch.setenv("PACT_V4_STATE_ROOT", str(state))
    # Use a bad out_base inside source — must fail before creation
    bad_out = src / "nested_out"
    assert not bad_out.exists()
    ok = _ok_report()
    with patch("pact_v4.runtime.runtime_config.run_runtime_preflight", return_value=ok):
        with patch("pact_full_pipeline_runner_v1.v4_book_run.main") as mock_book:
            with pytest.raises(SystemExit) as exc:
                main(["book", "--chapters", "28", "--local", "--out-base", str(bad_out)])
            assert exc.value.code == 2
            assert not mock_book.called
            assert not bad_out.exists()
    # Bad out_base inside canonical snapshot root — must also fail before creation
    bad_snap = Path("/home/rt/pact_runs/books/1/evil_out")
    # Ensure underlying /home/rt/pact_runs/books does not get created by mkdir
    # by mocking mkdir if it were called, but validation should exit before mkdir
    with patch("pact_v4.runtime.runtime_config.run_runtime_preflight", return_value=ok):
        with patch("pact_full_pipeline_runner_v1.v4_book_run.main") as mock_book:
            with pytest.raises(SystemExit) as exc:
                main(["book", "--chapters", "28", "--local", "--out-base", str(bad_snap)])
            assert exc.value.code == 2
            assert not mock_book.called


def test_preflight_path_readiness_missing_and_unwritable(tmp_path, monkeypatch):
    from pact_full_pipeline_runner_v1.v4_run import main
    src = tmp_path / "src"
    src.mkdir()
    (src / "0028_a.html").write_text("<html/>")
    # Use a nonexistent state dir whose parent exists and is writable -> should pass
    state_ok = tmp_path / "state_ok" / "nested"
    (tmp_path / "state_ok").mkdir()
    out_ok = tmp_path / "out_ok"
    out_ok.mkdir()
    monkeypatch.setenv("PACT_V4_SOURCE_ROOT", str(src))
    monkeypatch.setenv("PACT_V4_STATE_ROOT", str(state_ok))
    monkeypatch.setenv("PACT_V4_OUT_ROOT", str(out_ok))
    ok = _ok_report()
    with patch("pact_v4.runtime.runtime_config.run_runtime_preflight", return_value=ok):
        with patch("pact_full_pipeline_runner_v1.v4_book_run.main") as mock_book:
            mock_book.return_value = 0
            rc = main(["book", "--chapters", "28", "--local", "--preflight"])
            assert rc == 0
    # Now make parent unwritable via mock
    unwritable_parent = tmp_path / "unwritable"
    unwritable_parent.mkdir()
    state_bad = unwritable_parent / "nested_state"
    monkeypatch.setenv("PACT_V4_STATE_ROOT", str(state_bad))
    with patch("os.access", return_value=False):
        with patch("pact_v4.runtime.runtime_config.run_runtime_preflight", return_value=ok):
            rc = main(["book", "--chapters", "28", "--local", "--preflight"])
            assert rc == 1
    # Output unwritable similarly
    monkeypatch.setenv("PACT_V4_STATE_ROOT", str(state_ok))
    out_bad = unwritable_parent / "out_bad"
    monkeypatch.setenv("PACT_V4_OUT_ROOT", str(out_bad))
    with patch("os.access", return_value=False):
        with patch("pact_v4.runtime.runtime_config.run_runtime_preflight", return_value=ok):
            rc = main(["book", "--chapters", "28", "--local", "--preflight"])
            assert rc == 1
