#!/usr/bin/env python3
"""Generate minimal repair candidates for verified Pact v3.1 issues."""
from __future__ import annotations

import argparse
import difflib
import json
import logging
from collections import defaultdict
from pathlib import Path
from typing import Any

from v31_common import (
    VERSION, add_common_args, api_client, bible_prompt, complete_json,
    glossary_prompt, load_cfg, load_manifest, load_runtime, load_translations,
    cache_identity, cache_reuse, norm, read_json, scene_notes_for_pids, selected_chapters, setup_logging,
    stage_cfg, with_cache_identity, write_json,
)

DEFAULT_STAGE = {
    "temperature": 0.0, "top_p": 1.0, "top_k": 64,
    "enable_thinking": False, "max_tokens": 1600, "attempts": 3,
    "context_before": 2, "context_after": 2,
    "alternative_for_multiple_issues": True,
    "alternative_categories": ["idiom", "meaning", "register", "dialogue", "continuity"],
    "max_changed_ratio_span": 0.35,
}


def changed_ratio(before: str, after: str) -> float:
    matcher = difflib.SequenceMatcher(a=before, b=after)
    return 1.0 - matcher.ratio()


def prompt(runtime, cfg, work, blocks, block_map, translations, pid, issues, feedback):
    stage = stage_cfg(cfg, "repair", DEFAULT_STAGE)
    positions = {str(block["pid"]): i for i, block in enumerate(blocks)}
    pos = positions[pid]
    before_pids = [str(b["pid"]) for b in blocks[max(0,pos-int(stage["context_before"])):pos]]
    after_pids = [str(b["pid"]) for b in blocks[pos+1:pos+1+int(stage["context_after"])]]
    source_text = norm(block_map[pid].get("source_text"))
    bible = read_json(work / "chapter_bible.json", {})
    scene = scene_notes_for_pids(work, before_pids + [pid] + after_pids)
    difficult_categories = {str(x).casefold() for x in stage.get("alternative_categories", [])}
    difficult = (
        len(issues) > 1
        or any(str(issue.get("category", "")).casefold() in difficult_categories for issue in issues)
        or any(issue.get("scope") in {"paragraph", "cross_pid"} for issue in issues)
        or bool(feedback)
    )
    count_instruction = "Верни два действительно разных кандидата A и B." if difficult else "Верни один кандидат A."
    system = f"""Ты — литературный repair-редактор русского перевода.
Исправь только подтверждённые issues в одном PID и сохрани всё остальное.
По умолчанию используй replace_span: old должен быть ТОЧНОЙ непрерывной
подстрокой CURRENT_RU и встречаться ровно один раз. replace_full разрешён
только когда структура предложения не позволяет локальную замену.

Не копируй механически предложение аудитора. Сохраняй required_invariant,
не добавляй анатомию, причинность, эмоции или детали, которых нет в EN.
Не меняй имена, числа, ты/вы и соседние предложения без необходимости.

Если issue объективно ложный и текст менять нельзя, разрешён
challenge_issue. Такое оспаривание затем отдельно проверят Qwen и Gemma.
{count_instruction}

Верни только JSON:
{{
 "candidates":[{{
   "candidate_id":"A",
   "action":"replace_span|replace_full|challenge_issue",
   "old":"точная подстрока для replace_span",
   "new":"новая подстрока для replace_span",
   "text":"полный PID только для replace_full",
   "reason":"кратко",
   "challenge_reason":"только для challenge_issue"
 }}]
}}

ГЛОССАРИЙ:
{glossary_prompt(runtime, cfg, source_text)}

БИБЛИЯ:
{bible_prompt(runtime, cfg, source_text, bible)}

SOURCE NOTES:
{scene}
"""
    context = {
        "before": [{"pid": p, "ru": translations.get(p, "")} for p in before_pids],
        "target": {"pid": pid, "en": source_text, "current_ru": translations[pid]},
        "issues": issues,
        "previous_feedback": feedback,
        "after": [{"pid": p, "ru": translations.get(p, "")} for p in after_pids],
    }
    return stage, [
        {"role": "system", "content": system},
        {"role": "user", "content": json.dumps(context, ensure_ascii=False)},
    ], difficult


