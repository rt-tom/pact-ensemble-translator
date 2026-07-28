#!/usr/bin/env python3
"""Verify repair candidates and expose unresolved work for autonomous retries.

Contract v2 requires four independent checks for every changed paragraph:
faithfulness, issue resolution, natural Russian, and absence of a new error.
Rejected/uncertain candidates and repair PIDs with no valid candidate are written to
repair_retry_requests.json. Finalization is allowed only when unresolved_total is 0.
"""
from __future__ import annotations

import argparse
import copy
import json
import os
import re
import tempfile
import time
from pathlib import Path
from typing import Any

import requests

VERSION = "2.0.0"


def read_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return copy.deepcopy(default)
    return json.loads(path.read_text(encoding="utf-8-sig"))


def atomic_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def natural_key(value: str) -> list[Any]:
    return [int(x) if x.isdigit() else x.casefold() for x in re.split(r"(\d+)", value)]


def resolve_config(config_path: Path) -> dict[str, Any]:
    cfg = read_json(config_path, {})
    base = config_path.parent.resolve()
    for key, value in list(cfg.get("paths", {}).items()):
        path = Path(value)
        if not path.is_absolute():
            cfg["paths"][key] = str((base / path).resolve())
    return cfg


def glossary_target(record: Any) -> str | None:
    if isinstance(record, str):
        return record
    if isinstance(record, dict) and isinstance(record.get("target"), str):
        return record["target"]
    return None


def load_glossary(directory: Path) -> dict[str, str]:
    known: dict[str, str] = {}
    for filename in ("locked.json", "established.json", "provisional.json"):
        data = read_json(directory / filename, {})
        for source, record in data.items():
            value = glossary_target(record)
            if value and source not in known:
                known[source] = value
    return known


def relevant_glossary(known: dict[str, str], source_text: str) -> str:
    folded = source_text.casefold()
    lines = [
        f"- {source} → {target}"
        for source, target in sorted(known.items(), key=lambda item: item[0].casefold())
        if source.casefold() in folded
    ]
    return "\n".join(lines) or "(none)"


def extract_json_object(text: str) -> dict[str, Any]:
    text = text.strip()
    try:
        parsed = json.loads(text)
        if isinstance(parsed, dict):
            return parsed
    except Exception:
        pass
    start = text.find("{")
    if start < 0:
        raise ValueError("No JSON object in response")
    depth = 0
    quoted = False
    escaped = False
    for index in range(start, len(text)):
        char = text[index]
        if quoted:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                quoted = False
            continue
        if char == '"':
            quoted = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                parsed = json.loads(text[start:index + 1])
                if not isinstance(parsed, dict):
                    raise ValueError("Response JSON is not an object")
                return parsed
    raise ValueError("Unterminated JSON response")


def normalize_verdict(value: Any) -> str:
    verdict = str(value or "").strip().casefold()
    verdict = {
        "approve": "accept", "accepted": "accept", "yes": "accept",
        "rejected": "reject", "no": "reject",
        "unsure": "uncertain", "unknown": "uncertain",
    }.get(verdict, verdict)
    if verdict not in {"accept", "reject", "uncertain"}:
        raise ValueError(f"Invalid verdict: {value!r}")
    return verdict


def normalize_bool(value: Any, field: str) -> bool:
    if isinstance(value, bool):
        return value
    folded = str(value or "").strip().casefold()
    if folded in {"true", "yes", "1"}:
        return True
    if folded in {"false", "no", "0"}:
        return False
    raise ValueError(f"Invalid boolean for {field}: {value!r}")


def render_context(items: list[dict[str, Any]], translations: dict[str, str]) -> str:
    if not items:
        return "(none)"
    return "\n\n".join(
        f"[{item['pid']}]\nEN: {item['source_text']}\nRU: {translations.get(item['pid'], '')}"
        for item in items
    )


