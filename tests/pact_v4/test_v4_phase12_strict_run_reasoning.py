"""V4.1 reasoning/backend CLI wiring tests (review commit 301e9df).

Covers the fail-fast boundary in ``v4_phase12_strict_run``: the CLI must
reject ``--reasoning > 0`` with a local generator backend BEFORE the
pipeline/server starts (no out_dir is created, no server is launched),
while the remote OpenCode path still accepts and forwards the budget.

The remote path is only exercised up to config construction — actually
running ``run_with_runtime_config`` against a remote profile would hit the
network, which belongs to the fake-server contract suites, not here.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from pact_full_pipeline_runner_v1 import v4_phase12_strict_run as cli
from pact_v4.runtime.opencode_backend import OpenCodeServerBackendConfig
from pact_v4.runtime.runtime_config import OpenCodeBackendConfig


def _remote_cfg() -> OpenCodeBackendConfig:
    return OpenCodeBackendConfig(
        server=OpenCodeServerBackendConfig(
            base_url="http://127.0.0.1:4096",
            username="pact",
            password="secret",
            model_bindings={"generator": "opencode-go/deepseek-v4-flash"},
            structured_output_mode="prompt_only",
        )
    )


def _base_args(tmp_path: Path, **extra) -> list:
    args = [
        "--chapter-id", "test-chapter",
        "--chapter-html", str(tmp_path / "chapter.html"),
        "--memory-dir", str(tmp_path / "memory"),
        "--out-dir", str(tmp_path / "out"),
    ]
    for key, value in extra.items():
        args.append(f"--{key.replace('_', '-')}")
        args.append(str(value))
    return args


def test_run_local_default_rejects_reasoning_before_out_dir(tmp_path: Path):
    # Default local path (StrictBackendConfig = local_llama): --reasoning 1
    # must fail fast BEFORE out_dir creation / server start.
    args = cli.build_argparser().parse_args(
        _base_args(tmp_path, reasoning=1)
    )
    with pytest.raises(ValueError, match="requires an OpenCode"):
        cli.run_local_default(args)
    # Fail-fast boundary: nothing was created, no server was started.
    assert not (tmp_path / "out").exists()


def test_run_with_runtime_config_local_rejects_reasoning_before_out_dir(tmp_path: Path):
    # kind: local_llama runtime profile + --reasoning > 0 -> same fail-fast.
    cfg_path = tmp_path / "runtime_local.yaml"
    cfg_path.write_text(
        "kind: local_llama\n"
        "exe: C:/fake/llama-server.exe\n"
        "device: FAKE0\n"
        "host: 127.0.0.1\n"
        "port: 8094\n"
        "model_paths:\n"
        "  gemma: C:/fake/gemma.gguf\n"
        "  qwen: C:/fake/qwen.gguf\n"
        "model_names:\n"
        "  gemma: gemma-fake\n"
        "  qwen: qwen-fake\n"
        "server_args:\n"
        "  gemma: []\n"
        "  qwen: []\n",
        encoding="utf-8",
    )
    args = cli.build_argparser().parse_args(
        _base_args(tmp_path, reasoning=2, runtime_config=str(cfg_path))
    )
    with pytest.raises(ValueError, match="requires an OpenCode"):
        cli.run_with_runtime_config(args)
    assert not (tmp_path / "out").exists()


@pytest.mark.parametrize("level", [1, 2, 3])
def test_remote_config_reasoning_still_reaches_run_config(tmp_path: Path, level: int):
    # Non-regression: with an OpenCode generator the CLI must NOT reject the
    # combination and must forward the budget into StrictRunConfig.reasoning
    # (the same field GenerationParams consumes at Phase 2B).
    args = cli.build_argparser().parse_args(
        _base_args(tmp_path, reasoning=level)
    )
    cfg = cli._build_run_config(args, _remote_cfg())
    assert cfg.reasoning == level


def test_remote_config_reasoning_zero_is_baseline(tmp_path: Path):
    # reasoning=0 (default) is untouched by the policy on any backend.
    args = cli.build_argparser().parse_args(_base_args(tmp_path))
    assert args.reasoning == 0
    cfg = cli._build_run_config(args, _remote_cfg())
    assert cfg.reasoning == 0