def parse_candidates(data: dict[str, Any], pid: str, current: str, difficult: bool, runtime, cfg, block_map):
    raw = data.get("candidates") or []
    if not isinstance(raw, list):
        raise ValueError("candidates must be a list")
    candidates = []
    seen_ids = set()
    for index, item in enumerate(raw, 1):
        if not isinstance(item, dict):
            continue
        cid = norm(item.get("candidate_id")) or chr(64+index)
        if cid in seen_ids:
            continue
        seen_ids.add(cid)
        action = norm(item.get("action")).casefold()
        if action not in {"replace_span", "replace_full", "challenge_issue"}:
            continue
        declared_action = action
        errors = []
        raw_old = item.get("old")
        old = norm(raw_old) if isinstance(raw_old, str) else ""
        new = norm(item.get("new"))
        text = norm(item.get("text"))
        challenge = norm(item.get("challenge_reason"))
        after = current
        if action == "replace_span":
            if not old:
                errors.append("old span is empty")
            # A span is a literal edit address, never a normalized or fuzzy
            # match. This also rejects a missing or ambiguous target span.
            elif not isinstance(raw_old, str) or current.count(raw_old) != 1:
                errors.append(f"old span occurrence count={current.count(raw_old) if isinstance(raw_old, str) else 0}")
            if not new:
                errors.append("new span is empty")
            if not errors:
                after = current.replace(raw_old, new, 1)
                # Only byte-for-byte equality is whole-PID scope. Preserve
                # the declared operation in provenance, but normalize the
                # lifecycle action so it follows replace_full downstream.
                if raw_old == current:
                    action = "replace_full"
        elif action == "replace_full":
            after = text
            if not text:
                errors.append("full replacement is empty")
            # Models sometimes preserve CURRENT_RU in ``text`` while placing
            # the full replacement in ``new``.  Accept that unambiguous form
            # only when it supplies an actual replacement; the normal ``text``
            # field remains authoritative in every other case.
            elif text == current and new and new != current:
                after = new
        else:
            if not challenge:
                errors.append("challenge reason is empty")
        if action != "challenge_issue":
            errors.extend(runtime.validate_single_repair(pid, after, block_map, cfg, current))
            ratio = changed_ratio(current, after)
            if action == "replace_span" and ratio > float(cfg.get("ensemble_v31", {}).get("repair", {}).get("max_changed_ratio_span", DEFAULT_STAGE["max_changed_ratio_span"])):
                errors.append(f"span repair changed_ratio={ratio:.3f}")
        candidates.append({
            "candidate_id": cid,
            "action": action,
            "declared_action": declared_action,
            "operation_provenance": "whole_pid_replace_span_normalized_to_replace_full" if declared_action == "replace_span" and action == "replace_full" else "declared_action",
            "old": old,
            "new": new,
            "text": text,
            "before": current,
            "after": after,
            "reason": norm(item.get("reason")),
            "challenge_reason": challenge,
            "validation_errors": errors,
            "valid": not errors,
            "changed_ratio": round(changed_ratio(current, after), 4) if action != "challenge_issue" else 0.0,
        })
    valid = [c for c in candidates if c["valid"]]
    expected = 2 if difficult else 1
    if not valid:
        raise ValueError(f"No valid candidates: {candidates}")
    # A model may fail to produce two alternatives; do not invent one. The gate
    # can still evaluate the valid candidate and retry if needed.
    return valid[:expected]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    add_common_args(parser)
    parser.add_argument("--round", type=int, default=1)
    parser.add_argument("--translations-file")
    parser.add_argument("--retry-only", action="store_true")
    args = parser.parse_args()
    setup_logging()
    runtime = load_runtime(args.project_root.resolve())
    cfg = load_cfg(runtime, args.config.resolve())
    client = api_client(runtime, cfg, "translator_api", "gemma_v31_repair", args.model)

    for source_path, work in selected_chapters(runtime, cfg, args.start, args.end):
        _, blocks_raw, _ = load_manifest(work)
        block_objs = runtime.blocks_from_manifest(blocks_raw)
        block_map = {block.pid: block for block in block_objs}
        translations = load_translations(work, args.pass_name, args.translations_file)
        root = work / "v31" / args.pass_name
        issues = read_json(root / "verified_issues.json", [])
        by_pid = defaultdict(list)
        for issue in issues:
            by_pid[issue["pid"]].append(issue)

        retry_requests = read_json(root / f"retry_requests_round_{args.round-1:02d}.json", []) if args.round > 1 else []
        retry_by_pid = {item["pid"]: item for item in retry_requests}
        pids = sorted(retry_by_pid if args.retry_only else by_pid, key=lambda p: block_map[p].index)
        out = root / f"repair_candidates_round_{args.round:02d}.json"
        cache_dir = root / "repairs" / f"round_{args.round:02d}"
        cache_dir.mkdir(parents=True, exist_ok=True)
        records = []
        for index, pid in enumerate(pids, 1):
            cache = cache_dir / f"{pid}.json"
            feedback = retry_by_pid.get(pid, {}).get("feedback", [])
            stage, messages, difficult = prompt(runtime, cfg, work, blocks_raw, {b.pid: {"source_text": b.source_text} for b in block_objs}, translations, pid, by_pid[pid], feedback)
            identity = cache_identity(
                producer="v31_repair", schema="repair-record/v1",
                source={"chapter": source_path.name, "blocks": blocks_raw},
                inputs={"pid": pid, "issues": by_pid[pid], "feedback": feedback, "translations": translations, "round": args.round},
                config=stage, prompt=messages, profile={"model": args.model or cfg["translator_api"].get("model")},
            )
            record = None
            if not args.force:
                record, reason = cache_reuse(cache, identity)
                if record is None and cache.exists():
                    logging.info("Cache miss %s: %s", cache, reason)
            if record is None:
                candidates, attempts = complete_json(
                    runtime, client, messages, stage, int(stage["max_tokens"]),
                    f"v31_repair:{args.pass_name}:{source_path.stem}:{pid}:round{args.round}", int(stage["attempts"]),
                    validator=lambda data, target_pid=pid, original=translations[pid], hard=difficult: parse_candidates(
                        data, target_pid, original, hard, runtime, cfg, block_map
                    ),
                )
                record = {
                    "version": VERSION,
                    "pass": args.pass_name,
                    "round": args.round,
                    "pid": pid,
                    "issues": by_pid[pid],
                    "feedback": feedback,
                    "candidates": candidates,
                    "attempts": attempts,
                }
                write_json(cache, with_cache_identity(record, identity))
            records.append(record)
            logging.info("repair %s %s round %s: %s/%s candidates=%s", args.pass_name, source_path.name, args.round, index, len(pids), len(record.get("candidates") or []))
        write_json(out, {
            "version": VERSION,
            "chapter": source_path.name,
            "pass": args.pass_name,
            "round": args.round,
            "pid_count": len(pids),
            "records": records,
            "calls": client.calls,
        })
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
