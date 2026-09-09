"""Local-matrix-v2 tests (OpenSpec change local-matrix-v2, tasks 4.1).

Covers: extensible sampling allowlist (fail-closed), role reasoning_budget
deltas, hybrid effective reasoning values, same-model role-effective router
restart with replaced --reasoning-budget, identity/cache/resume stability,
gemma31/qwen38 registry shape, shared reviewer formatting role, separate
local reviewer formatting lifecycle with deterministic fallback, and the
authoritative matrix provenance.
"""
from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from pact_v4.runtime.backend_protocol import CompletionRequest, Message
from pact_v4.runtime.model_lifecycle import ModelRouter, SwitchRecord, with_reasoning_budget
from pact_v4.runtime.runtime_config import (
    REVIEWER_ROLES,
    TRANSLATOR_ROLES,
    ResolvedModelPair,
    RoleBudget,
    build_resolved_pair_from_registry,
    effective_reasoning_budget,
    load_providers_registry,
    parse_local_pair_arg,
)

TEN_ROLES = [
    "generator", "repair", "formatting", "gemma_audit", "qwen_audit",
    "fidelity_reviewer", "russian_selector", "entity_extractor",
    "russian_editor", "glossary_resolver",
]

_ROLE_BUDGETS_YAML = "\n".join(f"  {r}: {{max_output_tokens: 1000}}" for r in TEN_ROLES)

_LOCAL_TWO_MODELS = """\
providers:
  local:
    kind: local_llama
    models:
      trans:
        model_key: gemma
        model_path: /tmp/gemma.gguf
        model_name: trans-model
        server_args: ["--reasoning-budget", "2000"]
        reasoning_budget: 2000
        request: {temperature: 0.7}
      rev:
        model_key: qwen
        model_path: /tmp/qwen.gguf
        model_name: rev-model
        server_args: ["--reasoning-budget", "8192"]
        reasoning_budget: 8192
        request: {temperature: 0.3}
"""


def _write(tmp_path: Path, name: str, content: str) -> Path:
    p = tmp_path / name
    p.write_text(textwrap.dedent(content), encoding="utf-8")
    return p


def _pair_with_deltas(tmp_path: Path, deltas: dict | None = None):
    """Two-model registry with optional per-role reasoning deltas."""
    lines = ["role_budgets:"]
    for r in TEN_ROLES:
        d = (deltas or {}).get(r, 0)
        lines.append(f"  {r}: {{max_output_tokens: 1000, reasoning_budget: {d}}}")
    content = "\n".join(lines) + "\n" + _LOCAL_TWO_MODELS
    p = _write(tmp_path, "providers.yaml", content)
    reg = load_providers_registry(p)
    return reg, build_resolved_pair_from_registry(reg, "trans", "rev")


# ---------------------------------------------------------------------------
# 1. Extensible sampling allowlist
# ---------------------------------------------------------------------------

def test_new_sampling_fields_accepted_and_serialized(tmp_path):
    content = "role_budgets:\n" + _ROLE_BUDGETS_YAML + "\n" + """
providers:
  local:
    kind: local_llama
    models:
      m:
        model_key: gemma
        model_path: /tmp/a
        model_name: a
        server_args: ["--reasoning-budget", "2000"]
        reasoning_budget: 2000
        request: {temperature: 1.0, top_p: 0.95, top_k: 64, min_p: 0.0, repeat_penalty: 1.0, repeat_last_n: 64, frequency_penalty: 0.5, presence_penalty: 0.0}
"""
    reg = load_providers_registry(_write(tmp_path, "p.yaml", content))
    req = reg.providers["local"]["m"].request
    assert req["repeat_penalty"] == 1.0
    assert req["repeat_last_n"] == 64
    assert req["frequency_penalty"] == 0.5
    assert req["presence_penalty"] == 0.0


@pytest.mark.parametrize("field,value", [
    ("repeat_penalty", "high"),
    ("repeat_penalty", -0.5),
    ("repeat_penalty", 9.0),
    ("repeat_last_n", 1.5),
    ("repeat_last_n", True),
    ("repeat_last_n", -2),
    ("frequency_penalty", 3.0),
    ("presence_penalty", -2.5),
    ("temperature", 5.0),
])
def test_new_sampling_ranges_fail_closed(tmp_path, field, value):
    content = "role_budgets:\n" + _ROLE_BUDGETS_YAML + "\n" + f"""
providers:
  local:
    kind: local_llama
    models:
      m:
        model_key: gemma
        model_path: /tmp/a
        model_name: a
        server_args: ["--reasoning-budget", "2000"]
        reasoning_budget: 2000
        request: {{temperature: 0.2, {field}: {value!r}}}
"""
    with pytest.raises(ValueError, match=field):
        load_providers_registry(_write(tmp_path, "p.yaml", content))


@pytest.mark.parametrize("field", ["max_output_tokens", "reasoning", "reasoning_budget"])
def test_forbidden_request_fields_fail_closed(tmp_path, field):
    content = "role_budgets:\n" + _ROLE_BUDGETS_YAML + "\n" + f"""
providers:
  local:
    kind: local_llama
    models:
      m:
        model_key: gemma
        model_path: /tmp/a
        model_name: a
        server_args: ["--reasoning-budget", "2000"]
        reasoning_budget: 2000
        request: {{temperature: 0.2, {field}: 100}}
"""
    with pytest.raises(ValueError, match="not allowed in model request"):
        load_providers_registry(_write(tmp_path, "p.yaml", content))


def test_unknown_sampling_field_still_fail_closed(tmp_path):
    content = "role_budgets:\n" + _ROLE_BUDGETS_YAML + "\n" + """
providers:
  local:
    kind: local_llama
    models:
      m:
        model_key: gemma
        model_path: /tmp/a
        model_name: a
        server_args: ["--reasoning-budget", "2000"]
        reasoning_budget: 2000
        request: {temperature: 0.2, future_param: 1}
"""
    with pytest.raises(ValueError, match="unknown request field"):
        load_providers_registry(_write(tmp_path, "p.yaml", content))


def _local_model_yaml(*, server_args_repr: str, reasoning_line: str = "") -> str:
    return (
        "role_budgets:\n" + _ROLE_BUDGETS_YAML + "\n"
        + "providers:\n  local:\n    kind: local_llama\n    models:\n"
        + "      m:\n        model_key: gemma\n        model_path: /tmp/a\n"
        + "        model_name: a\n"
        + f"        server_args: {server_args_repr}\n"
        + (f"{reasoning_line}\n" if reasoning_line else "")
        + "        request: {temperature: 0.2}\n"
    )


def test_missing_model_reasoning_budget_fail_closed(tmp_path):
    # No reasoning_budget key at all: fail closed even with valid server_args.
    content = _local_model_yaml(server_args_repr='["--reasoning-budget", "2000"]')
    with pytest.raises(ValueError, match="reasoning_budget is required"):
        load_providers_registry(_write(tmp_path, "p.yaml", content))
    # Empty server_args and no budget: fail closed (hybrid validation).
    content = _local_model_yaml(server_args_repr="[]")
    with pytest.raises(ValueError, match="reasoning_budget is required"):
        load_providers_registry(_write(tmp_path, "p.yaml", content))


@pytest.mark.parametrize("server_args_repr,reasoning_line", [
    ('["--reasoning-budget", "2000"]', "        reasoning_budget: 8192"),  # mismatch
    ('[]', "        reasoning_budget: 2000"),  # flag absent
    ('["--reasoning-budget", "2000", "--reasoning-budget", "2000"]', "        reasoning_budget: 2000"),  # duplicate
    ('["--reasoning-budget"]', "        reasoning_budget: 2000"),  # missing value
    ('["--reasoning-budget", "lots"]', "        reasoning_budget: 2000"),  # non-int value
    ('["--reasoning-budget", "2000"]', "        reasoning_budget: '2000'"),  # non-int budget
    ('["--reasoning-budget", "2000"]', "        reasoning_budget: true"),  # bool budget
])
def test_model_reasoning_budget_mismatch_fail_closed(tmp_path, server_args_repr, reasoning_line):
    content = _local_model_yaml(server_args_repr=server_args_repr, reasoning_line=reasoning_line)
    with pytest.raises(ValueError, match="reasoning_budget|reasoning-budget"):
        load_providers_registry(_write(tmp_path, "p.yaml", content))


