"""Run-hook integration for the v4 book run (state-only).

Pre-init hook: fetch_current -> validate four files -> then MemoryManager inits.
Post-promote hook: build candidate from working dir after MemoryManager.promote('complete'),
push_candidate, handle STALE_PARENT bounded retry.

All hooks are fail-closed and preserve local state on failure.
Transport injectable for tests.
"""

from __future__ import annotations

import json
import logging
import uuid
from pathlib import Path
from typing import Any, Dict, Optional

from . import remote_client

LOG = logging.getLogger(__name__)

CANONICAL_FILES = ["glossary.json", "book_memory.json", "chapter_index.json", "observations.json"]

def _validate_working_dir_files(working_dir: Path) -> None:
    for fname in CANONICAL_FILES:
        p = working_dir / fname
        if p.is_symlink():
            raise RuntimeError(f"Working dir file is symlink (rejected): {fname}")
        if not p.is_file():
            raise RuntimeError(f"Working dir file missing: {fname}")
        try:
            json.loads(p.read_text(encoding="utf-8"))
        except Exception as e:
            raise RuntimeError(f"Working dir file not valid JSON: {fname}: {e}") from e

def pre_init_fetch(book_id: str, working_dir: str | Path, *, transport=None, ssh_target: str = "media", root: str = "/home/rt/pact_runs", execution_host: str | None = None) -> Dict[str, Any]:
    """Pre-init hook: fetch authoritative state from media into working_dir.

    Validates four files (regular, non-symlink, allowed names, valid JSON).
    Fails fast on media unreachable (raises RuntimeError, no fallback).
    Returns CURRENT dict.
    """
    wdir = Path(working_dir)
    wdir.mkdir(parents=True, exist_ok=True)
    try:
        cur = remote_client.fetch_current(book_id, wdir, transport=transport, ssh_target=ssh_target, root=root, execution_host=execution_host)
    except Exception as e:
        raise RuntimeError(f"pre_init_fetch: media unreachable or validation failed for book {book_id!r}: {e}") from e
    # Validate after fetch
    _validate_working_dir_files(wdir)
    return cur

