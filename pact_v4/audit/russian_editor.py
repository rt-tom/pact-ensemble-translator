"""V4.2 R: Russian-only editor stage — candidate generator + SAFE auto-apply.

Card t_4707e6e5 (4.2 = 4.1 + этап R). Owner decision 2026-08-11: Qwen edits
the translation WITHOUT the English source, right after whole-chapter
generation and BEFORE the audit (Qwen already resident — 0 restarts).
Pipeline position::

    перевод → R → entity → аудит → Tier A → Tier B(repair) → re-audit → formatting

Stage contract (v4.2-R1, mirroring the gemma_rewrite_v4.py test pattern and
the 25-edit run_010 analysis 2026-08-11: 56% useful / 20% doubtful / 24%
harmful):

1. **Input** — ``translations_raw.json``: the RUSSIAN translation map
   ``{pid: text}``. NO English source anywhere in the prompt.
2. **Chunking** — chunks of ``chunk_size`` (default 50) PIDs in source
   order, each chunk preceded by up to ``overlap_pairs`` CONTEXT_ONLY pairs
   from the ORIGINAL chapter (continuity; the model must NEVER propose an
   edit for a CONTEXT_ONLY pid).
3. **Prompt contract** — edits-only JSON
   ``{edits: [{pid, original, rewritten, reason, class}]}``; each edit is
   tagged with exactly one class:
   * SAFE (auto-apply): ``typo | grammar | duplicate | preposition``
   * REVIEW (candidates only): ``calque | logic | ambiguity | unnatural |
     register``
4. **Diff-gate** — a SAFE-classed edit is applied ONLY when
   ``rewritten != original`` (cuts the p00095-class false positive where
   Qwen proposes the same text). A no-op edit is dropped for BOTH classes
   (never applied, never forwarded as a candidate).
5. **Routing (R-FIX2 substring-replace)** — a SAFE edit is applied as
   ``current.replace(original, rewritten, 1)``: only the quoted fragment
   changes, the rest of the PID text is preserved (run_012 p00010-class
   fragments; also the run_011 p00244-class truncation guard). Applied
   edits become ``translations_edited.json``; REVIEW edits become
   ``edit_candidates.json`` and are NEVER auto-applied (they are later
   verified by the B2 repair-as-verifier against the ORIGINAL).
6. **Fail-closed (per-chunk)** — a structurally invalid chunk (unknown pid,
   pid outside the chunk, ``original`` not a verbatim substring of the
   current text, unknown class, non-string/missing fields) makes the WHOLE
   chunk FAILED, and the stage is recorded ``complete=False``. R-RETRY
   (t_8ab8ab35): a DUPLICATE pid is NOT structural — up to
   ``MAX_EDITS_PER_PID`` edits per pid are accepted (the model legitimately
   returns 2+ problems for one pid, run_remote_002 chunk4 p00180), the
   11th+ drops per-edit with a WARNING; TRANSPORT failures and
   INVALID_JSON/empty bodies get a bounded retry (3 attempts, backoff).
   Fail-closed is per-chunk: a failed chunk contributes NO edits to
   ``edits``/``applied``/``candidates``, so the caller applies exactly the
   successful chunks' work (RESILIENCE t_406fc48c, run_remote_001: 17 valid
   edits from 5 GOOD chunks are no longer discarded because 3 chunks failed;
   the B3 integration journals ``partial=true`` and the audit still protects
   the chapter, like a failed repair batch).

Transport: the evaluator is backend-neutral over ``CompletionBackend`` (the
same boundary the B1 chunked audit uses); it resolves the model ref via
``audit_model_ref`` (Qwen — the editor is the audit model, owner decision).
The lifecycle wrapper supplies the local ``llama-server`` backend; the
evaluator itself never imports ``model_lifecycle*``.

This module is pure and deterministic except for the injected model calls.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple

from pact_v4.audit.chunked_audit import audit_model_ref
from pact_v4.runtime.backend_protocol import (
    JSON_OBJECT_SCHEMA,
    CompletionBackend,
    CompletionRequest,
    Message,
)
from pact_v4.runtime.json_resilience import (
    JsonRetryPolicy,
    parse_json_response,
)
from pact_v4.runtime.prompts_runtime import (
    RUSSIAN_EDITOR_V4_2_R1,
    ReviewerPrompt,
    render_russian_editor_prompt,
)

LOG = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Frozen contract constants (card t_4707e6e5, contract v4.2-R1)
# ---------------------------------------------------------------------------

RUSSIAN_EDITOR_SCHEMA = "pact-v4-russian-editor/v1"
RUSSIAN_EDITOR_HARNESS_VERSION = "4.2"
RUSSIAN_EDITOR_PROMPT_VERSION = RUSSIAN_EDITOR_V4_2_R1.version

# SAFE classes are auto-applied (with the diff-gate); REVIEW classes become
# edit_candidates.json and are never auto-applied.
SAFE_CLASSES = frozenset({"typo", "grammar", "duplicate", "preposition"})
REVIEW_CLASSES = frozenset(
    {"calque", "logic", "ambiguity", "unnatural", "register"}
)
ALL_CLASSES = SAFE_CLASSES | REVIEW_CLASSES

DEFAULT_CHUNK_SIZE = 50
DEFAULT_OVERLAP_PAIRS = 6
# Qwen server profile (reasoning 8192 + content headroom) — same budget as
# the chunked audit; the editor never emits request_options (reasoning is a
# server arg, V4.1 rule).
DEFAULT_MAX_TOKENS = 12000

# R-RETRY (t_8ab8ab35, owner contract 2026-08-13): the R stage is the only
# phase WITHOUT retry; run_remote_002/013 showed two FAILED-chunk classes.
# This cap allows up to N edits per pid — the model legitimately returns
# 2+ problems for one pid (typo + grammar, run_remote_002 chunk4 p00180);
# the old "1 pid = 1 edit" fail-closed contract discarded the WHOLE chunk
# on a duplicate. The 11th+ edit of the same pid is dropped per-edit with a
# WARNING (journal), never a structural error.
MAX_EDITS_PER_PID = 10

# Bounded retry policy for the R stage (mirrors the re-audit B4 pattern:
# selective_repair.JsonRetryPolicy defaults — max_retries=2 -> 3 attempts,
# base_delay_seconds=1.0). Applied to TRANSPORT failures AND
# INVALID_JSON/empty bodies; structural errors (pid outside chunk, unknown
# class, original not a substring) are NEVER retried (not randomness).
DEFAULT_RETRY_MAX_RETRIES: int = JsonRetryPolicy().max_retries
DEFAULT_RETRY_BASE_DELAY_SECONDS: float = JsonRetryPolicy().base_delay_seconds


# ---------------------------------------------------------------------------
# Data shapes
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TranslationPair:
    """One Russian-only pair (a PID's Russian text, no English source)."""

    pid: str
    text: str


@dataclass(frozen=True)
class EditorEdit:
    """One parsed/validated edit proposal from the Russian editor.

    ``klass`` is the model-tagged class (one of ``ALL_CLASSES``); the
    routing (SAFE auto-apply vs REVIEW candidate) is a code-side decision
    over the class threshold, never a model decision.
    """

    pid: str
    original: str
    rewritten: str
    reason: str
    klass: str


@dataclass(frozen=True)
class ReviewCandidate:
    """One REVIEW-classed edit that is NOT auto-applied.

    Carried to the B2 selective repair as additional verify-before-repair
    input (``(pid, original, proposed, class)`` per the card); the verifier
    accepts/rejects each against the ORIGINAL. ``source_stage`` names the
    stage that produced the candidate (CANDIDATE-MERGE, t_0ffe56e1): always
    ``"russian_editor"`` — the repair prompt renders it so the verifier
    applies the right contract (editor: is there a Russian-language defect
    and can it be fixed without changing the SOURCE meaning?).
    """

    pid: str
    original: str
    proposed: str
    klass: str
    reason: str = ""
    source_stage: str = "russian_editor"


@dataclass(frozen=True)
class RussianEditorConfig:
    """Settings for one Russian-editor pass (frozen contract of the run).

    ``safe_classes`` is the class threshold: any class in this frozenset is
    auto-applied (with the diff-gate); every other known class routes to
    REVIEW candidates. Identity-bearing via ``StrictRunConfig`` — flipping
    the threshold or the chunk settings invalidates the repaired cache.
    """

    chunk_size: int = DEFAULT_CHUNK_SIZE
    overlap_pairs: int = DEFAULT_OVERLAP_PAIRS
    max_tokens: int = DEFAULT_MAX_TOKENS
    safe_classes: frozenset = frozenset(SAFE_CLASSES)
    template: ReviewerPrompt = RUSSIAN_EDITOR_V4_2_R1
    label: str = "phase3/russian_editor_v4"
    harness_version: str = RUSSIAN_EDITOR_HARNESS_VERSION
    prompt_version: str = RUSSIAN_EDITOR_PROMPT_VERSION
    # R-RETRY (t_8ab8ab35): the per-pid edit cap (duplicate pid is NOT an
    # error — up to this many edits per pid; the 11th+ drops per-edit with
    # a WARNING). Identity-bearing via StrictRunConfig (F5).
    max_edits_per_pid: int = MAX_EDITS_PER_PID
    # Bounded retry policy (transport + empty/truncated JSON), identity-
    # bearing via StrictRunConfig (F5): flipping it must invalidate cache.
    retry_max_retries: int = DEFAULT_RETRY_MAX_RETRIES
    retry_base_delay_seconds: float = DEFAULT_RETRY_BASE_DELAY_SECONDS

    def to_payload(self) -> Dict[str, Any]:
        return {
            "chunk_size": self.chunk_size,
            "overlap_pairs": self.overlap_pairs,
            "max_tokens": self.max_tokens,
            "safe_classes": sorted(self.safe_classes),
            "label": self.label,
            "harness_version": self.harness_version,
            "prompt_version": self.prompt_version,
            "max_edits_per_pid": self.max_edits_per_pid,
            "r_editor_retry": {
                "max_retries": self.retry_max_retries,
                "base_delay_seconds": self.retry_base_delay_seconds,
            },
        }


@dataclass(frozen=True)
class RussianEditorOutcome:
    """Aggregated result of one Russian-editor pass.

    ``edits`` carries every structurally valid edit of GOOD chunks;
    ``applied`` is the SAFE/diff-gated subset as ``(pid, new_text)`` where
    ``new_text`` is the current text with the ``original`` fragment replaced
    once by ``rewritten`` (R-FIX2 substring-replace — the rest of the PID is
    preserved); ``candidates`` is the REVIEW subset (never auto-applied).
    ``dropped`` counts no-op edits (rewritten == original) cut by the
    diff-gate. ``complete`` is False when ANY chunk failed — fail-closed is
    per-chunk: ``edits``/``applied``/``candidates`` contain ONLY the
    successful chunks' content, so a partial pass applies exactly the GOOD
    chunks' work (RESILIENCE t_406fc48c, run_remote_001) and the caller
    never sees a failed chunk's edits.
    """

    schema: str
    harness_version: str
    prompt_version: str
    model: str
    chunk_size: int
    overlap_pairs: int
    chunk_count: int
    successful_chunks: int
    failed_chunks: Tuple[int, ...]
    complete: bool
    edits: Tuple[EditorEdit, ...]
    applied: Tuple[Tuple[str, str], ...]
    candidates: Tuple[ReviewCandidate, ...]
    dropped: int
    # R-RETRY (t_8ab8ab35): count of per-edit drops that did NOT fail the
    # chunk (edits over MAX_EDITS_PER_PID dropped with a WARNING; SAFE edits
    # whose fragment stopped being a substring after earlier same-pid edits).
    warning_count: int = 0

    def to_payload(self) -> Dict[str, Any]:
        return {
            "schema": self.schema,
            "harness_version": self.harness_version,
            "prompt_version": self.prompt_version,
            "model": self.model,
            "chunk_size": self.chunk_size,
            "overlap_pairs": self.overlap_pairs,
            "chunk_count": self.chunk_count,
            "successful_chunks": self.successful_chunks,
            "failed_chunks": list(self.failed_chunks),
            "complete": self.complete,
            "edits": [
                {
                    "pid": e.pid,
                    "original": e.original,
                    "rewritten": e.rewritten,
                    "reason": e.reason,
                    "class": e.klass,
                }
                for e in self.edits
            ],
            "applied": [list(pair) for pair in self.applied],
            "candidates": [
                {
                    "pid": c.pid,
                    "original": c.original,
                    "proposed": c.proposed,
                    "class": c.klass,
                    "reason": c.reason,
                }
                for c in self.candidates
            ],
            "dropped": self.dropped,
            "warning_count": self.warning_count,
        }


# ---------------------------------------------------------------------------
# Chunking (pure, deterministic)
# ---------------------------------------------------------------------------


def build_editor_chunks(
    pairs: Sequence[TranslationPair],
    chunk_size: int = DEFAULT_CHUNK_SIZE,
) -> List[List[TranslationPair]]:
    """Fixed-count chunking: ``chunk_size`` PIDs per chunk (source order).

    The R stage is count-bounded (card: ``чанки 50``), unlike the audit's
    token-bounded greedy rule — the editor input is Russian-only text.
    """
    return [
        list(pairs[i : i + chunk_size])
        for i in range(0, len(pairs), chunk_size)
    ]


def get_editor_overlap(
    pairs: Sequence[TranslationPair],
    first_pid: str,
    max_pairs: int = DEFAULT_OVERLAP_PAIRS,
) -> List[TranslationPair]:
    """Preceding pairs from the ORIGINAL chapter (CONTEXT_ONLY overlap).

    Walks backwards from the chunk's first PID, collecting up to
    ``max_pairs`` preceding pairs. Always returns pairs from the original
    chapter (continuity), never from a sibling chunk.
    """
    index = -1
    for i, pair in enumerate(pairs):
        if pair.pid == first_pid:
            index = i
            break
    if index <= 0:
        return []
    start = max(0, index - max_pairs)
    return list(pairs[start:index])


# ---------------------------------------------------------------------------
# Response parsing (fail-closed)
# ---------------------------------------------------------------------------


def _is_retryable_json_error(errors: Sequence[str]) -> bool:
    """Whether ``parse_editor_edits`` errors are INVALID_JSON/empty (retryable).

    R-RETRY (t_8ab8ab35): only the JSON-level failure (empty body / not
    complete JSON after fence/prose stripping) is retried. Structural
    errors (pid outside chunk, unknown class, original not a substring,
    missing fields) and valid-JSON-wrong-shape (B4: not a retry trigger)
    are NOT randomness — fail-closed as-is.
    """
    if not errors:
        return False
    # parse_editor_edits reports the JSON-level failure as a single
    # "response is not valid JSON: ..." error; structural errors never
    # start with that prefix. A valid-JSON-wrong-shape body (bare string,
    # etc.) is reported by parse_json_response as "valid JSON but not an
    # object" — per B4 that is NOT a retry trigger (retry only
    # empty/truncated JSON).
    if len(errors) != 1 or not errors[0].startswith("response is not valid JSON:"):
        return False
    return "valid JSON but not an object" not in errors[0]


def parse_editor_edits(
    text: str,
    chunk_pids: Sequence[str],
    current_by_pid: Mapping[str, str],
    max_edits_per_pid: int = MAX_EDITS_PER_PID,
) -> Tuple[Tuple[EditorEdit, ...], Tuple[str, ...], Tuple[str, ...]]:
    """Parse and strictly validate one Russian-editor chunk response.

    Fail-closed contract: the response must be a JSON object with an
    ``edits`` array; every edit must name a pid OF THE CURRENT CHUNK (never
    CONTEXT_ONLY), quote the exact FRAGMENT being fixed verbatim from the
    current text as ``original`` (one sentence or a shorter span; it must be
    a substring of the current text — R-FIX2, run_012 p00010-class
    fragments), carry a non-empty ``rewritten`` and ``reason``, and tag a
    KNOWN class. Any structural violation (unknown pid, original not a
    substring, unknown class, missing/non-string fields) fails the WHOLE
    chunk — a bad chunk is never silently read as ``edits=[]``.

    R-RETRY (t_8ab8ab35, owner contract 2026-08-13): a DUPLICATE pid is NOT
    a structural violation anymore — the model legitimately returns 2+
    problems for one pid (typo + grammar, run_remote_002 chunk4 p00180),
    and the old fail-closed "1 pid = 1 edit" threw away the whole chunk.
    Up to ``max_edits_per_pid`` edits per pid are accepted; the edit over
    the cap is dropped per-edit and reported as a WARNING (third return
    element), never as an error. Fail-closed is preserved for: pid outside
    the chunk, unknown class, original not a substring, invalid JSON.

    The diff-gate (``rewritten == original`` → no-op) is NOT a parse error:
    a no-op edit is structurally valid but worthless, so it is cut per-edit
    by the caller (dropped count), never applied and never a candidate.

    Returns ``(edits, errors, warnings)`` — ``errors`` fail the chunk,
    ``warnings`` are per-edit drops that did NOT fail it.
    """
    errors: list = []
    warnings: list = []
    try:
        parsed = parse_json_response(text)
    except Exception as exc:
        return (), (f"response is not valid JSON: {exc}",), ()
    if not isinstance(parsed, dict) or "edits" not in parsed:
        return (), ("root object has no 'edits' array",), ()
    edits = parsed.get("edits")
    if not isinstance(edits, list):
        return (), ("'edits' is not an array",), ()
    chunk_pid_set = frozenset(chunk_pids)
    out: list = []
    pid_count: dict = {}
    for item in edits:
        if not isinstance(item, dict):
            errors.append("edit entry is not an object")
            continue
        pid = item.get("pid")
        original = item.get("original")
        rewritten = item.get("rewritten")
        reason = item.get("reason")
        klass = item.get("class")
        if not isinstance(pid, str) or not pid:
            errors.append(f"edit has invalid pid {pid!r}")
            continue
        if pid not in chunk_pid_set:
            errors.append(f"edit pid {pid!r} is not in the current chunk")
            continue
        # R-RETRY: duplicate pid is allowed up to the cap. The 11th+ edit
        # of the same pid is dropped per-edit with a WARNING (journal),
        # never a structural error — the chunk stays GOOD.
        if pid_count.get(pid, 0) >= max_edits_per_pid:
            warnings.append(
                f"duplicate edit pid {pid} dropped "
                f"(over MAX_EDITS_PER_PID={max_edits_per_pid})"
            )
            continue
        pid_count[pid] = pid_count.get(pid, 0) + 1
        if not isinstance(original, str) or original == "":
            errors.append(f"pid {pid}: original is missing or not a string")
            continue
        if original not in str(current_by_pid.get(pid, "")):
            errors.append(
                f"pid {pid}: original is not a substring of the current text "
                f"(model must quote the exact fragment verbatim from the "
                f"current Russian text)"
            )
            continue
        if not isinstance(rewritten, str) or not rewritten.strip():
            errors.append(f"pid {pid}: rewritten is missing or not a string")
            continue
        if not isinstance(reason, str) or not reason.strip():
            errors.append(f"pid {pid}: reason is missing or not a string")
            continue
        if not isinstance(klass, str) or klass not in ALL_CLASSES:
            errors.append(
                f"pid {pid}: unknown edit class {klass!r} "
                f"(allowed: {sorted(ALL_CLASSES)})"
            )
            continue
        out.append(
            EditorEdit(
                pid=pid,
                # RV fd7ee8e: strict exact-echo — original and rewritten are
                # preserved VERBATIM (no strip). A leading/trailing-whitespace
                # mismatch in original fails the chunk; an accepted SAFE edit
                # returns the exact rewritten, never a normalized one.
                original=original,
                rewritten=rewritten,
                reason=reason.strip(),
                klass=klass,
            )
        )
    if errors:
        # Fail-closed: a structurally invalid chunk is never partially used.
        return (), tuple(errors), tuple(warnings)
    return tuple(out), (), tuple(warnings)


def route_edits(
    edits: Sequence[EditorEdit],
    *,
    current_by_pid: Mapping[str, str],
    safe_classes: frozenset = frozenset(SAFE_CLASSES),
) -> Tuple[Tuple[Tuple[str, str], ...], Tuple[ReviewCandidate, ...], int, Tuple[str, ...]]:
    """Route parsed edits into (applied, candidates, dropped, warnings).

    * SAFE-classed edit with ``rewritten != original`` (diff-gate) →
      applied ``(pid, new_text)`` where ``new_text`` is the current text
      with the ``original`` FRAGMENT replaced once by ``rewritten``
      (R-FIX2 substring-replace: only the quoted fragment changes, the
      rest of the PID is preserved — run_012 p00010-class, and the
      run_011 p00244-class truncation guard);
    * REVIEW-classed edit → ``ReviewCandidate`` (never auto-applied);
    * any class with ``rewritten == original`` → no-op, dropped (the
      p00095-class false positive the diff-gate cuts).

    R-RETRY (t_8ab8ab35, owner contract 2026-08-13): SAFE edits of the same
    PID apply SEQUENTIALLY — the working text is updated between edits of
    one pid, so a later edit replaces against the ACTUAL current text (two
    different fragments of one pid are both applied, run_remote_002 chunk4
    p00180 typo+grammar). If a later edit's fragment stopped being a
    substring after the earlier edits, it is dropped per-edit with a
    WARNING (never a structural error — the chunk stays GOOD). REVIEW
    candidates go to repair ALL together (CANDIDATE-MERGE already full-
    merges by pid).

    ``current_by_pid`` is the pid→current-text map the parse validated
    against; parse guarantees ``original`` is a substring of the ORIGINAL
    text, so the FIRST replace always finds the fragment (fail-closed at
    parse, applied here).
    """
    applied: list = []
    candidates: list = []
    dropped = 0
    warnings: list = []
    # Working copy: SAFE edits of one pid update it between edits, so a
    # later same-pid edit replaces against the actual current text.
    working: dict = {pid: str(text) for pid, text in current_by_pid.items()}
    for edit in edits:
        if edit.rewritten == edit.original:
            dropped += 1
            continue
        if edit.klass in safe_classes:
            current = working.get(edit.pid, "")
            if edit.original not in current:
                warnings.append(
                    f"pid {edit.pid}: original is no longer a substring after "
                    f"earlier edits — SAFE edit skipped per-edit"
                )
                continue
            new_text = current.replace(edit.original, edit.rewritten, 1)
            working[edit.pid] = new_text
            applied.append((edit.pid, new_text))
        else:
            candidates.append(
                ReviewCandidate(
                    pid=edit.pid,
                    original=edit.original,
                    proposed=edit.rewritten,
                    klass=edit.klass,
                    reason=edit.reason,
                )
            )
    return tuple(applied), tuple(candidates), dropped, tuple(warnings)


# ---------------------------------------------------------------------------
# Evaluator
# ---------------------------------------------------------------------------


class RussianEditorEvaluator:
    """V4.2 R Russian-only editor over ``CompletionBackend`` (transport-neutral).

    Usage::

        evaluator = RussianEditorEvaluator(backend, config=RussianEditorConfig())
        outcome = evaluator(
            chapter_id="0001",
            translation={"p00001": "…", ...},   # RUSSIAN only, no source
        )

    One ``CompletionRequest`` per chunk (``max_output_tokens`` from config,
    temperature 0.0, ``json_object`` schema — never ``request_options``; the
    reasoning budget is a server arg). The model ref resolves to the audit
    (Qwen) role — the editor is the audit model (owner decision, 0 restarts).
    """

    def __init__(
        self,
        backend: CompletionBackend,
        *,
        config: Optional[RussianEditorConfig] = None,
        on_chunk_event: Optional[Callable[[str, Dict[str, Any]], None]] = None,
    ) -> None:
        self._backend = backend
        self._config = config or RussianEditorConfig()
        # Optional per-chunk hook (started/done) so the caller's append-only
        # journal can record chunk causality like the B1 audit does.
        self._on_chunk_event = on_chunk_event

    @property
    def backend(self) -> CompletionBackend:
        return self._backend

    def _emit_chunk_event(self, kind: str, **fields: Any) -> None:
        if self._on_chunk_event is not None:
            try:
                self._on_chunk_event(kind, fields)
            except Exception:  # noqa: BLE001 — a journal hook never breaks R
                LOG.debug("russian_editor on_chunk_event(%r) failed", kind, exc_info=True)

    @staticmethod
    def _write_chunk_artifacts(
        *,
        out_dir: Optional[Path],
        out_base: str,
        chunk_index: int,
        content: str,
        reasoning: str,
    ) -> None:
        """Persist one R chunk's raw response + reasoning (diagnostic trail).

        Mirrors ``ChunkedAudit._write_artifacts``: ``r_editor_chunk{N}_raw.txt``
        / ``r_editor_chunk{N}_reasoning.txt``. Written on EVERY chunk — a
        parse/transport failure then leaves a disk trail (run_011 lesson:
        7/8 R chunks FAILED with no artifacts, diagnosis impossible).
        """
        if out_dir is None:
            return
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / f"{out_base}_chunk{chunk_index}_raw.txt").write_text(
            content, encoding="utf-8"
        )
        (out_dir / f"{out_base}_chunk{chunk_index}_reasoning.txt").write_text(
            reasoning, encoding="utf-8"
        )

    def __call__(
        self,
        *,
        chapter_id: str,
        translation: Mapping[str, str],
        out_dir: Optional[Path] = None,
        out_base: str = "r_editor",
    ) -> RussianEditorOutcome:
        cfg = self._config
        model_ref = audit_model_ref(self._backend)

        if not translation:
            # Fail-closed: an empty input is rejected before any model call.
            raise ValueError(
                f"russian_editor: empty translation for chapter {chapter_id!r} "
                f"— rejected before any model call (never complete with 0 chunks)"
            )

        pairs = [
            TranslationPair(pid=pid, text=text)
            for pid, text in translation.items()
        ]
        chunks = build_editor_chunks(pairs, chunk_size=cfg.chunk_size)
        all_edits: List[EditorEdit] = []
        failed_chunks: List[int] = []
        # Per-chunk parse warnings (over MAX_EDITS_PER_PID drops) — counted
        # separately from route warnings in the outcome.
        chunk_warning_counts: List[int] = []

        for chunk_index, chunk_pairs in enumerate(chunks, start=1):
            chunk_pids = [p.pid for p in chunk_pairs]
            context_pairs = get_editor_overlap(
                pairs, chunk_pairs[0].pid, cfg.overlap_pairs
            )
            prompt = render_russian_editor_prompt(
                chunk_id=f"{chapter_id}/chunk{chunk_index}",
                edit_pairs=chunk_pairs,
                context_pairs=context_pairs,
                chunk_index=chunk_index,
                chunk_total=len(chunks),
                template=cfg.template,
            )
            request = CompletionRequest(
                model_ref=model_ref,
                messages=(Message(role="user", content=prompt),),
                max_output_tokens=cfg.max_tokens,
                temperature=0.0,
                response_schema=JSON_OBJECT_SCHEMA,
                label=cfg.label,
            )
            self._emit_chunk_event(
                "started", chunk=chunk_index, total=len(chunks)
            )
            # R-RETRY (t_8ab8ab35): the R stage is the only phase without
            # retry. Bounded retry (mirrors the re-audit B4 pattern) for
            # TRANSPORT failures and INVALID_JSON/empty bodies — both are
            # transient (run_remote_002 chunk3 empty body, run_remote_001
            # chunk1/6 truncation). Structural errors (pid outside chunk,
            # unknown class, original not a substring) are NOT retried —
            # they are not randomness, fail-closed as-is.
            max_attempts = cfg.retry_max_retries + 1
            chunk_edits: Tuple[EditorEdit, ...] = ()
            chunk_warnings: Tuple[str, ...] = ()
            for attempt in range(max_attempts):
                try:
                    response = self._backend.complete(request)
                except Exception as exc:  # CompletionError + transport failures
                    if attempt < cfg.retry_max_retries:
                        delay = cfg.retry_base_delay_seconds * (2 ** attempt)
                        LOG.warning(
                            "russian_editor chunk %d transport failure (%s) — "
                            "retry %d/%d in %.2fs",
                            chunk_index, type(exc).__name__,
                            attempt + 1, max_attempts, delay,
                        )
                        self._emit_chunk_event(
                            "retry", chunk=chunk_index, attempt=attempt + 1,
                            total=max_attempts,
                            error=f"{type(exc).__name__}: {exc}", delay=delay,
                        )
                        time.sleep(delay)
                        continue
                    LOG.error(
                        "russian_editor chunk %d transport failure (%s): %s",
                        chunk_index, type(exc).__name__, exc,
                    )
                    self._write_chunk_artifacts(
                        out_dir=out_dir, out_base=out_base, chunk_index=chunk_index,
                        content=f"TRANSPORT_ERROR: {type(exc).__name__}: {exc}\n",
                        reasoning="",
                    )
                    failed_chunks.append(chunk_index)
                    self._emit_chunk_event(
                        "done", chunk=chunk_index, status="FAILED",
                        error=f"{type(exc).__name__}: {exc}",
                    )
                    break
                content = response.text or ""
                reasoning = str((response.raw_metadata or {}).get("reasoning") or "")
                # Persist the raw response + reasoning on EVERY attempt — a
                # final parse/transport failure then leaves a disk trail
                # (run_011: 7/8 R chunks FAILED with no artifacts).
                self._write_chunk_artifacts(
                    out_dir=out_dir, out_base=out_base, chunk_index=chunk_index,
                    content=content, reasoning=reasoning,
                )
                edits, errors, warnings = parse_editor_edits(
                    content, chunk_pids, current_by_pid=dict(translation),
                    max_edits_per_pid=cfg.max_edits_per_pid,
                )
                if errors and _is_retryable_json_error(errors):
                    # INVALID_JSON / empty body: the model did not manage to
                    # answer (max_tokens exhausted inside <think>, truncated
                    # body). Retryable, bounded (B4 pattern).
                    if attempt < cfg.retry_max_retries:
                        delay = cfg.retry_base_delay_seconds * (2 ** attempt)
                        LOG.warning(
                            "russian_editor chunk %d invalid JSON (%s) — "
                            "retry %d/%d in %.2fs",
                            chunk_index, "; ".join(errors),
                            attempt + 1, max_attempts, delay,
                        )
                        self._emit_chunk_event(
                            "retry", chunk=chunk_index, attempt=attempt + 1,
                            total=max_attempts,
                            error="; ".join(errors), delay=delay,
                        )
                        time.sleep(delay)
                        continue
                if errors:
                    # Structural errors (pid outside chunk, unknown class,
                    # original not a substring) — NOT retried.
                    LOG.warning(
                        "russian_editor chunk %d invalid (%s) — chunk FAILED",
                        chunk_index, "; ".join(errors),
                    )
                    failed_chunks.append(chunk_index)
                    self._emit_chunk_event(
                        "done", chunk=chunk_index, status="FAILED",
                        error="; ".join(errors),
                    )
                    break
                chunk_edits = edits
                chunk_warnings = warnings
                break
            else:
                continue
            if chunk_index in failed_chunks:
                continue
            all_edits.extend(chunk_edits)
            chunk_warning_counts.append(len(chunk_warnings))
            self._emit_chunk_event(
                "done", chunk=chunk_index, status="GOOD",
                edit_count=len(chunk_edits),
                warning_count=len(chunk_warnings),
            )

        applied, candidates, dropped, route_warnings = route_edits(
            all_edits,
            current_by_pid=dict(translation),
            safe_classes=cfg.safe_classes,
        )
        # Total per-edit warnings: parse-level (over MAX_EDITS_PER_PID
        # drops) + route-level (SAFE fragment no longer a substring after
        # earlier same-pid edits). None of them fail the chunk.
        parse_warning_count = sum(chunk_warning_counts)
        successful = len(chunks) - len(failed_chunks)
        return RussianEditorOutcome(
            schema=RUSSIAN_EDITOR_SCHEMA,
            harness_version=cfg.harness_version,
            prompt_version=cfg.prompt_version,
            model=model_ref,
            chunk_size=cfg.chunk_size,
            overlap_pairs=cfg.overlap_pairs,
            chunk_count=len(chunks),
            successful_chunks=successful,
            failed_chunks=tuple(failed_chunks),
            complete=not failed_chunks,
            edits=tuple(all_edits),
            applied=tuple(applied),
            candidates=tuple(candidates),
            dropped=dropped,
            warning_count=parse_warning_count + len(route_warnings),
        )


__all__ = [
    "RUSSIAN_EDITOR_SCHEMA",
    "RUSSIAN_EDITOR_HARNESS_VERSION",
    "RUSSIAN_EDITOR_PROMPT_VERSION",
    "SAFE_CLASSES",
    "REVIEW_CLASSES",
    "ALL_CLASSES",
    "DEFAULT_CHUNK_SIZE",
    "DEFAULT_OVERLAP_PAIRS",
    "DEFAULT_MAX_TOKENS",
    "MAX_EDITS_PER_PID",
    "DEFAULT_RETRY_MAX_RETRIES",
    "DEFAULT_RETRY_BASE_DELAY_SECONDS",
    "TranslationPair",
    "EditorEdit",
    "ReviewCandidate",
    "RussianEditorConfig",
    "RussianEditorOutcome",
    "build_editor_chunks",
    "get_editor_overlap",
    "parse_editor_edits",
    "route_edits",
    "RussianEditorEvaluator",
]
