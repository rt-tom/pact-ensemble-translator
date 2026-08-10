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
from pact_v4.runtime.runtime_config import (
    LocalLlamaBackendConfig,
    OpenCodeBackendConfig,
)


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


# ---------------------------------------------------------------------------
# A2 RV finding 1 (whole-chapter retry ownership): run_local_default must pass
# JsonRetryPolicy(max_retries=0) to build_strict_lifecycle when --whole-chapter
# (the generation layer WholeChapterRetryPolicy is the single retry owner) and
# None for the chunked path (default adapter budget max_retries=2 preserved).
# ---------------------------------------------------------------------------


def test_run_local_default_whole_chapter_wires_single_retry_owner(
    tmp_path: Path, monkeypatch
):
    from pact_v4.runtime.json_resilience import JsonRetryPolicy

    captured = {}

    def _fake_lifecycle(backend, *, log_dir, bible_text="", json_retry_policy=None):
        captured["json_retry_policy"] = json_retry_policy
        raise RuntimeError("captured")

    monkeypatch.setattr(cli, "build_strict_lifecycle", _fake_lifecycle)

    args_wc = cli.build_argparser().parse_args(_base_args(tmp_path) + ["--whole-chapter"])
    with pytest.raises(RuntimeError, match="captured"):
        cli.run_local_default(args_wc)
    assert captured["json_retry_policy"] is not None
    assert captured["json_retry_policy"].max_retries == 0

    captured.clear()
    args_chunked = cli.build_argparser().parse_args(_base_args(tmp_path))
    with pytest.raises(RuntimeError, match="captured"):
        cli.run_local_default(args_chunked)
    assert captured["json_retry_policy"] is None


# ---------------------------------------------------------------------------
# F2 (B3 review): CLI exit semantics — B3 fail-closed/failed must exit
# non-zero; intentional --skip-audit / generation-only stays successful.
# ---------------------------------------------------------------------------


def _fake_result(*, step8: dict, halted_early: bool = False) -> Any:
    class _R:
        pass

    r = _R()
    r.halted_early = halted_early
    r.step8 = step8
    return r


def test_cli_exit_code_zero_on_success_and_skip():
    # complete/released -> 0
    assert cli._cli_exit_code(_fake_result(step8={
        "status": "complete", "released_as_audited": True,
    })) == 0
    # intentional --skip-audit / generation-only -> 0
    assert cli._cli_exit_code(_fake_result(step8={
        "status": "skipped", "reason": "whole_chapter_generation_only",
    })) == 0
    assert cli._cli_exit_code(_fake_result(step8={
        "status": "skipped_stop_after_generation",
    })) == 0


def test_cli_exit_code_nonzero_on_b3_fail_closed_and_failed():
    # B3 fail-closed (audit incomplete) -> 3
    assert cli._cli_exit_code(_fake_result(step8={
        "status": "fail_closed_audit_incomplete",
        "released_as_audited": False,
    })) == 3
    # B3 exception (runner records step6/7/8 failed) -> 3
    assert cli._cli_exit_code(_fake_result(step8={
        "status": "failed", "error": "boom",
    })) == 3
    # F1 repair debt (accepted_degraded, released_as_audited=False) -> 3
    assert cli._cli_exit_code(_fake_result(step8={
        "status": "accepted_degraded", "released_as_audited": False,
    })) == 3


def test_cli_exit_code_halted_early_stays_2():
    assert cli._cli_exit_code(_fake_result(
        step8={"status": "complete", "released_as_audited": True},
        halted_early=True,
    )) == 2


# ---------------------------------------------------------------------------
# F3 (B3 review): the default local Qwen audit transport is B3-capable (MTP
# draft, reasoning 8192, context 49k) and a non-B3 local profile fails loudly
# when the B3 audit would run.
# ---------------------------------------------------------------------------


def test_default_local_qwen_profile_is_b3_capable():
    # QWEN_PATH points at the MTP model variant.
    assert "Qwen3.6-35B-A3B-MTP" in str(cli.QWEN_PATH)
    # Server args express the B3 audit contract: MTP draft, reasoning on,
    # reasoning-budget 8192, context 49152.
    args = cli.QWEN_SERVER_ARGS
    assert args[args.index("--spec-type") + 1] == "draft-mtp"
    assert args[args.index("--reasoning-budget") + 1] == "8192"
    assert args[args.index("-c") + 1] == "49152"
    assert "--reasoning" in args


