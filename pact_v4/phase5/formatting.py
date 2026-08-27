"""Phase 5: translation-time formatting alignment (the §8.14 span contract).

Backing spec:

  * ``docs/architecture/PACT_RESPONSE_TO_CLAUDE_REVISED_ACCEPTED_PLAN_RU.md``
    §8.14 ("Translation-time formatting contract") and §6.1 ("Blocking
    formatting integrity"): every source inline span receives a mapping
    ``{span_id, translated_text, occurrence}``; the code verifies the
    substring exists, the occurrence is unambiguous, spans do not conflict,
    and every required span is mapped.
  * ``docs/architecture/V4_FINAL_REVIEW_AND_IMPLEMENTATION_PLAN_RU_v2.md``
    ("Phase 5 — formatting alignment") and ``docs/plans/
    V4_IMPLEMENTATION_ORDER_PLAN_RU.md`` §4 B3.
  * ``docs/architecture/V4_MVP_SPEC_RU.md`` §2 Step 6/8: the formatting
    contract is applied **before** Step 8 so the final integrity check and
    the terminal transition see the same text that goes into ``complete``.
  * ``docs/plans/V4_1_WHOLE_CHAPTER_ARCHITECTURE_PLAN_RU.md`` §8-C and
    ``docs/plans/V4_1_AUDIT_B1_RU.md`` §11 (card C): formatting is
    **model-free** — the rule "formatting = 0 model calls". The model
    fallback tier was removed; only the deterministic tiers remain.

What this module implements is the *restoration* half of the formatting
contract: the inline HTML spans (``em``/``strong``/``i``/``b``/``a``)
extracted at source parse time (``pact_v4.phase0b.source_html``) are
re-located in the translated Russian text and re-wrapped, so the final
chapter text carries the source's emphasis. The *verification* half — PID
coverage / numbers / mixed-script / glossary over the whole chapter — is the
Step 8 deterministic integrity check in ``pact_v4.phase4.repair``
(``run_integrity_check``), which now runs over the formatted text.

Key rules (owner decisions, DECISIONS.md 2026-08-02 / 2026-08-05; card C
2026-08-10):

  * Formatting is **wrap-only**: it never rewrites the translated text, it
    only locates fragments and wraps them in the source tags. The visible
    content is therefore identical to the repaired text, so Step 8's
    conditional narrow Qwen smoke (``_needs_qwen_smoke``) cannot be tripped
    by formatting alone.
  * Formatting apply step is **model-free** (card C for wrap): all wrap
    tiers are deterministic — ``preserved``/``exact``/``occurrence_aware``/
    ``fuzzy``. The *resolution* of ``target_text`` (Russian substring) for
    EN→RU is a separate targeted model-call ``resolve_format_mappings``
    (port of V3 ``formatting_messages`` + ``parse_format_mappings``).
    Deviation from card C (card C assumed deterministic tiers sufficient
    for EN→RU; POC 0/69 proved they are not, owner approved targeted
    formatting model-call per v41 proposal). The wrap (``apply_span_mappings``)
    remains model-free.
  * Every span resolution records its tier with the located range — no
    silent fallback anywhere.
  * Every unresolved required span is a blocking incident; the policy limit
    ``max_formatting_incidents`` (production default ``0``; book-production
    lenient default via ``v4_book_run.py``) decides whether the chapter can
    be ``complete``. Violating it yields ``accepted_degraded`` when the
    output profile remains structurally valid (a valid PID map) or ``failed``
    otherwise. Unresolved spans are debt, never a silent loss.

The module deliberately never imports ``pact_v4.runtime.model_lifecycle`` /
``model_lifecycle_adapters`` / ``ModelRouter`` / ``backend_role_adapters``
(dual-mode rule, now trivially satisfied — there is no transport at all
except via the injected formatting client in ``resolve_format_mappings``).
"""
from __future__ import annotations

import html
import json
import logging
import re
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from pact_v4.phase0b.source_html import SourceBlock, SourceSpan

LOG = logging.getLogger(__name__)

__all__ = [
    "FORMATTING_POLICY_VERSION",
    "FORMATTING_OUTCOME_SCHEMA",
    "FORMATTING_REPORT_SCHEMA",
    "MAX_FORMATTING_INCIDENTS_DEFAULT",
    "TIER_PRESERVED",
    "TIER_EXACT",
    "TIER_OCCURRENCE",
    "TIER_FUZZY",
    "TIER_MODEL_TARGET",
    "FormattingIncident",
    "SpanMappingRecord",
    "FormattingOutcome",
    "occurrence_ranges",
    "find_nonoverlapping_occurrence",
    "apply_span_mappings",
    "formatting_messages",
    "parse_format_mappings",
    "resolve_format_mappings",
    "run_formatting_align",
]

FORMATTING_POLICY_VERSION = "pact-v4-formatting/v1"
FORMATTING_OUTCOME_SCHEMA = "pact-v4-formatting-outcome/v1"
FORMATTING_REPORT_SCHEMA = "pact-v4-formatting-report/v1"
MAX_FORMATTING_INCIDENTS_DEFAULT = 0

# Deterministic tiers only (card C: formatting = 0 model calls). The former
# ``model_fallback`` tier was removed. TIER_MODEL_TARGET is the *resolution* tier
# for spans located via a targeted model-call (resolve_format_mappings) —
# the wrap itself remains deterministic (find_nonoverlapping_occurrence +
# apply_span_mappings). This documents the v41 deviation from card C.
TIER_PRESERVED = "preserved"
TIER_EXACT = "exact"
TIER_OCCURRENCE = "occurrence_aware"
TIER_FUZZY = "fuzzy"
TIER_MODEL_TARGET = "model_target"