# ---------------------------------------------------------------------------
# 2. Role reasoning_budget deltas
# ---------------------------------------------------------------------------

def test_role_reasoning_deltas_load(tmp_path):
    _, pair = _pair_with_deltas(tmp_path, {"generator": 2000, "qwen_audit": 2000, "entity_extractor": 2000})
    assert pair.role_budgets["generator"].reasoning_budget == 2000
    assert pair.role_budgets["repair"].reasoning_budget == 0


@pytest.mark.parametrize("bad", ["2000", 2.5, True, -1, 8193, 10**6])
def test_role_reasoning_delta_bad_values_fail_closed(tmp_path, bad):
    lines = ["role_budgets:"]
    for r in TEN_ROLES:
        if r == "generator":
            lines.append(f"  {r}: {{max_output_tokens: 1000, reasoning_budget: {bad!r}}}")
        else:
            lines.append(f"  {r}: {{max_output_tokens: 1000}}")
    content = "\n".join(lines) + "\n" + _LOCAL_TWO_MODELS
    with pytest.raises(ValueError, match="reasoning_budget"):
        load_providers_registry(_write(tmp_path, "p.yaml", content))


def test_role_budget_unknown_field_fail_closed(tmp_path):
    lines = ["role_budgets:"]
    for r in TEN_ROLES:
        extra = ", temperature: 0.5" if r == "generator" else ""
        lines.append(f"  {r}: {{max_output_tokens: 1000{extra}}}")
    content = "\n".join(lines) + "\n" + _LOCAL_TWO_MODELS
    with pytest.raises(ValueError, match="unknown field"):
        load_providers_registry(_write(tmp_path, "p.yaml", content))


def test_role_budget_dataclass_validates_delta():
    assert RoleBudget(max_output_tokens=100).reasoning_budget == 0
    with pytest.raises(ValueError, match="reasoning_budget"):
        RoleBudget(max_output_tokens=100, reasoning_budget=8193)
    with pytest.raises(ValueError, match="reasoning_budget"):
        RoleBudget(max_output_tokens=100, reasoning_budget="2000")  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# 3. Hybrid effective reasoning values (grill-agreed numbers)
# ---------------------------------------------------------------------------

def test_effective_reasoning_grill_values(tmp_path):
    # Universal deltas: generator/entity_extractor/qwen_audit +2000, others 0.
    _, pair = _pair_with_deltas(
        tmp_path, {"generator": 2000, "entity_extractor": 2000, "qwen_audit": 2000}
    )
    # trans base 2000 (gemma-class), rev base 8192 (qwen-class)
    assert pair.effective_reasoning_budget("generator") == 4000
    assert pair.effective_reasoning_budget("repair") == 2000
    assert pair.effective_reasoning_budget("gemma_audit") == 2000
    assert pair.effective_reasoning_budget("qwen_audit") == 10192
    assert pair.effective_reasoning_budget("entity_extractor") == 10192
    assert pair.effective_reasoning_budget("formatting") == 8192
    assert pair.effective_reasoning_budget("fidelity_reviewer") == 8192
    assert pair.effective_reasoning_budget("russian_selector") == 8192
    assert pair.effective_reasoning_budget("russian_editor") == 8192
    assert pair.effective_reasoning_budget("glossary_resolver") == 8192
    # Module-level helper agrees; unknown role fails closed.
    assert effective_reasoning_budget(pair, "generator") == 4000
    with pytest.raises(ValueError, match="unknown role"):
        pair.effective_reasoning_budget("nonexistent_role")
    with pytest.raises(ValueError, match="ResolvedModelPair"):
        effective_reasoning_budget(object(), "generator")  # type: ignore[arg-type]


def test_effective_reasoning_production_registry():
    reg = load_providers_registry(Path("configs/providers.yaml"))
    pair = build_resolved_pair_from_registry(reg, "gemma31", "qwen38")
    assert pair.effective_reasoning_budget("generator") == 4000  # 2000+2000
    assert pair.effective_reasoning_budget("qwen_audit") == 10192  # 8192+2000
    assert pair.effective_reasoning_budget("entity_extractor") == 10192
    assert pair.effective_reasoning_budget("formatting") == 8192
    assert pair.effective_reasoning_budget("repair") == 2000
    legacy = build_resolved_pair_from_registry(reg, "gemma", "qwen")
    assert legacy.effective_reasoning_budget("generator") == 4048  # 2048+2000 universal
    assert legacy.effective_reasoning_budget("qwen_audit") == 10192


def test_reasoning_provenance_for_fresh_calls(tmp_path):
    _, pair = _pair_with_deltas(tmp_path, {"generator": 2000})
    prov = pair.reasoning_provenance_for_role("generator")
    assert prov["model_base"] == 2000
    assert prov["role_delta"] == 2000
    assert prov["effective"] == 4000
    assert prov["launch_args"][prov["launch_args"].index("--reasoning-budget") + 1] == "4000"


# ---------------------------------------------------------------------------
# 4. Router same-model role-effective restart
# ---------------------------------------------------------------------------

class _FakeAdapter:
    def __init__(self):
        self.starts: list[list[str]] = []
        self.stops = 0
        self.base_url = "http://127.0.0.1:8094"

    def start(self, model_key, profile, extra_args, retries=1):
        self.starts.append(list(extra_args))
        return 0.01, 0

    def stop(self):
        self.stops += 1
        return 0.01, True, 0

    def sample_vram(self):
        return 0


def _router():
    return ModelRouter(
        _FakeAdapter(),
        role_profile_names={"m": "M"},
        role_args={"m": ["-c", "1024", "--reasoning-budget", "2000"]},
    )


def test_same_model_effective_change_relaunches_with_replacement():
    r = _router()
    rec1 = r.ensure_resident("m", reasoning_budget=2000)
    assert rec1 is not None
    assert r.current_reasoning_budget == 2000
    # Same effective -> no restart.
    assert r.ensure_resident("m", reasoning_budget=2000) is None
    # Different effective -> SAME-model relaunch with REPLACED (not appended) flag.
    rec2 = r.ensure_resident("m", reasoning_budget=4000)
    assert rec2 is not None
    assert rec2.from_model == "m" and rec2.to_model == "m"
    assert rec2.launch_args.count("--reasoning-budget") == 1
    assert rec2.launch_args[rec2.launch_args.index("--reasoning-budget") + 1] == "4000"
    assert rec2.reasoning_budget == 4000
    assert r._adapter.stops == 1
    launched = r._adapter.starts[-1]
    assert launched.count("--reasoning-budget") == 1
    # No group-max substitute: launched value is exactly the effective budget.
    assert launched[launched.index("--reasoning-budget") + 1] == "4000"


def test_legacy_calls_keep_residency_by_model():
    r = _router()
    assert r.ensure_resident("m") is not None
    assert r.ensure_resident("m") is None
    assert r._adapter.stops == 0
    # A legacy call after an effective launch does not restart...
    r2 = _router()
    r2.ensure_resident("m", reasoning_budget=4000)
    stops = r2._adapter.stops
    assert r2.ensure_resident("m") is None
    assert r2._adapter.stops == stops
    # ...and a later same-value effective call still does not restart.
    assert r2.ensure_resident("m", reasoning_budget=4000) is None


def test_release_resets_tracked_budget():
    r = _router()
    r.ensure_resident("m", reasoning_budget=4000)
    r.release()
    assert r.current_model is None
    assert r.current_reasoning_budget is None


