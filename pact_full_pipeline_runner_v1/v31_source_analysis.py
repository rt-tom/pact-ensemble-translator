#!/usr/bin/env python3
"""Qwen source-only scene analysis for Pact v3.1.1."""
from __future__ import annotations

import argparse
import logging
from pathlib import Path
from typing import Any

from v31_common import (
    VERSION, add_common_args, api_client, batched, bible_prompt,
    complete_json, glossary_prompt, load_cfg, load_manifest, load_runtime,
    norm, read_json, render_pairs, selected_chapters, setup_logging,
    stage_cfg, write_json,
)

DEFAULT_STAGE = {
    "temperature": 0.0,
    "top_p": 1.0,
    "top_k": 64,
    "enable_thinking": False,
    "max_tokens": 2400,
    "attempts": 3,
    "batch_pids": 4,
    "context_before": 2,
    "context_after": 2,
}

MAX_IDIOMS = 2
MAX_REFERENTS = 2
MAX_INVARIANTS = 3
MAX_FORBIDDEN_ADDITIONS = 2
MAX_ADDRESS_UPDATES = 4


def blank_stats() -> dict[str, int]:
    return {
        "successful_batches": 0,
        "split_batches": 0,
        "model_attempts": 0,
        "failed_attempts": 0,
        "truncated_json": 0,
        "dropped_address_updates": 0,
        "dropped_optional_entries": 0,
        "unexpected_pid_entries": 0,
        "duplicate_pid_entries": 0,
    }


def merge_stats(*items: dict[str, Any]) -> dict[str, int]:
    result = blank_stats()
    for item in items:
        for key in result:
            result[key] += int((item or {}).get(key, 0) or 0)
    return result


def is_truncated_attempt(attempt: dict[str, Any]) -> bool:
    finish = norm(attempt.get("finish_reason")).casefold()
    error = norm(attempt.get("error")).casefold()
    return finish in {"length", "max_tokens"} or "invalid json response" in error


def attempt_stats(attempts: list[dict[str, Any]]) -> dict[str, int]:
    result = blank_stats()
    result["model_attempts"] = len(attempts)
    result["failed_attempts"] = sum(1 for item in attempts if not item.get("ok"))
    result["truncated_json"] = sum(
        1 for item in attempts if not item.get("ok") and is_truncated_attempt(item)
    )
    return result


def messages_for_batch(runtime, cfg: dict[str, Any], work: Path, blocks, block_map, pids, book_ledger):
    positions = {str(block["pid"]): i for i, block in enumerate(blocks)}
    first = positions[pids[0]]
    last = positions[pids[-1]]
    stage = stage_cfg(cfg, "source_analysis", DEFAULT_STAGE)
    before = [str(b["pid"]) for b in blocks[max(0, first-int(stage["context_before"])):first]]
    after = [str(b["pid"]) for b in blocks[last+1:last+1+int(stage["context_after"])]]
    all_pids = before + list(pids) + after
    source_text = "\n".join(norm(block_map[pid].get("source_text")) for pid in all_pids)
    chapter_bible = read_json(work / "chapter_bible.json", {})

    system = f"""Ты анализируешь английский оригинал романа Pact ДО перевода.
Зафиксируй смысловые опоры, но не предлагай русский перевод.

Для КАЖДОГО PID из TARGET_PIDS верни ровно одну запись в results. Единственное
абсолютно обязательное поле каждой записи — правильный pid; остальные поля
заполняй кратко и только когда они полезны. Не выдумывай говорящего, адресата,
референта или обращение.

ОГРАНИЧЕНИЯ ДЛЯ КОМПАКТНОГО JSON:
- plain_meaning: одно короткое предложение, максимум 35 слов;
- idioms: максимум {MAX_IDIOMS};
- referents: максимум {MAX_REFERENTS};
- invariants: максимум {MAX_INVARIANTS};
- forbidden_additions: максимум {MAX_FORBIDDEN_ADDITIONS};
- не выводи пустые массивы, null-поля и пустые строки;
- speaker/addressee добавляй только когда они действительно известны;
- address_updates добавляй только при известной паре speaker+addressee и
  expected_register строго \"ты\" или \"вы\"; unknown не выводи.

Отмечай только значимое:
- буквальный смысл и причинно-временные связи;
- фразовые глаголы, идиомы и контекстное значение многозначных слов;
- субъект, объект, отрицание, модальность и неочевидные референты;
- говорящего и адресата прямой речи;
- детали, которые нельзя добавлять или терять.

Верни только JSON без markdown и комментариев. Минимальная схема:
{{
  "results":[
    {{"pid":"p00001","dialogue":false,"confidence":"high|medium|low",
      "plain_meaning":"short meaning",
      "idioms":[{{"source_span":"...","meaning":"...","forbidden_readings":["..."]}}],
      "referents":[{{"source_span":"it","refers_to":"..."}}],
      "invariants":["..."],"forbidden_additions":["..."]}}
  ],
  "address_updates":[
    {{"speaker":"Blake","addressee":"Duncan","expected_register":"вы",
      "confidence":"high|medium|low","evidence_pid":"p00001"}}
  ]
}}

ГЛОССАРИЙ ДЛЯ ИМЁН И СУЩНОСТЕЙ:
{glossary_prompt(runtime, cfg, source_text)}

БИБЛИЯ КНИГИ/ГЛАВЫ:
{bible_prompt(runtime, cfg, source_text, chapter_bible)}

УЖЕ УСТАНОВЛЕННАЯ МАТРИЦА ОБРАЩЕНИЙ:
{current_address_view(book_ledger.get("address_matrix") or {})}
Не меняй её без явного контекстного основания.
"""
    context = render_pairs(block_map, {}, before + after, include_source=True, tag="CONTEXT")
    targets = render_pairs(block_map, {}, pids, include_source=True, tag="TARGET")
    user = (
        "<TARGET_PIDS>" + ",".join(pids) + "</TARGET_PIDS>\n"
        "<CONTEXT_ONLY>\n" + context + "\n</CONTEXT_ONLY>\n"
        "<TARGET_BLOCKS>\n" + targets + "\n</TARGET_BLOCKS>"
    )
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


