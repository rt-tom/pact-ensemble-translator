#!/usr/bin/env python3
"""Independent ensemble audits for Pact pipeline v3.1."""
from __future__ import annotations

import argparse
import logging
from pathlib import Path
from typing import Any

from v31_common import (
    VERSION, add_common_args, api_client, batched, bible_prompt,
    chapter_context, complete_json, dialogue_scene_notes, glossary_prompt, issue_record,
    load_cfg, load_manifest, load_runtime, load_translations, norm,
    read_json, render_pairs, scene_notes_for_pids, selected_chapters,
    cache_identity, cache_reuse, setup_logging, stage_cfg, with_cache_identity, write_json,
)
from v31_final_ledger_scope import SCHEMA as FINAL_LEDGER_SCOPE_SCHEMA

DEFAULTS = {
    "qwen_global_smoke": {
        "temperature": 0.0, "top_p": 1.0, "top_k": 64,
        "enable_thinking": False, "max_tokens": 2600, "attempts": 3,
    },
    "qwen_semantic": {
        "temperature": 0.0, "top_p": 1.0, "top_k": 64,
        "enable_thinking": False, "max_tokens": 1900, "attempts": 3,
        "batch_pids": 5, "context_before": 2, "context_after": 2,
    },
    "gemma_semantic": {
        "temperature": 0.0, "top_p": 1.0, "top_k": 64,
        "enable_thinking": True, "max_tokens": 1900, "attempts": 3,
        "batch_pids": 5, "context_before": 2, "context_after": 2,
    },
    "gemma_russian": {
        "temperature": 0.0, "top_p": 1.0, "top_k": 64,
        "enable_thinking": True, "max_tokens": 1800, "attempts": 3,
        "batch_pids": 6, "context_before": 3, "context_after": 3,
    },
    "gemma_discourse": {
        "temperature": 0.0, "top_p": 1.0, "top_k": 64,
        "enable_thinking": True, "max_tokens": 2600, "attempts": 3,
        "window_pids": 30, "overlap_pids": 10,
    },
}


def qwen_global_smoke_messages(runtime, cfg, work, blocks, block_map, translations, pids, pass_name):
    """One source-grounded chapter smoke, deliberately not a second cascade."""
    stage = stage_cfg(cfg, "qwen_global_smoke", DEFAULTS["qwen_global_smoke"])
    source_text = "\n".join(norm(block_map[pid].get("source_text")) for pid in pids)
    bible = read_json(work / "chapter_bible.json", {})
    system = f"""Ты — Qwen, source-grounded финальный smoke-аудитор главы EN→RU.
Это РОВНО ОДИН глобальный проход, не полный каскад аудитов и не литературная
редактура. Сравни фактический финальный текст с исходной главой. Для КАЖДОГО
PID верни results/status; отмечай только крупные блокирующие дефекты:
gross omissions, дубли или переставленные passages, сломанную continuity
субъекта/референта, важную несогласованность имени/термина, mixed-script или
English residue, грубую formatting corruption, chapter-level contradiction.
Не отмечай допустимый стиль и не предлагай полный переписанный перевод.

Верни JSON {{"results":[{{"pid":"p00001","status":"ok|issue","issues":[{{
"severity":"critical|major", "category":"missing|duplication|ordering|reference|entity_consistency|mixed_script|english_residue|formatting|continuity|meaning",
"source_span":"", "target_span":"", "problem":"", "required_invariant":"",
"repair_instruction":"локальная безопасная инструкция", "scope":"span|sentence|paragraph|cross_pid", "confidence":"high|medium|low"
}}]}}]}}.

ГЛОССАРИЙ:\n{glossary_prompt(runtime, cfg, source_text)}
БИБЛИЯ:\n{bible_prompt(runtime, cfg, source_text, bible)}"""
    user = "<FINAL_CHAPTER_SOURCE_AND_RU>\n" + render_pairs(block_map, translations, pids, True, "PAIR") + "\n</FINAL_CHAPTER_SOURCE_AND_RU>"
    return stage, [{"role": "system", "content": system}, {"role": "user", "content": user}]