def build_messages(
    pid: str,
    block: dict[str, Any],
    before_ru: str,
    after_ru: str,
    issues: list[dict[str, Any]],
    ordered_blocks: list[dict[str, Any]],
    positions: dict[str, int],
    draft: dict[str, str],
    known: dict[str, str],
    chapter_bible: dict[str, Any],
    context_size: int,
) -> list[dict[str, str]]:
    index = positions[pid]
    before = ordered_blocks[max(0, index - context_size):index]
    after = ordered_blocks[index + 1:index + 1 + context_size]
    relevant_source = "\n".join(item["source_text"] for item in before + [block] + after)
    compact_bible = json.dumps(chapter_bible, ensure_ascii=False, separators=(",", ":"))
    if len(compact_bible) > 10000:
        compact_bible = compact_bible[:10000] + "…"

    system = (
        "You are the final safety gate for one proposed repair of an English-to-Russian "
        "literary translation. Evaluate the complete AFTER text independently on four "
        "mandatory dimensions. Broken or unnatural Russian is an automatic rejection, "
        "even when the intended issue was fixed. Return exactly one JSON object."
    )
    user = f"""RELEVANT GLOSSARY:
{relevant_glossary(known, relevant_source)}

CHAPTER BIBLE:
{compact_bible}

CONTEXT BEFORE:
{render_context(before, draft)}

TARGET PID: {pid}
ENGLISH SOURCE:
{block['source_text']}

ORIGINAL RUSSIAN (BEFORE):
{before_ru}

PROPOSED REPAIR (AFTER):
{after_ru}

VERIFIER-APPROVED ISSUES THAT MUST BE FIXED:
{json.dumps(issues, ensure_ascii=False, indent=2)}

CONTEXT AFTER:
{render_context(after, draft)}

Return exactly:
{{
  "faithful_to_source": true | false,
  "issue_fixed": true | false,
  "natural_russian": true | false,
  "introduced_new_error": true | false,
  "verdict": "accept" | "reject" | "uncertain",
  "confidence": "high" | "medium" | "low",
  "reason": "One concise sentence in English",
  "introduced_problem": "empty when none; otherwise concise"
}}

Rules:
- Check each boolean independently before choosing verdict.
- ACCEPT is valid only when faithful_to_source=true, issue_fixed=true,
  natural_russian=true, and introduced_new_error=false.
- REJECT broken grammar, malformed collocations, literal calques, changed subject/object,
  lost negation/modality/causality/promise, tense/aspect damage, register inconsistency,
  glossary violations, invented details, or text worse than BEFORE.
- REJECT if AFTER is unchanged in substance or merely rewrites acceptable text without
  resolving every verifier-approved issue for this PID.
- UNCERTAIN only when the source/context genuinely prevents a reliable judgment.
- Proper names must follow the glossary. Latin-only names in otherwise Russian prose are
  unacceptable unless the glossary explicitly preserves Latin.
"""
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


def validate_result(parsed: dict[str, Any]) -> dict[str, Any]:
    result = {
        "faithful_to_source": normalize_bool(parsed.get("faithful_to_source"), "faithful_to_source"),
        "issue_fixed": normalize_bool(parsed.get("issue_fixed"), "issue_fixed"),
        "natural_russian": normalize_bool(parsed.get("natural_russian"), "natural_russian"),
        "introduced_new_error": normalize_bool(parsed.get("introduced_new_error"), "introduced_new_error"),
        "verdict": normalize_verdict(parsed.get("verdict")),
        "confidence": str(parsed.get("confidence") or "low").strip().casefold(),
        "reason": re.sub(r"\s+", " ", str(parsed.get("reason") or "")).strip(),
        "introduced_problem": re.sub(r"\s+", " ", str(parsed.get("introduced_problem") or "")).strip(),
    }
    gates_ok = (
        result["faithful_to_source"]
        and result["issue_fixed"]
        and result["natural_russian"]
        and not result["introduced_new_error"]
    )
    if result["verdict"] == "accept" and not gates_ok:
        result["verdict"] = "reject"
        result["reason"] = (
            result["reason"] + " Structured safety gates do not permit acceptance."
        ).strip()
    if result["confidence"] not in {"high", "medium", "low"}:
        result["confidence"] = "low"
    return result


