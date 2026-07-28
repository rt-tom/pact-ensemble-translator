#!/usr/bin/env python3
"""Generate a new repair candidate for PIDs rejected by post-repair verification."""
from __future__ import annotations

import argparse
import copy
import json
import os
import re
import tempfile
import time
from collections import Counter
from pathlib import Path
from typing import Any

import requests

VERSION = "1.0.0"


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


def norm(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def digits(text: str) -> list[str]:
    return re.findall(r"(?<![\w])\d+(?:[.,]\d+)*(?![\w])", text)


def glossary_target(record: Any) -> str | None:
    if isinstance(record, str):
        return record
    if isinstance(record, dict) and isinstance(record.get("target"), str):
        return record["target"]
    return None


def load_glossary(directory: Path) -> dict[str, str]:
    known: dict[str, str] = {}
    for filename in ("locked.json", "established.json", "provisional.json"):
        for source, record in read_json(directory / filename, {}).items():
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
    end = text.rfind("}")
    if start < 0 or end < start:
        raise ValueError("No JSON object in response")
    parsed = json.loads(text[start:end + 1])
    if not isinstance(parsed, dict):
        raise ValueError("Response is not a JSON object")
    return parsed


def render_context(items: list[dict[str, Any]], draft: dict[str, str]) -> str:
    if not items:
        return "(none)"
    return "\n\n".join(
        f"[{item['pid']}]\nEN: {item['source_text']}\nRU: {draft.get(item['pid'], '')}"
        for item in items
    )


def validate_candidate(candidate: str, draft: str, previous: str, source: str) -> list[str]:
    errors: list[str] = []
    if not candidate:
        errors.append("empty")
    if norm(candidate) == norm(draft):
        errors.append("unchanged_from_draft")
    if previous and norm(candidate) == norm(previous):
        errors.append("repeated_rejected_candidate")
    if Counter(digits(source)) != Counter(digits(candidate)):
        errors.append(f"digits {digits(source)}->{digits(candidate)}")
    ratio = len(candidate) / max(1, len(source))
    if ratio < 0.25 or ratio > 3.2:
        errors.append(f"length_ratio={ratio:.2f}")
    return errors


def build_messages(
    request: dict[str, Any], block: dict[str, Any], draft_text: str,
    ordered: list[dict[str, Any]], positions: dict[str, int], draft: dict[str, str],
    known: dict[str, str], chapter_bible: dict[str, Any], feedback: list[str],
) -> list[dict[str, str]]:
    pid = request["pid"]
    index = positions[pid]
    before = ordered[max(0, index - 1):index]
    after = ordered[index + 1:index + 2]
    source_context = "\n".join(item["source_text"] for item in before + [block] + after)
    compact_bible = json.dumps(chapter_bible, ensure_ascii=False, separators=(",", ":"))
    if len(compact_bible) > 10000:
        compact_bible = compact_bible[:10000] + "…"
    system = (
        "You are the repair model in an autonomous English-to-Russian literary "
        "translation pipeline. A previous candidate failed the safety verifier. "
        "Produce a different, complete Russian paragraph that fixes every approved "
        "issue without adding details or damaging natural Russian. Return only JSON."
    )
    user = f"""RELEVANT GLOSSARY:
{relevant_glossary(known, source_context)}

CHAPTER BIBLE:
{compact_bible}

CONTEXT BEFORE:
{render_context(before, draft)}

PID: {pid}
ENGLISH SOURCE:
{block['source_text']}

ORIGINAL RUSSIAN DRAFT:
{draft_text}

PREVIOUS REJECTED/INVALID CANDIDATE:
{request.get('previous_candidate') or '(none)'}

VERIFIER-APPROVED ISSUES:
{json.dumps(request.get('issues') or [], ensure_ascii=False, indent=2)}

WHY THE PREVIOUS ATTEMPT FAILED:
{request.get('reason') or '(none)'}
{request.get('introduced_problem') or ''}
{json.dumps(request.get('failed_gates') or [], ensure_ascii=False)}

ADDITIONAL ATTEMPT FEEDBACK:
{chr(10).join(feedback) or '(none)'}

CONTEXT AFTER:
{render_context(after, draft)}

Return exactly:
{{
  "pid": "{pid}",
  "text": "complete corrected Russian paragraph",
  "reason": "concise description of the correction"
}}

Rules:
- The new text must differ from both the original draft and the rejected candidate.
- Fix every listed issue, but do not rewrite unrelated wording.
- Preserve subject/object, negation, modality, causality, promises, tense/aspect,
  address register, tone, profanity, numbers, and glossary forms.
- Use natural, grammatical literary Russian. Do not produce literal calques.
"""
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


def generate_candidate(
    session: requests.Session, url: str, model: str, messages_builder, validator,
    attempts: int, timeout: int, max_tokens: int,
) -> tuple[str | None, list[dict[str, Any]], str]:
    records: list[dict[str, Any]] = []
    feedback: list[str] = []
    last_reason = ""
    for attempt in range(1, attempts + 1):
        started = time.perf_counter()
        try:
            messages = messages_builder(feedback)
            response = session.post(url, json={
                "model": model,
                "messages": messages,
                "max_tokens": max_tokens,
                "temperature": 0.0,
                "top_p": 1.0,
                "stream": False,
                "response_format": {"type": "json_object"},
                "chat_template_kwargs": {"enable_thinking": False},
            }, timeout=timeout)
            status = response.status_code
            if not response.ok:
                raise RuntimeError(f"HTTP {status}: {norm(response.text)[:2000]}")
            payload = response.json()
            choice = payload["choices"][0]
            if choice.get("finish_reason") != "stop":
                raise ValueError(f"finish_reason={choice.get('finish_reason')!r}")
            content = (choice.get("message") or {}).get("content") or ""
            parsed = extract_json_object(content)
            candidate = norm(str(parsed.get("text") or ""))
            reason = norm(str(parsed.get("reason") or ""))
            errors = validator(candidate)
            record = {
                "attempt": attempt, "http_status": status,
                "wall_seconds": round(time.perf_counter() - started, 3),
                "usage": payload.get("usage") or {}, "content": content,
                "candidate": candidate, "reason": reason, "validation_errors": errors,
            }
            records.append(record)
            last_reason = reason
            if not errors:
                return candidate, records, reason
            feedback.append("Previous retry candidate failed validation: " + ", ".join(errors))
        except Exception as exc:
            records.append({
                "attempt": attempt,
                "wall_seconds": round(time.perf_counter() - started, 3),
                "error": f"{type(exc).__name__}: {exc}",
            })
            feedback.append(f"Previous retry attempt failed: {exc}")
    return None, records, last_reason


def process_chapter(
    work: Path, cfg: dict[str, Any], model: str, url: str,
    attempts: int, timeout: int, max_tokens: int, round_number: int,
) -> dict[str, Any]:
    requests_path = work / "repair_retry_requests.json"
    requests_data = read_json(requests_path, [])
    if not requests_data:
        report = {"version": VERSION, "chapter": work.name, "round": round_number,
                  "requested": 0, "generated": 0, "failed": 0, "records": []}
        atomic_json(work / f"repair_retry_round_{round_number:02d}.json", report)
        return report

    draft = read_json(work / "draft_translations.json", {})
    preverify_path = work / "repaired_translations.preverify.json"
    proposed = read_json(preverify_path, read_json(work / "repaired_translations.json", draft))
    manifest = read_json(work / "manifest.json", {})
    ordered = sorted(manifest.get("blocks") or [], key=lambda item: int(item.get("index", 0)))
    block_map = {item["pid"]: item for item in ordered}
    positions = {item["pid"]: index for index, item in enumerate(ordered)}
    chapter_bible = read_json(work / "chapter_bible.json", {})
    known = load_glossary(Path(cfg["paths"]["glossary_dir"]))
    session = requests.Session()
    records: list[dict[str, Any]] = []

    for number, request in enumerate(requests_data, 1):
        pid = str(request.get("pid") or "")
        if pid not in block_map or pid not in draft:
            records.append({"pid": pid, "generated": False, "error": "PID missing from manifest/draft"})
            continue
        previous = str(request.get("previous_candidate") or proposed.get(pid) or "")

        def builder(feedback: list[str]) -> list[dict[str, str]]:
            return build_messages(
                request, block_map[pid], str(draft[pid]), ordered, positions,
                draft, known, chapter_bible, feedback,
            )

        def validator(candidate: str, p: str = pid, prev: str = previous) -> list[str]:
            return validate_candidate(
                candidate, str(draft[p]), prev, str(block_map[p]["source_text"])
            )

        candidate, attempt_records, reason = generate_candidate(
            session, url, model, builder, validator, attempts, timeout, max_tokens,
        )
        generated = candidate is not None
        if generated:
            proposed[pid] = candidate
        records.append({
            "pid": pid, "generated": generated, "candidate": candidate or "",
            "reason": reason, "attempts": attempt_records, "round": round_number,
        })
        print(f"[{number}/{len(requests_data)}] {pid} -> {'candidate' if generated else 'failed'}", flush=True)

    atomic_json(preverify_path, proposed)
    # The post-verifier will rebuild repaired_translations.json from the draft and accepted candidates.
    history_path = work / "repair_retry_records.json"
    history = read_json(history_path, [])
    history.extend(records)
    atomic_json(history_path, history)
    report = {
        "version": VERSION, "chapter": work.name, "round": round_number,
        "requested": len(requests_data),
        "generated": sum(bool(item.get("generated")) for item in records),
        "failed": sum(not bool(item.get("generated")) for item in records),
        "records": records,
    }
    atomic_json(work / f"repair_retry_round_{round_number:02d}.json", report)
    return report


def self_test() -> int:
    assert validate_candidate("Новый текст.", "Старый текст.", "Плохой текст.", "New text.") == []
    assert "unchanged_from_draft" in validate_candidate("Старый текст.", "Старый текст.", "", "Old text.")
    print(f"Self-test passed (retry_rejected_repairs.py {VERSION})")
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
    parser.add_argument("--max-tokens", type=int, default=1200)
    parser.add_argument("--round", type=int, required=False, default=1)
    parser.add_argument("--self-test", action="store_true")
    parser.add_argument("--version", action="store_true")
    args = parser.parse_args()
    if args.version:
        print(f"retry_rejected_repairs.py {VERSION}")
        return 0
    if args.self_test:
        return self_test()
    if not args.config or not args.model:
        parser.error("--config and --model are required")

    cfg = resolve_config(args.config.resolve())
    input_files = sorted(Path(cfg["paths"]["input_dir"]).glob("*.html"), key=lambda p: natural_key(p.name))
    first = max(1, args.start or 1)
    last = min(len(input_files), args.end or len(input_files))
    selected = input_files[first - 1:last]
    if not selected:
        raise RuntimeError("No chapters selected")
    reports = []
    for source_path in selected:
        work = Path(cfg["paths"]["work_dir"]) / source_path.stem
        reports.append(process_chapter(
            work, cfg, args.model, args.url, args.attempts, args.timeout,
            args.max_tokens, args.round,
        ))
    print(json.dumps(reports, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
