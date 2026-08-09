"""Tests for tagged runtime backend configs (V4 C2, plan §8/§9.2/§11).

Focus: backend identity semantics — everything that can change the model
answer is in ``identity_hash``, credentials are never (API key rotation
must not invalidate resume), the routing map of a composite profile is part
of identity, and the loader records secret *references* (env var names),
never values.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from pact_v4.runtime.backend_protocol import KIND_COMPOSITE, KIND_LOCAL_LLAMA
from pact_v4.runtime.opencode_backend import OpenCodeServerBackendConfig
from pact_v4.runtime.runtime_config import (
    CompositeBackendConfig,
    LocalLlamaBackendConfig,
    OpenCodeBackendConfig,
    load_runtime_config,
    validate_reasoning_backend,
)


def _local() -> LocalLlamaBackendConfig:
    return LocalLlamaBackendConfig(
        exe=Path("C:/fake/llama-server.exe"), device="FAKE0", host="127.0.0.1",
        model_paths={"gemma": Path("C:/fake/gemma.gguf"), "qwen": Path("C:/fake/qwen.gguf")},
        model_names={"gemma": "gemma-fake", "qwen": "qwen-fake"},
        server_args={"gemma": ["-ngl", "99"], "qwen": []},
        port=8093,
    )


def _remote(
    *,
    password: str = "secret-A",
    model: str = "opencode-go/deepseek-v4-flash",
    structured_output_mode: str = "prompt_only",
) -> OpenCodeBackendConfig:
    return OpenCodeBackendConfig(
        server=OpenCodeServerBackendConfig(
            base_url="http://127.0.0.1:4096",
            username="pact",
            password=password,
            model_bindings={
                "generator": model,
                "fidelity_reviewer": "opencode-go/qwen3.7-plus",
                "russian_selector": "opencode-go/qwen3.7-plus",
            },
            structured_output_mode=structured_output_mode,
        )
    )


# ---------------------------------------------------------------------------
# Local config
# ---------------------------------------------------------------------------


def test_local_identity_hash_is_stable_and_port_independent():
    a = _local()
    b = _local()
    assert a.identity_hash == b.identity_hash
    # Changing the local TCP port must not change identity (does not change
    # the served model).
    moved = LocalLlamaBackendConfig(
        exe=a.exe, device=a.device, host=a.host, model_paths=a.model_paths,
        model_names=a.model_names, server_args=a.server_args, port=9999,
    )
    assert moved.identity_hash == a.identity_hash
    # Changing a model file must change identity.
    other_model = LocalLlamaBackendConfig(
        exe=a.exe, device=a.device, host=a.host,
        model_paths={"gemma": Path("C:/fake/gemma2.gguf"), "qwen": Path("C:/fake/qwen.gguf")},
        model_names=a.model_names, server_args=a.server_args,
    )
    assert other_model.identity_hash != a.identity_hash


def test_local_acceptable_identity_hashes_include_legacy_hash():
    cfg = _local()
    hashes = list(cfg.acceptable_identity_hashes())
    assert cfg.identity_hash in hashes
    assert cfg.legacy_identity_hash in hashes
    assert len(hashes) == len(set(hashes))


def test_local_public_record_and_descriptor_have_no_secrets():
    cfg = _local()
    record = cfg.public_record()
    assert record["kind"] == KIND_LOCAL_LLAMA
    blob = str(record)
    assert "password" not in blob.lower()
    assert "api_key" not in blob.lower()
    descriptor = cfg.build_descriptor()
    assert descriptor.kind == KIND_LOCAL_LLAMA
    assert descriptor.identity_hash == cfg.identity_hash


def test_local_config_profile_name_is_gemma_model_name():
    assert _local().config_profile_name() == "gemma-fake"


# ---------------------------------------------------------------------------
# OpenCode config
# ---------------------------------------------------------------------------


def test_opencode_identity_excludes_credentials():
    a = _remote(password="secret-A")
    b = _remote(password="secret-B")
    assert a.identity_hash == b.identity_hash
    assert a.public_record()["identity_hash"] == b.public_record()["identity_hash"]


def test_opencode_identity_changes_with_model_binding():
    a = _remote(model="opencode-go/deepseek-v4-flash")
    b = _remote(model="opencode-go/qwen3.7-plus")
    assert a.identity_hash != b.identity_hash


def test_opencode_identity_changes_with_structured_output_policy():
    a = _remote(structured_output_mode="prompt_only")
    b = _remote(structured_output_mode="json_schema")
    assert a.identity_hash != b.identity_hash


def test_opencode_server_mode_validation():
    server = OpenCodeServerBackendConfig(
        base_url="http://127.0.0.1:4096", model_bindings={"generator": "opencode-go/x"},
    )
    assert OpenCodeBackendConfig(server=server, server_mode="external").server_mode == "external"
    try:
        OpenCodeBackendConfig(server=server, server_mode="bogus")
    except ValueError as exc:
        assert "server_mode" in str(exc)
    else:
        raise AssertionError("expected server_mode ValueError")


def test_opencode_managed_requires_matching_port():
    server = OpenCodeServerBackendConfig(
        base_url="http://127.0.0.1:4096", model_bindings={"generator": "opencode-go/x"},
    )
    # A managed spec with a different port would make backend identity and
    # the actual endpoint disagree -> fail loudly.
    from pact_v4.runtime.opencode_server_lifecycle import ManagedServerSpec

    try:
        OpenCodeBackendConfig(
            server=server,
            server_mode="managed",
            managed=ManagedServerSpec(hostname="127.0.0.1", port=5000),
        )
    except ValueError as exc:
        assert "does not match" in str(exc)
    else:
        raise AssertionError("expected managed port mismatch ValueError")


def test_opencode_managed_default_spec_derived_from_base_url():
    server = OpenCodeServerBackendConfig(
        base_url="http://127.0.0.1:5555", model_bindings={"generator": "opencode-go/x"},
    )
    cfg = OpenCodeBackendConfig(server=server, server_mode="managed")
    assert cfg.managed is not None
    assert cfg.managed.port == 5555


def test_opencode_config_profile_name_uses_generator_binding():
    assert _remote().config_profile_name() == "opencode-go/deepseek-v4-flash"


# ---------------------------------------------------------------------------
# Composite config
# ---------------------------------------------------------------------------


def test_composite_identity_changes_with_routing_map():
    remote = _remote()
    backends = {"local": _local(), "remote": remote}
    map_a = {
        "generator": "remote",
        "fidelity_reviewer": "remote",
        "russian_selector": "local",
    }
    map_b = {
        "generator": "remote",
        "fidelity_reviewer": "remote",
        "russian_selector": "remote",
    }
    composite_a = CompositeBackendConfig(backends=backends, role_backend_map=map_a)
    composite_b = CompositeBackendConfig(backends=backends, role_backend_map=map_b)
    assert composite_a.identity_hash != composite_b.identity_hash
    # Routing map is part of the descriptor identity too.
    assert composite_a.build_descriptor().kind == KIND_COMPOSITE
    assert composite_a.build_descriptor().identity_hash == composite_a.identity_hash


def test_composite_rejects_unknown_backend_routing():
    try:
        CompositeBackendConfig(
            backends={"local": _local()},
            role_backend_map={"generator": "nope"},
        )
    except ValueError as exc:
        assert "unknown backend" in str(exc)
    else:
        raise AssertionError("expected unknown backend ValueError")


def test_composite_build_runtime_constructs_offline_and_closes():
    backends = {"local": _local(), "remote": _remote()}
    composite = CompositeBackendConfig(
        backends=backends,
        role_backend_map={
            "generator": "remote", "fidelity_reviewer": "remote",
            "russian_selector": "local",
        },
    )
    # Constructing the composite runtime must not touch the network or spawn
    # any process; close() tears both sub-runtimes down safely.
    runtime = composite.build_runtime(log_dir=Path("C:/fake/logs"))
    assert runtime.backend_descriptor.identity_hash == composite.identity_hash
    runtime.close()


# ---------------------------------------------------------------------------
# Loader
# ---------------------------------------------------------------------------


def test_load_local_config():
    payload = {
        "kind": "local_llama",
        "exe": "C:/llama/llama-server.exe",
        "device": "SYCL0",
        "model_paths": {"gemma": "C:/m/gemma.gguf"},
        "model_names": {"gemma": "gemma"},
        "server_args": {"gemma": ["-c", "32768"]},
        "port": 8093,
    }
    cfg = load_runtime_config(payload)
    assert isinstance(cfg, LocalLlamaBackendConfig)
    assert cfg.identity_hash


def test_load_opencode_config_records_env_refs_only() -> None:
    payload = {
        "kind": "opencode_server",
        "base_url": "http://127.0.0.1:4096",
        "server_mode": "external",
        "auth": {
            "type": "basic_env",
            "username_env": "MY_USERNAME_ENV",
            "password_env": "MY_PASSWORD_ENV",
        },
        "model_bindings": {"generator": "opencode-go/deepseek-v4-flash"},
    }
    cfg = load_runtime_config(payload)
    assert isinstance(cfg, OpenCodeBackendConfig)
    assert cfg.server.username_env == "MY_USERNAME_ENV"
    assert cfg.server.password_env == "MY_PASSWORD_ENV"
    # Secret *values* are never present in the config.
    assert cfg.server.username is None
    assert cfg.server.password is None


def test_load_opencode_remote_budget_parsed_from_yaml() -> None:
    # B11: ``remote_budget`` is now a runtime-YAML field like the other
    # OpenCodeServerBackendConfig options, not a code-only default.
    payload = {
        "kind": "opencode_server",
        "base_url": "http://127.0.0.1:4096",
        "server_mode": "external",
        "model_bindings": {"generator": "opencode-go/deepseek-v4-flash"},
        "remote_budget": {
            "max_requests_per_chapter": 700,
            "max_retry_requests_per_chapter": 3,
            "max_wait_seconds_on_rate_limit": 60.0,
        },
    }
    cfg = load_runtime_config(payload)
    assert isinstance(cfg, OpenCodeBackendConfig)
    assert cfg.server.remote_budget.max_requests_per_chapter == 700
    assert cfg.server.remote_budget.max_retry_requests_per_chapter == 3
    assert cfg.server.remote_budget.max_wait_seconds_on_rate_limit == 60.0
    # The budget participates in backend identity: a raised budget must
    # produce a different identity than the default-500 config.
    default_cfg = load_runtime_config(
        {k: v for k, v in payload.items() if k != "remote_budget"}
    )
    assert default_cfg.identity_hash != cfg.identity_hash


def test_load_opencode_remote_budget_defaults_to_500() -> None:
    # B11: without a ``remote_budget`` block the loaded config carries the
    # raised default (500), so the runtime YAML and the code default agree.
    payload = {
        "kind": "opencode_server",
        "base_url": "http://127.0.0.1:4096",
        "server_mode": "external",
        "model_bindings": {"generator": "opencode-go/deepseek-v4-flash"},
    }
    cfg = load_runtime_config(payload)
    assert isinstance(cfg, OpenCodeBackendConfig)
    assert cfg.server.remote_budget.max_requests_per_chapter == 500


def test_load_opencode_remote_budget_partial_block_keeps_other_defaults() -> None:
    # Only the overridden field changes; the rest fall back to the class
    # defaults (B11 identity stability for a single-field budget block).
    payload = {
        "kind": "opencode_server",
        "base_url": "http://127.0.0.1:4096",
        "server_mode": "external",
        "model_bindings": {"generator": "opencode-go/deepseek-v4-flash"},
        "remote_budget": {"max_requests_per_chapter": 600},
    }
    cfg = load_runtime_config(payload)
    assert cfg.server.remote_budget.max_requests_per_chapter == 600
    assert cfg.server.remote_budget.max_retry_requests_per_chapter == 10
    assert cfg.server.remote_budget.max_wait_seconds_on_rate_limit == 900.0


def test_load_opencode_remote_budget_non_positive_rejected() -> None:
    # B11: a <= 0 budget must fail loudly at load time, not after the run
    # starts burning requests (RemoteBudget.__post_init__ contract).
    payload = {
        "kind": "opencode_server",
        "base_url": "http://127.0.0.1:4096",
        "server_mode": "external",
        "model_bindings": {"generator": "opencode-go/deepseek-v4-flash"},
        "remote_budget": {"max_requests_per_chapter": 0},
    }
    try:
        load_runtime_config(payload)
    except ValueError as exc:
        assert "max_requests_per_chapter must be positive" in str(exc)
    else:
        raise AssertionError("expected a positive-budget ValueError")


def test_load_opencode_remote_budget_scalar_rejected() -> None:
    # B11-RV: an explicit scalar block (e.g. ``remote_budget: 700``) is a
    # malformed budget, not an absent one; it must fail loudly instead of
    # silently running with the 500 default and a different identity.
    payload = {
        "kind": "opencode_server",
        "base_url": "http://127.0.0.1:4096",
        "server_mode": "external",
        "model_bindings": {"generator": "opencode-go/deepseek-v4-flash"},
        "remote_budget": 700,
    }
    try:
        load_runtime_config(payload)
    except ValueError as exc:
        assert "remote_budget" in str(exc)
        assert "mapping" in str(exc)
    else:
        raise AssertionError("expected a remote_budget-shape ValueError")


def test_load_opencode_remote_budget_list_rejected() -> None:
    payload = {
        "kind": "opencode_server",
        "base_url": "http://127.0.0.1:4096",
        "server_mode": "external",
        "model_bindings": {"generator": "opencode-go/deepseek-v4-flash"},
        "remote_budget": [700, 10],
    }
    try:
        load_runtime_config(payload)
    except ValueError as exc:
        assert "remote_budget" in str(exc)
        assert "mapping" in str(exc)
    else:
        raise AssertionError("expected a remote_budget-shape ValueError")


def test_load_opencode_remote_budget_bool_rejected() -> None:
    payload = {
        "kind": "opencode_server",
        "base_url": "http://127.0.0.1:4096",
        "server_mode": "external",
        "model_bindings": {"generator": "opencode-go/deepseek-v4-flash"},
        "remote_budget": True,
    }
    try:
        load_runtime_config(payload)
    except ValueError as exc:
        assert "remote_budget" in str(exc)
        assert "mapping" in str(exc)
    else:
        raise AssertionError("expected a remote_budget-shape ValueError")


def test_load_opencode_remote_budget_null_keeps_default_500() -> None:
    # B11-RV: ``null`` is equivalent to an absent key — both keep the
    # dataclass default (500), never a rejection.
    payload = {
        "kind": "opencode_server",
        "base_url": "http://127.0.0.1:4096",
        "server_mode": "external",
        "model_bindings": {"generator": "opencode-go/deepseek-v4-flash"},
        "remote_budget": None,
    }
    cfg = load_runtime_config(payload)
    assert isinstance(cfg, OpenCodeBackendConfig)
    assert cfg.server.remote_budget.max_requests_per_chapter == 500


def test_load_composite_config():
    payload = {
        "kind": "composite",
        "backends": {
            "local": {
                "kind": "local_llama",
                "exe": "C:/llama/llama-server.exe",
                "model_paths": {"gemma": "C:/m/gemma.gguf"},
                "model_names": {"gemma": "gemma"},
                "server_args": {},
            },
            "remote": {
                "kind": "opencode_server",
                "base_url": "http://127.0.0.1:4096",
                "model_bindings": {"generator": "opencode-go/deepseek-v4-flash"},
            },
        },
        "role_backend_map": {"generator": "remote", "russian_selector": "local"},
    }
    cfg = load_runtime_config(payload)
    assert isinstance(cfg, CompositeBackendConfig)
    assert cfg.identity_hash


def test_load_unknown_kind_raises():
    try:
        load_runtime_config({"kind": "quantum"})
    except ValueError as exc:
        assert "unknown kind" in str(exc)
    else:
        raise AssertionError("expected unknown kind ValueError")


# ---------------------------------------------------------------------------
# Example runtime profiles (V4 C3)
# ---------------------------------------------------------------------------


def _repo_root() -> Path:
    return Path(__file__).parents[3]


def test_example_local_config_loads_with_string_server_args():
    """Regression: ``configs/runtime_local.example.yaml`` crashed at model start.

    The unquoted YAML 1.1 bool words ``-fa on`` / ``-fit on`` parsed as
    booleans and flowed into ``subprocess.Popen`` -> ``list2cmdline`` ->
    ``TypeError: expected str, bytes or os.PathLike object, not bool``. The
    example now quotes them; the loader must produce string-only server_args.
    """
    yaml = pytest.importorskip("yaml")
    path = _repo_root() / "configs" / "runtime_local.example.yaml"
    cfg = load_runtime_config(yaml.safe_load(path.read_text(encoding="utf-8")))
    assert isinstance(cfg, LocalLlamaBackendConfig)
    for args in cfg.server_args.values():
        assert all(isinstance(a, str) for a in args), args


def test_example_composite_config_loads_with_string_server_args():
    """Same regression for the composite profile's local sub-backends."""
    yaml = pytest.importorskip("yaml")
    path = _repo_root() / "configs" / "runtime_composite.example.yaml"
    cfg = load_runtime_config(yaml.safe_load(path.read_text(encoding="utf-8")))
    assert isinstance(cfg, CompositeBackendConfig)
    for sub in cfg.backends.values():
        if isinstance(sub, LocalLlamaBackendConfig):
            for args in sub.server_args.values():
                assert all(isinstance(a, str) for a in args), args


