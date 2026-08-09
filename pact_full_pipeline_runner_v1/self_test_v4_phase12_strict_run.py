#!/usr/bin/env python3
"""Regression tests for the strict-driver CLI (V4 C3).

PR #97: --chapter-id/--chapter-html/--memory-dir used to default to
chapter_046's own paths, which made it easy to silently re-run that
chapter again instead of whichever one was actually intended. This test
locks in that they are now required, without needing a real llama-server
or chapter file -- argparse fails at parse time, before any of that is
touched.

V4 C3 (PR 4): --runtime-config loading (JSON/YAML -> tagged config),
--managed-server forcing, the local default staying the unchanged legacy
path without the flag, and no credential values leaking into
public_record().
"""
from __future__ import annotations

import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stderr
from pathlib import Path

# Make the self-test runnable from anywhere without PYTHONPATH: the runner
# dir (for ``import v4_phase12_strict_run``, the Phase 0A convention) and the
# repo root (for ``from pact_v4...`` imports inside the module under test).
_RUNNER_DIR = Path(__file__).resolve().parent
_REPO_ROOT = _RUNNER_DIR.parent
for _path in (_RUNNER_DIR, _REPO_ROOT):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

import v4_phase12_strict_run as m

REQUIRED_FLAGS = ("--chapter-id", "--chapter-html", "--memory-dir")

BASE_ARGS = [
    "--chapter-id", "046_subordination-6-3",
    "--chapter-html", "D:/pact/pact_chapters/0046_subordination-6-3.html",
    "--memory-dir", "D:/pact/pact_chapters",
    "--out-dir", "D:/pact/gate_bench_runs/test_run",
]


def _local_payload() -> dict:
    return {
        "kind": "local_llama",
        "exe": "C:/llama/llama-server.exe",
        "device": "SYCL0",
        "host": "127.0.0.1",
        "model_paths": {"gemma": "C:/m/gemma.gguf", "qwen": "C:/m/qwen.gguf"},
        "model_names": {"gemma": "gemma", "qwen": "qwen"},
        "server_args": {"gemma": ["-c", "32768"], "qwen": []},
        "port": 8094,
    }


def _remote_payload() -> dict:
    return {
        "kind": "opencode_server",
        "server_mode": "external",
        "base_url": "http://127.0.0.1:4096",
        "auth": {
            "type": "basic_env",
            "username_env": "SMOKE_C3_USER_ENV",
            "password_env": "SMOKE_C3_PASS_ENV",
        },
        "model_bindings": {
            "generator": "opencode-go/deepseek-v4-flash",
            "fidelity_reviewer": "opencode-go/qwen3.7-plus",
            "russian_selector": "opencode-go/qwen3.7-plus",
        },
    }


class RequiredChapterArgsTest(unittest.TestCase):
    def test_no_args_at_all_exits_nonzero(self):
        with redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as cm:
                m.build_argparser().parse_args([])
        self.assertNotEqual(cm.exception.code, 0)

    def test_missing_any_one_required_flag_exits_nonzero(self):
        for missing in REQUIRED_FLAGS:
            argv = []
            for k in REQUIRED_FLAGS:
                if k == missing:
                    continue
                argv += [k, BASE_ARGS[BASE_ARGS.index(k) + 1]]
            with redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit):
                    m.build_argparser().parse_args(argv)

    def test_all_required_flags_present_parses_successfully(self):
        args = m.build_argparser().parse_args(BASE_ARGS)
        self.assertEqual(args.chapter_id, "046_subordination-6-3")
        self.assertEqual(str(args.chapter_html), "D:\\pact\\pact_chapters\\0046_subordination-6-3.html")
        self.assertEqual(str(args.memory_dir), "D:\\pact\\pact_chapters")


