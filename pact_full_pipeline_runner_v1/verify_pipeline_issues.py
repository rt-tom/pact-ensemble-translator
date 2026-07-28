#!/usr/bin/env python3
"""Verify Qwen and heuristic deterministic candidates with Gemma.

Designed for pact_translate_v3.py 3.0.4 work directories.
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

VERSION = "2.1.0"


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


def merge(a: dict[str, Any], b: dict[str, Any]) -> dict[str, Any]:
    result = copy.deepcopy(a)
    for key, value in b.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = merge(result[key], value)
        else:
            result[key] = value
    return result


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


def load_glossary(directory: Path) -> tuple[dict[str, str], dict[str, Any]]:
    locked = read_json(directory / "locked.json", {})
    established = read_json(directory / "established.json", {})
    provisional = read_json(directory / "provisional.json", {})
    known: dict[str, str] = {}
    metadata: dict[str, Any] = {}
    for source, target in locked.items():
        if isinstance(target, str):
            known[source] = target
            metadata[source] = {"target": target, "status": "locked"}
    for status, collection in (("established", established), ("provisional", provisional)):
        for source, record in collection.items():
            target = glossary_target(record)
            if target and source not in known:
                known[source] = target
                metadata[source] = {"target": target, "status": status, "record": record}
    return known, metadata


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


def normalize_decision(value: Any) -> str:
    decision = str(value or "").strip().casefold()
    decision = {
        "confirm": "repair", "confirmed": "repair", "yes": "repair", "true": "repair",
        "reject": "keep", "rejected": "keep", "no": "keep", "false": "keep",
        "leave": "keep", "unchanged": "keep",
        "unsure": "uncertain", "unknown": "uncertain",
    }.get(decision, decision)
    if decision not in {"repair", "keep", "uncertain"}:
        raise ValueError(f"Invalid verifier decision: {value!r}")
    return decision




def critique_explicitly_requests_keep(issue: dict[str, Any]) -> bool:
    """Recognize reviewer candidates that explicitly conclude no edit is needed.

    This is intentionally conservative: both an explicit keep instruction and a
    positive assessment of the current translation must be present.
    """
    instruction = re.sub(r"\s+", " ", str(issue.get("repair_instruction") or "")).casefold()
    problem = re.sub(r"\s+", " ", str(issue.get("problem") or "")).casefold()
    keep_cues = (
        "оставить как есть", "оставить без изменений", "не менять",
        "изменения не требуются", "правка не требуется",
    )
    acceptable_cues = (
        "перевод верен", "перевод корректен", "перевод допустим",
        "ошибки нет", "смысл верен", "регистр и смысл верны",
    )
    return any(cue in instruction for cue in keep_cues) and any(
        cue in problem for cue in acceptable_cues
    )


def mixed_script_tokens(text: str) -> list[str]:
    tokens = re.findall(r"[A-Za-zА-Яа-яЁё][A-Za-zА-Яа-яЁё'’\-]*", text)
    return sorted({
        token for token in tokens
        if re.search(r"[A-Za-z]", token) and re.search(r"[А-Яа-яЁё]", token)
    })


def english_residue_tokens(text: str) -> list[str]:
    tokens = re.findall(r"(?<![A-Za-z])[A-Za-z][A-Za-z'’\-]{2,}(?![A-Za-z])", text)
    result = []
    for token in tokens:
        if token.isupper() and len(token) <= 6:
            continue
        result.append(token)
    return sorted(set(result), key=str.casefold)


def augment_deterministic_issues(
    raw_issues: list[dict[str, Any]],
    blocks: list[dict[str, Any]],
    draft: dict[str, str],
) -> list[dict[str, Any]]:
    result = copy.deepcopy(raw_issues)
    existing = {
        (item.get("pid"), item.get("category"), item.get("problem"))
        for item in result
    }
    counter = 1
    used_ids = {str(item.get("issue_id") or "") for item in result}

    def next_id() -> str:
        nonlocal counter
        while f"external_deterministic_{counter:04d}" in used_ids:
            counter += 1
        value = f"external_deterministic_{counter:04d}"
        used_ids.add(value)
        counter += 1
        return value

    for block in blocks:
        pid = block["pid"]
        target = draft.get(pid, "")
        mixed = mixed_script_tokens(target)
        if mixed:
            problem = f"Mixed Latin/Cyrillic token(s): {mixed}"
            key = (pid, "mixed_script", problem)
            if key not in existing:
                result.append({
                    "pid": pid,
                    "severity": "critical",
                    "category": "mixed_script",
                    "problem": problem,
                    "repair_instruction": "Replace mixed-script tokens with consistent Russian text.",
                    "suggested_text": "",
                    "source": "deterministic_external",
                    "deterministic": True,
                    "status": "open",
                    "issue_id": next_id(),
                })
                existing.add(key)
        english = english_residue_tokens(target)
        if english:
            problem = f"Possible untranslated English token(s): {english}"
            key = (pid, "english_residue", problem)
            if key not in existing:
                result.append({
                    "pid": pid,
                    "severity": "major",
                    "category": "english_residue",
                    "problem": problem,
                    "repair_instruction": "Translate or correctly transliterate the English residue.",
                    "suggested_text": "",
                    "source": "deterministic_external",
                    "deterministic": True,
                    "status": "open",
                    "issue_id": next_id(),
                })
                existing.add(key)
    return result


def build_messages(
    issue: dict[str, Any],
    block: dict[str, Any],
    draft: dict[str, str],
    ordered_blocks: list[dict[str, Any]],
    positions: dict[str, int],
    known: dict[str, str],
    chapter_bible: dict[str, Any],
    context_size: int,
) -> list[dict[str, str]]:
    pid = issue["pid"]
    index = positions[pid]
    before = ordered_blocks[max(0, index - context_size):index]
    after = ordered_blocks[index + 1:index + 1 + context_size]

    def render(items: list[dict[str, Any]]) -> str:
        if not items:
            return "(none)"
        return "\n\n".join(
            f"[{item['pid']}]\nEN: {item['source_text']}\nRU: {draft.get(item['pid'], '')}"
            for item in items
        )

    relevant_source = "\n".join(
        item["source_text"] for item in before + [block] + after
    )
    compact_bible = json.dumps(chapter_bible, ensure_ascii=False, separators=(",", ":"))
    if len(compact_bible) > 10000:
        compact_bible = compact_bible[:10000] + "…"

    system = (
        "You make an operational decision for an autonomous English-to-Russian "
        "literary translation pipeline. Judge only the proposed critique. Decide "
        "whether the current Russian TARGET must actually be changed. Do not confirm "
        "a critique merely because its reasoning correctly says the translation is fine. "
        "Return exactly one JSON object and no text outside JSON."
    )
    user = f"""LOCKED/ESTABLISHED GLOSSARY RELEVANT TO THIS PASSAGE:
{relevant_glossary(known, relevant_source)}