def qwen_messages(runtime, cfg, work, blocks, block_map, translations, pids, pass_name):
    stage = stage_cfg(cfg, "qwen_semantic_audit", DEFAULTS["qwen_semantic"])
    before, after = chapter_context(
        blocks, translations, pids, int(stage["context_before"]), int(stage["context_after"])
    )
    all_pids = before + list(pids) + after
    source_text = "\n".join(norm(block_map[pid].get("source_text")) for pid in all_pids)
    bible = read_json(work / "chapter_bible.json", {})
    scene = scene_notes_for_pids(work, all_pids)
    residual = pass_name == "residual"
    system = f"""Ты — независимый двуязычный semantic-аудитор EN→RU.
Проверяй точность смысла, а не литературные предпочтения. Для КАЖДОГО PID
из TARGET_PIDS верни ровно одну запись results со status ok или issue.
Не ограничивай число issues искусственным максимумом. Если в PID несколько
независимых ошибок, перечисли все. Не отмечай допустимые стилистические
варианты.

Ищи: пропуски/добавления; неправильные субъект/объект; отрицание;
модальность; время и причинность; местоименные референты; конкретные
предметы; числа; родство; пол; говорящего/адресата; значение идиом и
фразовых глаголов; выдуманные детали. {'Это остаточный проход: отмечай только реально оставшиеся или новые ошибки в финальном кандидате.' if residual else ''}

Каждый issue должен задавать проверяемый required_invariant и по возможности
точные source_span/target_span. Не предлагай полный перевод абзаца.
Верни только JSON:
{{
 "results":[{{
   "pid":"p00001",
   "status":"ok|issue",
   "issues":[{{
     "severity":"critical|major|minor",
     "category":"meaning|missing|addition|subject|object|negation|modality|time|referent|idiom|entity|number|register",
     "source_span":"точный английский фрагмент",
     "target_span":"точный русский фрагмент",
     "problem":"кратко",
     "required_invariant":"какой смысл обязан сохраниться",
     "repair_instruction":"что исправить, не диктуя весь абзац",
     "scope":"span|sentence|paragraph|cross_pid",
     "confidence":"high|medium|low"
   }}]
 }}]
}}

ГЛОССАРИЙ:
{glossary_prompt(runtime, cfg, source_text)}

БИБЛИЯ:
{bible_prompt(runtime, cfg, source_text, bible)}

SOURCE SCENE NOTES (не считать безусловной истиной; использовать как подсказку):
{scene}
"""
    user = (
        "<TARGET_PIDS>" + ",".join(pids) + "</TARGET_PIDS>\n"
        "<CONTEXT_ONLY>\n" + render_pairs(block_map, translations, before + after, True, "CONTEXT") + "\n</CONTEXT_ONLY>\n"
        "<TARGET_PAIRS>\n" + render_pairs(block_map, translations, pids, True, "PAIR") + "\n</TARGET_PAIRS>"
    )
    return stage, [{"role": "system", "content": system}, {"role": "user", "content": user}]


