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
    same ``max_output_tokens``, same ``model_ref``, same backend, same
    ``request_options`` — including any pinned V4.1 generation reasoning
    budget), so retry never changes cache/resume identity (same
    prompt/backend -> same unit_hash/backend identity) and never switches
    the reasoning budget mid-request (B1 baseline: no option is ever
    *added* by a retry; DECISIONS 2026-08-01).
  * ``max_retries`` defaults to 2 (three attempts total) and is overridable
    per-adapter via ``JsonRetryPolicy`` and the runtime-config build hooks.
  * When the budget is exhausted the last error is re-raised: the audit unit
    is recorded failed / the repair is recorded as debt — never a semantic
    verdict.
"""
from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass
from typing import Any, Callable, Optional, Tuple

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
        if (
            isinstance(self.max_retries, bool)
            or not isinstance(self.max_retries, int)
            or self.max_retries < 0
        ):
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

    Returns ``None`` when the body is non-empty and parseable JSON after
    stripping markdown fences / BOM / surrounding prose (even if it is
    semantically the wrong shape — that is a downstream validation
    concern, not a retry trigger). Raises ``EmptyResponseError`` /
    ``TruncatedJSONError`` otherwise.
    """
    if not raw.strip():
        raise EmptyResponseError(
            "empty response body (likely max_tokens exhausted before any "
            "visible content)"
        )
    try:
        parse_json_response(raw)
    except TruncatedJSONError:
        raise
    except ValueError:
        # Valid JSON of the wrong shape (e.g. a bare string) is NOT a
        # retry trigger (B4: retry only empty/truncated JSON).
        return


def _strip_markdown_fences(text: str) -> str:
    """Remove one markdown code-fence wrapper (`````json ... `````) if present.

    Mirrors the entity extractor's historical ``_strip_fences``: a leading
    fence line (````` or `````json) and a trailing fence line are cut; the
    body between them is returned trimmed. Non-fenced text is returned
    unchanged.
    """
    text = text.strip()
    if text.startswith("```"):
        lines = text.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip().startswith("```"):
            lines = lines[:-1]
        return "\n".join(lines).strip()
    return text


def _first_balanced_json_block(text: str) -> Optional[str]:
    """Return the first balanced ``{...}`` block in ``text``, or ``None``.

    String-aware: braces inside a JSON string literal (and escaped quotes)
    never unbalance the scan, so prose like ``Here is the JSON: {...}``
    yields the object. Returns ``None`` when no balanced block exists —
    i.e. the body is truncated mid-object (the B4 retry zone) or contains
    no object at all.
    """
    start = text.find("{")
    if start == -1:
        return None
    depth = 0
    in_string = False
    escaped = False
    for i in range(start, len(text)):
        ch = text[i]
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
        else:
            if ch == '"':
                in_string = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    return text[start : i + 1]
    return None  # unbalanced -> truncated


_PID_COLON_COMMA_RE = re.compile(r'"p(\d{5})", "')


def repair_pid_colon_comma(text: str) -> Tuple[str, int]:
    """Deterministic repair for the whole-chapter pid-colon model error.

    run_remote_004 (t_34ceca50): DeepSeek on a long whole-chapter output
    occasionally emits a COMMA instead of a COLON after a PID key —
    ``"p00082", "`` instead of ``"p00082": "``. The comma form is never
    valid JSON inside an object (a bare string cannot be followed by `,`
    outside an array), so the pattern is unique to the model error and
    every occurrence is substituted deterministically.

    Returns ``(repaired_text, n_substitutions)``; the input is returned
    unchanged when no occurrence matches. The caller re-parses and keeps
    its own fail-closed path: a body that still does not parse after the
    substitution is rejected exactly as before (real damage is never
    masked).
    """
    return _PID_COLON_COMMA_RE.subn(r'"p\1": "', text)