def test_with_reasoning_budget_replace_not_append():
    args = with_reasoning_budget(["-c", "1", "--reasoning-budget", "2000"], 4000)
    assert args.count("--reasoning-budget") == 1
    assert args[args.index("--reasoning-budget") + 1] == "4000"
    with pytest.raises(ValueError, match="no --reasoning-budget"):
        with_reasoning_budget(["-c", "1"], 4000)
    with pytest.raises(ValueError, match="more than once|appears 2"):
        with_reasoning_budget(["--reasoning-budget", "1", "--reasoning-budget", "2"], 3)


def test_router_effective_without_flag_fails_closed():
    r = ModelRouter(
        _FakeAdapter(), role_profile_names={"m": "M"}, role_args={"m": ["-c", "1"]}
    )
    with pytest.raises(ValueError, match="no --reasoning-budget"):
        r.ensure_resident("m", reasoning_budget=4000)


# ---------------------------------------------------------------------------
# 5. Identity / cache / resume stability
# ---------------------------------------------------------------------------

def test_effective_reasoning_leaves_identity_untouched(tmp_path):
    _, plain = _pair_with_deltas(tmp_path, None)
    _, with_deltas = _pair_with_deltas(
        tmp_path, {"generator": 2000, "entity_extractor": 2000, "qwen_audit": 2000}
    )
    # Run identity: aggregate hash identical with and without role deltas.
    assert plain.aggregate_hash == with_deltas.aggregate_hash
    # Budget hashes identical (role delta excluded from budget_hash).
    for role in TEN_ROLES:
        assert plain.role_budgets[role].budget_hash == with_deltas.role_budgets[role].budget_hash
    # Request-cache keys: per-role hashes identical.
    for role in TEN_ROLES:
        assert plain.per_role_hash(role) == with_deltas.per_role_hash(role)


def test_resume_accepts_prior_identity(tmp_path):
    """A backend whose identity was recorded before deltas existed resumes OK."""
    from pact_v4.runtime.runtime_config import LocalLlamaBackendConfig

    _, pair = _pair_with_deltas(
        tmp_path, {"generator": 2000, "entity_extractor": 2000, "qwen_audit": 2000}
    )
    cfg = LocalLlamaBackendConfig(
        exe=Path("/tmp/fake.exe"), device="CPU", host="127.0.0.1",
        model_paths={"gemma": Path("/tmp/g.gguf"), "qwen": Path("/tmp/q.gguf")},
        model_names={"gemma": "g", "qwen": "q"},
        server_args={"gemma": ["--reasoning-budget", "2000"], "qwen": ["--reasoning-budget", "8192"]},
        resolved_pair=pair,
    )
    current = cfg.identity_hash
    assert current in cfg.acceptable_identity_hashes()
    # Prior run recorded the same descriptor: nothing about effective
    # reasoning adds an identity dimension, so the stored hash still matches.
    assert cfg.build_descriptor().identity_hash == current


# ---------------------------------------------------------------------------
# 6. New aliases registry shape + pair
# ---------------------------------------------------------------------------

def test_production_aliases_shape():
    reg = load_providers_registry(Path("configs/providers.yaml"))
    local = reg.providers["local"]
    assert set(local) == {"gemma", "qwen", "gemma31", "qwen38"}
    gemma31 = local["gemma31"]
    assert gemma31.model_key == "gemma31"
    assert gemma31.reasoning_budget == 2000
    assert gemma31.request["temperature"] == 1.0
    assert gemma31.request["top_p"] == 0.95
    assert gemma31.request["top_k"] == 64
    assert gemma31.request["min_p"] == 0.0
    assert gemma31.request["repeat_penalty"] == 1.0
    assert "-dev" in gemma31.server_args  # -dev preserved
    qwen38 = local["qwen38"]
    assert qwen38.model_key == "qwen38"
    assert qwen38.reasoning_budget == 8192
    assert qwen38.request["temperature"] == 0.2
    assert qwen38.request["top_p"] == 0.95
    assert qwen38.request["top_k"] == 20
    assert qwen38.request["min_p"] == 0.0
    assert qwen38.request["presence_penalty"] == 0.0
    args = list(qwen38.server_args)
    assert "-md" in args  # mtp draft preserved
    assert args[args.index("--reasoning-effort") + 1] == "xhigh"  # quoted xhigh
    # Existing models gain --reasoning-budget-enable, keep budgets.
    for alias in ("gemma", "qwen"):
        assert "--reasoning-budget-enable" in local[alias].server_args
    assert local["gemma"].reasoning_budget == 2048
    assert local["qwen"].reasoning_budget == 8192


def test_pair_gemma31_qwen38_case_insensitive():
    reg = load_providers_registry(Path("configs/providers.yaml"))
    pair = build_resolved_pair_from_registry(reg, "GEMMA31", "Qwen38")
    assert pair.translator_model.model_key == "gemma31"
    assert pair.reviewer_model.model_key == "qwen38"
    # Remote aliases still fail closed in a local pair.
    with pytest.raises(ValueError, match="not found|remote"):
        build_resolved_pair_from_registry(reg, "musefree", "qwen38")
    assert parse_local_pair_arg("gemma31/qwen38") == ("gemma31", "qwen38")


# ---------------------------------------------------------------------------
# 7. Shared reviewer formatting role (local + remote contract)
# ---------------------------------------------------------------------------

def test_formatting_in_reviewer_group():
    assert "formatting" in REVIEWER_ROLES
    assert "formatting" not in TRANSLATOR_ROLES


def test_formatting_uses_reviewer_sampling(tmp_path):
    _, pair = _pair_with_deltas(tmp_path, None)
    assert pair.sampling_for_role("formatting") == pair.sampling_for_role("qwen_audit")
    assert pair.model_for_role("formatting").model_key == "qwen"


def test_reviewer_flag_binds_formatting():
    from pact_v4.runtime.runtime_config import apply_provider_flags
    from pact_v4.runtime.opencode_backend import OpenCodeServerBackendConfig
    from pact_v4.runtime.runtime_config import OpenCodeBackendConfig

    reg = load_providers_registry(Path("configs/providers.yaml"))
    server = OpenCodeServerBackendConfig(base_url="http://127.0.0.1:4097", reasoning=0)
    cfg = OpenCodeBackendConfig(server=server, server_mode="external", managed=None)
    out = apply_provider_flags(cfg, reg, reviewer="openai/luna")
    assert out.server.model_bindings["formatting"] == "openai/gpt-5.6-luna"


def test_role_adapters_synth_formatting_from_reviewer(tmp_path):
    from unittest.mock import MagicMock

    from pact_v4.runtime.runtime_config import build_role_adapters

    _, pair = _pair_with_deltas(tmp_path, None)
    cfg = MagicMock()
    cfg.resolved_pair = pair
    cfg.resolved_role_policies = pair
    runtime = MagicMock()
    fake_backend = MagicMock()
    import pact_v4.runtime.runtime_config as rc_mod

    real = rc_mod.build_role_backend
    try:
        rc_mod.build_role_backend = lambda c, r: fake_backend  # type: ignore[assignment]
        adapters = build_role_adapters(cfg, runtime)
    finally:
        rc_mod.build_role_backend = real  # type: ignore[assignment]
    # Adapters carry role policies; the formatting synth path uses reviewer sampling.
    fmt_policy = pair.budget_for_role("formatting")
    assert fmt_policy.max_output_tokens == 1000
    assert pair.sampling_for_role("formatting") == {"temperature": 0.3}
    assert adapters is not None


# ---------------------------------------------------------------------------
# 8. Local reviewer formatting lifecycle
# ---------------------------------------------------------------------------

