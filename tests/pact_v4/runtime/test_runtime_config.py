"""Tests for tagged runtime backend configs (V4 C2, plan §8/§9.2/§11).

Focus: backend identity semantics — everything that can change the model
answer is in ``identity_hash``, credentials are never (API key rotation
must not invalidate resume), the routing map of a composite profile is part
of identity, and the loader records secret *references* (env var names),
never values.
"""
from __future__ import annotations

from pathlib import Path

from pact_v4.runtime.backend_protocol import KIND_COMPOSITE, KIND_LOCAL_LLAMA
from pact_v4.runtime.opencode_backend import OpenCodeServerBackendConfig
from pact_v4.runtime.runtime_config import (
    CompositeBackendConfig,
    LocalLlamaBackendConfig,
    OpenCodeBackendConfig,
    load_runtime_config,
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


def test_load_opencode_config_records_env_refs_only():
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
