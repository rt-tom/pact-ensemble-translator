#!/usr/bin/env python3
"""Sequential-model (two-pass) Phase 1C→2A→2B→2C driver for one chapter.

Unlike ``v4_phase12_draft_run.py`` (which needs Gemma and Qwen resident
at the same time, per ``v4_phase12_draft_runner.run_chapter``'s
interleaved design), this CLI is split into two independent phases so
each needs only one model loaded at a time:

Usage::

    # Phase 1: only Gemma needs to be running.
    python -m pact_full_pipeline_runner_v1.v4_phase12_sequential_run \\
        --phase generate \\
        --chapter-html "D:/pact/pact_chapters/0046_subordination-6-3.html" \\
        --memory-dir "D:/pact/pact_chapters" \\
        --out-dir "D:/pact/gate_bench_runs/v4_phase12_046_seq/draft_001" \\
        --gemma-url http://127.0.0.1:8080/v1/chat/completions \\
        --gemma-model gemma-4-26B-A4B-it-UD-Q4_K_XL.gguf

    # <operator swaps llama-server's loaded model here>

    # Phase 2: only Qwen needs to be running (Gemma optional).
    python -m pact_full_pipeline_runner_v1.v4_phase12_sequential_run \\
        --phase select \\
        --out-dir "D:/pact/gate_bench_runs/v4_phase12_046_seq/draft_001" \\
        --qwen-url http://127.0.0.1:8080/v1/chat/completions \\
        --qwen-model qwen-....gguf \\
        [--gemma-url ... --gemma-model ...]   # optional Gemma preference

This script never starts or stops ``llama-server`` itself -- swapping
the loaded model between phases is the operator's job.

The ``select`` phase's output (``translations.json`` + ``provenance.json``)
uses the same schema as ``v4_phase12_draft_run.py``'s, so
``v4_v3_draft_compare.py`` reads it without any changes.
"""
from __future__ import annotations

import argparse
import json
import logging
from pathlib import Path
from typing import Optional, Sequence

from pact_v4.phase1.chunker import (
    DEFAULT_MAX_WORDS,
    DEFAULT_MIN_WORDS,
    DEFAULT_TARGET_WORDS,
)
from pact_v4.pipeline.v4_phase12_sequential_runner import (
    SequentialGenerateConfig,
    SequentialSelectConfig,
    run_generate,
    run_select,
)
from pact_v4.runtime.api_client import ApiClient, ApiClientConfig
from pact_v4.runtime.gemma_selector import HttpGemmaSelector, HttpGemmaSelectorConfig
from pact_v4.runtime.model_caller import HttpModelCaller, HttpModelCallerConfig
from pact_v4.runtime.qwen_evaluator import HttpQwenEvaluator, HttpQwenEvaluatorConfig

LOG = logging.getLogger("v4_phase12_sequential_run")


def build_argparser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="pact_full_pipeline_runner_v1.v4_phase12_sequential_run",
        description=(
            "Two-pass (generate/select) Phase 1C-2C driver for one chapter, "
            "for hardware that cannot keep Gemma and Qwen resident at once."
        ),
    )
    p.add_argument("--phase", choices=("generate", "select"), required=True)
    p.add_argument(
        "--out-dir", type=Path, required=True,
        help="Directory to write/read artefacts. Shared between both phases.",
    )
    # generate-only ---------------------------------------------------
    p.add_argument("--chapter-html", type=Path, help="[generate] EN chapter HTML path.")
    p.add_argument("--memory-dir", type=Path, help="[generate] glossary.json/book_memory.json dir.")
    p.add_argument(
        "--chapter-id", default=None,
        help="[generate] Stable chapter id; defaults to --chapter-html's stem.",
    )
    p.add_argument("--min-chunk-words", type=int, default=DEFAULT_MIN_WORDS)
    p.add_argument("--target-chunk-words", type=int, default=DEFAULT_TARGET_WORDS)
    p.add_argument("--max-chunk-words", type=int, default=DEFAULT_MAX_WORDS)
    p.add_argument("--right-context-pids", type=int, default=0)
    p.add_argument("--temperature", type=float, default=0.2)
    p.add_argument("--seed", type=int, default=7)
    p.add_argument("--max-tokens", type=int, default=8192)
    # model endpoints ---------------------------------------------------
    p.add_argument(
        "--gemma-url", default="http://127.0.0.1:8080/v1/chat/completions",
        help="Gemma chat-completions URL ([generate]: generator; [select]: optional preference selector).",
    )
    p.add_argument("--gemma-model", default="gemma-4-26B-A4B-it-UD-Q4_K_XL.gguf")
    p.add_argument(
        "--use-gemma-selector", action="store_true",
        help="[select] Also call Gemma for Russian preference (requires Gemma resident during select).",
    )
    p.add_argument(
        "--qwen-url", default="http://127.0.0.1:8080/v1/chat/completions",
        help="[select] Qwen chat-completions URL (fidelity gate).",
    )
    p.add_argument("--qwen-model", default="qwen.gguf")
    p.add_argument("--run-label", default=None)
    p.add_argument("--log-level", default="INFO", choices=("DEBUG", "INFO", "WARNING", "ERROR"))
    return p


