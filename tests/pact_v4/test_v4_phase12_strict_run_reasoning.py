"""V4.1 reasoning/backend CLI wiring tests (review commit 301e9df, A2 update).

Covers the reasoning/backend boundary in ``v4_phase12_strict_run``. Since
V4.1 A2 (owner-verified 2026-08-08: ``--reasoning-budget 2048`` works) the
local generator receives its reasoning budget from the SERVER ARGS, not
from request_options — so ``--reasoning > 0`` is accepted with BOTH local
and remote backends (no fail-fast), and the budget is forwarded into
``StrictRunConfig.reasoning`` for every backend.

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


@pytest.mark.parametrize("level", [1, 2, 3])
def test_run_local_default_accepts_reasoning(tmp_path: Path, level: int):
    # V4.1 A2: local no longer fail-fasts on --reasoning > 0 — the local
    # generator receives the budget from server args (--reasoning-budget),
    # so the combination is a supported path.
    args = cli.build_argparser().parse_args(
        _base_args(tmp_path, reasoning=level)
    )
    cfg = cli._build_run_config(args, cli.StrictBackendConfig(
        exe=Path(r"C:\src\llama-sycl-edge\build\bin\llama-server.exe"),
        device="SYCL0", host=args.host,
        model_paths={"gemma": cli.GEMMA_PATH, "qwen": cli.QWEN_PATH},
        model_names={"gemma": cli.GEMMA_PATH.name, "qwen": cli.QWEN_PATH.name},
        server_args={"gemma": cli.GEMMA_SERVER_ARGS, "qwen": cli.QWEN_SERVER_ARGS},
        port=args.port, startup_timeout=args.startup_timeout, unload_timeout=args.unload_timeout,
    ))
    assert cfg.reasoning == level


@pytest.mark.parametrize("level", [1, 2, 3])
def test_run_with_runtime_config_local_accepts_reasoning(tmp_path: Path, level: int):
    # kind: local_llama runtime profile + --reasoning > 0 -> supported (A2),
    # provided the profile's generator server args EXPRESS the budget
    # (--reasoning-budget 2048, plan §3.4) — see the A2 RV policy.
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
        "  gemma:\n"
        "    - -ngl\n"
        "    - \"99\"\n"
        "    - --reasoning-budget\n"
        "    - \"2048\"\n"
        "  qwen: []\n",
        encoding="utf-8",
    )
    args = cli.build_argparser().parse_args(
        _base_args(tmp_path, reasoning=level, runtime_config=str(cfg_path))
    )
    cfg = cli._build_run_config(args, cli._load_runtime_config_file(cfg_path))
    assert cfg.reasoning == level


@pytest.mark.parametrize("level", [1, 2, 3])
def test_run_with_runtime_config_local_rejects_reasoning_without_budget(tmp_path: Path, level: int):
    # A2 RV fix: a local profile whose generator server_args carry NO
    # --reasoning-budget cannot express --reasoning > 0 — the run must fail
    # closed at validation, not silently record a reasoning value the server
    # never executes. (The CLI path calls validate_reasoning_backend before
    # building the run config.)
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
        _base_args(tmp_path, reasoning=level, runtime_config=str(cfg_path))
    )
    backend = cli._load_runtime_config_file(cfg_path)
    with pytest.raises(ValueError, match="--reasoning-budget"):
        cli.validate_reasoning_backend(args.reasoning, backend)


def test_local_default_gemma_server_args_follow_reasoning():
    # A2 RV fix: the default local CLI path DERIVES the Gemma server args
    # from --reasoning — budget 2048 (§3.4) for reasoning > 0, budget 0 for
    # the B1 baseline — so config identity and the actual server args always
    # agree (a default --reasoning 0 run never starts the server with a
    # reasoning budget the identity denies).
    nonzero = cli._gemma_server_args_for_reasoning(2)
    assert "--reasoning-budget" in nonzero
    assert nonzero[nonzero.index("--reasoning-budget") + 1] == "2048"
    assert nonzero[0] == "-ngl"  # base §3.4 args otherwise unchanged
    baseline = cli._gemma_server_args_for_reasoning(0)
    assert baseline[baseline.index("--reasoning-budget") + 1] == "0"
    # The §3.4 constant itself is untouched (the A2 profile).
    assert "2048" in cli.GEMMA_SERVER_ARGS


def test_local_default_reasoning_validation_passes_with_derived_args(tmp_path: Path):
    # End-to-end agreement: building the default local backend for
    # --reasoning 2 (server args budget 2048) passes validate_reasoning_backend;
    # building it for the default --reasoning 0 (budget 0) also passes.
    args_hi = cli.build_argparser().parse_args(_base_args(tmp_path, reasoning=2))
    backend_hi = cli.StrictBackendConfig(
        exe=Path(r"C:\src\llama-sycl-edge\build\bin\llama-server.exe"),
        device="SYCL0", host=args_hi.host,
        model_paths={"gemma": cli.GEMMA_PATH, "qwen": cli.QWEN_PATH},
        model_names={"gemma": cli.GEMMA_PATH.name, "qwen": cli.QWEN_PATH.name},
        server_args={
            "gemma": cli._gemma_server_args_for_reasoning(args_hi.reasoning),
            "qwen": cli.QWEN_SERVER_ARGS,
        },
        port=args_hi.port, startup_timeout=args_hi.startup_timeout,
        unload_timeout=args_hi.unload_timeout,
    )
    cli.validate_reasoning_backend(args_hi.reasoning, backend_hi)

    args_zero = cli.build_argparser().parse_args(_base_args(tmp_path))
    backend_zero = cli.StrictBackendConfig(
        exe=Path(r"C:\src\llama-sycl-edge\build\bin\llama-server.exe"),
        device="SYCL0", host=args_zero.host,
        model_paths={"gemma": cli.GEMMA_PATH, "qwen": cli.QWEN_PATH},
        model_names={"gemma": cli.GEMMA_PATH.name, "qwen": cli.QWEN_PATH.name},
        server_args={
            "gemma": cli._gemma_server_args_for_reasoning(args_zero.reasoning),
            "qwen": cli.QWEN_SERVER_ARGS,
        },
        port=args_zero.port, startup_timeout=args_zero.startup_timeout,
        unload_timeout=args_zero.unload_timeout,
    )
    cli.validate_reasoning_backend(args_zero.reasoning, backend_zero)


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