def safe_list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def safe_bool(value: Any) -> bool:
    return value if type(value) is bool else False


def parse_result(
    data: dict[str, Any], pids: list[str]
) -> tuple[dict[str, dict[str, Any]], list[dict[str, Any]], dict[str, int]]:
    """Validate mandatory PID coverage; sanitize optional model metadata."""
    if not isinstance(data, dict) or not isinstance(data.get("results"), list):
        raise ValueError("source analysis results must be a JSON list")

    stats = blank_stats()
    allowed = set(pids)
    by_pid: dict[str, dict[str, Any]] = {}

    for item in data["results"]:
        if not isinstance(item, dict):
            stats["dropped_optional_entries"] += 1
            continue
        pid = norm(item.get("pid"))
        if pid not in allowed:
            stats["unexpected_pid_entries"] += 1
            continue
        if pid in by_pid:
            stats["duplicate_pid_entries"] += 1
            continue

        confidence = norm(item.get("confidence")).casefold()
        if confidence not in {"high", "medium", "low"}:
            confidence = "low"

        idioms: list[dict[str, Any]] = []
        for entry in safe_list(item.get("idioms")):
            if not isinstance(entry, dict) or not norm(entry.get("source_span")):
                stats["dropped_optional_entries"] += 1
                continue
            readings = [norm(x) for x in safe_list(entry.get("forbidden_readings")) if norm(x)]
            idioms.append({
                "source_span": norm(entry.get("source_span"))[:500],
                "meaning": norm(entry.get("meaning"))[:800],
                "forbidden_readings": readings[:2],
            })
        if len(idioms) > MAX_IDIOMS:
            stats["dropped_optional_entries"] += len(idioms) - MAX_IDIOMS
            idioms = idioms[:MAX_IDIOMS]

        referents: list[dict[str, str]] = []
        for entry in safe_list(item.get("referents")):
            if not isinstance(entry, dict) or not norm(entry.get("source_span")):
                stats["dropped_optional_entries"] += 1
                continue
            referents.append({
                "source_span": norm(entry.get("source_span"))[:300],
                "refers_to": norm(entry.get("refers_to"))[:500],
            })
        if len(referents) > MAX_REFERENTS:
            stats["dropped_optional_entries"] += len(referents) - MAX_REFERENTS
            referents = referents[:MAX_REFERENTS]

        invariants = [norm(x) for x in safe_list(item.get("invariants")) if norm(x)]
        forbidden = [norm(x) for x in safe_list(item.get("forbidden_additions")) if norm(x)]
        if len(invariants) > MAX_INVARIANTS:
            stats["dropped_optional_entries"] += len(invariants) - MAX_INVARIANTS
        if len(forbidden) > MAX_FORBIDDEN_ADDITIONS:
            stats["dropped_optional_entries"] += len(forbidden) - MAX_FORBIDDEN_ADDITIONS

        by_pid[pid] = {
            "speaker": norm(item.get("speaker")) or None,
            "addressee": norm(item.get("addressee")) or None,
            "dialogue": safe_bool(item.get("dialogue")),
            "confidence": confidence,
            "plain_meaning": norm(item.get("plain_meaning"))[:1200],
            "idioms": idioms,
            "referents": referents,
            "invariants": invariants[:MAX_INVARIANTS],
            "forbidden_additions": forbidden[:MAX_FORBIDDEN_ADDITIONS],
        }

    missing = [pid for pid in pids if pid not in by_pid]
    if missing:
        raise ValueError(f"Source analysis omitted PIDs: {missing}")

    updates: list[dict[str, Any]] = []
    raw_updates = safe_list(data.get("address_updates"))
    for item in raw_updates[:MAX_ADDRESS_UPDATES]:
        if not isinstance(item, dict):
            stats["dropped_address_updates"] += 1
            continue
        speaker = norm(item.get("speaker"))
        addressee = norm(item.get("addressee"))
        register = norm(item.get("expected_register")).casefold()
        confidence = norm(item.get("confidence")).casefold()
        evidence_pid = norm(item.get("evidence_pid"))
        if not speaker or not addressee or register not in {"ты", "вы"}:
            stats["dropped_address_updates"] += 1
            continue
        if confidence not in {"high", "medium", "low"}:
            confidence = "low"
        if not evidence_pid or evidence_pid not in allowed:
            stats["dropped_address_updates"] += 1
            continue
        updates.append({
            "speaker": speaker,
            "addressee": addressee,
            "expected_register": register,
            "confidence": confidence,
            "evidence_pid": evidence_pid,
        })
    if len(raw_updates) > MAX_ADDRESS_UPDATES:
        stats["dropped_address_updates"] += len(raw_updates) - MAX_ADDRESS_UPDATES

    return by_pid, updates, stats


