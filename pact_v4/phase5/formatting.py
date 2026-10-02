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
import time
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple

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

FORMATTING_POLICY_VERSION = "pact-v4-formatting/v2"
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
# Policy-owned: temperature/top_p/top_k and max_output_tokens come from
# RoleCallPolicy (formatting) via derive_max_output_tokens; defaults here
# are only for backward-compat when no policy is wired.
DEFAULT_FORMATTING_CFG: Dict[str, Any] = {
    "enabled": True,
    "required": False,
    "temperature": float("0.1"),  # test-only fallback; production must supply role_policy,
    "top_p": 0.9,
    "top_k": 32,
    "enable_thinking": False,
    "max_tokens": None,
    "generation_retries": 2,
    "tags": ["em", "strong", "i", "b", "a"],
    "required_tags": ["em", "strong", "i", "b", "a"],
    "optional_tags": [],
    "max_blocks_per_call": None,  # explicit safety cap only; budget planner drives grouping (D2)
    "retry_unresolved_spans": True,
    "on_failure": "omit_tag",
    "formatting_single_call_whole_chapter": True,  # legacy hint; planner prefers single fit (D2)
    "plan_margin_tokens": 512,
    "max_calls": 32,
    "max_split_depth": 6,
}

# v41: dynamic max_tokens scaling constants (legacy unit-path fallback only;
# production budgeting is policy-owned via RoleCallPolicy/output_budget).
_FORMATTING_TOKENS_PER_SPAN = 40
_FORMATTING_TOKENS_OVERHEAD = 500
_FORMATTING_MIN_TOKENS = 800
_FORMATTING_MAX_TOKENS_CAP = 8192
# simplify-book-formatting D2/D5: budget planner + bounded recovery constants.
# No fixed 80-span / 12-PID / 12000-char thresholds drive the production path.
_FORMATTING_PLAN_MARGIN_TOKENS = 512
_FORMATTING_MAX_CALLS_DEFAULT = 32
_FORMATTING_MAX_SPLIT_DEPTH_DEFAULT = 6
# D5 transient transport recovery: at most one immediate repeat per group,
# preceded by a small bounded delay (pact-rev: no hot retry). Tests inject
# or patch the sleep; production sleeps this many seconds (upper bound 5s).
_TRANSPORT_RETRY_DELAY_SECONDS = 0.5
_TRANSPORT_RETRY_DELAY_MAX_SECONDS = 5.0
_LENGTH_FINISH_REASONS = frozenset({"length", "max_tokens", "context", "context_length", "too_long", "content_filter_length"})


def _effective_max_tokens(span_count: int, cfg_max: Any, role_policy: Any = None) -> int:
    """Policy-owned derivation: when role_policy is provided, derive via OutputBudgetPolicy; otherwise fallback."""
    if role_policy is not None:
        from pact_v4.runtime.runtime_config import derive_max_output_tokens as _derive
        return int(_derive(role_policy, span_tokens=span_count))
    # Fallback for legacy/tests: dynamic budget without policy, emit warning
    import warnings as _warnings
    try:
        from pact_v4.runtime.warnings import PactWarning as _PactWarning
    except Exception:
        _PactWarning = UserWarning  # type: ignore
    _warnings.warn("formatting role_policy missing — falling back to DEFAULT_FORMATTING_CFG (policy_missing)", _PactWarning, stacklevel=3)
    # dynamic fallback: 40*spans+500, min 800 cap 8192, respect explicit cfg_max if int
    val = int(_FORMATTING_TOKENS_PER_SPAN * int(span_count) + _FORMATTING_TOKENS_OVERHEAD)
    val = max(_FORMATTING_MIN_TOKENS, val)
    val = min(_FORMATTING_MAX_TOKENS_CAP, val)
    if isinstance(cfg_max, int) and cfg_max > 0:
        val = min(val, int(cfg_max))
    return int(val)

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


def _resolve_policy_for_formatting(client: Any, cfg: Mapping[str, Any], role_policy: Any) -> Any:
    """Explicit formatting RoleCallPolicy first, never a silent code literal."""
    if role_policy is not None:
        return role_policy
    if isinstance(cfg, Mapping):
        direct = cfg.get("role_policy")
        if direct is not None:
            return direct
    carried = getattr(client, "_role_policy", None)
    if carried is not None:
        return carried
    return None


