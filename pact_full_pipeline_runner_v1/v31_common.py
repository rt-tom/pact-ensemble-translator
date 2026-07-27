#!/usr/bin/env python3
"""Shared utilities for Pact ensemble quality pipeline v3.1."""
from __future__ import annotations

import argparse
import copy
import hashlib
import importlib.util
import json
import logging
import os
import re
import sys
from dataclasses import asdict, is_dataclass
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator, Sequence


class JsonGenerationError(RuntimeError):
    """JSON generation failed after all retries; preserves per-attempt diagnostics."""

    def __init__(self, message: str, attempt_errors: list[dict[str, Any]]):
        super().__init__(message)
        self.attempt_errors = attempt_errors


VERSION = "3.1.3"
CACHE_IDENTITY_SCHEMA = "pact-v31-cache-identity/v1"


def read_json(path: Path, default: Any = None) -> Any:
    if not path.exists():
        return copy.deepcopy(default)
    return json.loads(path.read_text(encoding="utf-8-sig"))


def write_text_atomic(path: Path, text: str, *, validator: Callable[[str], Any] | None = None) -> None:
    """Durably publish UTF-8 text only after the complete temp file validates."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    try:
        with tmp.open("w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        published = tmp.read_text(encoding="utf-8")
        if published != text:
            raise ValueError(f"atomic write read-back mismatch: {path}")
        if validator is not None:
            validator(published)
        os.replace(tmp, path)
    finally:
        # A malformed/interrupted temp must never replace the last good artifact.
        if tmp.exists():
            tmp.unlink()


def write_json(path: Path, data: Any) -> None:
    if is_dataclass(data):
        data = asdict(data)
    write_text_atomic(
        path, json.dumps(data, ensure_ascii=False, indent=2) + "\n",
        validator=json.loads,
    )


def norm(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def fold(value: Any) -> str:
    return norm(value).casefold()


def sha256_json(value: Any) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def cache_identity(*, producer: str, schema: str, source: Any, inputs: Any,
                   config: Any, prompt: Any, profile: Any) -> dict[str, str]:
    """Identity is explicit and content-based; mtime is deliberately excluded."""
    fields = {
        "producer": producer, "schema": schema, "source": source,
        "inputs": inputs, "config": config, "prompt": prompt, "profile": profile,
    }
    return {
        "identity_schema": CACHE_IDENTITY_SCHEMA,
        "producer": producer,
        "schema": schema,
        **{f"{name}_sha256": sha256_json(value) for name, value in fields.items()
           if name not in {"producer", "schema"}},
    }


def cache_reuse(path: Path, expected: dict[str, str]) -> tuple[Any | None, str]:
    """Return a validated cache value or a machine-readable miss reason."""
    if not path.exists():
        return None, "missing"
    try:
        value = read_json(path)
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return None, "malformed_artifact"
    if not isinstance(value, dict):
        return None, "invalid_artifact_schema"
    actual = value.get("_cache_identity")
    if not isinstance(actual, dict):
        return None, "legacy_missing_identity"
    if actual.get("identity_schema") != CACHE_IDENTITY_SCHEMA:
        return None, "identity_schema_mismatch"
    for key, wanted in expected.items():
        if actual.get(key) != wanted:
            return None, f"identity_mismatch:{key}"
    return value, "reused"


def with_cache_identity(record: dict[str, Any], identity: dict[str, str]) -> dict[str, Any]:
    result = dict(record)
    result["_cache_identity"] = identity
    return result


def pid_num(pid: str) -> int:
    match = re.search(r"(\d+)$", pid or "")
    return int(match.group(1)) if match else 10**9


def batched(items: Sequence[Any], size: int) -> Iterator[list[Any]]:
    size = max(1, int(size))
    for offset in range(0, len(items), size):
        yield list(items[offset : offset + size])


def load_runtime(project_root: Path):
    module_path = project_root / "pact_translate_v3.py"
    spec = importlib.util.spec_from_file_location("pact_v31_runtime", module_path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Could not import {module_path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def load_cfg(runtime, config_path: Path) -> dict[str, Any]:
    return runtime.resolve_paths(
        runtime.merge(runtime.DEFAULTS, runtime.read_json(config_path, {})),
        config_path,
    )


def selected_chapters(runtime, cfg: dict[str, Any], start: int, end: int):
    for source_path in runtime.select_files(cfg, start, end):
        work = Path(cfg["paths"]["work_dir"]) / source_path.stem
        yield source_path, work


def load_manifest(work: Path) -> tuple[dict[str, Any], list[dict[str, Any]], dict[str, dict[str, Any]]]:
    manifest = read_json(work / "manifest.json", {})
    blocks = list(manifest.get("blocks") or [])
    if not blocks:
        raise FileNotFoundError(f"Manifest has no blocks: {work / 'manifest.json'}")
    block_map = {str(item["pid"]): item for item in blocks}
    return manifest, blocks, block_map


def translation_path(work: Path, pass_name: str, explicit: str | None = None) -> Path:
    if explicit:
        path = work / explicit
        if path.exists():
            return path
    if pass_name == "residual":
        for name in (
            "v31_primary_translations.json",
            "repaired_translations.json",
            "draft_translations.json",
        ):
            path = work / name
            if path.exists():
                return path
    return work / "draft_translations.json"


def load_translations(work: Path, pass_name: str, explicit: str | None = None) -> dict[str, str]:
    path = translation_path(work, pass_name, explicit)
    data = read_json(path, {})
    if not isinstance(data, dict):
        raise ValueError(f"Translations must be an object: {path}")
    return {str(k): norm(v) for k, v in data.items()}


def chapter_context(blocks: list[dict[str, Any]], translations: dict[str, str], pids: Sequence[str], before: int, after: int) -> tuple[list[str], list[str]]:
    positions = {str(block["pid"]): index for index, block in enumerate(blocks)}
    if not pids:
        return [], []
    first = min(positions[pid] for pid in pids)
    last = max(positions[pid] for pid in pids)
    before_pids = [str(b["pid"]) for b in blocks[max(0, first-before):first]]
    after_pids = [str(b["pid"]) for b in blocks[last+1:last+1+after]]
    return before_pids, after_pids


def render_pairs(
    block_map: dict[str, dict[str, Any]],
    translations: dict[str, str],
    pids: Iterable[str],
    include_source: bool = True,
    tag: str = "PAIR",
) -> str:
    rows: list[str] = []
    for pid in pids:
        block = block_map[pid]
        source = norm(block.get("source_text"))
        target = norm(translations.get(pid, ""))
        body = f"<RU>{target}</RU>"
        if include_source:
            body = f"<EN>{source}</EN>\n" + body
        rows.append(f'<{tag} pid="{pid}">\n{body}\n</{tag}>')
    return "\n".join(rows)


def scene_notes_for_pids(work: Path, pids: Iterable[str]) -> dict[str, Any]:
    scene_map = read_json(work / "source_scene_map.json", {})
    by_pid = scene_map.get("by_pid") or {}
    return {pid: by_pid.get(pid, {}) for pid in pids if by_pid.get(pid)}




def strict_bool(value: Any, field: str) -> bool:
    """Accept only real JSON booleans, never truthy strings/numbers."""
    if type(value) is not bool:
        raise ValueError(f"{field} must be a JSON boolean, got {type(value).__name__}")
    return value


def dialogue_scene_notes(work: Path, pids: Iterable[str]) -> dict[str, Any]:
    """Return only relevant dialogue/register facts for the requested PIDs.

    The persistent address ledger may grow across a long book. Passing the full
    matrix to every local prompt wastes context and can make the model apply an
    unrelated relationship. Keep only pairs whose participants occur in the
    requested window.
    """
    scene_map = read_json(work / "source_scene_map.json", {})
    by_pid = scene_map.get("by_pid") or {}
    slim: dict[str, dict[str, Any]] = {}
    participants: set[str] = set()
    for pid in pids:
        item = by_pid.get(pid) or {}
        if not isinstance(item, dict):
            continue
        speaker = norm(item.get("speaker")) or None
        addressee = norm(item.get("addressee")) or None
        row = {
            "speaker": speaker,
            "addressee": addressee,
            "dialogue": bool(item.get("dialogue", False)),
            "confidence": item.get("confidence", "low"),
        }
        if speaker:
            participants.add(speaker)
        if addressee:
            participants.add(addressee)
        if row["speaker"] or row["addressee"] or row["dialogue"]:
            slim[pid] = row

    relevant_matrix: dict[str, Any] = {}
    for key, value in (scene_map.get("address_matrix") or {}).items():
        if not isinstance(value, dict):
            continue
        speaker = norm(value.get("speaker"))
        addressee = norm(value.get("addressee"))
        if speaker and addressee and speaker in participants and addressee in participants:
            relevant_matrix[str(key)] = value
    return {"by_pid": slim, "address_matrix": relevant_matrix}


def glossary_prompt(runtime, cfg: dict[str, Any], source_text: str) -> str:
    return runtime.Glossary(cfg).prompt(source_text)


def bible_prompt(runtime, cfg: dict[str, Any], source_text: str, chapter_bible: dict[str, Any]) -> str:
    book = runtime.BookBible(Path(cfg["paths"]["book_bible_file"]))
    return book.prompt(source_text, chapter_bible)


def stage_cfg(cfg: dict[str, Any], name: str, defaults: dict[str, Any]) -> dict[str, Any]:
    result = copy.deepcopy(defaults)
    section = cfg.get("ensemble_v31", {}).get(name, {})
    if isinstance(section, dict):
        result.update(section)
    return result


def api_client(runtime, cfg: dict[str, Any], api_section: str, name: str, model: str | None = None):
    api_cfg = copy.deepcopy(cfg[api_section])
    if model:
        api_cfg["model"] = model
    return runtime.ApiClient(api_cfg, name)


def complete_json(
    runtime, client, messages: list[dict[str, str]], stage: dict[str, Any],
    max_tokens: int, label: str, attempts: int = 3, validator=None,
    length_retry_max_tokens: int | None = None,
    retry_guidance: str | None = None,
) -> tuple[Any, list[dict[str, Any]]]:
    """Generate JSON and optionally validate/transform its schema inside retries."""
    errors: list[dict[str, Any]] = []
    base_max_tokens = int(max_tokens)
    length_retry_budget = (
        max(base_max_tokens, int(length_retry_max_tokens))
        if length_retry_max_tokens is not None else None
    )
    current_max_tokens = base_max_tokens
    for attempt in range(1, max(1, attempts) + 1):
        retry_messages = messages
        if attempt > 1:
            previous = errors[-1] if errors else {}
            if length_retry_budget is not None and previous.get("finish_reason") == "length":
                current_max_tokens = length_retry_budget
                retry_instruction = (
                    "Предыдущий ответ был обрезан лимитом длины "
                    "(finish_reason=length). Сгенерируй заново один полный "
                    "компактный JSON object. Не продолжай и не дописывай "
                    "предыдущий текст. Верни только JSON строго по указанной "
                    "схеме, без markdown, комментариев и текста вне JSON."
                )
            else:
                prior = previous.get("error", "invalid response")
                retry_instruction = (
                    "Предыдущий ответ был невалидным: " + str(prior)[:800] +
                    ". Верни только краткий JSON, строго по указанной схеме, "
                    "со всеми обязательными PID/полями, без markdown и комментариев."
                )
            if retry_guidance:
                retry_instruction += " " + retry_guidance.strip()
            retry_messages = list(messages) + [{
                "role": "system",
                "content": retry_instruction,
            }]
        generation = None
        actual_max_tokens = None
        try:
            actual_max_tokens = runtime.fit_output_budget(
                client, retry_messages, stage, current_max_tokens,
            )
            generation = client.complete(
                retry_messages, stage, actual_max_tokens,
                f"{label}:attempt{attempt}",
            )
            if length_retry_budget is not None and generation.finish_reason == "length":
                raise ValueError(
                    "finish_reason='length': response was truncated; "
                    "refusing to parse or repair it"
                )
            parsed = runtime.safe_json_loads(generation.content)
            value = validator(parsed) if validator is not None else parsed
            return value, errors + [{
                "attempt": attempt,
                "ok": True,
                "max_tokens": actual_max_tokens,
                "finish_reason": generation.finish_reason,
                "usage": generation.usage,
                "wall_seconds": round(generation.wall_seconds, 3),
            }]
        except Exception as exc:
            logging.warning("%s attempt %s failed: %s", label, attempt, exc)
            item: dict[str, Any] = {
                "attempt": attempt,
                "ok": False,
                "error": str(exc),
                "max_tokens": actual_max_tokens,
            }
            if generation is not None:
                item.update({
                    "finish_reason": generation.finish_reason,
                    "usage": generation.usage,
                    "wall_seconds": round(generation.wall_seconds, 3),
                })
            errors.append(item)
    raise JsonGenerationError(
        f"{label} failed after {attempts} attempts: {errors[-1] if errors else 'unknown'}",
        errors,
    )


def issue_record(
    *,
    pid: str,
    category: str,
    problem: str,
    detector: str,
    severity: str = "major",
    source_span: str = "",
    target_span: str = "",
    required_invariant: str = "",
    repair_instruction: str = "",
    scope: str = "span",
    confidence: str = "",
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    severity = fold(severity)
    if severity not in {"critical", "major", "minor"}:
        severity = "major"
    scope = fold(scope)
    if scope not in {"span", "sentence", "paragraph", "cross_pid"}:
        scope = "span"
    result = {
        "pid": pid,
        "severity": severity,
        "category": norm(category)[:80] or "meaning",
        "problem": norm(problem)[:1200],
        "source_span": norm(source_span)[:600],
        "target_span": norm(target_span)[:600],
        "required_invariant": norm(required_invariant)[:1200],
        "repair_instruction": norm(repair_instruction)[:1200],
        "scope": scope,
        "detected_by": [detector],
        "detector_confidence": fold(confidence),
        "metadata": metadata or {},
    }
    return result


def issue_fingerprint(issue: dict[str, Any]) -> str:
    # PID and target span are strongest. Problem/category provide fallback.
    target = fold(issue.get("target_span"))
    source = fold(issue.get("source_span"))
    problem = fold(issue.get("problem"))
    category = fold(issue.get("category"))
    compact = re.sub(r"[^\wа-яё]+", " ", target or source or problem)[:160]
    return f"{issue.get('pid','')}|{category}|{compact}"


def _merge_text_values(values: Iterable[Any]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        text = norm(value)
        folded = fold(text)
        if text and folded not in seen:
            result.append(text)
            seen.add(folded)
    return result


def merge_duplicate_issues(issues: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Merge only demonstrably duplicate findings.

    Exact fingerprints may merge across detector families. Span containment is
    only a fallback inside the same normalized category and never by itself
    proves independent detector agreement.
    """
    merged: list[dict[str, Any]] = []
    by_pid: dict[str, list[dict[str, Any]]] = {}
    for issue in sorted(issues, key=lambda x: (pid_num(str(x.get("pid"))), fold(x.get("category")))):
        pid = str(issue.get("pid") or "")
        if not pid or not norm(issue.get("problem")):
            continue
        candidates = by_pid.setdefault(pid, [])
        chosen = None
        merge_reason = ""
        fp = issue_fingerprint(issue)
        category = fold(issue.get("category"))
        for existing in candidates:
            if issue_fingerprint(existing) == fp:
                chosen = existing
                merge_reason = "fingerprint"
                break
            a = fold(issue.get("target_span"))
            b = fold(existing.get("target_span"))
            same_category = category == fold(existing.get("category"))
            if (
                same_category
                and a and b
                and (a == b or (min(len(a), len(b)) > 8 and (a in b or b in a)))
            ):
                chosen = existing
                merge_reason = "span_overlap"
                break
        if chosen is None:
            chosen = copy.deepcopy(issue)
            chosen["detected_by"] = list(dict.fromkeys(chosen.get("detected_by") or []))
            chosen["source_issues"] = [copy.deepcopy(issue)]
            chosen["merge_reasons"] = []
            chosen["merge_reason"] = "single"
            candidates.append(chosen)
            merged.append(chosen)
        else:
            chosen["detected_by"] = list(dict.fromkeys(
                list(chosen.get("detected_by") or []) + list(issue.get("detected_by") or [])
            ))
            chosen.setdefault("source_issues", []).append(copy.deepcopy(issue))
            chosen.setdefault("merge_reasons", []).append(merge_reason)
            reasons = set(chosen.get("merge_reasons") or [])
            chosen["merge_reason"] = next(iter(reasons)) if len(reasons) == 1 else "mixed"
            order = {"minor": 0, "major": 1, "critical": 2}
            if order.get(fold(issue.get("severity")), 1) > order.get(fold(chosen.get("severity")), 1):
                chosen["severity"] = issue.get("severity")
            for key in ("source_span", "target_span"):
                if len(norm(issue.get(key))) > len(norm(chosen.get(key))):
                    chosen[key] = issue.get(key)

        source_issues = chosen.get("source_issues") or [chosen]
        invariants = _merge_text_values(
            src.get("required_invariant") for src in source_issues if isinstance(src, dict)
        )
        instructions = _merge_text_values(
            src.get("repair_instruction") for src in source_issues if isinstance(src, dict)
        )
        chosen["required_invariants"] = invariants
        chosen["repair_instructions"] = instructions
        if invariants:
            chosen["required_invariant"] = " | ".join(invariants)
        if instructions:
            chosen["repair_instruction"] = " | ".join(instructions)

    for index, issue in enumerate(merged, 1):
        issue["issue_id"] = f"v31i{index:05d}"
        issue["agreement_count"] = len(set(issue.get("detected_by") or []))
    return merged


def add_common_args(parser: argparse.ArgumentParser, *, include_pass: bool = True) -> None:
    parser.add_argument("--project-root", type=Path, required=True)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--start", type=int, required=True)
    parser.add_argument("--end", type=int, required=True)
    if include_pass:
        parser.add_argument("--pass-name", choices=["primary", "residual", "final"], default="primary")
    parser.add_argument("--model")
    parser.add_argument("--force", action="store_true")


def setup_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s | %(message)s",
        force=True,
    )
