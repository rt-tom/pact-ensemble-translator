"""Card C regression guards: the model-fallback path is gone from the whole
formatting stack.

The audit (docs/plans/V4_1_FORMATTING_DETERMINISTIC_TASK_RU.md) counted 15
model-call sites: the ``model_fallback`` tier and its protocol/parsers in
``pact_v4/phase5/formatting.py``, ``BackendFormattingCaller`` in
``backend_role_adapters.py``, ``FORMAT_SPANS_V1`` and the formatting prompt
renders in ``prompts_runtime.py``, ``build_formatting_adapters`` in
``runtime_config.py``, and the strict-runner wiring. Card C removed all of
them (rule "formatting = 0 model calls"); these tests fail the suite the
moment any of the removed symbols or injection points is re-introduced.
"""
from __future__ import annotations

import ast
import inspect
from pathlib import Path

import pact_v4.phase5.formatting as fmt
import pact_v4.pipeline.v4_phase12_strict_runner as runner
import pact_v4.runtime.backend_role_adapters as bra
import pact_v4.runtime.prompts_runtime as pr
import pact_v4.runtime.runtime_config as rc


def test_formatting_module_has_no_model_tier_or_caller():
    # formatting.py: the model fallback tier, its protocol and its
    # model-mapping helpers are gone (they exist only in docstrings now).
    assert not hasattr(fmt, "FormattingCaller")
    assert not hasattr(fmt, "TIER_MODEL")
    for name in ("_parse_format_mappings", "_apply_model_mappings"):
        assert not hasattr(fmt, name), f"model-mapping helper {name} resurfaced"


def test_run_formatting_align_takes_no_caller():
    # run_formatting_align signature: no caller injection point and no batch
    # path remains — formatting cannot call a model even if asked to.
    params = set(inspect.signature(fmt.run_formatting_align).parameters)
    assert "formatting_caller" not in params
    assert "pid_batches" not in params


def test_runtime_stack_has_no_formatting_caller_adapters():
    # backend_role_adapters / prompts_runtime / runtime_config no longer
    # build or render a formatting model path.
    assert not hasattr(bra, "BackendFormattingCaller")
    assert not hasattr(pr, "FORMAT_SPANS_V1")
    assert not hasattr(pr, "render_formatting_prompt")
    assert not hasattr(pr, "render_formatting_prompt_batch")
    assert not hasattr(rc, "build_formatting_adapters")


def test_strict_runner_does_not_wire_a_formatting_caller():
    # Source-level guard on the strict runner: the formatting step must not
    # reference a ``formatting_caller`` variable/attribute anywhere (the
    # audit listed the runner wiring among the model-call sites). AST Name
    # nodes only — docstring/comment mentions are not matches.
    tree = ast.parse(Path(runner.__file__).read_text(encoding="utf-8"))
    names = [n.id for n in ast.walk(tree) if isinstance(n, ast.Name)]
    assert "formatting_caller" not in names