def _context_window_for_formatting(cfg: Mapping[str, Any], context_window: Any) -> Optional[int]:
    """Resolved context envelope in tokens, or None when unknown (remote rule)."""
    candidates: List[Any] = [context_window]
    if isinstance(cfg, Mapping):
        for key in ("context_window", "context_envelope_tokens"):
            try:
                candidates.append(cfg.get(key))
            except Exception:
                continue
    for raw in candidates:
        if raw is None:
            continue
        try:
            val = int(raw)
        except Exception:
            continue
        if val > 0:
            return val
    return None


def _plan_formatting_groups(
    pids: Sequence[str],
    block_map: Mapping[str, SourceBlock],
    translations: Mapping[str, str],
    *,
    output_for: Any,
    context_window: Optional[int],
    margin: int,
    safety_cap: Optional[int],
) -> Tuple[List[List[str]], Dict[str, Any]]:
    """Budget planner (simplify-book-formatting D2).

    Prefer one request for the whole tagged set whenever prompt +
    role-policy output allowance + margin fits the resolved envelope.
    Otherwise pack contiguous PIDs into the fewest fitting groups.
    Unknown envelope (remote) tries one request; splitting then happens
    only on an explicit length/context failure (D5). No fixed span/PID
    thresholds drive this decision.
    """
    ordered = list(pids)
    total_spans = sum(len(block_map[pid].inline_spans) for pid in ordered)
    def _group_span_counts(group_list: Sequence[Sequence[str]]) -> List[int]:
        return [sum(len(block_map[pid].inline_spans) for pid in grp) for grp in group_list]
    try:
        whole_prompt = _estimate_prompt_tokens(formatting_messages(ordered, block_map, translations))
    except Exception:
        whole_prompt = 0
    try:
        whole_output = int(output_for(total_spans))
    except Exception:
        whole_output = 0
    if context_window is None:
        plan = {"strategy": "single_no_known_window", "reason": "remote_rule_try_single_within_policy", "tagged_pids": len(ordered), "tagged_spans": total_spans, "prompt_tokens_est": whole_prompt, "output_allowance": whole_output, "context_window": None, "margin": int(margin)}
        groups = [ordered]
        plan["groups"] = len(groups)
        plan["group_sizes"] = [len(g) for g in groups]
        plan["group_span_counts"] = _group_span_counts(groups)
    elif whole_prompt + whole_output + int(margin) <= int(context_window):
        plan = {"strategy": "single_fit", "reason": "whole_tagged_set_fits_envelope", "tagged_pids": len(ordered), "tagged_spans": total_spans, "prompt_tokens_est": whole_prompt, "output_allowance": whole_output, "context_window": int(context_window), "margin": int(margin)}
        groups = [ordered]
        plan["groups"] = len(groups)
        plan["group_sizes"] = [len(g) for g in groups]
        plan["group_span_counts"] = _group_span_counts(groups)
    else:
        groups = []
        current: List[str] = []
        current_spans = 0
        for pid in ordered:
            cand = current + [pid]
            cand_spans = current_spans + len(block_map[pid].inline_spans)
            try:
                cand_prompt = _estimate_prompt_tokens(formatting_messages(cand, block_map, translations))
            except Exception:
                cand_prompt = 0
            try:
                cand_output = int(output_for(cand_spans))
            except Exception:
                cand_output = 0
            if current and cand_prompt + cand_output + int(margin) > int(context_window):
                groups.append(current)
                current = [pid]
                current_spans = len(block_map[pid].inline_spans)
            else:
                current = cand
                current_spans = cand_spans
        if current:
            groups.append(current)
        plan = {"strategy": "budget_split", "reason": "whole_tagged_set_exceeds_envelope", "tagged_pids": len(ordered), "tagged_spans": total_spans, "prompt_tokens_est": whole_prompt, "output_allowance": whole_output, "context_window": int(context_window), "margin": int(margin), "groups": len(groups), "group_sizes": [len(g) for g in groups], "group_span_counts": _group_span_counts(groups)}
    if safety_cap is not None and safety_cap > 0 and any(len(g) > safety_cap for g in groups):
        capped: List[List[str]] = []
        for grp in groups:
            capped.extend([grp[i:i + safety_cap] for i in range(0, len(grp), safety_cap)])
        groups = capped
        plan["safety_cap_pids"] = int(safety_cap)
        plan["groups"] = len(groups)
        plan["group_sizes"] = [len(g) for g in groups]
        plan["group_span_counts"] = _group_span_counts(groups)
    return groups, plan


