#!/usr/bin/env python3
"""Cross-verify ensemble issues with the model that did not detect them."""
from __future__ import annotations

import argparse
import logging
from pathlib import Path
from typing import Any, Callable

from v31_common import (
    VERSION, add_common_args, api_client, bible_prompt, complete_json,
    glossary_prompt, load_cfg, load_manifest, load_runtime, load_translations,
    norm, read_json, scene_notes_for_pids, selected_chapters, setup_logging,
    stage_cfg, write_json,
)

DEFAULT_QWEN = {
    "temperature": 0.0, "top_p": 1.0, "top_k": 64,
    "enable_thinking": False, "max_tokens": 1400,
    "length_retry_max_tokens": 1600, "attempts": 3,
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
Верни один полный JSON object. Никаких Markdown и текста вне JSON.
reason — 2–3 коротких предложения. required_invariant — одно короткое
проверяемое условие. Не повторяй весь контекст и рассуждения.
Строго соблюдай эту схему:
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


def required_enum(data: dict[str, Any], field: str, allowed: set[str]) -> str:
    raw = data.get(field)
    if not isinstance(raw, str):
        raise ValueError(f"{field} must be a string")
    value = norm(raw).casefold()
    if value not in allowed:
        raise ValueError(f"Invalid {field}: {value}")
    return value


def required_bounded_text(data: dict[str, Any], field: str, limit: int) -> str:
    raw = data.get(field)
    if not isinstance(raw, str):
        raise ValueError(f"{field} must be a string")
    value = norm(raw)
    if not value:
        raise ValueError(f"{field} must not be empty")
    if len(value) > limit:
        raise ValueError(f"{field} exceeds {limit} characters")
    return value


def parse(data: dict[str, Any]) -> dict[str, Any]:
    required = {
        "decision", "confidence", "reason", "required_invariant",
        "forbidden_interpretations", "repair_scope", "target_span",
    }
    missing = sorted(required.difference(data))
    if missing:
        raise ValueError(f"Missing required fields: {', '.join(missing)}")
    decision = required_enum(data, "decision", {"repair", "keep", "uncertain"})
    confidence = required_enum(data, "confidence", {"high", "medium", "low"})
    scope = required_enum(data, "repair_scope", {"span", "sentence", "paragraph"})
    reason = required_bounded_text(data, "reason", 800)
    required_invariant = required_bounded_text(data, "required_invariant", 500)
    forbidden_raw = data["forbidden_interpretations"]
    if not isinstance(forbidden_raw, list) or not all(isinstance(x, str) for x in forbidden_raw):
        raise ValueError("forbidden_interpretations must be a list of strings")
    target_raw = data["target_span"]
    if not isinstance(target_raw, str):
        raise ValueError("target_span must be a string")
    target_span = norm(target_raw)
    if len(target_span) > 600:
        raise ValueError("target_span exceeds 600 characters")
    return {
        "decision": decision,
        "confidence": confidence,
        "reason": reason,
        "required_invariant": required_invariant,
        "forbidden_interpretations": [norm(x) for x in forbidden_raw if norm(x)],
        "repair_scope": scope,
        "target_span": target_span,
    }


def load_or_generate(
    cache: Path, force: bool, generator: Callable[[], dict[str, Any]],
) -> dict[str, Any]:
    if cache.exists() and not force:
        return read_json(cache, {})
    record = generator()
    write_json(cache, record)
    return record


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

            def generate_record() -> dict[str, Any]:
                stage, prompt = messages(runtime, cfg, work, blocks, block_map, translations, issue, args.judge)
                verdict, attempts = complete_json(
                    runtime, client, prompt, stage, int(stage["max_tokens"]),
                    f"cross_verify:{args.judge}:{source_path.stem}:{issue_id}", int(stage["attempts"]),
                    validator=parse,
                    length_retry_max_tokens=(
                        int(stage["length_retry_max_tokens"])
                        if stage.get("length_retry_max_tokens") is not None else None
                    ),
                )
                return {
                    "version": VERSION,
                    "issue_id": issue_id,
                    "pid": issue["pid"],
                    "judge": args.judge,
                    "issue": issue,
                    **verdict,
                    "attempts": attempts,
                }

            record = load_or_generate(cache, args.force, generate_record)
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
