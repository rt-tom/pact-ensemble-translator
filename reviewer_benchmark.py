#!/usr/bin/env python3
"""
Robust reviewer benchmark for OpenAI-compatible llama.cpp servers.

Features:
- batch, scene-window, and whole-corpus modes;
- raw response and usage metadata saved for every attempt;
- retries on HTTP errors, invalid/truncated JSON, or finish_reason != stop;
- continues after a failed batch;
- atomic checkpointing and optional resume;
- PID recall and control false-positive rate;
- optional strict issue recall when the benchmark contains semantic gold data.

Backward compatible with:
{
  "items": [
    {
      "pid": "p00001",
      "source": "...",
      "translation": "...",
      "expected_issue": true
    }
  ]
}

For strict issue scoring, enrich an item with one of:
  "expected_issue": {
    "category": "meaning",
    "problem": "The translation reverses inhale and exhale.",
    "keywords": ["inhale", "exhale"]
  }

or:
  "expected_issues": [
    {
      "issue_id": "gold_001",
      "category": "meaning",
      "keywords": ["inhale", "exhale"]
    }
  ]
"""
from __future__ import annotations

import argparse
import copy
import json
import os
import re
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Optional

import requests

VERSION = "2.2.0"

CATEGORY_ALIASES = {
    "accuracy": "meaning",
    "mistranslation": "meaning",
    "semantic": "meaning",
    "semantic error": "meaning",
    "translation error": "meaning",
    "pronoun": "subject",
    "reference": "subject",
    "pronoun/reference": "subject",
    "gender error": "gender",
    "grammar/logic error": "grammar",
    "logic": "meaning",
    "tone": "register",
    "style": "register",
    "terminology": "name",
    "proper name": "name",
}

WORD_RE = re.compile(r"[A-Za-zА-Яа-яЁё0-9]+", re.UNICODE)
PID_RE = re.compile(r"^p\d+$", re.IGNORECASE)


class BenchmarkError(RuntimeError):
    pass


@dataclass(frozen=True)
class Job:
    job_id: str
    target_items: list[dict[str, Any]]
    context_before: list[dict[str, Any]]
    context_after: list[dict[str, Any]]

    @property
    def target_pids(self) -> list[str]:
        return [str(item["pid"]) for item in self.target_items]


def atomic_write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8", newline="\n") as handle:
        json.dump(data, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, path)


def load_json(path: Path) -> Any:
    with path.open("r", encoding="utf-8-sig") as handle:
        return json.load(handle)


