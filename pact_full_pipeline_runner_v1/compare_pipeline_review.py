#!/usr/bin/env python3
"""
compare_pipeline_review.py — наглядный отчёт по автоматическому Pact pipeline.

Сравнивает:
  1) английский оригинал;
  2) первый перевод Gemma из draft_translations.json;
  3) кандидаты Qwen;
  4) решения Gemma verifier;
  5) фактический текст после repair;
  6) финальный HTML, если этап formatting/finalize уже завершён.

Скрипт ничего не изменяет в переводе и не обращается к моделям.

Примеры:
    py compare_pipeline_review.py --start 60 --end 60 --open
    py compare_pipeline_review.py --run-root D:\\pact\\pact_translator_v3\\pipeline_runs\\chapter_60_to_60 --open
    py compare_pipeline_review.py --latest --open
"""

from __future__ import annotations

import argparse
import copy
import csv
import difflib
import html
import json
import os
import re
import sys
import webbrowser
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Iterable

from bs4 import BeautifulSoup, Tag

VERSION = "1.2.0"

TOKEN_RE = re.compile(
    r"\s+|[A-Za-zА-Яа-яЁё0-9]+(?:[’'\-][A-Za-zА-Яа-яЁё0-9]+)*|.",
    re.DOTALL,
)


@dataclass
class ParagraphRecord:
    pid: str
    index: int
    tag: str
    chunk_id: str
    source_text: str
    initial_text: str
    final_text: str
    final_stage: str
    text_changed: bool
    similarity: float
    change_ratio: float
    severity: str
    initial_words: int
    final_words: int
    word_delta: int
    initial_chars: int
    final_chars: int
    char_delta: int
    diff_html: str
    final_html: str
    raw_issue_count: int
    confirmed_issue_count: int
    rejected_issue_count: int
    uncertain_issue_count: int
    repair_action: str
    repair_accepted: bool
    repair_reason: str
    repair_validation_errors: list[str]
    post_repair_verdict: str
    post_repair_reason: str
    post_repair_action: str
    raw_issues: list[dict[str, Any]]
    verifier_decisions: list[dict[str, Any]]
    confirmed_issues: list[dict[str, Any]]
    repair_records: list[dict[str, Any]]


def read_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return copy.deepcopy(default)
    try:
        with path.open("r", encoding="utf-8-sig") as file:
            return json.load(file)
    except (OSError, json.JSONDecodeError):
        return copy.deepcopy(default)