CHAPTER CONTEXT:
{compact_bible}

CONTEXT BEFORE (context only):
{render(before)}

TARGET:
[{pid}]
EN: {block['source_text']}
RU: {draft.get(pid, '')}

PROPOSED CRITIQUE:
Severity: {issue.get('severity', 'major')}
Category: {issue.get('category', 'meaning')}
Claim: {issue.get('problem', '')}
Repair instruction: {issue.get('repair_instruction', '')}

CONTEXT AFTER (context only):
{render(after)}

Return exactly:
{{
  "decision": "repair" | "keep" | "uncertain",
  "confidence": "high" | "medium" | "low",
  "reason": "One concise sentence in English",
  "repair_goal": "Concise instruction describing what must change; empty unless decision=repair"
}}

Rules:
- REPAIR only when this exact critique identifies a real error and TARGET must be changed.
- KEEP when TARGET is acceptable, the critique itself says the translation is correct or should remain unchanged, the claim contradicts glossary/context, or it is only a stylistic alternative.
- Do not return REPAIR merely to agree with a critique whose conclusion is "no error", "translation is correct", or "leave as is".
- REPAIR may be appropriate for a small but objective error even when severity is minor.
- A model-generated chapter-bible target is contextual evidence, not permission to leave a proper name in Latin script. In otherwise Russian prose, a Latin-only name is acceptable only when the locked/established glossary explicitly preserves Latin.
- Use UNCERTAIN only when the evidence genuinely does not permit a reliable operational decision.
"""
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


def verify_issue(
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
        record: dict[str, Any] = {"attempt": attempt}
        try:
            payload = {
                "model": model,
                "messages": messages,
                "max_tokens": max_tokens,
                "temperature": 0.0,
                "top_p": 1.0,
                "stream": False,
                "response_format": {"type": "json_object"},
                "chat_template_kwargs": {"enable_thinking": True},
                "reasoning_format": "deepseek",
            }
            response = session.post(url, json=payload, timeout=timeout)
            record["http_status"] = response.status_code
            response.raise_for_status()
            data = response.json()
            choice = data["choices"][0]
            message = choice.get("message") or {}
            record.update({
                "wall_seconds": round(time.perf_counter() - started, 3),
                "finish_reason": choice.get("finish_reason"),
                "usage": data.get("usage") or {},
                "reasoning_chars": len(message.get("reasoning_content") or ""),
                "content": message.get("content") or "",
            })
            if choice.get("finish_reason") != "stop":
                raise ValueError(f"finish_reason={choice.get('finish_reason')!r}")
            parsed = extract_json_object(record["content"])
            decision = normalize_decision(parsed.get("decision", parsed.get("verdict")))
            confidence = str(parsed.get("confidence") or "").strip().casefold()
            if confidence not in {"high", "medium", "low"}:
                confidence = "unspecified"
            reason = re.sub(r"\s+", " ", str(parsed.get("reason") or "")).strip()
            repair_goal = re.sub(r"\s+", " ", str(parsed.get("repair_goal") or "")).strip()
            if decision == "repair" and confidence != "high":
                decision = "uncertain"
                reason = (reason + " Repair decision did not meet the required high confidence.").strip()
            result = {
                "decision": decision,
                "verdict": {"repair": "confirm", "keep": "reject", "uncertain": "uncertain"}[decision],
                "confidence": confidence,
                "reason": reason,
                "repair_goal": repair_goal,
            }
            record["parsed"] = result
            records.append(record)
            return result, records
        except Exception as exc:
            record.update({
                "wall_seconds": round(time.perf_counter() - started, 3),
                "error": f"{type(exc).__name__}: {exc}",
            })
            records.append(record)
    raise RuntimeError(f"Verifier failed after {attempts} attempts: {records[-1].get('error')}")


def process_chapter(
    work: Path,
    cfg: dict[str, Any],
    model: str,
    url: str,
    attempts: int,
    timeout: int,
    max_tokens: int,
    context_size: int,
    force: bool = False,
    reuse_raw_backup: bool = False,
) -> dict[str, Any]:
    if force:
        import shutil
        shutil.rmtree(work / "verifier", ignore_errors=True)
        for name in ("verified_issues.json", "verifier_report.json"):
            path = work / name
            if path.exists():
                path.unlink()

    manifest = read_json(work / "manifest.json", {})
    blocks = manifest.get("blocks") or []
    if not blocks:
        raise RuntimeError(f"No blocks in {work / 'manifest.json'}")
    draft = read_json(work / "draft_translations.json", {})
    raw_backup = work / "issues.qwen_raw.json"
    if reuse_raw_backup:
        if not raw_backup.exists():
            raise RuntimeError(
                f"Cannot reuse raw candidates: {raw_backup} does not exist"
            )
        raw_issues = read_json(raw_backup, [])
    else:
        raw_issues = read_json(work / "issues.json", [])
        if not raw_issues and not (work / "issues.json").exists():
            raise RuntimeError(f"Audit issues not found in {work}")
        raw_issues = augment_deterministic_issues(raw_issues, blocks, draft)
        if force or not raw_backup.exists():
            atomic_json(raw_backup, raw_issues)

    chapter_bible = read_json(work / "chapter_bible.json", {})
    known, _ = load_glossary(Path(cfg["paths"]["glossary_dir"]))
    block_map = {item["pid"]: item for item in blocks}
    ordered = sorted(blocks, key=lambda item: int(item.get("index", 0)))
    positions = {item["pid"]: index for index, item in enumerate(ordered)}

    verifier_dir = work / "verifier"
    verifier_dir.mkdir(parents=True, exist_ok=True)
    session = requests.Session()
    kept: list[dict[str, Any]] = []
    decisions: list[dict[str, Any]] = []

    for number, issue in enumerate(raw_issues, start=1):
        pid = issue.get("pid")
        if pid not in block_map:
            continue
        if critique_explicitly_requests_keep(issue):
            result = {
                "decision": "keep",
                "verdict": "reject",
                "confidence": "high",
                "reason": "The critique explicitly concludes that the current translation should remain unchanged.",
                "repair_goal": "",
            }
            decisions.append({
                "issue_id": issue.get("issue_id"), "pid": pid,
                **result, "issue": issue, "attempts": [],
                "prefilter": "explicit_keep",
            })
            print(
                f"[{number}/{len(raw_issues)}] {pid} "
                f"{issue.get('category')} explicit keep -> keep",
                flush=True,
            )
            continue

        hard_deterministic = set(
            cfg.get("verifier", {}).get(
                "hard_deterministic_categories",
                ["missing", "mixed_script"],
            )
        )
        if issue.get("deterministic") and issue.get("category") in hard_deterministic:
            confirmed = copy.deepcopy(issue)
            confirmed.update({
                "status": "verified_repair_deterministic_hard",
                "verifier_decision": "repair",
                "verifier_confidence": "deterministic",
                "verifier_reason": "Hard deterministic issue requires repair.",
                "verifier_repair_goal": str(issue.get("repair_instruction") or issue.get("problem") or ""),
            })
            kept.append(confirmed)
            decisions.append({
                "issue_id": issue.get("issue_id"), "pid": pid,
                "decision": "repair", "verdict": "confirm", "confidence": "deterministic",
                "reason": "Hard deterministic issue requires repair.",
                "repair_goal": str(issue.get("repair_instruction") or issue.get("problem") or ""),
                "issue": issue,
            })
            print(
                f"[{number}/{len(raw_issues)}] {pid} "
                f"{issue.get('category')} hard deterministic -> repair",
                flush=True,
            )
            continue

        cache_path = verifier_dir / f"{issue.get('issue_id') or f'issue_{number:04d}'}.json"
        if cache_path.exists():
            cached = read_json(cache_path, {})
            result = dict(cached["result"])
            if "decision" not in result:
                result["decision"] = normalize_decision(result.get("verdict"))
            if "verdict" not in result:
                result["verdict"] = {
                    "repair": "confirm", "keep": "reject", "uncertain": "uncertain"
                }[result["decision"]]
            result.setdefault("repair_goal", "")
            attempt_records = cached.get("attempts", [])
        else:
            messages = build_messages(
                issue, block_map[pid], draft, ordered, positions, known,
                chapter_bible, context_size,
            )
            result, attempt_records = verify_issue(
                session, url, model, messages, attempts, timeout, max_tokens,
            )
            atomic_json(cache_path, {
                "issue": issue, "result": result, "attempts": attempt_records,
            })
        decision = {
            "issue_id": issue.get("issue_id"), "pid": pid,
            **result, "issue": issue, "attempts": attempt_records,
        }
        decisions.append(decision)
        print(
            f"[{number}/{len(raw_issues)}] {pid} {issue.get('category')} -> "
            f"{result['decision']} ({result['confidence']})",
            flush=True,
        )
        if result["decision"] == "repair":
            confirmed = copy.deepcopy(issue)
            if issue.get("deterministic"):
                confirmed["source"] = "deterministic_gemma_verified"
                confirmed["status"] = "verified_repair_deterministic"
            else:
                confirmed["source"] = "qwen_audit_gemma_verified"
                confirmed["status"] = "verified_repair"
            confirmed.update({
                "verifier_decision": "repair",
                "verifier_confidence": result["confidence"],
                "verifier_reason": result.get("reason", ""),
                "verifier_repair_goal": result.get("repair_goal", ""),
            })
            kept.append(confirmed)

    # De-duplicate exact issues while retaining order.
    deduped: list[dict[str, Any]] = []
    seen: set[tuple[Any, ...]] = set()
    for issue in kept:
        key = (issue.get("pid"), issue.get("category"), issue.get("problem"))
        if key not in seen:
            deduped.append(issue)
            seen.add(key)

    report = {
        "version": VERSION,
        "chapter": work.name,
        "model": model,
        "raw_issue_count": len(raw_issues),
        "deterministic_count": sum(bool(item.get("deterministic")) for item in raw_issues),
        "reviewer_candidate_count": sum(not bool(item.get("deterministic")) for item in raw_issues),
        "repair_total": len(deduped),
        "keep_total": sum(item.get("decision") == "keep" for item in decisions),
        "uncertain_total": sum(item.get("decision") == "uncertain" for item in decisions),
        "repair_reviewer": sum(
            item.get("source") == "qwen_audit_gemma_verified" for item in deduped
        ),
        "repair_deterministic_verified": sum(
            item.get("source") == "deterministic_gemma_verified" for item in deduped
        ),
        "repair_deterministic_hard": sum(
            item.get("status") == "verified_repair_deterministic_hard" for item in deduped
        ),
        "confirmed_total": len(deduped),
        "rejected_candidates": sum(item.get("decision") == "keep" for item in decisions),
        "uncertain_candidates": sum(item.get("decision") == "uncertain" for item in decisions),
        "decisions": decisions,
    }
    atomic_json(work / "verified_issues.json", deduped)
    atomic_json(work / "verifier_report.json", report)
    atomic_json(work / "issues.json", deduped)
    if report["uncertain_total"] and cfg.get("verifier", {}).get("fail_on_uncertain", True):
        raise RuntimeError(
            f"{report['uncertain_total']} verifier candidate(s) remain uncertain"
        )
    return report


def self_test() -> int:
    assert normalize_decision("confirmed") == "repair"
    assert normalize_decision("NO") == "keep"
    assert mixed_script_tokens("Бristлс и Бристлз") == ["Бristлс"]
    assert "steady" in english_residue_tokens("свой steady progress")
    assert extract_json_object('x {"decision":"repair"} y')["decision"] == "repair"
    assert critique_explicitly_requests_keep({
        "problem": "Перевод верен.",
        "repair_instruction": "Оставить как есть.",
    })
    assert not critique_explicitly_requests_keep({
        "problem": "Перевод содержит ошибку.",
        "repair_instruction": "Оставить как есть.",
    })
    print(f"Self-test passed (verify_pipeline_issues.py {VERSION})")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path)
    parser.add_argument("--start", type=int)
    parser.add_argument("--end", type=int)
    parser.add_argument("--model", required=False)
    parser.add_argument("--url", default="http://127.0.0.1:8080/v1/chat/completions")
    parser.add_argument("--attempts", type=int, default=3)
    parser.add_argument("--timeout", type=int, default=600)
    parser.add_argument("--max-tokens", type=int, default=768)
    parser.add_argument("--context-size", type=int, default=2)
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--reuse-raw-backup", action="store_true")
    parser.add_argument("--self-test", action="store_true")
    parser.add_argument("--version", action="store_true")
    args = parser.parse_args()

    if args.version:
        print(f"verify_pipeline_issues.py {VERSION}")
        return 0
    if args.self_test:
        return self_test()
    if not args.config or not args.model:
        parser.error("--config and --model are required")

    config_path = args.config.resolve()
    cfg = resolve_config(config_path)
    input_files = sorted(Path(cfg["paths"]["input_dir"]).glob("*.html"), key=lambda p: natural_key(p.name))
    first = max(1, args.start or 1)
    last = min(len(input_files), args.end or len(input_files))
    selected = input_files[first - 1:last]
    if not selected:
        raise RuntimeError("No chapters selected")

    all_reports = []
    for source_path in selected:
        work = Path(cfg["paths"]["work_dir"]) / source_path.stem
        print(f"Verifying {source_path.name}", flush=True)
        all_reports.append(process_chapter(
            work, cfg, args.model, args.url, args.attempts,
            args.timeout, args.max_tokens, args.context_size, args.force,
            args.reuse_raw_backup,
        ))
    print(json.dumps(all_reports, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