def normalize_space(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def normalize_category(value: Any) -> str:
    category = normalize_space(value).casefold()
    return CATEGORY_ALIASES.get(category, category)


def normalize_pid(value: Any) -> str:
    pid = normalize_space(value)
    return pid.lower()


def tokenize(value: Any) -> set[str]:
    stop = {
        "the", "a", "an", "is", "are", "was", "were", "to", "of", "in",
        "on", "for", "and", "or", "but", "with", "as", "it", "this", "that",
        "translation", "translated", "russian", "english", "text", "source",
        "target", "word", "phrase", "incorrectly", "wrong", "error",
    }
    return {
        token.casefold()
        for token in WORD_RE.findall(str(value or ""))
        if len(token) >= 3 and token.casefold() not in stop
    }


def validate_items(raw: Any) -> list[dict[str, Any]]:
    if not isinstance(raw, dict) or not isinstance(raw.get("items"), list):
        raise BenchmarkError("Benchmark must be a JSON object with an 'items' list.")

    items: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, item in enumerate(raw["items"], 1):
        if not isinstance(item, dict):
            raise BenchmarkError(f"Item {index} is not an object.")

        pid = normalize_pid(item.get("pid"))
        source = normalize_space(item.get("source"))
        translation = normalize_space(item.get("translation"))

        if not pid or not PID_RE.fullmatch(pid):
            raise BenchmarkError(f"Item {index} has invalid pid={pid!r}.")
        if pid in seen:
            raise BenchmarkError(f"Duplicate PID in benchmark: {pid}")
        if not source or not translation:
            raise BenchmarkError(f"{pid}: source and translation are required.")

        clean = copy.deepcopy(item)
        clean["pid"] = pid
        clean["source"] = source
        clean["translation"] = translation
        items.append(clean)
        seen.add(pid)

    if not items:
        raise BenchmarkError("Benchmark contains no items.")
    return items


def chunked(items: list[dict[str, Any]], size: int) -> Iterable[tuple[int, list[dict[str, Any]]]]:
    for start in range(0, len(items), size):
        yield start, items[start:start + size]


def build_jobs(
    items: list[dict[str, Any]],
    mode: str,
    batch_size: int,
    scene_size: int,
    context_before: int,
    context_after: int,
) -> list[Job]:
    if mode == "whole":
        return [Job("whole", items, [], [])]

    size = batch_size if mode == "batch" else scene_size
    jobs: list[Job] = []
    for start, target in chunked(items, size):
        end = start + len(target)
        before = items[max(0, start - context_before):start]
        after = items[end:min(len(items), end + context_after)]
        jobs.append(
            Job(
                job_id=f"{mode}_{start // size + 1:04d}",
                target_items=target,
                context_before=before,
                context_after=after,
            )
        )
    return jobs


def pair_xml(item: dict[str, Any], role: str) -> str:
    # JSON escaping inside simple tags keeps source text unambiguous enough
    # without adding an XML dependency.
    source = json.dumps(item["source"], ensure_ascii=False)
    translation = json.dumps(item["translation"], ensure_ascii=False)
    return (
        f'<PAIR pid="{item["pid"]}" role="{role}">\n'
        f"<EN>{source}</EN>\n"
        f"<RU>{translation}</RU>\n"
        "</PAIR>"
    )


def build_messages(job: Job, max_issues: int) -> list[dict[str, str]]:
    system = f"""You are a strict bilingual literary-translation auditor.

Find only concrete, consequential English-to-Russian translation errors.
Do not flag acceptable stylistic alternatives.
Do not discuss uncertain possibilities.
Report issues only for PAIR elements whose role is TARGET.
CONTEXT pairs are reference only and must never be reported.

Return one compact JSON object and nothing else:
{{"issues":[{{"pid":"p00001","category":"meaning|subject|gender|name|register|grammar|omission|addition","problem":"Concise English description, at most 20 words"}}]}}

Rules:
- Maximum {max_issues} issues.
- At most one issue per PID.
- Keep every problem under 20 words.
- If no clear error exists, return {{"issues":[]}}.
- Do not explain your process.
"""
    parts: list[str] = []
    parts.extend(pair_xml(item, "CONTEXT") for item in job.context_before)
    parts.extend(pair_xml(item, "TARGET") for item in job.target_items)
    parts.extend(pair_xml(item, "CONTEXT") for item in job.context_after)
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": "\n".join(parts)},
    ]


def extract_json_object(content: str) -> dict[str, Any]:
    text = content.strip()
    if not text:
        raise ValueError("Empty model content.")

    # Remove one outer Markdown fence.
    text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.IGNORECASE)
    text = re.sub(r"\s*```$", "", text)

    try:
        data = json.loads(text)
        if isinstance(data, dict):
            return data
        raise ValueError("Top-level JSON is not an object.")
    except json.JSONDecodeError:
        pass

    decoder = json.JSONDecoder()
    for match in re.finditer(r"\{", text):
        try:
            data, _ = decoder.raw_decode(text[match.start():])
            if isinstance(data, dict):
                return data
        except json.JSONDecodeError:
            continue
    raise ValueError("No complete JSON object found in model content.")


