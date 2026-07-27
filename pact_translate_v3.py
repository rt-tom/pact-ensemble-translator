#!/usr/bin/env python3
"""Pact Translator v3: translate -> issue-only audit -> targeted repair."""
from __future__ import annotations

import argparse
import copy
import hashlib
import html
import json
import logging
import math
import os
import re
import shutil
import sys
import tempfile
import time
from collections import Counter, defaultdict, deque
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Optional

import requests
from bs4 import BeautifulSoup, Tag

__version__ = "3.1.2d"

DEFAULTS: dict[str, Any] = {
    "translator_api": {
        "chat_url": "http://127.0.0.1:8080/v1/chat/completions",
        "token_count_url": "http://127.0.0.1:8080/v1/chat/completions/input_tokens",
        "model": "gemma-4-26B-A4B-it-UD-Q4_K_XL.gguf",
        "timeout_seconds": 1800,
        "http_retries": 3,
        "retry_delay_seconds": 8,
        "context_size": 32768,
        "context_safety_margin": 3072,
        "reasoning_format": "deepseek",
    },
    "reviewer_api": {
        "enabled": True,
        "chat_url": "http://127.0.0.1:8080/v1/chat/completions",
        "token_count_url": "http://127.0.0.1:8080/v1/chat/completions/input_tokens",
        "model": "gemma-4-26B-A4B-it-UD-Q4_K_XL.gguf",
        "timeout_seconds": 1800,
        "http_retries": 3,
        "retry_delay_seconds": 8,
        "context_size": 32768,
        "context_safety_margin": 3072,
        "reasoning_format": "deepseek",
    },
    "paths": {
        "input_dir": "./pact_chapters",
        "output_dir": "./pact_translated_v3",
        "work_dir": "./pact_work_v3",
        "logs_dir": "./logs_v3",
        "glossary_dir": "./glossary",
        "run_glossary_candidate_ledger": "./pact_work_v3/glossary_candidates.run.json",
        "book_glossary_candidate_ledger": "./glossary_candidates.json",
        "arc_names_file": "./arc_names.json",
        "book_bible_file": "./book_bible.json",
    },
    "chunking": {
        "target_words": 900,
        "min_words": 450,
        "max_words": 1200,
        "previous_blocks": 3,
        "following_blocks": 2,
        "relevant_earlier_blocks": 2,
        "minimum_recursive_words": 180,
    },
    "chapter_bible": {
        "enabled": True,
        "required": False,
        "max_tokens": 2600,
        "temperature": 0.15,
        "top_p": 0.9,
        "top_k": 40,
        "enable_thinking": False,
    },
    "translation": {
        "temperature": 0.3,
        "top_p": 0.95,
        "top_k": 64,
        "enable_thinking": False,
        "output_multiplier": 3.0,
        "output_reserve": 500,
        "min_output_tokens": 1200,
        "max_output_tokens": 5600,
        "generation_retries": 3,
        "split_after_attempt": 2,
    },
    "audit": {
        "enabled": True,
        "required": False,
        "temperature": 0.0,
        "top_p": 1.0,
        "top_k": 64,
        "enable_thinking": False,
        "max_tokens": 1200,
        "generation_retries": 2,
        "batch_pids": 8,
        "context_before": 2,
        "context_after": 2,
        "max_issues_per_batch": 5,
        "include_deterministic_suspects": False,
        "split_on_failure": True,
        "fail_open": True,
        "minimum_success_rate": 0.90,
    },
    "repair": {
        "enabled": True,
        "required": False,
        "temperature": 0.2,
        "top_p": 0.9,
        "top_k": 48,
        "enable_thinking": False,
        "max_tokens": 2600,
        "generation_retries": 2,
        "max_pids_per_call": 6,
        "context_before": 1,
        "context_after": 1,
        "auto_repair_severities": ["critical", "major"],
        "auto_repair_verified_decisions": ["repair"],
        "auto_repair_verifier_confidences": ["high", "deterministic"],
        "retry_on_keep_or_invalid": True,
        "auto_repair_deterministic": True,
        "auto_repair_deterministic_categories": [
            "missing", "number", "english_residue",
            "entity_consistency", "narrator_gender",
            "name_consistency", "tone_profanity"
        ],
    },
    "formatting": {
        "enabled": True,
        "required": False,
        "temperature": 0.1,
        "top_p": 0.9,
        "top_k": 32,
        "enable_thinking": False,
        "max_tokens": 1600,
        "generation_retries": 2,
        "tags": ["em", "strong", "i", "b", "a"],
        # Formatting is semantic by default.  Cosmetic tags must be opted in
        # explicitly here rather than being silently downgraded on failure.
        "required_tags": ["em", "strong", "i", "b", "a"],
        "optional_tags": [],
        "max_blocks_per_call": 12,
        "retry_unresolved_spans": True,
        "on_failure": "omit_tag",
    },
    "validation": {
        "accepted_finish_reasons": ["stop"],
        "strict_digits": True,
        "digit_mismatch_is_error": False,
        "min_length_ratio": 0.30,
        "max_length_ratio": 3.2,
        "english_sequence_min_words": 5,
        "english_residue_is_error": False,
        "duplicate_is_error": False,
    },
    "deterministic_qa": {
        "enabled": True,
        "profanity_check": True,
        "number_words_check": True,
        "entity_check": True,
        "narrator_gender_check": True,
        "length_outlier_check": True,
        "mixed_script_check": True,
        "mixed_script_allow": [],
    },
    "glossary": {
        "locked_file": "locked.json",
        "established_file": "established.json",
        "provisional_file": "provisional.json",
        "conflicts_file": "conflicts.json",
        "proper_name_min_occurrences": 2,
        "term_min_chapters": 2,
        "term_min_occurrences": 3,
        "include_provisional_in_prompt": True,
    },
    "html": {
        "block_tags": ["h1", "h2", "h3", "h4", "h5", "h6", "p", "li", "figcaption"],
        "remove_tags": ["script", "style", "noscript", "nav", "footer"],
        "remove_selectors": [
            ".sharedaddy", ".post-navigation", ".entry-meta",
            ".comments-area", ".jp-relatedposts"
        ],
        "output_css": (
            "body{font-family:Georgia,serif;max-width:800px;"
            "margin:2em auto;line-height:1.65;padding:0 1em}"
        ),
        "remove_data_pid": True,
    },
    "style": {
        "book_title": "Pact",
        "author": "Wildbow / J.C. McCrae",
        "rules": [
            "Сохраняй плотность и ритм тёмного городского фэнтези.",
            "Не смягчай ругань и не повышай литературный регистр без причины.",
            "Не добавляй объяснений и не устраняй авторскую двусмысленность.",
            "Не подменяй точные предметы, родственные связи, пол, время и возраст.",
            "Диалоги должны звучать современно и естественно.",
            "Не привязывай POV автоматически к Блэйку: используй библию главы.",
        ],
        "example": (
            "«Чёрт. К чёрту их. К чёрту это всё».\n\n"
            "Несомненно, это была машина родителей или дяди. "
            "Её поставили поперёк подъездной дороги у подножия длинного "
            "подъёма к Дому-на-Холме."
        ),
    },
}


@dataclass
class InlineSpan:
    span_id: str
    tag: str
    source_text: str
    attrs: dict[str, Any]
    required: bool = True


@dataclass
class Block:
    pid: str
    index: int
    tag: str
    source_html: str
    source_text: str
    word_count: int
    digits: list[str]
    inline_spans: list[InlineSpan] = field(default_factory=list)


@dataclass
class Chunk:
    chunk_id: str
    pids: list[str]
    word_count: int


@dataclass
class Generation:
    content: str
    reasoning: str
    finish_reason: Optional[str]
    usage: dict[str, Any]
    timings: dict[str, Any]
    wall_seconds: float


@dataclass
class Issue:
    pid: str
    severity: str
    category: str
    problem: str
    repair_instruction: str = ""
    suggested_text: str = ""
    source: str = "reviewer"
    deterministic: bool = False
    status: str = "open"
    issue_id: str = ""
    verifier_decision: str = ""
    verifier_confidence: str = ""
    verifier_reason: str = ""
    verifier_repair_goal: str = ""


class PipelineError(RuntimeError):
    pass


def merge(a: dict[str, Any], b: dict[str, Any]) -> dict[str, Any]:
    out = copy.deepcopy(a)
    for key, value in b.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = merge(out[key], value)
        else:
            out[key] = value
    return out


def read_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return copy.deepcopy(default)
    with path.open("r", encoding="utf-8-sig") as f:
        return json.load(f)


def atomic_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8", newline="\n") as f:
        f.write(text)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def atomic_json(path: Path, data: Any) -> None:
    atomic_text(path, json.dumps(data, ensure_ascii=False, indent=2))


def norm(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def count_words(text: str) -> int:
    return len(re.findall(
        r"[A-Za-zА-Яа-яЁё0-9]+(?:['’\-][A-Za-zА-Яа-яЁё0-9]+)*",
        text,
    ))


def digits(text: str) -> list[str]:
    return re.findall(r"(?<![\w])\d+(?:[.,]\d+)*(?![\w])", text)


def natural_key(value: str) -> list[Any]:
    return [
        int(piece) if piece.isdigit() else piece.casefold()
        for piece in re.split(r"(\d+)", value)
    ]


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def clean_json_text(text: str) -> str:
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.I)
        text = re.sub(r"\s*```$", "", text)
    start = text.find("{")
    end = text.rfind("}")
    if start >= 0 and end >= start:
        text = text[start:end + 1]
    return text.strip()


def safe_json_loads(text: str) -> dict[str, Any]:
    try:
        value = json.loads(clean_json_text(text))
    except Exception as exc:
        raise PipelineError(f"Invalid JSON response: {text[:500]!r}") from exc
    if not isinstance(value, dict):
        raise PipelineError("JSON response must be an object.")
    return value


class ApiClient:
    def __init__(self, cfg: dict[str, Any], name: str):
        self.cfg = cfg
        self.name = name
        self.session = requests.Session()
        self.calls: list[dict[str, Any]] = []

    def _post(self, url: str, payload: dict[str, Any]) -> requests.Response:
        last: Optional[Exception] = None
        for attempt in range(1, int(self.cfg["http_retries"]) + 1):
            try:
                response = self.session.post(
                    url, json=payload,
                    timeout=int(self.cfg["timeout_seconds"]),
                )
                if not response.ok:
                    detail = norm(response.text)[:2000]
                    raise requests.HTTPError(
                        f"{response.status_code} {response.reason}; "
                        f"body={detail!r}",
                        response=response,
                    )
                return response
            except Exception as exc:
                last = exc
                logging.warning(
                    "%s HTTP attempt %s failed: %s",
                    self.name, attempt, exc,
                )
                if attempt < int(self.cfg["http_retries"]):
                    time.sleep(float(self.cfg["retry_delay_seconds"]))
        raise PipelineError(f"{self.name} API failed: {last}")

    def payload(
        self, messages: list[dict[str, str]],
        stage: dict[str, Any], max_tokens: int,
    ) -> dict[str, Any]:
        return {
            "model": self.cfg["model"],
            "messages": messages,
            "max_tokens": int(max_tokens),
            "temperature": float(stage["temperature"]),
            "top_p": float(stage["top_p"]),
            "top_k": int(stage["top_k"]),
            "stream": False,
            "chat_template_kwargs": {
                "enable_thinking": bool(stage.get("enable_thinking", False))
            },
            "reasoning_format": self.cfg.get("reasoning_format", "deepseek"),
        }

    def token_count(
        self, messages: list[dict[str, str]], stage: dict[str, Any]
    ) -> int:
        payload = self.payload(messages, stage, 1)
        payload.pop("max_tokens", None)
        try:
            result = self._post(
                self.cfg["token_count_url"], payload
            ).json()
            return int(result["input_tokens"])
        except Exception as exc:
            estimate = math.ceil(
                sum(len(m["content"]) for m in messages) / 2.6
            )
            logging.warning(
                "%s token endpoint unavailable (%s); estimate=%s",
                self.name, exc, estimate,
            )
            return estimate

    def complete(
        self, messages: list[dict[str, str]],
        stage: dict[str, Any], max_tokens: int,
        call_label: str,
    ) -> Generation:
        payload = self.payload(messages, stage, max_tokens)
        payload["response_format"] = {"type": "json_object"}
        started = time.perf_counter()
        data = self._post(self.cfg["chat_url"], payload).json()
        wall = time.perf_counter() - started
        try:
            choice = data["choices"][0]
            message = choice["message"]
        except Exception as exc:
            raise PipelineError(f"Malformed API response: {data}") from exc
        generation = Generation(
            content=message.get("content") or "",
            reasoning=message.get("reasoning_content") or "",
            finish_reason=choice.get("finish_reason"),
            usage=data.get("usage") or {},
            timings=data.get("timings") or {},
            wall_seconds=wall,
        )
        self.calls.append({
            "client": self.name,
            "label": call_label,
            "finish_reason": generation.finish_reason,
            "usage": generation.usage,
            "timings": generation.timings,
            "wall_seconds": round(wall, 3),
            "reasoning_chars": len(generation.reasoning),
            "created_at": utc_now(),
        })
        return generation


class Glossary:
    def __init__(self, cfg: dict[str, Any]):
        self.cfg = cfg["glossary"]
        self.directory = Path(cfg["paths"]["glossary_dir"])
        self.directory.mkdir(parents=True, exist_ok=True)
        self.locked_path = self.directory / self.cfg["locked_file"]
        self.established_path = self.directory / self.cfg["established_file"]
        self.provisional_path = self.directory / self.cfg["provisional_file"]
        self.conflicts_path = self.directory / self.cfg["conflicts_file"]
        self.locked = read_json(self.locked_path, {})
        self.established = read_json(self.established_path, {})
        self.provisional = read_json(self.provisional_path, {})
        self.conflicts = read_json(self.conflicts_path, {})
        self.candidate_ledger = GlossaryCandidateLedger(
            Path(cfg["paths"]["run_glossary_candidate_ledger"]),
            Path(cfg["paths"]["book_glossary_candidate_ledger"]),
        )

    @staticmethod
    def target(record: Any) -> Optional[str]:
        if isinstance(record, str):
            return record
        if isinstance(record, dict) and isinstance(record.get("target"), str):
            return record["target"]
        return None

    def all_known(self) -> dict[str, str]:
        result: dict[str, str] = {}
        for source, target in self.locked.items():
            if isinstance(target, str):
                result[source] = target
        for collection in (self.established, self.provisional):
            for source, record in collection.items():
                target = self.target(record)
                if target and source not in result:
                    result[source] = target
        return result

    def prompt(self, relevant_source: str) -> str:
        folded = relevant_source.casefold()
        lines: list[str] = []
        for source, target in sorted(
            self.locked.items(), key=lambda item: item[0].casefold()
        ):
            lines.append(f"- [LOCKED] {source} → {target}")
        for source, record in sorted(
            self.established.items(), key=lambda item: item[0].casefold()
        ):
            target = self.target(record)
            if target and source.casefold() in folded and source not in self.locked:
                lines.append(f"- [ESTABLISHED] {source} → {target}")
        if self.cfg.get("include_provisional_in_prompt", True):
            for source, record in sorted(
                self.provisional.items(), key=lambda item: item[0].casefold()
            ):
                target = self.target(record)
                if target and source.casefold() in folded and source not in self.locked:
                    lines.append(
                        f"- [PROVISIONAL: verify] {source} → {target}"
                    )
        return "\n".join(lines) or "(пусто)"

    def legacy_update(
        self, chapter_name: str, source_text: str,
        candidates: list[dict[str, Any]],
    ) -> dict[str, int]:
        stats = {"candidates": 0, "promoted": 0, "conflicts": 0}
        proper_types = {
            "person", "character", "proper_name", "location", "place",
            "organization", "family", "entity", "animal"
        }
        for raw in candidates:
            source = norm(str(raw.get("english") or raw.get("source") or ""))
            target = norm(str(raw.get("russian") or raw.get("target") or ""))
            kind = norm(str(raw.get("type") or "other")).casefold()
            if (
                not source or not target or source in self.locked
                or "/" in target or "→" in target or count_words(source) > 7
            ):
                continue
            occurrence_count = len(
                re.findall(re.escape(source), source_text, flags=re.I)
            )
            if occurrence_count == 0:
                continue
            existing = self.target(self.established.get(source))
            if existing and existing != target:
                conflict = self.conflicts.setdefault(
                    source, {"variants": {}, "chapters": []}
                )
                conflict["variants"][target] = (
                    conflict["variants"].get(target, 0) + occurrence_count
                )
                if chapter_name not in conflict["chapters"]:
                    conflict["chapters"].append(chapter_name)
                stats["conflicts"] += 1
                continue
            record = self.provisional.setdefault(
                source,
                {
                    "target": target, "type": kind,
                    "total_occurrences": 0, "chapters": [], "variants": {},
                },
            )
            record["total_occurrences"] += occurrence_count
            if chapter_name not in record["chapters"]:
                record["chapters"].append(chapter_name)
            record["variants"][target] = (
                record["variants"].get(target, 0) + occurrence_count
            )
            stats["candidates"] += 1
            no_conflict = len(record["variants"]) == 1
            promote_name = (
                kind in proper_types
                and occurrence_count >= int(
                    self.cfg["proper_name_min_occurrences"]
                )
                and no_conflict
            )
            promote_term = (
                len(record["chapters"]) >= int(
                    self.cfg["term_min_chapters"]
                )
                and record["total_occurrences"] >= int(
                    self.cfg["term_min_occurrences"]
                )
                and no_conflict
            )
            if promote_name or promote_term:
                self.established[source] = {
                    "target": target, "type": kind, "confidence": "auto",
                    "chapters": record["chapters"],
                    "total_occurrences": record["total_occurrences"],
                }
                self.provisional.pop(source, None)
                stats["promoted"] += 1
        atomic_json(self.established_path, self.established)
        atomic_json(self.provisional_path, self.provisional)
        atomic_json(self.conflicts_path, self.conflicts)
        return stats

    def update(
        self, chapter_name: str, source_text: str,
        candidates: list[dict[str, Any]],
    ) -> dict[str, int]:
        return self.candidate_ledger.observe_chapter(
            chapter_name, source_text, candidates,
            stage="chapter_bible", detector="translator",
        )


