#!/usr/bin/env python3
"""Benchmark a model as a verifier of proposed translation-review claims."""

from __future__ import annotations

import argparse
import json
import os
import re
import tempfile
import time
from pathlib import Path
from typing import Any

import requests

VERSION = "1.0.0"

LOCKED_GLOSSARY = """Locked project glossary:
Practitioner = практик
Other = Другой
Implement = инструмент
Familiar = фамильяр
Demesne = домен
Diagram = диаграмма
Binding = связывание / привязка
Awakening = Пробуждение
Karma = карма
Forsworn = Отречённый
Bogeymen = пугала
Enchantress = чародейка
Augur = прорицатель
Duchamp = Дюшам
Blake = Блэйк
Paige = Пэйдж
Callan = Калан
Rose = Роуз
Conquest = Конквест
Jacob's Bell = Якобс-Белл
Hillsglade House = Дом-на-Холме
"""


def load_json(path: Path) -> Any:
    with path.open("r", encoding="utf-8-sig") as handle:
        return json.load(handle)


def atomic_write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(
        prefix=path.name + ".",
        suffix=".tmp",
        dir=str(path.parent),
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle, ensure_ascii=False, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, path)
    finally:
        if os.path.exists(temp_name):
            os.unlink(temp_name)


def extract_json_object(text: str) -> dict[str, Any]:
    text = text.strip()
    if not text:
        raise ValueError("Empty response")

    try:
        parsed = json.loads(text)
        if isinstance(parsed, dict):
            return parsed
    except json.JSONDecodeError:
        pass

    start = text.find("{")
    if start < 0:
        raise ValueError("No JSON object found")

    depth = 0
    in_string = False
    escaped = False
    for index in range(start, len(text)):
        char = text[index]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue

        if char == '"':
            in_string = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                parsed = json.loads(text[start:index + 1])
                if not isinstance(parsed, dict):
                    raise ValueError("Parsed value is not an object")
                return parsed

    raise ValueError("Unterminated JSON object")


def render_context(items: list[dict[str, str]]) -> str:
    if not items:
        return "(none)"
    return "\n\n".join(
        f"[{item['pid']}]\nEN: {item['source']}\nRU: {item['translation']}"
        for item in items
    )


def build_messages(case: dict[str, Any]) -> list[dict[str, str]]:
    system = (
        "You verify one proposed critique of an English-to-Russian literary translation. "
        "Do not audit for other errors. Judge only whether the proposed critique itself is accurate. "
        "Return one JSON object and no reasoning outside JSON."
    )

    user = f"""{LOCKED_GLOSSARY}

CONTEXT BEFORE — context only, not the claim target:
{render_context(case.get('context_before', []))}

TARGET:
[{case['pid']}]
EN: {case['source']}
RU: {case['translation']}

PROPOSED CRITIQUE:
Category: {case['candidate_category']}
Claim: {case['candidate_problem']}

CONTEXT AFTER — context only, not the claim target:
{render_context(case.get('context_after', []))}

Return exactly:
{{
  "verdict": "confirm" | "reject" | "uncertain",
  "confidence": "high" | "medium" | "low",
  "reason": "One concise sentence in English"
}}

Rules:
- CONFIRM only if this specific claim identifies a real, material error in the TARGET.
- REJECT if the target is acceptable, the claim contradicts the locked glossary, the claim misreads context, or it points to a different error.
- Do not confirm merely because another wording could be better.
- Do not replace the proposed claim with a different critique.
- Use UNCERTAIN only when the source genuinely does not allow a reliable decision.
"""
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]


def normalize_verdict(value: Any) -> str:
    verdict = str(value or "").strip().casefold()
    aliases = {
        "confirmed": "confirm",
        "true": "confirm",
        "yes": "confirm",
        "rejected": "reject",
        "false": "reject",
        "no": "reject",
        "unknown": "uncertain",
        "unsure": "uncertain",
    }
    verdict = aliases.get(verdict, verdict)
    if verdict not in {"confirm", "reject", "uncertain"}:
        raise ValueError(f"Invalid verdict: {value!r}")
    return verdict


