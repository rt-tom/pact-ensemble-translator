#!/usr/bin/env python3
"""Cross-verify ensemble issues with the model that did not detect them."""
from __future__ import annotations

import argparse
import logging
from pathlib import Path
from typing import Any

from v31_common import (
    VERSION, add_common_args, api_client, bible_prompt, complete_json,
    glossary_prompt, load_cfg, load_manifest, load_runtime, load_translations,
    norm, read_json, scene_notes_for_pids, selected_chapters, setup_logging,
    stage_cfg, write_json,
)

DEFAULT_QWEN = {
    "temperature": 0.0, "top_p": 1.0, "top_k": 64,
    "enable_thinking": False, "max_tokens": 800, "attempts": 3,
    "context_size": 2,
}
DEFAULT_GEMMA = {
    "temperature": 0.0, "top_p": 1.0, "top_k": 64,
    "enable_thinking": True, "max_tokens": 800, "attempts": 3,
    "context_size": 2,
}


def messages(runtime, cfg, work, blocks, block_map, translations, issue, judge):
    defaults = DEFAULT_QWEN if judge == "qwen" else DEFAULT_GEMMA
    section = f"{judge}_cross_verifier"
    stage = stage_cfg(cfg, section, defaults)
    pid = issue["pid"]
    positions = {str(block["pid"]): i for i, block in enumerate(blocks)}
    pos = positions[pid]
    size = int(stage["context_size"])
    context_pids = [str(b["pid"]) for b in blocks[max(0,pos-size):pos] + blocks[pos+1:pos+1+size]]
    all_pids = context_pids + [pid]
    source_text = "\n".join(norm(block_map[p].get("source_text")) for p in all_pids)
    chapter_bible = read_json(work / "chapter_bible.json", {})
    scene = scene_notes_for_pids(work, all_pids)
    role = "Qwen" if judge == "qwen" else "Gemma"
    system = f"""Ты — {role}, независимый судья одного предполагаемого дефекта
перевода EN→RU. Ты НЕ должен подтверждать рассуждение аудитора как текст.
Ответь на прямой вопрос: необходимо ли изменить текущий русский PID?

decision:
- repair: объективная проблема существует и текст нужно изменить;
- keep: текущий перевод допустим, менять его не нужно;
- uncertain: контекста недостаточно или замечание спорно.

Не оценивай предложенную формулировку — её ещё нет. Сформулируй проверяемый
required_invariant, которому должна соответствовать будущая правка.
Верни только JSON:
{{
 "decision":"repair|keep|uncertain",
 "confidence":"high|medium|low",
 "reason":"кратко",
 "required_invariant":"обязательное смысловое/языковое условие",
 "forbidden_interpretations":["что нельзя исказить"],
 "repair_scope":"span|sentence|paragraph",
 "target_span":"точный русский фрагмент для локальной замены или пусто"
}}

ГЛОССАРИЙ:
{glossary_prompt(runtime, cfg, source_text)}

БИБЛИЯ:
{bible_prompt(runtime, cfg, source_text, chapter_bible)}

SOURCE NOTES:
{scene}
"""
    context = []
    for p in context_pids:
        context.append({"pid": p, "en": norm(block_map[p].get("source_text")), "ru": translations.get(p, "")})
    user = {
        "issue": issue,
        "target": {
            "pid": pid,
            "en": norm(block_map[pid].get("source_text")),
            "ru": translations.get(pid, ""),
        },
        "context": context,
    }
    import json
    return stage, [
        {"role": "system", "content": system},
        {"role": "user", "content": json.dumps(user, ensure_ascii=False)},
    ]


def parse(data: dict[str, Any]) -> dict[str, Any]:
    decision = norm(data.get("decision")).casefold()
    confidence = norm(data.get("confidence")).casefold()
    scope = norm(data.get("repair_scope")).casefold()
    if decision not in {"repair", "keep", "uncertain"}:
        raise ValueError(f"Invalid decision: {decision}")
    if confidence not in {"high", "medium", "low"}:
        raise ValueError(f"Invalid confidence: {confidence}")
    if scope not in {"span", "sentence", "paragraph"}:
        scope = "span"
    return {
        "decision": decision,
        "confidence": confidence,
        "reason": norm(data.get("reason"))[:1200],
        "required_invariant": norm(data.get("required_invariant"))[:1200],
        "forbidden_interpretations": [norm(x) for x in (data.get("forbidden_interpretations") or []) if norm(x)],
        "repair_scope": scope,
        "target_span": norm(data.get("target_span"))[:600],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    add_common_args(parser)
    parser.add_argument("--judge", choices=["qwen", "gemma"], required=True)
    parser.add_argument("--translations-file")
    args = parser.parse_args()
    setup_logging()
    runtime = load_runtime(args.project_root.resolve())
    cfg = load_cfg(runtime, args.config.resolve())
    api_section = "reviewer_api" if args.judge == "qwen" else "translator_api"
    client = api_client(runtime, cfg, api_section, f"{args.judge}_cross_verifier", args.model)

    for source_path, work in selected_chapters(runtime, cfg, args.start, args.end):
        _, blocks, block_map = load_manifest(work)
        translations = load_translations(work, args.pass_name, args.translations_file)
        root = work / "v31" / args.pass_name
        queue = read_json(root / f"verify_queue_{args.judge}.json", [])
        out = root / f"cross_verify_{args.judge}.json"
        cache_dir = root / "cross_verify" / args.judge
        if out.exists() and not args.force:
            logging.info("Reusing %s", out)
            continue
        cache_dir.mkdir(parents=True, exist_ok=True)
        decisions = []
        for index, issue in enumerate(queue, 1):
            issue_id = issue["issue_id"]
            cache = cache_dir / f"{issue_id}.json"
            if cache.exists() and not args.force:
                record = read_json(cache, {})
            else:
                stage, prompt = messages(runtime, cfg, work, blocks, block_map, translations, issue, args.judge)
                verdict, attempts = complete_json(
                    runtime, client, prompt, stage, int(stage["max_tokens"]),
                    f"cross_verify:{args.judge}:{source_path.stem}:{issue_id}", int(stage["attempts"]),
                    validator=parse,
                )
                record = {
                    "version": VERSION,
                    "issue_id": issue_id,
                    "pid": issue["pid"],
                    "judge": args.judge,
                    "issue": issue,
                    **verdict,
                    "attempts": attempts,
                }
                write_json(cache, record)
            decisions.append(record)
            logging.info("%s cross verify %s: %s/%s %s", args.judge, source_path.name, index, len(queue), record.get("decision"))
        write_json(out, {
            "version": VERSION,
            "chapter": source_path.name,
            "pass": args.pass_name,
            "judge": args.judge,
            "expected": len(queue),
            "completed": len(decisions),
            "decisions": decisions,
            "calls": client.calls,
        })
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