class GlossaryCandidateLedger:
    """Append-only, non-authoritative proposals kept outside the glossary."""

    VERSION = 1

    def __init__(self, run_path: Path, book_path: Path):
        self.run_path, self.book_path = run_path, book_path

    @staticmethod
    def _empty() -> dict[str, Any]:
        return {"version": GlossaryCandidateLedger.VERSION, "candidates": {}}

    @staticmethod
    def _identity(prefix: str, value: Any) -> str:
        encoded = json.dumps(value, ensure_ascii=False, sort_keys=True,
                             separators=(",", ":")).encode("utf-8")
        return prefix + hashlib.sha256(encoded).hexdigest()[:20]

    @classmethod
    def candidate_id(cls, source: str, kind: str) -> str:
        return cls._identity("glc_", {"source": source.casefold(), "type": kind.casefold()})

    @staticmethod
    def _load(path: Path) -> dict[str, Any]:
        data = read_json(path, GlossaryCandidateLedger._empty())
        if not isinstance(data, dict) or not isinstance(data.get("candidates"), dict):
            raise PipelineError(f"Invalid glossary candidate ledger: {path}")
        return data

    @staticmethod
    def _merge(data: dict[str, Any], incoming: dict[str, Any]) -> dict[str, int]:
        stats = {"candidates": 0, "observations": 0, "conflicts": 0}
        all_records = data.setdefault("candidates", {})
        for candidate_id, record in incoming.items():
            current = all_records.setdefault(candidate_id, {
                "candidate_id": candidate_id, "source": record["source"],
                "type": record["type"], "status": "candidate",
                "proposals": {}, "observations": [],
            })
            if current.get("status") not in {"candidate", "rejected", "promoted"}:
                raise PipelineError(f"Invalid glossary candidate status: {candidate_id}")
            known = {item["observation_id"] for item in current["observations"]}
            for observation in record["observations"]:
                proposal = observation["proposed_translation"]
                proposal_state = current["proposals"].setdefault(proposal, {
                    "sightings": 0, "observation_ids": [], "alternatives": [],
                })
                for alternative in observation["alternatives"]:
                    if alternative not in proposal_state["alternatives"]:
                        proposal_state["alternatives"].append(alternative)
                proposal_state["sightings"] += 1
                if observation["observation_id"] not in known:
                    current["observations"].append(observation)
                    proposal_state["observation_ids"].append(observation["observation_id"])
                    known.add(observation["observation_id"])
                    stats["observations"] += 1
            stats["candidates"] += 1
            stats["conflicts"] += int(len(current["proposals"]) > 1)
        return stats

    def observe_chapter(self, chapter: str, source_text: str,
                        raw_candidates: list[dict[str, Any]], *, stage: str,
                        detector: str) -> dict[str, int]:
        incoming: dict[str, Any] = {}
        for raw in raw_candidates:
            source = norm(str(raw.get("english") or raw.get("source") or ""))
            target = norm(str(raw.get("russian") or raw.get("target") or ""))
            kind = norm(str(raw.get("type") or "other")).casefold()
            occurrences = len(re.findall(re.escape(source), source_text, flags=re.I)) if source else 0
            if not source or not target or not occurrences or "/" in target or "→" in target:
                continue
            alternatives = [norm(str(item)) for item in raw.get("alternatives", [])
                            if norm(str(item)) and norm(str(item)) != target]
            observation = {
                "proposed_translation": target, "alternatives": alternatives,
                "confidence": raw.get("confidence"),
                "provenance": {
                    "chapter": chapter,
                    "pids": sorted({str(pid) for pid in raw.get("source_pids", [])}),
                    "stage": stage, "detector": detector,
                    "model": str(raw.get("model") or ""),
                    "evidence": raw.get("evidence") or source,
                    "occurrences": occurrences,
                },
            }
            observation["observation_id"] = self._identity("obs_", observation)
            candidate_id = self.candidate_id(source, kind)
            incoming.setdefault(candidate_id, {
                "source": source, "type": kind, "observations": [],
            })["observations"].append(observation)
        run = self._load(self.run_path)
        stats = self._merge(run, incoming)
        atomic_json(self.run_path, run)
        book = self._load(self.book_path)
        self._merge(book, incoming)
        atomic_json(self.book_path, book)
        stats["promoted"] = 0
        return stats


class BookBible:
    def __init__(self, path: Path):
        self.path = path
        self.data = read_json(path, {
            "version": 1, "characters": {}, "entities": {},
            "address_register": [], "facts": [], "chapters": [],
        })

    def merge_chapter(self, chapter: str, bible: dict[str, Any]) -> None:
        if chapter not in self.data["chapters"]:
            self.data["chapters"].append(chapter)
        for section in ("characters", "entities"):
            for item in bible.get(section) or []:
                if not isinstance(item, dict):
                    continue
                source = norm(str(item.get("source") or item.get("english") or ""))
                if not source:
                    continue
                current = self.data[section].setdefault(source, {
                    "target": item.get("target") or item.get("russian") or "",
                    "type": item.get("type") or section[:-1],
                    "gender": item.get("gender") or "unknown",
                    "notes": [], "chapters": [], "variants": {},
                })
                target = norm(str(item.get("target") or item.get("russian") or ""))
                if target:
                    current["variants"][target] = (
                        current["variants"].get(target, 0) + 1
                    )
                    if not current.get("target"):
                        current["target"] = target
                if item.get("gender") and current.get("gender") in {
                    "", "unknown", None
                }:
                    current["gender"] = item["gender"]
                notes = item.get("notes")
                if isinstance(notes, str) and notes and notes not in current["notes"]:
                    current["notes"].append(notes)
                if chapter not in current["chapters"]:
                    current["chapters"].append(chapter)
        for item in bible.get("address_register") or []:
            if isinstance(item, dict) and item not in self.data["address_register"]:
                self.data["address_register"].append(item)
        for item in bible.get("facts") or []:
            if isinstance(item, dict) and item not in self.data["facts"]:
                self.data["facts"].append(item)
        atomic_json(self.path, self.data)

    def prompt(self, relevant_source: str, chapter_bible: dict[str, Any]) -> str:
        folded = relevant_source.casefold()
        selected: dict[str, Any] = {
            "chapter_pov": chapter_bible.get("pov") or {},
            "characters": [], "entities": [],
            "address_register": chapter_bible.get("address_register") or [],
            "facts": chapter_bible.get("facts") or [],
        }
        for section in ("characters", "entities"):
            seen: set[str] = set()
            for item in chapter_bible.get(section) or []:
                if not isinstance(item, dict):
                    continue
                source = norm(str(item.get("source") or item.get("english") or ""))
                if source and source.casefold() in folded:
                    selected[section].append(item)
                    seen.add(source.casefold())
            for source, item in self.data.get(section, {}).items():
                if source.casefold() in folded and source.casefold() not in seen:
                    selected[section].append({"source": source, **item})
        return json.dumps(selected, ensure_ascii=False, indent=2)


def leaf_blocks(soup: BeautifulSoup, names: list[str]) -> list[Tag]:
    allowed = set(names)
    result: list[Tag] = []
    for tag in soup.find_all(names):
        if not isinstance(tag, Tag) or not norm(tag.get_text(" ", strip=True)):
            continue
        if any(
            isinstance(child, Tag) and child.name in allowed
            for child in tag.find_all(names)
        ):
            continue
        result.append(tag)
    return result


def extract_inline_spans(
    tag: Tag, names: list[str], required_tags: list[str], optional_tags: list[str],
) -> list[InlineSpan]:
    counters: Counter[str] = Counter()
    result: list[InlineSpan] = []
    for child in tag.find_all(names):
        if not isinstance(child, Tag):
            continue
        source_text = norm(child.get_text(" ", strip=True))
        if not source_text:
            continue
        counters[child.name] += 1
        attrs = {
            key: value for key, value in child.attrs.items()
            if key in {"href", "title", "lang", "class"}
        }
        result.append(InlineSpan(
            span_id=f"{child.name}{counters[child.name]:02d}",
            tag=child.name, source_text=source_text, attrs=attrs,
            required=child.name in required_tags and child.name not in optional_tags,
        ))
    return result


def prepare_html(raw: str, cfg: dict[str, Any]) -> tuple[str, list[Block]]:
    soup = BeautifulSoup(raw, "html.parser")
    for name in cfg["html"]["remove_tags"]:
        for tag in soup.find_all(name):
            tag.decompose()
    for selector in cfg["html"]["remove_selectors"]:
        try:
            for tag in soup.select(selector):
                tag.decompose()
        except Exception as exc:
            logging.warning("Bad selector %s: %s", selector, exc)
    blocks: list[Block] = []
    for index, tag in enumerate(
        leaf_blocks(soup, cfg["html"]["block_tags"]), 1
    ):
        pid = f"p{index:05d}"
        tag["data-pid"] = pid
        source_text = norm(tag.get_text(" ", strip=True))
        blocks.append(Block(
            pid=pid, index=index - 1, tag=tag.name,
            source_html=str(tag), source_text=source_text,
            word_count=count_words(source_text), digits=digits(source_text),
            inline_spans=extract_inline_spans(
                tag, cfg["formatting"]["tags"],
                cfg["formatting"].get("required_tags", cfg["formatting"]["tags"]),
                cfg["formatting"].get("optional_tags", []),
            ),
        ))
    if not blocks:
        raise PipelineError("No translatable blocks found.")
    return str(soup), blocks


def make_chunks(blocks: list[Block], cfg: dict[str, Any]) -> list[Chunk]:
    minimum = int(cfg["min_words"])
    target = int(cfg["target_words"])
    maximum = int(cfg["max_words"])
    if not (0 < minimum <= target <= maximum):
        raise PipelineError(
            "Require 0 < min_words <= target_words <= max_words."
        )
    groups: list[list[Block]] = []
    current: list[Block] = []
    current_words = 0

    def flush() -> None:
        nonlocal current, current_words
        if current:
            groups.append(current)
            current, current_words = [], 0

    for block in blocks:
        if current and current_words + block.word_count > maximum:
            flush()
        current.append(block)
        current_words += block.word_count
        if current_words >= target:
            flush()
    flush()
    if len(groups) >= 2:
        tail = sum(block.word_count for block in groups[-1])
        if tail < minimum:
            prior = sum(block.word_count for block in groups[-2])
            if prior + tail <= maximum:
                groups[-2].extend(groups[-1])
                groups.pop()
            else:
                while (
                    len(groups[-2]) > 1
                    and sum(b.word_count for b in groups[-1]) < minimum
                ):
                    candidate = groups[-2][-1]
                    new_tail = (
                        sum(b.word_count for b in groups[-1])
                        + candidate.word_count
                    )
                    new_prior = (
                        sum(b.word_count for b in groups[-2])
                        - candidate.word_count
                    )
                    if new_tail > maximum or new_prior < minimum:
                        break
                    groups[-1].insert(0, groups[-2].pop())
    return [
        Chunk(
            chunk_id=f"c{index:04d}",
            pids=[block.pid for block in group],
            word_count=sum(block.word_count for block in group),
        )
        for index, group in enumerate(groups, 1)
    ]


def split_chunk(chunk: Chunk, block_map: dict[str, Block]) -> tuple[Chunk, Chunk]:
    total = 0
    target = chunk.word_count / 2
    split_index = 1
    for index, pid in enumerate(chunk.pids[:-1], 1):
        total += block_map[pid].word_count
        split_index = index
        if total >= target:
            break
    left_pids = chunk.pids[:split_index]
    right_pids = chunk.pids[split_index:]
    if not left_pids or not right_pids:
        raise PipelineError(f"Cannot split {chunk.chunk_id}.")
    return (
        Chunk(
            f"{chunk.chunk_id}a", left_pids,
            sum(block_map[p].word_count for p in left_pids),
        ),
        Chunk(
            f"{chunk.chunk_id}b", right_pids,
            sum(block_map[p].word_count for p in right_pids),
        ),
    )


def blocks_to_manifest(blocks: list[Block]) -> list[dict[str, Any]]:
    return [asdict(block) for block in blocks]


def blocks_from_manifest(data: list[dict[str, Any]]) -> list[Block]:
    result = []
    for item in data:
        copied = dict(item)
        copied["inline_spans"] = [
            InlineSpan(**span) for span in copied.get("inline_spans", [])
        ]
        result.append(Block(**copied))
    return result


def fit_output_budget(
    client: ApiClient, messages: list[dict[str, str]],
    stage: dict[str, Any], proposed: int,
) -> int:
    input_tokens = client.token_count(messages, stage)
    limit = (
        int(client.cfg["context_size"])
        - int(client.cfg["context_safety_margin"])
        - input_tokens
    )
    if limit < 256:
        raise PipelineError(
            f"Prompt too large: {input_tokens} tokens for "
            f"context {client.cfg['context_size']}."
        )
    return max(256, min(int(proposed), limit))


def block_lines(pids: Iterable[str], block_map: dict[str, Block]) -> str:
    return "\n".join(
        f'<BLOCK pid="{pid}">{html.escape(block_map[pid].source_text)}</BLOCK>'
        for pid in pids
    )


def translation_lines(
    pids: Iterable[str], translations: dict[str, str]
) -> str:
    return "\n".join(
        f'<RU pid="{pid}">{html.escape(translations[pid])}</RU>'
        for pid in pids if pid in translations
    )


def arcs_text(cfg: dict[str, Any]) -> str:
    data = read_json(Path(cfg["paths"]["arc_names_file"]), {})
    return "\n".join(
        f"- {source} → {target}" for source, target in data.items()
    ) or "(нет)"


def sanitize_generated_bible(
    bible: dict[str, Any], glossary: Glossary,
) -> tuple[dict[str, Any], list[dict[str, str]]]:
    result = copy.deepcopy(bible) if isinstance(bible, dict) else {}
    known = glossary.all_known()
    folded_known = {key.casefold(): value for key, value in known.items()}
    changes: list[dict[str, str]] = []

    def expected_for(source: str) -> str:
        return known.get(source) or folded_known.get(source.casefold()) or ""

    def latin_only(value: str) -> bool:
        return bool(re.search(r"[A-Za-z]", value)) and not bool(
            re.search(r"[А-Яа-яЁё]", value)
        )

    for section in ("characters", "entities", "terms"):
        items = result.get(section) or []
        if not isinstance(items, list):
            continue
        for item in items:
            if not isinstance(item, dict):
                continue
            source = norm(str(item.get("source") or item.get("english") or ""))
            if not source:
                continue
            field = "russian" if "russian" in item and "target" not in item else "target"
            old = norm(str(item.get(field) or ""))
            expected = expected_for(source)
            if expected:
                if old != expected:
                    changes.append({
                        "section": section, "source": source,
                        "old": old, "new": expected,
                        "action": "glossary_override",
                    })
                item[field] = expected
            elif old and latin_only(old) and re.search(r"[A-Za-z]", source):
                changes.append({
                    "section": section, "source": source,
                    "old": old, "new": "",
                    "action": "remove_latin_placeholder",
                })
                item[field] = ""
                forbidden = item.setdefault("forbidden_targets", [])
                if old not in forbidden:
                    forbidden.append(old)
    result["target_sanitization"] = {
        "enabled": True,
        "changes": changes,
        "rule": (
            "Glossary overrides are authoritative; Latin-only placeholders "
            "are removed from Russian target fields."
        ),
    }
    return result, changes