def test_default_local_backend_passes_b3_profile_validation(tmp_path: Path):
    args = cli.build_argparser().parse_args(
        _base_args(tmp_path) + ["--whole-chapter"]
    )
    backend = cli.StrictBackendConfig(
        exe=Path(r"C:\src\llama-sycl-edge\build\bin\llama-server.exe"),
        device="SYCL0", host=args.host,
        model_paths={"gemma": cli.GEMMA_PATH, "qwen": cli.QWEN_PATH},
        model_names={"gemma": cli.GEMMA_PATH.name, "qwen": cli.QWEN_PATH.name},
        server_args={
            "gemma": cli._gemma_server_args_for_reasoning(args.reasoning),
            "qwen": cli.QWEN_SERVER_ARGS,
        },
        port=args.port, startup_timeout=args.startup_timeout,
        unload_timeout=args.unload_timeout,
    )
    # The B3-capable default profile passes validation (no exception).
    cli._validate_b3_qwen_profile(args, backend)


def test_non_b3_local_profile_fails_loudly(tmp_path: Path):
    args = cli.build_argparser().parse_args(
        _base_args(tmp_path) + ["--whole-chapter"]
    )
    backend = cli.StrictBackendConfig(
        exe=Path(r"C:\src\llama-sycl-edge\build\bin\llama-server.exe"),
        device="SYCL0", host=args.host,
        model_paths={"gemma": cli.GEMMA_PATH, "qwen": cli.QWEN_PATH},
        model_names={"gemma": cli.GEMMA_PATH.name, "qwen": cli.QWEN_PATH.name},
        server_args={
            "gemma": cli._gemma_server_args_for_reasoning(args.reasoning),
            # historical non-B3 Qwen profile: reasoning-budget 0, no MTP, 32k
            "qwen": [
                "--load-mode", "mmap",
                "--reasoning-budget", "0",
                "-c", "32768",
            ],
        },
        port=args.port, startup_timeout=args.startup_timeout,
        unload_timeout=args.unload_timeout,
    )
    with pytest.raises(ValueError, match="B3-capable"):
        cli._validate_b3_qwen_profile(args, backend)


def test_non_b3_local_profile_ok_when_skip_audit(tmp_path: Path):
    # --skip-audit turns the B3 stage off -> the non-B3 profile is fine.
    args = cli.build_argparser().parse_args(
        _base_args(tmp_path) + ["--whole-chapter", "--skip-audit"]
    )
    backend = cli.StrictBackendConfig(
        exe=Path(r"C:\src\llama-sycl-edge\build\bin\llama-server.exe"),
        device="SYCL0", host=args.host,
        model_paths={"gemma": cli.GEMMA_PATH, "qwen": cli.QWEN_PATH},
        model_names={"gemma": cli.GEMMA_PATH.name, "qwen": cli.QWEN_PATH.name},
        server_args={
            "gemma": cli._gemma_server_args_for_reasoning(args.reasoning),
            "qwen": [
                "--load-mode", "mmap",
                "--reasoning-budget", "0",
                "-c", "32768",
            ],
        },
        port=args.port, startup_timeout=args.startup_timeout,
        unload_timeout=args.unload_timeout,
    )
    cli._validate_b3_qwen_profile(args, backend)  # no exception


def test_default_local_qwen_transport_in_backend_identity(tmp_path: Path):
    # The effective transport (server_args) is part of the backend identity
    # hash — changing the Qwen profile invalidates old B3 caches/resume.
    base = cli.StrictBackendConfig(
        exe=Path(r"C:\src\llama-sycl-edge\build\bin\llama-server.exe"),
        device="SYCL0", host="127.0.0.1",
        model_paths={"gemma": cli.GEMMA_PATH, "qwen": cli.QWEN_PATH},
        model_names={"gemma": cli.GEMMA_PATH.name, "qwen": cli.QWEN_PATH.name},
        server_args={"gemma": cli.GEMMA_SERVER_ARGS, "qwen": cli.QWEN_SERVER_ARGS},
        port=8094, startup_timeout=240.0, unload_timeout=30.0,
    )
    other = cli.StrictBackendConfig(
        exe=Path(r"C:\src\llama-sycl-edge\build\bin\llama-server.exe"),
        device="SYCL0", host="127.0.0.1",
        model_paths={"gemma": cli.GEMMA_PATH, "qwen": cli.QWEN_PATH},
        model_names={"gemma": cli.GEMMA_PATH.name, "qwen": cli.QWEN_PATH.name},
        server_args={
            "gemma": cli.GEMMA_SERVER_ARGS,
            "qwen": list(cli.QWEN_SERVER_ARGS[:-2]) + ["--reasoning-budget", "0", "-c", "32768"],
        },
        port=8094, startup_timeout=240.0, unload_timeout=30.0,
    )
    assert base.identity_hash != other.identity_hash
    record = base.build_descriptor().public_record()
    qwen_args = record["effective_options"]["server_args"]["qwen"]
    assert qwen_args[qwen_args.index("--reasoning-budget") + 1] == "8192"