def parse_issues(
    content: str,
    allowed_pids: set[str],
) -> tuple[list[dict[str, str]], list[dict[str, str]]]:
    data = extract_json_object(content)
    raw_issues = data.get("issues", [])
    if raw_issues is None:
        raw_issues = []
    if not isinstance(raw_issues, list):
        raise ValueError("'issues' must be a list.")

    accepted: list[dict[str, str]] = []
    rejected: list[dict[str, str]] = []
    seen: set[str] = set()

    for raw in raw_issues:
        if not isinstance(raw, dict):
            rejected.append({"reason": "not_an_object", "raw": repr(raw)})
            continue

        pid = normalize_pid(raw.get("pid"))
        category = normalize_category(raw.get("category"))
        problem = normalize_space(raw.get("problem"))

        if pid not in allowed_pids:
            rejected.append({
                "reason": "pid_not_in_target",
                "pid": pid,
                "category": category,
                "problem": problem,
            })
            continue
        if pid in seen:
            rejected.append({
                "reason": "duplicate_pid",
                "pid": pid,
                "category": category,
                "problem": problem,
            })
            continue
        if not category or not problem:
            rejected.append({
                "reason": "missing_category_or_problem",
                "pid": pid,
                "category": category,
                "problem": problem,
            })
            continue

        accepted.append({
            "pid": pid,
            "category": category,
            "problem": problem,
        })
        seen.add(pid)

    return accepted, rejected


def issue_records_from_item(item: dict[str, Any]) -> list[dict[str, Any]]:
    pid = item["pid"]

    if "expected_issues" in item:
        raw = item["expected_issues"]
    elif item.get("gold"):
        raw = item["gold"]
    else:
        raw = item.get("expected_issue")

    if not raw:
        return []
    if raw is True:
        # PID-level gold only; there is not enough semantic data for strict scoring.
        return [{"pid": pid, "strict_available": False}]
    if isinstance(raw, (str, dict)):
        raw = [raw]
    if not isinstance(raw, list):
        return [{"pid": pid, "strict_available": False}]

    records: list[dict[str, Any]] = []
    for index, entry in enumerate(raw, 1):
        if entry is True:
            records.append({"pid": pid, "strict_available": False})
            continue
        if isinstance(entry, str):
            record = {
                "pid": pid,
                "issue_id": f"{pid}_{index}",
                "problem": normalize_space(entry),
                "strict_available": bool(normalize_space(entry)),
            }
        elif isinstance(entry, dict):
            category = entry.get("category") or item.get("expected_category")
            problem = (
                entry.get("problem")
                or entry.get("description")
                or item.get("expected_problem")
            )
            keywords = (
                entry.get("keywords")
                or entry.get("concepts")
                or item.get("expected_keywords")
                or []
            )
            if isinstance(keywords, str):
                keywords = [keywords]
            match_groups = entry.get("match_groups") or []
            clean_groups = []
            if isinstance(match_groups, list):
                for group in match_groups:
                    if isinstance(group, str):
                        group = [group]
                    if isinstance(group, list):
                        clean = [
                            normalize_space(value).casefold()
                            for value in group
                            if normalize_space(value)
                        ]
                        if clean:
                            clean_groups.append(clean)
            record = {
                "pid": pid,
                "issue_id": normalize_space(
                    entry.get("issue_id") or f"{pid}_{index}"
                ),
                "category": normalize_category(
                    entry.get("category_norm") or category
                ),
                "problem": normalize_space(problem),
                "keywords": [
                    normalize_space(keyword).casefold()
                    for keyword in keywords
                    if normalize_space(keyword)
                ],
                "match_groups": clean_groups,
                "category_optional": bool(entry.get("category_optional", True)),
            }
            record["strict_available"] = bool(
                record["category"] or record["problem"] or
                record["keywords"] or record["match_groups"]
            )
        else:
            record = {"pid": pid, "strict_available": False}
        records.append(record)

    return records