def score(results: list[dict[str, Any]]) -> dict[str, Any]:
    successful = [r for r in results if r.get("status") == "success"]
    expected_confirm = sum(
        r["expected_verdict"] == "confirm" for r in successful
    )
    expected_reject = sum(
        r["expected_verdict"] == "reject" for r in successful
    )

    tp = sum(
        r["expected_verdict"] == "confirm"
        and r["verdict"] == "confirm"
        for r in successful
    )
    fn = sum(
        r["expected_verdict"] == "confirm"
        and r["verdict"] != "confirm"
        for r in successful
    )
    fp = sum(
        r["expected_verdict"] == "reject"
        and r["verdict"] == "confirm"
        for r in successful
    )
    tn = sum(
        r["expected_verdict"] == "reject"
        and r["verdict"] == "reject"
        for r in successful
    )
    uncertain = sum(r["verdict"] == "uncertain" for r in successful)
    correct = sum(
        r["verdict"] == r["expected_verdict"]
        for r in successful
    )

    def ratio(numerator: int, denominator: int) -> float | None:
        return round(numerator / denominator, 4) if denominator else None

    return {
        "successful_cases": len(successful),
        "failed_cases": len(results) - len(successful),
        "expected_confirm": expected_confirm,
        "expected_reject": expected_reject,
        "true_positive": tp,
        "false_negative_or_abstain": fn,
        "false_positive": fp,
        "true_negative": tn,
        "uncertain": uncertain,
        "accuracy": ratio(correct, len(successful)),
        "confirmation_recall": ratio(tp, expected_confirm),
        "confirmation_precision": ratio(tp, tp + fp),
        "false_positive_rate": ratio(fp, expected_reject),
        "specificity": ratio(tn, expected_reject),
        "abstention_rate": ratio(uncertain, len(successful)),
    }


def run_case(
    session: requests.Session,
    case: dict[str, Any],
    args: argparse.Namespace,
) -> dict[str, Any]:
    attempts: list[dict[str, Any]] = []

    for attempt_number in range(1, args.attempts + 1):
        body = {
            "model": args.model,
            "messages": build_messages(case),
            "max_tokens": args.max_tokens,
            "temperature": args.temperature,
            "top_p": 1.0,
            "stream": False,
            "response_format": {"type": "json_object"},
            "chat_template_kwargs": {
                "enable_thinking": args.enable_thinking,
            },
        }

        started = time.perf_counter()
        attempt: dict[str, Any] = {"attempt": attempt_number}

        try:
            response = session.post(
                args.url,
                json=body,
                timeout=args.timeout,
            )
            attempt["http_status"] = response.status_code
            response.raise_for_status()
            payload = response.json()

            choice = payload["choices"][0]
            message = choice.get("message") or {}
            content = message.get("content") or ""
            attempt.update({
                "wall_seconds": round(time.perf_counter() - started, 3),
                "finish_reason": choice.get("finish_reason"),
                "usage": payload.get("usage") or {},
                "reasoning_chars": len(message.get("reasoning_content") or ""),
                "content": content,
            })

            parsed = extract_json_object(content)
            verdict = normalize_verdict(parsed.get("verdict"))
            confidence = str(parsed.get("confidence") or "").strip().casefold()
            if confidence not in {"high", "medium", "low"}:
                confidence = "unspecified"
            reason = re.sub(r"\s+", " ", str(parsed.get("reason") or "")).strip()

            if choice.get("finish_reason") != "stop":
                raise ValueError(
                    f"finish_reason={choice.get('finish_reason')!r}"
                )

            attempt["parsed"] = {
                "verdict": verdict,
                "confidence": confidence,
                "reason": reason,
            }
            attempts.append(attempt)

            return {
                "case_id": case["case_id"],
                "pid": case["pid"],
                "expected_verdict": case["expected_verdict"],
                "candidate_category": case["candidate_category"],
                "candidate_problem": case["candidate_problem"],
                "status": "success",
                "verdict": verdict,
                "confidence": confidence,
                "reason": reason,
                "attempts": attempts,
            }

        except Exception as exc:
            attempt.update({
                "wall_seconds": round(time.perf_counter() - started, 3),
                "error": f"{type(exc).__name__}: {exc}",
            })
            attempts.append(attempt)

    return {
        "case_id": case["case_id"],
        "pid": case["pid"],
        "expected_verdict": case["expected_verdict"],
        "candidate_category": case["candidate_category"],
        "candidate_problem": case["candidate_problem"],
        "status": "failed",
        "attempts": attempts,
    }


