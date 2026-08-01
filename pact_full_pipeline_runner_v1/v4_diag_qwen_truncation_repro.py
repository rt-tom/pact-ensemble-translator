"""One-off diagnostic: capture the real Qwen fidelity verdict text for
chunks that quarantined in the chapter_046 strict trial.

pact_v4.phase2.cascade.select_candidate only keeps a candidate's
GateResult in its `traces` dict when qwen_result.passed is True; a failed
candidate's actual verdict/reasoning is discarded and the quarantine
reason is hard-coded to the literal string "Qwen fidelity fail". This
script re-queries Qwen (only Qwen -- no Gemma needed) against the exact
candidate translations already on disk from run_001, to see the real
reasoning without a full chapter regeneration.
"""
from __future__ import annotations

import json
from pathlib import Path

from pact_v4.phase0b.source_html import load_source
from pact_v4.runtime.api_client import ApiClient, ApiClientConfig
from pact_v4.runtime.model_lifecycle import LifecycleAdapter
from pact_v4.runtime.qwen_evaluator import HttpQwenEvaluator, HttpQwenEvaluatorConfig

RUN_DIR = Path(r"D:\pact\gate_bench_runs\v4_phase12_strict_046\run_001")
CHAPTER_HTML = Path(r"D:\pact\pact_chapters\0046_subordination-6-3.html")

CONTEXT_SIZE = 32768
QWEN_ARGS = [
    "-fit", "on", "-fitt", "1280", "-b", "2048", "-ub", "512",
    "-ctk", "q8_0", "-ctv", "q8_0", "-t", "6", "-tb", "12",
    "--load-mode", "mmap", "--reasoning-budget", "0", "-np", "1",
    "-c", str(CONTEXT_SIZE), "-fa", "on", "--jinja",
    "--cache-ram", "0", "--ctx-checkpoints", "0",
]
QWEN_PATH = Path(r"C:\llama-cpp\models\Qwen3.6-35B-A3B\Qwen3.6-35B-A3B-UD-Q4_K_XL.gguf")


def main() -> None:
    blocks, _ = load_source(CHAPTER_HTML)
    source_map = {b.pid: b.text for b in blocks}

    outcomes = json.loads((RUN_DIR / "generation_outcomes.json").read_text(encoding="utf-8"))
    target_chunks = {"chunk0009", "chunk0010"}
    to_check = []
    for o in outcomes["outcomes"]:
        if o["chunk_id"] in target_chunks:
            for role, cand in o["candidates"].items():
                to_check.append((o["chunk_id"], role, cand["translation"]))

    adapter = LifecycleAdapter(
        Path(r"C:\llama-sycl-new\llama-server.exe"), "SYCL0", "127.0.0.1", 8095,
        RUN_DIR / "diag_logs", {"qwen": QWEN_PATH},
        startup_timeout=240.0, unload_timeout=30.0,
    )
    print("Starting Qwen...")
    cold, retries = adapter.start("qwen", "QwenDiag", QWEN_ARGS)
    print(f"Qwen ready in {cold:.1f}s (retries={retries})")

    api = ApiClient(ApiClientConfig(
        chat_url=f"{adapter.base_url}/v1/chat/completions",
        model=QWEN_PATH.name, context_size=CONTEXT_SIZE,
    ), name="qwen-diag")
    evaluator = HttpQwenEvaluator(api, config=HttpQwenEvaluatorConfig(api=api.config))

    results = []
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

    adapter.stop()
    out_path = RUN_DIR / "diag_qwen_reasons.json"
    out_path.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Written: {out_path}")


if __name__ == "__main__":
    main()