def strict_match(gold: dict[str, Any], detected: dict[str, str]) -> bool:
    if gold["pid"] != detected["pid"] or not gold.get("strict_available"):
        return False

    gold_category = normalize_category(gold.get("category"))
    detected_category = normalize_category(detected.get("category"))
    if (
        gold_category
        and not gold.get("category_optional", True)
        and detected_category != gold_category
    ):
        return False

    combined = (
        normalize_space(detected.get("category")) + " " +
        normalize_space(detected.get("problem"))
    ).casefold()

    match_groups = gold.get("match_groups") or []
    if match_groups:
        return all(
            any(term in combined for term in group)
            for group in match_groups
        )

    keywords = [str(value).casefold() for value in gold.get("keywords", [])]
    if keywords and not all(keyword in combined for keyword in keywords):
        return False

    gold_problem = normalize_space(gold.get("problem"))
    if gold_problem and not keywords:
        gold_tokens = tokenize(gold_problem)
        detected_tokens = tokenize(combined)
        if gold_tokens:
            overlap = len(gold_tokens & detected_tokens) / len(gold_tokens)
            if overlap < 0.25:
                return False

    return True


def score(
    items: list[dict[str, Any]],
    detected_issues: list[dict[str, str]],
) -> dict[str, Any]:
    gold_pids = {
        item["pid"]
        for item in items
        if bool(item.get("expected_issue") or item.get("expected_issues"))
    }
    has_evaluation_roles = any(
        normalize_space(item.get("evaluation_role"))
        for item in items
    )
    if has_evaluation_roles:
        control_pids = {
            item["pid"]
            for item in items
            if normalize_space(item.get("evaluation_role")).casefold()
            == "control"
        }
        unlabeled_pids = {
            item["pid"]
            for item in items
            if normalize_space(item.get("evaluation_role")).casefold()
            == "unlabeled"
        }
    else:
        control_pids = {
            item["pid"]
            for item in items
            if not bool(
                item.get("expected_issue") or item.get("expected_issues")
            )
        }
        unlabeled_pids = set()

    detected_pids = {issue["pid"] for issue in detected_issues}

    true_positive = detected_pids & gold_pids
    false_negative = gold_pids - detected_pids
    false_positive = detected_pids & control_pids
    unverified_candidates = detected_pids & unlabeled_pids

    gold_records = [
        record
        for item in items
        for record in issue_records_from_item(item)
    ]
    strict_gold = [record for record in gold_records if record.get("strict_available")]

    matched_gold_ids: list[str] = []
    strict_unmatched: list[str] = []
    if strict_gold:
        for index, gold in enumerate(strict_gold, 1):
            gold_id = normalize_space(
                gold.get("issue_id") or f'{gold["pid"]}_{index}'
            )
            if any(strict_match(gold, issue) for issue in detected_issues):
                matched_gold_ids.append(gold_id)
            else:
                strict_unmatched.append(gold_id)

    return {
        "gold_pid_count": len(gold_pids),
        "control_count": len(control_pids),
        "detected_pid_count": len(detected_pids),
        "pid_recall": (
            round(len(true_positive) / len(gold_pids), 4)
            if gold_pids else None
        ),
        "false_positive_rate": (
            round(len(false_positive) / len(control_pids), 4)
            if control_pids else None
        ),
        "true_positive_pids": sorted(true_positive),
        "false_negative_pids": sorted(false_negative),
        "false_positive_pids": sorted(false_positive),
        "unlabeled_count": len(unlabeled_pids),
        "unverified_candidate_pids": sorted(unverified_candidates),
        "strict_gold_count": len(strict_gold),
        "strict_issue_recall": (
            round(len(matched_gold_ids) / len(strict_gold), 4)
            if strict_gold else None
        ),
        "strict_matched_issue_ids": sorted(matched_gold_ids),
        "strict_unmatched_issue_ids": sorted(strict_unmatched),
        "strict_scoring_note": (
            None if strict_gold
            else "Unavailable: benchmark has PID-level booleans but no semantic gold metadata."
        ),
    }