def test_example_remote_config_loads_remote_budget_500():
    """B11: the remote example carries the identity-bound budget block, and
    the loader must surface it as the effective per-chapter budget."""
    yaml = pytest.importorskip("yaml")
    path = _repo_root() / "configs" / "runtime_remote.example.yaml"
    cfg = load_runtime_config(yaml.safe_load(path.read_text(encoding="utf-8")))
    assert isinstance(cfg, OpenCodeBackendConfig)
    assert cfg.server.remote_budget.max_requests_per_chapter == 500


def test_local_config_rejects_non_string_server_args():
    """Non-string server_args fail loudly at construction, not inside Popen."""
    try:
        LocalLlamaBackendConfig(
            exe=Path("C:/fake/llama-server.exe"), device="FAKE0", host="127.0.0.1",
            model_paths={"gemma": Path("C:/fake/gemma.gguf")},
            model_names={"gemma": "gemma"},
            server_args={"gemma": ["-fa", True]},
        )
    except ValueError as exc:
        assert "server_args must contain only strings" in str(exc)
    else:
        raise AssertionError("expected non-string server_args ValueError")


# ---------------------------------------------------------------------------
# V4.1 reasoning/backend compatibility policy (review commit 301e9df)
# ---------------------------------------------------------------------------


def test_validate_reasoning_backend_zero_is_baseline_for_local():
    # reasoning=0 (B1 baseline) is always accepted: it emits no
    # request_options at all, so every backend works unchanged.
    validate_reasoning_backend(0, _local())