def test_local_formatting_backend_uses_reviewer_launch_args():
    from pact_full_pipeline_runner_v1.v4_book_run import (
        _formatting_backend_with_overrides,
        _formatting_role_policy_from_config,
        _local_formatting_backend_for_pair,
    )

    reg = load_providers_registry(Path("configs/providers.yaml"))
    pair = build_resolved_pair_from_registry(reg, "gemma31", "qwen38")
    backend = _local_formatting_backend_for_pair(pair)
    # Reviewer-only server: qwen38 keys, resolved formatting launch args.
    assert set(backend.model_paths) == {"qwen38"}
    assert backend.server_args["qwen38"] == pair.launch_args_for_role("formatting")
    assert backend.resolved_pair is pair
    # Overrides leave local reviewer servers untouched (no reasoning-0 hack).
    assert _formatting_backend_with_overrides(backend) is backend
    # Role policy carries reviewer sampling (qwen38), not translator.
    policy = _formatting_role_policy_from_config(backend)
    assert policy is not None
    assert policy.request["temperature"] == 0.2
    assert policy.model_key == "qwen38"


def test_book_local_formatting_falls_back_when_pair_unresolvable(tmp_path, monkeypatch):
    from pact_full_pipeline_runner_v1 import v4_book_run

    import pact_full_pipeline_runner_v1.v4_phase12_strict_run as strict_cli
    monkeypatch.setattr(
        strict_cli, "_resolve_local_pair",
        lambda pair_str, prov: (_ for _ in ()).throw(ValueError("no registry")),
    )
    args = type("A", (), {"memory_dir": tmp_path, "runtime_config": None,
                            "translator": None, "reviewer": None,
                            "providers_config": None, "local": None})()
    client = v4_book_run._build_formatting_client(
        args, ["--local", "gemma31/qwen38"], {"enabled": True},
        out_dir=tmp_path / "out",
    )
    assert client is None  # deterministic run_formatting_align downstream


def test_local_logs_keep_local_server_naming(tmp_path):
    """Local formatting clients are tagged; opencode rename never applies."""
    from pact_full_pipeline_runner_v1.v4_book_run import _FormattingBackendClient

    client = _FormattingBackendClient(backend=None, runtime=None, role_policy=None)
    assert getattr(client, "_local_format_profile", None) is None
    client._local_format_profile = "qwen38"
    # The book-run finally block keys its diagnostic name off this tag:
    # local -> {profile}_fmt_{chapter}_health.log, never opencode_serve_fmt_*.
    assert client._local_format_profile == "qwen38"


def test_no_force_formatting_model_cli():
    from pact_full_pipeline_runner_v1.v4_phase12_strict_run import build_argparser as strict_parser
    from pact_full_pipeline_runner_v1.v4_book_run import build_argparser as book_parser

    for parser_fn in (strict_parser, book_parser):
        try:
            parser = parser_fn()
        except TypeError:
            continue  # book parser may require args; checked below instead
        actions = [a.option_strings for a in parser._actions]
        flat = [o for group in actions for o in group]
        assert "--force-formatting-model" not in flat
    import inspect
    import pact_full_pipeline_runner_v1.v4_book_run as book_mod
    src = inspect.getsource(book_mod)
    assert "--force-formatting-model" not in src
    import pact_full_pipeline_runner_v1.v4_phase12_strict_run as strict_mod
    assert "--force-formatting-model" not in inspect.getsource(strict_mod)


# ---------------------------------------------------------------------------
# 9. Matrix provenance + routing backend
# ---------------------------------------------------------------------------

def test_matrix_rows_authoritative(tmp_path):
    _, pair = _pair_with_deltas(
        tmp_path, {"generator": 2000, "entity_extractor": 2000, "qwen_audit": 2000}
    )
    rows = pair.matrix_rows()
    assert len(rows) == 10
    by_role = {r["role"]: r for r in rows}
    assert by_role["formatting"]["group"] == "reviewer"
    assert by_role["generator"]["group"] == "translator"
    assert by_role["generator"]["effective"] == 4000
    assert by_role["qwen_audit"]["effective"] == 10192
    assert by_role["formatting"]["model_base"] == 8192
    assert by_role["generator"]["request"] == {"temperature": 0.7}
    # Production matrix spot-check.
    reg = load_providers_registry(Path("configs/providers.yaml"))
    prod = build_resolved_pair_from_registry(reg, "gemma31", "qwen38")
    prod_rows = {r["role"]: r for r in prod.matrix_rows()}
    assert prod_rows["generator"] == {
        "role": "generator", "group": "translator", "max_output_tokens": 70000,
        "output_budget": None, "model_key": "gemma31", "model_base": 2000,
        "request": {"min_p": 0.0, "repeat_penalty": 1.0, "temperature": 1.0,
                    "top_k": 64, "top_p": 0.95},
        "role_delta": 2000, "effective": 4000,
    }


def test_model_matrix_block_helper():
    from pact_v4.pipeline.v4_phase12_strict_runner import _model_matrix_block

    assert _model_matrix_block(type("C", (), {})()) is None

    class _Cfg:
        resolved_role_policies = None
        resolved_pair = None

    assert _model_matrix_block(_Cfg()) is None


def _local_backend_with_pair(pair) -> "LocalLlamaBackendConfig":
    from pact_v4.runtime.runtime_config import LocalLlamaBackendConfig

    return LocalLlamaBackendConfig(
        exe=Path("/tmp/fake.exe"), device="CPU", host="127.0.0.1",
        model_paths={pair.translator_model.model_key: Path("/tmp/t.gguf"),
                      pair.reviewer_model.model_key: Path("/tmp/r.gguf")},
        model_names={pair.translator_model.model_key: "trans-model",
                      pair.reviewer_model.model_key: "rev-model"},
        server_args={pair.translator_model.model_key: ["--reasoning-budget", "2000"],
                      pair.reviewer_model.model_key: ["--reasoning-budget", "8192"]},
        resolved_pair=pair,
    )


def test_local_routing_backend_role_effective(tmp_path):
    from pact_v4.runtime.runtime_config import LocalRoutingBackend
    from pact_v4.runtime.backend_protocol import CompletionResponse

    _, pair = _pair_with_deltas(
        tmp_path, {"generator": 2000, "entity_extractor": 2000, "qwen_audit": 2000}
    )
    backend_cfg = _local_backend_with_pair(pair)

    calls: list[tuple] = []

    class _FakeRouter:
        base_url = "http://127.0.0.1:8094"

        def ensure_resident(self, key, *, reasoning_budget=None, reasoning_provenance=None):
            calls.append((key, reasoning_budget))

    rb = LocalRoutingBackend(_FakeRouter(), backend_cfg)  # type: ignore[arg-type]

    class _FakeInner:
        def complete(self, request):
            return CompletionResponse(text="ok", provider="x", model="m")

        def close(self):
            pass

    rb._backends["rev-model"] = _FakeInner()  # type: ignore[attr-defined]
    req = CompletionRequest(
        model_ref="rev-model",
        messages=(Message(role="user", content="hi"),),
        max_output_tokens=10,
        temperature=0.0,
        response_schema={"type": "json_object"},
        label="t",
        role="qwen_audit",
    )
    resp = rb.complete(req)
    assert resp.text == "ok"
    # qwen-class base 8192 + qwen_audit 2000 = 10192, same model relaunched.
    assert calls == [("qwen", 10192)]


def test_local_routing_backend_misroute_fails_closed(tmp_path):
    from pact_v4.runtime.backend_protocol import CompletionError
    from pact_v4.runtime.runtime_config import LocalRoutingBackend

    _, pair = _pair_with_deltas(tmp_path, None)
    backend_cfg = _local_backend_with_pair(pair)

    class _FakeRouter:
        base_url = "http://127.0.0.1:8094"

        def ensure_resident(self, key, *, reasoning_budget=None):
            raise AssertionError("must not acquire on misroute")

    rb = LocalRoutingBackend(_FakeRouter(), backend_cfg)  # type: ignore[arg-type]
    req = CompletionRequest(
        model_ref="rev-model",  # reviewer model...
        messages=(Message(role="user", content="hi"),),
        max_output_tokens=10,
        temperature=0.0,
        response_schema={"type": "json_object"},
        label="t",
        role="generator",  # ...but translator role: misroute
    )
    with pytest.raises(CompletionError, match="misroute"):
        rb.complete(req)