# ---------------------------------------------------------------------------
# F1 (RV2 B3 review): configs/runtime_local.example.yaml must be internally
# consistent — the qwen model path/name is the MTP variant when the server
# args declare MTP draft transport, and _validate_b3_qwen_profile rejects a
# non-MTP model path with MTP flags instead of silently accepting it.
# ---------------------------------------------------------------------------


def _repo_root() -> Path:
    return Path(__file__).parents[2]


def _example_local_backend():
    """Load configs/runtime_local.example.yaml through the CLI loader."""
    yaml = pytest.importorskip("yaml")
    path = _repo_root() / "configs" / "runtime_local.example.yaml"
    return cli._load_runtime_config_file(path)


def test_example_local_profile_qwen_is_mtp_consistent(tmp_path: Path):
    # F1: the shipped example local profile must be self-consistent — the
    # qwen model path points at the MTP variant (…/Qwen3.6-35B-A3B-MTP/…)
    # while the server args declare MTP draft transport. A real
    # --runtime-config configs/runtime_local.example.yaml --whole-chapter
    # run must pass B3 profile validation, not silently start a non-MTP
    # server with MTP flags.
    backend = _example_local_backend()
    assert isinstance(backend, LocalLlamaBackendConfig)
    qwen_path = str(backend.model_paths["qwen"])
    assert "MTP" in qwen_path, qwen_path
    args = cli.build_argparser().parse_args(
        _base_args(tmp_path) + ["--whole-chapter"]
    )
    # No exception: the example profile is B3-capable and self-consistent.
    cli._validate_b3_qwen_profile(args, backend)


def test_non_mtp_model_path_with_mtp_flags_fails_loudly(tmp_path: Path):
    # F1: a local profile whose qwen server args declare MTP draft but whose
    # model_paths.qwen points at the NON-MTP variant is an unsupported
    # mismatch — validation must fail loudly (never launch non-MTP with MTP
    # flags). This is the exact regression the example config used to have
    # before the F1 fix.
    args = cli.build_argparser().parse_args(
        _base_args(tmp_path) + ["--whole-chapter"]
    )
    backend = cli.StrictBackendConfig(
        exe=Path(r"C:\src\llama-sycl-edge\build\bin\llama-server.exe"),
        device="SYCL0", host=args.host,
        model_paths={
            "gemma": cli.GEMMA_PATH,
            # non-MTP Qwen variant (the F1 mismatch)
            "qwen": Path(r"C:/llama-cpp/models/Qwen3.6-35B-A3B/Qwen3.6-35B-A3B-UD-Q4_K_XL.gguf"),
        },
        model_names={
            "gemma": cli.GEMMA_PATH.name,
            "qwen": "Qwen3.6-35B-A3B-UD-Q4_K_XL.gguf",
        },
        server_args={
            "gemma": cli._gemma_server_args_for_reasoning(args.reasoning),
            "qwen": cli.QWEN_SERVER_ARGS,  # MTP draft, reasoning 8192, 49k
        },
        port=args.port, startup_timeout=args.startup_timeout,
        unload_timeout=args.unload_timeout,
    )
    with pytest.raises(ValueError, match="MTP variant"):
        cli._validate_b3_qwen_profile(args, backend)


