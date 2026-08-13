"""B1: chunked Qwen fidelity audit (production port of ``audit_v4.ps1``).

The v4.1 chunked audit (concept: ``docs/plans/V4_1_AUDIT_B1_RU.md`` §1-§8,
harness: ``D:\\test folder\\audit_v4.ps1``) runs the Qwen auditor over a
translated chapter in **input-token-bounded chunks** instead of one
whole-chapter call. This module is the production Python port of the harness
logic:

* greedy chunking by estimated input tokens (``max_input_tokens=3600``,
  honest name: greedy — the harness's K-balance math reduces to the same
  flush rule because ``target <= max_input`` always);
* ``CONTEXT_ONLY`` left-overlap between chunks (preceding pairs from the
  ORIGINAL chapter, ~400 tokens, min 2 / max 6 pairs) that the model must
  NEVER audit;
* RetryShrink by input: a failed chunk is re-balanced into sub-chunks with
  ``max_input/2`` then ``max_input/3`` limits; sub-overlap always comes from
  the original chapter, never from the cut child; a fully-shrunk chunk is
  ``GOOD_RETRIED`` (the harness bug where ``$pending`` was not cleared on
  success — false ``FAILED_RETRIED`` — is fixed here);
* fail-closed strict JSON validation: categories
  ``omission/addition/referent/invented_gender/changed_fact/negation``,
  severity ``major/minor``, confidence ``high/medium/low``, ``id`` must be a
  PID of the CURRENT chunk; a failed chunk is never silently read as
  ``issues=[]`` — ``audit_complete`` is false whenever any chunk failed;
* fail-closed transport: a ``CompletionBackend.complete`` exception (e.g.
  ``CompletionError``) becomes a failed ``TRANSPORT_ERROR`` chunk with the
  diagnostic + artifact mapping instead of escaping the evaluator;
  RetryShrink is an input-size strategy and never re-dispatches transport
  failures;
* fail-closed coverage: a source PID missing from the translation map raises
  ``CoverageError`` before any model call (never a silent partial audit);
  an empty pair set is rejected the same way (never ``audit_complete=True``
  with 0 chunks);
* deterministic dedup by ``id+category`` (higher confidence wins);
* per-issue debug metadata ``_debug: {chunk, reasoning_file}`` attached at
  collection time (sub-chunks carry their own ``reasoning_file``).

Transport: the evaluator is backend-neutral over ``CompletionBackend`` (the
same boundary ``BackendQwenAuditEvaluator`` uses). The lifecycle wrapper
(``pact_v4.runtime.model_lifecycle_adapters.LifecycleQwenAuditEvaluator``)
supplies the local ``llama-server`` backend. Reasoning capture depends on the
transport: ``LocalOpenAIBackend`` returns ``finish_reason`` and text only, so
``reasoning_chars`` is best-effort from ``raw_metadata``; the reasoning-file
names are deterministic artifact names recorded for the caller to persist.

The full-input-budget rule (concept §2) is enforced here too:
``fixed_prompt + narrator + entity + CONTEXT_ONLY + AUDIT_PAIRS <=
calibrated_total``; the entity block has a soft (500) / hard (800) token cap —
a hard overflow fails closed (never silently truncated).
"""
from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from pact_v4.runtime.backend_protocol import (
    JSON_OBJECT_SCHEMA,
    CompletionBackend,
    CompletionRequest,
    Message,
)
from pact_v4.runtime.json_resilience import JsonRetryPolicy
from pact_v4.runtime.prompts_runtime import (
    QWEN_AUDIT_V4_1,
    ReviewerPrompt,
    render_chunked_audit_prompt,
)

LOG = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Frozen contract constants (concept §2, §4, §8; harness audit_v4.ps1)
# ---------------------------------------------------------------------------

AUDIT_V4_CATEGORIES = frozenset({
    "omission", "addition", "referent", "invented_gender", "changed_fact", "negation",
})
AUDIT_V4_SEVERITIES = frozenset({"major", "minor"})
AUDIT_V4_CONFIDENCES = frozenset({"high", "medium", "low"})

SCHEMA = "pact-audit/v4"
HARNESS_VERSION = "4.1"
PROMPT_VERSION = "pact-v4-reviewer-qwen-audit/v4.1"

DEFAULT_MAX_INPUT_TOKENS = 3600   # reasoning_budget/2 x 0.88 (12% reserve)
DEFAULT_MAX_TOKENS = 12000        # reasoning (8192) + ~3500 content headroom
DEFAULT_OVERLAP_TOKENS = 400
MIN_OVERLAP_PAIRS = 2
MAX_OVERLAP_PAIRS = 6
DEFAULT_REASONING_BUDGET = 8192
# R-RETRY (t_8ab8ab35, operator extension 2026-08-13): a chunk-level
# TRANSPORT_ERROR is retried with a NEW session (the old one is dead),
# bounded like the HTTP path — a transport failure is not an input-size
# problem, so RetryShrink never applies. Defaults mirror JsonRetryPolicy.
DEFAULT_TRANSPORT_MAX_RETRIES: int = JsonRetryPolicy().max_retries
DEFAULT_TRANSPORT_BASE_DELAY_SECONDS: float = JsonRetryPolicy().base_delay_seconds

ENTITY_SOFT_TOKENS = 500
ENTITY_HARD_TOKENS = 800

_CONFIDENCE_RANK = {"high": 3, "medium": 2, "low": 1}

_LOOKS_LIKE_REASONING = re.compile(r"pass|p\d{5}|faithful|acceptable|source", re.IGNORECASE)


