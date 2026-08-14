"""Tests for the providers registry + --translator/--reviewer role mapping.

PROVIDERS-REGISTRY card (owner decision 2026-08-14): providers.yaml is the
model catalog (alias -> full provider/model ref + reasoning contract); the
CLI flags ``--translator <provider>/<alias>`` / ``--reviewer
<provider>/<alias>`` bind the resolved models to roles:

* ``--translator`` -> generator + repair (repair falls back to generator);
* ``--reviewer``   -> qwen_audit + fidelity_reviewer + russian_selector
  + entity_extractor (all audit roles).

Defaults are unchanged when the flags are absent. Changing a flag changes
the backend identity (the model/provider refs enter the descriptor), so
cache/resume is not replayed.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from pact_v4.runtime.opencode_backend import OpenCodeServerBackendConfig
from pact_v4.runtime.runtime_config import (
    OpenCodeBackendConfig,
    ProviderModel,
    ProvidersRegistry,
    build_reasoning_effort_map,
    load_providers_registry,
    nearest_declared_effort,
    apply_role_models,
    apply_provider_flags,
)

REGISTRY_YAML = """\
providers:
  opencode-go:
    kind: opencode_server
    models:
      deepseek4flash:
        ref: opencode-go/deepseek-v4-flash
        reasoning_contract:
          variants: [low, high, max]
      qwen37:
        ref: opencode-go/qwen3.7-plus
        reasoning_contract:
          variants: [high, max]
  openai:
    kind: opencode_server
    models:
      luna:
        ref: openai/gpt-5.6-luna
        reasoning_contract:
          variants: [none, low, medium, high, xhigh, max]
      sol:
        ref: openai/gpt-5.6-sol
        reasoning_contract:
          variants: [none, low, medium, high, xhigh, max]
      terra:
        ref: openai/gpt-5.6-terra
        reasoning_contract:
          variants: [none, low, medium, high, xhigh, max]