def _is_length_or_context_failure(exc: BaseException, generation: Any) -> bool:
    reason = str(getattr(generation, "finish_reason", "") or "").strip().lower() if generation is not None else ""
    if reason in _LENGTH_FINISH_REASONS:
        return True
    text = f"{exc} {reason}".lower()
    for marker in ("context", "too long", "too large", "max_tokens", "truncat", "length"):
        if marker in text:
            return True
    return False


def _write_policy_missing_diagnostics(out_path: Optional[Path], *, span_count: int, detail: str) -> None:
    if out_path is None:
        return
    try:
        out_path.mkdir(parents=True, exist_ok=True)
    except Exception:
        pass
    meta = {
        "batch": 1,
        "attempt": 0,
        "span_count": int(span_count),
        "effective_max_tokens": None,
        "finish_reason": None,
        "usage": None,
        "response_format_attempted": None,
        "cause": "formatting_policy_missing",
        "detail": str(detail),
        "model_calls": 0,
    }
    try:
        meta_path = out_path / "formatting_batch1_meta.json"
        if not meta_path.exists():
            meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
        attempt_meta = out_path / "formatting_batch1_attempt1_meta.json"
        if not attempt_meta.exists():
            attempt_meta.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
        plan_path = out_path / "formatting_plan.json"
        if not plan_path.exists():
            plan_path.write_text(json.dumps({"strategy": "policy_missing_no_call", "cause": "formatting_policy_missing", "detail": str(detail)}, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        pass


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
    role_policy: Optional[Any] = None,
    context_window: Optional[int] = None,
    require_policy: bool = False,
    max_calls: Optional[int] = None,
    max_split_depth: Optional[int] = None,
    plan_margin_tokens: Optional[int] = None,
    transport_sleep: Optional[Callable[[float], None]] = None,
) -> Dict[Tuple[str, str], Tuple[str, int]]:
    """Resolve ``target_text`` via model-call (simplify-book-formatting D1-D5).

    Only PIDs with ``inline_spans`` are sent to the model, each PID once
    with its English source context, span IDs/text and paired final Russian
    translation. The planner prefers one request for the whole tagged set
    whenever prompt + role-policy output allowance + margin fits the
    resolved envelope; otherwise it packs contiguous PIDs into the fewest
    fitting groups (no fixed 80-span / 12-PID thresholds). Sampling and
    ``max_output_tokens`` come only from the resolved formatting
    RoleCallPolicy. Recovery splits only the failed group (never an
    identical repeat with the same budget) and re-requests only unresolved
    spans after a valid partial response. Unresolved spans become debt.
    PIDs without inline_spans never trigger a model call (early return).

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
    policy = _resolve_policy_for_formatting(client, cfg, role_policy)
    out_path = Path(out_dir) if out_dir is not None else None
    if out_path is not None:
        try:
            out_path.mkdir(parents=True, exist_ok=True)
        except Exception:
            pass
    total_spans = sum(len(block_map[pid].inline_spans) for pid in pids)
    if policy is None:
        if require_policy:
            LOG.warning("formatting role_policy missing — diagnosed debt without model call (no legacy fallback budget)")
            _write_policy_missing_diagnostics(out_path, span_count=total_spans, detail="production formatting call has no resolved formatting RoleCallPolicy; no request issued")
            return {}
        # Legacy unit-path fallback (dynamic budget + warning); production
        # callers must pass require_policy=True with an explicit policy.
    raw_cfg_max = fmt_cfg.get("max_tokens")
    if raw_cfg_max is None:
        cfg_max: Any = None
    else:
        try:
            cfg_max = int(raw_cfg_max)
        except Exception:
            cfg_max = None

    def _output_for(span_count: int) -> int:
        return int(_effective_max_tokens(int(span_count), cfg_max, role_policy=policy))

    margin = int(plan_margin_tokens if plan_margin_tokens is not None else fmt_cfg.get("plan_margin_tokens", _FORMATTING_PLAN_MARGIN_TOKENS))
    window = _context_window_for_formatting(fmt_cfg, context_window)
    if window is None and isinstance(cfg, Mapping):
        window = _context_window_for_formatting(cfg, None)
    # ``max_blocks_per_call`` is only an explicit safety cap now, never the
    # planner driver; ``single_call=False`` forces budget packing without a
    # known envelope by treating the whole set as exceeding it.
    safety_cap = max_blocks_per_call
    if safety_cap is None:
        raw_cap = fmt_cfg.get("max_blocks_per_call")
        safety_cap = None if raw_cap is None else int(raw_cap)
    force_split = single_call is False
    groups, plan = _plan_formatting_groups(
        pids, block_map, translations,
        output_for=_output_for, context_window=window, margin=margin, safety_cap=safety_cap,
    )
    if force_split and len(groups) == 1 and len(pids) > 1:
        mid = len(pids) // 2
        groups = [pids[:mid], pids[mid:]]
        plan["strategy"] = "forced_split_single_call_false"
        plan["groups"] = len(groups)
        plan["group_sizes"] = [len(g) for g in groups]
        plan["group_span_counts"] = [sum(len(block_map[pid].inline_spans) for pid in grp) for grp in groups]
    try:
        whole_allowance = int(_output_for(total_spans))
    except Exception:
        whole_allowance = 0
    plan["actual_max_output_tokens"] = whole_allowance
    plan["policy_present"] = policy is not None
    if out_path is not None:
        try:
            (out_path / "formatting_plan.json").write_text(json.dumps(plan, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception:
            pass

    result: Dict[Tuple[str, str], Tuple[str, int]] = {}
    # --- D5 work-reducing recovery over a queue of contiguous groups ---
    retries = int(generation_retries if generation_retries is not None else fmt_cfg.get("generation_retries", 2))
    transient_budget = max(0, min(int(retries), 1))
    # Bounded pre-retry delay (pact-rev D5): the single permitted transport
    # repeat never fires hot. Injectable for deterministic/fast tests.
    _sleep_fn = transport_sleep if transport_sleep is not None else time.sleep
    _retry_delay = min(max(0.0, float(_TRANSPORT_RETRY_DELAY_SECONDS)), float(_TRANSPORT_RETRY_DELAY_MAX_SECONDS))
    call_cap = int(max_calls if max_calls is not None else fmt_cfg.get("max_calls", _FORMATTING_MAX_CALLS_DEFAULT))
    split_cap = int(max_split_depth if max_split_depth is not None else fmt_cfg.get("max_split_depth", _FORMATTING_MAX_SPLIT_DEPTH_DEFAULT))
    pending_work: List[Tuple[List[str], Optional[Mapping[str, Any]], int]] = [(list(grp), None, 0) for grp in groups]
    seen_requests: set = set()
    call_count = 0
    occupied_by_pid: Dict[str, List[Tuple[int, int]]] = {}

    def _occupied(pid: str) -> List[Tuple[int, int]]:
        occ = occupied_by_pid.get(pid)
        if occ is None:
            occ = []
            # Seed from already-retained mappings in this run.
            for (rpid, _sid), (rtarget, rocc) in result.items():
                if rpid != pid:
                    continue
                text = translations.get(pid, "")
                loc = find_nonoverlapping_occurrence(text, rtarget, int(rocc), occ)
                if loc is not None:
                    occ.append(loc)
            occupied_by_pid[pid] = occ
        return occ

    def _split_work(batch: List[str], pending_keys: set, depth: int) -> List[Tuple[List[str], Any, int]]:
        if depth >= split_cap:
            return []
        if len(batch) > 1:
            mid = len(batch) // 2
            return [(batch[:mid], None, depth + 1), (batch[mid:], None, depth + 1)]
        pid = batch[0]
        sids = sorted({sid for (p, sid) in pending_keys if p == pid})
        if len(sids) > 1:
            mid = len(sids) // 2
            return [
                ([pid], {pid: set(sids[:mid])}, depth + 1),
                ([pid], {pid: set(sids[mid:])}, depth + 1),
            ]
        return []

    def _write_call_diagnostics(group_seq: int, t_attempt: int, messages: Any, generation: Any, effective_max: int, span_count: int, extra: Optional[Mapping[str, Any]] = None, canonical: bool = True, pid_count: Optional[int] = None) -> None:
        if out_path is None:
            return
        try:
            if generation is not None:
                raw_content = getattr(generation, "content", None)
                if raw_content is None and isinstance(generation, dict):
                    raw_content = generation.get("content", "")
                raw_text = str(raw_content or getattr(generation, "text", "") or "")
                reasoning_text = str(getattr(generation, "reasoning", "") or getattr(generation, "reasoning_content", "") or "")
            else:
                raw_text = ""
                reasoning_text = ""
            meta: Dict[str, Any] = {
                "batch": group_seq,
                "attempt": t_attempt,
                "pid_count": int(pid_count) if pid_count is not None else None,
                "span_count": int(span_count),
                "effective_max_tokens": int(effective_max),
                "finish_reason": getattr(generation, "finish_reason", None) if generation is not None else None,
                "usage": getattr(generation, "usage", None) if generation is not None else None,
                "response_format_attempted": getattr(generation, "response_format_attempted", None) if generation is not None else None,
            }
            if extra:
                meta.update(dict(extra))
            if generation is not None:
                (out_path / f"formatting_batch{group_seq}_attempt{t_attempt}_raw.txt").write_text(raw_text, encoding="utf-8")
                (out_path / f"formatting_batch{group_seq}_attempt{t_attempt}_reasoning.txt").write_text(reasoning_text, encoding="utf-8")
            (out_path / f"formatting_batch{group_seq}_attempt{t_attempt}_messages.json").write_text(json.dumps(messages, ensure_ascii=False, indent=2), encoding="utf-8")
            (out_path / f"formatting_batch{group_seq}_attempt{t_attempt}_meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
            if canonical:
                if generation is not None:
                    (out_path / f"formatting_batch{group_seq}_raw.txt").write_text(raw_text, encoding="utf-8")
                    (out_path / f"formatting_batch{group_seq}_reasoning.txt").write_text(reasoning_text, encoding="utf-8")
                (out_path / f"formatting_batch{group_seq}_messages.json").write_text(json.dumps(messages, ensure_ascii=False, indent=2), encoding="utf-8")
                (out_path / f"formatting_batch{group_seq}_meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception:
            pass

    group_seq = 0
    while pending_work and call_count < call_cap:
        batch, span_filter, depth = pending_work.pop(0)
        group_seq += 1
        batch_idx = group_seq - 1
        allowed: Dict[Tuple[str, str], SourceSpan] = {}
        for pid in batch:
            for span in block_map[pid].inline_spans:
                if span_filter is not None:
                    wanted = span_filter.get(pid)
                    if wanted is not None and span.span_id not in wanted:
                        continue
                allowed[(pid, span.span_id)] = span
        pending_keys = {k for k in allowed if k not in result}
        if not pending_keys:
            continue
        span_count = len(pending_keys)
        effective_max = _output_for(span_count)
        filt: Dict[str, set] = {}
        for (ppid, ssid) in pending_keys:
            filt.setdefault(ppid, set()).add(ssid)
        messages = formatting_messages(batch, block_map, translations, span_filter=filt)
        sig = (frozenset(pending_keys), int(effective_max))
        if sig in seen_requests:
            # Never repeat an identical request with the same limiting budget.
            LOG.warning("formatting group %d identical repeat refused (same keys+budget) — splitting or debt", group_seq)
            for item in _split_work(batch, pending_keys, depth):
                pending_work.append(item)
            if not _split_work(batch, pending_keys, depth):
                _write_call_diagnostics(group_seq, 1, messages, None, effective_max, span_count, extra={"cause": "identical_repeat_refused_debt", "depth": depth, "planner": plan.get("strategy")}, canonical=True, pid_count=len(batch))
            continue
        seen_requests.add(sig)
        generation = None
        transport_error: Optional[BaseException] = None
        for t_attempt in range(1, transient_budget + 2):
            if call_count >= call_cap:
                break
            try:
                try:
                    generation = client.complete(messages, fmt_cfg, effective_max, f"formatting:batch{group_seq}:attempt{t_attempt}")
                except TypeError:
                    generation = client.complete(messages, fmt_cfg, effective_max)
                call_count += 1
                transport_error = None
                _write_call_diagnostics(group_seq, t_attempt, messages, generation, effective_max, span_count, extra={"depth": depth, "planner": plan.get("strategy")}, canonical=True, pid_count=len(batch))
                break
            except Exception as exc:
                call_count += 1
                transport_error = exc
                generation = None
                _write_call_diagnostics(group_seq, t_attempt, messages, None, effective_max, span_count, extra={"depth": depth, "error": str(exc)[:500], "planner": plan.get("strategy")}, canonical=True, pid_count=len(batch))
                LOG.warning("formatting group %d transport attempt %d failed: %s", group_seq, t_attempt, exc)
                if t_attempt <= transient_budget:
                    try:
                        _sleep_fn(_retry_delay)
                    except Exception:
                        pass
                continue
        if generation is None:
            # Transient budget exhausted — split only this group, never the whole plan.
            LOG.warning("formatting group %d transport failed after attempts — splitting or debt", group_seq)
            for item in _split_work(batch, pending_keys, depth):
                pending_work.append(item)
            continue
        try:
            parsed = parse_format_mappings(generation, allowed)
        except Exception as exc:
            length_like = _is_length_or_context_failure(exc, generation)
            LOG.warning("formatting group %d invalid response (%s, length_like=%s) — splitting or debt", group_seq, exc, length_like)
            for item in _split_work(batch, pending_keys, depth):
                pending_work.append(item)
            if not _split_work(batch, pending_keys, depth):
                LOG.warning("formatting group %d unsplittable — spans become debt", group_seq)
            continue
        # Full per-mapping validation before retention (D5): pid/span allowed,
        # non-empty substring of its own PID translation, occurrence in range
        # and non-overlapping, tag from the source span.
        newly_retained = 0
        for key, val in parsed.items():
            if key not in pending_keys or key in result:
                continue
            target_text = str(val.get("target_text", ""))
            try:
                occurrence = max(1, int(val.get("occurrence") or 1))
            except Exception:
                occurrence = 1
            if not target_text.strip():
                continue
            pid = key[0]
            text = translations.get(pid, "")
            if not text or target_text not in text:
                continue
            ranges = occurrence_ranges(text, target_text)
            if not ranges or occurrence > len(ranges):
                # pact-rev: an out-of-range requested occurrence identifies
                # no position, so the mapping is rejected (debt/recovery)
                # instead of silently binding a different occurrence while
                # retaining the invalid requested index.
                continue
            loc = find_nonoverlapping_occurrence(text, target_text, occurrence, _occupied(pid))
            if loc is None:
                continue
            _occupied(pid).append(loc)
            result[key] = (target_text, occurrence)
            newly_retained += 1
        unresolved = {k for k in pending_keys if k not in result}
        length_like = str(getattr(generation, "finish_reason", "") or "").strip().lower() in _LENGTH_FINISH_REASONS
        if not unresolved:
            continue
        if newly_retained > 0:
            # Valid partial: follow up with only unresolved spans.
            follow: Dict[str, set] = {}
            for (ppid, ssid) in unresolved:
                follow.setdefault(ppid, set()).add(ssid)
            follow_pids = [pid for pid in batch if pid in follow]
            if depth + 1 > split_cap or call_count >= call_cap:
                LOG.warning("formatting group %d partial (%d retained) but recovery capped — remainder debt", group_seq, newly_retained)
                continue
            pending_work.append((follow_pids, follow, depth + 1))
            LOG.warning("formatting group %d partial: retained %d, re-requesting %d unresolved", group_seq, newly_retained, len(unresolved))
        else:
            # Empty/invalid-content response (incl. length truncation): split.
            LOG.warning("formatting group %d no valid mappings (length_like=%s) — splitting or debt", group_seq, length_like)
            for item in _split_work(batch, pending_keys, depth):
                pending_work.append(item)
            if not _split_work(batch, pending_keys, depth):
                LOG.warning("formatting group %d unsplittable — spans become debt", group_seq)
    if pending_work:
        LOG.warning("formatting recovery capped (%d groups unprocessed) — remainder debt", len(pending_work))
    # for backward-compat enumeration the old batch loop is replaced;
    # the queue above owns all model calls.
    # Legacy fixed batch loop removed (D2/D5 queue above owns all model calls).
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
                if occurrence > len(ranges):
                    # pact-rev: the requested occurrence identifies no
                    # position; reject instead of wrapping a different
                    # occurrence while retaining the invalid index.
                    incidents.append(FormattingIncident(
                        pid=pid, span_id=span.span_id, tier=TIER_MODEL_TARGET,
                        reason="target_not_found",
                        detail=f"target_text {target_text!r} occurrence {occurrence} exceeds {len(ranges)} available occurrence(s)",
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