def gemma_semantic_messages(runtime, cfg, work, blocks, block_map, translations, pids, pass_name):
    """Independent bilingual Gemma audit; intentionally does not see Qwen source notes."""
    stage = stage_cfg(cfg, "gemma_semantic_audit", DEFAULTS["gemma_semantic"])
    before, after = chapter_context(
        blocks, translations, pids, int(stage["context_before"]), int(stage["context_after"])
    )
    all_pids = before + list(pids) + after
    source_text = "\n".join(norm(block_map[pid].get("source_text")) for pid in all_pids)
    bible = read_json(work / "chapter_bible.json", {})
    residual = pass_name == "residual"
    system = f"""Ты — независимый двуязычный semantic-аудитор EN→RU на Gemma.
Не опирайся на выводы Qwen: ты их не видишь. Для КАЖДОГО PID из
TARGET_PIDS верни ровно одну запись results со status ok или issue.
Проверяй точность смысла, а не вкусовую редактуру. Не ограничивай число
issues и не отмечай допустимые варианты.

Ищи пропуски и добавления, неправильные субъект/объект, отрицание,
модальность, время/причинность, местоименные референты, числа, конкретные
предметы, родство, говорящего/адресата, идиомы и фразовые глаголы.
Особенно проверяй естественно звучащие русские фразы, которые передают не
тот смысл. {'Это остаточный проход: ищи только оставшиеся или новые ошибки.' if residual else ''}

Верни только JSON:
{{
 "results":[{{
   "pid":"p00001",
   "status":"ok|issue",
   "issues":[{{
     "severity":"critical|major|minor",
     "category":"meaning|missing|addition|subject|object|negation|modality|time|referent|idiom|entity|number|register",
     "source_span":"точный английский фрагмент",
     "target_span":"точный русский фрагмент",
     "problem":"кратко",
     "required_invariant":"какой смысл обязан сохраниться",
     "repair_instruction":"что локально исправить",
     "scope":"span|sentence|paragraph|cross_pid",
     "confidence":"high|medium|low"
   }}]
 }}]
}}

ГЛОССАРИЙ:
{glossary_prompt(runtime, cfg, source_text)}

БИБЛИЯ:
{bible_prompt(runtime, cfg, source_text, bible)}
"""
    user = (
        "<TARGET_PIDS>" + ",".join(pids) + "</TARGET_PIDS>\n"
        "<CONTEXT_ONLY>\n" + render_pairs(block_map, translations, before + after, True, "CONTEXT") + "\n</CONTEXT_ONLY>\n"
        "<TARGET_PAIRS>\n" + render_pairs(block_map, translations, pids, True, "PAIR") + "\n</TARGET_PAIRS>"
    )
    return stage, [{"role": "system", "content": system}, {"role": "user", "content": user}]


def gemma_local_messages(runtime, cfg, work, blocks, block_map, translations, pids, pass_name):
    stage = stage_cfg(cfg, "gemma_russian_audit", DEFAULTS["gemma_russian"])
    before, after = chapter_context(
        blocks, translations, pids, int(stage["context_before"]), int(stage["context_after"])
    )
    scene = dialogue_scene_notes(work, before + list(pids) + after)
    residual = pass_name == "residual"
    system = f"""Ты — независимый редактор русского литературного текста.
Не сравнивай перевод с английским и не пытайся заново переводить. Проверяй
только качество и внутреннюю связность русского. Для КАЖДОГО PID из
TARGET_PIDS верни ровно одну запись results со status ok или issue.

Ищи объективные проблемы: грамматика, управление, согласование, вид/время,
сломанная сочетаемость, буквальные кальки, неестественная реплика,
непонятная референция, внезапный регистр, несогласованное ты/вы, повтор или
обрыв. Не отмечай просто иной допустимый стиль. {'Это остаточный проход после правок: отмечай только оставшиеся или внесённые дефекты.' if residual else ''}

Верни только JSON:
{{
 "results":[{{
   "pid":"p00001",
   "status":"ok|issue",
   "issues":[{{
     "severity":"critical|major|minor",
     "category":"grammar|collocation|calque|aspect|tense|register|dialogue|reference|style",
     "target_span":"точный русский фрагмент",
     "problem":"кратко и объективно",
     "required_invariant":"какое русское качество должно быть восстановлено",
     "repair_instruction":"локальная инструкция",
     "scope":"span|sentence|paragraph|cross_pid",
     "confidence":"high|medium|low"
   }}]
 }}]
}}

SOURCE SCENE NOTES используются только для говорящего/адресата и обращения:
{scene}
"""
    user = (
        "<TARGET_PIDS>" + ",".join(pids) + "</TARGET_PIDS>\n"
        "<CONTEXT_ONLY>\n" + render_pairs(block_map, translations, before + after, False, "CONTEXT") + "\n</CONTEXT_ONLY>\n"
        "<TARGET_RU>\n" + render_pairs(block_map, translations, pids, False, "RU_BLOCK") + "\n</TARGET_RU>"
    )
    return stage, [{"role": "system", "content": system}, {"role": "user", "content": user}]