# ---------------------------------------------------------------------------
# Data shapes
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AuditPair:
    """One source/translation pair (a PID's English source + Russian text)."""

    pid: str
    source: str
    translation: str


class CoverageError(ValueError):
    """Source PIDs missing from the translation map (rejected before any
    model call — a partial map must never silently claim a full-chapter
    audit, concept §5.3: failure → debt, never silent success)."""


def pairs_from_maps(
    source: Mapping[str, str], translation: Mapping[str, str]
) -> Tuple[AuditPair, ...]:
    """Build ordered ``AuditPair``s from parallel PID maps (source order).

    Fail-closed coverage: a source PID missing from ``translation`` raises
    ``CoverageError`` listing the missing PIDs instead of being silently
    skipped (the harness's ``ContainsKey`` guard could make a 400-PID
    chapter claim ``audit_complete=True`` while whole PIDs were never
    audited). PIDs are emitted in SOURCE insertion order (not ``sorted`` —
    ``sorted`` breaks the declared "source order" for non-zero-padded /
    non-lexical PID maps).
    """
    missing = [pid for pid in source if translation.get(pid) is None]
    if missing:
        raise CoverageError(
            f"translation map missing {len(missing)} source PID(s): "
            f"{missing}"
        )
    return tuple(
        AuditPair(pid=pid, source=source[pid], translation=translation[pid])
        for pid in source
    )


# Roles that may serve the audit call, in priority order (same fallback
# contract as the runtime role adapters: any role binding, else ``default``).
_AUDIT_ROLES = ("qwen_audit", "fidelity_reviewer", "qwen_fidelity")


def audit_model_ref(backend: CompletionBackend) -> str:
    """Resolve the model reference for the audit role from the backend
    descriptor (role binding, else ``default``). Raises when unbound so a
    role without an assigned model fails loudly instead of silently using
    whatever model the transport serves.
    """
    bindings = backend.descriptor.model_bindings
    for role in _AUDIT_ROLES:
        ref = bindings.get(role)
        if ref:
            return str(ref)
    ref = bindings.get("default")
    if ref:
        return str(ref)
    raise ValueError(
        f"no model binding for audit role(s) {list(_AUDIT_ROLES)!r}; "
        f"backend model_bindings={dict(bindings)!r}"
    )


def pair_token_estimate(source: str, translation: str) -> float:
    """Deterministic token estimate for one pair (harness ``Pair-TokenEstimate``)."""
    return len(source) / 4.0 + len(translation) / 3.0


def text_token_estimate(text: str) -> float:
    """Rough token estimate for a prompt block (English-leaning, len/4)."""
    return len(text) / 4.0


# ---------------------------------------------------------------------------
# 3-level audit context (concept §3.1/§8.3)
# ---------------------------------------------------------------------------

# Generic role descriptions that must NEVER be promoted to canonical
# narrator/entity context (lesson "The Nurse: female" → 3 poisoned FPs,
# concept §3.1): a label like "the nurse" is not a canonical entity even if
# book_memory happens to contain a character with a matching role.
GENERIC_DESCRIPTIONS = frozenset({
    "the nurse", "the man", "the woman", "the dog", "the driver",
    "the boy", "the girl", "the child", "the kid", "the baby",
    "the mother", "the father", "the brother", "the sister",
    "the aunt", "the uncle", "the grandmother", "the grandfather",
    "the doctor", "the lawyer", "the attendant", "the visitor",
    "the neighbor", "the relative", "the stranger", "the lady",
    "the gentleman", "the patient", "the client", "the guest",
    "the waiter", "the cook", "the maid", "the servant", "the owner",
    "the manager", "the boss", "the teacher", "the student",
    "the officer", "the guard", "the cop", "the priest", "the nun",
    "the monk", "the boy in scrubs", "the man in scrubs",
})


def _canonical_name(name: str) -> bool:
    """True for a canonical named entity (proper noun), not a generic role."""
    lowered = name.strip().lower()
    if lowered in GENERIC_DESCRIPTIONS:
        return False
    # A canonical name contains at least one capital letter (Blake, Callan,
    # Molly Walker) — generic descriptions are all-lowercase in source.
    return any(ch.isupper() for ch in name)


def _name_present_in_source(name: str, source_text: str) -> bool:
    """True when the canonical name appears as a whole token/phrase in source.

    Word-boundary matching (``\\b`` around the escaped name, whitespace runs
    collapsed to ``\\s+`` between the words of a multi-word name) — a
    substring like ``Ann`` never matches inside ``announced`` and ``Rich``
    never matches inside ``richness`` (review finding: plain ``in``-matching
    poisoned the narrator context with false positives). Python's ``re`` is
    Unicode-aware for ``str`` patterns, so Cyrillic / other-script names get
    correct word boundaries too.
    """
    words = [w for w in re.split(r"\s+", name.strip()) if w]
    if not words:
        return False
    pattern = r"\b" + r"\s+".join(re.escape(w) for w in words) + r"\b"
    return re.search(pattern, source_text, re.IGNORECASE) is not None