"""


@pytest.fixture()
def registry_path(tmp_path: Path) -> Path:
    path = tmp_path / "providers.yaml"
    path.write_text(REGISTRY_YAML, encoding="utf-8")
    return path


@pytest.fixture()
def registry(registry_path: Path) -> ProvidersRegistry:
    return load_providers_registry(registry_path)


def _remote_cfg() -> OpenCodeBackendConfig:
    return OpenCodeBackendConfig(
        server=OpenCodeServerBackendConfig(
            base_url="http://127.0.0.1:4096",
            username="pact",
            password="secret",
            model_bindings={
                "generator": "opencode-go/deepseek-v4-flash",
                "fidelity_reviewer": "opencode-go/qwen3.7-plus",
                "russian_selector": "opencode-go/qwen3.7-plus",
                "qwen_audit": "opencode-go/qwen3.7-plus",
                "gemma_audit": "opencode-go/deepseek-v4-flash",
                "repair": "opencode-go/deepseek-v4-flash",
            },
            structured_output_mode="prompt_only",
        )
    )


# ---------------------------------------------------------------------------
# Registry loading
# ---------------------------------------------------------------------------


def test_load_registry_resolves_aliases(registry: ProvidersRegistry):
    assert registry.resolve("opencode-go/deepseek4flash").ref == (
        "opencode-go/deepseek-v4-flash"
    )
    assert registry.resolve("openai/luna").ref == "openai/gpt-5.6-luna"
    assert registry.resolve("opencode-go/deepseek4flash").reasoning_variants == (
        "low", "high", "max",
    )


def test_load_registry_missing_file(tmp_path: Path):
    with pytest.raises(ValueError, match="providers.yaml"):
        load_providers_registry(tmp_path / "nope.yaml")


def test_load_registry_rejects_flat_model_entry(registry_path: Path):
    # Every model must carry its reasoning contract (acceptance: adding a
    # model REQUIRES variants verification + reasoning_contract fixation).
    registry_path.write_text(
        "providers:\n"
        "  opencode-go:\n"
        "    kind: opencode_server\n"
        "    models:\n"
        "      flat: opencode-go/deepseek-v4-flash\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="must be a mapping"):
        load_providers_registry(registry_path)


def test_load_registry_requires_reasoning_variants(registry_path: Path):
    registry_path.write_text(
        "providers:\n"
        "  opencode-go:\n"
        "    kind: opencode_server\n"
        "    models:\n"
        "      bare:\n"
        "        ref: opencode-go/deepseek-v4-flash\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="reasoning_contract.variants"):
        load_providers_registry(registry_path)


def test_load_registry_rejects_unknown_variant(registry_path: Path):
    registry_path.write_text(
        "providers:\n"
        "  opencode-go:\n"
        "    kind: opencode_server\n"
        "    models:\n"
        "      bad:\n"
        "        ref: opencode-go/deepseek-v4-flash\n"
        "        reasoning_contract:\n"
        "          variants: [turbo]\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="unknown reasoning variant"):
        load_providers_registry(registry_path)


def test_load_registry_rejects_unsupported_kind(registry_path: Path):
    registry_path.write_text(
        "providers:\n"
        "  opencode-go:\n"
        "    kind: codex_cli\n"
        "    models:\n"
        "      x:\n"
        "        ref: opencode-go/deepseek-v4-flash\n"
        "        reasoning_contract:\n"
        "          variants: [low]\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="unsupported kind"):
        load_providers_registry(registry_path)


def test_resolve_unknown_provider_and_alias(registry: ProvidersRegistry):
    with pytest.raises(ValueError, match="unknown provider"):
        registry.resolve("nope/luna")
    with pytest.raises(ValueError, match="unknown alias"):
        registry.resolve("openai/nope")


def test_resolve_rejects_dash_and_bare_spec(registry: ProvidersRegistry):
    # Dash separator is ambiguous (model ids contain dashes); only slash.
    with pytest.raises(ValueError, match="provider.*alias"):
        registry.resolve("opencode-go-deepseek4flash")
    with pytest.raises(ValueError, match="provider.*alias"):
        registry.resolve("opencode-go")


# ---------------------------------------------------------------------------
# Reasoning contract mapping (--reasoning N -> reasoningEffort)
# ---------------------------------------------------------------------------


def test_nearest_declared_effort_deepseek():
    # deepseek-v4-flash declares {low, high, max}: canonical medium (2) is
    # not declared -> nearest DECLARED is high (spec example 2->high).
    assert nearest_declared_effort(1, ("low", "high", "max")) == "low"
    assert nearest_declared_effort(2, ("low", "high", "max")) == "high"
    assert nearest_declared_effort(3, ("low", "high", "max")) == "high"


def test_build_reasoning_effort_map_deepseek(registry: ProvidersRegistry):
    model = registry.resolve("opencode-go/deepseek4flash")
    assert build_reasoning_effort_map(model) == {1: "low", 2: "high", 3: "high"}


def test_build_reasoning_effort_map_luna(registry: ProvidersRegistry):
    # luna declares every canonical value -> default map (unchanged wire).
    model = registry.resolve("openai/luna")
    assert build_reasoning_effort_map(model) == {1: "low", 2: "medium", 3: "high"}


def test_build_reasoning_effort_map_qwen(registry: ProvidersRegistry):
    # qwen3.7-plus declares {high, max}: every level clamps to the nearest
    # declared variant (high for 1/2, high for 3).
    model = registry.resolve("opencode-go/qwen37")
    assert build_reasoning_effort_map(model) == {1: "high", 2: "high", 3: "high"}


# ---------------------------------------------------------------------------
# Role mapping / apply_provider_flags
# ---------------------------------------------------------------------------


def test_no_flags_leaves_config_untouched():
    cfg = _remote_cfg()
    registry = ProvidersRegistry(providers={})
    assert apply_provider_flags(cfg, registry).identity_hash == cfg.identity_hash


def test_translator_flag_binds_generator_and_repair(registry: ProvidersRegistry):
    cfg = _remote_cfg()
    applied = apply_provider_flags(cfg, registry, translator="opencode-go/deepseek4flash")
    bindings = applied.server.model_bindings
    assert bindings["generator"] == "opencode-go/deepseek-v4-flash"
    assert bindings["repair"] == "opencode-go/deepseek-v4-flash"
    # Audit roles stay on the default reviewer model.
    assert bindings["qwen_audit"] == "opencode-go/qwen3.7-plus"


def test_reviewer_flag_binds_all_audit_roles(registry: ProvidersRegistry):
    cfg = _remote_cfg()
    applied = apply_provider_flags(cfg, registry, reviewer="openai/luna")
    bindings = applied.server.model_bindings
    assert bindings["qwen_audit"] == "openai/gpt-5.6-luna"
    assert bindings["fidelity_reviewer"] == "openai/gpt-5.6-luna"
    assert bindings["russian_selector"] == "openai/gpt-5.6-luna"
    assert bindings["entity_extractor"] == "openai/gpt-5.6-luna"
    # Generator/repair/gemma_audit stay on the default translator model.
    assert bindings["generator"] == "opencode-go/deepseek-v4-flash"
    assert bindings["gemma_audit"] == "opencode-go/deepseek-v4-flash"


def test_both_flags_apply_and_change_identity(registry: ProvidersRegistry):
    cfg = _remote_cfg()
    applied = apply_provider_flags(
        cfg, registry, translator="opencode-go/deepseek4flash", reviewer="openai/luna"
    )
    assert applied.server.model_bindings["generator"] == "opencode-go/deepseek-v4-flash"
    assert applied.server.model_bindings["qwen_audit"] == "openai/gpt-5.6-luna"
    # Changing a flag = new backend identity (cache not replayed).
    assert applied.identity_hash != cfg.identity_hash
    record = applied.public_record()
    assert "opencode-go/deepseek-v4-flash" in str(record)
    assert "openai/gpt-5.6-luna" in str(record)


def test_translator_flag_sets_generator_reasoning_effort_map(
    registry: ProvidersRegistry,
):
    cfg = _remote_cfg()
    applied = apply_provider_flags(cfg, registry, translator="opencode-go/deepseek4flash")
    # deepseek contract {low, high, max} -> level 2 maps to high.
    assert applied.server.reasoning_effort_map == {1: "low", 2: "high", 3: "high"}


def test_reviewer_only_does_not_touch_reasoning_map(registry: ProvidersRegistry):
    cfg = _remote_cfg()
    applied = apply_provider_flags(cfg, registry, reviewer="openai/luna")
    assert applied.server.reasoning_effort_map is None


def test_reasoning_effort_map_is_identity_bearing(registry: ProvidersRegistry):
    # PROVIDERS-REGISTRY identity rule: a contract-derived effort map changes
    # the wire value, so it MUST change backend identity (cache not replayed).
    cfg = _remote_cfg()
    applied = apply_provider_flags(cfg, registry, translator="opencode-go/deepseek4flash")
    assert applied.identity_hash != cfg.identity_hash
    assert "reasoning_effort_map" in applied.public_record()["effective_options"]


def test_apply_role_models_rejects_local_backend():
    from pact_v4.runtime.runtime_config import LocalLlamaBackendConfig

    local = LocalLlamaBackendConfig(
        exe=Path("C:/fake/llama-server.exe"), device="FAKE0", host="127.0.0.1",
        model_paths={"gemma": Path("C:/fake/gemma.gguf")},
        model_names={"gemma": "gemma-fake"},
        server_args={"gemma": []},
    )
    with pytest.raises(ValueError, match="local_llama"):
        apply_role_models(local, {"generator": "opencode-go/deepseek-v4-flash"})


def test_provider_model_validation():
    with pytest.raises(ValueError, match="provider/model"):
        ProviderModel(ref="no-slash", reasoning_variants=("low",))
    with pytest.raises(ValueError, match="reasoning_variants"):
        ProviderModel(ref="opencode-go/x", reasoning_variants=())