def discourse_messages(cfg, work, block_map, translations, pids, pass_name):
    stage = stage_cfg(cfg, "gemma_discourse_audit", DEFAULTS["gemma_discourse"])
    scene_map = dialogue_scene_notes(work, pids)
    residual = pass_name == "residual"
    system = f"""Ты — редактор связности русской главы. Проверяй большое
скользящее окно как сцену, но НЕ переписывай его. Верни coverage со всеми
переданными PID и только конкретные issues, привязанные к PID.

Ищи межабзацные проблемы: непоследовательное ты/вы для одной пары,
смена говорящего/адресата, разные формы одного имени/термина, логически
несовместимые соседние действия, сломанные переходы, голос персонажа,
местоимение без референта. Не отмечай локальную стилистическую вкусовщину.
{'Это остаточный проход после ремонта.' if residual else ''}

Верни только JSON:
{{
 "coverage":["p00001"],
 "issues":[{{
   "pid":"p00001",
   "related_pids":["p00002"],
   "severity":"critical|major|minor",
   "category":"register|speaker|addressee|term_consistency|reference|continuity|voice",
   "target_span":"точный русский фрагмент",
   "problem":"кратко",
   "required_invariant":"что должно быть согласовано",
   "repair_instruction":"какой PID и что локально изменить",
   "scope":"cross_pid",
   "confidence":"high|medium|low"
 }}]
}}

ADDRESS MATRIX И SOURCE NOTES:
{scene_map}
"""
    user = "<SCENE_WINDOW>\n" + render_pairs(block_map, translations, pids, False, "RU_BLOCK") + "\n</SCENE_WINDOW>"
    return stage, [{"role": "system", "content": system}, {"role": "user", "content": user}]


def _parse_issue_item(item: dict[str, Any], pid: str, detector: str, *, discourse: bool = False) -> dict[str, Any]:
    if not isinstance(item, dict):
        raise ValueError(f"issue for {pid} must be an object")
    problem = norm(item.get("problem"))
    category = norm(item.get("category"))
    severity = norm(item.get("severity")).casefold()
    confidence = norm(item.get("confidence")).casefold()
    scope = "cross_pid" if discourse else norm(item.get("scope")).casefold()
    if not problem or not category:
        raise ValueError(f"issue for {pid} lacks problem/category")
    if severity not in {"critical", "major", "minor"}:
        raise ValueError(f"invalid severity for {pid}: {severity}")
    if confidence not in {"high", "medium", "low"}:
        raise ValueError(f"invalid confidence for {pid}: {confidence}")
    if scope not in {"span", "sentence", "paragraph", "cross_pid"}:
        raise ValueError(f"invalid scope for {pid}: {scope}")
    metadata = {}
    if discourse:
        related = [norm(x) for x in (item.get("related_pids") or []) if norm(x)]
        metadata["related_pids"] = related
    return issue_record(
        pid=pid,
        severity=severity,
        category=category,
        problem=problem,
        detector=detector,
        source_span=norm(item.get("source_span")),
        target_span=norm(item.get("target_span")),
        required_invariant=norm(item.get("required_invariant")),
        repair_instruction=norm(item.get("repair_instruction")),
        scope=scope,
        confidence=confidence,
        metadata=metadata,
    )