def build_narrator_context(
    book_memory: Mapping[str, Any],
    source_text: str,
) -> str:
    """Build the ``BOOK CONTEXT - FALLBACK ONLY`` narrator block (concept §3.1).

    Only canonical NAMED characters with a known gender from ``book_memory``
    that actually appear in the chapter's ``source_text`` are listed; generic
    role descriptions ("the nurse", "the man", ...) are excluded (they caused
    poisoned FPs, §3.1 lesson). Format matches the harness
    ``narrator_context_0001.txt``::

        narrator: Blake Thorburn (gender male)
        Blake Thorburn: male
        Callan: male
        ...

    Returns an empty string when nothing is renderable (caller omits the
    block). Deterministic: sorted, no set-iteration order.
    """
    from pact_v4.runtime.bible_renderer import extract_narrator_gender

    narrator = extract_narrator_gender(book_memory)
    lines: List[str] = []
    if narrator:
        source_name = ""
        pov = book_memory.get("pov")
        if isinstance(pov, Mapping):
            source_name = _norm_value(pov.get("source_name")) or ""
        if not source_name:
            source_name = "narrator"
        lines.append(f"narrator: {source_name} (gender {narrator})")

    lookup: Dict[str, str] = {}
    for section in ("characters", "entities"):
        data = book_memory.get(section)
        if isinstance(data, Mapping):
            for name, attrs in data.items():
                if isinstance(attrs, Mapping) and attrs.get("gender"):
                    lookup[str(name)] = str(attrs["gender"])
        elif isinstance(data, list):
            for entry in data:
                if not isinstance(entry, Mapping):
                    continue
                name = (
                    _norm_value(entry.get("name"))
                    or _norm_value(entry.get("source"))
                    or _norm_value(entry.get("english"))
                )
                gender = _norm_value(entry.get("gender"))
                if name and gender:
                    lookup[name] = gender

    for name in sorted(lookup):
        if not _canonical_name(name):
            continue
        if not _name_present_in_source(name, source_text):
            continue
        lines.append(f"{name}: {lookup[name]}")

    if not lines:
        return ""
    return "\n".join(lines) + "\n"


def _norm_value(value: Any) -> str:
    return str(value) if value is not None else ""


# ---------------------------------------------------------------------------
# Chunking + overlap (pure, deterministic)
# ---------------------------------------------------------------------------


def build_greedy_chunks(
    pairs: Sequence[AuditPair], max_input: int = DEFAULT_MAX_INPUT_TOKENS
) -> List[List[AuditPair]]:
    """Greedy chunking by estimated input tokens.

    Flush the current chunk as soon as adding the next pair would exceed
    ``max_input``; the next pair opens a new chunk. This is the honest
    greedy rule (the harness's ``Build-BalancedChunks`` reduces to it because
    ``target = total/K <= max_input`` makes both flush branches identical),
    and it yields exactly 8 chunks for chapter 0001 at ``max_input=3600``
    (B1 acceptance: ``chunking главы 0001 = ровно 8 чанков``).
    """
    chunks: List[List[AuditPair]] = []
    current: List[AuditPair] = []
    current_tokens = 0.0
    for pair in pairs:
        tokens = pair_token_estimate(pair.source, pair.translation)
        if current and current_tokens + tokens > max_input:
            chunks.append(current)
            current = []
            current_tokens = 0.0
        current.append(pair)
        current_tokens += tokens
    if current:
        chunks.append(current)
    return chunks


def get_overlap_context(
    all_pairs: Sequence[AuditPair],
    first_pid: str,
    max_tokens: int = DEFAULT_OVERLAP_TOKENS,
    min_pairs: int = MIN_OVERLAP_PAIRS,
    max_pairs: int = MAX_OVERLAP_PAIRS,
) -> List[AuditPair]:
    """Preceding pairs from the ORIGINAL chapter (``CONTEXT_ONLY`` overlap).

    Walks backwards from the chunk's first PID, collecting up to ~``max_tokens``
    (at least ``min_pairs``, at most ``max_pairs``). Always returns pairs from
    ``all_pairs`` (the original chapter), never from a cut child — that is the
    contract RetryShrink relies on for sub-chunks too.
    """
    index = -1
    for i, pair in enumerate(all_pairs):
        if pair.pid == first_pid:
            index = i
            break
    if index <= 0:
        return []
    context: List[AuditPair] = []
    tokens = 0.0
    count = 0
    for j in range(index - 1, -1, -1):
        if count >= max_pairs:
            break
        tokens_for = pair_token_estimate(all_pairs[j].source, all_pairs[j].translation)
        if count >= min_pairs and tokens + tokens_for > max_tokens:
            break
        context.insert(0, all_pairs[j])
        tokens += tokens_for
        count += 1
    return context


# ---------------------------------------------------------------------------
# Strict JSON validation (fail-closed; concept §2, harness Test-ChunkJson)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ValidationResult:
    valid: bool
    issues: Tuple[Dict[str, Any], ...] = ()
    errors: Tuple[str, ...] = ()


def validate_chunk_json(parsed: Any, chunk_pids: Sequence[str]) -> ValidationResult:
    """Validate one chunk response against the v4.1 issue contract.

    Fail-closed: any invalid issue makes the whole chunk invalid (a failed
    chunk is NEVER read as ``issues=[]``). Valid issues are still returned
    (harness behaviour) so a partially-invalid chunk keeps its valid findings
    while the chunk itself is recorded as failed.
    """
    errors: List[str] = []
    if not isinstance(parsed, dict):
        return ValidationResult(valid=False, errors=("not a JSON object",))
    issues = parsed.get("issues")
    if issues is None:
        return ValidationResult(valid=False, errors=("root object has no 'issues' field",))
    if not isinstance(issues, list):
        return ValidationResult(valid=False, errors=("'issues' is not an array",))
    valid_issues: List[Dict[str, Any]] = []
    chunk_pid_set = frozenset(chunk_pids)
    for index, issue in enumerate(issues):
        if not isinstance(issue, dict):
            errors.append(f"issue {index}: not an object")
            continue
        category = issue.get("category")
        severity = issue.get("severity")
        confidence = issue.get("confidence")
        pid = issue.get("id")
        problem = ""
        if category not in AUDIT_V4_CATEGORIES:
            problem = f"invalid category '{category}'"
        elif severity not in AUDIT_V4_SEVERITIES:
            problem = f"invalid severity '{severity}'"
        elif confidence not in AUDIT_V4_CONFIDENCES:
            problem = f"invalid confidence '{confidence}'"
        elif pid not in chunk_pid_set:
            problem = f"id '{pid}' not in chunk"
        if problem:
            errors.append(f"issue {pid}: {problem}")
        else:
            valid_issues.append(dict(issue))
    return ValidationResult(
        valid=not errors,
        issues=tuple(valid_issues),
        errors=tuple(errors),
    )


