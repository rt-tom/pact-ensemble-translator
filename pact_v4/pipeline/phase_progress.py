"""Write-only, append-only run-progress artifact for the strict chapter driver.

Card: ``docs/plans/V4_PHASE12_RUN_PROGRESS_TRACKER_TASK_RU.md``.

The strict driver emits one NDJSON line per event into
``out_dir/phase_progress.ndjson`` (``started`` + ``done`` pairs) so a
read-only CLI (``pact_full_pipeline_runner_v1.v4_phase_progress``) can show
where a run is: which phase, which chunks/audit units/regions are in flight,
and what is left. Diagnostics only:

* nothing here is read back by the pipeline (``phase_progress.ndjson`` is
  never used in any pipeline decision, resume, cache-identity, journal schema
  or terminal-status logic);
* a write failure (e.g. disk full) disables the artifact and never breaks the
  run — progress is a diagnostic, not a gate.

The artifact is crash-safe in the journal's sense: it is append-only and
flushed per event, so a crash leaves a cleanly-terminated prefix, and a
partial trailing line (a crash mid-write) is tolerated by readers, which
skip malformed lines.

Event vocabulary (each event carries ``schema``, ``event`` and ``ts`` plus
event-specific fields):

  * ``run_started``  -- chapter_id, out_dir, started_at,
    backend_identity_hash, resumed_from_index;
  * ``chunk_started`` / ``chunk_done`` (Steps 1-5) -- chunk_id; chunk_done
    also ``outcome``;
  * Step 6: ``audit_unit_started`` / ``audit_unit_done`` (chunk_id,
    detector; done also ``status`` ok|failed), ``audit_done`` (status);
  * Step 7: ``repair_round_started`` (round_number), ``region_started`` /
    ``region_done`` (chunk_id, repair_id, target_pids, action; done also
    ``committed`` and ``reason``), ``reaudit_unit_started`` /
    ``reaudit_unit_done`` (chunk_id, detector), ``repair_done`` (rounds);
  * Step 8: ``formatting_done`` (incidents, blocking), ``terminal``
    (status complete|accepted_degraded|failed).
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

LOG = logging.getLogger(__name__)

PHASE_PROGRESS_SCHEMA = "pact-v4-phase-progress/ndjson/v1"
PHASE_PROGRESS_FILENAME = "phase_progress.ndjson"


class PhaseProgressWriter:
    """Append-only NDJSON progress writer (write-only, diagnostics only).

    One line per event, flushed immediately (journal-style crash-safety).
    Open once in append mode so a resumed run keeps writing to the same
    cumulative file. A write failure disables the writer and is logged, never
    raised — progress must never break a run.
    """

    def __init__(
        self,
        out_dir: Path,
        *,
        now: Optional[Callable[[], datetime]] = None,
    ) -> None:
        self.path = Path(out_dir) / PHASE_PROGRESS_FILENAME
        self._now = now or (lambda: datetime.now(timezone.utc))
        self._handle: Optional[Any] = None
        self._disabled = False

    def _ensure_open(self) -> None:
        if self._handle is None:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self._handle = open(self.path, "a", encoding="utf-8")

    def emit(self, event: str, **fields: Any) -> None:
        if self._disabled:
            return
        record = {
            "schema": PHASE_PROGRESS_SCHEMA,
            "event": event,
            "ts": self._now().isoformat(timespec="seconds"),
            **fields,
        }
        try:
            self._ensure_open()
            assert self._handle is not None
            self._handle.write(json.dumps(record, ensure_ascii=False) + "\n")
            self._handle.flush()
        except OSError as exc:  # disk full / permission -- diagnostics, never a gate
            LOG.warning(
                "Phase progress write failed (%s); disabling progress artifact", exc
            )
            self._disabled = True

    # ------------------------------------------------------------------
    # Event helpers
    # ------------------------------------------------------------------

    def run_started(
        self,
        *,
        chapter_id: str,
        out_dir: Path,
        started_at: str,
        backend_identity_hash: str,
        resumed_from_index: int,
    ) -> None:
        self.emit(
            "run_started",
            chapter_id=chapter_id,
            out_dir=str(out_dir),
            started_at=started_at,
            backend_identity_hash=backend_identity_hash,
            resumed_from_index=resumed_from_index,
        )

    def chunk_started(self, *, chunk_id: str) -> None:
        self.emit("chunk_started", chunk_id=chunk_id)

    def chunk_done(self, *, chunk_id: str, outcome: str) -> None:
        self.emit("chunk_done", chunk_id=chunk_id, outcome=outcome)

    def audit_unit_started(self, *, chunk_id: str, detector: str) -> None:
        self.emit("audit_unit_started", chunk_id=chunk_id, detector=detector)

    def audit_unit_done(self, *, chunk_id: str, detector: str, status: str) -> None:
        self.emit("audit_unit_done", chunk_id=chunk_id, detector=detector, status=status)

    def audit_done(self, *, status: str) -> None:
        self.emit("audit_done", status=status)

    def repair_round_started(self, *, round_number: int) -> None:
        self.emit("repair_round_started", round_number=round_number)

    def region_started(
        self,
        *,
        chunk_id: str,
        repair_id: str,
        target_pids: list,
        action: str,
    ) -> None:
        self.emit(
            "region_started",
            chunk_id=chunk_id,
            repair_id=repair_id,
            target_pids=list(target_pids),
            action=action,
        )

    def region_done(
        self,
        *,
        chunk_id: str,
        repair_id: str,
        target_pids: list,
        action: str,
        committed: bool,
        reason: str,
    ) -> None:
        self.emit(
            "region_done",
            chunk_id=chunk_id,
            repair_id=repair_id,
            target_pids=list(target_pids),
            action=action,
            committed=bool(committed),
            reason=reason,
        )

    def reaudit_unit_started(self, *, chunk_id: str, detector: str) -> None:
        self.emit("reaudit_unit_started", chunk_id=chunk_id, detector=detector)

    def reaudit_unit_done(self, *, chunk_id: str, detector: str, status: str) -> None:
        self.emit("reaudit_unit_done", chunk_id=chunk_id, detector=detector, status=status)

    def repair_done(self, *, rounds: int) -> None:
        self.emit("repair_done", rounds=rounds)

    def formatting_done(self, *, incidents: int, blocking: bool) -> None:
        self.emit("formatting_done", incidents=incidents, blocking=blocking)

    def terminal(self, *, status: str) -> None:
        self.emit("terminal", status=status)

    def close(self) -> None:
        if self._handle is not None:
            try:
                self._handle.close()
            except OSError:  # noqa: S110 -- best-effort close
                pass
            finally:
                self._handle = None