def parse_per_pid(data: dict[str, Any], pids: list[str], detector: str) -> tuple[list[dict[str, Any]], list[str]]:
    if not isinstance(data, dict) or not isinstance(data.get("results"), list):
        raise ValueError("audit results must be a JSON list")
    allowed = set(pids)
    seen: set[str] = set()
    issues: list[dict[str, Any]] = []
    extras: list[str] = []
    for result in data["results"]:
        if not isinstance(result, dict):
            raise ValueError("every audit result must be an object")
        pid = norm(result.get("pid"))
        if pid not in allowed:
            extras.append(pid or "<empty>")
            continue
        if pid in seen:
            raise ValueError(f"duplicate audit PID: {pid}")
        seen.add(pid)
        status = norm(result.get("status")).casefold()
        raw_issues = result.get("issues") or []
        if not isinstance(raw_issues, list):
            raise ValueError(f"issues must be a list for {pid}")
        if status not in {"ok", "issue"}:
            raise ValueError(f"Invalid status for {pid}: {status}")
        if status == "ok" and raw_issues:
            raise ValueError(f"ok status contains issues for {pid}")
        if status == "issue" and not raw_issues:
            raise ValueError(f"Issue status without issues for {pid}")
        parsed = [_parse_issue_item(item, pid, detector) for item in raw_issues]
        if status == "issue" and not parsed:
            raise ValueError(f"No valid issues for {pid}")
        issues.extend(parsed)
    if extras:
        raise ValueError(f"Audit returned unexpected PIDs: {extras}")
    missing = [pid for pid in pids if pid not in seen]
    if missing:
        raise ValueError(f"Audit omitted PIDs: {missing}")
    return issues, list(pids)


def parse_discourse(data: dict[str, Any], pids: list[str], detector: str) -> tuple[list[dict[str, Any]], list[str]]:
    if not isinstance(data, dict) or not isinstance(data.get("coverage"), list):
        raise ValueError("discourse coverage must be a JSON list")
    coverage = [norm(x) for x in data["coverage"] if norm(x)]
    if len(coverage) != len(set(coverage)):
        raise ValueError("discourse coverage contains duplicate PIDs")
    if set(coverage) != set(pids):
        raise ValueError(f"Discourse coverage mismatch expected={pids}, got={coverage}")
    raw_issues = data.get("issues") or []
    if not isinstance(raw_issues, list):
        raise ValueError("discourse issues must be a list")
    issues: list[dict[str, Any]] = []
    allowed = set(pids)
    for item in raw_issues:
        if not isinstance(item, dict):
            raise ValueError("discourse issue must be an object")
        pid = norm(item.get("pid"))
        if pid not in allowed:
            raise ValueError(f"discourse issue PID outside window: {pid}")
        related = [norm(x) for x in (item.get("related_pids") or []) if norm(x)]
        if any(other not in allowed for other in related):
            raise ValueError(f"related PID outside discourse window for {pid}: {related}")
        issues.append(_parse_issue_item(item, pid, detector, discourse=True))
    return issues, list(pids)


def windows(pids: list[str], size: int, overlap: int):
    size = max(2, size)
    overlap = max(0, min(overlap, size - 1))
    step = size - overlap
    for start in range(0, len(pids), step):
        window = pids[start:start+size]
        if window:
            yield window
        if start + size >= len(pids):
            break


def scoped_ledger_paths(path: Path) -> dict[str, Path]:
    payload = read_json(path, {})
    if payload.get("schema") != FINAL_LEDGER_SCOPE_SCHEMA or not isinstance(payload.get("chapters"), list):
        raise ValueError(f"Invalid final ledger scope map: {path}")
    result: dict[str, Path] = {}
    for entry in payload["chapters"]:
        if not isinstance(entry, dict) or not entry.get("work_stem") or not entry.get("ledger_path"):
            raise ValueError(f"Invalid final ledger scope entry: {entry!r}")
        stem = str(entry["work_stem"])
        if stem in result:
            raise ValueError(f"Duplicate final ledger scope entry for chapter work stem: {stem}")
        result[stem] = Path(str(entry["ledger_path"]))
    return result