def chapter_bible_messages(
    blocks: list[Block], glossary: Glossary, book_bible: BookBible
) -> list[dict[str, str]]:
    source = "\n".join(
        f"[{block.pid}] {block.source_text}" for block in blocks
    )
    system = """Проанализируй главу художественного текста до перевода.
Создай factual-библию и ничего не додумывай. Строго JSON:
{
 "summary":"...",
 "pov":{"source_name":"","target_name":"","gender":"male|female|unknown","person":"first|third"},
 "characters":[{"source":"","target":"","type":"character","gender":"male|female|unknown","notes":""}],
 "entities":[{"source":"","target":"","type":"vehicle|animal|place|object|term",
              "gender":"male|female|unknown","notes":"","forbidden_targets":[]}],
 "address_register":[{"from":"","to":"","register":"ты|вы|unknown","source_pids":[]}],
 "facts":[{"fact":"","source_pids":[]}],
 "terms":[{"english":"","russian":"","type":"character|term|place|entity",
           "source_pids":[],"evidence":"","alternatives":[],"confidence":null}]
}
Особенно фиксируй пол, родство, транспорт, животных, возраст, точное время,
числа, ты/вы и устойчивые имена.
Поле target/russian предназначено только для русского написания. Никогда не
копируй английское имя или термин в target как заглушку. Если русского варианта
нет в GLOSSARY/KNOWN_BOOK_BIBLE и ты не уверен, оставь target пустым; не создавай
латинскую форму как норму для русского перевода."""
    user = (
        "<KNOWN_BOOK_BIBLE>\n"
        + json.dumps(book_bible.data, ensure_ascii=False, indent=2)
        + "\n</KNOWN_BOOK_BIBLE>\n<GLOSSARY>\n"
        + glossary.prompt(source)
        + "\n</GLOSSARY>\n<CHAPTER>\n"
        + source
        + "\n</CHAPTER>"
    )
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]


def chunk_context(
    chunk: Chunk, blocks: list[Block],
    cfg: dict[str, Any], glossary: Glossary,
) -> tuple[list[str], list[str], list[str]]:
    positions = {block.pid: block.index for block in blocks}
    start = positions[chunk.pids[0]]
    end = positions[chunk.pids[-1]]
    previous = [
        block.pid for block in blocks[
            max(0, start - int(cfg["chunking"]["previous_blocks"])):start
        ]
    ]
    following = [
        block.pid for block in blocks[
            end + 1:end + 1 + int(cfg["chunking"]["following_blocks"])
        ]
    ]
    target_text = " ".join(
        blocks[positions[pid]].source_text for pid in chunk.pids
    )
    keywords = set(re.findall(
        r"\b[A-Z][A-Za-z’'\-]{2,}\b", target_text
    ))
    keywords.update(
        term for term in glossary.all_known()
        if term.casefold() in target_text.casefold()
    )
    excluded = set(previous + following + chunk.pids)
    scored = []
    for block in blocks[:start]:
        if block.pid in excluded:
            continue
        score = sum(
            1 for key in keywords
            if key.casefold() in block.source_text.casefold()
        )
        if score >= 2:
            scored.append((score, block.index, block.pid))
    scored.sort(key=lambda item: (-item[0], -item[1]))
    relevant = [
        item[2] for item in
        scored[:int(cfg["chunking"]["relevant_earlier_blocks"])]
    ]
    relevant.sort(key=lambda pid: positions[pid])
    return previous, following, relevant


def translation_messages(
    cfg: dict[str, Any], glossary: Glossary, book_bible: BookBible,
    chapter_bible: dict[str, Any], source_scene_map: dict[str, Any], chunk: Chunk,
    block_map: dict[str, Block], previous: list[str],
    following: list[str], relevant: list[str],
    accepted: dict[str, str], feedback: Optional[list[str]],
) -> list[dict[str, str]]:
    relevant_source = "\n".join(
        block_map[pid].source_text
        for pid in chunk.pids + previous + following + relevant
    )
    rules = "\n".join(f"- {rule}" for rule in cfg["style"]["rules"])
    system = f"""Ты — профессиональный литературный переводчик EN→RU для
веб-романа «{cfg['style']['book_title']}» ({cfg['style']['author']}).

ПРАВИЛА:
{rules}
- Сохраняй субъект, объект, отрицание, модальность, точное время, возраст,
  род, родственные связи и конкретные предметы.
- Не превращай мотоцикл в велосипед.
- Не цензурируй fuck/shit/cunt.
- Не меняй ты/вы без основания.
- Цифры можно естественно передавать словами, но числовое значение не теряй.
- Форматирование сейчас не обрабатывай: верни только чистый текст.

АРКИ:
{arcs_text(cfg)}

ГЛОССАРИЙ:
{glossary.prompt(relevant_source)}

БИБЛИЯ:
{book_bible.prompt(relevant_source, chapter_bible)}

Строго JSON:
{{"translations":[{{"pid":"p00001","text":"русский текст"}}]}}
Каждый TARGET PID ровно один раз, в исходном порядке. Без HTML и комментариев.

ЭТАЛОН:
{cfg['style']['example']}"""
    sections: list[str] = []
    scene_by_pid = (source_scene_map or {}).get("by_pid") or {}
    scene_notes = {
        pid: scene_by_pid.get(pid)
        for pid in chunk.pids
        if scene_by_pid.get(pid)
    }
    full_address_matrix = (source_scene_map or {}).get("address_matrix") or {}
    participants = {
        norm(value.get(field) or "")
        for value in scene_notes.values()
        if isinstance(value, dict)
        for field in ("speaker", "addressee")
        if norm(value.get(field) or "")
    }
    address_matrix = {
        str(key): value
        for key, value in full_address_matrix.items()
        if isinstance(value, dict)
        and norm(value.get("speaker") or "") in participants
        and norm(value.get("addressee") or "") in participants
    }
    if scene_notes or address_matrix:
        sections += [
            "<SOURCE_ANALYSIS_DO_NOT_TRANSLATE>",
            json.dumps({
                "target_pid_notes": scene_notes,
                "address_matrix": address_matrix,
            }, ensure_ascii=False, separators=(",", ":")),
            "</SOURCE_ANALYSIS_DO_NOT_TRANSLATE>",
        ]
    if relevant:
        sections += [
            "<RELEVANT_EARLIER_SOURCE>",
            block_lines(relevant, block_map),
            "</RELEVANT_EARLIER_SOURCE>",
        ]
    if previous:
        sections += [
            "<PREVIOUS_SOURCE>", block_lines(previous, block_map),
            "</PREVIOUS_SOURCE>",
        ]
        previous_ru = translation_lines(previous, accepted)
        if previous_ru:
            sections += [
                "<PREVIOUS_TRANSLATION>", previous_ru,
                "</PREVIOUS_TRANSLATION>",
            ]
    if following:
        sections += [
            "<FOLLOWING_SOURCE_DO_NOT_TRANSLATE>",
            block_lines(following, block_map),
            "</FOLLOWING_SOURCE_DO_NOT_TRANSLATE>",
        ]
    if feedback:
        sections += [
            "<FAILED_ATTEMPT_ERRORS>",
            "\n".join(f"- {error}" for error in feedback),
            "</FAILED_ATTEMPT_ERRORS>",
        ]
    sections += [
        f'<TARGET id="{chunk.chunk_id}">',
        block_lines(chunk.pids, block_map),
        "</TARGET>",
    ]
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": "\n".join(sections)},
    ]


def parse_translation_response(
    generation: Generation, expected_pids: list[str],
) -> tuple[dict[str, str], list[str]]:
    data = safe_json_loads(generation.content)
    raw = data.get("translations")
    if isinstance(raw, dict):
        records = [{"pid": pid, "text": text} for pid, text in raw.items()]
    elif isinstance(raw, list):
        records = [item for item in raw if isinstance(item, dict)]
    else:
        return {}, ["missing translations list/object"]
    result: dict[str, str] = {}
    order: list[str] = []
    for record in records:
        pid = str(record.get("pid") or "")
        text = norm(str(record.get("text") or ""))
        if pid and pid not in result:
            result[pid] = text
            order.append(pid)
    allowed = set(expected_pids)
    filtered_order = [pid for pid in order if pid in allowed]
    errors = []
    if filtered_order != expected_pids:
        errors.append(
            f"PID mismatch expected={expected_pids}, got={filtered_order}"
        )
    return {
        pid: result[pid] for pid in expected_pids if pid in result
    }, errors


def detect_english_sentence(text: str, minimum: int) -> str:
    for sentence in re.split(r"(?<=[.!?])\s+|\n+", text):
        latin = re.findall(
            r"\b[A-Za-z]+(?:['’\-][A-Za-z]+)*\b", sentence
        )
        cyrillic = re.findall(
            r"\b[А-Яа-яЁё]+(?:['’\-][А-Яа-яЁё]+)*\b", sentence
        )
        if len(latin) >= minimum and len(latin) >= max(1, len(cyrillic) * 2):
            return sentence
    return ""


_URL_OR_EMAIL_RE = re.compile(
    r"(?:https?://|www\.)\S+|"
    r"\b[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}\b",
    re.I,
)
_SCRIPT_TOKEN_RE = re.compile(
    r"[A-Za-zА-Яа-яЁё][A-Za-zА-Яа-яЁё'’\-]*"
)


def mixed_script_tokens(text: str, allow: Iterable[str] = ()) -> list[str]:
    """Return Latin or mixed Latin/Cyrillic tokens in Russian target text.

    URLs and email addresses are ignored. Intentional Latin tokens must be
    listed explicitly in deterministic_qa.mixed_script_allow.
    """
    allowed = {norm(str(item)).casefold() for item in allow if norm(str(item))}
    cleaned = _URL_OR_EMAIL_RE.sub(" ", text or "")
    result: list[str] = []
    seen: set[str] = set()
    for token in _SCRIPT_TOKEN_RE.findall(cleaned):
        if not re.search(r"[A-Za-z]", token):
            continue
        folded = token.casefold()
        if folded in allowed or folded in seen:
            continue
        seen.add(folded)
        result.append(token)
    return result


RU_DIGIT_EQUIVALENTS: dict[str, re.Pattern[str]] = {
    "0": re.compile(r"\b(?:нол(?:ь|я|ю|ём|е)|нул(?:евой|евая|евое|евых?))\b", re.I),
    "1": re.compile(r"\b(?:один|одна|одно|одну|одного|одной|перв\w*)\b", re.I),
    "2": re.compile(r"\b(?:два|две|двух|втор\w*)\b", re.I),
    "3": re.compile(r"\b(?:три|тр[её]х|трет\w*)\b", re.I),
    "4": re.compile(r"\b(?:четыре|четыр[её]х|четв[её]рт\w*)\b", re.I),
    "5": re.compile(r"\b(?:пять|пяти|пят\w*)\b", re.I),
    "6": re.compile(r"\b(?:шесть|шести|шест\w*)\b", re.I),
    "7": re.compile(r"\b(?:семь|семи|седьм\w*)\b", re.I),
    "8": re.compile(r"\b(?:восемь|восьми|восьм\w*)\b", re.I),
    "9": re.compile(r"\b(?:девять|девяти|девят\w*)\b", re.I),
    "10": re.compile(r"\b(?:десять|десяти|десят\w*)\b", re.I),
    "11": re.compile(r"\b(?:одиннадцать|одиннадцати|одиннадцат\w*)\b", re.I),
    "12": re.compile(r"\b(?:двенадцать|двенадцати|двенадцат\w*)\b", re.I),
    "13": re.compile(r"\b(?:тринадцать|тринадцати|тринадцат\w*)\b", re.I),
    "14": re.compile(r"\b(?:четырнадцать|четырнадцати|четырнадцат\w*)\b", re.I),
    "15": re.compile(r"\b(?:пятнадцать|пятнадцати|пятнадцат\w*)\b", re.I),
    "16": re.compile(r"\b(?:шестнадцать|шестнадцати|шестнадцат\w*)\b", re.I),
    "17": re.compile(r"\b(?:семнадцать|семнадцати|семнадцат\w*)\b", re.I),
    "18": re.compile(r"\b(?:восемнадцать|восемнадцати|восемнадцат\w*)\b", re.I),
    "19": re.compile(r"\b(?:девятнадцать|девятнадцати|девятнадцат\w*)\b", re.I),
    "20": re.compile(r"\b(?:двадцать|двадцати|двадцат\w*)\b", re.I),
}


def missing_numeric_values(
    source_values: list[str], target_text: str,
) -> list[str]:
    """Return source numeric values with no exact or word-form equivalent."""
    target_counts = Counter(digits(target_text))
    missing: list[str] = []
    for value, count in Counter(source_values).items():
        exact_count = target_counts.get(value, 0)
        remaining = max(0, count - exact_count)
        if remaining == 0:
            continue
        pattern = RU_DIGIT_EQUIVALENTS.get(value)
        word_count = len(pattern.findall(target_text)) if pattern else 0
        remaining = max(0, remaining - word_count)
        missing.extend([value] * remaining)
    return missing


def validate_translation_map(
    translations: dict[str, str], chunk: Chunk,
    block_map: dict[str, Block], cfg: dict[str, Any],
) -> tuple[bool, list[str], list[str]]:
    errors: list[str] = []
    warnings: list[str] = []
    source_chars = target_chars = 0
    normalized: dict[str, str] = {}
    for pid in chunk.pids:
        source = block_map[pid]
        target = norm(translations.get(pid, ""))
        if not target:
            errors.append(f"{pid}: empty or missing")
            continue
        if cfg["validation"]["strict_digits"]:
            missing_values = missing_numeric_values(
                source.digits, target
            )
            if missing_values:
                message = (
                    f"{pid}: numeric values missing {missing_values}; "
                    f"source={source.digits}, target_digits={digits(target)}"
                )
                if cfg["validation"].get(
                    "digit_mismatch_is_error", False
                ):
                    errors.append(message)
                else:
                    warnings.append(message)
        source_chars += len(source.source_text)
        target_chars += len(target)
        normalized[pid] = re.sub(r"[\W_]+", "", target).casefold()
        english = detect_english_sentence(
            target, int(cfg["validation"]["english_sequence_min_words"])
        )
        if english:
            message = f"{pid}: possible English residue: {english[:120]}"
            if cfg["validation"]["english_residue_is_error"]:
                errors.append(message)
            else:
                warnings.append(message)
    if source_chars:
        ratio = target_chars / source_chars
        if (
            ratio < float(cfg["validation"]["min_length_ratio"])
            or ratio > float(cfg["validation"]["max_length_ratio"])
        ):
            errors.append(f"chunk length ratio={ratio:.2f}")
    pids = list(normalized)
    for index, left in enumerate(pids):
        for right in pids[index + 1:]:
            if (
                len(normalized[left]) > 30
                and normalized[left] == normalized[right]
                and block_map[left].source_text != block_map[right].source_text
            ):
                message = f"{left}/{right}: duplicate translation"
                if cfg["validation"]["duplicate_is_error"]:
                    errors.append(message)
                else:
                    warnings.append(message)
    return not errors, errors, warnings


def output_budget_for_chunk(chunk: Chunk, cfg: dict[str, Any]) -> int:
    stage = cfg["translation"]
    value = math.ceil(
        chunk.word_count * float(stage["output_multiplier"])
        + int(stage["output_reserve"])
    )
    return max(
        int(stage["min_output_tokens"]),
        min(int(stage["max_output_tokens"]), value),
    )