# Default formatting model-call config (mirror V3 Defaults["formatting"]).
# v41 fix: max_tokens is dynamic sentinel (None) — _effective_max_tokens computes
# per-batch budget (40*spans+500, min 800 cap 8192). None means "use dynamic"
# without forcing the legacy 1600 which starved small calls.
DEFAULT_FORMATTING_CFG: Dict[str, Any] = {
    "enabled": True,
    "required": False,
    "temperature": 0.1,
    "top_p": 0.9,
    "top_k": 32,
    "enable_thinking": False,
    "max_tokens": None,
    "generation_retries": 2,
    "tags": ["em", "strong", "i", "b", "a"],
    "required_tags": ["em", "strong", "i", "b", "a"],
    "optional_tags": [],
    "max_blocks_per_call": 12,
    "retry_unresolved_spans": True,
    "on_failure": "omit_tag",
    "formatting_single_call_whole_chapter": True,
}

# v41: dynamic max_tokens scaling constants
_FORMATTING_TOKENS_PER_SPAN = 40
_FORMATTING_TOKENS_OVERHEAD = 500
_FORMATTING_MIN_TOKENS = 800
_FORMATTING_MAX_TOKENS_CAP = 8192
_FORMATTING_SINGLE_CALL_SPAN_LIMIT = 80
_FORMATTING_SINGLE_CALL_PROMPT_LIMIT = 12000


def _effective_max_tokens(span_count: int, cfg_max: Any) -> int:
    """v41 dynamic budget: max(800, 40*span_count+500, cfg_max) capped at 8192."""
    if cfg_max is None:
        cfg_val = 0
    else:
        try:
            cfg_val = int(cfg_max)
        except Exception:
            cfg_val = 0
    needed = _FORMATTING_TOKENS_PER_SPAN * int(span_count) + _FORMATTING_TOKENS_OVERHEAD
    return min(_FORMATTING_MAX_TOKENS_CAP, max(_FORMATTING_MIN_TOKENS, needed, cfg_val))

# Word-boundary charset matches ``_SOURCE_BOUNDARY`` in
# ``pact_v4._integrity_checks`` (same convention as the glossary/number
# checks, so a needle is never matched as a substring of a larger token).
_WORD_BOUNDARY = r"A-Za-z0-9_"

# A resolved span's fragment must be non-empty and free of placeholder
# markers. The marker check mirrors v3's "FMT marker leaked into final HTML"
# guard: no placeholder of ours may ever reach the output text.
_MARKER_RE = re.compile(r"\[\[FMT_|@@FMT|%%FMT|<<FMT")

_CURVE_QUOTES = str.maketrans({
    "“": '"', "”": '"', "‘": "'", "’": "'",
})

# Inline tags whose presence in the translated text counts as "already
# restored" for the preserved tier (same set as ``source_html``).
_INLINE_TAG_OPEN_RE = re.compile(r"<(em|strong|i|b|a)\b[^>]*>")

# All inline open/close tokens (``<em>``, ``</em>``, ``<strong …>`` …) — the
# preserved tier must detect ANY unbalanced/orphaned/malformed token, not
# only balanced pairs (RV2 finding: an unclosed opening tag or an orphan
# closing tag must become ``preserved_tag_mismatch`` debt, never fall
# through to the text tiers and double-wrap the verbatim fragment).
_INLINE_TAG_TOKEN_RE = re.compile(r"</?(em|strong|i|b|a)\b[^>]*>")


def _fold(text: str) -> str:
    """Conservative normalization used for grouping and fuzzy matching."""
    return text.casefold().replace("ё", "е").translate(_CURVE_QUOTES)


# ---------------------------------------------------------------------------
# Span mapping / incident records
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SpanMappingRecord:
    """One resolved source span -> located translation fragment."""

    pid: str
    span_id: str
    tag: str
    source_text: str
    translated_text: str
    occurrence: int
    tier: str
    start: int
    end: int
    attrs: Mapping[str, str] = field(default_factory=dict)
    preserved: bool = False

    def to_payload(self) -> Dict[str, Any]:
        return {
            "pid": self.pid,
            "span_id": self.span_id,
            "tag": self.tag,
            "source_text": self.source_text,
            "translated_text": self.translated_text,
            "occurrence": self.occurrence,
            "tier": self.tier,
            "start": self.start,
            "end": self.end,
            "attrs": dict(sorted(self.attrs.items())),
            "preserved": self.preserved,
        }


@dataclass(frozen=True)
class FormattingIncident:
    """One unresolved required inline span."""

    pid: str
    span_id: str
    tier: str
    reason: str
    required: bool = True
    detail: str = ""

    def to_payload(self) -> Dict[str, Any]:
        return {
            "pid": self.pid,
            "span_id": self.span_id,
            "tier": self.tier,
            "reason": self.reason,
            "required": self.required,
            "detail": self.detail,
        }


@dataclass(frozen=True)
class FormattingOutcome:
    """Result of one ``run_formatting_align`` call over a chapter."""

    formatted_text: Tuple[Tuple[str, str], ...]
    span_mapping: Tuple[SpanMappingRecord, ...]
    incidents: Tuple[FormattingIncident, ...]
    backend_identity_hash: str
    policy_version: str
    max_formatting_incidents: int
    model_fallback_count: int = 0
    model_call_count: int = 0

    @property
    def incident_count(self) -> int:
        return len(self.incidents)

    @property
    def resolved_count(self) -> int:
        return len(self.span_mapping)

    @property
    def blocking(self) -> bool:
        return self.incident_count > self.max_formatting_incidents

    def as_pid_map(self) -> Dict[str, str]:
        return dict(self.formatted_text)

    def to_payload(self) -> Dict[str, Any]:
        return {
            "schema": FORMATTING_OUTCOME_SCHEMA,
            "policy_version": self.policy_version,
            "backend_identity_hash": self.backend_identity_hash,
            "formatted_text": [list(item) for item in self.formatted_text],
            "span_mapping": [record.to_payload() for record in self.span_mapping],
            "incidents": [incident.to_payload() for incident in self.incidents],
            "resolved_count": self.resolved_count,
            "incident_count": self.incident_count,
            "model_fallback_count": self.model_fallback_count,
            "model_call_count": self.model_call_count,
            "max_formatting_incidents": self.max_formatting_incidents,
            "blocking": self.blocking,
        }


