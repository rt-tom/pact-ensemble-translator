#!/usr/bin/env python3
"""Independent semantic and Russian gates for Pact v3.1 repair candidates."""
from __future__ import annotations

import argparse
import json
import logging
from typing import Any

from v31_common import (
    VERSION, add_common_args, api_client, bible_prompt, complete_json,
    dialogue_scene_notes, glossary_prompt, load_cfg, load_manifest,
    load_runtime, load_translations, norm, read_json, scene_notes_for_pids,
    selected_chapters, setup_logging, stage_cfg, strict_bool, write_json,
)

DEFAULTS = {
    "qwen_semantic": {
        "temperature": 0.0, "top_p": 1.0, "top_k": 64,
        "enable_thinking": False, "max_tokens": 900, "attempts": 3,
        "context_size": 2,
    },
    "gemma_semantic": {
        "temperature": 0.0, "top_p": 1.0, "top_k": 64,
        "enable_thinking": True, "max_tokens": 900, "attempts": 3,
        "context_size": 2,
    },
    "gemma_russian": {
        "temperature": 0.0, "top_p": 1.0, "top_k": 64,
        "enable_thinking": True, "max_tokens": 900, "attempts": 3,
        "context_size": 2,
    },
}


def build_prompt(runtime, cfg, work, blocks, block_map, translations, record, candidate, judge):
    stage = stage_cfg(cfg, f"{judge}_post_gate", DEFAULTS[judge])
    pid = record["pid"]
    positions = {str(block["pid"]): i for i, block in enumerate(blocks)}
    pos = positions[pid]
    size = int(stage["context_size"])
    context_pids = [
        str(b["pid"])
        for b in blocks[max(0, pos-size):pos] + blocks[pos+1:pos+1+size]
    ]
    source_text = norm(block_map[pid].get("source_text"))
    chapter_bible = read_json(work / "chapter_bible.json", {})
    action = candidate["action"]

    if judge in {"qwen_semantic", "gemma_semantic"}:
        identity = "Qwen" if judge == "qwen_semantic" else "Gemma"
        independence = (
            "SOURCE NOTES даны как дополнительная подсказка и не являются истиной."
            if judge == "qwen_semantic"
            else "Ты намеренно не получаешь Qwen SOURCE NOTES: делай независимую проверку EN/RU."
        )
        notes = scene_notes_for_pids(work, context_pids + [pid]) if judge == "qwen_semantic" else {}
        system = f"""Ты — {identity}, независимый semantic gate после repair EN→RU.
Не доверяй repair-модели и не подтверждай ответ автоматически. Сравни EN,
BEFORE_RU и CANDIDATE_RU с каждым подтверждённым issue и required_invariant.
{independence}

Для обычной правки verdict=accept только если:
- смысл EN сохранён;
- все перечисленные issues устранены;
- не добавлены новые детали;
- субъект, объект, отрицание, модальность, время, числа и референты не сломаны.

Для action=challenge_issue оцени валидность исходных issues. accept_challenge
допустим только если ВСЕ issues ложны и BEFORE_RU действительно не требует
изменения. Иначе reject_challenge.

Верни только JSON:
{{
 "verdict":"accept|reject|uncertain|accept_challenge|reject_challenge",
 "confidence":"high|medium|low",
 "issue_valid":true,
 "faithful_to_source":true,
 "all_issues_fixed":true,
 "introduced_new_semantic_error":false,
 "reason":"кратко",
 "feedback":"точная инструкция для следующей попытки при reject"
}}

ГЛОССАРИЙ:
{glossary_prompt(runtime, cfg, source_text)}

БИБЛИЯ:
{bible_prompt(runtime, cfg, source_text, chapter_bible)}

SOURCE NOTES:
{notes}
"""
        payload = {
            "pid": pid,
            "action": action,
            "en": source_text,
            "before_ru": candidate["before"],
            "candidate_ru": candidate["after"],
            "challenge_reason": candidate.get("challenge_reason", ""),
            "issues": record.get("issues") or [],
            "context": [
                {
                    "pid": p,
                    "en": norm(block_map[p].get("source_text")),
                    "ru": translations.get(p, ""),
                }
                for p in context_pids
            ],
        }
    else:
        scene = dialogue_scene_notes(work, context_pids + [pid])
        system = f"""Ты — независимый монолингвальный редактор русского
текста после repair. Не смотри на английский оригинал и не угадывай его.
Проверь CANDIDATE_RU в русском контексте.

Для обычной правки verdict=accept только если:
- русский грамматически корректен и естественен;
- нет кальки, сломанной сочетаемости, обрыва или неясного референта;
- обращение, голос и ты/вы согласованы с контекстом и DIALOGUE NOTES;
- заявленные русскоязычные issues устранены;
- repair не ухудшил хороший текст.

Для action=challenge_issue оцени, существует ли русскоязычная проблема.
accept_challenge допустим только если ВСЕ issues ложны. Иначе reject_challenge.

Верни только JSON:
{{
 "verdict":"accept|reject|uncertain|accept_challenge|reject_challenge",
 "confidence":"high|medium|low",
 "issue_valid":true,
 "natural_russian":true,
 "all_issues_fixed":true,
 "introduced_new_russian_error":false,
 "reason":"кратко",
 "feedback":"точная инструкция для следующей попытки при reject"
}}

DIALOGUE NOTES только для говорящего/адресата и регистра:
{scene}
"""
        payload = {
            "pid": pid,
            "action": action,
            "before_ru": candidate["before"],
            "candidate_ru": candidate["after"],
            "challenge_reason": candidate.get("challenge_reason", ""),
            "issues": record.get("issues") or [],
            "context_before_after": [
                {"pid": p, "ru": translations.get(p, "")} for p in context_pids
            ],
        }
    return stage, [
        {"role": "system", "content": system},
        {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
    ]


def parse(data: dict[str, Any], judge: str, action: str) -> dict[str, Any]:
    verdict = norm(data.get("verdict")).casefold()
    confidence = norm(data.get("confidence")).casefold()
    allowed = {"accept", "reject", "uncertain", "accept_challenge", "reject_challenge"}
    if verdict not in allowed:
        raise ValueError(f"Invalid verdict {verdict}")
    if confidence not in {"high", "medium", "low"}:
        raise ValueError(f"Invalid confidence {confidence}")
    if action == "challenge_issue" and verdict not in {"accept_challenge", "reject_challenge", "uncertain"}:
        raise ValueError(f"Invalid challenge verdict {verdict}")
    if action != "challenge_issue" and verdict not in {"accept", "reject", "uncertain"}:
        raise ValueError(f"Invalid repair verdict {verdict}")

    result = {
        "verdict": verdict,
        "confidence": confidence,
        "issue_valid": strict_bool(data.get("issue_valid"), "issue_valid"),
        "all_issues_fixed": strict_bool(data.get("all_issues_fixed"), "all_issues_fixed"),
        "reason": norm(data.get("reason"))[:1200],
        "feedback": norm(data.get("feedback"))[:1200],
    }
    if judge in {"qwen_semantic", "gemma_semantic"}:
        result.update({
            "faithful_to_source": strict_bool(data.get("faithful_to_source"), "faithful_to_source"),
            "introduced_new_semantic_error": strict_bool(
                data.get("introduced_new_semantic_error"),
                "introduced_new_semantic_error",
            ),
        })
    else:
        result.update({
            "natural_russian": strict_bool(data.get("natural_russian"), "natural_russian"),
            "introduced_new_russian_error": strict_bool(
                data.get("introduced_new_russian_error"),
                "introduced_new_russian_error",
            ),
        })
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    add_common_args(parser)
    parser.add_argument("--judge", choices=list(DEFAULTS), required=True)
    parser.add_argument("--round", type=int, required=True)
    parser.add_argument("--translations-file")
    args = parser.parse_args()
    setup_logging()
    runtime = load_runtime(args.project_root.resolve())
    cfg = load_cfg(runtime, args.config.resolve())
    api_section = "reviewer_api" if args.judge == "qwen_semantic" else "translator_api"
    client = api_client(runtime, cfg, api_section, f"{args.judge}_v31_post_gate", args.model)

    for source_path, work in selected_chapters(runtime, cfg, args.start, args.end):
        _, blocks, block_map = load_manifest(work)
        translations = load_translations(work, args.pass_name, args.translations_file)
        root = work / "v31" / args.pass_name
        candidate_report = read_json(root / f"repair_candidates_round_{args.round:02d}.json", {})
        records = candidate_report.get("records") or []
        out = root / f"post_gate_{args.judge}_round_{args.round:02d}.json"
        cache_dir = root / "post_gates" / args.judge / f"round_{args.round:02d}"
        if out.exists() and not args.force:
            logging.info("Reusing %s", out)
            continue
        cache_dir.mkdir(parents=True, exist_ok=True)
        decisions = []
        total = sum(len(record.get("candidates") or []) for record in records)
        done = 0
        for record in records:
            pid = record["pid"]
            for candidate in record.get("candidates") or []:
                cid = candidate["candidate_id"]
                cache = cache_dir / f"{pid}_{cid}.json"
                if cache.exists() and not args.force:
                    decision = read_json(cache, {})
                else:
                    stage, messages = build_prompt(
                        runtime, cfg, work, blocks, block_map,
                        translations, record, candidate, args.judge,
                    )
                    verdict, attempts = complete_json(
                        runtime, client, messages, stage, int(stage["max_tokens"]),
                        f"post_gate:{args.judge}:{args.pass_name}:{source_path.stem}:{pid}:{cid}:round{args.round}",
                        int(stage["attempts"]),
                        validator=lambda data, judge=args.judge, action=candidate["action"]: parse(data, judge, action),
                    )
                    decision = {
                        "version": VERSION,
                        "pass": args.pass_name,
                        "round": args.round,
                        "judge": args.judge,
                        "pid": pid,
                        "candidate_id": cid,
                        "action": candidate["action"],
                        **verdict,
                        "attempts": attempts,
                    }
                    write_json(cache, decision)
                decisions.append(decision)
                done += 1
                logging.info(
                    "%s post gate %s: %s/%s %s/%s=%s",
                    args.judge, source_path.name, done, total,
                    pid, cid, decision.get("verdict"),
                )
        write_json(out, {
            "version": VERSION,
            "chapter": source_path.name,
            "pass": args.pass_name,
            "round": args.round,
            "judge": args.judge,
            "expected": total,
            "completed": len(decisions),
            "decisions": decisions,
            "calls": client.calls,
        })
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