class RuntimeConfigCliTest(unittest.TestCase):
    def test_no_runtime_config_flag_means_legacy_local_default(self):
        args = m.build_argparser().parse_args(BASE_ARGS)
        self.assertIsNone(args.runtime_config)
        self.assertFalse(args.managed_server)

    def test_runtime_config_and_managed_server_parse(self):
        args = m.build_argparser().parse_args(
            BASE_ARGS + ["--runtime-config", "configs/runtime_remote.example.yaml",
                         "--managed-server"]
        )
        self.assertEqual(str(args.runtime_config), "configs\\runtime_remote.example.yaml")
        self.assertTrue(args.managed_server)

    def test_load_json_local_config(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "local.json"
            path.write_text(json.dumps(_local_payload()), encoding="utf-8")
            cfg = m._load_runtime_config_file(path)
        from pact_v4.runtime.runtime_config import LocalLlamaBackendConfig
        self.assertIsInstance(cfg, LocalLlamaBackendConfig)

    def test_load_yaml_remote_config_records_env_refs_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "remote.yaml"
            path.write_text(
                "kind: opencode_server\n"
                "server_mode: external\n"
                "base_url: http://127.0.0.1:4096\n"
                "auth:\n"
                "  type: basic_env\n"
                "  username_env: SMOKE_C3_USER_ENV\n"
                "  password_env: SMOKE_C3_PASS_ENV\n"
                "model_bindings:\n"
                "  generator: opencode-go/deepseek-v4-flash\n",
                encoding="utf-8",
            )
            cfg = m._load_runtime_config_file(path)
        from pact_v4.runtime.runtime_config import OpenCodeBackendConfig
        self.assertIsInstance(cfg, OpenCodeBackendConfig)
        self.assertEqual(cfg.server.username_env, "SMOKE_C3_USER_ENV")
        self.assertEqual(cfg.server.password_env, "SMOKE_C3_PASS_ENV")
        # Secret *values* never enter the loaded config.
        self.assertIsNone(cfg.server.username)
        self.assertIsNone(cfg.server.password)

    def test_force_managed_switches_opencode_to_managed(self):
        from pact_v4.runtime.runtime_config import OpenCodeBackendConfig, load_runtime_config
        cfg = load_runtime_config(_remote_payload())
        managed = m.force_managed(cfg)
        self.assertIsInstance(managed, OpenCodeBackendConfig)
        self.assertEqual(managed.server_mode, "managed")

    def test_force_managed_rejects_top_level_local(self):
        from pact_v4.runtime.runtime_config import load_runtime_config
        cfg = load_runtime_config(_local_payload())
        with self.assertRaises(ValueError):
            m.force_managed(cfg)

    def test_force_managed_composite_manages_opencode_leaves_local(self):
        from pact_v4.runtime.runtime_config import (
            CompositeBackendConfig,
            LocalLlamaBackendConfig,
            OpenCodeBackendConfig,
            load_runtime_config,
        )
        composite = load_runtime_config({
            "kind": "composite",
            "backends": {
                "opencode": _remote_payload(),
                "local": _local_payload(),
            },
            "role_backend_map": {
                "generator": "opencode",
                "fidelity_reviewer": "opencode",
                "russian_selector": "local",
            },
        })
        managed = m.force_managed(composite)
        self.assertIsInstance(managed, CompositeBackendConfig)
        self.assertIsInstance(managed.backends["opencode"], OpenCodeBackendConfig)
        self.assertEqual(managed.backends["opencode"].server_mode, "managed")
        self.assertIsInstance(managed.backends["local"], LocalLlamaBackendConfig)

    def test_public_record_contains_no_credential_values(self):
        from pact_v4.runtime.runtime_config import load_runtime_config
        # Distinctive values: these could never appear as a substring of a
        # legitimate identity string like "pact-v4-neutral/v1".
        os.environ["SMOKE_C3_USER_ENV"] = "distinct-user-abc123"
        os.environ["SMOKE_C3_PASS_ENV"] = "distinct-pass-xyz789"
        try:
            cfg = load_runtime_config(_remote_payload())
            blob = json.dumps(cfg.public_record())
            self.assertNotIn("distinct-user-abc123", blob)
            self.assertNotIn("distinct-pass-xyz789", blob)
        finally:
            os.environ.pop("SMOKE_C3_USER_ENV", None)
            os.environ.pop("SMOKE_C3_PASS_ENV", None)


class RunLabelCliTest(unittest.TestCase):
    """B8: the historical trial run_label must stay the default, and a
    caller must be able to give a re-validation run its own label without
    changing the (identity-bearing) config hash."""

    DEFAULT_LABEL = "v4-phase12-strict-chapter-trial"

    def test_default_run_label_is_historical_trial_label(self):
        args = m.build_argparser().parse_args(BASE_ARGS)
        self.assertEqual(args.run_label, self.DEFAULT_LABEL)

    def test_custom_run_label_propagates_to_run_config(self):
        argv = BASE_ARGS + ["--run-label", "v4-phase12-strict-0001-run002"]
        args = m.build_argparser().parse_args(argv)
        self.assertEqual(args.run_label, "v4-phase12-strict-0001-run002")
        cfg = m._build_run_config(args, backend=None)
        self.assertEqual(cfg.run_label, "v4-phase12-strict-0001-run002")

    def test_run_label_does_not_participate_in_config_identity(self):
        # The label is a record/cosmetic field only: two configs differing
        # only in run_label must produce the same config_identity.
        args_default = m.build_argparser().parse_args(BASE_ARGS)
        args_custom = m.build_argparser().parse_args(
            BASE_ARGS + ["--run-label", "v4-phase12-strict-0001-run002"]
        )
        cfg_default = m._build_run_config(args_default, backend=None)
        cfg_custom = m._build_run_config(args_custom, backend=None)
        self.assertEqual(
            cfg_default.to_config_artifact(model_profile="local-llama/v1").config_identity,
            cfg_custom.to_config_artifact(model_profile="local-llama/v1").config_identity,
        )


class WholeChapterCliTest(unittest.TestCase):
    """V4.1 A1 CLI: --whole-chapter and the --stop-after-generation rename."""

    def test_whole_chapter_flag_parses_and_reaches_config(self):
        args = m.build_argparser().parse_args(BASE_ARGS + ["--whole-chapter"])
        self.assertTrue(args.whole_chapter)
        cfg = m._build_run_config(args, backend=None)
        self.assertTrue(cfg.whole_chapter)
        artifact = cfg.to_config_artifact(model_profile="test")
        self.assertIs(artifact.values["whole_chapter"], True)

    def test_default_is_chunked_mode(self):
        args = m.build_argparser().parse_args(BASE_ARGS)
        self.assertFalse(args.whole_chapter)
        cfg = m._build_run_config(args, backend=None)
        self.assertFalse(cfg.whole_chapter)
        artifact = cfg.to_config_artifact(model_profile="test")
        self.assertIs(artifact.values["whole_chapter"], False)

    def test_stop_after_generation_flag_sets_stop_after_generation(self):
        args = m.build_argparser().parse_args(BASE_ARGS + ["--stop-after-generation"])
        self.assertTrue(args.stop_after_generation)
        cfg = m._build_run_config(args, backend=None)
        self.assertEqual(cfg.stop_after, "generation")
        artifact = cfg.to_config_artifact(model_profile="test")
        self.assertEqual(artifact.values["stop_after"], "generation")

    def test_stop_after_generation_default_is_full_cycle(self):
        args = m.build_argparser().parse_args(BASE_ARGS)
        self.assertFalse(args.stop_after_generation)
        cfg = m._build_run_config(args, backend=None)
        self.assertEqual(cfg.stop_after, "")
        artifact = cfg.to_config_artifact(model_profile="test")
        self.assertEqual(artifact.values["stop_after"], "")

    def test_generation_max_tokens_default_is_32768(self):
        args = m.build_argparser().parse_args(BASE_ARGS)
        cfg = m._build_run_config(args, backend=None)
        self.assertEqual(cfg.max_tokens, 32768)
        artifact = cfg.to_config_artifact(model_profile="test")
        self.assertEqual(artifact.values["generation"]["max_tokens"], 32768)


if __name__ == "__main__":
    unittest.main()