def parse_json_response(text: str) -> dict:
    """Parse a model response into a JSON dict, tolerating the formatting
    noise models wrap their answers in.

    RESILIENCE (t_406fc48c, run_remote_001): R / repair / re-audit used
    bare ``json.loads`` and failed when the model wrapped the payload in
    markdown fences (`````json ... `````), a BOM, or prose
    (``Here is the JSON: {...}``). This is the single tolerant parse for
    every phase:

    1. BOM + surrounding whitespace are stripped;
    2. markdown fences are removed;
    3. if the whole body is not directly JSON, a deterministic
       pid-colon repair is applied first (t_34ceca50, run_remote_004):
       a COMMA after a PID key — ``"p00082", "`` instead of ``"p00082": "``
       — is substituted back to a colon (ALL occurrences; the comma form is
       never valid JSON inside an object, so the pattern is unique to the
       model error). A successful parse after the substitution is returned
       with a WARNING logging the substitution count;
    4. if the body is STILL not JSON, the FIRST balanced ``{...}`` block
       is extracted (string-aware, so braces inside strings never
       unbalance it);
    5. ``json.loads``; on failure a ``TruncatedJSONError`` (a
       ``ValueError``) with a clear message is raised — fail-closed, the
       retry zone stays unchanged (broken/truncated JSON is NOT repaired
       here beyond the single deterministic pid-colon substitution, it is
       retried by B4).

    Raises:
      * ``EmptyResponseError`` — empty body (B4 retryable);
      * ``TruncatedJSONError`` — not complete JSON even after
        fence/prose stripping (B4 retryable);
      * ``ValueError`` — valid JSON but not an object (not a retry
        trigger; wrong shape is a downstream validation concern).
    """
    if not text or not text.strip():
        raise EmptyResponseError(
            "empty response body (likely max_tokens exhausted before any "
            "visible content)"
        )
    cleaned = _strip_markdown_fences(text.strip().lstrip("\ufeff"))
    try:
        parsed = json.loads(cleaned)
    except json.JSONDecodeError:
        # JSON-REPAIR (t_34ceca50, run_remote_004): DeepSeek on a long
        # whole-chapter output occasionally emits `"p00082", "` (COMMA)
        # instead of `"p00082": "` (COLON) after a PID key. The comma form
        # is never valid JSON inside an object (a bare string cannot be
        # followed by `,` outside an array), so the pattern is unique to
        # the model error. Deterministically substitute ALL occurrences
        # and re-parse; a successful parse is returned with a WARNING so
        # the model error rate stays visible. Fail-closed is preserved:
        # if the body still does not parse, the existing
        # _first_balanced_json_block path runs unchanged (TruncatedJSONError).
        repaired, n_subs = repair_pid_colon_comma(cleaned)
        parsed = None
        if n_subs:
            try:
                parsed = json.loads(repaired)
            except json.JSONDecodeError:
                parsed = None
            else:
                LOG.warning("json repair: %d substitution(s) pid-colon", n_subs)
        if parsed is None:
            block = _first_balanced_json_block(cleaned)
            if block is None:
                raise TruncatedJSONError(
                    "response is not complete JSON (no balanced {...} object "
                    "found after stripping fences/prose — truncated or "
                    "missing)"
                )
            try:
                parsed = json.loads(block)
            except json.JSONDecodeError as exc:
                raise TruncatedJSONError(
                    f"response is not complete JSON: {exc}"
                ) from exc
    if not isinstance(parsed, dict):
        raise ValueError(
            f"response is valid JSON but not an object: "
            f"{type(parsed).__name__}"
        )
    return parsed


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

    Logging: a retry-trigger event (a transient, self-healing blip) is logged
    at ``INFO`` so a healthy run is not noisy; only budget exhaustion is
    logged at ``WARNING`` (the raised error also carries the detail into the
    failed-unit / debt record).
    """
    attempt = 0
    while True:
        raw = fn()
        try:
            classify_response_text(raw)
        except _RETRYABLE as exc:  # type: ignore[arg-type]
            if attempt >= policy.max_retries:
                LOG.warning(
                    "%s: JSON retry budget exhausted (%s) after %d attempts; "
                    "recording failed unit / debt",
                    label, type(exc).__name__, policy.max_retries + 1,
                )
                raise
            delay = policy.delay_for(attempt)
            LOG.info(
                "%s: transient %s on attempt %d/%d; retrying in %.2fs",
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
    "parse_json_response",
    "retry_json_call",
]