def verify_one(
    session: requests.Session,
    url: str,
    model: str,
    messages: list[dict[str, str]],
    attempts: int,
    timeout: int,
    max_tokens: int,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    records: list[dict[str, Any]] = []
    for attempt in range(1, attempts + 1):
        started = time.perf_counter()
        try:
            response = session.post(
                url,
                json={
                    "model": model,
                    "messages": messages,
                    "max_tokens": max_tokens,
                    "temperature": 0.0,
                    "top_p": 1.0,
                    "stream": False,
                    "response_format": {"type": "json_object"},
                    "chat_template_kwargs": {"enable_thinking": True},
                    "reasoning_format": "deepseek",
                },
                timeout=timeout,
            )
            response.raise_for_status()
            payload = response.json()
            choice = payload["choices"][0]
            if choice.get("finish_reason") != "stop":
                raise ValueError(f"finish_reason={choice.get('finish_reason')!r}")
            content = (choice.get("message") or {}).get("content") or ""
            result = validate_result(extract_json_object(content))
            records.append({
                "attempt": attempt,
                "wall_seconds": round(time.perf_counter() - started, 3),
                "usage": payload.get("usage") or {},
                "content": content,
                "result": result,
            })
            return result, records
        except Exception as exc:
            records.append({
                "attempt": attempt,
                "wall_seconds": round(time.perf_counter() - started, 3),
                "error": f"{type(exc).__name__}: {exc}",
            })
            if attempt < attempts:
                time.sleep(2)
    return {
        "faithful_to_source": False,
        "issue_fixed": False,
        "natural_russian": False,
        "introduced_new_error": True,
        "verdict": "uncertain",
        "confidence": "low",
        "reason": "Verifier failed after all attempts; candidate remains unresolved.",
        "introduced_problem": "verification_failed",
    }, records


def process_chapter(
    work: Path,
    cfg: dict[str, Any],
    model: str,
    url: str,
    attempts: int,
    timeout: int,
    max_tokens: int,
    context_size: int,
    force: bool,
    continue_round: bool,
    round_number: int,
) -> dict[str, Any]:
    report_path = work / "post_repair_report.json"
    if report_path.exists() and not force and not continue_round:
        report = read_json(report_path, {})
        print(
            f"{work.name}: post-repair verification already complete: "
            f"accepted={report.get('accepted', 0)}, unresolved={report.get('unresolved_total', 0)}",
            flush=True,
        )
        return report

    draft = read_json(work / "draft_translations.json", {})
    repaired_path = work / "repaired_translations.json"
    if not repaired_path.exists():
        raise FileNotFoundError(repaired_path)
    preverify_path = work / "repaired_translations.preverify.json"
    if preverify_path.exists():
        proposed = read_json(preverify_path, {})
    else:
        proposed = read_json(repaired_path, {})
        atomic_json(preverify_path, proposed)

    manifest = read_json(work / "manifest.json", {})
    ordered = sorted(manifest.get("blocks") or [], key=lambda item: int(item.get("index", 0)))
    block_map = {item["pid"]: item for item in ordered}
    positions = {item["pid"]: index for index, item in enumerate(ordered)}
    confirmed_issues = read_json(work / "issues.json", [])
    issues_by_pid: dict[str, list[dict[str, Any]]] = {}
    for issue in confirmed_issues:
        issues_by_pid.setdefault(str(issue.get("pid") or ""), []).append(issue)
    chapter_bible = read_json(work / "chapter_bible.json", {})
    known = load_glossary(Path(cfg["paths"]["glossary_dir"]))
    repair_records = read_json(work / "repair_records.json", [])
    record_by_pid = {
        str(item.get("pid") or ""): item
        for item in repair_records if isinstance(item, dict)
    }

    changed = [pid for pid in draft if str(proposed.get(pid, draft[pid])) != str(draft[pid])]
    changed.sort(key=lambda pid: positions.get(pid, 10**9))
    verifier_dir = work / "post_repair_verifier"
    verifier_dir.mkdir(parents=True, exist_ok=True)
    safe = dict(draft)
    decisions: list[dict[str, Any]] = []
    session = requests.Session()

    for number, pid in enumerate(changed, 1):
        cache_path = verifier_dir / f"{pid}.json"
        cached = read_json(cache_path, {}) if cache_path.exists() else {}
        cache_matches = (
            str(cached.get("before", "")) == str(draft[pid])
            and str(cached.get("after", "")) == str(proposed[pid])
            and isinstance(cached.get("result"), dict)
        )
        if cache_matches and not force:
            result = cached["result"]
            attempt_records = cached.get("attempts", [])
        else:
            messages = build_messages(
                pid, block_map[pid], str(draft[pid]), str(proposed[pid]),
                issues_by_pid.get(pid, []), ordered, positions, draft, known,
                chapter_bible, context_size,
            )
            result, attempt_records = verify_one(
                session, url, model, messages, attempts, timeout, max_tokens,
            )
            atomic_json(cache_path, {
                "pid": pid, "before": draft[pid], "after": proposed[pid],
                "issues": issues_by_pid.get(pid, []), "result": result,
                "attempts": attempt_records, "round": round_number,
            })

        allowed_confidences = {
            str(value).casefold() for value in cfg.get("post_repair_verifier", {}).get(
                "accept_confidences", ["high"]
            )
        }
        accepted = (
            result["verdict"] == "accept"
            and str(result.get("confidence", "low")).casefold() in allowed_confidences
        )
        if result["verdict"] == "accept" and not accepted:
            result = dict(result)
            result["verdict"] = "uncertain"
            result["reason"] = (
                str(result.get("reason") or "")
                + " Acceptance confidence did not meet the configured threshold."
            ).strip()
        if accepted:
            safe[pid] = proposed[pid]
        decision = {
            "pid": pid,
            **result,
            "action": "keep_repair" if accepted else "revert_to_draft",
            "before": draft[pid],
            "proposed_after": proposed[pid],
            "final_after": safe[pid],
            "issues": issues_by_pid.get(pid, []),
            "attempts": attempt_records,
            "round": round_number,
        }
        decisions.append(decision)
        print(
            f"[{number}/{len(changed)}] {pid} -> {result['verdict']} "
            f"({decision['action']})",
            flush=True,
        )

    decision_by_pid = {item["pid"]: item for item in decisions}
    retry_requests: list[dict[str, Any]] = []
    lifecycle: list[dict[str, Any]] = []

    selected_pids = sorted(issues_by_pid, key=lambda pid: positions.get(pid, 10**9))
    for pid in selected_pids:
        issue_list = issues_by_pid[pid]
        decision = decision_by_pid.get(pid)
        repair_record = record_by_pid.get(pid, {})
        if decision and decision["action"] == "keep_repair":
            status = "resolved"
        elif decision:
            status = "unresolved_post_rejected"
            retry_requests.append({
                "pid": pid,
                "reason": decision.get("reason", ""),
                "introduced_problem": decision.get("introduced_problem", ""),
                "failed_gates": [
                    name for name in (
                        "faithful_to_source", "issue_fixed", "natural_russian"
                    ) if not decision.get(name, False)
                ] + (["introduced_new_error"] if decision.get("introduced_new_error") else []),
                "previous_candidate": decision.get("proposed_after", ""),
                "issues": issue_list,
                "round": round_number,
            })
        else:
            status = "unresolved_no_candidate"
            retry_requests.append({
                "pid": pid,
                "reason": repair_record.get("reason", "No valid repair candidate was produced."),
                "introduced_problem": ", ".join(repair_record.get("validation_errors") or []),
                "failed_gates": ["candidate_missing"],
                "previous_candidate": repair_record.get("proposed_after", ""),
                "issues": issue_list,
                "round": round_number,
            })
        for issue in issue_list:
            lifecycle.append({
                "issue_id": issue.get("issue_id"),
                "pid": pid,
                "severity": issue.get("severity"),
                "category": issue.get("category"),
                "verifier_decision": issue.get("verifier_decision", "repair"),
                "verifier_confidence": issue.get("verifier_confidence", ""),
                "status": status,
                "final_text_changed": safe.get(pid, draft.get(pid)) != draft.get(pid),
                "post_repair_verdict": decision.get("verdict", "not_checked") if decision else "not_checked",
                "post_repair_reason": decision.get("reason", "") if decision else repair_record.get("reason", ""),
            })

    atomic_json(repaired_path, safe)
    atomic_json(work / "repair_retry_requests.json", retry_requests)
    atomic_json(work / "issue_lifecycle.json", lifecycle)
    report = {
        "version": VERSION,
        "chapter": work.name,
        "model": model,
        "round": round_number,
        "changed_candidates": len(changed),
        "accepted": sum(item["action"] == "keep_repair" for item in decisions),
        "reverted": sum(item["action"] == "revert_to_draft" for item in decisions),
        "uncertain": sum(item["verdict"] == "uncertain" for item in decisions),
        "no_candidate": sum(1 for pid in selected_pids if pid not in decision_by_pid),
        "retry_required": len(retry_requests),
        "unresolved_total": len(retry_requests),
        "resolved_issue_count": sum(item["status"] == "resolved" for item in lifecycle),
        "unresolved_issue_count": sum(item["status"] != "resolved" for item in lifecycle),
        "decisions": decisions,
        "retry_requests": retry_requests,
    }
    atomic_json(report_path, report)
    return report


def self_test() -> int:
    assert normalize_verdict("approve") == "accept"
    assert normalize_verdict("rejected") == "reject"
    accepted = validate_result({
        "faithful_to_source": True, "issue_fixed": True, "natural_russian": True,
        "introduced_new_error": False, "verdict": "accept", "confidence": "high",
    })
    assert accepted["verdict"] == "accept"
    inconsistent = validate_result({
        "faithful_to_source": True, "issue_fixed": True, "natural_russian": False,
        "introduced_new_error": False, "verdict": "accept", "confidence": "high",
    })
    assert inconsistent["verdict"] == "reject"
    print(f"Self-test passed (verify_repair_results.py {VERSION})")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path)
    parser.add_argument("--start", type=int)
    parser.add_argument("--end", type=int)
    parser.add_argument("--model")
    parser.add_argument("--url", default="http://127.0.0.1:8080/v1/chat/completions")
    parser.add_argument("--attempts", type=int, default=3)
    parser.add_argument("--timeout", type=int, default=600)
    parser.add_argument("--max-tokens", type=int, default=768)
    parser.add_argument("--context-size", type=int, default=1)
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--continue-round", action="store_true")
    parser.add_argument("--round", type=int, default=1)
    parser.add_argument("--self-test", action="store_true")
    parser.add_argument("--version", action="store_true")
    args = parser.parse_args()

    if args.version:
        print(f"verify_repair_results.py {VERSION}")
        return 0
    if args.self_test:
        return self_test()
    if not args.config or not args.model:
        parser.error("--config and --model are required")

    config_path = args.config.resolve()
    cfg = resolve_config(config_path)
    input_files = sorted(
        Path(cfg["paths"]["input_dir"]).glob("*.html"), key=lambda path: natural_key(path.name)
    )
    first = max(1, args.start or 1)
    last = min(len(input_files), args.end or len(input_files))
    selected = input_files[first - 1:last]
    if not selected:
        raise RuntimeError("No chapters selected")

    reports = []
    for source_path in selected:
        work = Path(cfg["paths"]["work_dir"]) / source_path.stem
        print(f"Post-verifying repairs for {source_path.name}, round {args.round}", flush=True)
        reports.append(process_chapter(
            work, cfg, args.model, args.url, args.attempts, args.timeout,
            args.max_tokens, args.context_size, args.force, args.continue_round,
            args.round,
        ))
    print(json.dumps(reports, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