def completed_job_map(existing: Any) -> dict[str, dict[str, Any]]:
    if not isinstance(existing, dict):
        return {}
    result: dict[str, dict[str, Any]] = {}
    for job in existing.get("jobs", []):
        if isinstance(job, dict) and job.get("job_id"):
            result[str(job["job_id"])] = job
    return result


def max_tokens_for_job(args: argparse.Namespace, job: Job) -> int:
    if args.max_tokens > 0:
        return args.max_tokens
    # Compact response: roughly 70 tokens per possible issue plus envelope.
    possible = min(args.max_issues, len(job.target_items))
    return max(384, min(1600, 180 + possible * 90))


def execute_job(
    session: requests.Session,
    job: Job,
    args: argparse.Namespace,
) -> dict[str, Any]:
    allowed_pids = set(job.target_pids)
    base_max_tokens = max_tokens_for_job(args, job)
    attempts: list[dict[str, Any]] = []

    for attempt_number in range(1, args.attempts + 1):
        current_max_tokens = (
            base_max_tokens + (attempt_number - 1) * args.retry_token_step
        )
        body = {
            "model": args.model,
            "messages": build_messages(
                job,
                max_issues=min(args.max_issues, len(job.target_items)),
            ),
            "max_tokens": current_max_tokens,
            "temperature": args.temperature,
            "top_p": args.top_p,
            "top_k": args.top_k,
            "stream": False,
            "response_format": {"type": "json_object"},
            "chat_template_kwargs": {
                "enable_thinking": args.enable_thinking,
            },
        }

        started = time.perf_counter()
        attempt: dict[str, Any] = {
            "attempt": attempt_number,
            "max_tokens": current_max_tokens,
        }

        try:
            response = session.post(
                args.url,
                json=body,
                timeout=args.timeout,
            )
            attempt["http_status"] = response.status_code
            response.raise_for_status()
            data = response.json()

            choice = data["choices"][0]
            message = choice.get("message") or {}
            content = message.get("content") or ""
            finish_reason = choice.get("finish_reason")
            usage = data.get("usage") or {}

            attempt.update({
                "wall_seconds": round(time.perf_counter() - started, 3),
                "finish_reason": finish_reason,
                "usage": usage,
                "reasoning_chars": len(
                    message.get("reasoning_content") or ""
                ),
                "content": content,
            })

            issues, rejected = parse_issues(content, allowed_pids)
            attempt["issues"] = issues
            attempt["rejected_issues"] = rejected

            if finish_reason != "stop":
                attempt["error"] = (
                    f"finish_reason={finish_reason!r}; expected 'stop'"
                )
            else:
                return {
                    "job_id": job.job_id,
                    "status": "success",
                    "target_pids": job.target_pids,
                    "context_before_pids": [
                        item["pid"] for item in job.context_before
                    ],
                    "context_after_pids": [
                        item["pid"] for item in job.context_after
                    ],
                    "issues": issues,
                    "attempts": attempts + [attempt],
                }

        except Exception as exc:
            attempt.setdefault(
                "wall_seconds",
                round(time.perf_counter() - started, 3),
            )
            attempt["error"] = f"{type(exc).__name__}: {exc}"

        attempts.append(attempt)
        if attempt_number < args.attempts:
            time.sleep(args.retry_delay)

    return {
        "job_id": job.job_id,
        "status": "failed",
        "target_pids": job.target_pids,
        "context_before_pids": [
            item["pid"] for item in job.context_before
        ],
        "context_after_pids": [
            item["pid"] for item in job.context_after
        ],
        "issues": [],
        "attempts": attempts,
    }