def generate_translation_recursive(
    chunk: Chunk, client: ApiClient, cfg: dict[str, Any],
    glossary: Glossary, book_bible: BookBible,
    chapter_bible: dict[str, Any], source_scene_map: dict[str, Any], blocks: list[Block],
    block_map: dict[str, Block], accepted: dict[str, str],
    drafts_dir: Optional[Path] = None,
    meta_dir: Optional[Path] = None,
) -> tuple[dict[str, str], dict[str, Any]]:
    """Translate a chunk, caching successful recursive leaves immediately."""
    draft_path = (
        drafts_dir / f"{chunk.chunk_id}.json"
        if drafts_dir is not None else None
    )
    meta_path = (
        meta_dir / f"{chunk.chunk_id}.translation.json"
        if meta_dir is not None else None
    )
    split_path = (
        meta_dir / f"{chunk.chunk_id}.split.json"
        if meta_dir is not None else None
    )

    if draft_path is not None and draft_path.exists():
        saved = read_json(draft_path, {})
        cached = saved.get("translations") or {}
        if all(pid in cached for pid in chunk.pids):
            logging.info("translation %s loaded from recursive cache", chunk.chunk_id)
            meta = read_json(meta_path, {}) if meta_path else {}
            return (
                {pid: cached[pid] for pid in chunk.pids},
                meta or {
                    "chunk_id": chunk.chunk_id,
                    "cached": True,
                    "split": False,
                },
            )

    # If an earlier run already established that this parent needs splitting,
    # do not waste time retrying the large parent again on resume.
    if split_path is not None and split_path.exists():
        left, right = split_chunk(chunk, block_map)
        left_map, left_meta = generate_translation_recursive(
            left, client, cfg, glossary, book_bible, chapter_bible, source_scene_map,
            blocks, block_map, accepted, drafts_dir, meta_dir,
        )
        right_map, right_meta = generate_translation_recursive(
            right, client, cfg, glossary, book_bible, chapter_bible, source_scene_map,
            blocks, block_map, {**accepted, **left_map},
            drafts_dir, meta_dir,
        )
        combined = {**left_map, **right_map}
        meta = {
            "chunk_id": chunk.chunk_id,
            "cached_split": True,
            "split": True,
            "children": [left_meta, right_meta],
        }
        if draft_path is not None:
            atomic_json(draft_path, {
                "chunk_id": chunk.chunk_id,
                "translations": combined,
                "assembled_from_children": True,
            })
        if meta_path is not None:
            atomic_json(meta_path, meta)
        return combined, meta

    stage = cfg["translation"]
    previous, following, relevant = chunk_context(
        chunk, blocks, cfg, glossary
    )
    feedback: list[str] = []
    attempts_meta: list[dict[str, Any]] = []
    for attempt in range(1, int(stage["generation_retries"]) + 1):
        messages = translation_messages(
            cfg, glossary, book_bible, chapter_bible, source_scene_map, chunk,
            block_map, previous, following, relevant,
            accepted, feedback or None,
        )
        max_tokens = fit_output_budget(
            client, messages, stage, output_budget_for_chunk(chunk, cfg)
        )
        logging.info(
            "translation %s attempt %s words=%s max_tokens=%s",
            chunk.chunk_id, attempt, chunk.word_count, max_tokens,
        )
        generation = client.complete(
            messages, stage, max_tokens,
            f"translation:{chunk.chunk_id}:attempt{attempt}",
        )
        errors: list[str] = []
        warnings: list[str] = []
        if generation.finish_reason not in set(
            cfg["validation"]["accepted_finish_reasons"]
        ):
            errors.append(f"finish_reason={generation.finish_reason!r}")
        try:
            translated, parse_errors = parse_translation_response(
                generation, chunk.pids
            )
            errors.extend(parse_errors)
        except Exception as exc:
            translated = {}
            errors.append(str(exc))
        if translated:
            valid, validation_errors, warnings = validate_translation_map(
                translated, chunk, block_map, cfg
            )
            errors.extend(validation_errors)
        else:
            valid = False
        attempts_meta.append({
            "attempt": attempt, "errors": errors, "warnings": warnings,
            "generation": client.calls[-1] if client.calls else {},
        })
        if not errors and valid:
            meta = {
                "chunk_id": chunk.chunk_id,
                "attempts": attempts_meta,
                "split": False,
            }
            if draft_path is not None:
                atomic_json(draft_path, {
                    "chunk_id": chunk.chunk_id,
                    "translations": {
                        pid: translated[pid] for pid in chunk.pids
                    },
                    "recursive_cache": True,
                })
            if meta_path is not None:
                atomic_json(meta_path, meta)
            return translated, meta
        logging.warning(
            "translation %s failed validation: %s",
            chunk.chunk_id, "; ".join(errors),
        )
        feedback = errors
        if (
            attempt >= int(stage["split_after_attempt"])
            and chunk.word_count >= int(
                cfg["chunking"]["minimum_recursive_words"]
            ) * 2
        ):
            break

    if chunk.word_count < int(
        cfg["chunking"]["minimum_recursive_words"]
    ) * 2:
        raise PipelineError(f"translation {chunk.chunk_id} failed")

    left, right = split_chunk(chunk, block_map)
    if split_path is not None:
        atomic_json(split_path, {
            "chunk_id": chunk.chunk_id,
            "left": asdict(left),
            "right": asdict(right),
            "created_at": utc_now(),
        })
    left_map, left_meta = generate_translation_recursive(
        left, client, cfg, glossary, book_bible, chapter_bible, source_scene_map,
        blocks, block_map, accepted, drafts_dir, meta_dir,
    )
    right_map, right_meta = generate_translation_recursive(
        right, client, cfg, glossary, book_bible, chapter_bible, source_scene_map,
        blocks, block_map, {**accepted, **left_map},
        drafts_dir, meta_dir,
    )
    combined = {**left_map, **right_map}
    meta = {
        "chunk_id": chunk.chunk_id,
        "attempts": attempts_meta,
        "split": True,
        "children": [left_meta, right_meta],
    }
    if draft_path is not None:
        atomic_json(draft_path, {
            "chunk_id": chunk.chunk_id,
            "translations": combined,
            "assembled_from_children": True,
        })
    if meta_path is not None:
        atomic_json(meta_path, meta)
    return combined, meta


EN_NUMBER_WORDS = {
    "zero", "one", "two", "three", "four", "five", "six", "seven",
    "eight", "nine", "ten", "eleven", "twelve", "thirteen",
    "fourteen", "fifteen", "sixteen", "seventeen", "eighteen",
    "nineteen", "twenty", "thirty", "forty", "fifty", "sixty",
    "seventy", "eighty", "ninety", "hundred", "thousand", "half",
    "quarter",
}
RU_NUMBER_HINTS = {
    "ноль", "один", "одна", "одно", "два", "две", "три", "четыре",
    "пять", "шесть", "семь", "восемь", "девять", "десять",
    "одиннадцать", "двенадцать", "тринадцать", "четырнадцать",
    "пятнадцать", "шестнадцать", "семнадцать", "восемнадцать",
    "девятнадцать", "двадцать", "тридцать", "сорок", "пятьдесят",
    "шестьдесят", "семьдесят", "восемьдесят", "девяносто",
    "сто", "тысяч", "половин", "четверт", "полноч",
}


EN_NUMBER_DIGITS = {
    "zero": "0", "two": "2", "three": "3", "four": "4",
    "five": "5", "six": "6", "seven": "7", "eight": "8",
    "nine": "9", "ten": "10", "eleven": "11", "twelve": "12",
    "thirteen": "13", "fourteen": "14", "fifteen": "15",
    "sixteen": "16", "seventeen": "17", "eighteen": "18",
    "nineteen": "19", "twenty": "20", "thirty": "30",
    "forty": "40", "fifty": "50", "sixty": "60",
    "seventy": "70", "eighty": "80", "ninety": "90",
    "hundred": "100", "thousand": "1000",
}

# Stems/forms are intentional: Russian number words are heavily inflected and
# often become compounds (e.g. three-hour -> трёхчасовой).
EN_TO_RU_NUMBER_PATTERNS = {
    "zero": r"(?:нол|нул)",
    "two": r"(?:два|две|двое|двух|двум|двумя|обоих|обеих|двойн)",
    "three": r"(?:три|трое|тр[её]х|тр[её]м|тремя|тройн)",
    "four": r"(?:четыр|четвер)",
    "five": r"пят",
    "six": r"шест",
    "seven": r"сем",
    "eight": r"восем",
    "nine": r"девят",
    "ten": r"десят",
    "eleven": r"одиннадцат",
    "twelve": r"двенадцат",
    "thirteen": r"тринадцат",
    "fourteen": r"четырнадцат",
    "fifteen": r"пятнадцат",
    "sixteen": r"шестнадцат",
    "seventeen": r"семнадцат",
    "eighteen": r"восемнадцат",
    "nineteen": r"девятнадцат",
    "twenty": r"двадцат",
    "thirty": r"тридцат",
    "forty": r"сорок",
    "fifty": r"пятидесят|пятьдесят",
    "sixty": r"шестидесят|шестьдесят",
    "seventy": r"семидесят|семьдесят",
    "eighty": r"восьмидесят|восемьдесят",
    "ninety": r"девяност",
    "hundred": r"(?:сто|сот|стах|стами)",
    "thousand": r"тысяч",
}


def missing_written_number_words(source_text: str, target: str) -> set[str]:
    """Return source number words with no plausible Russian equivalent.

    `one`, `half`, and `quarter` are deliberately excluded from deterministic
    enforcement because they are frequently pronominal or idiomatic.
    """
    source_words = {
        word.casefold()
        for word in re.findall(r"\b[A-Za-z]+\b", source_text)
    }
    candidates = (source_words & EN_NUMBER_WORDS) - {"one", "half", "quarter"}
    target_folded = target.casefold().replace("ё", "е")
    target_digits = set(digits(target))
    missing: set[str] = set()
    for word in candidates:
        digit = EN_NUMBER_DIGITS.get(word)
        if digit and digit in target_digits:
            continue
        pattern = EN_TO_RU_NUMBER_PATTERNS.get(word)
        if pattern and re.search(pattern.replace("ё", "е"), target_folded, flags=re.I):
            continue
        missing.add(word)
    return missing


_SOURCE_BOUNDARY = r"A-Za-z0-9_"


def source_term_present(text: str, source: str) -> bool:
    """Match glossary source forms as terms, not arbitrary substrings."""
    source = norm(source)
    if not source:
        return False

    # `Other` is both a setting noun and a very common adjective. Only treat
    # the singular capitalized form as an entity in clear noun contexts.
    if source == "Other":
        return bool(re.search(
            r"\b(?:an?|the|this|that|some|any|particular|which|what|one|another)\s+Other\b",
            text,
        ))
    if source in {"Others", "The Other", "The Others"}:
        return bool(re.search(
            rf"(?<![{_SOURCE_BOUNDARY}]){re.escape(source)}(?![{_SOURCE_BOUNDARY}])",
            text,
        ))

    escaped = re.escape(source)
    escaped = escaped.replace(r"\ ", r"\s+")
    escaped = escaped.replace("’", "['’]").replace("\\'", "['’]")
    flags = 0 if source[:1].isupper() else re.I
    return bool(re.search(
        rf"(?<![{_SOURCE_BOUNDARY}]){escaped}(?![{_SOURCE_BOUNDARY}])",
        text,
        flags=flags,
    ))


_RU_ENDINGS = sorted({
    "иями", "ями", "ами", "ого", "ему", "ому", "ыми", "ими",
    "иях", "ах", "ях", "ой", "ей", "ый", "ий", "ая", "яя", "ую",
    "юю", "ом", "ем", "ым", "им", "ов", "ев", "ам", "ям", "а", "я",
    "у", "ю", "ы", "и", "е", "о", "ь",
}, key=len, reverse=True)


def _ru_core(token: str) -> str:
    token = re.sub(r"[^А-Яа-яЁё]", "", token).casefold().replace("ё", "е")
    if len(token) <= 4:
        return token
    for ending in _RU_ENDINGS:
        if token.endswith(ending) and len(token) - len(ending) >= 4:
            return token[:-len(ending)]
    return token


def target_form_present(text: str, expected: str) -> bool:
    """Allow ordinary Russian inflection of established names/terms."""
    expected = known_target_core(expected)
    if not expected:
        return False
    folded = text.casefold().replace("ё", "е")
    if expected.casefold().replace("ё", "е") in folded:
        return True
    if expected in {"Иной", "Иные"}:
        return bool(re.search(
            r"\bин(?:ой|ого|ому|ым|ом|ая|ую|ые|ых|ыми)\b",
            folded,
        ))
    expected_tokens = re.findall(r"[А-Яа-яЁё]+", expected)
    target_tokens = [
        token.casefold().replace("ё", "е")
        for token in re.findall(r"[А-Яа-яЁё]+", text)
    ]
    cores = [_ru_core(token) for token in expected_tokens]
    cores = [core for core in cores if len(core) >= 4]
    if not cores:
        return False
    return all(any(token.startswith(core) for token in target_tokens) for core in cores)
SOURCE_PROFANITY = re.compile(
    r"\b(fuck(?:ing|ed|er)?|shit(?:ty)?|cunt|bitch|damn(?:ed)?|"
    r"bastard|asshole)\b",
    re.I,
)
TARGET_PROFANITY = re.compile(
    r"(бля|бляд|еб|ёб|ху[йяе]|пизд|сука|дерьм|говн|ч[её]рт|"
    r"ублюд|мудак|сволоч)",
    re.I,
)


def issue_key(issue: Issue) -> tuple[str, str, str]:
    return (
        issue.pid,
        issue.category.casefold(),
        re.sub(r"\W+", " ", issue.problem.casefold()).strip()[:100],
    )


def assign_issue_ids(issues: list[Issue]) -> list[Issue]:
    seen: set[tuple[str, str, str]] = set()
    result: list[Issue] = []
    for issue in issues:
        key = issue_key(issue)
        if key in seen:
            continue
        seen.add(key)
        issue.issue_id = f"i{len(result) + 1:04d}"
        result.append(issue)
    return result


def known_target_core(value: str) -> str:
    value = re.sub(r"\([^)]*\)", "", value)
    value = value.split("/")[0]
    return norm(value)


def deterministic_issues(
    blocks: list[Block], translations: dict[str, str],
    cfg: dict[str, Any], glossary: Glossary,
    chapter_bible: dict[str, Any], book_bible: BookBible,
) -> list[Issue]:
    if not cfg["deterministic_qa"]["enabled"]:
        return []
    issues: list[Issue] = []
    narrator_gender = norm(str(
        (chapter_bible.get("pov") or {}).get("gender") or "unknown"
    )).casefold()
    known = glossary.all_known()
    entity_records: list[dict[str, Any]] = []
    for item in chapter_bible.get("entities") or []:
        if isinstance(item, dict):
            entity_records.append(item)
    for source, item in book_bible.data.get("entities", {}).items():
        entity_records.append({"source": source, **item})

    for block in blocks:
        target = translations.get(block.pid, "")
        if not target:
            issues.append(Issue(
                pid=block.pid, severity="critical", category="missing",
                problem="Translation is missing.",
                repair_instruction="Translate the complete source block.",
                source="deterministic", deterministic=True,
            ))
            continue

        missing_values = missing_numeric_values(
            block.digits, target
        )
        if missing_values:
            issues.append(Issue(
                pid=block.pid, severity="critical", category="number",
                problem=(
                    f"Numeric values may be missing: {missing_values}; "
                    f"source={block.digits}, target_digits={digits(target)}"
                ),
                repair_instruction=(
                    "Restore the numerical value. Digits may be written "
                    "as natural Russian words when appropriate."
                ),
                source="deterministic", deterministic=True,
            ))

        if cfg["deterministic_qa"].get("number_words_check", True):
            missing_number_words = missing_written_number_words(
                block.source_text, target
            )
            if missing_number_words:
                issues.append(Issue(
                    pid=block.pid, severity="major",
                    category="number_word",
                    problem=(
                        "Source contains written-out number expression(s) "
                        f"{sorted(missing_number_words)}, but target has no "
                        "detectable equivalent."
                    ),
                    repair_instruction=(
                        "Verify and restore the exact number, age, quantity "
                        "or clock time from the source."
                    ),
                    source="deterministic", deterministic=True,
                ))

        if (
            cfg["deterministic_qa"].get("profanity_check", True)
            and SOURCE_PROFANITY.search(block.source_text)
            and not TARGET_PROFANITY.search(target)
        ):
            issues.append(Issue(
                pid=block.pid, severity="major",
                category="tone_profanity",
                problem="Strong source profanity may have been softened.",
                repair_instruction=(
                    "Preserve comparable force, vulgarity and speaker tone."
                ),
                source="deterministic", deterministic=True,
            ))

        english = detect_english_sentence(
            target, int(cfg["validation"]["english_sequence_min_words"])
        )
        if english:
            issues.append(Issue(
                pid=block.pid, severity="critical",
                category="english_residue",
                problem=f"English residue: {english}",
                repair_instruction="Translate the English residue.",
                source="deterministic", deterministic=True,
            ))

        if cfg["deterministic_qa"].get("mixed_script_check", True):
            mixed = mixed_script_tokens(
                target,
                cfg["deterministic_qa"].get("mixed_script_allow", []),
            )
            if mixed:
                issues.append(Issue(
                    pid=block.pid, severity="critical",
                    category="mixed_script",
                    problem=f"Latin or mixed-script token(s) in Russian text: {mixed}",
                    repair_instruction=(
                        "Replace untranslated or mixed-script tokens with "
                        "consistent Russian text, unless explicitly allowlisted."
                    ),
                    source="deterministic", deterministic=True,
                ))

        if (
            cfg["deterministic_qa"].get("length_outlier_check", True)
            and len(block.source_text) >= 20
        ):
            ratio = len(target) / len(block.source_text)
            if ratio < 0.35 or ratio > 2.6:
                issues.append(Issue(
                    pid=block.pid, severity="major",
                    category="length_outlier",
                    problem=f"Suspicious character-length ratio {ratio:.2f}.",
                    repair_instruction=(
                        "Check for omissions, additions or merged text."
                    ),
                    source="deterministic", deterministic=True,
                ))

        if cfg["deterministic_qa"].get("entity_check", True):
            for source, expected in known.items():
                expected_core = known_target_core(expected)
                if (
                    source_term_present(block.source_text, source)
                    and expected_core
                    and source[:1].isupper()
                    and expected_core[:1].isupper()
                    and not target_form_present(target, expected_core)
                ):
                    issues.append(Issue(
                        pid=block.pid, severity="major",
                        category="name_consistency",
                        problem=(
                            f"Known name/entity {source!r} should use "
                            f"{expected_core!r}."
                        ),
                        repair_instruction=(
                            f"Use the established form {expected_core!r}."
                        ),
                        source="deterministic", deterministic=True,
                    ))
            for entity in entity_records:
                source = norm(str(entity.get("source") or ""))
                expected = norm(str(entity.get("target") or ""))
                forbidden = [
                    norm(str(item))
                    for item in (entity.get("forbidden_targets") or [])
                ]
                if not source or not source_term_present(block.source_text, source):
                    continue
                if expected and not target_form_present(target, expected):
                    if any(
                        value and value.casefold() in target.casefold()
                        for value in forbidden
                    ):
                        issues.append(Issue(
                            pid=block.pid, severity="critical",
                            category="entity_consistency",
                            problem=(
                                f"Entity {source!r} used a forbidden "
                                f"translation; expected {expected!r}."
                            ),
                            repair_instruction=(
                                f"Restore the entity as {expected!r}."
                            ),
                            source="deterministic", deterministic=True,
                        ))

        if (
            cfg["deterministic_qa"].get("narrator_gender_check", True)
            and narrator_gender == "male"
            and re.search(r"\b(I|me|my)\b", block.source_text, flags=re.I)
        ):
            if re.search(r"\bмы обе\b", target, flags=re.I):
                issues.append(Issue(
                    pid=block.pid, severity="critical",
                    category="narrator_gender",
                    problem="Male first-person narrator translated as «мы обе».",
                    repair_instruction="Use masculine/mixed-group agreement.",
                    source="deterministic", deterministic=True,
                ))
            if re.search(
                r"\bя\s+(?:была|могла|сказала|подумала|надеялась|"
                r"увидела|услышала|решила|почувствовала)\b",
                target, flags=re.I,
            ):
                issues.append(Issue(
                    pid=block.pid, severity="critical",
                    category="narrator_gender",
                    problem="Male narrator uses feminine agreement.",
                    repair_instruction="Restore masculine agreement.",
                    source="deterministic", deterministic=True,
                ))
    return assign_issue_ids(issues)