def classify_chunk(
    finish_reason: Optional[str],
    content: str,
    reasoning: str,
    valid: bool,
) -> str:
    """Harness ``Classify-Chunk``: GOOD / LENGTH / EMPTY / SPILL / INVALID_JSON."""
    if finish_reason == "length":
        return "LENGTH"
    if not content.strip():
        return "EMPTY"
    if valid:
        return "GOOD"
    if reasoning and _LOOKS_LIKE_REASONING.search(content.lower()):
        return "SPILL"
    return "INVALID_JSON"


def dedupe_issues(issues: Sequence[Mapping[str, Any]]) -> List[Dict[str, Any]]:
    """Dedup by ``id+category``; higher confidence wins (harness dedup)."""
    deduped: Dict[Tuple[str, str], Dict[str, Any]] = {}
    for issue in issues:
        key = (str(issue.get("id")), str(issue.get("category")))
        existing = deduped.get(key)
        if existing is None or _CONFIDENCE_RANK.get(
            str(issue.get("confidence")), 0
        ) > _CONFIDENCE_RANK.get(str(existing.get("confidence")), 0):
            deduped[key] = dict(issue)
    return list(deduped.values())


# ---------------------------------------------------------------------------
# Per-chunk execution + RetryShrink
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ChunkMeta:
    """Per-chunk outcome, mirroring the harness chunk-meta JSON."""

    chunk: int
    first_pid: str
    last_pid: str
    pair_count: int
    context_count: int
    status: str
    finish_reason: Optional[str]
    content_chars: int
    reasoning_chars: int
    json_parse: str
    validation_errors: Tuple[str, ...]
    reasoning_file: str
    issues: Tuple[Dict[str, Any], ...] = ()

    def to_payload(self) -> Dict[str, Any]:
        return {
            "chunk": self.chunk,
            "first_pid": self.first_pid,
            "last_pid": self.last_pid,
            "pair_count": self.pair_count,
            "context_count": self.context_count,
            "status": self.status,
            "finish_reason": self.finish_reason,
            "reasoning_chars": self.reasoning_chars,
            "reasoning_file": self.reasoning_file,
            "issue_count": len(self.issues),
        }


class BudgetOverflowError(ValueError):
    """Full-input budget exceeded (entity hard cap or calibrated_total)."""


def validate_input_budget(
    *,
    template: ReviewerPrompt,
    pairs: Sequence[AuditPair],
    narrator_context: str,
    entity_context: str,
    overlap_tokens: int,
    max_input_tokens: int,
    calibrated_total: Optional[int],
) -> int:
    """Enforce the full-input budget (concept §2) and return the effective
    per-chunk pair budget.

    ``fixed_prompt + narrator + entity + CONTEXT_ONLY + AUDIT_PAIRS <=
    calibrated_total``. The entity block has a soft (500) / hard (800) token
    cap: a hard overflow raises ``BudgetOverflowError`` (never silently
    truncated); a soft overflow is logged as a warning. When
    ``calibrated_total`` is not supplied it is derived from the fixed prompt +
    max pair budget + overlap + entity hard cap + narrator allowance, so a
    compliant context never shrinks the pair budget.
    """
    fixed = text_token_estimate(template.instructions)
    narrator_tokens = text_token_estimate(narrator_context)
    entity_tokens = text_token_estimate(entity_context)
    if entity_tokens > ENTITY_HARD_TOKENS:
        raise BudgetOverflowError(
            f"entity context ~{int(entity_tokens)} tokens exceeds hard cap "
            f"{ENTITY_HARD_TOKENS}"
        )
    if entity_tokens > ENTITY_SOFT_TOKENS:
        LOG.warning(
            "entity context ~%d tokens exceeds soft cap %d",
            int(entity_tokens), ENTITY_SOFT_TOKENS,
        )
    if calibrated_total is None:
        calibrated_total = int(
            fixed + max_input_tokens + overlap_tokens
            + ENTITY_HARD_TOKENS + 200.0  # narrator allowance + margin
        )
    pair_budget = int(
        min(max_input_tokens, calibrated_total - fixed - narrator_tokens
            - entity_tokens - overlap_tokens)
    )
    if pair_budget < 1:
        raise BudgetOverflowError(
            f"full input budget {calibrated_total} cannot fit fixed prompt "
            f"~{int(fixed)} + narrator ~{int(narrator_tokens)} + entity "
            f"~{int(entity_tokens)} + overlap {overlap_tokens}; "
            f"no room for AUDIT_PAIRS"
        )
    return pair_budget