def self_test() -> int:
    assert normalize_verdict("confirmed") == "confirm"
    assert normalize_verdict("NO") == "reject"
    assert extract_json_object('prefix {"verdict":"confirm"} suffix')["verdict"] == "confirm"

    sample = [
        {"status": "success", "expected_verdict": "confirm", "verdict": "confirm"},
        {"status": "success", "expected_verdict": "reject", "verdict": "reject"},
        {"status": "success", "expected_verdict": "reject", "verdict": "confirm"},
    ]
    metrics = score(sample)
    assert metrics["true_positive"] == 1
    assert metrics["false_positive"] == 1
    assert metrics["true_negative"] == 1
    print(f"Self-test passed (candidate_verifier_benchmark.py {VERSION})")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--benchmark", type=Path)
    parser.add_argument("--model")
    parser.add_argument(
        "--url",
        default="http://127.0.0.1:8080/v1/chat/completions",
    )
    parser.add_argument("--attempts", type=int, default=3)
    parser.add_argument("--max-tokens", type=int, default=256)
    parser.add_argument("--temperature", type=float, default=0.0)
    parser.add_argument("--timeout", type=int, default=300)
    parser.add_argument("--enable-thinking", action="store_true")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--version", action="store_true")
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args()

    if args.version:
        print(f"candidate_verifier_benchmark.py {VERSION}")
        return 0
    if args.self_test:
        return self_test()

    if not args.benchmark or not args.model or not args.output:
        parser.error("--benchmark, --model, and --output are required")

    data = load_json(args.benchmark)
    cases = data.get("cases") if isinstance(data, dict) else data
    if not isinstance(cases, list) or not cases:
        raise ValueError("Benchmark contains no cases")

    session = requests.Session()
    results: list[dict[str, Any]] = []
    started = time.perf_counter()

    for index, case in enumerate(cases, start=1):
        print(
            f"[{index}/{len(cases)}] {case['case_id']} "
            f"expected={case['expected_verdict']}",
            flush=True,
        )
        result = run_case(session, case, args)
        results.append(result)
        if result["status"] == "success":
            marker = "OK" if result["verdict"] == result["expected_verdict"] else "MISS"
            print(
                f"  {marker}: verdict={result['verdict']} "
                f"confidence={result['confidence']}",
                flush=True,
            )
        else:
            print("  FAILED", flush=True)

        partial = {
            "version": VERSION,
            "benchmark": str(args.benchmark),
            "model": args.model,
            "results": results,
            "metrics": score(results),
            "run_wall_seconds": round(time.perf_counter() - started, 3),
        }
        atomic_write_json(args.output, partial)

    final = {
        "version": VERSION,
        "benchmark": str(args.benchmark),
        "model": args.model,
        "url": args.url,
        "enable_thinking": args.enable_thinking,
        "case_count": len(cases),
        "results": results,
        "metrics": score(results),
        "run_wall_seconds": round(time.perf_counter() - started, 3),
    }
    atomic_write_json(args.output, final)
    print(json.dumps(final["metrics"], ensure_ascii=False, indent=2))
    print(f"Saved: {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