def deduplicate_issues(jobs: list[dict[str, Any]]) -> list[dict[str, str]]:
    result: list[dict[str, str]] = []
    seen: set[tuple[str, str, str]] = set()
    for job in jobs:
        if job.get("status") != "success":
            continue
        for issue in job.get("issues", []):
            if not isinstance(issue, dict):
                continue
            clean = {
                "pid": normalize_pid(issue.get("pid")),
                "category": normalize_category(issue.get("category")),
                "problem": normalize_space(issue.get("problem")),
            }
            key = (
                clean["pid"],
                clean["category"],
                clean["problem"].casefold(),
            )
            if clean["pid"] and clean["problem"] and key not in seen:
                result.append(clean)
                seen.add(key)
    return result


def self_test() -> int:
    sample = {
        "items": [
            {
                "pid": "p00001",
                "source": "He inhaled.",
                "translation": "Он выдохнул.",
                "expected_issue": {
                    "category": "meaning",
                    "keywords": ["inhaled", "exhaled"],
                },
            },
            {
                "pid": "p00002",
                "source": "The door opened.",
                "translation": "Дверь открылась.",
                "expected_issue": False,
            },
        ]
    }
    items = validate_items(sample)
    parsed, rejected = parse_issues(
        '{"issues":[{"pid":"p00001","category":"meaning",'
        '"problem":"Inhaled is rendered as exhaled."}]}',
        {"p00001", "p00002"},
    )
    assert not rejected
    metrics = score(items, parsed)
    assert metrics["pid_recall"] == 1.0
    assert metrics["false_positive_rate"] == 0.0
    assert metrics["strict_issue_recall"] == 1.0

    jobs = build_jobs(items, "batch", 1, 1, 0, 0)
    assert len(jobs) == 2 and jobs[0].target_pids == ["p00001"]
    print(f"Self-test passed (reviewer_benchmark.py {VERSION})")
    return 0