# ---------------------------------------------------------------------------
# Occurrence helpers (word-boundary aware for the deterministic tiers)
# ---------------------------------------------------------------------------


def occurrence_ranges(
    text: str, needle: str, *, word_boundary: bool = False
) -> List[Tuple[int, int]]:
    if not needle:
        return []
    escaped = re.escape(needle)
    if word_boundary:
        escaped = rf"(?<![{_WORD_BOUNDARY}]){escaped}(?![{_WORD_BOUNDARY}])"
    matches = list(re.finditer(escaped, text))
    if not matches:
        matches = list(re.finditer(escaped, text, flags=re.I))
    return [(match.start(), match.end()) for match in matches]


def find_nonoverlapping_occurrence(
    text: str,
    needle: str,
    preferred: int,
    occupied: Sequence[Tuple[int, int]],
    *,
    word_boundary: bool = False,
) -> Optional[Tuple[int, int]]:
    ranges = occurrence_ranges(text, needle, word_boundary=word_boundary)
    if not ranges:
        return None
    order = list(range(len(ranges)))
    preferred_index = preferred - 1
    if 0 <= preferred_index < len(ranges):
        order.remove(preferred_index)
        order.insert(0, preferred_index)
    for index in order:
        start, end = ranges[index]
        if not any(not (end <= a or start >= b) for a, b in occupied):
            return start, end
    return None


def _fuzzy_pattern(needle: str) -> str:
    parts: List[str] = []
    for ch in _fold(needle):
        if ch.isspace():
            parts.append(r"\s+")
        elif ch == "\u0435":
            parts.append("[еЕёЁ]")
        elif ch == "-" or ch in "–—":
            parts.append(r"[-–—\s]+")
        elif ch in "'":
            parts.append(r"['’]")
        elif ch in '"':
            parts.append('["”]')
        else:
            parts.append(re.escape(ch))
    pattern = "".join(parts)
    if _fold(needle) and (_fold(needle)[0].isalnum() or _fold(needle)[-1].isalnum()):
        pattern = rf"(?<![{_WORD_BOUNDARY}]){pattern}(?![{_WORD_BOUNDARY}])"
    return pattern


# ---------------------------------------------------------------------------
# Formatting model-call helpers (port of V3 formatting_messages /
# parse_format_mappings) — targeted model-call to produce target_text
# ---------------------------------------------------------------------------

def _norm_ws(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def _clean_json_text(text: str) -> str:
    t = text.strip()
    if t.startswith("```"):
        t = re.sub(r"^```(?:json)?\s*", "", t, flags=re.I)
        t = re.sub(r"\s*```$", "", t)
    start = t.find("{")
    end = t.rfind("}")
    if start >= 0 and end >= start:
        t = t[start:end + 1]
    return t.strip()


def formatting_messages(
    pids: Sequence[str],
    block_map: Mapping[str, SourceBlock],
    translations: Mapping[str, str],
    span_filter: Optional[Mapping[str, Any]] = None,
    *,
    retry: bool = False,
) -> List[Dict[str, str]]:
    """Build formatting model-call messages (port of V3 formatting_messages).

    System instructs model to be a formatting specialist: do not change text,
    for each SOURCE_SPAN find its place in TRANSLATION; target_text is an
    exact substring of TRANSLATION; on ambiguity specify occurrence.
    """
    retry_rule = (
        "Это повторная попытка только для ранее не восстановленных spans. "
        "Если английское выделенное слово не имеет прямого русского аналога "
        "из-за грамматики (например, опущенная связка), выбери минимальную "
        "русскую фразу, несущую тот же смысловой акцент."
        if retry else ""
    )
    system = (
        "Восстанови только смысловой курсив/жирный текст/ссылки.\n"
        "Не переписывай перевод. Для каждого SOURCE_SPAN найди точную непрерывную\n"
        "подстроку в TRANSLATION. target_text должен дословно встречаться в переводе.\n"
        "Для нескольких одинаковых выделений выбирай разные occurrence по порядку.\n"
        "Не возвращай пустую строку, если смысловой акцент можно перенести на ближайший\n"
        "русский эквивалент или короткую фразу. "
        + retry_rule
        + "\nЕсли соответствия действительно нет, верни пустую строку.\n\n"
        'Строго JSON:\n{"mappings":[{"pid":"p00001","span_id":"em01","target_text":"они сами","occurrence":1}]}'
    )
    items: List[str] = []
    for pid in pids:
        block = block_map[pid]
        spans_payload: List[Dict[str, Any]] = []
        for span in block.inline_spans:
            if span_filter is not None and span.span_id not in span_filter.get(pid, set()):
                continue
            spans_payload.append({
                "span_id": span.span_id,
                "tag": span.tag,
                "source_text": span.text,
                "attrs": dict(span.attrs),
                "required": True,
            })
        items.append(
            f'<FORMAT_ITEM pid="{pid}">\n'
            f"<SOURCE>{html.escape(block.text)}</SOURCE>\n"
            f"<SOURCE_SPANS>{html.escape(json.dumps(spans_payload, ensure_ascii=False))}</SOURCE_SPANS>\n"
            f"<TRANSLATION>{html.escape(translations.get(pid, ''))}</TRANSLATION>\n"
            "</FORMAT_ITEM>"
        )
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": "\n".join(items)},
    ]