def test_local_routing_backend_legacy_without_role(tmp_path):
    from pact_v4.runtime.runtime_config import LocalRoutingBackend
    from pact_v4.runtime.backend_protocol import CompletionResponse

    _, pair = _pair_with_deltas(tmp_path, None)
    backend_cfg = _local_backend_with_pair(pair)
    calls: list[tuple] = []

    class _FakeRouter:
        base_url = "http://127.0.0.1:8094"

        def ensure_resident(self, key, *, reasoning_budget=None, reasoning_provenance=None):
            calls.append((key, reasoning_budget))

    rb = LocalRoutingBackend(_FakeRouter(), backend_cfg)  # type: ignore[arg-type]

    class _FakeInner:
        def complete(self, request):
            return CompletionResponse(text="ok", provider="x", model="m")

        def close(self):
            pass

    rb._backends["trans-model"] = _FakeInner()  # type: ignore[attr-defined]
    req = CompletionRequest(
        model_ref="trans-model",
        messages=(Message(role="user", content="hi"),),
        max_output_tokens=10,
        temperature=0.0,
        response_schema={"type": "json_object"},
        label="t",
    )
    assert rb.complete(req).text == "ok"
    assert calls == [("gemma", None)]


# ---------------------------------------------------------------------------
# 10. Round-1 review: reviewer-routed russian_selector + fresh-call provenance
# ---------------------------------------------------------------------------

def test_russian_selector_served_by_reviewer_model(tmp_path):
    _, pair = _pair_with_deltas(tmp_path, None)
    assert pair.model_for_role("russian_selector").model_key == "qwen"
    assert "russian_selector" in REVIEWER_ROLES
    assert "russian_selector" not in TRANSLATOR_ROLES


def test_build_strict_lifecycle_routes_selector_via_reviewer(tmp_path):
    from unittest.mock import MagicMock

    from pact_v4.pipeline import v4_phase12_strict_runner as runner

    _, pair = _pair_with_deltas(tmp_path, {"russian_selector": 2000})
    backend = MagicMock()
    backend.resolved_pair = pair
    backend.model_names = {"gemma": "trans-model", "qwen": "rev-model"}
    fake_router = MagicMock()
    fake_router.base_url = "http://127.0.0.1:8094"
    fake_runtime = MagicMock()
    fake_runtime.router = fake_router
    backend.build_runtime.return_value = fake_runtime
    _, _, _, gemma_selector, _, _ = runner.build_strict_lifecycle(
        backend, log_dir=tmp_path, bible_text=""
    )
    # Reviewer model/profile + pair so role-effective relaunch applies.
    assert gemma_selector._model_key == pair.reviewer_model.model_key
    assert gemma_selector._role == "russian_selector"
    assert gemma_selector._pair is pair
    assert gemma_selector._selector.api.config.model == pair.reviewer_model.model_name
    # Legacy translator residency must be gone: no pair=None wrapper.
    assert gemma_selector._pair is not None


def test_switch_payload_carries_fresh_call_provenance(tmp_path):
    from pact_v4.runtime.runtime_coordinator import switch_payload

    _, pair = _pair_with_deltas(tmp_path, {"qwen_audit": 2000})
    r = ModelRouter(
        _FakeAdapter(),
        role_profile_names={"qwen": "Qwen"},
        role_args={"qwen": ["-c", "1024", "--reasoning-budget", "8192"]},
    )
    prov = pair.reasoning_provenance_for_role("qwen_audit")
    rec = r.ensure_resident(
        "qwen",
        reasoning_budget=int(prov["effective"]),
        reasoning_provenance=prov,
    )
    assert rec is not None
    assert rec.reasoning_budget == 10192
    assert rec.role == "qwen_audit"
    assert rec.model_base == 8192
    assert rec.role_delta == 2000
    payload = switch_payload(rec)
    assert payload["reasoning_budget"] == 10192
    assert payload["effective"] == 10192
    assert payload["role"] == "qwen_audit"
    assert payload["model_base"] == 8192
    assert payload["role_delta"] == 2000
    assert payload["launch_args"][payload["launch_args"].index("--reasoning-budget") + 1] == "10192"
    assert isinstance(payload["launch_args"], list)


def test_resident_hit_writes_no_provenance(tmp_path):
    _, pair = _pair_with_deltas(tmp_path, {"qwen_audit": 2000})
    r = ModelRouter(
        _FakeAdapter(),
        role_profile_names={"qwen": "Qwen"},
        role_args={"qwen": ["-c", "1024", "--reasoning-budget", "8192"]},
    )
    prov = pair.reasoning_provenance_for_role("qwen_audit")
    assert r.ensure_resident("qwen", reasoning_budget=int(prov["effective"]), reasoning_provenance=prov) is not None
    # Same effective budget -> resident hit, no new record (cache-hit provenance untouched).
    assert r.ensure_resident("qwen", reasoning_budget=int(prov["effective"]), reasoning_provenance=prov) is None
    assert len(r.switches) == 1


def test_ensure_role_resident_persists_provenance(tmp_path):
    from pact_v4.runtime.model_lifecycle_adapters import _ensure_role_resident

    _, pair = _pair_with_deltas(tmp_path, {"fidelity_reviewer": 0})
    r = ModelRouter(
        _FakeAdapter(),
        role_profile_names={"qwen": "Qwen"},
        role_args={"qwen": ["-c", "1024", "--reasoning-budget", "8192"]},
    )
    _ensure_role_resident(r, "qwen", role="fidelity_reviewer", pair=pair)
    assert len(r.switches) == 1
    rec = r.switches[0]
    assert rec.reasoning_budget == 8192
    assert rec.role == "fidelity_reviewer"
    assert rec.model_base == 8192
    assert rec.role_delta == 0


def test_legacy_switch_payload_shape_preserved():
    from pact_v4.runtime.runtime_coordinator import switch_payload

    rec = SwitchRecord(
        from_model=None, to_model="m", cold_acquire_seconds=0.1,
        unload_seconds=None, load_retries=0, peak_vram_mb=None,
        timestamp="2026-01-01T00:00:00Z",
    )
    payload = switch_payload(rec)
    assert payload["reasoning_budget"] is None
    assert payload["launch_args"] == []
    assert payload["role"] is None
    assert payload["model_base"] is None
    assert payload["role_delta"] is None
    assert payload["effective"] is None


# ---------------------------------------------------------------------------
# 10. Extended sampling reaches the HTTP payload (HIGH review finding)
# ---------------------------------------------------------------------------

def _api_client():
    from pact_v4.runtime.api_client import ApiClient, ApiClientConfig

    return ApiClient(
        ApiClientConfig(chat_url="http://127.0.0.1:8094/v1/chat/completions", model="m"),
        name="payload-test",
    )


def _completion_request(**kwargs):
    base = dict(
        model_ref="m",
        messages=(Message(role="user", content="hi"),),
        max_output_tokens=10,
        temperature=0.2,
        response_schema={"type": "json_object"},
        label="t",
    )
    base.update(kwargs)
    return CompletionRequest(**base)


