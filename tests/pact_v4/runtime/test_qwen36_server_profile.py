"""Tuned qwen (Qwen3.6-35B-A3B) server profile regression (local-matrix-v2 §2,
qwen38-gemma31-profile-refresh addendum: no speculative drafting).

Asserts the exact ordered profile and parity across:
- configs/providers.yaml (providers.local.models.qwen)
- configs/runtime_local.example.yaml (server_args.qwen)
- pact_full_pipeline_runner_v1.v4_phase12_strict_run.QWEN_SERVER_ARGS

Tuned profile: -ub "2048", -ctv q8_0, no --spec-type/draft-mtp,
no --spec-draft-*, no -md, no --device, required --reasoning-budget-enable,
reasoning_budget 8192.
"""
from __future__ import annotations

from pathlib import Path

import yaml

from pact_v4.runtime.runtime_config import load_providers_registry

EXPECTED_QWEN_SERVER_ARGS = [
    "-fit", "on",
    "-fitt", "1280",
    "-b", "2048",
    "-ub", "2048",
    "-ctk", "q8_0",
    "-ctv", "q8_0",
    "-t", "6",
    "-tb", "12",
    "--load-mode", "mmap",
    "--reasoning", "on",
    "--reasoning-budget", "8192",
    "--reasoning-budget-enable",
    "-np", "1",
    "-c", "49152",
    "-fa", "on",
    "--jinja",
    "--cache-ram", "0",
    "--ctx-checkpoints", "0",
]

REMOVED_FLAGS = ("--spec-type", "draft-mtp", "--spec-draft-n-max", "--spec-draft-p-min", "--spec-draft-ngl", "--spec-draft-device", "-md", "--device")


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[3]


def test_qwen_profile_exact_ordered_args():
    assert EXPECTED_QWEN_SERVER_ARGS[EXPECTED_QWEN_SERVER_ARGS.index("-ub") + 1] == "2048"
    assert EXPECTED_QWEN_SERVER_ARGS[EXPECTED_QWEN_SERVER_ARGS.index("-ctv") + 1] == "q8_0"
    assert "--reasoning-budget-enable" in EXPECTED_QWEN_SERVER_ARGS
    for flag in REMOVED_FLAGS:
        assert flag not in EXPECTED_QWEN_SERVER_ARGS


def test_qwen_parity_across_registry_example_and_cli():
    root = _repo_root()
    reg = load_providers_registry(root / "configs" / "providers.yaml")
    registry_args = list(reg.providers["local"]["qwen"].server_args)
    assert registry_args == EXPECTED_QWEN_SERVER_ARGS

    example = yaml.safe_load((root / "configs" / "runtime_local.example.yaml").read_text(encoding="utf-8"))
    example_args = [str(v) for v in example["server_args"]["qwen"]]
    assert example_args == EXPECTED_QWEN_SERVER_ARGS

    from pact_full_pipeline_runner_v1.v4_phase12_strict_run import QWEN_SERVER_ARGS

    assert list(QWEN_SERVER_ARGS) == EXPECTED_QWEN_SERVER_ARGS


def test_qwen_model_identity_matches_b3_approved_file():
    # Round-4 finding: the registry qwen file must equal the exact path/name
    # the B3 gate requires (not merely the MTP directory).
    from pact_full_pipeline_runner_v1.v4_phase12_strict_run import (
        _B3_QWEN_MODEL_NAME,
        _B3_QWEN_MODEL_PATH,
        _canonical_b3_model_path,
    )

    root = _repo_root()
    reg = load_providers_registry(root / "configs" / "providers.yaml")
    qwen = reg.providers["local"]["qwen"]
    assert qwen.model_path == _B3_QWEN_MODEL_PATH
    assert qwen.model_name == _B3_QWEN_MODEL_NAME
    assert (
        _canonical_b3_model_path(str(qwen.model_path))
        == _canonical_b3_model_path(_B3_QWEN_MODEL_PATH)
    )


def test_qwen_budget_and_request_agreement():
    root = _repo_root()
    reg = load_providers_registry(root / "configs" / "providers.yaml")
    qwen = reg.providers["local"]["qwen"]
    assert qwen.reasoning_budget == 8192
    args = list(qwen.server_args)
    assert args[args.index("--reasoning-budget") + 1] == "8192"
    assert "--reasoning-budget-enable" in args
    for flag in REMOVED_FLAGS:
        assert flag not in args
    assert qwen.request.get("temperature") == 0.0