def audit_messages(
    cfg: dict[str, Any], glossary: Glossary, book_bible: BookBible,
    chapter_bible: dict[str, Any], chunk: Chunk,
    block_map: dict[str, Block], translations: dict[str, str],
    deterministic: list[Issue], strict_retry: bool = False,
) -> list[dict[str, str]]:
    ordered_blocks = sorted(
        block_map.values(), key=lambda block: block.index
    )
    positions = {block.pid: index for index, block in enumerate(ordered_blocks)}
    first = positions[chunk.pids[0]]
    last = positions[chunk.pids[-1]]
    before = [
        block.pid for block in ordered_blocks[
            max(0, first - int(cfg["audit"].get("context_before", 2))):first
        ]
    ]
    after = [
        block.pid for block in ordered_blocks[
            last + 1:last + 1 + int(cfg["audit"].get("context_after", 2))
        ]
    ]
    context_pids = before + after
    relevant_source = "\n".join(
        block_map[pid].source_text for pid in context_pids + chunk.pids
    )
    max_issues = int(cfg["audit"].get("max_issues_per_batch", 5))
    system = f"""Ты — независимый двуязычный редактор EN→RU.
Проверяй только существенные ошибки перевода: пропуски/добавления, субъект,
отрицание, модальность, род, число, родство, говорящего/адресата, время,
возраст, конкретные предметы, имена, ругань, кальки и сломанный русский.
Не отмечай допустимые стилистические варианты.

Не показывай ход рассуждений. Не обсуждай подсказки или детекторы. Если
предполагаемая проблема ложная, просто не включай её в issues. Максимум
{max_issues} замечаний. problem и repair_instruction — по одному короткому
предложению; suggested_text всегда оставляй пустой строкой.

ГЛОССАРИЙ:
{glossary.prompt(relevant_source)}

БИБЛИЯ:
{book_bible.prompt(relevant_source, chapter_bible)}

Верни только JSON:
{{
 "issues":[{{
  "pid":"p00001",
  "severity":"critical|major|minor",
  "category":"meaning|grammar|gender|number|entity|tone|register|style",
  "problem":"краткое точное описание",
  "repair_instruction":"кратко: что исправить или сохранить",
  "suggested_text":""
 }}]
}}
"""
    if strict_retry:
        system += (
            "\nПРЕДЫДУЩИЙ ОТВЕТ БЫЛ НЕКОРРЕКТНЫМ ИЛИ СЛИШКОМ ДЛИННЫМ. "
            "Ответь предельно кратким валидным JSON без анализа."
        )

    suspects_text = ""
    if cfg["audit"].get("include_deterministic_suspects", False):
        suspects = [
            {
                "pid": issue.pid,
                "category": issue.category,
                "problem": issue.problem,
            }
            for issue in deterministic
            if issue.pid in set(chunk.pids)
        ]
        suspects_text = (
            "<OPTIONAL_SUSPECTS>\n"
            + json.dumps(suspects, ensure_ascii=False, separators=(",", ":"))
            + "\n</OPTIONAL_SUSPECTS>\n"
        )

    user = (
        suspects_text
        + "<CONTEXT_ONLY>\n"
        + "\n".join(
            (
                f'<CONTEXT pid="{pid}">\n'
                f"<EN>{html.escape(block_map[pid].source_text)}</EN>\n"
                f"<RU>{html.escape(translations.get(pid, ''))}</RU>\n"
                "</CONTEXT>"
            )
            for pid in context_pids
        )
        + "\n</CONTEXT_ONLY>\n<PAIRS>\n"
        + "\n".join(
            (
                f'<PAIR pid="{pid}">\n'
                f"<EN>{html.escape(block_map[pid].source_text)}</EN>\n"
                f"<RU>{html.escape(translations[pid])}</RU>\n"
                "</PAIR>"
            )
            for pid in chunk.pids
        )
        + "\n</PAIRS>"
    )
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]


def parse_issues_response(
    generation: Generation, allowed_pids: set[str],
) -> list[Issue]:
    data = safe_json_loads(generation.content)
    raw = data.get("issues") or []
    if not isinstance(raw, list):
        raise PipelineError("issues must be a list")
    result: list[Issue] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        pid = norm(str(item.get("pid") or ""))
        if pid not in allowed_pids:
            continue
        severity = norm(str(item.get("severity") or "major")).casefold()
        if severity not in {"critical", "major", "minor"}:
            severity = "major"
        problem = norm(str(item.get("problem") or ""))[:800]
        if not problem:
            continue
        result.append(Issue(
            pid=pid, severity=severity,
            category=norm(str(item.get("category") or "meaning"))[:80],
            problem=problem,
            repair_instruction=norm(str(
                item.get("repair_instruction") or ""
            ))[:800],
            suggested_text="",
            source="reviewer", deterministic=False,
        ))
    return result


def make_audit_units(
    chunks: list[Chunk], block_map: dict[str, Block],
    batch_pids: int,
) -> list[Chunk]:
    """Split translation chunks into small reviewer batches."""
    units: list[Chunk] = []
    batch_pids = max(1, int(batch_pids))
    for chunk in chunks:
        for offset in range(0, len(chunk.pids), batch_pids):
            pids = chunk.pids[offset:offset + batch_pids]
            units.append(Chunk(
                chunk_id=(
                    f"{chunk.chunk_id}_q{offset // batch_pids + 1:03d}"
                ),
                pids=pids,
                word_count=sum(block_map[pid].word_count for pid in pids),
            ))
    return units


def _split_audit_chunk(chunk: Chunk) -> tuple[Chunk, Chunk]:
    middle = max(1, len(chunk.pids) // 2)
    left_pids = chunk.pids[:middle]
    right_pids = chunk.pids[middle:]
    return (
        Chunk(
            chunk_id=f"{chunk.chunk_id}a",
            pids=left_pids,
            word_count=0,
        ),
        Chunk(
            chunk_id=f"{chunk.chunk_id}b",
            pids=right_pids,
            word_count=0,
        ),
    )


def run_audit(
    chunks: list[Chunk], client: ApiClient, cfg: dict[str, Any],
    glossary: Glossary, book_bible: BookBible,
    chapter_bible: dict[str, Any], block_map: dict[str, Block],
    translations: dict[str, str], deterministic: list[Issue],
    audit_dir: Path,
) -> list[Issue]:
    audit_dir.mkdir(parents=True, exist_ok=True)
    reviewer_issues: list[Issue] = []
    if not cfg["audit"]["enabled"] or not cfg["reviewer_api"]["enabled"]:
        return assign_issue_ids(deterministic)

    queue = deque(make_audit_units(
        chunks, block_map, int(cfg["audit"].get("batch_pids", 8))
    ))
    successful_units = 0
    failed_leaf_units: list[str] = []
    max_issues = int(cfg["audit"].get("max_issues_per_batch", 5))

    while queue:
        chunk = queue.popleft()
        path = audit_dir / f"{chunk.chunk_id}.json"
        if path.exists():
            saved = read_json(path, {})
            if not saved.get("failed") and not saved.get("split_into"):
                reviewer_issues.extend(
                    Issue(**item) for item in saved.get("issues", [])
                )
                successful_units += 1
                continue
            # Failed/incomplete cache entries must not silently suppress audit.
            path.unlink()

        attempts: list[dict[str, Any]] = []
        last_error = ""
        completed = False
        for attempt in range(
            1, int(cfg["audit"]["generation_retries"]) + 1
        ):
            messages = audit_messages(
                cfg, glossary, book_bible, chapter_bible, chunk,
                block_map, translations, deterministic,
                strict_retry=(attempt > 1),
            )
            max_tokens = fit_output_budget(
                client, messages, cfg["audit"],
                int(cfg["audit"]["max_tokens"]),
            )
            logging.info(
                "audit %s attempt %s max_tokens=%s",
                chunk.chunk_id, attempt, max_tokens,
            )
            generation = client.complete(
                messages, cfg["audit"], max_tokens,
                f"audit:{chunk.chunk_id}:attempt{attempt}",
            )
            try:
                if generation.finish_reason not in {"stop", None}:
                    raise PipelineError(
                        f"finish_reason={generation.finish_reason!r}"
                    )
                issues = parse_issues_response(
                    generation, set(chunk.pids)
                )[:max_issues]
                reviewer_issues.extend(issues)
                attempts.append({
                    "attempt": attempt,
                    "error": "",
                    "generation": client.calls[-1],
                    "content": generation.content,
                })
                atomic_json(path, {
                    "chunk_id": chunk.chunk_id,
                    "pids": chunk.pids,
                    "issues": [asdict(issue) for issue in issues],
                    "attempts": attempts,
                })
                successful_units += 1
                completed = True
                break
            except Exception as exc:
                last_error = str(exc)
                attempts.append({
                    "attempt": attempt,
                    "error": last_error,
                    "generation": client.calls[-1],
                    "content": generation.content,
                })

        if completed:
            continue

        if cfg["audit"].get("split_on_failure", True) and len(chunk.pids) > 1:
            left, right = _split_audit_chunk(chunk)
            left.word_count = sum(block_map[pid].word_count for pid in left.pids)
            right.word_count = sum(block_map[pid].word_count for pid in right.pids)
            atomic_json(path, {
                "chunk_id": chunk.chunk_id,
                "pids": chunk.pids,
                "issues": [],
                "attempts": attempts,
                "failed": last_error,
                "split_into": [left.chunk_id, right.chunk_id],
            })
            logging.warning(
                "audit %s failed; splitting into %s and %s",
                chunk.chunk_id, left.chunk_id, right.chunk_id,
            )
            queue.appendleft(right)
            queue.appendleft(left)
            continue

        failed_leaf_units.append(chunk.chunk_id)
        atomic_json(path, {
            "chunk_id": chunk.chunk_id,
            "pids": chunk.pids,
            "issues": [],
            "attempts": attempts,
            "failed": last_error,
        })
        logging.error("audit leaf %s failed: %s", chunk.chunk_id, last_error)
        if not cfg["audit"].get("fail_open", True):
            raise PipelineError(
                f"Audit {chunk.chunk_id} failed: {last_error}"
            )

    total_leaf_units = successful_units + len(failed_leaf_units)
    success_rate = (
        successful_units / total_leaf_units if total_leaf_units else 0.0
    )
    atomic_json(audit_dir / "audit_summary.json", {
        "successful_units": successful_units,
        "failed_leaf_units": failed_leaf_units,
        "success_rate": round(success_rate, 4),
    })
    minimum = float(cfg["audit"].get("minimum_success_rate", 0.90))
    if cfg["audit"].get("required", False) and success_rate < minimum:
        raise PipelineError(
            "Audit coverage too low: "
            f"{success_rate:.1%} < required {minimum:.1%}; "
            f"failed={failed_leaf_units}"
        )
    return assign_issue_ids(deterministic + reviewer_issues)


def should_auto_repair(issue: Issue, cfg: dict[str, Any]) -> bool:
    """Select issues after verifier without conflating severity with action.

    New verified issues carry an explicit repair/keep/uncertain decision.  The
    legacy severity/category policy remains only as a compatibility fallback
    for work directories created before verifier contract v2.
    """
    repair_cfg = cfg["repair"]
    decision = norm(issue.verifier_decision).casefold()
    confidence = norm(issue.verifier_confidence).casefold()
    if decision:
        allowed_decisions = {
            str(value).casefold() for value in repair_cfg.get(
                "auto_repair_verified_decisions", ["repair"]
            )
        }
        allowed_confidences = {
            str(value).casefold() for value in repair_cfg.get(
                "auto_repair_verifier_confidences", ["high", "deterministic"]
            )
        }
        return decision in allowed_decisions and confidence in allowed_confidences

    if issue.deterministic:
        if not repair_cfg.get("auto_repair_deterministic", True):
            return False
        allowed = set(
            repair_cfg.get("auto_repair_deterministic_categories", [])
        )
        return issue.category in allowed
    return issue.severity in set(repair_cfg.get(
        "auto_repair_severities", ["critical", "major"]
    ))


def repair_messages(
    glossary: Glossary, book_bible: BookBible,
    chapter_bible: dict[str, Any], pids: list[str],
    block_map: dict[str, Block], current: dict[str, str],
    issues: list[Issue], cfg: Optional[dict[str, Any]] = None,
    feedback_by_pid: Optional[dict[str, list[str]]] = None,
) -> list[dict[str, str]]:
    relevant_source = "\n".join(
        block_map[pid].source_text for pid in pids
    )
    system = f"""Исправляй только отмеченные конкретные ошибки.
Не редактируй без необходимости и не переноси текст между PID.
Сохраняй смысл, субъект действия, отрицание, модальность, обещания, причинность,
тон, грубость, род, числа, предметы и ты/вы.
Verifier уже решил, что каждый переданный issue требует исправления.
Для такого PID верни action=replace и действительно измени текст. action=keep
допустим только при прямом неразрешимом противоречии в исходных данных; он будет
считаться незавершённой ошибкой и отправлен на повторную попытку. Перед replace
заново сопоставь полный EN, CURRENT_RU и предлагаемый полный русский абзац.
Не исправляй одну деталь ценой новой ошибки.

ГЛОССАРИЙ:
{glossary.prompt(relevant_source)}

БИБЛИЯ:
{book_bible.prompt(relevant_source, chapter_bible)}

Строго JSON:
{{"repairs":[{{
 "pid":"p00001","action":"replace|keep",
 "text":"полный русский абзац","reason":"кратко"
}}]}}
Каждый запрошенный PID ровно один раз."""
    by_pid: dict[str, list[dict[str, Any]]] = defaultdict(list)
    selected = set(pids)
    for issue in issues:
        if issue.pid in selected:
            by_pid[issue.pid].append(asdict(issue))

    ordered = sorted(block_map.values(), key=lambda block: block.index)
    positions = {block.pid: index for index, block in enumerate(ordered)}
    before_count = int((cfg or {}).get("repair", {}).get("context_before", 1))
    after_count = int((cfg or {}).get("repair", {}).get("context_after", 1))

    def context_text(pid: str) -> tuple[str, str]:
        index = positions[pid]
        before = ordered[max(0, index - before_count):index]
        after = ordered[index + 1:index + 1 + after_count]

        def render(items: list[Block]) -> str:
            if not items:
                return "(none)"
            return "\n".join(
                f'[{item.pid}] EN: {html.escape(item.source_text)}\n'
                f'[{item.pid}] RU: {html.escape(current.get(item.pid, ""))}'
                for item in items
            )

        return render(before), render(after)

    rendered_items = []
    for pid in pids:
        before, after = context_text(pid)
        feedback = "\n".join((feedback_by_pid or {}).get(pid, [])) or "(none)"
        rendered_items.append(
            f'<ITEM pid="{pid}">\n'
            f"<CONTEXT_BEFORE>{before}</CONTEXT_BEFORE>\n"
            f"<EN>{html.escape(block_map[pid].source_text)}</EN>\n"
            f"<CURRENT_RU>{html.escape(current[pid])}</CURRENT_RU>\n"
            f"<ISSUES>{html.escape(json.dumps(by_pid[pid], ensure_ascii=False))}</ISSUES>\n"
            f"<PREVIOUS_ATTEMPT_FEEDBACK>{html.escape(feedback)}</PREVIOUS_ATTEMPT_FEEDBACK>\n"
            f"<CONTEXT_AFTER>{after}</CONTEXT_AFTER>\n"
            "</ITEM>"
        )
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": "\n".join(rendered_items)},
    ]

