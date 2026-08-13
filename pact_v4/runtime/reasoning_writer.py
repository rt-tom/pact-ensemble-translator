"""Live reasoning-file writer shared by the model phases (REASONING-STREAM).

Phases that persist ``*_reasoning.txt`` artifacts create the file BEFORE the
model call and pass the returned appender as
``CompletionRequest.on_reasoning_chunk``, so the file grows live while the
model is still generating (gemma_rewrite_v4 pattern). The phase still writes
the authoritative final reasoning after completion — the live writer is a
diagnostics/monitoring bonus, never the source of truth.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Callable, Optional

LOG = logging.getLogger(__name__)


def open_reasoning_writer(
    path: Optional[Path],
) -> Optional[Callable[[str], None]]:
    """Create (or truncate) the reasoning file BEFORE the model call and
    return an appending writer for live reasoning chunks.

    Returns ``None`` when ``path`` is ``None`` (no artifact requested, e.g.
    the phase runs without ``out_dir``). Appends are best-effort: a disk
    failure is logged and swallowed so the live writer never breaks the
    model call — the phase's post-completion write is authoritative.
    """
    if path is None:
        return None
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("", encoding="utf-8")

    def _append(chunk: str) -> None:
        try:
            with path.open("a", encoding="utf-8") as fh:
                fh.write(chunk)
        except OSError as exc:  # pragma: no cover - disk failure only
            LOG.warning("reasoning writer failed for %s: %s", path, exc)

    return _append


def append_error_marker(path: Optional[Path], exc: BaseException) -> None:
    """Append a ``TRANSPORT_ERROR`` marker to the reasoning file.

    Preserves any reasoning already streamed live before the failure (the
    run_011 lesson: a failure must leave a disk trail), instead of wiping
    the file with an empty write. Best-effort, like ``open_reasoning_writer``.
    """
    if path is None:
        return
    try:
        with path.open("a", encoding="utf-8") as fh:
            fh.write(f"\nTRANSPORT_ERROR: {type(exc).__name__}: {exc}\n")
    except OSError as err:  # pragma: no cover - disk failure only
        LOG.warning("reasoning error marker failed for %s: %s", path, err)


__all__ = ["open_reasoning_writer", "append_error_marker"]
