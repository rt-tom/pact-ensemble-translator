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

REPAIR-RECEIVER (t_b590c24f, run_remote_007): whole-chapter generation keeps
hitting a NEW JSON defect class on long outputs (pid-colon PR #178, then the
string-aware variant, then p00087's typographic „ closed by an ASCII ``"``).
Point repairs per error class are the wrong path. The single tolerant
receiver ``extract_pid_pairs`` splits a whole-chapter body on its top-level
``"p\\d{5}"`` keys and is robust to ANY defect (pid-colon, ASCII quote inside
a value, truncation, missing commas, garbage); ``parse_json_response`` tries
it after the fences/prose strip. It is fail-closed: a dict is returned only
when coverage >= ``min_coverage`` (90%) and every value is clean; otherwise
the unchanged ``TruncatedJSONError`` -> bounded retry path runs — a damaged
response is never accepted. ``repair_pid_colon_comma`` (PR #178) is removed;
its logic is absorbed by the extractor.

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
from typing import Any, Callable, Optional, Sequence

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


def _matching_brace_end(text: str, start: int) -> Optional[int]:
    """Return the index of the ``}`` matching the ``{`` at ``start``.

    String-aware (braces inside string literals and escaped quotes never
    unbalance the scan). ``None`` when no matching brace exists — the body
    is truncated mid-object or the brace sits inside a string.
    """
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
                    return i
    return None  # unbalanced -> truncated


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
    end = _matching_brace_end(text, start)
    if end is None:
        return None
    return text[start : end + 1]


def extract_json_blocks(text: str) -> Tuple[str, ...]:
    """Extract EVERY balanced ``{...}`` block from ``text`` (string-aware).

    REPAIR-ROBUST (card t_b6fd6cbd): unlike ``_first_balanced_json_block``
    this returns all balanced blocks, so a TRUNCATED outer object (e.g. the
    model dropped the final ``}`` of ``{"results": [...]}``) still yields
    its complete inner record objects. Blocks are found by scanning for a
    ``{`` and matching its ``}`` (string-aware); when the outer object is
    unbalanced the scan continues past it to the inner balanced blocks.
    """
    blocks: list[str] = []
    i = 0
    while True:
        start = text.find("{", i)
        if start == -1:
            break
        end = _matching_brace_end(text, start)
        if end is None:
            # Unbalanced from here (truncated outer object) — skip this
            # brace and keep scanning so inner balanced blocks are still
            # recovered.
            i = start + 1
            continue
        blocks.append(text[start : end + 1])
        i = end + 1
    return tuple(blocks)


_PID_KEY_RE = re.compile(r'"p(\d{5})"')


def _unescape_json_string(value: str) -> str:
    """Unescape JSON string escapes in a value slice extracted without
    ``json.loads`` (the tolerant receiver path): ``\\"``, ``\\\\``, ``\\n``,
    ``\\t``, ``\\r``, ``\\uXXXX``. Unknown escapes are kept verbatim.
    """
    out: list[str] = []
    i, n = 0, len(value)
    while i < n:
        ch = value[i]
        if ch == "\\" and i + 1 < n:
            nxt = value[i + 1]
            if nxt == '"':
                out.append('"')
                i += 2
                continue
            if nxt == "\\":
                out.append("\\")
                i += 2
                continue
            if nxt == "n":
                out.append("\n")
                i += 2
                continue
            if nxt == "t":
                out.append("\t")
                i += 2
                continue
            if nxt == "r":
                out.append("\r")
                i += 2
                continue
            if nxt == "u" and i + 5 < n:
                try:
                    out.append(chr(int(value[i + 2 : i + 6], 16)))
                    i += 6
                    continue
                except ValueError:
                    pass
        out.append(ch)
        i += 1
    return "".join(out)


# Typographic quote pairs used in Russian prose: «…» / „…" / “…” / ‘…’.
# An ASCII `"` may close a typographic opener (the p00087 defect: „…"
# where the model typed the closer as ASCII). This is a COARSE balance
# check for the tolerant receiver — it only needs to flag clearly broken
# values, not to validate typography.
_TYPO_OPEN = {"«", "„", "“"}
_TYPO_CLOSE = {"»", "”", "’"}


def _quotes_balanced(value: str) -> bool:
    """Coarse quote-balance check for an extracted value.

    Escape-aware: a backslash escape (``\\"``, ``\\\\``, ``\\n``, …) skips the
    next char, so a legitimately escaped interior quote never counts as a
    structural quote — the F1 embedded literal ``\\"p12345\\", \\"`` (escaped)
    is balanced by construction. Typographic openers (« „ “) must be closed
    by a typographic closer (» ” ’) or by an ASCII `"`; a lone ASCII quote
    without an open typographic partner toggles its own depth (a pair of
    ASCII quotes balances). Returns ``True`` when no opener is left unclosed
    and no ASCII quote is left dangling.
    """
    depth = 0
    i, n = 0, len(value)
    while i < n:
        ch = value[i]
        if ch == "\\":
            i += 2  # skip the escape sequence (\" \\ \n \t \r \uXXXX …)
            continue
        if ch in _TYPO_OPEN:
            depth += 1
        elif ch in _TYPO_CLOSE:
            if depth:
                depth -= 1
        elif ch == '"':
            if depth:
                depth -= 1
            else:
                depth += 1
        i += 1
    return depth == 0


def _clean_pid_value(segment: str, *, is_last: bool) -> Optional[str]:
    """Extract and clean one PID value from the raw slice between two keys.

    ``segment`` is ``raw[key_end : next_key_start]`` — it begins with the
    key→value separator (``: "`` normally, ``, "`` in the pid-colon model
    error) and ends at the next key's opening quote (or at the end of the
    response for the last key). Cleaning strips the leading separator and
    the opening quote, then cuts at the LAST quote (the value's closing
    quote; any trailing ``,``/``}``/garbage between values is dropped).
    The p00087 defect — a typographic „ opened inside the value and closed
    with an ASCII ``"`` — leaves the interior quote INTACT (it is not the
    last quote).

    For the LAST key, a missing closing quote is tolerated (the response
    was cut off mid-value at the end — the tail is accepted; see the
    REPAIR-RECEIVER contract). For any other key an unclosed value is
    suspicious.

    Returns the unescaped value, or ``None`` when the slice is
    suspicious (fail-closed): empty value, a quoted ``"p\\d{5}"`` trace of
    a following key inside the value, or unbalanced quotes.
    """
    seg = segment.strip()
    if seg.startswith((":", ",")):
        seg = seg[1:].lstrip()
    if not seg.startswith('"'):
        return None  # value is not a quoted string
    seg = seg[1:]  # drop the opening quote
    idx = seg.rfind('"')
    if idx == -1:
        if not is_last:
            return None  # unclosed value in the middle — suspicious
        value = seg  # tail of the response — accepted for the last key
    else:
        value = seg[:idx]
    value = value.strip()
    if not value:
        return None  # empty value — suspicious
    if _PID_KEY_RE.search(value):
        # A quoted p\d{5} trace inside a value means the split boundary is
        # wrong (a key form the model embedded in text) — fail-closed.
        # Checked on the RAW slice (pre-unescape): a legitimately escaped
        # literal like `\"p12345\"` contains a backslash between the digits
        # and the closing quote, so it does NOT match `"p\d{5}"` here and
        # survives verbatim (F1 t_0626267d).
        return None
    if not _quotes_balanced(value):
        # Unbalanced quotes — suspicious. Escape-aware and typographic-aware
        # on the RAW slice: the p00087 defect („…" with an ASCII closer)
        # balances, and escaped interior quotes are balanced by construction.
        return None
    return _unescape_json_string(value)


def extract_pid_pairs(
    raw: str,
    *,
    expected_pids: Optional[Sequence[str]] = None,
    min_coverage: float = 0.9,
) -> Optional[dict]:
    """REPAIR-RECEIVER: string-aware pair extractor for whole-chapter bodies.

    One tolerant receiver replaces the growing stack of point repairs
    (pid-colon, string-aware variants, …). It splits the raw response on
    every top-level ``"p\\d{5}"`` key and takes each value as the text up
    to the next key, so it is robust to ANY defect the model can emit on a
    long pid-keyed JSON: the pid-colon ``, "`` separator, an ASCII quote
    inside a value (run_remote_007 p00087 „…"), truncation mid-object,
    missing commas, and garbage around/between values.

    Fail-closed success criteria (all must hold, else ``None``):

      * the response's FIRST key (right after ``{``) is a PID key — the
        extractor is ONLY for whole-chapter top-level pid-keyed objects;
        R / audit / repair / re-audit bodies (``{edits: [...]}`` /
        ``{issues: [...]}``, arrays) are refused here and keep the
        existing fences/prose path untouched;
      * keys are unique (no duplicate PID);
      * every value is non-empty, quote-balanced (typographic-aware) and
        free of a quoted ``"p\\d{5}"`` next-key trace;
      * coverage ``len(extracted) / expected`` >= ``min_coverage``, where
        ``expected`` is ``len(expected_pids)`` when the caller knows the
        contract PID set (whole-chapter / chunk generation) and otherwise
        the number of keys found in the text (the generic
        ``parse_json_response`` path). Below 90% the body is honestly
        truncated and must go through the SAME fail-closed path
        (``TruncatedJSONError`` -> bounded retry) — a damaged response is
        never accepted.

    Returns ``{pid: value}`` in source order, or ``None`` on suspicion.
    """
    if not 0 < min_coverage <= 1:
        raise ValueError("extract_pid_pairs: min_coverage must be in (0, 1]")
    text = raw.lstrip("\ufeff").lstrip()
    brace = text.find("{")
    if brace == -1:
        return None
    first = _PID_KEY_RE.search(text, brace)
    if first is None:
        return None
    if text[brace + 1 : first.start()].strip():
        # The object's first key is not a PID key (e.g. {"edits": [...]},
        # {"issues": [...]}) — not a whole-chapter pid-keyed body.
        return None
    matches = list(_PID_KEY_RE.finditer(text))
    if len({m.group(1) for m in matches}) != len(matches):
        return None  # duplicate PID keys — suspicious
    result: dict = {}
    for i, m in enumerate(matches):
        start = m.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        value = _clean_pid_value(text[start:end], is_last=(i + 1 == len(matches)))
        if value is None:
            return None
        result["p" + m.group(1)] = value
    expected = len(expected_pids) if expected_pids is not None else len(matches)
    if expected == 0:
        return None
    if len(result) / expected < min_coverage:
        return None  # honest truncation — fail-closed
    return result


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
    3. if the whole body is not directly JSON, the REPAIR-RECEIVER pair
       extractor (``extract_pid_pairs``) is tried — ONLY for whole-chapter
       top-level pid-keyed bodies (first key right after ``{`` is a
       ``"p\\d{5}"``). It repairs ANY defect the model can emit on a long
       pid-keyed object (pid-colon ``, "``, an ASCII quote inside a value
       like run_remote_007 p00087, truncation, missing commas, garbage)
       and returns the pairs when coverage >= 90% of the keys found;
    4. if the body is STILL not JSON, the FIRST balanced ``{...}`` block
       is extracted (string-aware, so braces inside strings never
       unbalance it) — this keeps the fences/prose path for R / audit /
       repair / re-audit bodies untouched (they have no top-level pid
       keys, so step 3 refuses them);
    5. ``json.loads``; on failure a ``TruncatedJSONError`` (a
       ``ValueError``) with a clear message is raised — fail-closed, the
       retry zone stays unchanged (broken/truncated JSON is NOT repaired
       beyond the pair extractor, it is retried by B4).

    Raises:
      * ``EmptyResponseError`` — empty body (B4 retryable);
      * ``TruncatedJSONError`` — not complete JSON even after
        fence/prose stripping and the pair extractor (B4 retryable);
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
        # REPAIR-RECEIVER (t_b590c24f, run_remote_007): DeepSeek on a long
        # whole-chapter output emits a THIRD class of JSON defect — a
        # typographic „ inside a value closed with an ASCII `"` (p00087:
        # `«Когда я думаю о „побеге из дома", ...»`) that breaks
        # json.loads. Point repairs per error class are the wrong path
        # (pid-colon PR #178, string-aware variant, …); the single
        # tolerant receiver ``extract_pid_pairs`` splits the text on the
        # top-level ``"p\\d{5}"`` keys and is robust to ANY of them. It is
        # fail-closed: a dict is returned only when every value is clean
        # and coverage >= min_coverage; a suspicious body returns None and
        # falls through to the unchanged TruncatedJSONError path (bounded
        # retry) — a damaged response is never accepted.
        extracted = extract_pid_pairs(cleaned)
        parsed = None
        if extracted is not None:
            LOG.warning(
                "json repair: pid-pair extractor recovered %d pair(s)",
                len(extracted),
            )
            parsed = extracted
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
    "extract_json_blocks",
    "extract_pid_pairs",
    "parse_json_response",
    "retry_json_call",
]