def _run_generate(args: argparse.Namespace) -> int:
    if args.chapter_html is None or args.memory_dir is None:
        raise SystemExit("--phase generate requires --chapter-html and --memory-dir")
    chapter_id = args.chapter_id or args.chapter_html.stem
    cfg = SequentialGenerateConfig(
        chapter_id=chapter_id,
        chapter_html_path=args.chapter_html,
        memory_dir=args.memory_dir,
        out_dir=args.out_dir,
        min_chunk_words=args.min_chunk_words,
        target_chunk_words=args.target_chunk_words,
        max_chunk_words=args.max_chunk_words,
        right_context_pids=args.right_context_pids,
        temperature=args.temperature,
        seed=args.seed,
        max_tokens=args.max_tokens,
        run_label=args.run_label or "v4-phase12-sequential-generate",
    )
    gemma_api = ApiClient(
        ApiClientConfig(chat_url=args.gemma_url, model=args.gemma_model),
        name="gemma-phase2b-sequential",
    )
    model_caller = HttpModelCaller(api=gemma_api, config=HttpModelCallerConfig(
        api=gemma_api.config, label=gemma_api.name, max_tokens=args.max_tokens,
    ))
    LOG.info("Starting sequential generate pass: chapter=%s out=%s", chapter_id, args.out_dir)
    result = run_generate(cfg, model_caller=model_caller)
    LOG.info(
        "Generate pass finished: chunks=%d bundle=%s",
        result.chunk_count, result.generation_bundle_path,
    )
    print(json.dumps({
        "phase": "generate",
        "chapter_id": result.chapter_id,
        "out_dir": str(result.out_dir),
        "chunk_count": result.chunk_count,
        "generation_bundle_path": str(result.generation_bundle_path),
    }, ensure_ascii=False, indent=2))
    return 0


def _run_select(args: argparse.Namespace) -> int:
    bundle_path = args.out_dir / "generation_bundle.json"
    cfg = SequentialSelectConfig(
        generation_bundle_path=bundle_path,
        out_dir=args.out_dir,
        run_label=args.run_label or "v4-phase12-sequential-select",
    )
    qwen_api = ApiClient(
        ApiClientConfig(chat_url=args.qwen_url, model=args.qwen_model),
        name="qwen-phase2c-sequential",
    )
    qwen_evaluator = HttpQwenEvaluator(api=qwen_api, config=HttpQwenEvaluatorConfig(
        api=qwen_api.config, label=qwen_api.name,
    ))
    gemma_selector = None
    if args.use_gemma_selector:
        gemma_api = ApiClient(
            ApiClientConfig(chat_url=args.gemma_url, model=args.gemma_model),
            name="gemma-phase2c-selector-sequential",
        )
        gemma_selector = HttpGemmaSelector(api=gemma_api, config=HttpGemmaSelectorConfig(
            api=gemma_api.config, label=gemma_api.name,
        ))
    LOG.info("Starting sequential select pass: bundle=%s out=%s", bundle_path, args.out_dir)
    result = run_select(cfg, qwen_evaluator=qwen_evaluator, gemma_selector=gemma_selector)
    LOG.info(
        "Select pass finished: chunks=%d selected=%d quarantined=%d needs_synthesis=%d "
        "incomplete_generation=%d role_counts=%s",
        result.chunk_count, result.selected_count, result.quarantined_count,
        result.needs_synthesis_count, result.incomplete_generation_count,
        result.selected_role_counts,
    )
    print(json.dumps({
        "phase": "select",
        "chapter_id": result.chapter_id,
        "out_dir": str(result.out_dir),
        "chunk_count": result.chunk_count,
        "selected_count": result.selected_count,
        "quarantined_count": result.quarantined_count,
        "needs_synthesis_count": result.needs_synthesis_count,
        "incomplete_generation_count": result.incomplete_generation_count,
        "selected_role_counts": result.selected_role_counts,
        "provenance_path": str(result.provenance_path),
        "translations_path": str(result.translations_path),
    }, ensure_ascii=False, indent=2))
    return 0


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_argparser().parse_args(argv)
    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )
    if args.phase == "generate":
        return _run_generate(args)
    return _run_select(args)


if __name__ == "__main__":
    raise SystemExit(main())