def test_validate_reasoning_backend_accepts_nonzero_for_local():
    # V4.1 A2 (owner-verified 2026-08-08): the local llama-server
    # transport receives the reasoning budget from the SERVER ARGS
    # (--reasoning-budget 2048, plan §3.4), not from request_options —
    # so --reasoning > 0 with a local generator is a supported path and
    # must NOT fail fast. LocalOpenAIBackend still rejects request_options
    # as a library-level guard; the CLI local path never emits them.
    for level in (1, 2, 3):
        validate_reasoning_backend(level, _local())


def test_validate_reasoning_backend_accepts_nonzero_for_opencode():
    # The OpenCode backend maps 1/2/3 -> reasoningEffort low/medium/high;
    # nonzero reasoning with a remote generator is the supported path.
    for level in (1, 2, 3):
        validate_reasoning_backend(level, _remote())


def test_validate_reasoning_backend_composite_follows_generator_routing():
    # The check follows the composite's *generator* role routing: A2
    # accepts nonzero reasoning on every generator transport (remote via
    # request_options, local via server args).
    backends = {"remote": _remote(), "local": _local()}
    remote_gen = CompositeBackendConfig(
        backends=backends,
        role_backend_map={"generator": "remote", "fidelity_reviewer": "remote"},
    )
    for level in (1, 2, 3):
        validate_reasoning_backend(level, remote_gen)

    local_gen = CompositeBackendConfig(
        backends=backends,
        role_backend_map={"generator": "local", "fidelity_reviewer": "remote"},
    )
    for level in (1, 2, 3):
        validate_reasoning_backend(level, local_gen)


def test_validate_reasoning_backend_composite_first_wins_without_generator_route():
    # Without an explicit generator routing the composite descriptor binds
    # the generator role to the FIRST sub-backend declaring it — the
    # reasoning policy must mirror that first-wins rule. A2: every
    # generator transport accepts nonzero reasoning.
    remote = _remote()
    local = _local()
    remote_first = CompositeBackendConfig(
        backends={"remote": remote, "local": local},
        role_backend_map={"fidelity_reviewer": "remote"},  # no generator key
    )
    for level in (1, 2, 3):
        validate_reasoning_backend(level, remote_first)

    local_first = CompositeBackendConfig(
        backends={"local": local, "remote": remote},
        role_backend_map={"fidelity_reviewer": "remote"},  # no generator key
    )
    for level in (1, 2, 3):
        validate_reasoning_backend(level, local_first)