def build_result(
    args: argparse.Namespace,
    benchmark_path: Path,
    items: list[dict[str, Any]],
    jobs: list[dict[str, Any]],
    started: float,
) -> dict[str, Any]:
    detected = deduplicate_issues(jobs)
    metrics = score(items, detected)
    successful = sum(job.get("status") == "success" for job in jobs)
    failed = sum(job.get("status") == "failed" for job in jobs)

    prompt_tokens = 0
    completion_tokens = 0
    total_wall = 0.0
    for job in jobs:
        for attempt in job.get("attempts", []):
            total_wall += float(attempt.get("wall_seconds") or 0)
            usage = attempt.get("usage") or {}
            prompt_tokens += int(usage.get("prompt_tokens") or 0)
            completion_tokens += int(usage.get("completion_tokens") or 0)

    return {
        "version": VERSION,
        "benchmark": str(benchmark_path),
        "model": args.model,
        "url": args.url,
        "mode": args.mode,
        "batch_size": args.batch_size,
        "scene_size": args.scene_size,
        "context_before": args.context_before,
        "context_after": args.context_after,
        "enable_thinking": args.enable_thinking,
        "item_count": len(items),
        "job_count": len(jobs),
        "successful_jobs": successful,
        "failed_jobs": failed,
        "detected_issues": detected,
        "metrics": metrics,
        "usage": {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": prompt_tokens + completion_tokens,
            "attempt_wall_seconds": round(total_wall, 3),
            "run_wall_seconds": round(time.perf_counter() - started, 3),
        },
        "jobs": jobs,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--benchmark",
        default="./benchmarks/chapter1_reviewer_benchmark.json",
    )
    parser.add_argument(
        "--url",
        default="http://127.0.0.1:8080/v1/chat/completions",
    )
    parser.add_argument(
        "--model",
        default="gemma-4-26B-A4B-it-UD-Q4_K_XL.gguf",
    )
    parser.add_argument(
        "--mode",
        choices=("batch", "scene", "whole"),
        default="batch",
    )
    parser.add_argument("--batch-size", type=int, default=8)
    parser.add_argument("--scene-size", type=int, default=48)
    parser.add_argument("--context-before", type=int, default=0)
    parser.add_argument("--context-after", type=int, default=0)
    parser.add_argument("--max-issues", type=int, default=8)
    parser.add_argument(
        "--max-tokens",
        type=int,
        default=0,
        help="0 = dynamic output budget.",
    )
    parser.add_argument("--attempts", type=int, default=3)
    parser.add_argument("--retry-token-step", type=int, default=384)
    parser.add_argument("--retry-delay", type=float, default=1.0)
    parser.add_argument("--timeout", type=int, default=1800)
    parser.add_argument("--temperature", type=float, default=0.0)
    parser.add_argument("--top-p", type=float, default=1.0)
    parser.add_argument("--top-k", type=int, default=0)
    parser.add_argument("--enable-thinking", action="store_true")
    parser.add_argument(
        "--output",
        default="./benchmark_results/reviewer_latest.json",
    )
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--self-test", action="store_true")
    parser.add_argument("--version", action="store_true")
    parser.add_argument(
        "--rescore-result",
        help="Recalculate metrics for an existing result JSON without model calls.",
    )
    args = parser.parse_args()

    if args.version:
        print(f"reviewer_benchmark.py {VERSION}")
        return 0
    if args.self_test:
        return self_test()

    if args.batch_size < 1 or args.scene_size < 1:
        raise BenchmarkError("Batch and scene sizes must be positive.")
    if args.attempts < 1:
        raise BenchmarkError("--attempts must be at least 1.")
    if args.max_issues < 1:
        raise BenchmarkError("--max-issues must be at least 1.")

    benchmark_path = Path(args.benchmark)
    output_path = Path(args.output)
    items = validate_items(load_json(benchmark_path))

    if args.rescore_result:
        existing_result = load_json(Path(args.rescore_result))
        detected = existing_result.get("detected_issues") or deduplicate_issues(
            existing_result.get("jobs") or []
        )
        existing_result["benchmark"] = str(benchmark_path)
        existing_result["metrics"] = score(items, detected)
        existing_result["rescored_with_version"] = VERSION
        atomic_write_json(output_path, existing_result)
        print(json.dumps(existing_result["metrics"], ensure_ascii=False, indent=2))
        return 0

    planned_jobs = build_jobs(
        items,
        args.mode,
        args.batch_size,
        args.scene_size,
        args.context_before,
        args.context_after,
    )

    existing = {}
    if args.resume and output_path.exists():
        existing = completed_job_map(load_json(output_path))

    session = requests.Session()
    job_records: list[dict[str, Any]] = []
    started = time.perf_counter()

    for index, job in enumerate(planned_jobs, 1):
        old = existing.get(job.job_id)
        if old and old.get("status") == "success":
            record = old
            print(
                f"[{index}/{len(planned_jobs)}] {job.job_id}: "
                "resume success"
            )
        else:
            print(
                f"[{index}/{len(planned_jobs)}] {job.job_id}: "
                f"{len(job.target_items)} target PID(s)"
            )
            record = execute_job(session, job, args)
            print(
                f"  status={record['status']} "
                f"issues={len(record.get('issues', []))}"
            )

        job_records.append(record)
        checkpoint = build_result(
            args,
            benchmark_path,
            items,
            job_records,
            started,
        )
        # Planned job count is more useful than the temporary completed count.
        checkpoint["planned_job_count"] = len(planned_jobs)
        atomic_write_json(output_path, checkpoint)

    final = build_result(
        args,
        benchmark_path,
        items,
        job_records,
        started,
    )
    final["planned_job_count"] = len(planned_jobs)
    atomic_write_json(output_path, final)
    print(json.dumps(final, ensure_ascii=False, indent=2))
    return 0 if final["failed_jobs"] == 0 else 2


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("\nInterrupted.", file=sys.stderr)
        raise SystemExit(130)
    except Exception as exc:
        print(f"ERROR: {type(exc).__name__}: {exc}", file=sys.stderr)
        raise SystemExit(1)