def parse_format_mappings(
    generation: Any,
    allowed: Mapping[Tuple[str, str], Any],
) -> Dict[Tuple[str, str], Dict[str, Any]]:
    """Parse and validate generation ``{"mappings": [...]}`` (port of V3).

    Returns ``{(pid, span_id): {target_text, occurrence}}``.
    Validates key in allowed, target_text non-empty, occurrence >=1.
    Duplicate keys are ignored (first wins).
    """
    content = getattr(generation, "content", None)
    if content is None and isinstance(generation, dict):
        content = generation.get("content", "")
    text = str(content or "")
    cleaned = _clean_json_text(text)
    try:
        data = json.loads(cleaned)
    except Exception as exc:
        raise ValueError(f"Invalid JSON response: {text[:500]!r}") from exc
    if not isinstance(data, dict):
        raise ValueError("JSON response must be an object")
    raw = data.get("mappings") or []
    if not isinstance(raw, list):
        raise ValueError("mappings must be a list")
    result: Dict[Tuple[str, str], Dict[str, Any]] = {}
    for item in raw:
        if not isinstance(item, dict):
            continue
        pid = _norm_ws(str(item.get("pid") or ""))
        span_id = _norm_ws(str(item.get("span_id") or ""))
        key = (pid, span_id)
        if key not in allowed or key in result:
            continue
        target_text = str(item.get("target_text") or "")
        if not target_text.strip():
            continue
        try:
            occurrence = max(1, int(item.get("occurrence") or 1))
        except Exception:
            occurrence = 1
        result[key] = {"target_text": target_text, "occurrence": occurrence}
    return result


def _formatting_cfg(cfg: Mapping[str, Any]) -> Dict[str, Any]:
    """Extract formatting config with defaults (mirror V3)."""
    # cfg may be the whole book-run cfg or the formatting sub-cfg
    fmt = cfg.get("formatting") if isinstance(cfg.get("formatting"), Mapping) else None
    if fmt is not None:
        merged = dict(DEFAULT_FORMATTING_CFG)
        merged.update(dict(fmt))
        return merged
    # If cfg itself looks like formatting cfg (has max_blocks_per_call etc.), use it
    if any(k in cfg for k in ("max_blocks_per_call", "generation_retries", "max_tokens", "formatting_single_call_whole_chapter")):
        merged = dict(DEFAULT_FORMATTING_CFG)
        merged.update(dict(cfg))
        return merged
    return dict(DEFAULT_FORMATTING_CFG)