def ledger_target_pids(manifest_pids: list[str], ledger_path: Path, work_stem: str) -> list[str]:
    """Restrict a chapter to its own ledger; never substitute another chapter's."""
    if not ledger_path.is_file():
        raise FileNotFoundError(f"Final ledger is missing for selected chapter {work_stem}: {ledger_path}")
    requested = read_json(ledger_path, {})
    requested = requested.get("changed_pids", []) if isinstance(requested, dict) else requested
    if not isinstance(requested, list):
        raise ValueError("--pids-file must contain a list or changed_pids list")
    requested_set = {str(pid) for pid in requested}
    unknown = requested_set - set(manifest_pids)
    if unknown:
        raise ValueError(f"Final ledger contains unknown PIDs for selected chapter {work_stem}: {sorted(unknown)}")
    return [pid for pid in manifest_pids if pid in requested_set]


def scoped_ledger_path_for_work(ledger_paths: dict[str, Path], work: Path) -> Path:
    ledger_path = ledger_paths.get(work.name)
    if ledger_path is None:
        raise ValueError(f"Final ledger scope map has no entry for selected chapter: {work.name}")
    expected = (work / "v31_final_changed_pid_ledger.json").resolve()
    if ledger_path.resolve() != expected:
        raise ValueError(f"Final ledger scope map points outside selected chapter {work.name}: {ledger_path}")
    return ledger_path


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    add_common_args(parser)
    parser.add_argument("--mode", choices=list(DEFAULTS), required=True)
    parser.add_argument("--translations-file")
    parser.add_argument("--pids-file", help="JSON ledger or list restricting TARGET_PIDS; context remains adjacent manifest PIDs")
    parser.add_argument("--pids-map", type=Path, help="Canonical per-chapter final changed-PID ledger scope map")
    args = parser.parse_args()
    setup_logging()
    runtime = load_runtime(args.project_root.resolve())
    cfg = load_cfg(runtime, args.config.resolve())
    api_section = "reviewer_api" if args.mode in {"qwen_semantic", "qwen_global_smoke"} else "translator_api"
    client = api_client(runtime, cfg, api_section, args.mode, args.model)
    if args.pids_file and args.pids_map:
        raise ValueError("Use either --pids-file or --pids-map, not both")
    ledger_paths = scoped_ledger_paths(args.pids_map) if args.pids_map else {}

    for source_path, work in selected_chapters(runtime, cfg, args.start, args.end):
        _, blocks, block_map = load_manifest(work)
        translations = load_translations(work, args.pass_name, args.translations_file)
        detector = f"{args.mode}_{args.pass_name}"
        root = work / "v31" / args.pass_name / "audits" / args.mode
        consolidated = work / "v31" / args.pass_name / f"{args.mode}.json"
        root.mkdir(parents=True, exist_ok=True)
        all_issues: list[dict[str, Any]] = []
        covered: set[str] = set()
        pids = [str(block["pid"]) for block in blocks]
        ledger_path = Path(args.pids_file) if args.pids_file else (scoped_ledger_path_for_work(ledger_paths, work) if args.pids_map else None)
        if ledger_path:
            pids = ledger_target_pids(pids, ledger_path, work.name)

        if args.mode == "qwen_global_smoke":
            stage = stage_cfg(cfg, "qwen_global_smoke", DEFAULTS[args.mode])
            units = [pids]
        elif args.mode == "gemma_discourse":
            stage = stage_cfg(cfg, "gemma_discourse_audit", DEFAULTS["gemma_discourse"])
            units = list(windows(pids, int(stage["window_pids"]), int(stage["overlap_pids"])))
        else:
            section = {
                "qwen_semantic": "qwen_semantic_audit",
                "gemma_semantic": "gemma_semantic_audit",
                "gemma_russian": "gemma_russian_audit",
            }[args.mode]
            stage = stage_cfg(cfg, section, DEFAULTS[args.mode])
            units = list(batched(pids, int(stage["batch_pids"])))

        def evaluate_unit(unit_pids: list[str], cache_path: Path, label: str):
            if args.mode == "qwen_global_smoke":
                local_stage, messages = qwen_global_smoke_messages(runtime, cfg, work, blocks, block_map, translations, unit_pids, args.pass_name)
            elif args.mode == "qwen_semantic":
                local_stage, messages = qwen_messages(runtime, cfg, work, blocks, block_map, translations, unit_pids, args.pass_name)
            elif args.mode == "gemma_semantic":
                local_stage, messages = gemma_semantic_messages(runtime, cfg, work, blocks, block_map, translations, unit_pids, args.pass_name)
            elif args.mode == "gemma_russian":
                local_stage, messages = gemma_local_messages(runtime, cfg, work, blocks, block_map, translations, unit_pids, args.pass_name)
            else:
                local_stage, messages = discourse_messages(cfg, work, block_map, translations, unit_pids, args.pass_name)
            identity = cache_identity(
                producer="v31_audit", schema="audit-unit/v1",
                source={"chapter": source_path.name, "blocks": blocks},
                inputs={"pids": unit_pids, "translations": translations, "pass": args.pass_name, "mode": args.mode},
                config=local_stage, prompt=messages,
                profile={"model": args.model or cfg[api_section].get("model"), "api": api_section},
            )
            if not args.force:
                saved, reason = cache_reuse(cache_path, identity)
                if saved is not None:
                    return saved.get("issues") or [], saved.get("coverage") or []
                if cache_path.exists():
                    logging.info("Cache miss %s: %s", cache_path, reason)
            validator = (
                (lambda data, expected=unit_pids, source=detector: parse_discourse(data, expected, source))
                if args.mode == "gemma_discourse"
                else (lambda data, expected=unit_pids, source=detector: parse_per_pid(data, expected, source))
            )
            try:
                (local_issues, local_covered), attempts = complete_json(
                    runtime, client, messages, local_stage, int(local_stage["max_tokens"]),
                    f"{detector}:{source_path.stem}:{label}", int(local_stage["attempts"]),
                    validator=validator,
                )
                record = {
                    "version": VERSION,
                    "pids": unit_pids,
                    "coverage": local_covered,
                    "issues": local_issues,
                    "attempts": attempts,
                    "split": False,
                }
            except RuntimeError as exc:
                if args.mode in {"gemma_discourse", "qwen_global_smoke"} or len(unit_pids) <= 1:
                    raise
                midpoint = len(unit_pids) // 2
                left_pids, right_pids = unit_pids[:midpoint], unit_pids[midpoint:]
                logging.warning("%s failed for %s; splitting into %s + %s PID(s)", label, unit_pids, len(left_pids), len(right_pids))
                left_issues, left_covered = evaluate_unit(
                    left_pids, cache_path.with_name(cache_path.stem + "_a.json"), label + "a"
                )
                right_issues, right_covered = evaluate_unit(
                    right_pids, cache_path.with_name(cache_path.stem + "_b.json"), label + "b"
                )
                local_issues = left_issues + right_issues
                local_covered = left_covered + right_covered
                record = {
                    "version": VERSION,
                    "pids": unit_pids,
                    "coverage": local_covered,
                    "issues": local_issues,
                    "attempts": [{"ok": False, "error": str(exc), "split_recovery": True}],
                    "split": True,
                    "children": [left_pids, right_pids],
                }
            write_json(cache_path, with_cache_identity(record, identity))
            return local_issues, local_covered

        for index, unit in enumerate(units, 1):
            cache = root / f"unit_{index:04d}.json"
            unit_issues, unit_covered = evaluate_unit(unit, cache, f"unit{index}")
            all_issues.extend(unit_issues)
            covered.update(unit_covered)
            logging.info("%s %s: unit %s/%s, coverage=%s/%s, issues=%s", args.mode, source_path.name, index, len(units), len(covered), len(pids), len(all_issues))

        if covered != set(pids):
            missing = sorted(set(pids)-covered)
            raise RuntimeError(f"Incomplete {args.mode} coverage: {missing[:20]}")
        write_json(consolidated, {
            "version": VERSION,
            "chapter": source_path.name,
            "pass": args.pass_name,
            "detector": detector,
            "coverage": {"expected": len(pids), "completed": len(covered), "ok": True},
            "issue_count": len(all_issues),
            "issues": all_issues,
            "calls": client.calls,
        })
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
