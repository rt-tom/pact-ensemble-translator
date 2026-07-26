#!/usr/bin/env python3
from __future__ import annotations
import argparse
import json
import statistics
import time
from pathlib import Path
import requests

PASSAGE = """The storm arrived without ceremony. Mara closed the workshop,
checked the old lock twice, and listened to rain hammer the metal roof.
Her brother had promised to return before eleven-fifty, but the motorcycle
was still gone. “Jesus fuck,” she whispered. The cat raised his head from
the crate, then settled again. Nothing in the room moved, yet the reflection
in the dark window smiled first."""

def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default="http://127.0.0.1:8080/v1/chat/completions")
    parser.add_argument("--model", default="gemma-4-26B-A4B-it-UD-Q4_K_XL.gguf")
    parser.add_argument("--rounds", type=int, default=3)
    parser.add_argument("--label", required=True)
    parser.add_argument("--output-dir", default="./benchmark_results")
    args = parser.parse_args()

    session = requests.Session()
    samples = []
    for index in range(args.rounds):
        body = {
            "model": args.model,
            "messages": [
                {"role": "system", "content": (
                    'Translate into literary Russian. Preserve exact time, '
                    'motorcycle, male cat and profanity. Return JSON '
                    '{"translation":"..."}. No reasoning.'
                )},
                {"role": "user", "content": PASSAGE},
            ],
            "max_tokens": 900,
            "temperature": 0.2,
            "top_p": 0.95,
            "top_k": 64,
            "stream": False,
            "response_format": {"type": "json_object"},
            "chat_template_kwargs": {"enable_thinking": False},
        }
        started = time.perf_counter()
        response = session.post(args.url, json=body, timeout=1800)
        response.raise_for_status()
        data = response.json()
        wall = time.perf_counter() - started
        choice = data["choices"][0]
        usage = data.get("usage") or {}
        samples.append({
            "round": index + 1,
            "wall_seconds": round(wall, 3),
            "prompt_tokens": usage.get("prompt_tokens"),
            "completion_tokens": usage.get("completion_tokens"),
            "finish_reason": choice.get("finish_reason"),
            "reasoning_chars": len(
                choice["message"].get("reasoning_content") or ""
            ),
            "content": choice["message"].get("content") or "",
        })

    result = {
        "label": args.label,
        "model": args.model,
        "median_wall_seconds": statistics.median(
            sample["wall_seconds"] for sample in samples
        ),
        "rounds": samples,
    }
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / f"{args.label}.json"
    path.write_text(
        json.dumps(result, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    print(path)

if __name__ == "__main__":
    main()