def parse_repairs(
    generation: Generation, pids: list[str],
) -> dict[str, dict[str, str]]:
    data = safe_json_loads(generation.content)
    raw = data.get("repairs") or []
    if not isinstance(raw, list):
        raise PipelineError("repairs must be a list")
    result: dict[str, dict[str, str]] = {}
    for item in raw:
        if not isinstance(item, dict):
            continue
        pid = norm(str(item.get("pid") or ""))
        if pid not in set(pids) or pid in result:
            continue
        action = norm(str(item.get("action") or "keep")).casefold()
        if action not in {"replace", "keep"}:
            action = "keep"
        result[pid] = {
            "action": action,
            "text": norm(str(item.get("text") or "")),
            "reason": norm(str(item.get("reason") or "")),
        }
    if list(result) != pids:
        raise PipelineError(
            f"repair PID mismatch expected={pids}, got={list(result)}"
        )
    return result


def validate_single_repair(
    pid: str, candidate: str, block_map: dict[str, Block],
    cfg: dict[str, Any], current_text: str = "",
) -> list[str]:
    source = block_map[pid]
    errors = []
    if not candidate:
        errors.append("empty")
    if current_text and norm(candidate) == norm(current_text):
        errors.append("unchanged")
    if cfg["validation"]["strict_digits"]:
        candidate_digits = digits(candidate)
        missing_values = missing_numeric_values(source.digits, candidate)
        extra_values: list[str] = []
        source_counts = Counter(source.digits)
        for value, count in Counter(candidate_digits).items():
            extra_values.extend([value] * max(0, count - source_counts.get(value, 0)))
        if missing_values or extra_values:
            errors.append(
                "numeric values "
                f"missing={missing_values} extra={extra_values}; "
                f"source={source.digits} candidate_digits={candidate_digits}"
            )
    english = detect_english_sentence(
        candidate, int(cfg["validation"]["english_sequence_min_words"])
    )
    if english:
        errors.append(f"English residue: {english}")
    ratio = len(candidate) / max(1, len(source.source_text))
    if ratio < 0.25 or ratio > 3.2:
        errors.append(f"length ratio={ratio:.2f}")
    return errors


def run_repairs(
    client: ApiClient, cfg: dict[str, Any], glossary: Glossary,
    book_bible: BookBible, chapter_bible: dict[str, Any],
    block_map: dict[str, Block], draft: dict[str, str],
    issues: list[Issue], repair_dir: Path,
) -> tuple[dict[str, str], list[dict[str, Any]]]:
    result = dict(draft)
    repair_dir.mkdir(parents=True, exist_ok=True)
    selected = [issue for issue in issues if should_auto_repair(issue, cfg)]
    pids = sorted(
        {issue.pid for issue in selected},
        key=lambda pid: block_map[pid].index,
    )
    records: list[dict[str, Any]] = []
    size = int(cfg["repair"]["max_pids_per_call"])
    retry_invalid = bool(cfg["repair"].get("retry_on_keep_or_invalid", True))

    for offset in range(0, len(pids), size):
        batch = pids[offset:offset + size]
        path = repair_dir / f"batch_{offset // size + 1:04d}.json"
        if path.exists():
            saved = read_json(path, {})
            result.update(saved.get("accepted") or {})
            records.extend(saved.get("records") or [])
            continue

        batch_issues = [issue for issue in selected if issue.pid in set(batch)]
        issue_ids_by_pid: dict[str, list[str]] = defaultdict(list)
        for issue in batch_issues:
            issue_ids_by_pid[issue.pid].append(issue.issue_id)

        attempts: list[dict[str, Any]] = []
        accepted: dict[str, str] = {}
        pending = list(batch)
        feedback_by_pid: dict[str, list[str]] = defaultdict(list)
        last_records: dict[str, dict[str, Any]] = {}

        for attempt in range(1, int(cfg["repair"]["generation_retries"]) + 1):
            if not pending:
                break
            pending_issues = [issue for issue in batch_issues if issue.pid in set(pending)]
            messages = repair_messages(
                glossary, book_bible, chapter_bible, pending,
                block_map, result, pending_issues, cfg, feedback_by_pid,
            )
            max_tokens = fit_output_budget(
                client, messages, cfg["repair"], int(cfg["repair"]["max_tokens"]),
            )
            logging.info(
                "repair batch %s attempt %s pids=%s",
                offset // size + 1, attempt, ",".join(pending),
            )
            try:
                generation = client.complete(
                    messages, cfg["repair"], max_tokens,
                    f"repair:batch{offset // size + 1}:attempt{attempt}",
                )
            except Exception as exc:
                logging.warning(
                    "repair batch %s generation attempt %s failed: %s",
                    offset // size + 1, attempt, exc,
                )
                attempts.append({
                    "attempt": attempt, "error": str(exc), "stage": "generation",
                })
                for pid in pending:
                    feedback_by_pid[pid].append(
                        f"Attempt {attempt} failed at generation: {exc}"
                    )
                continue

            try:
                proposed = parse_repairs(generation, pending)
                attempt_records: list[dict[str, Any]] = []
                next_pending: list[str] = []
                for pid in pending:
                    item = proposed[pid]
                    base_record = {
                        "pid": pid,
                        "reason": item["reason"],
                        "issue_ids": issue_ids_by_pid.get(pid, []),
                        "attempt": attempt,
                    }
                    if item["action"] == "keep":
                        record = {
                            **base_record, "action": "keep", "accepted": False,
                            "outcome": "repair_keep_unresolved",
                        }
                        feedback_by_pid[pid].append(
                            "The previous response used action=keep, but verifier-approved "
                            "issues require a real replacement. Produce a corrected full paragraph."
                        )
                        next_pending.append(pid)
                    else:
                        errors = validate_single_repair(
                            pid, item["text"], block_map, cfg, result[pid]
                        )
                        if errors:
                            record = {
                                **base_record, "action": "replace", "accepted": False,
                                "outcome": "repair_invalid_unresolved",
                                "validation_errors": errors,
                                "proposed_after": item["text"],
                            }
                            feedback_by_pid[pid].append(
                                "Previous replacement was invalid: " + ", ".join(errors) +
                                ". Return a materially corrected full Russian paragraph."
                            )
                            next_pending.append(pid)
                        else:
                            accepted[pid] = item["text"]
                            record = {
                                **base_record, "action": "replace", "accepted": True,
                                "outcome": "candidate_created",
                                "before": result[pid], "after": item["text"],
                            }
                    last_records[pid] = record
                    attempt_records.append(record)
                attempts.append({
                    "attempt": attempt, "records": attempt_records,
                    "generation": client.calls[-1],
                })
                pending = next_pending if retry_invalid else []
            except Exception as exc:
                attempts.append({
                    "attempt": attempt, "error": str(exc),
                    "stage": "parse_or_validate", "generation": client.calls[-1],
                })
                for pid in pending:
                    feedback_by_pid[pid].append(
                        f"Attempt {attempt} could not be parsed or validated: {exc}"
                    )

        final_records: list[dict[str, Any]] = []
        for pid in batch:
            if pid in accepted:
                final_records.append(last_records[pid])
            else:
                record = dict(last_records.get(pid) or {
                    "pid": pid, "action": "keep", "reason": "repair generation failed",
                    "accepted": False, "attempt": len(attempts),
                    "issue_ids": issue_ids_by_pid.get(pid, []),
                })
                record["accepted"] = False
                record["outcome"] = "repair_unresolved"
                record["feedback"] = feedback_by_pid.get(pid, [])
                final_records.append(record)

        if pending and cfg["repair"].get("required", False):
            logging.warning(
                "Repair left unresolved PIDs after %s attempts: %s",
                len(attempts), ",".join(pending),
            )

        result.update(accepted)
        records.extend(final_records)
        atomic_json(path, {
            "pids": batch, "accepted": accepted, "unresolved_pids": pending,
            "records": final_records, "attempts": attempts,
        })
    return result, records


def formatting_messages(
    pids: list[str], block_map: dict[str, Block],
    translations: dict[str, str],
    span_filter: Optional[dict[str, set[str]]] = None,
    retry: bool = False,
) -> list[dict[str, str]]:
    retry_rule = (
        "Это повторная попытка только для ранее не восстановленных spans. "
        "Если английское выделенное слово не имеет прямого русского аналога "
        "из-за грамматики (например, опущенная связка), выбери минимальную "
        "русскую фразу, несущую тот же смысловой акцент."
        if retry else ""
    )
    system = f"""Восстанови только смысловой курсив/жирный текст/ссылки.
Не переписывай перевод. Для каждого SOURCE_SPAN найди точную непрерывную
подстроку в TRANSLATION. target_text должен дословно встречаться в переводе.
Для нескольких одинаковых выделений выбирай разные occurrence по порядку.
Не возвращай пустую строку, если смысловой акцент можно перенести на ближайший
русский эквивалент или короткую фразу. {retry_rule}
Если соответствия действительно нет, верни пустую строку.

Строго JSON:
{{"mappings":[{{
 "pid":"p00001","span_id":"em01",
 "target_text":"они сами","occurrence":1
}}]}}"""
    items = []
    for pid in pids:
        spans = [
            asdict(span) for span in block_map[pid].inline_spans
            if span_filter is None or span.span_id in span_filter.get(pid, set())
        ]
        items.append(
            f'<FORMAT_ITEM pid="{pid}">\n'
            f"<SOURCE>{html.escape(block_map[pid].source_text)}</SOURCE>\n"
            f"<SOURCE_SPANS>{html.escape(json.dumps(spans, ensure_ascii=False))}</SOURCE_SPANS>\n"
            f"<TRANSLATION>{html.escape(translations[pid])}</TRANSLATION>\n"
            "</FORMAT_ITEM>"
        )
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": "\n".join(items)},
    ]

def parse_format_mappings(
    generation: Generation,
    allowed: dict[tuple[str, str], InlineSpan],
) -> dict[tuple[str, str], dict[str, Any]]:
    data = safe_json_loads(generation.content)
    raw = data.get("mappings") or []
    if not isinstance(raw, list):
        raise PipelineError("mappings must be a list")
    result: dict[tuple[str, str], dict[str, Any]] = {}
    for item in raw:
        if not isinstance(item, dict):
            continue
        key = (
            norm(str(item.get("pid") or "")),
            norm(str(item.get("span_id") or "")),
        )
        if key not in allowed or key in result:
            continue
        try:
            occurrence = max(1, int(item.get("occurrence") or 1))
        except Exception:
            occurrence = 1
        result[key] = {
            "target_text": str(item.get("target_text") or ""),
            "occurrence": occurrence,
        }
    return result


def occurrence_ranges(text: str, needle: str) -> list[tuple[int, int]]:
    if not needle:
        return []
    matches = list(re.finditer(re.escape(needle), text))
    if not matches:
        matches = list(re.finditer(re.escape(needle), text, flags=re.I))
    return [(match.start(), match.end()) for match in matches]


def find_occurrence(
    text: str, needle: str, occurrence: int,
) -> Optional[tuple[int, int]]:
    ranges = occurrence_ranges(text, needle)
    if occurrence < 1 or occurrence > len(ranges):
        return None
    return ranges[occurrence - 1]


def find_nonoverlapping_occurrence(
    text: str, needle: str, preferred: int,
    occupied: list[tuple[int, int, InlineSpan]],
) -> Optional[tuple[int, int]]:
    ranges = occurrence_ranges(text, needle)
    if not ranges:
        return None
    order = list(range(len(ranges)))
    preferred_index = preferred - 1
    if 0 <= preferred_index < len(ranges):
        order.remove(preferred_index)
        order.insert(0, preferred_index)
    for index in order:
        start, end = ranges[index]
        if not any(not (end <= a or start >= b) for a, b, _ in occupied):
            return start, end
    return None


def attrs_to_html(attrs: dict[str, Any]) -> str:
    parts = []
    for key, value in attrs.items():
        if isinstance(value, list):
            value = " ".join(str(item) for item in value)
        parts.append(
            f' {html.escape(str(key), quote=True)}='
            f'"{html.escape(str(value), quote=True)}"'
        )
    return "".join(parts)


def apply_inline_mappings(
    text: str, spans: list[InlineSpan],
    mappings: dict[tuple[str, str], dict[str, Any]],
    pid: str,
) -> tuple[str, list[dict[str, Any]]]:
    ranges: list[tuple[int, int, InlineSpan]] = []
    incidents = []
    for span in spans:
        mapping = mappings.get((pid, span.span_id))
        if not mapping:
            incidents.append({
                "pid": pid, "span_id": span.span_id,
                "status": "missing_mapping",
                "source_text": span.source_text,
                "required": span.required,
                "severity": "blocking" if span.required else "warning",
            })
            continue
        location = find_nonoverlapping_occurrence(
            text, str(mapping["target_text"]),
            int(mapping["occurrence"]), ranges,
        )
        if not location:
            status = (
                "target_not_found"
                if not occurrence_ranges(text, str(mapping["target_text"]))
                else "overlap"
            )
            incidents.append({
                "pid": pid, "span_id": span.span_id,
                "status": status,
                "source_text": span.source_text,
                "target_text": mapping["target_text"],
                "required": span.required,
                "severity": "blocking" if span.required else "warning",
            })
            continue
        start, end = location
        ranges.append((start, end, span))
    ranges.sort(key=lambda item: item[0])
    parts = []
    cursor = 0
    for start, end, span in ranges:
        parts.append(html.escape(text[cursor:start]))
        parts.append(f"<{span.tag}{attrs_to_html(span.attrs)}>")
        parts.append(html.escape(text[start:end]))
        parts.append(f"</{span.tag}>")
        cursor = end
    parts.append(html.escape(text[cursor:]))
    return "".join(parts), incidents