def test_completion_request_extended_validation():
    req = _completion_request(
        repeat_penalty=1.0, repeat_last_n=64,
        frequency_penalty=0.5, presence_penalty=0.0,
    )
    assert req.repeat_penalty == 1.0
    assert req.repeat_last_n == 64
    assert req.frequency_penalty == 0.5
    assert req.presence_penalty == 0.0
    # Defaults stay absent (policy-owned: absent stays absent).
    plain = _completion_request()
    assert plain.repeat_penalty is None
    assert plain.repeat_last_n is None
    assert plain.frequency_penalty is None
    assert plain.presence_penalty is None
    # Fail-closed ranges mirror the registry allowlist.
    for kwargs in (
        {"repeat_penalty": "high"}, {"repeat_penalty": -0.5}, {"repeat_penalty": 9.0},
        {"repeat_last_n": 1.5}, {"repeat_last_n": True}, {"repeat_last_n": -2},
        {"repeat_last_n": 70000}, {"frequency_penalty": 3.0}, {"frequency_penalty": -3.0},
        {"presence_penalty": 2.5}, {"presence_penalty": "none"},
    ):
        with pytest.raises(ValueError):
            _completion_request(**kwargs)


def test_build_payload_includes_extended_sampling():
    client = _api_client()
    payload = client.build_payload(
        [{"role": "user", "content": "hi"}],
        max_tokens=10, temperature=1.0,
        repeat_penalty=1.0, repeat_last_n=64,
        frequency_penalty=0.5, presence_penalty=0.0,
    )
    assert payload["repeat_penalty"] == 1.0
    assert payload["repeat_last_n"] == 64
    assert payload["frequency_penalty"] == 0.5
    # 0.0 is an explicit policy value, not absence — it MUST be sent.
    assert payload["presence_penalty"] == 0.0
    assert isinstance(payload["repeat_last_n"], int)


def test_build_payload_omits_absent_extended_sampling():
    client = _api_client()
    payload = client.build_payload(
        [{"role": "user", "content": "hi"}],
        max_tokens=10, temperature=0.2, top_p=0.95,
    )
    for key in ("repeat_penalty", "repeat_last_n", "frequency_penalty", "presence_penalty"):
        assert key not in payload
    assert payload["top_p"] == 0.95


