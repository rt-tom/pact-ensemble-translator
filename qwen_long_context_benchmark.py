#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import time
from pathlib import Path
from typing import Any

import requests


def load_json(path: Path) -> Any:
    with path.open("r", encoding="utf-8-sig") as f:
        return json.load(f)


def extract_blocks(manifest: Any) -> list[dict[str, str]]:
    raw_blocks = manifest.get("blocks", []) if isinstance(manifest, dict) else manifest
    if not isinstance(raw_blocks, list):
        raise ValueError("Unsupported manifest structure.")

    blocks: list[dict[str, str]] = []
    for item in raw_blocks:
        if not isinstance(item, dict):
            continue
        pid = str(item.get("pid", "")).strip()
        source = item.get("source_text") or item.get("source") or item.get("text") or ""
        source = str(source).strip()
        if pid and source:
            blocks.append({"pid": pid, "source": source})

    if not blocks:
        raise ValueError("No source blocks found in manifest.")
    return blocks


def extract_translations(data: Any) -> dict[str, str]:
    if isinstance(data, dict):
        direct = {
            str(k): str(v)
            for k, v in data.items()
            if isinstance(k, str)
            and k.startswith("p")
            and isinstance(v, (str, int, float))
        }
        if direct:
            return direct

        for key in ("translations", "draft_translations", "blocks", "items"):
            if key in data:
                nested = extract_translations(data[key])
                if nested:
                    return nested

    if isinstance(data, list):
        result: dict[str, str] = {}
        for item in data:
            if not isinstance(item, dict):
                continue
            pid = str(item.get("pid", "")).strip()
            text = item.get("text") or item.get("translation") or item.get("target_text") or ""
            if pid and str(text).strip():
                result[pid] = str(text).strip()
        return result

    return {}


def build_prompt(
    blocks: list[dict[str, str]],
    translations: dict[str, str],
) -> tuple[str, int]:
    pairs: list[str] = []
    missing = 0

    for block in blocks:
        pid = block["pid"]
        target = translations.get(pid, "").strip()
        if not target:
            missing += 1
            target = "[MISSING TRANSLATION]"
        pairs.append(f"[{pid}]\nEN: {block['source']}\nRU: {target}")

    prompt = """Audit this English-to-Russian literary translation.

Return ONLY one compact JSON object:
{
  "chapter_complete": true,
  "issues": [
    {
      "pid": "p00001",
      "severity": "critical|major",
      "category": "meaning|subject|gender|name|register|grammar|omission|addition",
      "problem": "One concise sentence in English"
    }
  ]
}

Rules:
- Report only clear, consequential errors.
- Compare every Russian block with its English original.
- Do not rewrite the chapter.
- Maximum 10 issues.
- Keep each problem under 25 words.
- If no clear issue exists, return an empty issues array.

BILINGUAL CHAPTER:
""" + "\n\n".join(pairs)

    return prompt, missing


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--draft", required=True, type=Path)
    parser.add_argument("--url", default="http://127.0.0.1:8080/v1/chat/completions")
    parser.add_argument("--model", default="Qwen3.6-35B-A3B-UD-Q4_K_XL.gguf")
    parser.add_argument("--rounds", type=int, default=2)
    parser.add_argument("--max-output-tokens", type=int, default=768)
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("benchmark_results/qwen36_long_context_32k.json"),
    )
    args = parser.parse_args()

    blocks = extract_blocks(load_json(args.manifest))
    translations = extract_translations(load_json(args.draft))
    prompt, missing = build_prompt(blocks, translations)

    payload = {
        "model": args.model,
        "messages": [
            {
                "role": "system",
                "content": (
                    "You are a strict bilingual literary-translation auditor. "
                    "Return only valid JSON and no reasoning."
                ),
            },
            {"role": "user", "content": prompt},
        ],
        "temperature": 0,
        "max_tokens": args.max_output_tokens,
        "stream": False,
        "response_format": {"type": "json_object"},
        "chat_template_kwargs": {"enable_thinking": False},
    }

    results: list[dict[str, Any]] = []

    for round_number in range(1, args.rounds + 1):
        started = time.perf_counter()
        response = requests.post(args.url, json=payload, timeout=1800)
        wall_seconds = time.perf_counter() - started
        response.raise_for_status()
        data = response.json()

        choice = data["choices"][0]
        message = choice.get("message", {})
        usage = data.get("usage", {})

        result = {
            "round": round_number,
            "wall_seconds": round(wall_seconds, 3),
            "prompt_tokens": usage.get("prompt_tokens"),
            "completion_tokens": usage.get("completion_tokens"),
            "total_tokens": usage.get("total_tokens"),
            "finish_reason": choice.get("finish_reason"),
            "reasoning_chars": len(message.get("reasoning_content") or ""),
            "content": message.get("content", ""),
        }
        results.append(result)
        print(json.dumps(result, ensure_ascii=False, indent=2))

    output = {
        "model": args.model,
        "manifest": str(args.manifest),
        "draft": str(args.draft),
        "blocks": len(blocks),
        "translations": len(translations),
        "missing_translations": missing,
        "rounds": results,
    }

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(output, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(f"\nSaved: {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
