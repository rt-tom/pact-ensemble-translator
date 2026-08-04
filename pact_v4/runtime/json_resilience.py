"""JSON resilience for model calls: bounded retry on empty/truncated output.

B4 (post-run_001 refinements, ``docs/plans/V4_B4_JSON_RESILIENCE_TASK_RU.md``):
run_001 exposed two recurring failure modes that are *not* semantic gate
verdicts and must not be treated as one:

  * an **empty** model response (qwen-audit, chunk0011) — the budget was
    exhausted inside the model's ``<think>`` block before any visible
    content;
  * a **truncated** JSON response (repair) — the body stops mid-object /
    mid-string because ``max_tokens`` ran out.

Both already correctly avoided a semantic terminal status ("never accept
truncated JSON"), but a single transient occurrence permanently failed the
unit/debt. This module turns them into a bounded, deterministic retry at the
role-adapter boundary (``BackendQwenAuditEvaluator`` /
``BackendRepairCaller``) so a one-off empty/truncated answer is retried with
exponential backoff before the unit is declared failed / the repair is
declared debt.

Error classification (explicit, per B4 §3):

  * ``EmptyResponseError`` — empty body;
  * ``TruncatedJSONError`` — body that cannot be parsed as JSON (includes
    mid-JSON truncation);
  * transport failures are *not* classified here: they surface as
    ``CompletionError`` (C1 ``OpenCodeError`` is a subclass) and propagate
    immediately — the transport already owns its bounded retry policy, and a
    JSON retry must never re-try a transport failure as if it were a JSON
    problem (B4 §1/§2/§3).

Retry invariants (B4 §4/§5):

  * A retry re-issues the **identical** ``CompletionRequest`` (same prompt,
    same ``max_output_tokens``, same ``model_ref``, same backend), so retry
    never changes cache/resume identity (same prompt/backend -> same
    unit_hash/backend identity). It also preserves the B1 decision to *not*
    set ``request_options`` (``reasoning=0`` stays the B1 baseline — the
    retry must not start adding per-request options; DECISIONS 2026-08-01).
  * ``max_retries`` defaults to 2 (three attempts total) and is overridable
    per-adapter via ``JsonRetryPolicy`` and the runtime-config build hooks.
  * When the budget is exhausted the last error is re-raised: the audit unit
    is recorded failed / the repair is recorded as debt — never a semantic
    verdict.
"""
from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from typing import Callable

LOG = logging.getLogger(__name__)


class EmptyResponseError(ValueError):
    """The model returned an empty body (likely max_tokens exhausted inside
    a ``<think>`` block before any visible content). Retryable by B4 policy."""


class TruncatedJSONError(ValueError):
    """The model returned a body that is not complete JSON (mid-object /
    mid-string truncation or malformed JSON). Retryable by B4 policy."""


@dataclass(frozen=True)
class JsonRetryPolicy:
    """Bounded retry policy for empty/truncated JSON (B4 §5).

    ``max_retries`` is the number of *additional attempts after the first
    call* (default 2 -> three calls total). ``base_delay_seconds`` is the
    exponential-backoff base: the k-th retry waits ``base * 2**k`` seconds.
    """

    max_retries: int = 2
    base_delay_seconds: float = 1.0

    def __post_init__(self) -> None:
        if int(self.max_retries) != self.max_retries or self.max_retries < 0:
            raise ValueError("JsonRetryPolicy: max_retries must be a non-negative int")
        if self.base_delay_seconds < 0:
            raise ValueError(
                "JsonRetryPolicy: base_delay_seconds must be >= 0"
            )

    def delay_for(self, attempt: int) -> float:
        """Backoff delay before the attempt-th retry (0-based)."""
        return self.base_delay_seconds * (2 ** int(attempt))


def classify_response_text(raw: str) -> None:
    """Raise the matching retryable error for an empty/truncated body.

    Returns ``None`` when the body is non-empty and parseable JSON (even if
    it is semantically the wrong shape — that is a downstream validation
    concern, not a retry trigger). Raises ``EmptyResponseError`` /
    ``TruncatedJSONError`` otherwise.
    """
    if not raw.strip():
        raise EmptyResponseError(
            "empty response body (likely max_tokens exhausted before any "
            "visible content)"
        )
    try:
        json.loads(raw)
    except json.JSONDecodeError as exc:
        raise TruncatedJSONError(
            f"response is not complete JSON (truncated or malformed): {exc}"
        ) from exc


_RETRYABLE = (EmptyResponseError, TruncatedJSONError)


def retry_json_call(
    fn: Callable[[], str],
    policy: JsonRetryPolicy,
    *,
    label: str,
) -> str:
    """Call ``fn()`` (one model call returning raw text) with bounded retry.

    ``fn`` re-issues the identical request on every attempt. A transport
    failure (``CompletionError`` or any subclass such as C1 ``OpenCodeError``)
    raised by ``fn`` propagates immediately — it is never retried here as a
    JSON problem. An empty/truncated body is retried up to ``policy.max_retries``
    times with exponential backoff; when the budget is exhausted the last
    ``EmptyResponseError`` / ``TruncatedJSONError`` is re-raised so the caller
    records a failed unit (audit) or debt (repair) — never a semantic verdict.
    """
    attempt = 0
    while True:
        raw = fn()
        try:
            classify_response_text(raw)
        except _RETRYABLE as exc:  # type: ignore[arg-type]
            if attempt >= policy.max_retries:
                raise
            delay = policy.delay_for(attempt)
            LOG.warning(
                "%s: retryable JSON failure (%s) on attempt %d/%d; "
                "retrying in %.2fs",
                label, type(exc).__name__, attempt + 1, policy.max_retries, delay,
            )
            time.sleep(delay)
            attempt += 1
            continue
        return raw


__all__ = [
    "EmptyResponseError",
    "TruncatedJSONError",
    "JsonRetryPolicy",
    "classify_response_text",
    "retry_json_call",
]