def write_text_atomic(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8", newline="\n") as file:
        file.write(text)
        file.flush()
        os.fsync(file.fileno())
    os.replace(temporary, path)


def write_json_atomic(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8", newline="\n") as file:
        json.dump(data, file, ensure_ascii=False, indent=2)
        file.flush()
        os.fsync(file.fileno())
    os.replace(temporary, path)


def normalize_space(value: str) -> str:
    return re.sub(r"\s+", " ", value or "").strip()


def count_words(value: str) -> int:
    return len(
        re.findall(
            r"[A-Za-zА-Яа-яЁё0-9]+(?:[’'\-][A-Za-zА-Яа-яЁё0-9]+)*",
            value or "",
        )
    )


def natural_key(value: str) -> list[Any]:
    return [
        int(piece) if piece.isdigit() else piece.casefold()
        for piece in re.split(r"(\d+)", value)
    ]


def esc(value: Any) -> str:
    return html.escape(str(value or ""))


def tokenize_for_diff(value: str) -> list[str]:
    return TOKEN_RE.findall(value or "")


def word_diff_html(before: str, after: str) -> str:
    before_tokens = tokenize_for_diff(before)
    after_tokens = tokenize_for_diff(after)
    matcher = difflib.SequenceMatcher(
        None,
        before_tokens,
        after_tokens,
        autojunk=False,
    )
    result: list[str] = []
    for opcode, i1, i2, j1, j2 in matcher.get_opcodes():
        old = "".join(before_tokens[i1:i2])
        new = "".join(after_tokens[j1:j2])
        if opcode == "equal":
            result.append(html.escape(new))
        elif opcode == "delete":
            result.append(f'<del title="Удалено">{html.escape(old)}</del>')
        elif opcode == "insert":
            result.append(f'<ins title="Добавлено">{html.escape(new)}</ins>')
        else:
            result.append(f'<del title="Заменено">{html.escape(old)}</del>')
            result.append(f'<ins title="Новая версия">{html.escape(new)}</ins>')
    return "".join(result)


def severity_from_ratio(changed: bool, ratio: float) -> str:
    if not changed:
        return "unchanged"
    if ratio < 0.05:
        return "minor"
    if ratio < 0.18:
        return "moderate"
    return "major"


def visible_text(fragment: str) -> str:
    if not fragment:
        return ""
    return normalize_space(
        BeautifulSoup(fragment, "html.parser").get_text(" ", strip=True)
    )


def inner_html(tag: Tag) -> str:
    return "".join(str(child) for child in tag.contents)


def parse_html_by_pid(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    soup = BeautifulSoup(path.read_text(encoding="utf-8"), "html.parser")
    result: dict[str, str] = {}
    for tag in soup.find_all(attrs={"data-pid": True}):
        if isinstance(tag, Tag):
            result[str(tag.get("data-pid"))] = inner_html(tag)
    return result


def parse_html_by_manifest(path: Path, manifest: dict[str, Any]) -> dict[str, str]:
    if not path.exists():
        return {}
    by_pid = parse_html_by_pid(path)
    if by_pid:
        return by_pid
    soup = BeautifulSoup(path.read_text(encoding="utf-8"), "html.parser")
    blocks = sorted(
        [item for item in (manifest.get("blocks") or []) if isinstance(item, dict)],
        key=lambda item: int(item.get("index", 0)),
    )
    allowed = {str(item.get("tag") or "p") for item in blocks}
    candidates: list[Tag] = []
    for tag in soup.find_all(list(allowed)):
        if not isinstance(tag, Tag) or not normalize_space(tag.get_text(" ", strip=True)):
            continue
        if any(
            isinstance(child, Tag) and child.name in allowed
            for child in tag.find_all(list(allowed))
        ):
            continue
        candidates.append(tag)
    if len(candidates) != len(blocks):
        return {}
    return {
        str(block.get("pid")): inner_html(tag)
        for block, tag in zip(blocks, candidates)
    }


def issue_selected_for_repair(issue: dict[str, Any], cfg: dict[str, Any]) -> bool:
    repair = cfg.get("repair") or {}
    decision = str(issue.get("verifier_decision") or "").casefold()
    confidence = str(issue.get("verifier_confidence") or "").casefold()
    if decision:
        return (
            decision in {
                str(value).casefold() for value in
                (repair.get("auto_repair_verified_decisions") or ["repair"])
            }
            and confidence in {
                str(value).casefold() for value in
                (repair.get("auto_repair_verifier_confidences") or ["high", "deterministic"])
            }
        )
    if issue.get("deterministic"):
        if not repair.get("auto_repair_deterministic", True):
            return False
        return str(issue.get("category") or "") in set(
            repair.get("auto_repair_deterministic_categories") or []
        )
    return str(issue.get("severity") or "") in set(
        repair.get("auto_repair_severities") or ["critical", "major"]
    )


def chunk_map(manifest: dict[str, Any]) -> dict[str, str]:
    result: dict[str, str] = {}
    for chunk in manifest.get("chunks") or []:
        if not isinstance(chunk, dict):
            continue
        chunk_id = str(chunk.get("chunk_id") or "")
        for pid in chunk.get("pids") or []:
            result[str(pid)] = chunk_id
    return result


def issue_key(issue: dict[str, Any]) -> tuple[str, str, str]:
    return (
        str(issue.get("pid") or ""),
        str(issue.get("category") or ""),
        normalize_space(str(issue.get("problem") or "")),
    )


def load_raw_issues(chapter_dir: Path) -> list[dict[str, Any]]:
    # After verifier starts, issues.qwen_raw.json is the authoritative backup.
    for name in ("issues.qwen_raw.json", "issues.json"):
        path = chapter_dir / name
        if path.exists():
            data = read_json(path, [])
            if isinstance(data, list):
                return [item for item in data if isinstance(item, dict)]

    # In-progress audit fallback: combine deterministic candidates and completed
    # Qwen leaf packages without counting split metadata or summaries.
    combined: list[dict[str, Any]] = []
    deterministic = read_json(chapter_dir / "deterministic_issues.json", [])
    if isinstance(deterministic, list):
        combined.extend(
            item for item in deterministic if isinstance(item, dict)
        )

    audit_dir = chapter_dir / "audit"
    if audit_dir.exists():
        for path in sorted(
            audit_dir.glob("c*_q*.json"),
            key=lambda item: natural_key(item.name),
        ):
            data = read_json(path, {})
            if not isinstance(data, dict):
                continue
            if data.get("split_into") or data.get("failed"):
                continue
            issues = data.get("issues") or []
            if isinstance(issues, list):
                combined.extend(
                    item for item in issues if isinstance(item, dict)
                )

    # De-duplicate exact candidates while retaining order.
    result: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()
    for issue in combined:
        key = issue_key(issue)
        if key not in seen:
            result.append(issue)
            seen.add(key)
    return result


def load_confirmed_issues(chapter_dir: Path) -> list[dict[str, Any]]:
    verified = chapter_dir / "verified_issues.json"
    if verified.exists():
        data = read_json(verified, [])
        if isinstance(data, list):
            return [item for item in data if isinstance(item, dict)]

    # issues.json contains raw Qwen output before stage 4, and confirmed output
    # only after verifier_report.json has been written.
    if (chapter_dir / "verifier_report.json").exists():
        data = read_json(chapter_dir / "issues.json", [])
        if isinstance(data, list):
            return [item for item in data if isinstance(item, dict)]
    return []


def load_verifier_decisions(chapter_dir: Path) -> list[dict[str, Any]]:
    report = read_json(chapter_dir / "verifier_report.json", {})
    decisions = report.get("decisions") or []
    if isinstance(decisions, list):
        return [item for item in decisions if isinstance(item, dict)]

    # Fallback for an in-progress verifier: read cached files.
    result: list[dict[str, Any]] = []
    verifier_dir = chapter_dir / "verifier"
    if verifier_dir.exists():
        for path in sorted(verifier_dir.glob("*.json"), key=lambda item: natural_key(item.name)):
            data = read_json(path, {})
            if not isinstance(data, dict):
                continue
            issue = data.get("issue") or {}
            verdict = data.get("result") or {}
            if isinstance(issue, dict) and isinstance(verdict, dict):
                result.append(
                    {
                        "issue_id": issue.get("issue_id"),
                        "pid": issue.get("pid"),
                        **verdict,
                        "issue": issue,
                        "attempts": data.get("attempts") or [],
                    }
                )
    return result


def load_repair_records(chapter_dir: Path) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    data = read_json(chapter_dir / "repair_records.json", [])
    if isinstance(data, list):
        result.extend(item for item in data if isinstance(item, dict))
    retries = read_json(chapter_dir / "repair_retry_records.json", [])
    if isinstance(retries, list):
        for item in retries:
            if not isinstance(item, dict):
                continue
            result.append({
                "pid": item.get("pid"),
                "action": "replace" if item.get("generated") else "keep",
                "accepted": bool(item.get("generated")),
                "reason": item.get("reason") or "retry candidate",
                "after": item.get("candidate") or "",
                "outcome": "retry_candidate_created" if item.get("generated") else "retry_failed",
                "round": item.get("round"),
            })
    if not result:
        repair_dir = chapter_dir / "repairs"
        if repair_dir.exists():
            for path in sorted(
                repair_dir.glob("batch_*.json"),
                key=lambda item: natural_key(item.name),
            ):
                batch = read_json(path, {})
                for record in batch.get("records") or []:
                    if isinstance(record, dict):
                        result.append(record)
    return result


def find_output_html(
    run_root: Path,
    manifest: dict[str, Any],
    chapter_dir: Path,
) -> Path | None:
    output_dir = run_root / "output"
    candidates: list[Path] = []

    chapter_name = str(manifest.get("chapter") or "")
    if chapter_name:
        candidates.append(output_dir / chapter_name)

    candidates.append(output_dir / f"{chapter_dir.name}.html")

    if output_dir.exists():
        candidates.extend(sorted(output_dir.glob("*.html")))

    for path in candidates:
        if path.exists():
            if path.stem == chapter_dir.name or chapter_name == path.name:
                return path

    return None


def determine_final_maps(
    run_root: Path,
    chapter_dir: Path,
    manifest: dict[str, Any],
    initial: dict[str, str],
) -> tuple[dict[str, str], dict[str, str], str]:
    output_path = find_output_html(run_root, manifest, chapter_dir)
    output_html = (
        parse_html_by_manifest(output_path, manifest)
        if output_path else {}
    )

    repaired_path = chapter_dir / "repaired_translations.json"
    if repaired_path.exists():
        final_text = read_json(repaired_path, initial)
        if not isinstance(final_text, dict):
            final_text = dict(initial)
        stage = "after_repair"
    else:
        final_text = dict(initial)
        stage = "awaiting_repair"

    # Final HTML is used for visual display after formatting; text comparison
    # remains based on repaired_translations.json so markup-only changes do not
    # look like semantic rewrites.
    final_html: dict[str, str] = {}
    for pid, text in final_text.items():
        final_html[pid] = output_html.get(pid) or esc(text)

    if output_path and output_path.exists():
        stage = "finalized_html"

    return final_text, final_html, stage


def decision_indexes(
    decisions: list[dict[str, Any]],
) -> tuple[dict[str, dict[str, Any]], dict[tuple[str, str, str], dict[str, Any]]]:
    by_id: dict[str, dict[str, Any]] = {}
    by_key: dict[tuple[str, str, str], dict[str, Any]] = {}
    for decision in decisions:
        issue = decision.get("issue") or {}
        issue_id = str(decision.get("issue_id") or issue.get("issue_id") or "")
        if issue_id:
            by_id[issue_id] = decision
        if isinstance(issue, dict):
            by_key[issue_key(issue)] = decision
    return by_id, by_key


def decision_for_issue(
    issue: dict[str, Any],
    by_id: dict[str, dict[str, Any]],
    by_key: dict[tuple[str, str, str], dict[str, Any]],
) -> dict[str, Any] | None:
    issue_id = str(issue.get("issue_id") or "")
    if issue_id and issue_id in by_id:
        return by_id[issue_id]
    return by_key.get(issue_key(issue))


def build_records(
    run_root: Path,
    chapter_dir: Path,
) -> tuple[list[ParagraphRecord], dict[str, Any]]:
    manifest = read_json(chapter_dir / "manifest.json", {})
    blocks = [
        item for item in (manifest.get("blocks") or [])
        if isinstance(item, dict) and item.get("pid")
    ]
    if not blocks:
        raise RuntimeError(f"{chapter_dir}: manifest.json не содержит blocks")

    initial = read_json(chapter_dir / "draft_translations.json", {})
    if not isinstance(initial, dict):
        initial = {}

    final_text_map, final_html_map, final_stage = determine_final_maps(
        run_root, chapter_dir, manifest, initial
    )

    raw_issues = load_raw_issues(chapter_dir)
    confirmed_issues = load_confirmed_issues(chapter_dir)
    decisions = load_verifier_decisions(chapter_dir)
    repairs = load_repair_records(chapter_dir)
    cfg = read_json(run_root / "config.full_pipeline.json", {})
    post_repair_report = read_json(chapter_dir / "post_repair_report.json", {})
    post_by_pid = {
        str(item.get("pid") or ""): item
        for item in (post_repair_report.get("decisions") or [])
        if isinstance(item, dict)
    }

    raw_by_pid: dict[str, list[dict[str, Any]]] = defaultdict(list)
    confirmed_by_pid: dict[str, list[dict[str, Any]]] = defaultdict(list)
    decisions_by_pid: dict[str, list[dict[str, Any]]] = defaultdict(list)
    repairs_by_pid: dict[str, list[dict[str, Any]]] = defaultdict(list)

    for issue in raw_issues:
        raw_by_pid[str(issue.get("pid") or "")].append(issue)
    for issue in confirmed_issues:
        confirmed_by_pid[str(issue.get("pid") or "")].append(issue)
    for decision in decisions:
        pid = str(decision.get("pid") or (decision.get("issue") or {}).get("pid") or "")
        decisions_by_pid[pid].append(decision)
    for repair in repairs:
        repairs_by_pid[str(repair.get("pid") or "")].append(repair)

    chunk_by_pid = chunk_map(manifest)
    records: list[ParagraphRecord] = []

    for fallback_index, block in enumerate(
        sorted(blocks, key=lambda item: int(item.get("index", 0)))
    ):
        pid = str(block.get("pid"))
        index = int(block.get("index", fallback_index))
        source_text = str(block.get("source_text") or block.get("text") or "")
        initial_text = str(initial.get(pid) or "")
        final_text = str(final_text_map.get(pid) or initial_text)
        changed = initial_text != final_text
        similarity = difflib.SequenceMatcher(
            None, initial_text, final_text, autojunk=False
        ).ratio() if initial_text or final_text else 1.0
        ratio = 1.0 - similarity

        verifier_for_pid = decisions_by_pid.get(pid, [])
        verdicts = Counter(
            str(
                item.get("decision")
                or {"confirm": "repair", "reject": "keep"}.get(
                    str(item.get("verdict") or "").casefold(),
                    item.get("verdict") or "",
                )
            ).casefold()
            for item in verifier_for_pid
        )

        repair_for_pid = repairs_by_pid.get(pid, [])
        accepted_record = next(
            (
                item for item in repair_for_pid
                if item.get("action") == "replace" and item.get("accepted") is True
            ),
            None,
        )
        keep_record = next(
            (
                item for item in repair_for_pid
                if item.get("action") == "keep"
            ),
            None,
        )
        failed_replace = next(
            (
                item for item in repair_for_pid
                if item.get("action") == "replace" and not item.get("accepted")
            ),
            None,
        )

        post_decision = post_by_pid.get(pid, {})
        post_verdict = str(post_decision.get("verdict") or "")
        post_reason = str(post_decision.get("reason") or "")
        post_action = str(post_decision.get("action") or "")

        if accepted_record:
            if post_action == "revert_to_draft":
                repair_action = "replace_reverted"
                repair_accepted = False
            elif post_action == "keep_repair":
                repair_action = "replace_verified"
                repair_accepted = True
            else:
                repair_action = "replace"
                repair_accepted = True
            repair_reason = str(accepted_record.get("reason") or "")
            validation_errors: list[str] = []
        elif keep_record:
            repair_action = "keep"
            repair_accepted = False
            repair_reason = str(keep_record.get("reason") or "")
            validation_errors = []
        elif failed_replace:
            repair_action = "replace_rejected"
            repair_accepted = False
            repair_reason = str(failed_replace.get("reason") or "")
            validation_errors = [
                str(item)
                for item in (failed_replace.get("validation_errors") or [])
            ]
        elif confirmed_by_pid.get(pid):
            selected_for_repair = any(
                issue_selected_for_repair(issue, cfg)
                for issue in confirmed_by_pid.get(pid, [])
            )
            if selected_for_repair and not (chapter_dir / "repaired_translations.json").exists():
                repair_action = "pending"
            else:
                repair_action = "not_selected"
            repair_accepted = False
            repair_reason = ""
            validation_errors = []
        else:
            repair_action = "none"
            repair_accepted = False
            repair_reason = ""
            validation_errors = []

        records.append(
            ParagraphRecord(
                pid=pid,
                index=index,
                tag=str(block.get("tag") or "p"),
                chunk_id=chunk_by_pid.get(pid, "unknown"),
                source_text=source_text,
                initial_text=initial_text,
                final_text=final_text,
                final_stage=final_stage,
                text_changed=changed,
                similarity=round(similarity, 6),
                change_ratio=round(ratio, 6),
                severity=severity_from_ratio(changed, ratio),
                initial_words=count_words(initial_text),
                final_words=count_words(final_text),
                word_delta=count_words(final_text) - count_words(initial_text),
                initial_chars=len(initial_text),
                final_chars=len(final_text),
                char_delta=len(final_text) - len(initial_text),
                diff_html=word_diff_html(initial_text, final_text),
                final_html=final_html_map.get(pid) or esc(final_text),
                raw_issue_count=len(raw_by_pid.get(pid, [])),
                confirmed_issue_count=len(confirmed_by_pid.get(pid, [])),
                rejected_issue_count=verdicts["reject"],
                uncertain_issue_count=verdicts["uncertain"],
                repair_action=repair_action,
                repair_accepted=repair_accepted,
                repair_reason=repair_reason,
                repair_validation_errors=validation_errors,
                post_repair_verdict=post_verdict,
                post_repair_reason=post_reason,
                post_repair_action=post_action,
                raw_issues=raw_by_pid.get(pid, []),
                verifier_decisions=verifier_for_pid,
                confirmed_issues=confirmed_by_pid.get(pid, []),
                repair_records=repair_for_pid,
            )
        )

    metadata = {
        "manifest": manifest,
        "raw_issues": raw_issues,
        "confirmed_issues": confirmed_issues,
        "verifier_decisions": decisions,
        "repair_records": repairs,
        "post_repair_report": post_repair_report,
        "final_stage": final_stage,
        "output_html": str(find_output_html(run_root, manifest, chapter_dir) or ""),
    }
    return records, metadata


def build_statistics(
    chapter_dir: Path,
    records: list[ParagraphRecord],
    metadata: dict[str, Any],
) -> dict[str, Any]:
    decisions = metadata["verifier_decisions"]
    verdicts = Counter(
        str(item.get("verdict") or "").casefold()
        for item in decisions
    )

    changed = [item for item in records if item.text_changed]
    reviewed = [
        item for item in records
        if item.raw_issue_count or item.verifier_decisions or item.confirmed_issue_count
    ]
    repair_selected = [
        item for item in records if item.repair_action not in {"none", "not_selected"}
    ]
    accepted_repairs = [
        item for item in records if item.repair_accepted
    ]
    pending_repairs = [
        item for item in records if item.repair_action == "pending"
    ]

    base_chars = sum(
        max(item.initial_chars, item.final_chars)
        for item in records
        if max(item.initial_chars, item.final_chars) > 0
    )
    weighted_changes = sum(
        max(item.initial_chars, item.final_chars) * item.change_ratio
        for item in changed
    )

    return {
        "script_version": VERSION,
        "chapter": chapter_dir.name,
        "pipeline_stage": metadata["final_stage"],
        "total_paragraphs": len(records),
        "initial_ready": sum(bool(item.initial_text) for item in records),
        "raw_issue_count": len(metadata["raw_issues"]),
        "confirmed_issue_count": len(metadata["confirmed_issues"]),
        "verifier_decision_count": len(decisions),
        "verifier_confirm": verdicts["confirm"],
        "verifier_reject": verdicts["reject"],
        "verifier_uncertain": verdicts["uncertain"],
        "paragraphs_reviewed": len(reviewed),
        "paragraphs_with_confirmed_issues": sum(
            bool(item.confirmed_issue_count) for item in records
        ),
        "repair_selected_paragraphs": len(repair_selected),
        "accepted_repair_paragraphs": len(accepted_repairs),
        "post_repair_reverted": sum(
            item.repair_action == "replace_reverted" for item in records
        ),
        "pending_repair_paragraphs": len(pending_repairs),
        "changed_paragraphs": len(changed),
        "changed_percent": round(
            100.0 * len(changed) / len(records), 2
        ) if records else 0.0,
        "overall_weighted_change_percent": round(
            100.0 * weighted_changes / base_chars, 2
        ) if base_chars else 0.0,
        "initial_words": sum(item.initial_words for item in records),
        "final_words": sum(item.final_words for item in records),
        "word_delta": sum(item.word_delta for item in records),
        "output_html": metadata["output_html"],
    }


def issue_badge(issue: dict[str, Any]) -> str:
    severity = str(issue.get("severity") or "unknown")
    category = str(issue.get("category") or "unknown")
    source = str(issue.get("source") or "")
    deterministic = bool(issue.get("deterministic"))
    origin = "deterministic" if deterministic else (source or "Qwen")
    return (
        f'<span class="badge severity-{esc(severity)}">{esc(severity)}</span>'
        f'<span class="badge">{esc(category)}</span>'
        f'<span class="badge badge-origin">{esc(origin)}</span>'
    )


def render_issue_block(
    issue: dict[str, Any],
    decision: dict[str, Any] | None,
    confirmed_keys: set[tuple[str, str, str]],
) -> str:
    key = issue_key(issue)
    if decision:
        verdict = str(decision.get("verdict") or "unknown")
        confidence = str(decision.get("confidence") or "")
        reason = str(decision.get("reason") or "")
    elif key in confirmed_keys:
        verdict = "confirm"
        confidence = "deterministic"
        reason = "Сохранено без модельной проверки."
    else:
        verdict = "pending"
        confidence = ""
        reason = ""

    verdict_label = {
        "confirm": "ПОДТВЕРЖДЕНО",
        "reject": "ОТКЛОНЕНО",
        "uncertain": "НЕУВЕРЕННО",
        "pending": "ОЖИДАЕТ VERIFIER",
    }.get(verdict, verdict.upper())

    suggested = str(issue.get("suggested_text") or "")
    repair_instruction = str(issue.get("repair_instruction") or "")

    return f"""
<div class="issue issue-{esc(verdict)}">
  <div class="issue-head">
    <div>{issue_badge(issue)}</div>
    <span class="verdict verdict-{esc(verdict)}">{esc(verdict_label)}</span>
  </div>
  <div class="issue-problem">{esc(issue.get("problem"))}</div>
  {f'<div class="instruction"><strong>Инструкция:</strong> {esc(repair_instruction)}</div>' if repair_instruction else ''}
  {f'<details><summary>Вариант Qwen</summary><div class="suggested">{esc(suggested)}</div></details>' if suggested else ''}
  {f'<div class="verifier-reason"><strong>Gemma:</strong> {esc(reason)} {f"<span class=confidence>{esc(confidence)}</span>" if confidence else ""}</div>' if reason else ''}
</div>
"""


def render_repair_block(record: ParagraphRecord) -> str:
    if record.repair_action == "none":
        return '<div class="repair repair-none">Repair не требовался.</div>'
    if record.repair_action == "not_selected":
        return (
            '<div class="repair repair-none">'
            'Замечание подтверждено, но не входит в настроенные категории auto-repair.'
            '</div>'
        )
    if record.repair_action == "pending":
        return (
            '<div class="repair repair-pending">'
            'Ошибка подтверждена, но этап repair ещё не завершён.'
            '</div>'
        )
    if record.repair_action == "keep":
        return (
            '<div class="repair repair-keep">'
            '<strong>Gemma repair оставила текст без изменения.</strong>'
            f'<div>{esc(record.repair_reason)}</div>'
            '</div>'
        )
    if record.repair_action == "replace_rejected":
        errors = "".join(
            f"<li>{esc(item)}</li>"
            for item in record.repair_validation_errors
        )
        return (
            '<div class="repair repair-rejected">'
            '<strong>Предложенная замена отклонена валидатором.</strong>'
            f'<div>{esc(record.repair_reason)}</div>'
            f'<ul>{errors}</ul>'
            '</div>'
        )
    if record.repair_action == "replace_reverted":
        return (
            '<div class="repair repair-rejected">'
            '<strong>Замена была создана, но post-repair verifier вернул исходный текст.</strong>'
            f'<div>{esc(record.repair_reason)}</div>'
            f'<div><strong>Verifier:</strong> {esc(record.post_repair_reason)}</div>'
            '</div>'
        )
    verifier_line = (
        f'<div><strong>Post-repair verifier:</strong> {esc(record.post_repair_reason)}</div>'
        if record.post_repair_reason else ''
    )
    return (
        '<div class="repair repair-accepted">'
        '<strong>Замена принята.</strong>'
        f'<div>{esc(record.repair_reason)}</div>'
        f'{verifier_line}'
        '</div>'
    )


def render_card(
    record: ParagraphRecord,
    decisions_by_id: dict[str, dict[str, Any]],
    decisions_by_key: dict[tuple[str, str, str], dict[str, Any]],
    confirmed_keys: set[tuple[str, str, str]],
) -> str:
    issues_html = "".join(
        render_issue_block(
            issue,
            decision_for_issue(issue, decisions_by_id, decisions_by_key),
            confirmed_keys,
        )
        for issue in record.raw_issues
    )
    if not issues_html:
        issues_html = '<div class="empty-note">Замечаний к абзацу нет.</div>'

    final_heading = {
        "awaiting_repair": "Текущий текст — repair ещё не выполнен",
        "after_repair": "После repair",
        "finalized_html": "Финальный текст",
    }.get(record.final_stage, "Итоговый текст")

    change_percent = round(record.change_ratio * 100, 1)
    review_class = (
        "has-confirmed" if record.confirmed_issue_count
        else "has-review" if record.raw_issue_count
        else "no-review"
    )

    return f"""
<section class="change-card severity-{esc(record.severity)} {review_class}" id="{esc(record.pid)}">
  <header>
    <div class="identity">
      <strong>{esc(record.pid)}</strong>
      <span class="tag">&lt;{esc(record.tag)}&gt;</span>
      <span class="chunk">{esc(record.chunk_id)}</span>
      {f'<span class="badge confirmed-count">{record.confirmed_issue_count} confirmed</span>' if record.confirmed_issue_count else ''}
    </div>
    <div class="metrics">
      Изменение текста: <strong>{change_percent}%</strong>;
      слова: {record.initial_words} → {record.final_words} ({record.word_delta:+d});
      символы: {record.char_delta:+d}
    </div>
  </header>

  <details class="source-details" open>
    <summary>Английский оригинал</summary>
    <div class="source">{esc(record.source_text)}</div>
  </details>

  <div class="columns">
    <article>
      <h3>Изначальный перевод Gemma</h3>
      <div class="prose">{esc(record.initial_text) or '<em>Не готов</em>'}</div>
    </article>
    <article>
      <h3>{esc(final_heading)}</h3>
      <div class="prose">{record.final_html or '<em>Не готов</em>'}</div>
    </article>
  </div>

  <div class="diff">
    <h3>Разница между первым и итоговым текстом</h3>
    <div class="diff-text">
      {record.diff_html if record.text_changed else '<span class="unchanged">Текст не изменился.</span>'}
    </div>
  </div>

  <details class="review-details" {'open' if record.raw_issue_count else ''}>
    <summary>
      Qwen → Gemma verifier:
      {record.raw_issue_count} кандидатов,
      {record.confirmed_issue_count} подтверждено,
      {record.rejected_issue_count} отклонено
    </summary>
    <div class="issues">{issues_html}</div>
  </details>

  <div class="repair-wrap">
    <h3>Решение этапа repair</h3>
    {render_repair_block(record)}
  </div>
</section>
"""


def render_report(
    chapter_dir: Path,
    records: list[ParagraphRecord],
    stats: dict[str, Any],
    metadata: dict[str, Any],
    mode: str,
) -> str:
    if mode == "changed":
        shown = [item for item in records if item.text_changed]
        scope = "Только реально изменённые абзацы"
    elif mode == "reviewed":
        shown = [
            item for item in records
            if item.raw_issue_count
            or item.confirmed_issue_count
            or item.repair_action not in {"none"}
        ]
        scope = "Все абзацы, затронутые аудитом или repair"
    elif mode == "confirmed":
        shown = [
            item for item in records
            if item.confirmed_issue_count
            or item.repair_action not in {"none"}
        ]
        scope = "Только подтверждённые замечания и repair"
    else:
        shown = records
        scope = "Все абзацы"

    decisions_by_id, decisions_by_key = decision_indexes(
        metadata["verifier_decisions"]
    )
    confirmed_keys = {
        issue_key(issue)
        for issue in metadata["confirmed_issues"]
    }

    cards = "".join(
        render_card(
            record,
            decisions_by_id,
            decisions_by_key,
            confirmed_keys,
        )
        for record in shown
    )

    stage_label = {
        "awaiting_repair": (
            "Verifier завершён или выполняется, но фактический repair-текст "
            "ещё не создан. Правая колонка пока совпадает с первоначальной."
        ),
        "after_repair": (
            "Показан фактический текст после repair. Форматирование может "
            "ещё не быть завершено."
        ),
        "finalized_html": (
            "Показан финальный текст после repair и восстановления форматирования."
        ),
    }.get(stats["pipeline_stage"], stats["pipeline_stage"])

    return f"""<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{esc(chapter_dir.name)} — отчёт Qwen/Gemma</title>
<style>
:root{{
  --bg:#f3f5f8;--card:#fff;--line:#d9dee8;--muted:#667085;
  --text:#182230;--green:#067647;--red:#b42318;--amber:#b54708;
  --blue:#175cd3;--purple:#6938ef;
}}
*{{box-sizing:border-box}}
body{{margin:0;background:var(--bg);color:var(--text);font-family:system-ui,-apple-system,"Segoe UI",sans-serif}}
main{{max-width:1500px;margin:auto;padding:24px}}
h1{{margin:0 0 6px}}
.subtitle{{color:var(--muted);margin-bottom:14px}}
.stage-note{{background:#eef4ff;border:1px solid #b2ccff;padding:12px 15px;border-radius:10px;line-height:1.5}}
.toolbar{{display:flex;gap:9px;flex-wrap:wrap;margin:16px 0}}
.toolbar a{{background:#fff;border:1px solid var(--line);padding:8px 11px;border-radius:8px;color:#344054;text-decoration:none}}
.stats{{display:grid;grid-template-columns:repeat(auto-fit,minmax(165px,1fr));gap:10px;margin:18px 0}}
.stat{{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:13px}}
.stat strong{{display:block;font-size:1.45rem}}
.change-card{{background:var(--card);border:1px solid var(--line);border-left:6px solid #98a2b3;border-radius:11px;margin:18px 0;overflow:hidden}}
.change-card.has-confirmed{{box-shadow:0 0 0 1px #fec84b}}
.severity-minor{{border-left-color:#12b76a}}
.severity-moderate{{border-left-color:#f79009}}
.severity-major{{border-left-color:#f04438}}
.severity-unchanged{{border-left-color:#98a2b3}}
.change-card>header{{display:flex;justify-content:space-between;gap:12px;flex-wrap:wrap;padding:12px 16px;background:#fafafa;border-bottom:1px solid var(--line)}}
.identity{{display:flex;gap:7px;align-items:center;flex-wrap:wrap}}
.tag,.chunk,.badge{{display:inline-block;padding:2px 7px;background:#eef2f6;border-radius:12px;font-size:.79rem}}
.confirmed-count{{background:#dcfae6;color:#05603a}}
.metrics{{color:var(--muted);font-size:.9rem}}
.source-details,.review-details{{padding:10px 16px;border-bottom:1px solid var(--line)}}
.source{{font-family:Georgia,serif;line-height:1.55;padding-top:8px}}
.columns{{display:grid;grid-template-columns:1fr 1fr}}
.columns article{{padding:14px 18px;min-width:0}}
.columns article+article{{border-left:1px solid var(--line)}}
.columns h3,.diff h3,.repair-wrap h3{{font-size:.82rem;text-transform:uppercase;letter-spacing:.04em;color:var(--muted)}}
.prose,.diff-text{{font-family:Georgia,serif;line-height:1.65;overflow-wrap:anywhere}}
.prose p,.prose [data-pid]{{margin:0}}
.diff{{border-top:1px solid var(--line);padding:12px 18px;background:#fcfcfd}}
del{{background:#fee4e2;color:#912018;text-decoration:line-through;padding:1px 2px}}
ins{{background:#dcfae6;color:#05603a;text-decoration:none;padding:1px 2px}}
.unchanged,.empty-note{{color:var(--muted);font-style:italic}}
.issues{{padding:10px 0}}
.issue{{border:1px solid var(--line);border-left:5px solid #98a2b3;border-radius:8px;padding:11px;margin:9px 0;background:#fff}}
.issue-confirm{{border-left-color:#12b76a;background:#f6fef9}}
.issue-reject{{border-left-color:#f04438;background:#fffbfa;opacity:.83}}
.issue-uncertain{{border-left-color:#f79009;background:#fffcf5}}
.issue-pending{{border-left-color:#667085}}
.issue-head{{display:flex;justify-content:space-between;gap:10px;align-items:center;flex-wrap:wrap}}
.severity-critical,.severity-major{{background:#fee4e2;color:#b42318}}
.severity-minor{{background:#dcfae6;color:#067647}}
.badge-origin{{background:#eef4ff;color:#175cd3}}
.verdict{{font-weight:700;font-size:.78rem;padding:3px 8px;border-radius:12px}}
.verdict-confirm{{background:#dcfae6;color:#067647}}
.verdict-reject{{background:#fee4e2;color:#b42318}}
.verdict-uncertain{{background:#fef0c7;color:#b54708}}
.verdict-pending{{background:#eaecf0;color:#475467}}
.issue-problem{{margin:9px 0;line-height:1.5}}
.instruction,.verifier-reason,.suggested{{font-size:.92rem;line-height:1.5;margin-top:7px}}
.confidence{{color:var(--muted);font-size:.82rem}}
.repair-wrap{{border-top:1px solid var(--line);padding:12px 18px}}
.repair{{padding:10px 12px;border-radius:8px}}
.repair-none{{background:#f2f4f7;color:#475467}}
.repair-pending{{background:#fef0c7;color:#93370d}}
.repair-keep{{background:#eef4ff;color:#1849a9}}
.repair-rejected{{background:#fee4e2;color:#912018}}
.repair-accepted{{background:#dcfae6;color:#05603a}}
.empty{{background:#fff;border:1px solid var(--line);padding:24px;border-radius:10px}}
@media(max-width:900px){{
  .columns{{grid-template-columns:1fr}}
  .columns article+article{{border-left:0;border-top:1px solid var(--line)}}
}}
</style>
</head>
<body>
<main>
<h1>{esc(chapter_dir.name)}</h1>
<div class="subtitle">{esc(scope)}. Отчёт версии {VERSION}.</div>
<div class="stage-note">{esc(stage_label)}</div>
<div class="toolbar">
  <a href="index.html">Все абзацы</a>
  <a href="reviewed.html">Затронутые аудитом</a>
  <a href="confirmed.html">Подтверждённые</a>
  <a href="changed.html">Реально изменённые</a>
  <a href="initial_full.html">Первый перевод целиком</a>
  <a href="final_full.html">Итоговый текст целиком</a>
  <a href="statistics.json">JSON</a>
  <a href="paragraph_changes.csv">CSV</a>
</div>
<div class="stats">
  <div class="stat"><strong>{stats['raw_issue_count']}</strong>сырых кандидатов</div>
  <div class="stat"><strong>{stats['confirmed_issue_count']}</strong>подтверждено</div>
  <div class="stat"><strong>{stats['verifier_reject']}</strong>отклонено verifier</div>
  <div class="stat"><strong>{stats['accepted_repair_paragraphs']}</strong>принято замен</div>
  <div class="stat"><strong>{stats['changed_paragraphs']}</strong>абзацев изменено</div>
  <div class="stat"><strong>{stats['overall_weighted_change_percent']}%</strong>взвешенный объём правок</div>
  <div class="stat"><strong>{stats['word_delta']:+d}</strong>изменение числа слов</div>
</div>
{cards if cards else '<div class="empty">Подходящих абзацев пока нет.</div>'}
</main>
</body>
</html>"""


def replace_block_contents(original: Tag, replacement_html: str) -> None:
    original.clear()
    fragment = BeautifulSoup(replacement_html or "", "html.parser")
    for child in list(fragment.contents):
        original.append(copy.deepcopy(child))


def assemble_full_html(
    source_path: Path,
    manifest: dict[str, Any],
    text_map: dict[str, str],
    html_map: dict[str, str],
    title: str,
    banner: str,
) -> str:
    soup = BeautifulSoup(source_path.read_text(encoding="utf-8"), "html.parser")
    for block in manifest.get("blocks") or []:
        if not isinstance(block, dict):
            continue
        pid = str(block.get("pid") or "")
        tag = soup.find(attrs={"data-pid": pid})
        if not isinstance(tag, Tag):
            continue
        replacement = html_map.get(pid)
        if replacement is None:
            replacement = esc(text_map.get(pid, f"[{pid}: перевод не готов]"))
        replace_block_contents(tag, replacement)

    if soup.html:
        soup.html["lang"] = "ru"

    if soup.head is None:
        head = soup.new_tag("head")
        if soup.html:
            soup.html.insert(0, head)
    else:
        head = soup.head

    title_tag = head.find("title")
    if title_tag is None:
        title_tag = soup.new_tag("title")
        head.append(title_tag)
    title_tag.string = title

    style = soup.new_tag("style")
    style.string = """
body{font-family:Georgia,serif;max-width:900px;margin:2em auto;line-height:1.65;padding:0 1em}
.review-banner{font-family:system-ui,sans-serif;background:#eef4ff;border:1px solid #b2ccff;padding:12px 16px;border-radius:9px;margin-bottom:24px}
"""
    head.append(style)

    if soup.body:
        banner_tag = soup.new_tag("div")
        banner_tag["class"] = "review-banner"
        banner_tag.string = banner
        soup.body.insert(0, banner_tag)

    return str(soup)


def write_csv(path: Path, records: list[ParagraphRecord]) -> None:
    fields = [
        "pid",
        "index",
        "chunk_id",
        "tag",
        "final_stage",
        "text_changed",
        "severity",
        "similarity",
        "change_ratio",
        "initial_words",
        "final_words",
        "word_delta",
        "initial_chars",
        "final_chars",
        "char_delta",
        "raw_issue_count",
        "confirmed_issue_count",
        "rejected_issue_count",
        "uncertain_issue_count",
        "repair_action",
        "repair_accepted",
        "repair_reason",
        "post_repair_verdict",
        "post_repair_action",
        "post_repair_reason",
        "source_text",
        "initial_text",
        "final_text",
    ]
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8-sig", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=fields)
        writer.writeheader()
        for record in records:
            data = asdict(record)
            writer.writerow({field: data[field] for field in fields})
        file.flush()
        os.fsync(file.fileno())
    os.replace(temporary, path)


def create_chapter_report(
    run_root: Path,
    chapter_dir: Path,
    output_dir: Path,
) -> dict[str, Any]:
    records, metadata = build_records(run_root, chapter_dir)
    stats = build_statistics(chapter_dir, records, metadata)

    output_dir.mkdir(parents=True, exist_ok=True)
    write_json_atomic(output_dir / "statistics.json", stats)
    write_csv(output_dir / "paragraph_changes.csv", records)

    for mode, filename in (
        ("all", "index.html"),
        ("reviewed", "reviewed.html"),
        ("confirmed", "confirmed.html"),
        ("changed", "changed.html"),
    ):
        write_text_atomic(
            output_dir / filename,
            render_report(chapter_dir, records, stats, metadata, mode),
        )

    manifest = metadata["manifest"]
    source_path = chapter_dir / "source.normalized.html"
    initial_map = {record.pid: record.initial_text for record in records}
    final_map = {record.pid: record.final_text for record in records}
    final_html_map = {record.pid: record.final_html for record in records}

    write_text_atomic(
        output_dir / "initial_full.html",
        assemble_full_html(
            source_path,
            manifest,
            initial_map,
            {},
            f"{chapter_dir.name}: первый перевод",
            "Первый перевод Gemma до Qwen audit, Gemma verifier и repair.",
        ),
    )
    write_text_atomic(
        output_dir / "final_full.html",
        assemble_full_html(
            source_path,
            manifest,
            final_map,
            final_html_map,
            f"{chapter_dir.name}: итоговый перевод",
            {
                "awaiting_repair": (
                    "Repair ещё не завершён. Этот текст пока совпадает с первым переводом."
                ),
                "after_repair": (
                    "Текст после repair; форматирование ещё может быть не завершено."
                ),
                "finalized_html": (
                    "Финальный текст после audit, verifier, repair и formatting."
                ),
            }.get(metadata["final_stage"], metadata["final_stage"]),
        ),
    )

    return {
        "chapter": chapter_dir.name,
        "output_dir": str(output_dir),
        "index": str(output_dir / "index.html"),
        "statistics": stats,
    }


def discover_chapters(run_root: Path) -> list[Path]:
    work_dir = run_root / "work"
    if not work_dir.exists():
        return []
    return sorted(
        [
            path for path in work_dir.iterdir()
            if path.is_dir() and (path / "manifest.json").exists()
        ],
        key=lambda path: natural_key(path.name),
    )


def resolve_run_root(args: argparse.Namespace) -> Path:
    project_root = Path(args.project_root).resolve()

    if args.run_root:
        run_root = Path(args.run_root).resolve()
        if not run_root.exists():
            raise FileNotFoundError(f"Не найден run root: {run_root}")
        return run_root

    if args.start is not None:
        end = args.end if args.end is not None else args.start
        run_root = (
            project_root
            / "pipeline_runs"
            / f"chapter_{args.start}_to_{end}"
        )
        if not run_root.exists():
            raise FileNotFoundError(f"Не найден run root: {run_root}")
        return run_root.resolve()

    pipeline_runs = project_root / "pipeline_runs"
    candidates = [
        path for path in pipeline_runs.iterdir()
        if path.is_dir() and (path / "config.full_pipeline.json").exists()
    ] if pipeline_runs.exists() else []
    if not candidates:
        raise FileNotFoundError(
            f"В {pipeline_runs} не найдено запусков pipeline."
        )
    return max(candidates, key=lambda path: path.stat().st_mtime).resolve()


def render_run_index(
    run_root: Path,
    reports: list[dict[str, Any]],
) -> str:
    rows = []
    for report in reports:
        stats = report["statistics"]
        relative = Path(report["output_dir"]).name
        rows.append(
            f"""
<tr>
  <td><a href="{esc(relative)}/index.html">{esc(report['chapter'])}</a></td>
  <td>{stats['raw_issue_count']}</td>
  <td>{stats['confirmed_issue_count']}</td>
  <td>{stats['verifier_reject']}</td>
  <td>{stats['accepted_repair_paragraphs']}</td>
  <td>{stats['changed_paragraphs']}</td>
  <td>{esc(stats['pipeline_stage'])}</td>
</tr>
"""
        )
    return f"""<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Pact pipeline review report</title>
<style>
body{{font-family:system-ui,-apple-system,"Segoe UI",sans-serif;max-width:1200px;margin:2em auto;padding:0 1em;background:#f5f6f8;color:#182230}}
h1{{margin-bottom:6px}}
.subtitle{{color:#667085;margin-bottom:20px}}
table{{width:100%;border-collapse:collapse;background:#fff;border:1px solid #d9dee8}}
th,td{{padding:10px 12px;border-bottom:1px solid #eaecf0;text-align:left}}
th{{background:#f9fafb}}
a{{color:#175cd3}}
</style>
</head>
<body>
<h1>Pact pipeline review report</h1>
<div class="subtitle">{esc(run_root)}</div>
<table>
<thead>
<tr>
<th>Глава</th><th>Qwen</th><th>Confirmed</th><th>Rejected</th>
<th>Accepted repair</th><th>Changed</th><th>Stage</th>
</tr>
</thead>
<tbody>{''.join(rows)}</tbody>
</table>
</body>
</html>"""


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Красивое сравнение первого перевода, Qwen audit, "
            "Gemma verifier и итогового repair."
        )
    )
    parser.add_argument(
        "--project-root",
        default=r"D:\pact\pact_translator_v3",
        help="Корень проекта Pact Translator",
    )
    parser.add_argument("--run-root", help="Путь к pipeline run")
    parser.add_argument("--start", type=int, help="Начальный номер главы")
    parser.add_argument("--end", type=int, help="Конечный номер главы")
    parser.add_argument(
        "--output-dir",
        help=(
            "Папка отчёта. По умолчанию "
            "<run-root>/review_comparison"
        ),
    )
    parser.add_argument(
        "--open",
        action="store_true",
        help="Открыть общий index.html в браузере",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {VERSION}",
    )
    return parser


def main() -> int:
    args = build_parser().parse_args()

    try:
        run_root = resolve_run_root(args)
        chapter_dirs = discover_chapters(run_root)
        if not chapter_dirs:
            raise RuntimeError(f"В {run_root / 'work'} не найдены главы.")

        output_root = (
            Path(args.output_dir).resolve()
            if args.output_dir
            else run_root / "review_comparison"
        )
        output_root.mkdir(parents=True, exist_ok=True)

        reports: list[dict[str, Any]] = []
        for chapter_dir in chapter_dirs:
            report = create_chapter_report(
                run_root,
                chapter_dir,
                output_root / chapter_dir.name,
            )
            reports.append(report)
            stats = report["statistics"]
            print(
                f"{chapter_dir.name}: "
                f"Qwen={stats['raw_issue_count']}, "
                f"confirmed={stats['confirmed_issue_count']}, "
                f"rejected={stats['verifier_reject']}, "
                f"changed={stats['changed_paragraphs']}, "
                f"stage={stats['pipeline_stage']}"
            )
            print(f"  {report['index']}")

        index_path = output_root / "index.html"
        write_text_atomic(
            index_path,
            render_run_index(run_root, reports),
        )

        print(f"\nОбщий отчёт: {index_path}")

        if args.open:
            webbrowser.open(index_path.resolve().as_uri())

        return 0
    except Exception as exc:
        print(f"Ошибка: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