# ---------------------------------------------------------------------------
# Evaluator
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ChunkedAuditConfig:
    """Settings for one chunked audit run (frozen contract of the run)."""

    max_input_tokens: int = DEFAULT_MAX_INPUT_TOKENS
    max_tokens: int = DEFAULT_MAX_TOKENS
    overlap_tokens: int = DEFAULT_OVERLAP_TOKENS
    min_overlap_pairs: int = MIN_OVERLAP_PAIRS
    max_overlap_pairs: int = MAX_OVERLAP_PAIRS
    retry_shrink: bool = True
    # R-RETRY (t_8ab8ab35, operator extension): bounded TRANSPORT_ERROR
    # retry with a NEW session (per_request backend → new session per
    # complete call). Identity-bearing via StrictRunConfig (F5) — a cache
    # written under a different transport-retry policy must never replay.
    transport_max_retries: int = DEFAULT_TRANSPORT_MAX_RETRIES
    transport_base_delay_seconds: float = DEFAULT_TRANSPORT_BASE_DELAY_SECONDS
    reasoning_budget: int = DEFAULT_REASONING_BUDGET
    calibrated_total: Optional[int] = None
    template: ReviewerPrompt = QWEN_AUDIT_V4_1
    label: str = "phase3/qwen_chapter_audit_v4"
    harness_version: str = HARNESS_VERSION
    prompt_version: str = PROMPT_VERSION


@dataclass(frozen=True)
class ChunkedAuditOutcome:
    """Aggregated result (``schema: pact-audit/v4``)."""

    schema: str
    harness_version: str
    prompt_version: str
    model: str
    reasoning_budget: int
    max_input_tokens: int
    max_tokens: int
    overlap_tokens: int
    narrator_context: bool
    entity_context: bool
    chunk_count: int
    successful_chunks: int
    failed_chunks: Tuple[int, ...]
    audit_complete: bool
    issue_count: int
    issues: Tuple[Dict[str, Any], ...]
    chunks: Tuple[Dict[str, Any], ...]

    def to_payload(self) -> Dict[str, Any]:
        return {
            "schema": self.schema,
            "harness_version": self.harness_version,
            "prompt_version": self.prompt_version,
            "model": self.model,
            "reasoning_budget": self.reasoning_budget,
            "max_input_tokens": self.max_input_tokens,
            "max_tokens": self.max_tokens,
            "overlap_tokens": self.overlap_tokens,
            "narrator_context": self.narrator_context,
            "entity_context": self.entity_context,
            "chunk_count": self.chunk_count,
            "successful_chunks": self.successful_chunks,
            "failed_chunks": list(self.failed_chunks),
            "audit_complete": self.audit_complete,
            "issue_count": self.issue_count,
            "issues": list(self.issues),
            "chunks": [dict(c) for c in self.chunks],
        }