def run_formatting(
    client: ApiClient, cfg: dict[str, Any], blocks: list[Block],
    block_map: dict[str, Block], translations: dict[str, str],
    formatting_dir: Path,
) -> tuple[dict[str, str], list[dict[str, Any]]]:
    formatting_dir.mkdir(parents=True, exist_ok=True)
    formatted = {
        block.pid: html.escape(translations[block.pid])
        for block in blocks
    }
    pids = [block.pid for block in blocks if block.inline_spans]
    incidents: list[dict[str, Any]] = []
    if not pids or not cfg["formatting"]["enabled"]:
        return formatted, incidents
    size = int(cfg["formatting"]["max_blocks_per_call"])
    for offset in range(0, len(pids), size):
        batch = pids[offset:offset + size]
        path = formatting_dir / f"batch_{offset // size + 1:04d}.json"
        if path.exists():
            saved = read_json(path, {})
            formatted.update(saved.get("formatted_inner") or {})
            incidents.extend(saved.get("incidents") or [])
            continue
        allowed = {
            (pid, span.span_id): span
            for pid in batch for span in block_map[pid].inline_spans
        }
        mappings = {}
        attempts = []
        for attempt in range(
            1, int(cfg["formatting"]["generation_retries"]) + 1
        ):
            messages = formatting_messages(
                batch, block_map, translations
            )
            max_tokens = fit_output_budget(
                client, messages, cfg["formatting"],
                int(cfg["formatting"]["max_tokens"]),
            )
            logging.info(
                "formatting batch %s attempt %s pids=%s",
                offset // size + 1, attempt, ",".join(batch),
            )
            generation = client.complete(
                messages, cfg["formatting"], max_tokens,
                f"formatting:batch{offset // size + 1}:attempt{attempt}",
            )
            try:
                mappings = parse_format_mappings(generation, allowed)
                attempts.append({
                    "attempt": attempt, "error": "",
                    "generation": client.calls[-1],
                })
                break
            except Exception as exc:
                attempts.append({
                    "attempt": attempt, "error": str(exc),
                    "generation": client.calls[-1],
                })
        # A failed batch always becomes per-span incidents below.  Do not
        # return a flat fallback without durable evidence of what was lost.
        if not mappings:
            attempts.append({"attempt": "final", "error": "no valid mappings", "generation": {}})
        local_formatted = {}
        local_incidents = []
        retry_attempts = []
        for pid in batch:
            inner, initial_found = apply_inline_mappings(
                translations[pid], block_map[pid].inline_spans,
                mappings, pid,
            )
            found = initial_found
            unresolved_ids = {
                item["span_id"] for item in found
                if item.get("status") in {"missing_mapping", "target_not_found"}
            }
            if unresolved_ids and cfg["formatting"].get("retry_unresolved_spans", True):
                retry_allowed = {
                    (pid, span.span_id): span
                    for span in block_map[pid].inline_spans
                    if span.span_id in unresolved_ids
                }
                retry_messages = formatting_messages(
                    [pid], block_map, translations,
                    {pid: unresolved_ids}, retry=True,
                )
                retry_max_tokens = fit_output_budget(
                    client, retry_messages, cfg["formatting"],
                    min(int(cfg["formatting"]["max_tokens"]), 800),
                )
                try:
                    retry_generation = client.complete(
                        retry_messages, cfg["formatting"], retry_max_tokens,
                        f"formatting:retry:{pid}",
                    )
                    retry_mappings = parse_format_mappings(
                        retry_generation, retry_allowed
                    )
                    mappings.update(retry_mappings)
                    inner, found = apply_inline_mappings(
                        translations[pid], block_map[pid].inline_spans,
                        mappings, pid,
                    )
                    retry_attempts.append({
                        "pid": pid, "error": "",
                        "generation": client.calls[-1],
                    })
                except Exception as exc:
                    retry_attempts.append({
                        "pid": pid, "error": str(exc),
                        "generation": client.calls[-1]
                        if client.calls else {},
                    })
            unresolved_keys = {(item["pid"], item["span_id"]) for item in found}
            resolved = [
                {**item, "status": "resolved", "resolution": "retry_restored"}
                for item in initial_found
                if (item["pid"], item["span_id"]) not in unresolved_keys
            ]
            local_formatted[pid] = inner
            local_incidents.extend(resolved)
            local_incidents.extend({**item, "resolution": "unresolved"} for item in found)
        formatted.update(local_formatted)
        incidents.extend(local_incidents)
        atomic_json(path, {
            "pids": batch,
            "mappings": {
                f"{pid}:{span_id}": value
                for (pid, span_id), value in mappings.items()
            },
            "formatted_inner": local_formatted,
            "incidents": local_incidents,
            "attempts": attempts,
            "retry_attempts": retry_attempts,
        })
    return formatted, incidents



def build_final_html(
    normalized_html: str, blocks: list[Block],
    translations: dict[str, str], formatted_inner: dict[str, str],
    cfg: dict[str, Any], filename: str,
) -> str:
    soup = BeautifulSoup(normalized_html, "html.parser")
    for block in blocks:
        target = soup.find(attrs={"data-pid": block.pid})
        if not isinstance(target, Tag):
            raise PipelineError(f"Final assembly missing {block.pid}")
        target.clear()
        fragment = BeautifulSoup(
            formatted_inner.get(
                block.pid, html.escape(translations[block.pid])
            ),
            "html.parser",
        )
        for child in list(fragment.contents):
            target.append(copy.deepcopy(child))
    first_heading = next(
        (
            translations[block.pid]
            for block in blocks if block.tag == "h1"
        ),
        filename,
    )
    if soup.html:
        soup.html["lang"] = "ru"
    head = soup.head
    if head is None:
        head = soup.new_tag("head")
        if soup.html:
            soup.html.insert(0, head)
        else:
            soup.insert(0, head)
    title = head.find("title")
    if title is None:
        title = soup.new_tag("title")
        head.append(title)
    title.string = first_heading
    style = soup.new_tag("style")
    style.string = cfg["html"]["output_css"]
    head.append(style)
    if cfg["html"]["remove_data_pid"]:
        for tag in soup.find_all(attrs={"data-pid": True}):
            del tag["data-pid"]
    return str(soup)


def final_integrity(
    final_html: str, blocks: list[Block],
    translations: dict[str, str],
    formatting_incidents: list[dict[str, Any]],
    cfg: dict[str, Any],
) -> dict[str, Any]:
    errors = []
    warnings = []
    if "[[FMT_" in final_html:
        errors.append("FMT marker leaked into final HTML.")
    final_text = BeautifulSoup(
        final_html, "html.parser"
    ).get_text(" ", strip=True)
    if detect_english_sentence(
        final_text, int(cfg["validation"]["english_sequence_min_words"])
    ):
        warnings.append("Possible English sentence remains in final HTML.")
    for block in blocks:
        if block.pid not in translations:
            errors.append(f"Missing translation {block.pid}")
        elif (
            cfg["validation"]["strict_digits"]
            and missing_numeric_values(
                block.digits, translations[block.pid]
            )
        ):
            errors.append(f"{block.pid}: final numeric value mismatch")
    unresolved_incidents = [
        item for item in formatting_incidents
        if item.get("resolution") != "retry_restored" and item.get("status") != "resolved"
    ]
    blocking_incidents = [item for item in unresolved_incidents if item.get("required", True)]
    optional_incidents = [item for item in unresolved_incidents if not item.get("required", True)]
    if blocking_incidents:
        errors.append(f"{len(blocking_incidents)} required inline spans remain unresolved.")
    if optional_incidents:
        warnings.append(f"{len(optional_incidents)} optional inline spans were not restored.")

    # Validate the produced HTML itself, not the model JSON or mapping cache.
    # data-pid may have been removed, so match the preserved leaf-block order.
    output_blocks = leaf_blocks(BeautifulSoup(final_html, "html.parser"), cfg["html"]["block_tags"])
    if len(output_blocks) != len(blocks):
        errors.append("Final HTML block structure mismatch.")
    else:
        for source, output_block in zip(blocks, output_blocks):
            required_by_tag = Counter(
                span.tag for span in source.inline_spans if span.required
            )
            for tag, expected in required_by_tag.items():
                actual = len(output_block.find_all(tag))
                if actual < expected:
                    errors.append(
                        f"{source.pid}: required {tag} spans missing from final HTML "
                        f"({actual}/{expected})"
                    )
    return {
        "ok": not errors, "errors": errors, "warnings": warnings,
        "formatting_incidents": formatting_incidents,
        "formatting_incident_counts": {
            "resolved": sum(item.get("status") == "resolved" for item in formatting_incidents),
            "unresolved_required": len(blocking_incidents),
            "unresolved_optional": len(optional_incidents),
        },
    }


def render_audit_report(
    chapter_name: str, blocks: list[Block],
    draft: dict[str, str], final: dict[str, str],
    issues: list[Issue], integrity: dict[str, Any],
) -> str:
    by_pid: dict[str, list[Issue]] = defaultdict(list)
    for issue in issues:
        by_pid[issue.pid].append(issue)
    block_map = {block.pid: block for block in blocks}
    changed = {
        pid for pid in draft
        if draft.get(pid) != final.get(pid)
    }
    rows = []
    for issue in issues:
        rows.append(
            "<tr>"
            f"<td>{html.escape(issue.issue_id)}</td>"
            f"<td>{html.escape(issue.pid)}</td>"
            f"<td>{html.escape(issue.severity)}</td>"
            f"<td>{html.escape(issue.category)}</td>"
            f"<td>{html.escape(issue.problem)}</td>"
            f"<td>{html.escape(issue.source)}</td>"
            "</tr>"
        )
    cards = []
    for pid in sorted(
        set(by_pid) | changed,
        key=lambda value: block_map[value].index,
    ):
        source_html = block_map[pid].source_html
        cards.append(
            f"""<section class="card">
<h2>{html.escape(pid)}</h2>
<details open><summary>Original with formatting</summary>
<div class="source">{source_html}</div></details>
<div class="grid">
<div><h3>Draft</h3><p>{html.escape(draft.get(pid, ""))}</p></div>
<div><h3>Final</h3><p>{html.escape(final.get(pid, ""))}</p></div>
</div>
<pre>{html.escape(json.dumps(
    [asdict(item) for item in by_pid.get(pid, [])],
    ensure_ascii=False, indent=2
))}</pre>
</section>"""
        )
    return f"""<!doctype html>
<html lang="ru"><head><meta charset="utf-8">
<title>{html.escape(chapter_name)} audit</title>
<style>
body{{font-family:system-ui,sans-serif;max-width:1400px;margin:2em auto;padding:0 1em;background:#f6f7f9}}
table{{border-collapse:collapse;width:100%;background:white}}
th,td{{border:1px solid #ddd;padding:7px;vertical-align:top}}
.card{{background:white;border:1px solid #ddd;border-radius:10px;padding:16px;margin:18px 0}}
.grid{{display:grid;grid-template-columns:1fr 1fr;gap:18px}}
.source,p{{font-family:Georgia,serif;line-height:1.55}}
pre{{white-space:pre-wrap;background:#f2f4f7;padding:12px}}
@media(max-width:800px){{.grid{{grid-template-columns:1fr}}}}
</style></head><body>
<h1>{html.escape(chapter_name)} — v3 audit</h1>
<p>Issues: {len(issues)}. Changed PIDs: {len(changed)}.
Integrity: {"OK" if integrity["ok"] else "FAILED"}.</p>
<table><thead><tr><th>ID</th><th>PID</th><th>Severity</th>
<th>Category</th><th>Problem</th><th>Source</th></tr></thead>
<tbody>{''.join(rows)}</tbody></table>
{''.join(cards) if cards else '<p>No changed or flagged PIDs.</p>'}
</body></html>"""


def aggregate_calls(*clients: ApiClient) -> dict[str, Any]:
    calls = [call for client in clients for call in client.calls]
    prompt_tokens = completion_tokens = 0
    wall = 0.0
    for call in calls:
        usage = call.get("usage") or {}
        prompt_tokens += int(usage.get("prompt_tokens") or 0)
        completion_tokens += int(usage.get("completion_tokens") or 0)
        wall += float(call.get("wall_seconds") or 0)
    return {
        "calls": calls, "call_count": len(calls),
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "wall_seconds": round(wall, 3),
    }


