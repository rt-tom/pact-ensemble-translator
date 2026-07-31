#!/usr/bin/env python3
"""Phase 1C → 2A → 2B → 2C end-to-end driver for chapter 046.

Usage::

    python -m pact_full_pipeline_runner_v1.v4_phase12_draft_run \\
        --chapter-html "D:/pact/pact_chapters/0046_subordination-6-3.html" \\
        --memory-dir "D:/pact/pact_chapters" \\
        --out-dir "D:/pact/gate_bench_runs/v4_phase12_046/draft_001" \\
        [--gemma-url http://127.0.0.1:8080/v1/chat/completions] \\
        [--gemma-model gemma-4-26B-A4B-it-UD-Q4_K_XL.gguf] \\
        [--qwen-url ...] [--qwen-model ...]

The default URLs/models point at the v3 production llama-server stack
(Gemma 4 26B on 127.0.0.1:8080). For the v3/v4 A/B gate, the Qwen
reviewer is intended to live on a second llama-server (or a second
context size on the same server) — pass ``--qwen-url`` to point the
Qwen evaluator at the right endpoint. If the Qwen endpoint is
unreachable, the driver still runs (the Qwen evaluator surfaces
failures as gate results, not as exceptions), but every chunk will
quarantine.

The driver does **not** run the v3 production pipeline, does **not**
touch any cache other than the in-process ``GenerationCache`` that the
library already owns, and writes its outputs only under ``--out-dir``.
Nothing else on disk is modified.
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path
from typing import List, Optional, Sequence

from pact_v4.pipeline.v4_phase12_draft_runner import (
    ChapterRunResult,
    PipelineConfig,
    run_chapter,
)
from pact_v4.runtime.api_client import ApiClient, ApiClientConfig
from pact_v4.runtime.gemma_selector import HttpGemmaSelector
from pact_v4.runtime.model_caller import HttpModelCaller, HttpModelCallerConfig
from pact_v4.runtime.qwen_evaluator import HttpQwenEvaluator, HttpQwenEvaluatorConfig

LOG = logging.getLogger("v4_phase12_draft_run")


def build_argparser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="pact_full_pipeline_runner_v1.v4_phase12_draft_run",
        description=(
            "Phase 1C → 2A → 2B → 2C end-to-end driver for one chapter. "
            "Real model calls go to llama-server over HTTP; no other "
            "side effects."
        ),
    )
    p.add_argument(
        "--chapter-html", type=Path, required=True,
        help="Path to the chapter EN HTML file (e.g. 0046_subordination-6-3.html).",
    )
    p.add_argument(
        "--memory-dir", type=Path, required=True,
        help=(
            "Directory holding glossary.json + book_memory.json for the "
            "chapter. Empty files are fine: a missing memory is treated as "
            "an empty one, not as an error."
        ),
    )
    p.add_argument(
        "--chapter-id", default=None,
        help=(
            "Stable chapter id used in artefact names and identities. "
            "Defaults to the chapter HTML file's stem (e.g. '0046_subordination-6-3')."
        ),
    )
    p.add_argument(
        "--out-dir", type=Path, required=True,
        help="Directory to write chunk_plan.json, translations.json, etc. into.",
    )
    # Model endpoints ----------------------------------------------------
    p.add_argument(
        "--gemma-url",
        default="http://127.0.0.1:8080/v1/chat/completions",
        help="Gemma 4 chat-completions URL (Phase 2B generator + Phase 2C selector).",
    )
    p.add_argument(
        "--gemma-model",
        default="gemma-4-26B-A4B-it-UD-Q4_K_XL.gguf",
        help="Gemma 4 model name as configured on llama-server.",
    )
    p.add_argument(
        "--qwen-url",
        default="http://127.0.0.1:8080/v1/chat/completions",
        help="Qwen chat-completions URL (Phase 2C fidelity evaluator).",
    )
    p.add_argument(
        "--qwen-model",
        default="gemma-4-26B-A4B-it-UD-Q4_K_XL.gguf",
        help=(
            "Qwen model name as configured on llama-server. Defaults to "
            "the Gemma model so the driver works against a single "
            "llama-server during early A/B gate work; point this at a "
            "Qwen build before recording the actual benchmark."
        ),
    )
    # Provisional parameters ---------------------------------------------
    p.add_argument(
        "--temperature", type=float, default=0.2,
        help="Provisional Phase 2B temperature (placeholder until gate).",
    )
    p.add_argument(
        "--seed", type=int, default=7,
        help="Provisional Phase 2B seed (placeholder until gate).",
    )
    p.add_argument(
        "--max-tokens", type=int, default=8192,
        help="Max tokens for one Phase 2B generation call.",
    )
    p.add_argument(
        "--right-context-pids", type=int, default=0,
        help="Number of next-chunk PIDs rendered into OWNED_SOURCE/right context.",
    )
    p.add_argument(
        "--run-label", default="v4-phase12-draft",
        help="Free-form label recorded in provenance.json.",
    )
    p.add_argument(
        "--log-level", default="INFO",
        choices=("DEBUG", "INFO", "WARNING", "ERROR"),
    )
    return p


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_argparser().parse_args(argv)
    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )

    chapter_id = args.chapter_id or args.chapter_html.stem
    cfg = PipelineConfig(
        chapter_id=chapter_id,
        chapter_html_path=args.chapter_html,
        memory_dir=args.memory_dir,
        out_dir=args.out_dir,
        temperature=args.temperature,
        seed=args.seed,
        max_tokens=args.max_tokens,
        right_context_pids=args.right_context_pids,
        run_label=args.run_label,
    )

    # Build HTTP clients --------------------------------------------------
    gemma_gen_api = ApiClient(
        ApiClientConfig(chat_url=args.gemma_url, model=args.gemma_model),
        name="gemma-phase2b",
    )
    qwen_api = ApiClient(
        ApiClientConfig(chat_url=args.qwen_url, model=args.qwen_model),
        name="qwen-phase2c",
    )
    gemma_sel_api = ApiClient(
        ApiClientConfig(chat_url=args.gemma_url, model=args.gemma_model),
        name="gemma-phase2c-selector",
    )

    model_caller = HttpModelCaller(api=gemma_gen_api, config=HttpModelCallerConfig(
        api=gemma_gen_api.config, label=gemma_gen_api.name, max_tokens=args.max_tokens,
    ))
    qwen_evaluator = HttpQwenEvaluator(api=qwen_api, config=HttpQwenEvaluatorConfig(
        api=qwen_api.config, label=qwen_api.name,
    ))
    gemma_selector = HttpGemmaSelector(api=gemma_sel_api, config=HttpGemmaSelectorConfig(
        api=gemma_sel_api.config, label=gemma_sel_api.name,
    ))

    LOG.info(
        "Starting v4 phase1-2C draft run: chapter=%s out=%s",
        chapter_id, args.out_dir,
    )
    result = run_chapter(
        cfg,
        model_caller=model_caller,
        qwen_evaluator=qwen_evaluator,
        gemma_selector=gemma_selector,
    )
    LOG.info(
        "Run finished: chunks=%d selected=%d quarantined=%d needs_synthesis=%d "
        "incomplete_generation=%d role_counts=%s",
        result.chunk_count, result.selected_count, result.quarantined_count,
        result.needs_synthesis_count, result.incomplete_generation_count,
        result.selected_role_counts,
    )
    print(json.dumps({
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


if __name__ == "__main__":
    raise SystemExit(main())
