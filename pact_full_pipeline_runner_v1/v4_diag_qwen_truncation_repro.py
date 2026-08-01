"""One-off diagnostic: capture the real Qwen fidelity verdict text for
chunks that quarantined in a strict-driver chapter trial.

pact_v4.phase2.cascade.select_candidate only keeps a candidate's
GateResult in its `traces` dict when qwen_result.passed is True; a failed
candidate's actual verdict/reasoning is discarded and the quarantine
reason is hard-coded to the literal string "Qwen fidelity fail". This
script re-queries Qwen (only Qwen -- no Gemma needed) against candidate
translations already on disk from a prior run's generation_outcomes.json,
to see the real reasoning without a full chapter regeneration.

Usage (defaults match the chapter_046 run that found the max_tokens
truncation bug fixed in pact_v4/runtime/qwen_evaluator.py)::

    python -m pact_full_pipeline_runner_v1.v4_diag_qwen_truncation_repro \\
        --run-dir "D:/pact/gate_bench_runs/v4_phase12_strict_046/run_001" \\
        --chunks chunk0009,chunk0010
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Optional, Sequence

from pact_v4.phase0b.source_html import load_source
from pact_v4.runtime.api_client import ApiClient, ApiClientConfig
from pact_v4.runtime.model_lifecycle import LifecycleAdapter
from pact_v4.runtime.qwen_evaluator import HttpQwenEvaluator, HttpQwenEvaluatorConfig

CONTEXT_SIZE = 32768
QWEN_ARGS = [
    "-fit", "on", "-fitt", "1280", "-b", "2048", "-ub", "512",
    "-ctk", "q8_0", "-ctv", "q8_0", "-t", "6", "-tb", "12",
    "--load-mode", "mmap", "--reasoning-budget", "0", "-np", "1",
    "-c", str(CONTEXT_SIZE), "-fa", "on", "--jinja",
    "--cache-ram", "0", "--ctx-checkpoints", "0",
]


def build_argparser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--run-dir", type=Path,
                    default=Path(r"D:\pact\gate_bench_runs\v4_phase12_strict_046\run_001"),
                    help="Directory containing generation_outcomes.json to re-check.")
    p.add_argument("--chapter-html", type=Path,
                    default=Path(r"D:\pact\pact_chapters\0046_subordination-6-3.html"))
    p.add_argument("--chunks", default="chunk0009,chunk0010",
                    help="Comma-separated chunk_ids from generation_outcomes.json to re-check.")
    p.add_argument("--llama-server-exe", type=Path, default=Path(r"C:\llama-sycl-new\llama-server.exe"))
    p.add_argument("--device", default="SYCL0")
    p.add_argument("--qwen-model", type=Path,
                    default=Path(r"C:\llama-cpp\models\Qwen3.6-35B-A3B\Qwen3.6-35B-A3B-UD-Q4_K_XL.gguf"))
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8095)
    return p


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_argparser().parse_args(argv)
    target_chunks = {c.strip() for c in args.chunks.split(",") if c.strip()}

    blocks, _ = load_source(args.chapter_html)
    source_map = {b.pid: b.text for b in blocks}

    outcomes = json.loads((args.run_dir / "generation_outcomes.json").read_text(encoding="utf-8"))
    to_check = []
    for o in outcomes["outcomes"]:
        if o["chunk_id"] in target_chunks:
            for role, cand in o["candidates"].items():
                to_check.append((o["chunk_id"], role, cand["translation"]))
    if not to_check:
        raise SystemExit(
            f"No candidates found for chunks {sorted(target_chunks)} in "
            f"{args.run_dir / 'generation_outcomes.json'} -- check --chunks / --run-dir."
        )

    adapter = LifecycleAdapter(
        args.llama_server_exe, args.device, args.host, args.port,
        args.run_dir / "diag_logs", {"qwen": args.qwen_model},
        startup_timeout=240.0, unload_timeout=30.0,
    )
    print("Starting Qwen...")
    cold, retries = adapter.start("qwen", "QwenDiag", QWEN_ARGS)
    print(f"Qwen ready in {cold:.1f}s (retries={retries})")

    results = []
    try:
        api = ApiClient(ApiClientConfig(
            chat_url=f"{adapter.base_url}/v1/chat/completions",
            model=args.qwen_model.name, context_size=CONTEXT_SIZE,
        ), name="qwen-diag")
        evaluator = HttpQwenEvaluator(api, config=HttpQwenEvaluatorConfig(api=api.config))

        for chunk_id, role, translation in to_check:
            pids = list(translation.keys())
            src = {pid: source_map[pid] for pid in pids if pid in source_map}
            print(f"Querying Qwen: {chunk_id} / {role} ({len(pids)} PIDs)...")
            verdict = evaluator(src, translation)
            print(f"  passed={verdict.passed} detail={verdict.detail}")
            results.append({
                "chunk_id": chunk_id, "role": role,
                "passed": verdict.passed, "detail": verdict.detail,
            })
    finally:
        adapter.stop()

    out_path = args.run_dir / "diag_qwen_reasons.json"
    out_path.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Written: {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