def _estimate_prompt_tokens(messages: Sequence[Mapping[str, str]]) -> int:
    """Heuristic token estimate: ~4 chars per token."""
    total_chars = sum(len(str(m.get("content", ""))) for m in messages)
    return max(1, total_chars // 4)


def resolve_format_mappings(
    client: Any,
    cfg: Mapping[str, Any],
    blocks: Sequence[SourceBlock],
    translations: Mapping[str, str],
    *,
    max_blocks_per_call: Optional[int] = None,
    generation_retries: Optional[int] = None,
    out_dir: Optional[Any] = None,
    single_call: Optional[bool] = None,
) -> Dict[Tuple[str, str], Tuple[str, int]]:
    """Resolve ``target_text`` via model-call (port of V3 formatting stage).

    Only PIDs with ``inline_spans`` are sent to the model. Batches of
    ``max_blocks_per_call`` (default 12) are each retried
    ``generation_retries`` times (default 2). A failed batch yields an empty
    mapping for its spans (they become debt incidents, never silent loss).
    PIDs without inline_spans never trigger a model call (early return).

    v41: dynamic max_tokens per batch/single-call (40*span_count+500,
    min 800 cap 8192 respect cfg max), single-call for whole_chapter with
    fallback to batches if prompt>12000 or spans>80, diagnostics artifacts.

    Returns ``{(pid, span_id): (target_text, occurrence)}``.
    """
    fmt_cfg = _formatting_cfg(cfg)
    if not fmt_cfg.get("enabled", True):
        return {}
    blocks_with_spans = [b for b in blocks if b.inline_spans]
    if not blocks_with_spans:
        return {}
    if client is None:
        return {}
    block_map: Dict[str, SourceBlock] = {b.pid: b for b in blocks}
    pids = [b.pid for b in blocks_with_spans]
    max_per_call = int(max_blocks_per_call if max_blocks_per_call is not None else fmt_cfg.get("max_blocks_per_call", 12))
    retries = int(generation_retries if generation_retries is not None else fmt_cfg.get("generation_retries", 2))
    raw_cfg_max = fmt_cfg.get("max_tokens")
    if raw_cfg_max is None:
        cfg_max: Any = None
    else:
        try:
            cfg_max = int(raw_cfg_max)
        except Exception:
            cfg_max = None
    # v41 single-call decision
    use_single = single_call if single_call is not None else bool(fmt_cfg.get("formatting_single_call_whole_chapter", True))
    total_spans = sum(len(block_map[pid].inline_spans) for pid in pids)
    batches: List[List[str]]
    if use_single and total_spans <= _FORMATTING_SINGLE_CALL_SPAN_LIMIT:
        # Estimate prompt tokens for single-call; fallback if too large
        try:
            probe_msgs = formatting_messages(pids, block_map, translations)
            est = _estimate_prompt_tokens(probe_msgs)
        except Exception:
            est = 0
        if est <= _FORMATTING_SINGLE_CALL_PROMPT_LIMIT:
            batches = [pids]
        else:
            batches = [pids[i:i + max_per_call] for i in range(0, len(pids), max_per_call)]
    else:
        batches = [pids[i:i + max_per_call] for i in range(0, len(pids), max_per_call)]

    result: Dict[Tuple[str, str], Tuple[str, int]] = {}
    # Resolve out_dir path once
    out_path = Path(out_dir) if out_dir is not None else None
    if out_path is not None:
        try:
            out_path.mkdir(parents=True, exist_ok=True)
        except Exception:
            pass
    for batch_idx, batch in enumerate(batches):
        allowed: Dict[Tuple[str, str], SourceSpan] = {
            (pid, span.span_id): span
            for pid in batch
            for span in block_map[pid].inline_spans
        }
        span_count = len(allowed)
        effective_max = _effective_max_tokens(span_count, cfg_max)
        batch_mappings: Dict[Tuple[str, str], Dict[str, Any]] = {}
        success = False
        for attempt in range(1, retries + 1):
            generation = None
            messages = formatting_messages(batch, block_map, translations)
            try:
                try:
                    generation = client.complete(messages, fmt_cfg, effective_max, f"formatting:batch{batch_idx + 1}:attempt{attempt}")
                except TypeError:
                    generation = client.complete(messages, fmt_cfg, effective_max)
                # Diagnostics: persist raw/reasoning/messages/meta per call (v41 3.3)
                # v41 round2 fix: preserve per-attempt artifacts so retry overwrites do not lose failure evidence
                if out_path is not None:
                    try:
                        raw_content = getattr(generation, "content", None)
                        if raw_content is None and isinstance(generation, dict):
                            raw_content = generation.get("content", "")
                        raw_text = str(raw_content or getattr(generation, "text", "") or "")
                        reasoning_text = str(getattr(generation, "reasoning", "") or getattr(generation, "reasoning_content", "") or "")
                        meta = {
                            "batch": batch_idx + 1,
                            "attempt": attempt,
                            "span_count": span_count,
                            "effective_max_tokens": effective_max,
                            "finish_reason": getattr(generation, "finish_reason", None),
                            "usage": getattr(generation, "usage", None),
                            "response_format_attempted": getattr(generation, "response_format_attempted", None),
                        }
                        # canonical (latest) for backward compat
                        (out_path / f"formatting_batch{batch_idx + 1}_raw.txt").write_text(raw_text, encoding="utf-8")
                        (out_path / f"formatting_batch{batch_idx + 1}_reasoning.txt").write_text(reasoning_text, encoding="utf-8")
                        (out_path / f"formatting_batch{batch_idx + 1}_messages.json").write_text(json.dumps(messages, ensure_ascii=False, indent=2), encoding="utf-8")
                        (out_path / f"formatting_batch{batch_idx + 1}_meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
                        # per-attempt (always) — retains failed-attempt diagnostics when retry succeeds
                        (out_path / f"formatting_batch{batch_idx + 1}_attempt{attempt}_raw.txt").write_text(raw_text, encoding="utf-8")
                        (out_path / f"formatting_batch{batch_idx + 1}_attempt{attempt}_reasoning.txt").write_text(reasoning_text, encoding="utf-8")
                        (out_path / f"formatting_batch{batch_idx + 1}_attempt{attempt}_messages.json").write_text(json.dumps(messages, ensure_ascii=False, indent=2), encoding="utf-8")
                        (out_path / f"formatting_batch{batch_idx + 1}_attempt{attempt}_meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
                    except Exception:
                        pass
                parsed = parse_format_mappings(generation, allowed)
                batch_mappings = parsed
                success = True
                break
            except Exception as exc:
                # Enhanced diagnostics on parse failure
                # generation is reset to None at each attempt start, so transport failure (client.complete raises) yields None here
                finish_reason = getattr(generation, "finish_reason", "") if generation is not None else ""
                usage = getattr(generation, "usage", {}) if generation is not None else {}
                response_format_attempted = getattr(generation, "response_format_attempted", "") if generation is not None else ""
                # Also try generation object attributes for call record
                if not finish_reason:
                    try:
                        finish_reason = getattr(generation, "finish_reason", "") or ""
                    except Exception:
                        finish_reason = ""
                raw_preview = ""
                try:
                    raw = getattr(generation, "content", None)
                    if raw is None and isinstance(generation, dict):
                        raw = generation.get("content", "")
                    raw_preview = str(raw or getattr(generation, "text", "") or "")[:500]
                except Exception:
                    raw_preview = ""
                LOG.warning("formatting batch %d attempt %d failed: %s finish_reason=%r usage=%r response_format_attempted=%r max_tokens=%r content_preview=%r", batch_idx + 1, attempt, exc, finish_reason, usage, response_format_attempted, effective_max, raw_preview)
                # Write diagnostics even on failure (raw may be empty) + meta
                # v41 round2: always preserve per-attempt file; canonical only if not already written
                if out_path is not None:
                    try:
                        gen_obj = generation
                        if gen_obj is not None:
                            rc = getattr(gen_obj, "content", None)
                            if rc is None and isinstance(gen_obj, dict):
                                rc = gen_obj.get("content", "")
                            rt = str(rc or getattr(gen_obj, "text", "") or "")
                            rs = str(getattr(gen_obj, "reasoning", "") or getattr(gen_obj, "reasoning_content", "") or "")
                            meta_fail = {
                                "batch": batch_idx + 1,
                                "attempt": attempt,
                                "span_count": span_count,
                                "effective_max_tokens": effective_max,
                                "finish_reason": getattr(gen_obj, "finish_reason", None),
                                "usage": getattr(gen_obj, "usage", None),
                                "response_format_attempted": getattr(gen_obj, "response_format_attempted", None),
                            }
                            # per-attempt always (failure evidence)
                            (out_path / f"formatting_batch{batch_idx + 1}_attempt{attempt}_raw.txt").write_text(rt, encoding="utf-8")
                            (out_path / f"formatting_batch{batch_idx + 1}_attempt{attempt}_reasoning.txt").write_text(rs, encoding="utf-8")
                            (out_path / f"formatting_batch{batch_idx + 1}_attempt{attempt}_messages.json").write_text(json.dumps(messages, ensure_ascii=False, indent=2), encoding="utf-8")
                            (out_path / f"formatting_batch{batch_idx + 1}_attempt{attempt}_meta.json").write_text(json.dumps(meta_fail, ensure_ascii=False, indent=2), encoding="utf-8")
                            # canonical only if not already present (retry path already wrote canonical in try-block)
                            if not (out_path / f"formatting_batch{batch_idx + 1}_raw.txt").exists():
                                (out_path / f"formatting_batch{batch_idx + 1}_raw.txt").write_text(rt, encoding="utf-8")
                            if not (out_path / f"formatting_batch{batch_idx + 1}_reasoning.txt").exists():
                                (out_path / f"formatting_batch{batch_idx + 1}_reasoning.txt").write_text(rs, encoding="utf-8")
                            if not (out_path / f"formatting_batch{batch_idx + 1}_messages.json").exists():
                                (out_path / f"formatting_batch{batch_idx + 1}_messages.json").write_text(json.dumps(messages, ensure_ascii=False, indent=2), encoding="utf-8")
                            if not (out_path / f"formatting_batch{batch_idx + 1}_meta.json").exists():
                                (out_path / f"formatting_batch{batch_idx + 1}_meta.json").write_text(json.dumps(meta_fail, ensure_ascii=False, indent=2), encoding="utf-8")
                        else:
                            # client.complete raised before generation — still preserve per-attempt messages/meta
                            meta_fail2 = {
                                "batch": batch_idx + 1,
                                "attempt": attempt,
                                "span_count": span_count,
                                "effective_max_tokens": effective_max,
                                "finish_reason": None,
                                "usage": None,
                                "response_format_attempted": None,
                                "error": str(exc)[:500],
                            }
                            (out_path / f"formatting_batch{batch_idx + 1}_attempt{attempt}_messages.json").write_text(json.dumps(messages, ensure_ascii=False, indent=2), encoding="utf-8")
                            (out_path / f"formatting_batch{batch_idx + 1}_attempt{attempt}_meta.json").write_text(json.dumps(meta_fail2, ensure_ascii=False, indent=2), encoding="utf-8")
                    except Exception:
                        pass
                continue
        if not success:
            LOG.warning("formatting batch %d failed after %d attempts — spans become debt", batch_idx + 1, retries)
            batch_mappings = {}
        for key, val in batch_mappings.items():
            result[key] = (str(val["target_text"]), int(val["occurrence"]))
    return result


# ---------------------------------------------------------------------------
# Preserved-markup tier (whole-chapter case: the translation already carries
# the inline tags — card C §11 "whole-chapter перевод держит <em> 101/101")
# ---------------------------------------------------------------------------


def _malformed_inline_markup(text: str) -> List[Tuple[str, int, int]]:
    malformed: List[Tuple[str, int, int]] = []
    stack: List[Tuple[str, int, int]] = []
    for match in _INLINE_TAG_TOKEN_RE.finditer(text):
        token = match.group(0)
        start, end = match.start(), match.end()
        is_closing = token.startswith("</")
        if not is_closing:
            stack.append((match.group(1), start, end))
            continue
        if not stack or stack[-1][0] != match.group(1):
            malformed.append((token, start, end))
            continue
        stack.pop()
    malformed.extend((token, start, end) for _tag, start, end in stack)
    return malformed


def _existing_inline_tags(
    text: str,
) -> List[Tuple[str, int, int]]:
    results: List[Tuple[str, int, int]] = []
    for match in _INLINE_TAG_OPEN_RE.finditer(text):
        tag = match.group(1)
        close = re.search(rf"</{tag}>", text[match.end():])
        if close is None:
            continue
        inner_end = match.end() + close.start()
        results.append((tag, match.end(), inner_end))
    return results


def _resolve_preserved(
    *,
    pid: str,
    translation: str,
    spans: Sequence[SourceSpan],
) -> Tuple[List[SpanMappingRecord], List[SourceSpan], List[SourceSpan]]:
    if not spans:
        return [], [], []
    src_seq = [span.tag for span in spans]
    malformed = _malformed_inline_markup(translation)
    if malformed:
        return [], [], list(spans)
    existing = _existing_inline_tags(translation)
    if not existing:
        return [], list(spans), []
    if len(existing) != len(src_seq) or any(
        tag != expected for (tag, _s, _e), expected in zip(existing, src_seq)
    ):
        return [], [], list(spans)
    resolved: List[SpanMappingRecord] = []
    for span, (tag, start, end) in zip(spans, existing):
        resolved.append(SpanMappingRecord(
            pid=pid,
            span_id=span.span_id,
            tag=tag,
            source_text=span.text,
            translated_text=translation[start:end],
            occurrence=span.occurrence,
            tier=TIER_PRESERVED,
            start=start,
            end=end,
            attrs=dict(span.attrs),
            preserved=True,
        ))
    return resolved, [], []


# ---------------------------------------------------------------------------
# Deterministic tier resolution (exact / occurrence-aware / fuzzy)
# ---------------------------------------------------------------------------


def _group_spans(spans: Sequence[SourceSpan]) -> Dict[str, List[SourceSpan]]:
    groups: Dict[str, List[SourceSpan]] = defaultdict(list)
    for span in spans:
        groups[_fold(span.text)].append(span)
    return dict(groups)


def _resolve_deterministic(
    *,
    pid: str,
    translation: str,
    spans: Sequence[SourceSpan],
    occupied: List[Tuple[int, int]],
) -> Tuple[List[SpanMappingRecord], List[SourceSpan], List[SourceSpan], List[SourceSpan]]:
    preserved, preserved_remaining, preserved_mismatch = _resolve_preserved(
        pid=pid, translation=translation, spans=spans,
    )
    resolved: List[SpanMappingRecord] = list(preserved)
    if preserved_mismatch:
        return resolved, [], [], preserved_mismatch
    if not preserved_remaining:
        return resolved, [], [], []
    spans = preserved_remaining

    fuzzy_candidates: List[SourceSpan] = []
    ambiguous: List[SourceSpan] = []
    for group in _group_spans(spans).values():
        needle = group[0].text
        ranges = occurrence_ranges(translation, needle, word_boundary=True)
        if not ranges:
            fuzzy_candidates.extend(group)
            continue
        if len(ranges) != len(group):
            ambiguous.extend(group)
            continue
        if any(
            any(not (end <= a or start >= b) for a, b in occupied)
            for start, end in ranges
        ):
            ambiguous.extend(group)
            continue
        tier = TIER_EXACT if len(group) == 1 else TIER_OCCURRENCE
        for index, span in enumerate(group):
            start, end = ranges[index]
            occupied.append((start, end))
            resolved.append(SpanMappingRecord(
                pid=pid,
                span_id=span.span_id,
                tag=span.tag,
                source_text=span.text,
                translated_text=translation[start:end],
                occurrence=index + 1,
                tier=tier,
                start=start,
                end=end,
                attrs=dict(span.attrs),
            ))

    if not fuzzy_candidates:
        return resolved, fuzzy_candidates, ambiguous, []

    still_unresolved: List[SourceSpan] = []
    for span in fuzzy_candidates:
        pattern = _fuzzy_pattern(span.text)
        location = None
        for match in re.finditer(pattern, translation):
            start, end = match.span()
            if not any(not (end <= a or start >= b) for a, b in occupied):
                location = (start, end)
                break
        if location is None:
            still_unresolved.append(span)
            continue
        start, end = location
        occupied.append((start, end))
        resolved.append(SpanMappingRecord(
            pid=pid,
            span_id=span.span_id,
            tag=span.tag,
            source_text=span.text,
            translated_text=translation[start:end],
            occurrence=1,
            tier=TIER_FUZZY,
            start=start,
            end=end,
            attrs=dict(span.attrs),
        ))
    return resolved, still_unresolved, ambiguous, []


# ---------------------------------------------------------------------------
# Markup application
# ---------------------------------------------------------------------------


def apply_span_mappings(
    text: str, records: Sequence[SpanMappingRecord]
) -> str:
    parts: List[str] = []
    cursor = 0
    for record in sorted(records, key=lambda r: r.start):
        if record.start < cursor:
            continue
        parts.append(text[cursor:record.start])
        if record.preserved:
            parts.append(text[record.start:record.end])
            cursor = record.end
            continue
        attrs = "".join(
            f' {html.escape(str(key), quote=True)}="'
            f'{html.escape(str(value), quote=True)}"'
            for key, value in sorted(record.attrs.items())
        )
        parts.append(f"<{record.tag}{attrs}>")
        parts.append(text[record.start:record.end])
        parts.append(f"</{record.tag}>")
        cursor = record.end
    parts.append(text[cursor:])
    return "".join(parts)


# ---------------------------------------------------------------------------
# Chapter-level alignment
# ---------------------------------------------------------------------------


def run_formatting_align(
    *,
    blocks: Sequence[SourceBlock],
    translation: Mapping[str, str],
    backend_identity_hash: str,
    policy_version: str = FORMATTING_POLICY_VERSION,
    max_formatting_incidents: int = MAX_FORMATTING_INCIDENTS_DEFAULT,
    mappings: Optional[Mapping[Tuple[str, str], Tuple[str, int]]] = None,
) -> FormattingOutcome:
    """Run the Phase 5 formatting alignment over one chapter.

    ``blocks`` are the parsed source blocks (``pact_v4.phase0b.source_html``)
    carrying the inline spans; ``translation`` is the repaired chapter PID
    map produced by Phase 4 convergence. The output ``formatted_text`` covers
    every PID of ``translation`` (the visible text passed through verbatim
    with the restored inline tags — B14: wrap-only without entities); it is
    the text the Step 8 final integrity check and the terminal transition
    must see.

    Two modes:

    * **With ``mappings``** (per-chapter v41 path): ``mappings`` is the
      ``{(pid, span_id): (target_text, occurrence)}`` dict produced by the
      separate model-call step ``resolve_format_mappings`` (port of V3
      ``formatting_messages`` + ``parse_format_mappings``). Each span's
      Russian ``target_text`` is located deterministically via
      ``find_nonoverlapping_occurrence`` and wrapped by
      ``apply_span_mappings``. Missing / not-found / overlap →
      ``FormattingIncident`` (debt). Deviation from card C (formatting = 0
      model calls) — card C assumed deterministic tiers sufficient for EN→RU;
      POC 0/69 + V3 proved ``target_text`` via model is required. The wrap
      (apply) remains model-free.
    * **Without ``mappings``** (legacy strict-runner path): preserves the
      historic deterministic tiers ``preserved`` → ``exact`` →
      ``occurrence_aware`` → ``fuzzy`` via ``_resolve_deterministic``.
      Used when the translation itself already carries ``<em>`` (whole-chapter
      preserved tier). Behaviour unchanged for backward-compat.

    Tier cascade (legacy path) per PID with a span contract (deterministic
    only — card C: formatting = 0 model calls):

      1. ``preserved`` — the translation already carries the inline tags
      2. ``exact`` — the source text survives verbatim, a single occurrence
      3. ``occurrence_aware`` — ``M`` identical source spans map 1:1 to ``M``
         occurrences
      4. ``fuzzy`` — conservative normalization match
      5. ``model_target`` — (mappings path only) target_text from model

    Every unresolved required span becomes a blocking ``FormattingIncident``;
    ``blocking`` on the outcome is ``incident_count > max_formatting_incidents``.
    """
    span_map: Dict[str, Tuple[SourceSpan, ...]] = {
        block.pid: tuple(block.inline_spans)
        for block in blocks
        if block.inline_spans
    }
    formatted: Dict[str, str] = {
        pid: text for pid, text in translation.items()
    }
    span_mapping: List[SpanMappingRecord] = []
    incidents: List[FormattingIncident] = []

    if mappings is not None:
        # Model-target path (v41): locate each span's Russian target_text.
        for pid, spans in span_map.items():
            text = translation.get(pid, "")
            if not text:
                for span in spans:
                    incidents.append(FormattingIncident(
                        pid=pid, span_id=span.span_id, tier=TIER_MODEL_TARGET,
                        reason="missing_mapping", detail="no translation text for PID",
                    ))
                continue
            occupied: List[Tuple[int, int]] = []
            for span in spans:
                key = (pid, span.span_id)
                if key not in mappings:
                    incidents.append(FormattingIncident(
                        pid=pid, span_id=span.span_id, tier=TIER_MODEL_TARGET,
                        reason="missing_mapping",
                        detail="no target_text mapping for span (model did not return or batch failed)",
                    ))
                    continue
                target_text, occurrence = mappings[key]
                if not target_text:
                    incidents.append(FormattingIncident(
                        pid=pid, span_id=span.span_id, tier=TIER_MODEL_TARGET,
                        reason="target_not_found",
                        detail="empty target_text",
                    ))
                    continue
                ranges = occurrence_ranges(text, target_text)
                if not ranges:
                    incidents.append(FormattingIncident(
                        pid=pid, span_id=span.span_id, tier=TIER_MODEL_TARGET,
                        reason="target_not_found",
                        detail=f"target_text {target_text!r} not found in translation",
                    ))
                    continue
                loc = find_nonoverlapping_occurrence(text, target_text, occurrence, occupied)
                if loc is None:
                    # Distinguish overlap vs not-found: if any occurrence exists, it's overlap
                    incidents.append(FormattingIncident(
                        pid=pid, span_id=span.span_id, tier=TIER_MODEL_TARGET,
                        reason="overlap",
                        detail=f"target_text {target_text!r} occurrence {occurrence} overlaps occupied range",
                    ))
                    continue
                start, end = loc
                occupied.append((start, end))
                span_mapping.append(SpanMappingRecord(
                    pid=pid,
                    span_id=span.span_id,
                    tag=span.tag,
                    source_text=span.text,
                    translated_text=target_text,
                    occurrence=occurrence,
                    tier=TIER_MODEL_TARGET,
                    start=start,
                    end=end,
                    attrs=dict(span.attrs),
                    preserved=False,
                ))
    else:
        # Legacy deterministic path (strict-runner backward-compat)
        for pid, spans in span_map.items():
            text = translation.get(pid, "")
            if not text:
                continue
            occupied: List[Tuple[int, int]] = []
            resolved, fuzzy_candidates, ambiguous, preserved_mismatch = _resolve_deterministic(
                pid=pid, translation=text, spans=spans, occupied=occupied,
            )
            span_mapping.extend(resolved)
            unresolved = fuzzy_candidates + ambiguous + preserved_mismatch
            fuzzy_ids = {span.span_id for span in fuzzy_candidates}
            mismatch_ids = {span.span_id for span in preserved_mismatch}

            def _last_tier(span: SourceSpan) -> str:
                if span.span_id in mismatch_ids:
                    return TIER_PRESERVED
                if span.span_id in fuzzy_ids:
                    return TIER_FUZZY
                return TIER_OCCURRENCE

            def _reason(span: SourceSpan) -> str:
                if span.span_id in mismatch_ids:
                    return "preserved_tag_mismatch"
                if span.span_id in fuzzy_ids:
                    return "target_not_found"
                return "ambiguous_occurrence"

            def _detail(span: SourceSpan) -> str:
                if span.span_id in mismatch_ids:
                    return (
                        "translation already carries inline markup that is "
                        "malformed (unbalanced/orphaned tag) or whose tag "
                        "sequence (count/order) does not match the source "
                        "spans; never claimed, never re-wrapped (formatting is "
                        "model-free by rule — unresolved spans are debt)"
                    )
                return (
                    "no deterministic fragment found (formatting is "
                    "model-free by rule — unresolved spans are debt)"
                )

            if unresolved:
                incidents.extend(
                    FormattingIncident(
                        pid=pid, span_id=span.span_id, tier=_last_tier(span),
                        reason=_reason(span),
                        detail=_detail(span),
                    )
                    for span in unresolved
                )

    for pid, spans in span_map.items():
        text = translation.get(pid, "")
        records_for_pid = [r for r in span_mapping if r.pid == pid]
        formatted[pid] = apply_span_mappings(text, records_for_pid)

    for pid, text in formatted.items():
        if _MARKER_RE.search(text):
            raise AssertionError(
                f"Formatting marker leaked into PID {pid}: {text!r}"
            )

    return FormattingOutcome(
        formatted_text=tuple((pid, formatted.get(pid, "")) for pid in translation),
        span_mapping=tuple(span_mapping),
        incidents=tuple(incidents),
        backend_identity_hash=backend_identity_hash,
        policy_version=policy_version,
        max_formatting_incidents=max_formatting_incidents,
        model_fallback_count=0,
        model_call_count=0,
    )