class ChunkedAuditEvaluator:
    """Chunked Qwen audit over a ``CompletionBackend`` (transport-neutral).

    Usage::

        evaluator = ChunkedAuditEvaluator(backend, config=ChunkedAuditConfig())
        outcome = evaluator(
            chapter_id="0001",
            pairs=pairs_from_maps(source_map, translation_map),
            narrator_context="narrator: Blake Thorburn (gender male)\\n...",
            entity_context="- entity: Blake's vehicle\\n  ...",
        )

    Each chunk is one ``CompletionRequest`` (``max_output_tokens`` from
    config, temperature 0.0, ``json_object`` schema) — never a reasoning
    ``request_options`` value (V4.1: the reasoning budget is a SERVER ARG
    ``--reasoning-budget``; ``LocalOpenAIBackend`` rejects request_options).
    """

    def __init__(
        self,
        backend: CompletionBackend,
        *,
        config: Optional[ChunkedAuditConfig] = None,
        on_chunk_event: Optional[Callable[[str, Dict[str, Any]], None]] = None,
    ) -> None:
        self._backend = backend
        self._config = config or ChunkedAuditConfig()
        # V4.1 B3 review fix (F7): an optional per-model-call event hook so
        # the caller (b3_audit_repair's append-only journal) can record
        # ``audit_chunk_started`` BEFORE the model call and a terminal
        # ``audit_chunk_done`` after it — a crash during a chunk leaves the
        # started event as evidence instead of nothing. Invoked as
        # ``on_chunk_event("started"|"done", {chunk, total, status,
        # issue_count, ...})``.
        self._on_chunk_event = on_chunk_event

    @property
    def backend(self) -> CompletionBackend:
        return self._backend

    def _emit_chunk_event(self, kind: str, **fields: Any) -> None:
        if self._on_chunk_event is not None:
            try:
                self._on_chunk_event(kind, fields)
            except Exception:  # noqa: BLE001 — a journal hook must never break the audit
                LOG.debug(
                    "chunked audit on_chunk_event(%r) failed", kind, exc_info=True
                )


    def __call__(
        self,
        *,
        chapter_id: str,
        pairs: Sequence[AuditPair],
        narrator_context: str = "",
        entity_context: str = "",
        out_dir: Optional[Path] = None,
        out_base: str = "audit",
    ) -> ChunkedAuditOutcome:
        cfg = self._config
        model_ref = audit_model_ref(self._backend)

        if not pairs:
            raise CoverageError(
                f"no audit pairs for chapter {chapter_id!r}: empty "
                f"source/translation input rejected before any model call "
                f"(fail-closed — never audit_complete with 0 chunks)"
            )

        pair_budget = validate_input_budget(
            template=cfg.template,
            pairs=pairs,
            narrator_context=narrator_context,
            entity_context=entity_context,
            overlap_tokens=cfg.overlap_tokens,
            max_input_tokens=cfg.max_input_tokens,
            calibrated_total=cfg.calibrated_total,
        )

        chunks = build_greedy_chunks(pairs, max_input=pair_budget)
        all_meta: List[ChunkMeta] = []
        all_issues: List[Dict[str, Any]] = []

        for chunk_index, chunk_pairs in enumerate(chunks, start=1):
            chunk_pids = [p.pid for p in chunk_pairs]
            context_pairs = get_overlap_context(
                pairs, chunk_pairs[0].pid, cfg.overlap_tokens,
                cfg.min_overlap_pairs, cfg.max_overlap_pairs,
            )
            meta = self._run_one_chunk(
                chapter_id=chapter_id,
                chunk_index=chunk_index,
                chunk_total=len(chunks),
                chunk_pairs=chunk_pairs,
                context_pairs=context_pairs,
                narrator_context=narrator_context,
                entity_context=entity_context,
                model_ref=model_ref,
                out_dir=out_dir,
                out_base=out_base,
                suffix="",
            )
            # RetryShrink is an INPUT-SIZE strategy (LENGTH / INVALID_JSON /
            # EMPTY / SPILL): a TRANSPORT_ERROR cannot be fixed by shrinking
            # the chunk, so it is recorded as failed as-is (fail-closed).
            if cfg.retry_shrink and meta.status != "GOOD" and meta.status != "TRANSPORT_ERROR":
                meta = self._retry_shrink(
                    chapter_id=chapter_id,
                    chunk_index=chunk_index,
                    chunk_total=len(chunks),
                    original_pairs=pairs,
                    pending=chunk_pairs,
                    narrator_context=narrator_context,
                    entity_context=entity_context,
                    model_ref=model_ref,
                    out_dir=out_dir,
                    out_base=out_base,
                )
            all_meta.append(meta)
            all_issues.extend(meta.issues)

        final_issues = self._attach_debug_and_dedupe(all_issues, all_meta)

        successful = [
            m.chunk for m in all_meta
            if m.status in ("GOOD", "GOOD_RETRIED")
        ]
        failed = [
            m.chunk for m in all_meta
            if m.status not in ("GOOD", "GOOD_RETRIED")
        ]
        audit_complete = not failed

        return ChunkedAuditOutcome(
            schema=SCHEMA,
            harness_version=cfg.harness_version,
            prompt_version=cfg.prompt_version,
            model=model_ref,
            reasoning_budget=cfg.reasoning_budget,
            max_input_tokens=cfg.max_input_tokens,
            max_tokens=cfg.max_tokens,
            overlap_tokens=cfg.overlap_tokens,
            narrator_context=bool(narrator_context.strip()),
            entity_context=bool(entity_context.strip()),
            chunk_count=len(all_meta),
            successful_chunks=len(successful),
            failed_chunks=tuple(failed),
            audit_complete=audit_complete,
            issue_count=len(final_issues),
            issues=tuple(final_issues),
            chunks=tuple(m.to_payload() for m in all_meta),
        )

    # ------------------------------------------------------------------
    # internals
    # ------------------------------------------------------------------

    def _request(
        self,
        *,
        chapter_id: str,
        prompt: str,
        model_ref: str,
    ) -> CompletionRequest:
        return CompletionRequest(
            model_ref=model_ref,
            messages=(Message(role="user", content=prompt),),
            max_output_tokens=self._config.max_tokens,
            temperature=0.0,
            response_schema=JSON_OBJECT_SCHEMA,
            label=self._config.label,
            # NOTE: no request_options — the reasoning budget is a server
            # arg (--reasoning-budget); LocalOpenAIBackend rejects options.
        )

    def _write_artifacts(
        self,
        *,
        out_dir: Optional[Path],
        file_stem: str,
        content: str,
        reasoning: str,
    ) -> str:
        """Persist raw/reasoning artifacts; return the reasoning file NAME."""
        raw_name = f"{file_stem}_raw.txt"
        reason_name = f"{file_stem}_reasoning.txt"
        if out_dir is not None:
            out_dir.mkdir(parents=True, exist_ok=True)
            (out_dir / raw_name).write_text(content, encoding="utf-8")
            (out_dir / reason_name).write_text(reasoning, encoding="utf-8")
        return reason_name

    def _run_one_chunk(
        self,
        *,
        chapter_id: str,
        chunk_index: int,
        chunk_total: int,
        chunk_pairs: Sequence[AuditPair],
        context_pairs: Sequence[AuditPair],
        narrator_context: str,
        entity_context: str,
        model_ref: str,
        out_dir: Optional[Path],
        out_base: str,
        suffix: str,
    ) -> ChunkMeta:
        cfg = self._config
        prompt = render_chunked_audit_prompt(
            chunk_id=chapter_id,
            audit_pairs=chunk_pairs,
            context_pairs=context_pairs,
            narrator_context=narrator_context,
            entity_context=entity_context,
            chunk_index=chunk_index,
            chunk_total=chunk_total,
            template=cfg.template,
        )
        request = self._request(chapter_id=chapter_id, prompt=prompt, model_ref=model_ref)
        chunk_pids = [p.pid for p in chunk_pairs]
        file_stem = (
            f"{out_base}_{suffix}_chunk{chunk_index}"
            if suffix else f"{out_base}_chunk{chunk_index}"
        )
        try:
            self._emit_chunk_event(
                "started",
                chunk=chunk_index,
                total=chunk_total,
                pids=chunk_pids,
                sub=suffix or "",
            )
            # R-RETRY (t_8ab8ab35, operator extension 2026-08-13): a
            # TRANSPORT_ERROR is retried with a NEW session (per_request
            # backend → each complete() call creates a fresh session), not
            # RetryShrink — a transport failure is not an input-size
            # problem. Bounded by cfg.transport_max_retries + backoff.
            max_attempts = cfg.transport_max_retries + 1
            for attempt in range(max_attempts):
                try:
                    response = self._backend.complete(request)
                    break
                except Exception as exc:  # CompletionError + transport-level failures
                    if attempt < cfg.transport_max_retries:
                        delay = cfg.transport_base_delay_seconds * (2 ** attempt)
                        LOG.warning(
                            "audit chunk %s transport failure (%s) — retry "
                            "%d/%d in %.2fs (new session)",
                            chunk_index, type(exc).__name__,
                            attempt + 1, max_attempts, delay,
                        )
                        self._emit_chunk_event(
                            "retry",
                            chunk=chunk_index,
                            total=chunk_total,
                            attempt=attempt + 1,
                            error=f"{type(exc).__name__}: {exc}",
                            delay=delay,
                        )
                        time.sleep(delay)
                        continue
                    LOG.error(
                        "audit chunk %s transport failure (%s): %s",
                        chunk_index, type(exc).__name__, exc,
                    )
                    self._write_artifacts(
                        out_dir=out_dir, file_stem=file_stem,
                        content=f"TRANSPORT_ERROR: {type(exc).__name__}: {exc}\n",
                        reasoning="",
                    )
                    meta = ChunkMeta(
                        chunk=chunk_index,
                        first_pid=chunk_pids[0],
                        last_pid=chunk_pids[-1],
                        pair_count=len(chunk_pairs),
                        context_count=len(context_pairs),
                        status="TRANSPORT_ERROR",
                        finish_reason=None,
                        content_chars=0,
                        reasoning_chars=0,
                        json_parse="transport_error",
                        validation_errors=(f"{type(exc).__name__}: {exc}",),
                        reasoning_file=f"{file_stem}_reasoning.txt",
                        issues=(),
                    )
                    self._emit_chunk_event(
                        "done",
                        chunk=chunk_index,
                        total=chunk_total,
                        status=meta.status,
                        issue_count=0,
                        error=f"{type(exc).__name__}: {exc}",
                    )
                    return meta
        except Exception as exc:  # pragma: no cover - defensive (event hook)
            LOG.error(
                "audit chunk %s unexpected failure (%s): %s",
                chunk_index, type(exc).__name__, exc,
            )
            self._write_artifacts(
                out_dir=out_dir, file_stem=file_stem,
                content=f"TRANSPORT_ERROR: {type(exc).__name__}: {exc}\n",
                reasoning="",
            )
            meta = ChunkMeta(
                chunk=chunk_index,
                first_pid=chunk_pids[0],
                last_pid=chunk_pids[-1],
                pair_count=len(chunk_pairs),
                context_count=len(context_pairs),
                status="TRANSPORT_ERROR",
                finish_reason=None,
                content_chars=0,
                reasoning_chars=0,
                json_parse="transport_error",
                validation_errors=(f"{type(exc).__name__}: {exc}",),
                reasoning_file=f"{file_stem}_reasoning.txt",
                issues=(),
            )
            self._emit_chunk_event(
                "done",
                chunk=chunk_index,
                total=chunk_total,
                status=meta.status,
                issue_count=0,
                error=f"{type(exc).__name__}: {exc}",
            )
            return meta
        content = response.text or ""
        reasoning = str((response.raw_metadata or {}).get("reasoning") or "")

        reason_file = self._write_artifacts(
            out_dir=out_dir, file_stem=file_stem, content=content, reasoning=reasoning,
        )

        clean_json = re.sub(r"```json|```", "", content).strip()
        parsed = None
        try:
            parsed = json.loads(clean_json)
            json_parse = "ok"
        except Exception:
            parsed = None
            json_parse = "failed"
        validation = validate_chunk_json(parsed, chunk_pids)
        status = classify_chunk(
            response.finish_reason, content, reasoning, validation.valid
        )

        meta = ChunkMeta(
            chunk=chunk_index,
            first_pid=chunk_pids[0],
            last_pid=chunk_pids[-1],
            pair_count=len(chunk_pairs),
            context_count=len(context_pairs),
            status=status,
            finish_reason=response.finish_reason,
            content_chars=len(content),
            reasoning_chars=len(reasoning),
            json_parse=json_parse,
            validation_errors=validation.errors,
            reasoning_file=reason_file,
            issues=validation.issues,
        )
        self._emit_chunk_event(
            "done",
            chunk=chunk_index,
            total=chunk_total,
            status=meta.status,
            issue_count=len(validation.issues),
        )
        LOG.debug(
            "audit chunk %s: %s | finish=%s | issues=%d",
            chunk_index, status, response.finish_reason, len(validation.issues),
        )
        return meta

    def _retry_shrink(
        self,
        *,
        chapter_id: str,
        chunk_index: int,
        chunk_total: int,
        original_pairs: Sequence[AuditPair],
        pending: Sequence[AuditPair],
        narrator_context: str,
        entity_context: str,
        model_ref: str,
        out_dir: Optional[Path],
        out_base: str,
    ) -> ChunkMeta:
        """RetryShrink by input: re-balance failed pairs at /2 then /3.

        Overlap for every sub-chunk comes from ``original_pairs`` (the
        ORIGINAL chapter), never from the cut child (harness contract). When
        every sub of a level is GOOD the chunk becomes ``GOOD_RETRIED`` and
        pending is CLEARED (the harness bug that left ``$pending`` populated
        and reported a false ``FAILED_RETRIED`` is fixed here).
        """
        cfg = self._config
        sub_issues: List[Dict[str, Any]] = []
        still_pending: List[AuditPair] = list(pending)
        all_ok = False

        for level, sub_limit in enumerate(
            (
                max(1, int(cfg.max_input_tokens / 2.0)),
                max(1, int(cfg.max_input_tokens / 3.0)),
            ),
            start=1,
        ):
            if not still_pending:
                break
            sub_chunks = build_greedy_chunks(still_pending, max_input=sub_limit)
            new_pending: List[AuditPair] = []
            level_ok = True
            for sub_index, sub_pairs in enumerate(sub_chunks, start=1):
                sub_pids = [p.pid for p in sub_pairs]
                sub_context = get_overlap_context(
                    original_pairs, sub_pairs[0].pid, cfg.overlap_tokens,
                    cfg.min_overlap_pairs, cfg.max_overlap_pairs,
                )
                sub_meta = self._run_one_chunk(
                    chapter_id=chapter_id,
                    chunk_index=chunk_index,
                    chunk_total=chunk_total,
                    chunk_pairs=sub_pairs,
                    context_pairs=sub_context,
                    narrator_context=narrator_context,
                    entity_context=entity_context,
                    model_ref=model_ref,
                    out_dir=out_dir,
                    out_base=out_base,
                    suffix=f"lvl{level}_sub{sub_index}",
                )
                if sub_meta.status == "TRANSPORT_ERROR":
                    # transport failure is not input-size: re-queueing would
                    # just multiply dead calls; record the sub as failed and
                    # stop shrinking it.
                    level_ok = False
                    continue
                if sub_meta.status not in ("GOOD", "GOOD_RETRIED"):
                    level_ok = False
                    new_pending.extend(sub_pairs)
                else:
                    for issue in sub_meta.issues:
                        sub_issues.append(self._with_debug(
                            issue, sub_meta.chunk, sub_meta.reasoning_file,
                        ))
            if level_ok:
                all_ok = True
                still_pending = []
                break
            still_pending = new_pending

        if all_ok and not still_pending:
            status = "GOOD_RETRIED"
        else:
            status = "FAILED_RETRIED"
            LOG.warning(
                "[retry-shrink] STILL FAILED after shrink (%d pairs un-audited)",
                len(still_pending),
            )
        first_pid = pending[0].pid if pending else ""
        last_pid = pending[-1].pid if pending else ""
        return ChunkMeta(
            chunk=chunk_index,
            first_pid=first_pid,
            last_pid=last_pid,
            pair_count=len(pending),
            context_count=0,
            status=status,
            finish_reason=None,
            content_chars=0,
            reasoning_chars=0,
            json_parse="retried",
            validation_errors=(),
            reasoning_file="",
            issues=tuple(sub_issues),
        )

    @staticmethod
    def _with_debug(
        issue: Mapping[str, Any], chunk: Optional[int], reasoning_file: str
    ) -> Dict[str, Any]:
        return {**dict(issue), "_debug": {"chunk": chunk, "reasoning_file": reasoning_file}}

    @staticmethod
    def _attach_debug_and_dedupe(
        issues: Sequence[Mapping[str, Any]], metas: Sequence[ChunkMeta]
    ) -> List[Dict[str, Any]]:
        """Dedup by ``id+category`` (high confidence wins), filling ``_debug``.

        Mirrors the harness: issues from sub-chunks already carry their own
        ``_debug`` (attached at collection time); after dedup, an issue still
        missing ``_debug`` gets ``{chunk, reasoning_file}`` from the first
        chunk whose issue list contains the id.
        """
        deduped: Dict[Tuple[str, str], Dict[str, Any]] = {}
        for issue in issues:
            key = (str(issue.get("id")), str(issue.get("category")))
            existing = deduped.get(key)
            if existing is None or _CONFIDENCE_RANK.get(
                str(issue.get("confidence")), 0
            ) > _CONFIDENCE_RANK.get(str(existing.get("confidence")), 0):
                deduped[key] = dict(issue)
        for key, issue in list(deduped.items()):
            if "_debug" in issue:
                continue
            pid = issue.get("id")
            for meta in metas:
                if any(str(i.get("id")) == pid for i in meta.issues):
                    deduped[key] = {**issue, "_debug": {
                        "chunk": meta.chunk, "reasoning_file": meta.reasoning_file,
                    }}
                    break
        return list(deduped.values())


__all__ = [
    "AUDIT_V4_CATEGORIES",
    "AUDIT_V4_CONFIDENCES",
    "AUDIT_V4_SEVERITIES",
    "AuditPair",
    "BudgetOverflowError",
    "ChunkedAuditConfig",
    "ChunkedAuditEvaluator",
    "ChunkedAuditOutcome",
    "ChunkMeta",
    "CoverageError",
    "GENERIC_DESCRIPTIONS",
    "HARNESS_VERSION",
    "PROMPT_VERSION",
    "SCHEMA",
    "ValidationResult",
    "audit_model_ref",
    "build_greedy_chunks",
    "build_narrator_context",
    "classify_chunk",
    "dedupe_issues",
    "get_overlap_context",
    "pairs_from_maps",
    "pair_token_estimate",
    "text_token_estimate",
    "validate_chunk_json",
    "validate_input_budget",
]