def test_non_mtp_model_path_with_mtp_flags_ok_when_skip_audit(tmp_path: Path):
    # F1: the MTP-path consistency check applies only when the B3 audit will
    # actually run — with --skip-audit the non-MTP profile is allowed (the
    # audit never starts a Qwen server).
    args = cli.build_argparser().parse_args(
        _base_args(tmp_path) + ["--whole-chapter", "--skip-audit"]
    )
    backend = cli.StrictBackendConfig(
        exe=Path(r"C:\src\llama-sycl-edge\build\bin\llama-server.exe"),
        device="SYCL0", host=args.host,
        model_paths={
            "gemma": cli.GEMMA_PATH,
            "qwen": Path(r"C:/llama-cpp/models/Qwen3.6-35B-A3B/Qwen3.6-35B-A3B-UD-Q4_K_XL.gguf"),
        },
        model_names={
            "gemma": cli.GEMMA_PATH.name,
            "qwen": "Qwen3.6-35B-A3B-UD-Q4_K_XL.gguf",
        },
        server_args={
            "gemma": cli._gemma_server_args_for_reasoning(args.reasoning),
            "qwen": cli.QWEN_SERVER_ARGS,
        },
        port=args.port, startup_timeout=args.startup_timeout,
        unload_timeout=args.unload_timeout,
    )
    cli._validate_b3_qwen_profile(args, backend)  # no exception


# ---------------------------------------------------------------------------
# RV3 (HIGH, t_a0500b7e): _validate_b3_qwen_profile must enforce an EXACT
# MTP-variant identity on the ACTUAL qwen model path — a substring match
# (…/Qwen-non-MTP.gguf, …/MTP-disabled/…) or a forged model_names.qwen can
# never satisfy the B3 MTP requirement, and a misleading name cannot override
# a non-MTP path.
# ---------------------------------------------------------------------------


def _b3_local_backend(args, qwen_path: Path, qwen_name: str):
    return cli.StrictBackendConfig(
        exe=Path(r"C:\src\llama-sycl-edge\build\bin\llama-server.exe"),
        device="SYCL0", host=args.host,
        model_paths={"gemma": cli.GEMMA_PATH, "qwen": qwen_path},
        model_names={"gemma": cli.GEMMA_PATH.name, "qwen": qwen_name},
        server_args={
            "gemma": cli._gemma_server_args_for_reasoning(args.reasoning),
            "qwen": cli.QWEN_SERVER_ARGS,  # MTP draft, reasoning 8192, 49k
        },
        port=args.port, startup_timeout=args.startup_timeout,
        unload_timeout=args.unload_timeout,
    )


@pytest.mark.parametrize(
    "qwen_path, qwen_name",
    [
        # substring false positive: "MTP" inside a non-MTP file name
        (Path(r"C:/llama-cpp/models/Qwen-non-MTP.gguf"), "Qwen-non-MTP.gguf"),
        # substring false positive: "MTP" as a prefix of a disabled dir
        (Path(r"C:/llama-cpp/models/MTP-disabled/Qwen3.6-35B-A3B-UD-Q4_K_XL.gguf"),
         "Qwen3.6-35B-A3B-UD-Q4_K_XL.gguf"),
        # forged MTP name must NOT override a non-MTP path (BYPASS probe)
        (Path(r"C:/llama-cpp/models/Qwen3.6-35B-A3B/Qwen3.6-35B-A3B-UD-Q4_K_XL.gguf"),
         "Qwen-MTP.gguf"),
        # clearly non-MTP path and name
        (Path(r"C:/llama-cpp/models/Qwen3.6-35B-A3B/Qwen3.6-35B-A3B-UD-Q4_K_XL.gguf"),
         "Qwen3.6-35B-A3B-UD-Q4_K_XL.gguf"),
    ],
)
def test_mtp_identity_adversarial_paths_fail_loudly(
    tmp_path: Path, qwen_path: Path, qwen_name: str
):
    args = cli.build_argparser().parse_args(
        _base_args(tmp_path) + ["--whole-chapter"]
    )
    backend = _b3_local_backend(args, qwen_path, qwen_name)
    with pytest.raises(ValueError, match="MTP variant"):
        cli._validate_b3_qwen_profile(args, backend)


def test_mtp_identity_valid_path_but_name_negates_mtp_fails_loudly(tmp_path: Path):
    # Name/path coherence: the path IS the exact MTP variant, but a name that
    # explicitly negates MTP contradicts it — must fail loudly too.
    args = cli.build_argparser().parse_args(
        _base_args(tmp_path) + ["--whole-chapter"]
    )
    backend = _b3_local_backend(args, cli.QWEN_PATH, "Qwen-non-MTP.gguf")
    with pytest.raises(ValueError, match="contradicts"):
        cli._validate_b3_qwen_profile(args, backend)