def post_promote_push(
    book_id: str,
    working_dir: str | Path,
    *,
    transport=None,
    ssh_target: str = "media",
    root: str = "/home/rt/pact_runs",
    candidate_id: Optional[str] = None,
    max_retries: int = 1,
    execution_host: str | None = None,
) -> Dict[str, Any]:
    """Post-promote hook: build candidate from working_dir and push to media.

    Handles STALE_PARENT with bounded re-pull + retry (default 1).
    On other rejections or transport failure, raises RuntimeError and preserves local state.
    Returns verdict dict (ACCEPTED with revision_id).

    caller should have already called MemoryManager.promote('complete') before this.
    """
    wdir = Path(working_dir)
    _validate_working_dir_files(wdir)
    if candidate_id is None:
        candidate_id = f"cand-{uuid.uuid4().hex[:8]}"
    # Attempt push with retry loop
    last_verdict: Optional[Dict[str, Any]] = None
    for attempt in range(max_retries + 1):
        try:
            verdict = remote_client.push_candidate(book_id, candidate_id, wdir, transport=transport, ssh_target=ssh_target, root=root, execution_host=execution_host)
        except Exception as e:
            raise RuntimeError(f"post_promote_push: transport failure for {candidate_id}: {e}") from e
        if verdict.get("status") == "ACCEPTED":
            LOG.info("post_promote_push ACCEPTED %s -> %s", candidate_id, verdict.get("revision_id"))
            # Advance local parent pointer so next per-chapter push uses new revision
            try:
                cur_path = wdir / "CURRENT.json"
                # Prefer verdict current dict if present, else minimal
                new_current: Dict[str, Any]
                if isinstance(verdict.get("current"), dict):
                    new_current = dict(verdict["current"])
                else:
                    # Fallback: read existing and update revision_id
                    if cur_path.is_file() and not cur_path.is_symlink():
                        try:
                            new_current = json.loads(cur_path.read_text(encoding="utf-8"))
                        except Exception:
                            new_current = {}
                    else:
                        new_current = {}
                    new_current["revision_id"] = verdict.get("revision_id")
                    if "manifest_sha256" in verdict:
                        new_current["manifest_sha256"] = verdict["manifest_sha256"]
                    new_current["book_id"] = book_id
                # Ensure revision_id present
                if "revision_id" not in new_current and "revision_id" in verdict:
                    new_current["revision_id"] = verdict["revision_id"]
                # Atomic write via tmp + replace
                tmp_path = cur_path.with_suffix(".tmp")
                tmp_path.write_text(json.dumps(new_current, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
                tmp_path.replace(cur_path)
            except Exception as e:
                LOG.warning("Failed to advance local parent pointer after ACCEPTED: %s", e)
            return verdict
        reason = verdict.get("reason")
        if reason == "STALE_PARENT" and attempt < max_retries:
            LOG.warning("STALE_PARENT for %s, re-pulling and retrying (attempt %d/%d)", candidate_id, attempt + 1, max_retries)
            # Preserve completed canonical state before any re-pull (do not lose RT update)
            preserved: Dict[str, bytes | None] = {}
            for fname in CANONICAL_FILES:
                p = wdir / fname
                try:
                    preserved[fname] = p.read_bytes() if p.is_file() and not p.is_symlink() else None
                except Exception:
                    preserved[fname] = None
            # Bounded re-pull: fetch new parent revision (may overwrite working dir)
            try:
                pre_init_fetch(book_id, wdir, transport=transport, ssh_target=ssh_target, root=root, execution_host=execution_host)
            except Exception as e:
                raise RuntimeError(f"post_promote_push: re-pull after STALE_PARENT failed: {e}") from e
            # Restore RT-updated canonical files without overwriting the new CURRENT.json parent pointer
            for fname, data in preserved.items():
                if data is not None:
                    try:
                        (wdir / fname).write_bytes(data)
                    except Exception as e:
                        raise RuntimeError(f"Failed to restore preserved state {fname} after re-pull: {e}") from e
            # Need new candidate_id for retry (promote quarantined previous)
            candidate_id = f"cand-{uuid.uuid4().hex[:8]}"
            last_verdict = verdict
            continue
        # Other rejection: report and preserve local state (do not delete working dir)
        msg = f"post_promote_push REJECTED {candidate_id}: {reason} {verdict.get('message','')}"
        LOG.error(msg)
        # Attach last verdict for caller to report
        # Raise with verdict details
        raise RuntimeError(msg + f" verdict={verdict}")
    # If loop exhausted without ACCEPTED
    raise RuntimeError(f"post_promote_push: STALE_PARENT retry exhausted, last verdict={last_verdict}")

def run_book_with_media_sync(
    book_id: str,
    working_dir: str | Path,
    run_fn,
    *args,
    transport=None,
    ssh_target: str = "media",
    root: str = "/home/rt/pact_runs",
    max_retries: int = 1,
    execution_host: str | None = None,
    **kwargs,
) -> Any:
    """Wrap a book-run function with pre-init fetch and post-promote push.

    run_fn is expected to be a callable that performs the translation run and
    internally calls MemoryManager.promote('complete'). This wrapper does:
      1. pre_init_fetch (fail-fast)
      2. run_fn(*args, **kwargs)
      3. post_promote_push (with STALE_PARENT retry)
    """
    pre_init_fetch(book_id, working_dir, transport=transport, ssh_target=ssh_target, root=root, execution_host=execution_host)
    result = run_fn(*args, **kwargs)
    verdict = post_promote_push(book_id, working_dir, transport=transport, ssh_target=ssh_target, root=root, max_retries=max_retries, execution_host=execution_host)
    # Attach confirmation to result if dict
    if isinstance(result, dict):
        result["media_confirmation"] = verdict
    return result