def current_address_view(matrix: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
    fields = ("speaker", "addressee", "expected_register", "confidence", "chapter", "evidence_pid", "conflict")
    return {
        key: {name: value.get(name) for name in fields if value.get(name) is not None}
        for key, value in (matrix or {}).items()
        if isinstance(value, dict)
    }


def merge_address_matrix(matrix: dict[str, dict[str, Any]], updates: list[dict[str, Any]], chapter: str) -> dict[str, dict[str, Any]]:
    """Merge register evidence conservatively while preserving chapter history."""
    rank = {"low": 1, "medium": 2, "high": 3}
    result = {key: dict(value) for key, value in (matrix or {}).items() if isinstance(value, dict)}
    for update in updates:
        key = f"{update['speaker']} -> {update['addressee']}"
        incoming = dict(update)
        incoming["chapter"] = chapter
        current = result.get(key)
        if current is None:
            incoming["history"] = [dict(incoming)]
            result[key] = incoming
            continue
        history = list(current.get("history") or [])
        history.append(dict(incoming))
        cur_register = current.get("expected_register", "unknown")
        inc_register = incoming.get("expected_register", "unknown")
        cur_conf = current.get("confidence", "low")
        inc_conf = incoming.get("confidence", "low")
        if inc_register == "unknown":
            current["history"] = history
            continue
        if cur_register in {"unknown", inc_register}:
            if rank.get(inc_conf, 1) >= rank.get(cur_conf, 1) or cur_register == "unknown":
                current.update(incoming)
            current["history"] = history
            result[key] = current
            continue
        if rank.get(inc_conf, 1) > rank.get(cur_conf, 1):
            current.update(incoming)
            current["history"] = history
            current["changed_from"] = cur_register
        elif rank.get(inc_conf, 1) == rank.get(cur_conf, 1):
            result[key] = {
                "speaker": update["speaker"],
                "addressee": update["addressee"],
                "expected_register": "unknown",
                "confidence": "low",
                "conflict": [cur_register, inc_register],
                "history": history,
                "chapter": chapter,
            }
        else:
            current["history"] = history
            current.setdefault("rejected_conflicts", []).append(dict(incoming))
            result[key] = current
    return result



def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    add_common_args(parser, include_pass=False)
    args = parser.parse_args()
    setup_logging()
    project = args.project_root.resolve()
    runtime = load_runtime(project)
    cfg = load_cfg(runtime, args.config.resolve())
    stage = stage_cfg(cfg, "source_analysis", DEFAULT_STAGE)
    client = api_client(runtime, cfg, "reviewer_api", "qwen_source_analysis", args.model)
    ledger_path = Path(cfg["paths"]["work_dir"]).parent / "book_consistency_ledger.json"
    book_ledger = read_json(ledger_path, {"version": VERSION, "address_matrix": {}})

    for source_path, work in selected_chapters(runtime, cfg, args.start, args.end):
        _, blocks, block_map = load_manifest(work)
        out = work / "source_scene_map.json"
        cache_dir = work / "v31_source_analysis"
        if out.exists() and not args.force:
            logging.info("Reusing %s", out)
            existing = read_json(out, {})
            book_ledger["address_matrix"] = merge_address_matrix(
                book_ledger.get("address_matrix") or {},
                [dict(value) for value in (existing.get("address_matrix") or {}).values() if isinstance(value, dict)],
                source_path.name,
            )
            write_json(ledger_path, book_ledger)
            continue

        cache_dir.mkdir(parents=True, exist_ok=True)
        by_pid: dict[str, dict[str, Any]] = {}
        address_updates: list[dict[str, Any]] = []
        chapter_stats = blank_stats()
        pids = [str(block["pid"]) for block in blocks]

        def evaluate_batch(batch_pids: list[str], cache_path: Path, label: str):
            if cache_path.exists() and not args.force:
                saved = read_json(cache_path, {})
                return (
                    saved.get("by_pid") or {},
                    saved.get("address_updates") or [],
                    saved.get("statistics") or blank_stats(),
                )

            messages = messages_for_batch(
                runtime, cfg, work, blocks, block_map, batch_pids, book_ledger
            )
            try:
                (local_by_pid, local_updates, validation_stats), attempts = complete_json(
                    runtime, client, messages, stage, int(stage["max_tokens"]),
                    f"source_analysis:{source_path.stem}:{label}", int(stage["attempts"]),
                    validator=lambda data, expected=batch_pids: parse_result(data, expected),
                )
                local_stats = merge_stats(validation_stats, attempt_stats(attempts))
                local_stats["successful_batches"] += 1
                record = {
                    "version": VERSION,
                    "pids": batch_pids,
                    "by_pid": local_by_pid,
                    "address_updates": local_updates,
                    "attempts": attempts,
                    "statistics": local_stats,
                    "split": False,
                }
            except RuntimeError as exc:
                error_attempts = list(getattr(exc, "attempt_errors", []) or [])
                failed_stats = attempt_stats(error_attempts)
                if len(batch_pids) <= 1:
                    raise
                midpoint = len(batch_pids) // 2
                left_pids, right_pids = batch_pids[:midpoint], batch_pids[midpoint:]
                logging.warning("%s failed for %s; splitting source analysis", label, batch_pids)
                left_map, left_updates, left_stats = evaluate_batch(
                    left_pids, cache_path.with_name(cache_path.stem + "_a.json"), label + "a"
                )
                right_map, right_updates, right_stats = evaluate_batch(
                    right_pids, cache_path.with_name(cache_path.stem + "_b.json"), label + "b"
                )
                local_by_pid = {**left_map, **right_map}
                local_updates = left_updates + right_updates
                local_stats = merge_stats(failed_stats, left_stats, right_stats)
                local_stats["split_batches"] += 1
                record = {
                    "version": VERSION,
                    "pids": batch_pids,
                    "by_pid": local_by_pid,
                    "address_updates": local_updates,
                    "attempts": error_attempts or [{"ok": False, "error": str(exc), "split_recovery": True}],
                    "statistics": local_stats,
                    "split": True,
                    "children": [left_pids, right_pids],
                }
            write_json(cache_path, record)
            return local_by_pid, local_updates, local_stats

        for index, batch in enumerate(batched(pids, int(stage["batch_pids"])), 1):
            cache = cache_dir / f"batch_{index:04d}.json"
            parsed_by_pid, updates, stats = evaluate_batch(batch, cache, f"batch{index}")
            by_pid.update(parsed_by_pid)
            address_updates.extend(updates)
            chapter_stats = merge_stats(chapter_stats, stats)
            logging.info("source analysis %s: %s/%s", source_path.name, len(by_pid), len(pids))

        if set(by_pid) != set(pids):
            raise RuntimeError(f"Incomplete source analysis coverage for {source_path.name}")

        effective_matrix = merge_address_matrix(
            book_ledger.get("address_matrix") or {}, address_updates, source_path.name
        )
        book_ledger.update({
            "version": VERSION,
            "last_chapter": source_path.name,
            "address_matrix": effective_matrix,
        })
        write_json(ledger_path, book_ledger)

        write_json(out, {
            "version": VERSION,
            "chapter": source_path.name,
            "coverage": {"expected": len(pids), "completed": len(by_pid), "ok": len(by_pid) == len(pids)},
            "statistics": chapter_stats,
            "by_pid": by_pid,
            "address_matrix": current_address_view(effective_matrix),
            "book_ledger": str(ledger_path),
            "calls": client.calls,
        })
        logging.info(
            "Source analysis stats for %s: successful=%s split=%s truncated_json=%s dropped_address_updates=%s",
            source_path.name,
            chapter_stats["successful_batches"],
            chapter_stats["split_batches"],
            chapter_stats["truncated_json"],
            chapter_stats["dropped_address_updates"],
        )
        logging.info("Wrote %s", out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