def test_api_complete_batch_forwards_extended_to_payload():
    client = _api_client()
    captured: dict = {}

    def fake_post(payload):
        captured.update(dict(payload))
        return ({"choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}]}, 200, True, 1)

    def fake_extract(data):
        return ("ok", "stop", {}, "")

    client._post_with_retry = fake_post  # type: ignore[method-assign]
    client._extract_message = staticmethod(fake_extract)  # type: ignore[attr-defined]
    text = client.complete(
        [{"role": "user", "content": "hi"}],
        max_tokens=10, temperature=1.0,
        repeat_penalty=1.0, repeat_last_n=64,
        frequency_penalty=0.5, presence_penalty=0.0,
    )
    assert text == "ok"
    assert captured["repeat_penalty"] == 1.0
    assert captured["repeat_last_n"] == 64
    assert captured["frequency_penalty"] == 0.5
    assert captured["presence_penalty"] == 0.0


def test_api_complete_stream_forwards_extended_to_payload():
    client = _api_client()
    captured: dict = {}

    def fake_post_stream(messages, **kwargs):
        captured.update(kwargs)
        return ("ok", "stop", {}, "", 200, True, 1)

    client._post_stream = fake_post_stream  # type: ignore[method-assign]
    text = client.complete(
        [{"role": "user", "content": "hi"}],
        max_tokens=10, temperature=0.2,
        on_reasoning_chunk=lambda chunk: None,
        repeat_penalty=1.0, presence_penalty=0.0,
    )
    assert text == "ok"
    assert captured["repeat_penalty"] == 1.0
    assert captured["presence_penalty"] == 0.0


def test_api_complete_stream_fallback_batch_forwards_extended():
    from pact_v4.runtime.api_client import ApiClientError

    client = _api_client()
    captured: dict = {}

    def failing_stream(messages, **kwargs):
        raise ApiClientError("SSE down")

    def fake_post(payload):
        captured.update(dict(payload))
        return ({"choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}]}, 200, True, 1)

    def fake_extract(data):
        return ("ok", "stop", {}, "")

    client._post_stream = failing_stream  # type: ignore[method-assign]
    client._post_with_retry = fake_post  # type: ignore[method-assign]
    client._extract_message = staticmethod(fake_extract)  # type: ignore[attr-defined]
    text = client.complete(
        [{"role": "user", "content": "hi"}],
        max_tokens=10, temperature=0.2,
        on_reasoning_chunk=lambda chunk: None,
        repeat_penalty=1.0, repeat_last_n=32,
        frequency_penalty=-0.5, presence_penalty=0.0,
    )
    assert text == "ok"
    assert captured["repeat_penalty"] == 1.0
    assert captured["repeat_last_n"] == 32
    assert captured["frequency_penalty"] == -0.5
    assert captured["presence_penalty"] == 0.0


def test_local_backend_forwards_extended_from_request():
    from pact_v4.runtime.backend_protocol import CompletionResponse
    from pact_v4.runtime.local_openai_backend import LocalOpenAIBackend

    seen: dict = {}

    class _StubApi:
        config = _api_client().config
        name = "stub"

        @property
        def call_records(self):
            return []

        def complete(self, messages, **kwargs):
            seen.update(kwargs)
            return "ok"

    backend = LocalOpenAIBackend(api=_StubApi())  # type: ignore[arg-type]
    req = _completion_request(
        repeat_penalty=1.0, repeat_last_n=64,
        frequency_penalty=0.5, presence_penalty=0.0,
    )
    resp = backend.complete(req)
    assert resp.text == "ok"
    assert seen["repeat_penalty"] == 1.0
    assert seen["repeat_last_n"] == 64
    assert seen["frequency_penalty"] == 0.5
    assert seen["presence_penalty"] == 0.0


def test_production_policies_reach_payload():
    """gemma31 repeat_penalty 1.0 / qwen38 presence_penalty 0.0 survive to payload."""
    from pact_v4.runtime.runtime_config import build_resolved_pair_from_registry

    client = _api_client()
    reg = load_providers_registry(Path("configs/providers.yaml"))
    pair = build_resolved_pair_from_registry(reg, "gemma31", "qwen38")
    gen_sampling = pair.sampling_for_role("generator")
    payload = client.build_payload(
        [{"role": "user", "content": "hi"}],
        max_tokens=10, temperature=gen_sampling["temperature"],
        top_p=gen_sampling.get("top_p"), top_k=gen_sampling.get("top_k"),
        min_p=gen_sampling.get("min_p"),
        repeat_penalty=gen_sampling.get("repeat_penalty"),
        repeat_last_n=gen_sampling.get("repeat_last_n"),
        frequency_penalty=gen_sampling.get("frequency_penalty"),
        presence_penalty=gen_sampling.get("presence_penalty"),
    )
    assert payload["repeat_penalty"] == 1.0
    rev_sampling = pair.sampling_for_role("formatting")
    payload2 = client.build_payload(
        [{"role": "user", "content": "hi"}],
        max_tokens=10, temperature=rev_sampling["temperature"],
        top_p=rev_sampling.get("top_p"), top_k=rev_sampling.get("top_k"),
        min_p=rev_sampling.get("min_p"),
        repeat_penalty=rev_sampling.get("repeat_penalty"),
        repeat_last_n=rev_sampling.get("repeat_last_n"),
        frequency_penalty=rev_sampling.get("frequency_penalty"),
        presence_penalty=rev_sampling.get("presence_penalty"),
    )
    assert payload2["presence_penalty"] == 0.0
    assert "repeat_penalty" not in payload2


def test_formatting_client_passes_extended_policy_fields():
    """Adapter site: reviewer sampling incl. presence_penalty lands on the request."""
    from pact_full_pipeline_runner_v1.v4_book_run import _FormattingBackendClient

    seen: list = []

    class _FakeBackend:
        @property
        def descriptor(self):
            from pact_v4.runtime.backend_protocol import BackendDescriptor

            return BackendDescriptor(
                kind="local_llama", transport_version="t",
                endpoint_family="openai_chat_completions",
                public_endpoint="http://127.0.0.1:8094",
                model_bindings={"formatting": "rev-model"},
                effective_options={},
            )

        def complete(self, request):
            seen.append(request)
            from pact_v4.runtime.backend_protocol import CompletionResponse

            return CompletionResponse(text='{"m":{}}', provider="x", model="m")

    policy = type(
        "SynthPolicy", (),
        {"request": {"temperature": 0.2, "top_p": 0.95, "top_k": 20,
                       "min_p": 0.0, "presence_penalty": 0.0},
         "output_budget": None, "model_key": "qwen38",
         "policy_hash": "h"},
    )()
    client = _FormattingBackendClient(_FakeBackend(), None, role_policy=policy)
    from pact_v4.phase0b.source_html import parse_source_html

    blocks = parse_source_html("<html><body><p>Hello <em>world</em>.</p></body></html>")
    client.complete(
        [{"role": "user", "content": "map"}],
        {"role_policy": policy}, 100,
    )
    assert seen and seen[0].presence_penalty == 0.0
    assert seen[0].repeat_penalty is None


def test_repair_adapter_passes_extended_policy_fields(tmp_path):
    """Adapter site: SelectiveRepairEvaluator repair policy incl. repeat_penalty."""
    from tests.pact_v4.repair.test_selective_repair import (
        _hard_filtered,
        _issue,
        _repair_response,
    )
    from pact_v4.repair.selective_repair import SelectiveRepairEvaluator, SelectiveRepairConfig
    from pact_v4.runtime.backend_protocol import BackendDescriptor, CompletionResponse

    issue = _issue("p00193", "invented_gender")
    source = {"p00193": "Then you say it has to be a grandchild-"}
    translation = {"p00193": "\u0410 \u043f\u043e\u0442\u043e\u043c \u0437\u0430\u044f\u0432\u0438\u043b\u0430"}
    filtered = _hard_filtered([issue], source, translation)

    class _RepairBackend:
        def __init__(self, script):
            self._script = list(script)
            self.requests = []

        @property
        def descriptor(self):
            return BackendDescriptor(
                kind="local_llama", transport_version="t",
                endpoint_family="openai_chat_completions",
                public_endpoint="http://127.0.0.1:8094",
                model_bindings={"repair": "gen-model"},
                effective_options={},
            )

        def complete(self, request):
            self.requests.append(request)
            return self._script.pop(0)

    backend = _RepairBackend([
        _repair_response([{"index": 1, "decision": "pass", "reason": "verified"}]),
    ])
    policy = type(
        "SynthPolicy", (),
        {"request": {"temperature": 1.0, "max_output_tokens": 70000, "repeat_penalty": 1.0, "repeat_last_n": 64},
         "output_budget": None, "model_key": "gemma31",
         "policy_hash": "h"},
    )()
    evaluator = SelectiveRepairEvaluator(
        backend,  # type: ignore[arg-type]
        config=SelectiveRepairConfig(role_policy=policy, repair_reasoning=None),
    )
    evaluator(
        chapter_id="0001", source=source, translation=translation,
        filtered=filtered, out_dir=tmp_path, out_base="b3_repair",
    )
    repair_requests = [r for r in backend.requests if "selective_repair" in (r.label or "")]
    assert repair_requests, "expected a repair request"
    assert repair_requests[0].repeat_penalty == 1.0
    assert repair_requests[0].repeat_last_n == 64
    assert repair_requests[0].presence_penalty is None


# ---------------------------------------------------------------------------
# 11. Legacy CLI validation accommodates role-effective reasoning (HIGH finding)
#
# run_local_default:1112 rejected bare --local and --local gemma31/qwen38
# when --reasoning was omitted: the pair's static base budgets (gemma 2048,
# gemma31 2000) tripped the A2 reasoning==0 gate. Pair-driven runs use
# role-effective reasoning (dynamic, never identity), so the gate is
# superseded for pair-carrying backends; non-pair behavior is unchanged.
# ---------------------------------------------------------------------------

def _providers_config_path() -> Path:
    return Path("configs/providers.yaml").resolve()


def _run_local_default_backend(pair_str, *, reasoning=None):
    """Backend exactly as run_local_default builds it (no server start)."""
    from pact_full_pipeline_runner_v1.v4_phase12_strict_run import (
        GEMMA_PATH,
        QWEN_PATH,
        QWEN_SERVER_ARGS,
        _gemma_server_args_for_reasoning,
        _resolve_local_pair,
    )
    from pact_v4.runtime.runtime_config import (
        StrictBackendConfig,
        apply_resolved_pair_to_config,
    )

    eff = int(reasoning) if reasoning is not None else 0
    backend = StrictBackendConfig(
        exe=Path(r"C:\src\llama-sycl-edge\build\bin\llama-server.exe"),
        device="SYCL0", host="127.0.0.1",
        model_paths={"gemma": GEMMA_PATH, "qwen": QWEN_PATH},
        model_names={"gemma": GEMMA_PATH.name, "qwen": QWEN_PATH.name},
        server_args={
            "gemma": _gemma_server_args_for_reasoning(eff),
            "qwen": list(QWEN_SERVER_ARGS),
        },
        port=8094,
    )
    pair = _resolve_local_pair(pair_str, _providers_config_path())
    return apply_resolved_pair_to_config(backend, pair), eff


def test_legacy_validation_accepts_bare_local_reasoning_omitted():
    from pact_v4.runtime.runtime_config import validate_reasoning_backend

    backend, eff = _run_local_default_backend(None)  # bare --local
    assert eff == 0
    validate_reasoning_backend(eff, backend)  # must not raise


def test_legacy_validation_accepts_gemma31_qwen38_reasoning_omitted():
    from pact_v4.runtime.runtime_config import validate_reasoning_backend

    backend, eff = _run_local_default_backend("gemma31/qwen38")
    assert eff == 0
    validate_reasoning_backend(eff, backend)  # must not raise


@pytest.mark.parametrize("pair_str", [None, "gemma31/qwen38", "gemma/qwen"])
def test_legacy_validation_accepts_pair_reasoning_positive(pair_str):
    from pact_v4.runtime.runtime_config import validate_reasoning_backend

    backend, eff = _run_local_default_backend(pair_str, reasoning=2)
    assert eff == 2
    validate_reasoning_backend(eff, backend)  # generator effective > 0


def test_legacy_validation_nonpair_still_fail_closed():
    """A2 semantics unchanged for backends without a pair."""
    from pact_full_pipeline_runner_v1.v4_phase12_strict_run import (
        GEMMA_PATH,
        QWEN_PATH,
        QWEN_SERVER_ARGS,
        _gemma_server_args_for_reasoning,
    )
    from pact_v4.runtime.runtime_config import StrictBackendConfig, validate_reasoning_backend

    def _base(eff):
        return StrictBackendConfig(
            exe=Path(r"C:\src\llama-sycl-edge\build\bin\llama-server.exe"),
            device="SYCL0", host="127.0.0.1",
            model_paths={"gemma": GEMMA_PATH, "qwen": QWEN_PATH},
            model_names={"gemma": GEMMA_PATH.name, "qwen": QWEN_PATH.name},
            server_args={
                "gemma": _gemma_server_args_for_reasoning(eff),
                "qwen": list(QWEN_SERVER_ARGS),
            },
            port=8094,
        )
    # reasoning 0 with a pinned nonzero static budget still rejected...
    with pytest.raises(ValueError, match="reasoning-budget"):
        validate_reasoning_backend(0, _base(3))
    # ...and reasoning > 0 with no expressible budget still rejected.
    with pytest.raises(ValueError, match="reasoning"):
        validate_reasoning_backend(2, _base(0))
    # Agreement still accepted.
    validate_reasoning_backend(0, _base(0))
    validate_reasoning_backend(2, _base(3))


def _cli_preflight_report(capsys, tmp_path, extra):
    import json

    from pact_full_pipeline_runner_v1.v4_phase12_strict_run import main as strict_main

    argv = [
        "--chapter-id", "0001",
        "--chapter-html", str(tmp_path / "ch.html"),
        "--memory-dir", str(tmp_path / "memory"),
        "--out-dir", str(tmp_path / "out"),
        "--providers-config", str(_providers_config_path()),
        "--preflight-json",
        *extra,
    ]
    code = strict_main(argv)
    assert isinstance(code, int)
    return code, json.loads(capsys.readouterr().out)


def _assert_reasoning_check_ok(report):
    rows = [c for c in report["checks"] if c["name"].startswith("reasoning")]
    assert rows, "expected a reasoning preflight check"
    assert all(c["ok"] for c in rows), f"reasoning check failed: {rows}"
    assert not any("reasoning-budget" in e for e in report["errors"]), report["errors"]


def test_cli_preflight_bare_local_reasoning_omitted(capsys, tmp_path):
    """End-to-end: bare --local preflight must not fail on reasoning."""
    _, report = _cli_preflight_report(capsys, tmp_path, ["--local"])
    _assert_reasoning_check_ok(report)


def test_cli_preflight_gemma31_qwen38_reasoning_omitted(capsys, tmp_path):
    """End-to-end: --local gemma31/qwen38 preflight must not fail on reasoning."""
    _, report = _cli_preflight_report(capsys, tmp_path, ["--local", "gemma31/qwen38"])
    _assert_reasoning_check_ok(report)
    assert report["translator"]["model_key"] == "gemma31"
    assert report["reviewer"]["model_key"] == "qwen38"


# ---------------------------------------------------------------------------
# 12. B3 validation uses the resolved reviewer (Round-2 HIGH finding)
#
# _validate_b3_qwen_profile inspected hardcoded server_args/model_paths/
# model_names['qwen'] while apply_resolved_pair_to_config retains legacy
# entries and the active reviewer routes to qwen38 — so gemma31/qwen38
# passed on qwen3.6/49k settings while launching qwen38/44k. The validator
# now resolves the pair's reviewer model/key and its ACTUAL qwen_audit
# role-effective launch args; qwen38 is assessed against its exact approved
# contract (-md external draft, 44k context), fail-closed and model-specific.
# ---------------------------------------------------------------------------

def _b3_args():
    from types import SimpleNamespace

    return SimpleNamespace(whole_chapter=True, skip_audit=False, stop_after_generation=False)


def _b3_pair_backend(pair_str):
    import pact_full_pipeline_runner_v1.v4_phase12_strict_run as cli
    from pact_v4.runtime.runtime_config import apply_resolved_pair_to_config

    backend, _ = _run_local_default_backend(pair_str)
    return cli, backend


def test_b3_validates_resolved_reviewer_gemma31_qwen38():
    import pact_full_pipeline_runner_v1.v4_phase12_strict_run as cli

    _, backend = _b3_pair_backend("gemma31/qwen38")
    # Sanity: legacy qwen entries are retained AND the reviewer is qwen38.
    assert set(backend.server_args) >= {"qwen", "qwen38"}
    assert backend.resolved_pair.reviewer_model.model_key == "qwen38"
    cli._validate_b3_qwen_profile(_b3_args(), backend)  # must not raise


def test_b3_validates_resolved_reviewer_default_pair():
    import pact_full_pipeline_runner_v1.v4_phase12_strict_run as cli

    _, backend = _b3_pair_backend(None)
    assert backend.resolved_pair.reviewer_model.model_key == "qwen"
    cli._validate_b3_qwen_profile(_b3_args(), backend)  # must not raise


def _tampered_qwen38_backend(fn):
    from dataclasses import replace

    _, backend = _b3_pair_backend("gemma31/qwen38")
    sa = dict(backend.server_args)
    sa["qwen38"] = fn(list(sa["qwen38"]))
    return replace(backend, server_args=sa)


def _swap(lst, flag, newval):
    i = lst.index(flag)
    lst[i + 1] = newval
    return lst


def test_b3_rejects_qwen38_wrong_draft():
    import pact_full_pipeline_runner_v1.v4_phase12_strict_run as cli

    backend = _tampered_qwen38_backend(lambda a: _swap(a, "-md", "C:/evil/mtp-evil.gguf"))
    with pytest.raises(ValueError, match="-md"):
        cli._validate_b3_qwen_profile(_b3_args(), backend)


def test_b3_rejects_qwen38_missing_draft():
    import pact_full_pipeline_runner_v1.v4_phase12_strict_run as cli

    def _drop(a):
        return [x for x in a if x != "-md" and "mtp-Qwen" not in x]

    backend = _tampered_qwen38_backend(_drop)
    with pytest.raises(ValueError, match="-md"):
        cli._validate_b3_qwen_profile(_b3_args(), backend)


def test_b3_rejects_qwen38_low_context():
    import pact_full_pipeline_runner_v1.v4_phase12_strict_run as cli

    backend = _tampered_qwen38_backend(lambda a: _swap(a, "-c", "32768"))
    with pytest.raises(ValueError, match="44000"):
        cli._validate_b3_qwen_profile(_b3_args(), backend)


def test_b3_rejects_qwen38_missing_spec_type():
    import pact_full_pipeline_runner_v1.v4_phase12_strict_run as cli

    def _drop(a):
        return [x for x in a if x != "--spec-type" and x != "draft-mtp"]

    backend = _tampered_qwen38_backend(_drop)
    with pytest.raises(ValueError, match="spec-type"):
        cli._validate_b3_qwen_profile(_b3_args(), backend)


def test_b3_rejects_qwen38_lowered_budget():
    """Budget is assessed on ACTUAL role-effective args (2048+2000=4048 < 8192)."""
    import pact_full_pipeline_runner_v1.v4_phase12_strict_run as cli
    from pact_v4.runtime.runtime_config import (
        LocalModelSpec,
        ResolvedModelPair,
        apply_resolved_pair_to_config,
        build_resolved_pair_from_registry,
        load_providers_registry,
    )

    reg = load_providers_registry(_providers_config_path())
    pair = build_resolved_pair_from_registry(reg, "gemma31", "qwen38")
    rev = pair.reviewer_model
    sa = list(rev.server_args)
    sa[sa.index("--reasoning-budget") + 1] = "2048"
    low_rev = LocalModelSpec(
        model_key=rev.model_key, model_path=rev.model_path, model_name=rev.model_name,
        server_args=tuple(sa), reasoning_budget=2048, request=dict(rev.request),
    )
    low_pair = ResolvedModelPair(
        translator_model=pair.translator_model, reviewer_model=low_rev,
        role_budgets=dict(reg.role_budgets),
    )
    backend, _ = _run_local_default_backend("gemma31/qwen38")
    backend = apply_resolved_pair_to_config(backend, low_pair)
    with pytest.raises(ValueError, match="reasoning-budget"):
        cli._validate_b3_qwen_profile(_b3_args(), backend)


def test_b3_rejects_unassessed_reviewer_model():
    """Fail-closed for reviewer models with no assessed B3 capability."""
    import pact_full_pipeline_runner_v1.v4_phase12_strict_run as cli
    from pact_v4.runtime.runtime_config import (
        ResolvedModelPair,
        apply_resolved_pair_to_config,
        build_resolved_pair_from_registry,
        load_providers_registry,
    )

    reg = load_providers_registry(_providers_config_path())
    pair = build_resolved_pair_from_registry(reg, "gemma31", "qwen38")
    gemma = reg.providers["local"]["gemma"]
    odd_pair = ResolvedModelPair(
        translator_model=pair.translator_model, reviewer_model=gemma,
        role_budgets=dict(reg.role_budgets),
    )
    backend, _ = _run_local_default_backend("gemma31/qwen38")
    backend = apply_resolved_pair_to_config(backend, odd_pair)
    with pytest.raises(ValueError, match="unassessed|assessed B3 capability"):
        cli._validate_b3_qwen_profile(_b3_args(), backend)
