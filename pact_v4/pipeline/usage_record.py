"""Write-only, append-only per-call remote-usage artifact (V4 D1).

Card: ``docs/plans/V4_D1_USAGE_RECORD_TASK_RU.md`` (owner decision
2026-08-04: usage-record is mandatory in D1).

The strict driver appends one NDJSON line per remote model call into
``out_dir/usage.ndjson`` (success **and** failed calls), so the owner can
see, after a remote chapter run, how many tokens went in/out through
OpenCode, through which models, and at what read/generation speed. The
record's ``runtime.remote_calls`` aggregate stays as-is; this artifact only
adds per-call detail. Diagnostics only:

* nothing here is read back by the pipeline (``usage.ndjson`` is never used
  in any pipeline decision, resume, cache-identity, journal schema or
  terminal-status logic);
* a write failure (e.g. disk full) disables the artifact and never breaks
  the run — usage is a diagnostic, not a gate.

Crash-safe in the journal's sense: append-only, flushed per line, and a
partial trailing line (crash mid-write) is tolerated by readers, which skip
malformed lines. Resume-safe: before the first resumed append the writer
isolates a crash-left partial tail with a trailing newline (completed
records are never rewritten) and durably de-duplicates already journaled
call identities (``session_id`` + ``request_id``), so re-pushing a journaled
call never adds a second row; calls without an identity are always written.

Line vocabulary (each line carries ``schema``, ``ts`` and per-call fields):

* ``label`` — role (generator / fidelity_reviewer / russian_selector /
  qwen_audit / gemma_audit / repair / formatting);
* ``model_ref`` (provider/model), ``provider``, ``model``;
* usage keys the provider actually reported (``input_tokens``,
  ``output_tokens``, ``reasoning_tokens``, ``cached_input_tokens``,
  ``cached_write_tokens``, ``reported_cost``) — only present when the
  provider reported them (plan §9.3: never invented);
* ``wall_seconds``, ``request_id``, ``session_id``, ``finish_reason``,
  ``retry_count``, ``error_class`` (failed calls only).

Local llama-server calls are never written here: the runner only feeds
``EVENT_KIND_REMOTE_CALL`` events, so local runs produce no ``usage.ndjson``
(their lifecycle stays in ``local_lifecycle``).
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Optional

from pact_v4.runtime.runtime_coordinator import EVENT_KIND_REMOTE_CALL

LOG = logging.getLogger(__name__)

USAGE_SCHEMA = "pact-v4-usage/ndjson/v1"
USAGE_FILENAME = "usage.ndjson"

# Usage keys the writer copies from an event's ``usage`` mapping, in order.
# Keys are included ONLY when the provider reported them (plan §9.3).
USAGE_KEYS = (
    "input_tokens",
    "output_tokens",
    "reasoning_tokens",
    "cached_input_tokens",
    "cached_write_tokens",
    "reported_cost",
)


def _split_model_ref(model_ref: str) -> tuple[str, str]:
    """Tolerant ``provider/model`` split (local refs have no slash)."""
    provider, sep, model = str(model_ref or "").partition("/")
    if not sep:
        return "", str(model_ref or "")
    return provider, model


class UsageRecordWriter:
    """Append-only NDJSON usage writer (write-only, diagnostics only).

    One line per remote call, flushed immediately (journal-style
    crash-safety). Open once in append mode so a resumed run keeps writing
    to the same cumulative file. A write failure disables the writer and is
    logged, never raised — usage must never break a run.

    Resume safety (D1 review HIGH-1/HIGH-2):

    * **Framing** — if a crash left a partial trailing line (no final
      newline), a fresh resume writer appends a newline *before* its first
      record so the resumed append starts on a clean line. Completed
      records are never rewritten; the partial tail simply stays isolated
      on its own (malformed) line, which readers skip.
    * **Dedup** — the writer loads the identities of already journaled rows
      (``session_id`` + ``request_id``, when both are present) on first
      open and skips a re-pushed call whose identity is already journaled,
      so a duplicate push/append never duplicates a row. Calls without an
      identity (either id missing — e.g. local backend) are always written.
    """

    def __init__(
        self,
        out_dir: Path,
        *,
        now: Optional[Callable[[], datetime]] = None,
    ) -> None:
        self.path = Path(out_dir) / USAGE_FILENAME
        self._now = now or (lambda: datetime.now(timezone.utc))
        self._handle: Optional[Any] = None
        self._disabled = False
        # Durable dedup: identities of every row already journaled in the
        # file at the time this writer instance opened it. Filled lazily on
        # the first write (``_load_journaled_identities``).
        self._journaled: Optional[set[tuple[str, str]]] = None
        # Set once the malformed-tail framing fix has been applied for this
        # writer instance (idempotent across re-opens).
        self._framing_fixed = False

    def _ensure_open(self) -> None:
        if self._handle is None:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self._repair_trailing_framing()
            self._handle = open(self.path, "a", encoding="utf-8")

    def _repair_trailing_framing(self) -> None:
        """Crash-safe resume framing (D1 review HIGH-1).

        If the existing file is non-empty and does not end with a newline, a
        crash left a partial trailing line. Appending the next record
        directly would glue it onto that partial line, producing one
        combined malformed line that readers skip — silently losing the
        first post-resume record. Fix: append a newline to isolate the
        partial tail *before* the first resumed append. Completed records
        are never touched.
        """
        if self._framing_fixed or not self.path.exists():
            return
        self._framing_fixed = True
        try:
            data = self.path.read_bytes()
        except OSError:
            # Unreadable journal: leave framing as-is; the writer still
            # appends (diagnostics never break the run).
            return
        if data and not data.endswith(b"\n"):
            try:
                with open(self.path, "ab") as handle:
                    handle.write(b"\n")
            except OSError:
                LOG.warning(
                    "Usage record framing repair failed (%s); appending anyway",
                    self.path,
                )

    @staticmethod
    def _row_identity(row: Mapping[str, Any]) -> Optional[tuple[str, str]]:
        """Identity key for durable dedup, or None when the row has none.

        Uses the provider call identity documented in the artifact:
        ``session_id`` + ``request_id``. A row missing either id is treated
        as identity-less (always written, never deduped) — the local backend
        reports ``None`` for both, and distinct calls must stay distinct
        (D1 review HIGH-2: "retaining distinct calls when identity is
        absent").
        """
        session_id = row.get("session_id")
        request_id = row.get("request_id")
        if not session_id or not request_id:
            return None
        return (str(session_id), str(request_id))

    def _load_journaled_identities(self) -> None:
        """Populate the seen-identity set from rows already in the file.

        Runs once per writer instance, on the first write. Uses the same
        tolerant parse as readers (malformed / partial trailing lines are
        skipped), so a crash-corrupted tail never breaks dedup.
        """
        if self._journaled is not None:
            return
        journaled: set[tuple[str, str]] = set()
        if not self.path.exists():
            self._journaled = journaled
            return
        try:
            raw = self.path.read_bytes()
        except OSError:
            raw = b""
        for raw_line in raw.splitlines():
            if not raw_line.strip():
                continue
            try:
                line = raw_line.decode("utf-8")
            except UnicodeDecodeError:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(row, Mapping):
                continue
            identity = self._row_identity(row)
            if identity is not None:
                journaled.add(identity)
        self._journaled = journaled

    def write_call(self, record: Any) -> None:
        """Append one line for a completed remote call.

        ``record`` is a ``BackendCallRecord`` (from the backend's per-call
        usage sink) or a ``BackendEvent`` with ``kind ==
        EVENT_KIND_REMOTE_CALL`` (coordinator path). Local switch events /
        local lifecycle records are ignored so local runs never write usage.

        Durable dedup: a call whose ``session_id`` + ``request_id`` identity
        is already journaled is skipped (no duplicate row). Identity-less
        calls are always written.
        """
        if self._disabled:
            return
        if isinstance(record, Mapping):
            # BackendCallRecord is a dataclass, not a Mapping; this branch
            # is a defensive guard for plain-dict callers.
            fields = record
            kind = fields.get("kind")
            if kind is not None and kind != EVENT_KIND_REMOTE_CALL:
                return
            label = fields.get("label")
            model_ref = fields.get("model_ref") or ""
            usage = fields.get("usage") if isinstance(fields.get("usage"), Mapping) else {}
            error_class = fields.get("error_class")
            identity = self._row_identity(fields)
        else:
            kind = getattr(record, "kind", None)
            if kind is not None and kind != EVENT_KIND_REMOTE_CALL:
                return
            label = getattr(record, "label", None)
            model_ref = getattr(record, "model_ref", None) or ""
            usage = getattr(record, "usage", None)
            usage = usage if isinstance(usage, Mapping) else {}
            error_class = getattr(record, "error_class", None)
            if error_class is None:
                raw = getattr(record, "raw_metadata", None)
                if isinstance(raw, Mapping):
                    error_class = raw.get("error_class")
            identity = self._row_identity(
                {
                    "session_id": getattr(record, "session_id", None),
                    "request_id": getattr(record, "request_id", None),
                }
            )
        self._load_journaled_identities()
        if identity is not None and self._journaled is not None \
                and identity in self._journaled:
            # Durable dedup: already journaled by this or a previous writer
            # instance (resume) — never duplicate the row.
            return
        provider, model = _split_model_ref(model_ref)
        line: dict[str, Any] = {
            "schema": USAGE_SCHEMA,
            "ts": self._now().isoformat(timespec="seconds"),
            "label": label,
            "model_ref": model_ref,
            "provider": provider,
            "model": model,
        }
        for key in USAGE_KEYS:
            if key in usage and usage.get(key) is not None:
                line[key] = usage[key]
        line.update({
            "wall_seconds": (
                getattr(record, "wall_seconds", 0.0)
                if not isinstance(record, Mapping)
                else record.get("wall_seconds", 0.0)
            ),
            "request_id": (
                getattr(record, "request_id", None)
                if not isinstance(record, Mapping)
                else record.get("request_id")
            ),
            "session_id": (
                getattr(record, "session_id", None)
                if not isinstance(record, Mapping)
                else record.get("session_id")
            ),
            "finish_reason": (
                getattr(record, "finish_reason", None)
                if not isinstance(record, Mapping)
                else record.get("finish_reason")
            ),
            "retry_count": (
                getattr(record, "retry_count", 0)
                if not isinstance(record, Mapping)
                else record.get("retry_count", 0)
            ),
        })
        if error_class is not None:
            line["error_class"] = error_class
        try:
            self._ensure_open()
            assert self._handle is not None
            self._handle.write(json.dumps(line, ensure_ascii=False) + "\n")
            self._handle.flush()
        except OSError as exc:  # disk full / permission -- diagnostics, never a gate
            LOG.warning("Usage record write failed (%s); disabling usage artifact", exc)
            self._disabled = True
            return
        if identity is not None:
            # Durable dedup: remember the just-written identity so a repeated
            # push of the same call within this writer instance is also
            # skipped (not only across resume instances).
            assert self._journaled is not None
            self._journaled.add(identity)

    def close(self) -> None:
        if self._handle is not None:
            try:
                self._handle.close()
            except OSError:  # noqa: S110 -- best-effort close
                pass
            finally:
                self._handle = None
