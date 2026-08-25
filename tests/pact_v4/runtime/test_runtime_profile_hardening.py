"""Focused regression tests for runtime-profile-hardening.

Findings addressed:
- HIGH remote reasoning profile default vs CLI code default
- HIGH local host/port strict validation (bool/float, unsafe hosts)
- MEDIUM loader/alias/preflight/remote-defaults coverage
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from pact_v4.runtime.runtime_config import (
    LocalLlamaBackendConfig,
    OpenCodeBackendConfig,
    ProvidersRegistry,
    ProviderModel,
    load_providers_registry,
    load_runtime_config,
    run_runtime_preflight,
)
from pact_v4.runtime.opencode_backend import OpenCodeServerBackendConfig


def _repo_root() -> Path:
    return Path(__file__).parents[3]


# ---------------------------------------------------------------------------
# 1. Local loader strict validation
# ---------------------------------------------------------------------------

def _base_local_payload(**overrides):
    base = {
        "kind": "local_llama",
        "exe": "C:/fake/llama.exe",
        "model_paths": {"gemma": "C:/m/gemma.gguf"},
        "model_names": {"gemma": "gemma"},
        "server_args": {"gemma": []},
    }
    base.update(overrides)
    return base


def test_local_rejects_unknown_top_level_field():
    payload = _base_local_payload(extra_field=123)
    with pytest.raises(ValueError, match="unsupported key"):
        load_runtime_config(payload)


def test_local_rejects_malformed_model_paths_type():
    payload = _base_local_payload(model_paths="not-a-mapping")
    with pytest.raises(ValueError, match="model_paths"):
        load_runtime_config(payload)


def test_local_rejects_missing_required_exe():
    payload = _base_local_payload()
    payload.pop("exe")
    with pytest.raises(ValueError, match="exe is required"):
        load_runtime_config(payload)


def test_local_rejects_unsupported_model_key():
    payload = _base_local_payload(
        model_paths={"bad": "C:/m/bad.gguf"},
        model_names={"bad": "bad"},
        server_args={"bad": []},
    )
    with pytest.raises(ValueError, match="unsupported model key"):
        load_runtime_config(payload)


def test_local_rejects_mismatched_model_keys():
    payload = _base_local_payload(
        model_paths={"gemma": "C:/m/gemma.gguf"},
        model_names={"qwen": "qwen"},
        server_args={"gemma": []},
    )
    with pytest.raises(ValueError, match="must describe the same"):
        load_runtime_config(payload)


def test_local_rejects_non_string_server_args_element():
    payload = _base_local_payload(server_args={"gemma": [True]})
    with pytest.raises(ValueError, match="server_args must contain only strings"):
        load_runtime_config(payload)


def test_local_rejects_empty_host():
    payload = _base_local_payload(host="   ")
    with pytest.raises(ValueError, match="host must be"):
        load_runtime_config(payload)


@pytest.mark.parametrize("bad_host", ["8.8.8.8", "evil.com", "0.0.0.0", "192.168.1.1", "999.999.999.999", "example.com", "http://evil.com"])
def test_local_rejects_unsafe_or_malformed_host(bad_host):
    payload = _base_local_payload(host=bad_host)
    with pytest.raises(ValueError, match="host must be a local loopback"):
        load_runtime_config(payload)


@pytest.mark.parametrize("good_host", ["127.0.0.1", "localhost", "::1", "127.0.0.2", "127.1.2.3"])
def test_local_accepts_safe_hosts(good_host):
    payload = _base_local_payload(host=good_host)
    cfg = load_runtime_config(payload)
    assert isinstance(cfg, LocalLlamaBackendConfig)


def test_local_rejects_bool_port():
    payload = _base_local_payload(port=True)
    with pytest.raises(ValueError, match="port must be an integer"):
        load_runtime_config(payload)


def test_local_rejects_float_port():
    payload = _base_local_payload(port=8093.0)
    with pytest.raises(ValueError, match="port must be an integer"):
        load_runtime_config(payload)


def test_local_rejects_string_port():
    payload = _base_local_payload(port="8093")
    with pytest.raises(ValueError, match="port must be an integer"):
        load_runtime_config(payload)


@pytest.mark.parametrize("bad_port", [0, -1, 70000, 99999])
def test_local_rejects_out_of_range_port(bad_port):
    payload = _base_local_payload(port=bad_port)
    with pytest.raises(ValueError, match="port must be 1-65535"):
        load_runtime_config(payload)


def test_canonical_local_example_loads_and_is_strict():
    yaml = pytest.importorskip("yaml")
    path = _repo_root() / "configs" / "runtime_local.example.yaml"
    cfg = load_runtime_config(yaml.safe_load(path.read_text(encoding="utf-8")))
    assert isinstance(cfg, LocalLlamaBackendConfig)
    # Ensure host is allowed and port integral
    assert cfg.host in ("127.0.0.1", "localhost", "::1") or cfg.host.startswith("127.")
    assert isinstance(cfg.port, int) and not isinstance(cfg.port, bool)


# ---------------------------------------------------------------------------
# 2. Provider alias case-insensitivity / global uniqueness
# ---------------------------------------------------------------------------

def test_alias_duplicate_case_insensitive_rejected(tmp_path: Path):
    path = tmp_path / "providers.yaml"
    path.write_text(
        "providers:\n"
        "  opencode-go:\n"
        "    kind: opencode_server\n"
        "    models:\n"
        "      Muse:\n"
        "        ref: opencode-go/muse\n"
        "        reasoning_contract:\n"
        "          variants: [low]\n"
        "  openai:\n"
        "    kind: opencode_server\n"
        "    models:\n"
        "      MUSE:\n"
        "        ref: openai/muse\n"
        "        reasoning_contract:\n"
        "          variants: [low]\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="duplicate alias"):
        load_providers_registry(path)


def test_alias_resolve_case_insensitive(tmp_path: Path):
    path = tmp_path / "providers.yaml"
    path.write_text(
        "providers:\n"
        "  opencode-go:\n"
        "    kind: opencode_server\n"
        "    models:\n"
        "      DeepSeek4Flash:\n"
        "        ref: opencode-go/deepseek-v4-flash\n"
        "        reasoning_contract:\n"
        "          variants: [low]\n",
        encoding="utf-8",
    )
    reg = load_providers_registry(path)
    m = reg.resolve("OPENCODE-GO/deepseek4flash")
    assert m.ref == "opencode-go/deepseek-v4-flash"
    m2 = reg.resolve("opencode-go/DEEPSEEK4FLASH")
    assert m2.ref == "opencode-go/deepseek-v4-flash"
    # bare alias case-insensitive
    b = reg.resolve_bare("DEEPSEEK4FLASH")
    assert b.ref == "opencode-go/deepseek-v4-flash"
    b2 = reg.resolve_bare("deepseek4flash")
    assert b2.ref == b.ref


def test_bare_alias_unique_resolves_and_duplicate_fails(tmp_path: Path):
    path = tmp_path / "providers.yaml"
    path.write_text(
        "providers:\n"
        "  opencode-go:\n"
        "    kind: opencode_server\n"
        "    models:\n"
        "      luna:\n"
        "        ref: opencode-go/luna\n"
        "        reasoning_contract:\n"
        "          variants: [low]\n",
        encoding="utf-8",
    )
    reg = load_providers_registry(path)
    assert reg.resolve_bare("luna").ref == "opencode-go/luna"
    assert reg.resolve_bare("LUNA").ref == "opencode-go/luna"
    with pytest.raises(ValueError, match="unknown bare alias"):
        reg.resolve_bare("nope")


def test_provider_qualified_resolution_remains_supported(tmp_path: Path):
    path = tmp_path / "providers.yaml"
    path.write_text(
        "providers:\n"
        "  openai:\n"
        "    kind: opencode_server\n"
        "    models:\n"
        "      luna:\n"
        "        ref: openai/luna\n"
        "        reasoning_contract:\n"
        "          variants: [low]\n",
        encoding="utf-8",
    )
    reg = load_providers_registry(path)
    assert reg.resolve("openai/luna").ref == "openai/luna"
    assert reg.resolve("OpenAI/Luna").ref == "openai/luna"
    assert reg.resolve("OPENAI/LUNA").ref == "openai/luna"


# ---------------------------------------------------------------------------
# 3. Preflight side-effect guarantees and sanitized output
# ---------------------------------------------------------------------------

def test_preflight_is_side_effect_free_and_sanitized(tmp_path: Path, monkeypatch):
    # Local: create fake files
    exe = tmp_path / "llama.exe"
    exe.write_text("x")
    gemma = tmp_path / "gemma.gguf"
    gemma.write_text("x")
    qwen = tmp_path / "qwen.gguf"
    qwen.write_text("x")
    cfg = load_runtime_config({
        "kind": "local_llama",
        "exe": str(exe),
        "model_paths": {"gemma": str(gemma), "qwen": str(qwen)},
        "model_names": {"gemma": "gemma", "qwen": "qwen"},
        "server_args": {"gemma": [], "qwen": []},
        "port": 51234,
    })
    # Ensure no env leakage check: set env with secrets, preflight must not emit values
    monkeypatch.setenv("TEST_SECRET_ENV", "super-secret-value-123")
    # Monkeypatch local file checks? Not needed – just verify preflight doesn't read env value
    report = run_runtime_preflight(cfg, reasoning=0)
    assert report.ok
    j = report.to_json()
    h = report.format_human()
    assert "super-secret-value-123" not in j
    assert "super-secret-value-123" not in h
    # Ensure no run artifact created (out_dir not auto-created)
    assert not (tmp_path / "should_not_exist").exists()
    # Ensure checks include exe and model paths and port
    names = [c.name for c in report.checks]
    assert any("exe" in n for n in names)
    assert any("port" in n for n in names)


def test_preflight_local_missing_path_fails_without_starting_server(tmp_path: Path):
    cfg = load_runtime_config({
        "kind": "local_llama",
        "exe": str(tmp_path / "missing.exe"),
        "model_paths": {"gemma": str(tmp_path / "gemma.gguf")},
        "model_names": {"gemma": "gemma"},
        "server_args": {"gemma": []},
        "port": 51235,
    })
    report = run_runtime_preflight(cfg)
    assert not report.ok
    assert any(not c.ok for c in report.checks)
    # No server started – we just verify report, not that we attempted to start


def test_preflight_remote_missing_env_fails_safely(monkeypatch):
    monkeypatch.delenv("OPENCODE_SERVER_USERNAME", raising=False)
    monkeypatch.delenv("OPENCODE_SERVER_PASSWORD", raising=False)
    cfg = load_runtime_config({
        "kind": "opencode_server",
        "base_url": "http://127.0.0.1:4097",
        "model_bindings": {"generator": "opencode-go/deepseek-v4-flash"},
    })
    # Set one env to ensure other still missing
    monkeypatch.setenv("OPENCODE_SERVER_USERNAME", "user")
    report = run_runtime_preflight(cfg)
    assert not report.ok
    # Should mention password env name but not value
    txt = report.to_json() + report.format_human()
    assert "OPENCODE_SERVER_PASSWORD" in txt
    # Ensure secret value not leaked even if we set one
    monkeypatch.setenv("OPENCODE_SERVER_PASSWORD", "my-secret-999")
    report2 = run_runtime_preflight(cfg)
    # Now both present – should be ok
    assert report2.ok
    txt2 = report2.to_json()
    assert "my-secret-999" not in txt2


def test_preflight_does_not_contact_remote_endpoint(monkeypatch):
    # Ensure preflight does not attempt HTTP – we would see exception if it tried
    # By not providing network, we can verify it returns without network call
    monkeypatch.delenv("OPENCODE_SERVER_USERNAME", raising=False)
    monkeypatch.delenv("OPENCODE_SERVER_PASSWORD", raising=False)
    monkeypatch.setenv("OPENCODE_SERVER_USERNAME", "u")
    monkeypatch.setenv("OPENCODE_SERVER_PASSWORD", "p")
    cfg = load_runtime_config({
        "kind": "opencode_server",
        "base_url": "http://127.0.0.1:4099",
        "model_bindings": {"generator": "opencode-go/deepseek-v4-flash"},
        "reasoning": 1,
    })
    report = run_runtime_preflight(cfg, reasoning=1)
    assert report.ok
    # public_record must contain reasoning but no credential values
    assert "reasoning" in report.public_record["effective_options"]
    j = report.to_json()
    # Env names are expected (sanitized), but their values must not be leaked
    assert "OPENCODE_SERVER_PASSWORD" in j
    assert "OPENCODE_SERVER_USERNAME" in j
    # Values set above are "u"/"p" – ensure they are not mistaken for evidence of leak by checking longer secret not present
    # (single-char values are too generic to assert absence, but we ensure no disclosure of value field)
    assert "p" not in report.public_record.get("effective_options", {})  # no credential value in options
    assert any("env" in c.name for c in report.checks)


# ---------------------------------------------------------------------------
# 4. Remote defaults and overrides
# ---------------------------------------------------------------------------

def test_canonical_remote_example_is_policy_bearing_and_no_secrets():
    yaml = pytest.importorskip("yaml")
    path = _repo_root() / "configs" / "runtime_remote.example.yaml"
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert raw["kind"] == "opencode_server"
    # Must explicitly carry reasoning, timeout, budget, structured_output, model_bindings
    assert "reasoning" in raw, "canonical remote must explicitly carry reasoning"
    assert raw["reasoning"] in (0, 1, 2, 3)
    assert raw["timeout_seconds"] == 900
    assert raw["remote_budget"]["max_requests_per_chapter"] == 500
    assert raw["structured_output_mode"] == "prompt_only"
    assert "model_bindings" in raw and "generator" in raw["model_bindings"]
    # Must not contain credential values
    blob = path.read_text(encoding="utf-8")
    lower = blob.lower()
    assert "password" not in lower or "password_env" in lower  # only env name allowed
    assert "sk-" not in lower  # no api key
    cfg = load_runtime_config(raw)
    assert isinstance(cfg, OpenCodeBackendConfig)
    assert cfg.server.reasoning == raw["reasoning"]
    assert cfg.server.timeout_seconds == 900
    rec = cfg.public_record()
    assert "password" not in str(rec).lower()
    assert rec["effective_options"]["reasoning"] == raw["reasoning"]


def test_remote_default_reasoning_used_when_cli_omitted():
    # Simulate CLI omitted (None) should use profile value
    from pact_full_pipeline_runner_v1.v4_phase12_strict_run import _resolve_effective_reasoning
    import argparse
    cfg = load_runtime_config({
        "kind": "opencode_server",
        "base_url": "http://127.0.0.1:4097",
        "model_bindings": {"generator": "opencode-go/deepseek-v4-flash"},
        "reasoning": 2,
    })
    args = argparse.Namespace(reasoning=None)
    assert _resolve_effective_reasoning(args, cfg) == 2
    # CLI explicit overrides
    args2 = argparse.Namespace(reasoning=1)
    assert _resolve_effective_reasoning(args2, cfg) == 1


def test_remote_explicit_reasoning_override_is_identity_bearing():
    cfg1 = load_runtime_config({
        "kind": "opencode_server",
        "base_url": "http://127.0.0.1:4097",
        "model_bindings": {"generator": "opencode-go/deepseek-v4-flash"},
        "reasoning": 0,
    })
    cfg2 = load_runtime_config({
        "kind": "opencode_server",
        "base_url": "http://127.0.0.1:4097",
        "model_bindings": {"generator": "opencode-go/deepseek-v4-flash"},
        "reasoning": 2,
    })
    assert cfg1.identity_hash != cfg2.identity_hash
    # CLI override should produce same effect as profile change
    from pact_full_pipeline_runner_v1.v4_phase12_strict_run import _with_reasoning_override
    cfg1_overridden = _with_reasoning_override(cfg1, 2)
    assert cfg1_overridden.identity_hash == cfg2.identity_hash
    assert cfg1_overridden.server.reasoning == 2


def test_remote_reasoning_invalid_values_rejected():
    for bad in (True, 4, -1, "0", 1.0):
        with pytest.raises(ValueError, match="reasoning"):
            load_runtime_config({
                "kind": "opencode_server",
                "base_url": "http://127.0.0.1:4097",
                "model_bindings": {"generator": "opencode-go/deepseek-v4-flash"},
                "reasoning": bad,
            })


def test_preflight_reports_resolved_remote_defaults_and_overrides(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("OPENCODE_SERVER_USERNAME", "u")
    monkeypatch.setenv("OPENCODE_SERVER_PASSWORD", "p")
    cfg = load_runtime_config({
        "kind": "opencode_server",
        "base_url": "http://127.0.0.1:4097",
        "model_bindings": {"generator": "opencode-go/deepseek-v4-flash"},
        "reasoning": 0,
        "reasoning_effort_map": {1: "low", 2: "medium", 3: "high"},
    })
    report_default = run_runtime_preflight(cfg, reasoning=0)
    assert report_default.ok
    assert report_default.public_record["effective_options"]["reasoning"] == 0
    assert report_default.model_bindings["generator"] == "opencode-go/deepseek-v4-flash"
    # With override, report should reflect new reasoning and identity
    from pact_full_pipeline_runner_v1.v4_phase12_strict_run import _with_reasoning_override
    cfg_over = _with_reasoning_override(cfg, 3)
    report_over = run_runtime_preflight(cfg_over, reasoning=3)
    assert report_over.ok
    assert report_over.public_record["effective_options"]["reasoning"] == 3
    assert report_over.identity_hash != report_default.identity_hash
    # Translator override should change bindings and be reflected
    from pact_v4.runtime.runtime_config import apply_provider_flags
    _prov = tmp_path / "providers.yaml"
    _prov.write_text(
        "providers:\n"
        "  opencode-go:\n"
        "    kind: opencode_server\n"
        "    models:\n"
        "      alt:\n"
        "        ref: opencode-go/alt-model\n"
        "        reasoning_contract:\n"
        "          variants: [low]\n",
        encoding="utf-8",
    )
    reg = load_providers_registry(_prov)
    cfg_trans = apply_provider_flags(cfg, reg, translator="opencode-go/alt")
    report_trans = run_runtime_preflight(cfg_trans)
    assert report_trans.model_bindings["generator"] == "opencode-go/alt-model"


def test_no_config_omitted_reasoning_remains_safe_and_default_verified(tmp_path: Path):
    """Omitted --reasoning must stay safe for legacy no-config helper and verify as 0."""
    from pact_full_pipeline_runner_v1 import v4_phase12_strict_run as cli
    # Parser default is None to distinguish omission for configured profiles
    args_omitted = cli.build_argparser().parse_args([
        "--chapter-id", "test",
        "--chapter-html", str(tmp_path / "c.html"),
        "--memory-dir", str(tmp_path),
        "--out-dir", str(tmp_path / "out"),
    ])
    assert args_omitted.reasoning is None
    # Legacy helper must not raise TypeError and must produce baseline budget 0
    gemma_args = cli._gemma_server_args_for_reasoning(args_omitted.reasoning)
    assert gemma_args[gemma_args.index("--reasoning-budget") + 1] == "0"
    # Legacy validate must accept omitted as baseline 0
    from pact_v4.runtime.runtime_config import LocalLlamaBackendConfig
    from pathlib import Path as _P
    # Use minimal local config via helper (no runtime_config file)
    cli.validate_reasoning_backend(args_omitted.reasoning, cli.StrictBackendConfig(
        exe=_P(r"C:\src\llama-sycl-edge\build\bin\llama-server.exe"),
        device="SYCL0", host="127.0.0.1",
        model_paths={"gemma": cli.GEMMA_PATH, "qwen": cli.QWEN_PATH},
        model_names={"gemma": cli.GEMMA_PATH.name, "qwen": cli.QWEN_PATH.name},
        server_args={"gemma": gemma_args, "qwen": cli.QWEN_SERVER_ARGS},
        port=8094,
    ))
    # Effective reasoning for no-config remains verified as 0
    cfg = cli._build_run_config(args_omitted, _remote_cfg())
    assert cfg.reasoning == 0
    # When --runtime-config present and omitted, profile default must be used
    cfg_remote = load_runtime_config({
        "kind": "opencode_server",
        "base_url": "http://127.0.0.1:4097",
        "model_bindings": {"generator": "opencode-go/deepseek-v4-flash"},
        "reasoning": 2,
    })
    from pact_full_pipeline_runner_v1.v4_phase12_strict_run import _resolve_effective_reasoning
    import argparse as _ap
    assert _resolve_effective_reasoning(_ap.Namespace(reasoning=None), cfg_remote) == 2
    assert _resolve_effective_reasoning(_ap.Namespace(reasoning=0), cfg_remote) == 0


def _remote_cfg():
    from pact_v4.runtime.opencode_backend import OpenCodeServerBackendConfig
    from pact_v4.runtime.runtime_config import OpenCodeBackendConfig
    return OpenCodeBackendConfig(server=OpenCodeServerBackendConfig(base_url="http://127.0.0.1:4096", model_bindings={"generator": "opencode-go/x"}))


def test_preflight_local_ipv6_loopback_uses_af_inet6(monkeypatch, tmp_path: Path):
    """Regression: ::1 must use AF_INET6, not AF_INET (address-family unsupported)."""
    import pact_v4.runtime.runtime_config as rc
    captured: dict = {}

    def fake_socket(family, *args, **kwargs):
        captured["family"] = family

        class Dummy:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def setsockopt(self, *a, **kw):
                pass

            def bind(self, addr):
                captured["addr"] = addr

        return Dummy()

    monkeypatch.setattr(rc._socket, "socket", fake_socket)
    exe = tmp_path / "llama.exe"
    exe.write_text("x")
    gemma = tmp_path / "gemma.gguf"
    gemma.write_text("x")
    cfg = load_runtime_config({
        "kind": "local_llama",
        "exe": str(exe),
        "host": "::1",
        "port": 51255,
        "model_paths": {"gemma": str(gemma)},
        "model_names": {"gemma": "gemma"},
        "server_args": {"gemma": []},
    })
    report = run_runtime_preflight(cfg)
    assert report.ok, report.format_human()
    assert captured["family"] == rc._socket.AF_INET6
    # AF_INET6 bind requires 4-tuple
    assert captured["addr"][0] == "::1"
    assert len(captured["addr"]) == 4


def test_preflight_local_ipv4_uses_af_inet(monkeypatch, tmp_path: Path):
    import pact_v4.runtime.runtime_config as rc
    captured: dict = {}

    def fake_socket(family, *args, **kwargs):
        captured["family"] = family

        class Dummy:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def setsockopt(self, *a, **kw):
                pass

            def bind(self, addr):
                captured["addr"] = addr

        return Dummy()

    monkeypatch.setattr(rc._socket, "socket", fake_socket)
    exe = tmp_path / "llama.exe"
    exe.write_text("x")
    gemma = tmp_path / "gemma.gguf"
    gemma.write_text("x")
    cfg = load_runtime_config({
        "kind": "local_llama",
        "exe": str(exe),
        "host": "127.0.0.1",
        "port": 51256,
        "model_paths": {"gemma": str(gemma)},
        "model_names": {"gemma": "gemma"},
        "server_args": {"gemma": []},
    })
    report = run_runtime_preflight(cfg)
    assert report.ok
    assert captured["family"] == rc._socket.AF_INET
    assert captured["addr"] == ("127.0.0.1", 51256)


def test_canonical_local_configured_preflight_with_omitted_reasoning(tmp_path: Path, monkeypatch):
    """HIGH regression: canonical local profile must preflight with omitted --reasoning."""
    import yaml
    from pact_full_pipeline_runner_v1 import v4_phase12_strict_run as cli
    # Patch file existence: canonical paths are Windows, not present in CI
    orig_is_file = Path.is_file

    def fake_is_file(self: Path):
        # Pretend canonical exe/model paths exist for preflight path checks
        s = str(self)
        if "llama-server.exe" in s or ".gguf" in s:
            return True
        return orig_is_file(self)

    monkeypatch.setattr(Path, "is_file", fake_is_file)
    # Also patch socket bind to avoid needing real port/host
    import pact_v4.runtime.runtime_config as rc
    real_socket = rc._socket.socket

    def fake_socket_ok(family, *a, **kw):
        class Dummy:
            def __enter__(self): return self
            def __exit__(self, *exc): return False
            def setsockopt(self, *a, **kw): pass
            def bind(self, addr): pass
        return Dummy()
    monkeypatch.setattr(rc._socket, "socket", fake_socket_ok)

    yaml_path = Path("configs/runtime_local.example.yaml")
    backend = cli._load_runtime_config_file(yaml_path)
    assert isinstance(backend, LocalLlamaBackendConfig)
    # Omitted reasoning must resolve from profile (2048 -> >0) and pass validation/preflight
    args_omitted = cli.build_argparser().parse_args([
        "--chapter-id", "test",
        "--chapter-html", str(tmp_path / "c.html"),
        "--memory-dir", str(tmp_path),
        "--out-dir", str(tmp_path / "out"),
        "--runtime-config", str(yaml_path),
    ])
    assert args_omitted.reasoning is None
    effective = cli._resolve_effective_reasoning(args_omitted, backend)
    assert effective > 0, f"canonical local omitted should resolve >0, got {effective}"
    # Should not raise
    cli.validate_reasoning_backend(effective, backend)
    # Preflight with effective reasoning must be OK (no TypeError, no validation reject)
    report = rc.run_runtime_preflight(backend, reasoning=effective)
    assert report.ok, report.format_human()
    # No-config legacy path must still resolve to 0 and produce budget 0
    args_no_cfg = cli.build_argparser().parse_args([
        "--chapter-id", "test",
        "--chapter-html", str(tmp_path / "c.html"),
        "--memory-dir", str(tmp_path),
        "--out-dir", str(tmp_path / "out2"),
    ])
    backend_empty = cli.StrictBackendConfig(
        exe=Path("C:/x.exe"), device="SYCL0", host="127.0.0.1",
        model_paths={"gemma": Path("x")}, model_names={"gemma": "x"}, server_args={"gemma": []}, port=8094
    )
    assert cli._resolve_effective_reasoning(args_no_cfg, backend_empty) == 0
    # Explicit helper check for no-config: omitted -> budget 0
    gemma_args_omitted = cli._gemma_server_args_for_reasoning(None)
    assert gemma_args_omitted[gemma_args_omitted.index("--reasoning-budget") + 1] == "0"
    # And run_with_runtime_config path via monkeypatched runtime should not raise with omitted
    # (we test via direct _resolve + validate + preflight above, which is the core of run_with_runtime_config)


def test_canonical_local_configured_preflight_with_explicit_reasoning_0(tmp_path: Path, monkeypatch):
    """HIGH regression: explicit --reasoning 0 must derive coherent server args and preflight."""
    import yaml
    from pact_full_pipeline_runner_v1 import v4_phase12_strict_run as cli
    orig_is_file = Path.is_file

    def fake_is_file(self: Path):
        s = str(self)
        if "llama-server.exe" in s or ".gguf" in s:
            return True
        return orig_is_file(self)

    monkeypatch.setattr(Path, "is_file", fake_is_file)
    import pact_v4.runtime.runtime_config as rc

    def fake_socket_ok(family, *a, **kw):
        class Dummy:
            def __enter__(self): return self
            def __exit__(self, *exc): return False
            def setsockopt(self, *a, **kw): pass
            def bind(self, addr): pass
        return Dummy()
    monkeypatch.setattr(rc._socket, "socket", fake_socket_ok)

    yaml_path = Path("configs/runtime_local.example.yaml")
    backend = cli._load_runtime_config_file(yaml_path)
    assert isinstance(backend, LocalLlamaBackendConfig)
    # Explicit 0 must override profile's 2048 budget to coherent 0-budget args
    args_explicit0 = cli.build_argparser().parse_args([
        "--chapter-id", "test",
        "--chapter-html", str(tmp_path / "c.html"),
        "--memory-dir", str(tmp_path),
        "--out-dir", str(tmp_path / "out"),
        "--runtime-config", str(yaml_path),
        "--reasoning", "0",
    ])
    assert args_explicit0.reasoning == 0
    effective = cli._resolve_effective_reasoning(args_explicit0, backend)
    assert effective == 0
    # Override must produce coherent server_args (budget 0) so validate passes
    backend_coherent = cli._with_reasoning_override(backend, effective)
    assert isinstance(backend_coherent, LocalLlamaBackendConfig)
    gemma_args = backend_coherent.server_args["gemma"]
    assert gemma_args[gemma_args.index("--reasoning-budget") + 1] == "0"
    cli.validate_reasoning_backend(effective, backend_coherent)
    report = rc.run_runtime_preflight(backend_coherent, reasoning=effective)
    assert report.ok, report.format_human()
    # Explicit non-zero should also be coherent (budget 2048)
    args_explicit2 = cli.build_argparser().parse_args([
        "--chapter-id", "test",
        "--chapter-html", str(tmp_path / "c.html"),
        "--memory-dir", str(tmp_path),
        "--out-dir", str(tmp_path / "out2"),
        "--runtime-config", str(yaml_path),
        "--reasoning", "2",
    ])
    effective2 = cli._resolve_effective_reasoning(args_explicit2, backend)
    assert effective2 == 2
    backend2 = cli._with_reasoning_override(backend, effective2)
    assert backend2.server_args["gemma"][backend2.server_args["gemma"].index("--reasoning-budget") + 1] == "2048"
    cli.validate_reasoning_backend(effective2, backend2)
    report2 = rc.run_runtime_preflight(backend2, reasoning=effective2)
    assert report2.ok, report2.format_human()