class Runner:
    def __init__(self, cfg: dict[str, Any]):
        self.cfg = cfg
        self.translator = ApiClient(
            cfg["translator_api"], "translator"
        )
        reviewer_cfg = (
            cfg["reviewer_api"]
            if cfg["reviewer_api"]["enabled"]
            else cfg["translator_api"]
        )
        self.reviewer = ApiClient(reviewer_cfg, "reviewer")
        self.glossary = Glossary(cfg)
        self.book_bible = BookBible(
            Path(cfg["paths"]["book_bible_file"])
        )

    def prepare_chapter(
        self, source_path: Path, force: bool,
    ) -> tuple[Path, str, list[Block], list[Chunk], dict[str, Block]]:
        work = Path(self.cfg["paths"]["work_dir"]) / source_path.stem
        manifest_path = work / "manifest.json"
        raw = source_path.read_text(encoding="utf-8")
        source_hash = sha256_text(raw)
        if force and work.exists():
            shutil.rmtree(work)
        if manifest_path.exists():
            manifest = read_json(manifest_path, {})
            if manifest.get("source_sha256") != source_hash:
                raise PipelineError(
                    f"{source_path.name}: source changed; use --force."
                )
            normalized = (work / "source.normalized.html").read_text(
                encoding="utf-8"
            )
            blocks = blocks_from_manifest(manifest["blocks"])
            chunks = [Chunk(**item) for item in manifest["chunks"]]
        else:
            normalized, blocks = prepare_html(raw, self.cfg)
            chunks = make_chunks(blocks, self.cfg["chunking"])
            work.mkdir(parents=True, exist_ok=True)
            atomic_text(work / "source.normalized.html", normalized)
            atomic_json(manifest_path, {
                "version": __version__, "chapter": source_path.name,
                "source_sha256": source_hash,
                "blocks": blocks_to_manifest(blocks),
                "chunks": [asdict(chunk) for chunk in chunks],
                "created_at": utc_now(),
            })
        return (
            work, normalized, blocks, chunks,
            {block.pid: block for block in blocks},
        )

    def get_chapter_bible(
        self, source_path: Path, work: Path, blocks: list[Block],
    ) -> dict[str, Any]:
        path = work / "chapter_bible.json"
        if path.exists():
            return read_json(path, {})
        if not self.cfg["chapter_bible"]["enabled"]:
            return {}
        messages = chapter_bible_messages(
            blocks, self.glossary, self.book_bible
        )
        stage = self.cfg["chapter_bible"]
        max_tokens = fit_output_budget(
            self.translator, messages, stage, int(stage["max_tokens"])
        )
        logging.info("chapter bible %s", source_path.name)
        generation = self.translator.complete(
            messages, stage, max_tokens,
            f"chapter_bible:{source_path.stem}",
        )
        try:
            bible = safe_json_loads(generation.content)
        except Exception as exc:
            if stage["required"]:
                raise
            logging.warning("Chapter bible failed: %s", exc)
            bible = {}
        bible, bible_changes = sanitize_generated_bible(
            bible, self.glossary
        )
        atomic_json(path, bible)
        source_text = "\n".join(block.source_text for block in blocks)
        terms = bible.get("terms") or []
        if isinstance(terms, list):
            enriched_terms = []
            for term in terms:
                if not isinstance(term, dict):
                    continue
                item = dict(term)
                source = norm(str(item.get("english") or item.get("source") or ""))
                if not item.get("source_pids") and source:
                    item["source_pids"] = [
                        block.pid for block in blocks
                        if re.search(re.escape(source), block.source_text, flags=re.I)
                    ]
                item.setdefault("model", self.translator.cfg.get("model", ""))
                enriched_terms.append(item)
            terms = enriched_terms
        glossary_stats = (
            self.glossary.update(source_path.name, source_text, terms)
            if isinstance(terms, list) else {}
        )
        self.book_bible.merge_chapter(source_path.name, bible)
        atomic_json(work / "chapter_bible.meta.json", {
            "generation": self.translator.calls[-1]
            if self.translator.calls else {},
            "glossary_update": glossary_stats,
            "target_sanitization_changes": bible_changes,
        })
        return bible

    def translate(
        self, work: Path, blocks: list[Block], chunks: list[Chunk],
        block_map: dict[str, Block], chapter_bible: dict[str, Any],
    ) -> dict[str, str]:
        drafts_dir = work / "drafts"
        meta_dir = work / "meta"
        drafts_dir.mkdir(parents=True, exist_ok=True)
        meta_dir.mkdir(parents=True, exist_ok=True)
        accepted: dict[str, str] = {}
        source_scene_map = read_json(work / "source_scene_map.json", {})
        for chunk in chunks:
            draft_path = drafts_dir / f"{chunk.chunk_id}.json"
            meta_path = meta_dir / f"{chunk.chunk_id}.translation.json"
            if draft_path.exists():
                saved = read_json(draft_path, {})
                current = saved.get("translations") or {}
                if all(pid in current for pid in chunk.pids):
                    accepted.update(
                        {pid: current[pid] for pid in chunk.pids}
                    )
                    continue
            translated, meta = generate_translation_recursive(
                chunk, self.translator, self.cfg, self.glossary,
                self.book_bible, chapter_bible, source_scene_map, blocks, block_map,
                accepted, drafts_dir, meta_dir,
            )
            accepted.update(translated)
            atomic_json(draft_path, {
                "chunk_id": chunk.chunk_id,
                "translations": {
                    pid: translated[pid] for pid in chunk.pids
                },
            })
            atomic_json(meta_path, meta)
        atomic_json(work / "draft_translations.json", accepted)
        return accepted

    def audit(
        self, work: Path, blocks: list[Block], chunks: list[Chunk],
        block_map: dict[str, Block], chapter_bible: dict[str, Any],
        draft: dict[str, str], redo: bool,
    ) -> list[Issue]:
        if redo:
            shutil.rmtree(work / "audit", ignore_errors=True)
            for name in ("deterministic_issues.json", "issues.json"):
                path = work / name
                if path.exists():
                    path.unlink()
        deterministic_path = work / "deterministic_issues.json"
        if deterministic_path.exists():
            deterministic = [
                Issue(**item)
                for item in read_json(deterministic_path, [])
            ]
        else:
            deterministic = deterministic_issues(
                blocks, draft, self.cfg, self.glossary,
                chapter_bible, self.book_bible,
            )
            atomic_json(
                deterministic_path,
                [asdict(issue) for issue in deterministic],
            )
        issues_path = work / "issues.json"
        if issues_path.exists():
            return [
                Issue(**item) for item in read_json(issues_path, [])
            ]
        issues = run_audit(
            chunks, self.reviewer, self.cfg, self.glossary,
            self.book_bible, chapter_bible, block_map, draft,
            deterministic, work / "audit",
        )
        atomic_json(issues_path, [asdict(issue) for issue in issues])
        return issues

    def repair(
        self, work: Path, block_map: dict[str, Block],
        chapter_bible: dict[str, Any], draft: dict[str, str],
        issues: list[Issue], redo: bool,
    ) -> tuple[dict[str, str], list[dict[str, Any]]]:
        if redo:
            shutil.rmtree(work / "repairs", ignore_errors=True)
            shutil.rmtree(work / "post_repair_verifier", ignore_errors=True)
            for name in (
                "repaired_translations.json", "repaired_translations.preverify.json",
                "repair_records.json", "post_repair_report.json",
                "repair_retry_requests.json", "repair_retry_records.json",
                "issue_lifecycle.json",
            ):
                path = work / name
                if path.exists():
                    path.unlink()
            for path in work.glob("repair_retry_round_*.json"):
                path.unlink()
        repaired_path = work / "repaired_translations.json"
        records_path = work / "repair_records.json"
        if repaired_path.exists():
            return (
                read_json(repaired_path, draft),
                read_json(records_path, []),
            )
        if not self.cfg["repair"]["enabled"]:
            return draft, []
        repaired, records = run_repairs(
            self.translator, self.cfg, self.glossary,
            self.book_bible, chapter_bible, block_map, draft,
            issues, work / "repairs",
        )
        atomic_json(repaired_path, repaired)
        atomic_json(records_path, records)
        return repaired, records

    def finalize(
        self, source_path: Path, work: Path, normalized: str,
        blocks: list[Block], block_map: dict[str, Block],
        draft: dict[str, str], final_translations: dict[str, str],
        issues: list[Issue], repair_records: list[dict[str, Any]],
        redo_formatting: bool,
    ) -> Path:
        post_cfg = self.cfg.get("post_repair_verifier", {})
        if post_cfg.get("enabled", False) and post_cfg.get("required", False):
            post_report_path = work / "post_repair_report.json"
            if not post_report_path.exists():
                raise PipelineError("Required post-repair verification report is missing.")
            post_report = read_json(post_report_path, {})
            unresolved = int(post_report.get("unresolved_total", post_report.get("retry_required", 0)))
            if unresolved:
                raise PipelineError(
                    f"Cannot finalize: {unresolved} confirmed repair issue(s) remain unresolved."
                )
        if redo_formatting:
            shutil.rmtree(work / "formatting", ignore_errors=True)
        formatted, formatting_incidents = run_formatting(
            self.translator, self.cfg, blocks, block_map,
            final_translations, work / "formatting",
        )
        final_html = build_final_html(
            normalized, blocks, final_translations, formatted,
            self.cfg, source_path.name,
        )
        integrity = final_integrity(
            final_html, blocks, final_translations,
            formatting_incidents, self.cfg,
        )
        if not integrity["ok"]:
            raise PipelineError(
                "Final integrity failed: "
                + "; ".join(integrity["errors"])
            )
        output = Path(
            self.cfg["paths"]["output_dir"]
        ) / source_path.name
        atomic_text(output, final_html)
        report = {
            "version": __version__, "chapter": source_path.name,
            "integrity": integrity,
            "issue_counts": dict(Counter(
                issue.severity for issue in issues
            )),
            "issues": [asdict(issue) for issue in issues],
            "repair_records": repair_records,
            "issue_lifecycle": read_json(work / "issue_lifecycle.json", []),
            "changed_pids": [
                pid for pid in final_translations
                if draft.get(pid) != final_translations.get(pid)
            ],
            "api": aggregate_calls(self.translator, self.reviewer),
            "completed_at": utc_now(),
        }
        atomic_json(work / "quality_report.json", report)
        atomic_text(
            work / "audit_report.html",
            render_audit_report(
                source_path.name, blocks, draft,
                final_translations, issues, integrity,
            ),
        )
        atomic_json(work / "state.json", {
            "status": "complete", "output": str(output),
            "completed_at": utc_now(),
        })
        logging.info("%s -> %s", source_path.name, output)
        return output

    def process(
        self, source_path: Path, phase: str, force: bool,
        redo_audit: bool, redo_repair: bool,
        redo_formatting: bool,
    ) -> Optional[Path]:
        work, normalized, blocks, chunks, block_map = self.prepare_chapter(
            source_path, force
        )
        bible = self.get_chapter_bible(source_path, work, blocks)
        draft_path = work / "draft_translations.json"
        if phase in {"all", "translate"}:
            draft = self.translate(
                work, blocks, chunks, block_map, bible
            )
            if phase == "translate":
                return None
        else:
            if not draft_path.exists():
                raise PipelineError("Run translation phase first.")
            draft = read_json(draft_path, {})

        if phase in {"all", "audit"}:
            issues = self.audit(
                work, blocks, chunks, block_map, bible,
                draft, redo_audit,
            )
            if phase == "audit":
                return None
        else:
            issues_path = work / "issues.json"
            if not issues_path.exists():
                raise PipelineError("Run audit phase first.")
            issues = [
                Issue(**item) for item in read_json(issues_path, [])
            ]

        if phase in {"all", "repair"}:
            repaired, records = self.repair(
                work, block_map, bible, draft, issues, redo_repair
            )
            if phase == "repair":
                return None
        else:
            repaired = read_json(
                work / "repaired_translations.json", draft
            )
            records = read_json(work / "repair_records.json", [])

        return self.finalize(
            source_path, work, normalized, blocks, block_map,
            draft, repaired, issues, records, redo_formatting,
        )


def setup_logging(cfg: dict[str, Any]) -> Path:
    logs = Path(cfg["paths"]["logs_dir"])
    logs.mkdir(parents=True, exist_ok=True)
    path = logs / f"run_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log"
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s | %(message)s",
        handlers=[
            logging.StreamHandler(sys.stdout),
            logging.FileHandler(path, encoding="utf-8"),
        ],
        force=True,
    )
    return path


def resolve_paths(
    cfg: dict[str, Any], config_path: Path,
) -> dict[str, Any]:
    base = config_path.parent.resolve()
    for key, value in list(cfg["paths"].items()):
        path = Path(value)
        if not path.is_absolute():
            cfg["paths"][key] = str((base / path).resolve())
    return cfg


def select_files(
    cfg: dict[str, Any], start: Optional[int], end: Optional[int],
) -> list[Path]:
    manifest_value = cfg["paths"].get("chapter_manifest_file")
    if manifest_value:
        resolver_dir = Path(__file__).resolve().parent / "pact_full_pipeline_runner_v1"
        if str(resolver_dir) not in sys.path:
            sys.path.insert(0, str(resolver_dir))
        from v31_chapter_resolver import chapters_from_manifest
        return chapters_from_manifest(
            Path(manifest_value), Path(__file__).resolve().parent,
            Path(cfg["paths"]["input_dir"]), start, end,
        )
    files = sorted(
        Path(cfg["paths"]["input_dir"]).glob("*.html"),
        key=lambda path: natural_key(path.name),
    )
    if not files:
        raise PipelineError(
            f"No HTML files in {cfg['paths']['input_dir']}"
        )
    first = max(1, start or 1)
    last = min(len(files), end or len(files))
    return files[first - 1:last] if first <= last else []


def self_test(cfg: dict[str, Any]) -> int:
    """Run deterministic tests without reading or modifying project state."""
    sample = """<!doctype html><html><head><title>Bonds 1.1</title></head>
<body><h1>Bonds 1.1</h1>
<p>“Jesus fuck,” Paige said.</p>
<p>I parked my bike. Molly and I both stopped.</p>
<p>I would argue <em>they</em> are the problem.</p>
</body></html>"""

    # Self-test must be isolated from the user's real glossary/book bible.
    with tempfile.TemporaryDirectory(prefix="pact_v3_selftest_") as temp_name:
        temp_root = Path(temp_name)
        test_cfg = copy.deepcopy(cfg)
        test_cfg["paths"]["glossary_dir"] = str(temp_root / "glossary")
        test_cfg["paths"]["book_bible_file"] = str(
            temp_root / "book_bible.json"
        )

        glossary_dir = Path(test_cfg["paths"]["glossary_dir"])
        glossary_dir.mkdir(parents=True, exist_ok=True)
        for name in (
            "locked.json", "established.json",
            "provisional.json", "conflicts.json"
        ):
            atomic_json(glossary_dir / name, {})
        atomic_json(
            Path(test_cfg["paths"]["book_bible_file"]),
            {
                "version": 1,
                "characters": {},
                "entities": {},
                "address_register": [],
                "facts": [],
                "chapters": [],
            },
        )

        normalized, blocks = prepare_html(sample, test_cfg)
        assert make_chunks(blocks, {
            "target_words": 8, "min_words": 4, "max_words": 12
        })

        translations = {
            blocks[0].pid: "Узы 1.1",
            blocks[1].pid: "— Господи Иисусе, — сказала Пэйдж.",
            blocks[2].pid: (
                "Я припарковал велосипед. Мы обе остановились."
            ),
            blocks[3].pid: "Я бы сказал, что они — проблема.",
        }

        glossary = Glossary(test_cfg)
        bible = BookBible(
            Path(test_cfg["paths"]["book_bible_file"])
        )
        chapter_bible = {
            "pov": {"gender": "male"},
            "entities": [{
                "source": "bike",
                "target": "мотоцикл",
                "forbidden_targets": ["велосипед"],
            }],
        }

        issues = deterministic_issues(
            blocks, translations, test_cfg,
            glossary, chapter_bible, bible,
        )
        categories = {issue.category for issue in issues}
        assert "tone_profanity" in categories, categories
        assert "entity_consistency" in categories, categories
        assert "narrator_gender" in categories, categories

        span_block = blocks[3]
        span = span_block.inline_spans[0]
        inner, incidents = apply_inline_mappings(
            translations[span_block.pid],
            span_block.inline_spans,
            {
                (span_block.pid, span.span_id): {
                    "target_text": "они",
                    "occurrence": 1,
                }
            },
            span_block.pid,
        )
        assert not incidents, incidents
        assert "<em>они</em>" in inner

        formatted = {
            block.pid: html.escape(translations[block.pid])
            for block in blocks
        }
        formatted[span_block.pid] = inner
        final = build_final_html(
            normalized, blocks, translations,
            formatted, test_cfg, "test.html",
        )
        assert "<title>Узы 1.1</title>" in final
        assert "data-pid" not in final

        # Verify that type="character" can be promoted. Use a synthetic
        # name so the result cannot collide with a locked project entry.
        glossary.update(
            "test.html",
            "TestCharacter TestCharacter",
            [{
                "english": "TestCharacter",
                "russian": "ТестовыйПерсонаж",
                "type": "character",
            }],
        )
        assert (
            glossary.target(
                glossary.established.get("TestCharacter")
            )
            == "ТестовыйПерсонаж"
        )

        # Numeric values may be rendered as words without a false failure.
        assert missing_numeric_values(["3"], "правило трёх") == []
        assert missing_numeric_values(["3"], "никакого правила") == ["3"]
        assert mixed_script_tokens("Mary кивнула.") == ["Mary"]
        assert mixed_script_tokens("Мэри кивнула.") == []
        assert mixed_script_tokens("Mэри кивнула.") == ["Mэри"]
        assert mixed_script_tokens("GPS работает.", ["GPS"]) == []
        assert mixed_script_tokens("https://example.com Mary") == ["Mary"]
        assert missing_written_number_words(
            "three hour window", "трехчасовое окно"
        ) == set()
        assert missing_written_number_words(
            "the three around him", "трое вокруг него"
        ) == set()
        assert missing_written_number_words(
            "three seconds", "пара секунд"
        ) == {"three"}

        # Glossary checks must use term boundaries and allow Russian inflection.
        assert not source_term_present("city layout", "Ty")
        assert not source_term_present("Other options", "Other")
        assert source_term_present("a particular Other", "Other")
        assert source_term_present("The Others arrived", "Others")
        assert target_form_present("рядом с Дунканом Бехаймом", "Дункан Бехайм")
        assert target_form_present("для Завоевателя", "Завоеватель")

    print(f"Self-test passed (version {__version__})")
    return 0


def smoke_test(cfg: dict[str, Any]) -> int:
    client = ApiClient(cfg["translator_api"], "translator")
    generation = client.complete(
        [
            {
                "role": "system",
                "content": (
                    'Translate to Russian. Return JSON '
                    '{"translations":[{"pid":"p1","text":"..."}]}. '
                    "No reasoning."
                ),
            },
            {
                "role": "user",
                "content": '<BLOCK pid="p1">The door opened quietly.</BLOCK>',
            },
        ],
        {
            "temperature": 0.0, "top_p": 1.0,
            "top_k": 1, "enable_thinking": False,
        },
        128, "smoke",
    )
    print(json.dumps({
        "content": generation.content,
        "reasoning_content_chars": len(generation.reasoning),
        "finish_reason": generation.finish_reason,
        "usage": generation.usage,
        "wall_seconds": generation.wall_seconds,
        "enable_thinking_sent": False,
    }, ensure_ascii=False, indent=2))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Autonomous literary translation pipeline v3"
    )
    parser.add_argument("--config", default="config.v3.json")
    parser.add_argument("--start", type=int)
    parser.add_argument("--end", type=int)
    parser.add_argument(
        "--phase",
        choices=["all", "translate", "audit", "repair", "finalize"],
        default="all",
    )
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--redo-audit", action="store_true")
    parser.add_argument("--redo-repair", action="store_true")
    parser.add_argument("--redo-formatting", action="store_true")
    parser.add_argument("--plan", action="store_true")
    parser.add_argument("--self-test", action="store_true")
    parser.add_argument("--smoke-test", action="store_true")
    parser.add_argument(
        "--version", action="version",
        version=f"%(prog)s {__version__}",
    )
    return parser


def main() -> int:
    args = build_parser().parse_args()
    config_path = Path(args.config).resolve()
    cfg = resolve_paths(
        merge(DEFAULTS, read_json(config_path, {})), config_path
    )
    setup_logging(cfg)
    logging.info(
        "version=%s translator=%s reviewer=%s context=%s chunk=%s",
        __version__, cfg["translator_api"]["model"],
        cfg["reviewer_api"]["model"],
        cfg["translator_api"]["context_size"],
        cfg["chunking"]["target_words"],
    )
    if args.self_test:
        return self_test(cfg)
    if args.smoke_test:
        return smoke_test(cfg)
    files = select_files(cfg, args.start, args.end)
    if args.plan:
        for index, path in enumerate(files, 1):
            _, blocks = prepare_html(
                path.read_text(encoding="utf-8"), cfg
            )
            chunks = make_chunks(blocks, cfg["chunking"])
            logging.info(
                "[%s/%s] %s: words=%s blocks=%s chunks=%s",
                index, len(files), path.name,
                sum(block.word_count for block in blocks),
                len(blocks),
                ", ".join(
                    f"{chunk.chunk_id}:{chunk.word_count}"
                    for chunk in chunks
                ),
            )
        return 0
    runner = Runner(cfg)
    failures = []
    for index, path in enumerate(files, 1):
        logging.info("[%s/%s] %s", index, len(files), path.name)
        try:
            runner.process(
                path, args.phase, args.force,
                args.redo_audit, args.redo_repair,
                args.redo_formatting,
            )
        except Exception as exc:
            logging.exception("%s failed: %s", path.name, exc)
            failures.append((path.name, str(exc)))
    failures_path = Path(cfg["paths"]["logs_dir"]) / "failures_latest.json"
    if failures:
        atomic_json(
            failures_path,
            [{"chapter": chapter, "error": error}
             for chapter, error in failures],
        )
        return 1
    atomic_json(failures_path, [])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
