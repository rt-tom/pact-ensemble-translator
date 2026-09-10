"""Book-chapter retry: stage-aware resume, journal rewind, book resume helpers.

Owner-approved change ``book-chapter-retry`` (2026-09-09). This module holds
the pure decision logic so the strict driver (``v4_phase12_strict_runner``)
and the book wrapper (``v4_book_run``) share one definition of:

* durable stage checkpoints (``stage_manifest.json``) binding each completed
  stage to its input hashes and exact artifact set, plus legacy inference of
  the first unfinished stage from existing status/artifact files;
* journal rewind for ``--retry-incomplete`` (rewind to the first
  ``incomplete_generation`` entry; regenerate it plus the dependent tail)
  with monotonic attempt/revision markers on an append-only journal;
* ready-chapter detection and self-consistency (fail-closed identity) for
  ``book --resume``;
* additive shared-memory semantics: retrying one chapter never rolls back or
  recalculates already completed downstream chapters.

No model calls, no I/O beyond the chapter out-dir passed in. All writers are
atomic (tmp file + os.replace) so a crash mid-write never leaves a torn
checkpoint that could hide the last completed stage.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import secrets
import stat
from pathlib import Path
from typing import Any, Dict, FrozenSet, List, Mapping, Optional, Sequence, Tuple

LOG = logging.getLogger(__name__)

STAGE_MANIFEST_NAME = "stage_manifest.json"
STAGE_MANIFEST_SCHEMA = "pact-v4-stage-manifest/v1"
STAGE_ATTEMPTS_NAME = "stage_attempts.ndjson"

# Canonical stage order. Spec aliases (``generation/selection``,
# ``formatting/finalization``) are normalized to the canonical names.
STAGE_ORDER: Tuple[str, ...] = ("generation", "audit", "repair", "formatting")
STAGE_ALIASES: Dict[str, str] = {
    "generation/selection": "generation",
    "selection": "generation",
    "formatting/finalization": "formatting",
    "finalization": "formatting",
}

# Terminal statuses after which a chapter is "ready" (promotion-only on
# book resume). Mirrors ``v4_book_run._PROMOTING_STATUSES`` without importing
# the wrapper (import cycle: the wrapper imports this module).
READY_STATUSES: Tuple[str, ...] = ("complete", "accepted_degraded")

# Legacy artifact mapping: stage -> artifact files whose valid presence
# proves the stage completed when no durable manifest exists. Checked in
# stage order; the first stage with a missing/corrupt artifact is the
# resume point. ``strict_chapter_trial_record.json`` step statuses refine
# the decision (see ``infer_legacy_stage``).
LEGACY_STAGE_ARTIFACTS: Dict[str, Tuple[str, ...]] = {
    "generation": ("journal.ndjson", "translations.json"),
    "audit": ("audit_cache.json", "b2_handoff.json"),
    "repair": ("repair_report.json",),
    "formatting": ("formatting_report.json",),
}

# Canonical per-stage artifact sets for readiness validation of legacy
# out-dirs (no manifest) and as documentation of the stage contract. When
# a manifest exists, its recorded per-stage sets are authoritative instead.
# ``translations.json``/``selection_results.json`` are promotion inputs and
# are always required separately by :func:`chapter_readiness`.
STAGE_ARTIFACT_SETS: Dict[str, Tuple[str, ...]] = {
    "generation": (
        "journal.ndjson", "generation_outcomes.json",
        "selection_results.json",
    ),
    "audit": ("audit_cache.json", "audit_findings.json", "b2_handoff.json"),
    "repair": ("repair_cache.json", "repair_report.json"),
    "formatting": ("formatting_report.json",),
}

# Exact legitimate complete artifact sets per stage for manifest
# acceptance (see ``_manifest_entry_structure_ok``). Mirrors the manifest
# writer's outputs: the chunked runner records the full sets below (with
# a translations-only formatting variant when formatting is not
# required); whole-chapter runs record a five-file generation set plus
# the actual B3 artifacts per completed stage (audit journal/cache with
# an entity-cache variant, repaired map plus B3 cache, final alias).
# Intentionally skipped steps record an empty set (handled separately via
# their ``skipped`` ``step_status``, not listed here). Any other
# combination -- a removed artifact, an added file, a missing or extra
# hash -- is rejected so a truncated manifest can never validate. Sets
# are mode-agnostic by design: every listed file must exist, parse, and
# hash-match, so a set can only validate where its files genuinely are.
STAGE_MANIFEST_SETS: Dict[str, Tuple[Tuple[str, ...], ...]] = {
    "generation": (
        ("journal.ndjson", "generation_outcomes.json",
         "selection_results.json", "translations.json"),
        ("journal.ndjson", "generation_outcomes.json",
         "selection_results.json", "translations_raw.json",
         "translations.json"),
    ),
    "audit": (
        ("audit_cache.json", "audit_findings.json", "b2_handoff.json"),
        ("audit_journal.ndjson", "audit_cache_b3.json"),
        ("audit_journal.ndjson", "audit_cache_b3.json",
         "entity_context_cache.json"),
    ),
    "repair": (
        ("repair_cache.json", "repair_report.json"),
        ("translations_repaired.json", "audit_cache_b3.json"),
    ),
    "formatting": (
        ("formatting_report.json", "translations.json"),
        ("translations.json",),
    ),
}

# Record step -> stage mapping for legacy inference.
RECORD_STEP_STAGE: Dict[str, str] = {
    "step6": "audit",
    "step7": "repair",
    "step8": "formatting",
}

# Step statuses that count as "satisfied" for resume: completed work plus
# intentional skips (a generation-only run must not suddenly run audit on
# resume; a run without repair adapters must not fabricate repair).
# Step statuses that count as "satisfied" for resume: completed work --
# including converged-with-debt ``accepted_degraded`` terminals, whose
# artifacts are complete and reusable -- plus intentional skips (a
# generation-only run must not suddenly run audit on resume; a run without
# repair adapters must not fabricate repair). The actual degraded status
# is never rewritten by this classification; it only routes resume to
# reuse instead of rerun, with artifacts re-verified in both branches.
SATISFIED_STEP_STATUSES: Tuple[str, ...] = (
    "complete",
    "accepted_degraded",
    "skipped",
    "skipped_stop_after_generation",
    "skipped_no_adapters",
)


def _load_journal_lines(journal_path: Path) -> List[Dict[str, Any]]:
    """Tolerant journal loader (malformed lines are skipped, not fatal)."""
    entries: List[Dict[str, Any]] = []
    try:
        text = journal_path.read_text(encoding="utf-8")
    except OSError:
        return []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            parsed = json.loads(line)
        except ValueError:
            continue
        if isinstance(parsed, dict):
            entries.append(parsed)
    return entries


def _planned_chunk_ids(out_dir: Path) -> List[str]:
    """Chunk ids from ``chunk_plan.json``; ``[]`` when missing/corrupt."""
    path = Path(out_dir) / "chunk_plan.json"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    if not isinstance(payload, dict):
        return []
    chunks = payload.get("chunks")
    if not isinstance(chunks, list):
        return []
    return [
        str(chunk["chunk_id"]) for chunk in chunks
        if isinstance(chunk, dict) and isinstance(chunk.get("chunk_id"), str)
    ]


def normalize_stage(stage: str) -> str:
    """Canonicalize a stage name (spec aliases -> canonical)."""
    name = str(stage or "").strip().lower()
    return STAGE_ALIASES.get(name, name)


def stage_index(stage: str) -> int:
    """Position of a stage in :data:`STAGE_ORDER` (unknown stages sort last)."""
    try:
        return STAGE_ORDER.index(normalize_stage(stage))
    except ValueError:
        return len(STAGE_ORDER)


# ---------------------------------------------------------------------------
# Artifact integrity
# ---------------------------------------------------------------------------


def artifact_sha256(path: Path) -> str:
    """Hex SHA-256 of a file's bytes."""
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(65536), b""):
            digest.update(block)
    return digest.hexdigest()


def _is_readable_json(path: Path) -> bool:
    try:
        json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    return True


def verify_artifact(path: Path) -> Tuple[bool, str]:
    """Check one artifact file: exists, is a regular file, parses as JSON.

    Returns ``(ok, reason)``. Non-JSON artifacts (``journal.ndjson``) are
    checked for existence/non-emptiness instead of JSON parsing.
    """
    try:
        if not path.exists() and not os.path.lexists(str(path)):
            return False, f"missing: {path.name}"
        if path.is_symlink() or not path.is_file():
            return False, f"not a regular file: {path.name}"
        if path.suffix == ".ndjson":
            try:
                text = path.read_text(encoding="utf-8")
            except OSError:
                return False, f"unreadable: {path.name}"
            lines = [line for line in text.splitlines() if line.strip()]
            if not lines:
                return False, f"empty: {path.name}"
            try:
                for line in lines:
                    parsed = json.loads(line)
                    if not isinstance(parsed, dict):
                        return False, f"malformed line: {path.name}"
            except ValueError:
                return False, f"malformed JSON lines: {path.name}"
            return True, "ok"
        if not _is_readable_json(path):
            return False, f"missing/corrupt: {path.name}"
        return True, "ok"
    except OSError as exc:
        return False, f"unreadable {path.name}: {exc}"


# ---------------------------------------------------------------------------
# Stage manifest (durable checkpoints)
# ---------------------------------------------------------------------------


def stage_manifest_present(out_dir: Path) -> bool:
    """Whether a ``stage_manifest.json`` directory entry exists at all.

    This distinguishes a genuinely absent manifest (legacy out-dir: legacy
    artifact inference is the approved fallback) from a present-but-bad
    one (symlink, FIFO, unreadable, foreign schema, malformed stages).
    ``lexists`` is deliberate: a symlink or special file where the
    manifest belongs is PRESENT (and therefore present-invalid), never
    genuinely absent. The distinction matters: :func:`load_stage_manifest`
    maps both cases to ``{}``, but only absence may resolve to ready -- a
    present-but-bad manifest must block promotion and select a safe resume
    instead.
    """
    try:
        return os.path.lexists(os.fspath(Path(out_dir) / STAGE_MANIFEST_NAME))
    except OSError:
        # A directory entry we cannot even stat is present-invalid, never
        # silently absent.
        return True


def load_stage_manifest(out_dir: Path) -> Dict[str, Any]:
    """Load ``stage_manifest.json``; missing/corrupt file -> ``{}``.

    The manifest itself must be an in-directory regular non-symlink file:
    symlinks (which can point outside the chapter dir), FIFOs/special
    files (which can block a reader forever), and unreadable entries never
    load -- they are present-invalid (see :func:`stage_manifest_present`).
    A corrupt manifest is NEVER treated as completed stages: callers fall
    back to legacy inference, which re-verifies artifacts from scratch.
    NOTE: the empty result is ambiguous (genuinely absent vs present but
    unusable) -- promotion gates must consult :func:`stage_manifest_present`
    first and only legacy-infer to ready when the file is genuinely absent.
    Residual TOCTOU note: the type check and the read are not atomic; a
    concurrent swap between them needs a local attacker racing a
    microsecond window and can only hang this process, never silently
    validate -- fail-closed in every outcome that matters.
    """
    path = Path(out_dir) / STAGE_MANIFEST_NAME
    try:
        file_stat = os.lstat(os.fspath(path))
    except OSError:
        return {}
    if stat.S_ISLNK(file_stat.st_mode) or not stat.S_ISREG(file_stat.st_mode):
        LOG.warning("stage manifest %s is not a regular file; ignoring", path)
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        LOG.warning("stage manifest %s unreadable; using legacy inference", path)
        return {}
    if not isinstance(payload, dict):
        return {}
    if payload.get("schema") != STAGE_MANIFEST_SCHEMA:
        LOG.warning("stage manifest %s has foreign schema %r; ignoring",
                    path, payload.get("schema"))
        return {}
    stages = payload.get("stages")
    return dict(stages) if isinstance(stages, dict) else {}


_TMP_WRITE_ATTEMPTS = 10

# O_NOFOLLOW is absent on Windows; there O_CREAT|O_EXCL alone still fails
# on pre-existing paths, and temp names are unguessable regardless.
_NO_FOLLOW = getattr(os, "O_NOFOLLOW", 0)


def _atomic_write_json(path: Path, payload: Any) -> None:
    """Atomically replace ``path`` with serialized ``payload`` (securely).

    The temp file uses a unique unpredictable name in the SAME directory
    (same filesystem, so the final rename stays atomic) and is created
    with ``O_CREAT | O_EXCL`` (+ ``O_NOFOLLOW`` where the platform
    provides it) mode ``0o600``: a pre-created symlink at the temp path
    can never be followed (``ELOOP``/``EEXIST`` instead of an outside
    write), an existing file is never truncated (``EEXIST`` retries with
    a fresh name), and a pre-created FIFO can never block this process
    (no reader is ever needed -- the name is unguessable and exclusively
    created). Bytes are fsynced before the atomic
    ``os.replace``; the directory entry is fsynced afterwards on a
    best-effort basis. Our own temp file is unlinked on any failure.
    Pre-existing attacker-planted paths are never opened, followed, or
    removed -- only ignored. Raises ``OSError`` on failure, exactly like
    the plain write this replaces.
    """
    data = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
    directory = os.fspath(path.parent)
    stem = Path(path).name
    last_error: Optional[OSError] = None
    for _ in range(_TMP_WRITE_ATTEMPTS):
        tmp_name = f".{stem}.{os.getpid()}.{secrets.token_hex(8)}.tmp"
        tmp_path = os.path.join(directory, tmp_name)
        try:
            fd = os.open(tmp_path,
                         os.O_WRONLY | os.O_CREAT | os.O_EXCL | _NO_FOLLOW,
                         0o600)
        except FileExistsError as exc:
            last_error = exc
            continue
        try:
            try:
                writer = os.fdopen(fd, "wb")
            except BaseException:
                os.close(fd)
                raise
            with writer:
                writer.write(data)
                writer.flush()
                os.fsync(writer.fileno())
            os.replace(tmp_path, os.fspath(path))
        except BaseException:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise
        try:
            dir_fd = os.open(directory, os.O_RDONLY)
        except OSError:
            pass
        else:
            try:
                os.fsync(dir_fd)
            except OSError:
                pass
            finally:
                try:
                    os.close(dir_fd)
                except OSError:
                    pass
        return
    raise last_error if last_error is not None else OSError(
        f"could not create unique temp file near {path}")


def write_stage_checkpoint(
    out_dir: Path,
    stage: str,
    *,
    status: str,
    attempt: int = 0,
    inputs: Optional[Mapping[str, Any]] = None,
    artifacts: Sequence[str] = (),
) -> Dict[str, Any]:
    """Record a stage attempt; persist completion only for ``status="complete"``.

    A checkpoint is written atomically ONLY after its artifact set passes
    integrity validation. A failed attempt is appended to
    ``stage_attempts.ndjson`` and NEVER replaces the last completed
    checkpoint, so a crash during a new attempt cannot hide the last
    complete artifact.
    """
    out_dir = Path(out_dir)
    stage = normalize_stage(stage)
    artifact_names = [str(name) for name in artifacts]
    integrity: Dict[str, str] = {}
    missing: List[str] = []
    for name in artifact_names:
        ok, reason = verify_artifact(out_dir / name)
        if not ok:
            missing.append(reason)
            continue
        try:
            integrity[name] = artifact_sha256(out_dir / name)
        except OSError as exc:
            missing.append(f"unreadable {name}: {exc}")
    manifest = load_stage_manifest(out_dir)
    record = {
        "stage": stage,
        "status": status,
        "attempt": int(attempt),
        "inputs": dict(inputs or {}),
        "artifacts": artifact_names,
        "integrity": integrity,
    }
    if status == "complete" and not missing:
        manifest[stage] = record
        _atomic_write_json(out_dir / STAGE_MANIFEST_NAME, {
            "schema": STAGE_MANIFEST_SCHEMA,
            "stages": manifest,
        })
    else:
        # Failed or unverifiable attempt: journal it, keep last completion.
        if status != "complete":
            record["error"] = "stage did not complete"
        else:
            record["error"] = "; ".join(missing) or "integrity check failed"
            record["status"] = "failed"
        try:
            with open(out_dir / STAGE_ATTEMPTS_NAME, "a", encoding="utf-8") as handle:
                handle.write(json.dumps(record, ensure_ascii=False) + "\n")
        except OSError:
            LOG.warning("could not append stage attempt for %s", stage, exc_info=True)
    return record


def _valid_sha256_hex(value: Any) -> bool:
    """Whether ``value`` looks like a recorded SHA-256 hex digest."""
    if not isinstance(value, str) or len(value) != 64:
        return False
    return all(char in "0123456789abcdef" for char in value)


def _manifest_entry_structure_ok(
    stage: str,
    entry: Any,
) -> Tuple[bool, str]:
    """Structural acceptance of one manifest checkpoint entry (no file I/O).

    A syntactically valid but truncated manifest must never validate: the
    entry must carry ``status == "complete"``, an artifact list that
    EXACTLY matches a legitimate complete set for the stage (order is
    insignificant), and an integrity map covering exactly that set with
    valid SHA-256 hex digests -- no removed artifacts, no removed hash
    entries, no extras, no placeholders. Entries recorded for
    intentionally skipped steps (their ``inputs`` carry a ``step_status``
    starting with ``"skipped"``) must have an empty artifact set and an
    empty integrity map instead; anything else empty is rejected (an
    empty set proves no artifacts, so it can never validate completed
    work). Returns ``(ok, reason)``; anything malformed is rejected
    (fail-closed).
    """
    stage = normalize_stage(stage)
    if not isinstance(entry, dict):
        return False, f"malformed checkpoint for {stage}"
    if entry.get("status") != "complete":
        return False, f"no completed checkpoint for {stage}"
    artifacts = entry.get("artifacts")
    if not isinstance(artifacts, list) or any(
        not isinstance(name, str) for name in artifacts
    ):
        return False, f"malformed artifact set for {stage}"
    integrity = entry.get("integrity")
    if not isinstance(integrity, dict):
        return False, f"malformed integrity map for {stage}"
    inputs = entry.get("inputs")
    if not isinstance(inputs, dict):
        return False, f"missing checkpoint inputs for {stage}"
    for field in ("snapshot_hash", "chunk_plan_hash", "config_identity",
                  "backend_identity_hash"):
        value = inputs.get(field)
        if not isinstance(value, str) or not value:
            return False, f"missing checkpoint input {field} for {stage}"
    skipped = str(inputs.get("step_status") or "").startswith("skipped")
    if skipped:
        if artifacts or integrity:
            return False, f"skipped checkpoint for {stage} must not list artifacts"
        return True, "ok"
    legitimate = STAGE_MANIFEST_SETS.get(stage, ())
    if tuple(sorted(artifacts)) not in tuple(
        tuple(sorted(legitimate_set)) for legitimate_set in legitimate
    ):
        return False, f"artifact set mismatch for {stage}: {sorted(artifacts)!r}"
    if sorted(integrity.keys()) != sorted(artifacts):
        return False, f"integrity map mismatch for {stage}"
    for name in artifacts:
        if not _valid_sha256_hex(integrity[name]):
            return False, f"invalid integrity hash for {stage}/{name}"
    return True, "ok"


def refresh_stage_hashes(
    out_dir: Path,
    stages: Sequence[str],
) -> bool:
    """Re-hash current bytes into existing completed stage checkpoints.

    The chapter runner is not the only writer of chapter artifacts: the
    book wrapper's formatting restoration rewrites ``translations.json``
    and ``formatting_report.json`` after the chapter run. Whoever mutates
    the artifact set must refresh the checkpoint, or the manifest would
    permanently disagree with current bytes and every later resume would
    needlessly rerun valid stages. Only stages already present with
    ``status == "complete"`` are refreshed (their recorded artifact sets
    and inputs/attempt are preserved; only ``integrity`` is recomputed);
    missing stages are never created. All-or-nothing: when any recorded
    file fails validation nothing is written and ``False`` is returned, so
    the manifest stays fully consistent or untouched (a stale manifest
    fails closed to rerun, never to silent validation). Best-effort: I/O
    errors return ``False`` instead of raising.
    """
    out_dir = Path(out_dir)
    manifest_path = out_dir / STAGE_MANIFEST_NAME
    if not manifest_path.exists():
        return False
    manifest = load_stage_manifest(out_dir)
    if not manifest:
        return False
    refreshed: Dict[str, Any] = {}
    for stage in stages:
        stage = normalize_stage(stage)
        entry = manifest.get(stage)
        if not isinstance(entry, dict) or entry.get("status") != "complete":
            continue
        # Never bless a structurally non-conforming entry (e.g. a
        # truncated artifact set): refreshing it would launder the
        # truncation into hash-validity.
        ok, _reason = _manifest_entry_structure_ok(stage, entry)
        if not ok:
            return False
        integrity: Dict[str, str] = {}
        valid = True
        for name in entry.get("artifacts") or ():
            ok, _reason = verify_artifact(out_dir / name)
            if not ok:
                valid = False
                break
            try:
                integrity[name] = artifact_sha256(out_dir / name)
            except OSError:
                valid = False
                break
        if not valid:
            return False
        refreshed[stage] = {**entry, "integrity": integrity}
    if not refreshed:
        return False
    try:
        manifest.update(refreshed)
        _atomic_write_json(manifest_path, {
            "schema": STAGE_MANIFEST_SCHEMA,
            "stages": manifest,
        })
    except OSError:
        LOG.warning("stage manifest refresh failed for %s", out_dir, exc_info=True)
        return False
    return True


def manifest_stage_complete(
    out_dir: Path,
    stage: str,
    manifest: Optional[Mapping[str, Any]] = None,
) -> Tuple[bool, str]:
    """Is ``stage`` complete per the manifest AND its artifacts still valid?

    Re-validates artifact existence/integrity on every call: a checkpoint
    whose files were deleted or corrupted after the fact rolls back to that
    stage instead of reusing an unchecked file.
    """
    if manifest is None:
        manifest = load_stage_manifest(out_dir)
    stage = normalize_stage(stage)
    if not isinstance(manifest, dict):
        return False, f"no completed checkpoint for {stage}"
    entry = manifest.get(stage)
    # Structural acceptance first: exact artifact set, exact hash map with
    # valid hashes. A truncated entry (removed artifact or removed hash)
    # is rejected here even when every remaining file verifies.
    ok, reason = _manifest_entry_structure_ok(stage, entry)
    if not ok:
        return False, reason
    expected = entry.get("integrity") or {}
    for name in entry.get("artifacts") or ():
        ok, reason = verify_artifact(Path(out_dir) / name)
        if not ok:
            return False, reason
        try:
            if artifact_sha256(Path(out_dir) / name) != expected[name]:
                return False, f"integrity mismatch: {name}"
        except OSError as exc:
            return False, f"unreadable {name}: {exc}"
    return True, "ok"


# ---------------------------------------------------------------------------
# Legacy inference (no manifest: derive stage from status/artifact files)
# ---------------------------------------------------------------------------


def _record_step_status(record: Mapping[str, Any], step: str) -> Optional[str]:
    block = record.get(step)
    if isinstance(block, dict):
        status = block.get("status")
        return str(status) if status is not None else None
    return None


def infer_legacy_stage(out_dir: Path) -> Tuple[str, str]:
    """First unfinished stage from existing status/artifact files.

    Uses ``strict_chapter_trial_record.json`` step6/7/8 when present and
    parseable; every claimed-complete stage is re-verified against its
    legacy artifact files (missing/corrupt artifact rolls back to that
    stage). No manifest required; a missing manifest is never a refusal
    reason when the artifacts themselves verify.
    """
    out_dir = Path(out_dir)
    record: Mapping[str, Any] = {}
    record_path = out_dir / "strict_chapter_trial_record.json"
    if record_path.exists():
        try:
            payload = json.loads(record_path.read_text(encoding="utf-8"))
            if isinstance(payload, dict):
                record = payload
        except (OSError, ValueError):
            record = {}
    # Generation: journal must exist and be non-empty. An empty journal
    # means generation never committed anything -> start at generation.
    journal_ok, _ = verify_artifact(out_dir / "journal.ndjson")
    if not journal_ok:
        return "generation", "no usable journal"
    # Plan coverage: when chunk_plan.json parses, every planned chunk must
    # have a latest replay entry, and no latest entry may be incomplete.
    # This catches crashed generations (partial journal, no record yet)
    # that artifact-presence alone cannot see.
    planned_ids = _planned_chunk_ids(out_dir)
    replay = replay_entries(_load_journal_lines(out_dir / "journal.ndjson"))
    # Whole-chapter runs journal a single synthetic unit, not the plan's
    # chunk ids: plan coverage does not apply to them.
    if any(str(e.get("chunk_id")) == "whole_chapter" for e in replay):
        planned_ids = []
    if planned_ids:
        replay_ids = {str(e.get("chunk_id")) for e in replay}
        if not set(planned_ids) <= replay_ids:
            return "generation", "journal does not cover the chunk plan"
        if any(e.get("outcome") == "incomplete_generation" for e in replay):
            return "generation", "journal replays incomplete_generation"
    translations_ok, _ = verify_artifact(out_dir / "translations.json")
    generation_records_ok = _is_readable_json(out_dir / "generation_outcomes.json")
    if not translations_ok and not generation_records_ok:
        return "generation", "generation artifacts missing"
    # Later stages from the record when available, else artifact presence.
    for step in ("step6", "step7", "step8"):
        stage = RECORD_STEP_STAGE[step]
        status = _record_step_status(record, step)
        artifacts = LEGACY_STAGE_ARTIFACTS[stage]
        if status is not None:
            if status in SATISFIED_STEP_STATUSES:
                # Claimed complete/skipped: trust only with valid artifacts
                # (a formatting claim without formatting_report.json, or a
                # repair claim without repair_report.json, rolls back).
                if stage == "formatting" and status.startswith("skipped"):
                    continue
                bad = [
                    name for name in artifacts
                    if not verify_artifact(out_dir / name)[0]
                ]
                # Repair/formatting may legitimately lack reports when the
                # stage was skipped by policy; only hard-require artifacts
                # for a "complete" claim on audit (cache/handoff) and
                # generation (checked above). For repair/formatting a
                # "complete" claim without its report rolls back.
                if bad and status in ("complete", "accepted_degraded") and stage in ("audit", "repair", "formatting"):
                    return stage, f"{stage} claimed complete but {bad[0]} invalid"
                continue
            return stage, f"{step} status {status!r}"
        # No record: pure artifact inference.
        if stage == "audit":
            # Audit runs only when the driver reached it; without a record
            # we cannot tell "audit skipped by policy" from "audit crashed".
            # The safe resume point is the first stage whose artifacts are
            # absent, but an absent audit cache alone must not force a full
            # re-audit when repair/formatting already completed below.
            continue
        if all(verify_artifact(out_dir / name)[0] for name in artifacts):
            continue
        return stage, f"legacy artifact missing for {stage}"
    # Record-less repair/formatting check (audit handled above via record).
    if not record:
        for stage in ("repair", "formatting"):
            artifacts = LEGACY_STAGE_ARTIFACTS[stage]
            if all(verify_artifact(out_dir / name)[0] for name in artifacts):
                continue
            # A missing repair/formatting report with a complete journal
            # means those stages never ran -> resume there.
            return stage, f"legacy artifact missing for {stage}"
    return "formatting", "all stages satisfied"


def first_unfinished_stage(out_dir: Path) -> Tuple[str, str]:
    """First failed/missing stage: manifest first, legacy inference fallback.

    A manifest whose completed checkpoints do not bind to the trial record
    (mixed vintage) is distrusted as a whole: selection falls through to
    legacy inference, which re-verifies artifact bytes from scratch.
    Returns ``(stage, reason)``. ``("formatting", "all stages satisfied")``
    means every stage verified (the caller then treats the chapter as fully
    complete and runs promotion only).
    """
    out_dir = Path(out_dir)
    manifest = load_stage_manifest(out_dir)
    if manifest:
        bound, _bind_reason = manifest_inputs_bind_record(
            manifest, _load_record(out_dir))
        if not bound:
            return infer_legacy_stage(out_dir)
        for stage in STAGE_ORDER:
            ok, reason = manifest_stage_complete(out_dir, stage, manifest)
            if not ok:
                # A manifest stage without completion falls back to legacy
                # inference only to pick the *earliest* safe point: never
                # report a later stage when legacy says an earlier one is
                # unfinished.
                legacy_stage, legacy_reason = infer_legacy_stage(out_dir)
                if stage_index(legacy_stage) < stage_index(stage):
                    return legacy_stage, f"legacy: {legacy_reason}"
                return stage, reason
        return "formatting", "all stages satisfied"
    return infer_legacy_stage(out_dir)


# ---------------------------------------------------------------------------
# Journal rewind / replay (append-only, revision/attempt markers)
# ---------------------------------------------------------------------------


def entry_attempt(entry: Mapping[str, Any]) -> int:
    """Effective attempt of a journal entry (legacy entries -> 0)."""
    for key in ("attempt", "revision"):
        try:
            value = int(entry.get(key, 0) or 0)
        except (TypeError, ValueError):
            continue
        if value > 0:
            return value
    try:
        return int(entry.get("attempt", 0) or 0)
    except (TypeError, ValueError):
        return 0


def next_attempt(entries: Sequence[Mapping[str, Any]]) -> int:
    """Monotonic attempt for the next journal append (legacy journal -> 1)."""
    best = 0
    for entry in entries:
        if isinstance(entry, dict):
            attempt = entry_attempt(entry)
            if attempt > best:
                best = attempt
    return best + 1


def first_incomplete_index(entries: Sequence[Mapping[str, Any]]) -> Optional[int]:
    """Positional index of the first ``incomplete_generation`` entry."""
    for index, entry in enumerate(entries):
        if isinstance(entry, dict) and entry.get("outcome") == "incomplete_generation":
            return index
    return None


def resume_index(
    entries: Sequence[Mapping[str, Any]],
    *,
    retry_incomplete: bool = False,
    force_rerun: bool = False,
) -> int:
    """Journal resume position.

    * ``force_rerun`` -> 0 (regenerate everything as a new attempt; the old
      entries stay on disk, append-only).
    * ``retry_incomplete`` -> first ``incomplete_generation`` index (that
      chunk plus the dependent tail regenerate); no incomplete entry ->
      ``len(entries)`` (nothing to rewind to).
    * default -> ``len(entries)`` (legacy positional resume, unchanged).
    """
    if force_rerun:
        return 0
    if retry_incomplete:
        found = first_incomplete_index(entries)
        return len(entries) if found is None else found
    return len(entries)


def replay_entries(
    entries: Sequence[Mapping[str, Any]],
) -> List[Mapping[str, Any]]:
    """Logical replay state: last valid entry per chunk, in last-write order.

    A retry appends new attempts for already-journaled chunks; the replay
    selects the latest entry per ``chunk_id`` so an old ``len()`` can never
    hide a retry. Without duplicates the result equals the input order, so
    legacy behavior is byte-identical.
    """
    latest: Dict[str, Mapping[str, Any]] = {}
    order: List[str] = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        chunk_id = entry.get("chunk_id")
        if not isinstance(chunk_id, str) or not chunk_id:
            continue
        if chunk_id not in latest:
            order.append(chunk_id)
        # Later writes win; on equal attempts the later line wins too.
        current = latest.get(chunk_id)
        if current is None or entry_attempt(entry) >= entry_attempt(current):
            latest[chunk_id] = entry
    return [latest[chunk_id] for chunk_id in order]


# ---------------------------------------------------------------------------
# Ready-chapter detection (book resume)
# ---------------------------------------------------------------------------


def _load_record(out_dir: Path) -> Dict[str, Any]:
    path = Path(out_dir) / "strict_chapter_trial_record.json"
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _whole_chapter_journal(entries: Sequence[Mapping[str, Any]]) -> bool:
    """Whether journal lines describe a whole-chapter run (single unit)."""
    return any(
        isinstance(entry, dict) and str(entry.get("chunk_id")) == "whole_chapter"
        for entry in entries
    )


def _legacy_stages_valid(
    out_dir: Path,
    record: Mapping[str, Any],
    *,
    whole_chapter_run: bool,
) -> Tuple[bool, str, str]:
    """Validate per-stage artifact sets for a manifest-less out-dir.

    Returns ``(ok, stage, reason)``. Every stage the record claims as
    ``complete`` must have its full :data:`STAGE_ARTIFACT_SETS` set present
    and parseable; a failed/unexpected step status routes resume to that
    stage; intentionally skipped stages require nothing. Whole-chapter
    runs skip the audit-set requirement (their B3-managed audit never
    wrote those files; the manifest covers new whole-chapter runs fully).
    """
    out_dir = Path(out_dir)
    if not _is_readable_json(out_dir / "generation_outcomes.json"):
        return False, "generation", "generation_outcomes.json missing/corrupt"
    step6 = _record_step_status(record, "step6")
    if step6 in ("complete", "accepted_degraded") and not whole_chapter_run:
        for name in STAGE_ARTIFACT_SETS["audit"]:
            ok, reason = verify_artifact(out_dir / name)
            if not ok:
                return False, "audit", reason
    elif step6 is not None and step6 not in SATISFIED_STEP_STATUSES:
        return False, "audit", f"step6 status {step6!r}"
    step7 = _record_step_status(record, "step7")
    if step7 in ("complete", "accepted_degraded"):
        for name in STAGE_ARTIFACT_SETS["repair"]:
            ok, reason = verify_artifact(out_dir / name)
            if not ok:
                return False, "repair", reason
    elif step7 is not None and step7 not in SATISFIED_STEP_STATUSES:
        return False, "repair", f"step7 status {step7!r}"
    step7_block = record.get("step7")
    formatting_ran = (
        isinstance(step7_block, dict) and step7_block.get("formatting") is not None
    )
    if formatting_ran:
        for name in STAGE_ARTIFACT_SETS["formatting"]:
            ok, reason = verify_artifact(out_dir / name)
            if not ok:
                return False, "formatting", reason
    return True, "formatting", "all stages satisfied"


def manifest_inputs_bind_record(
    manifest: Mapping[str, Any],
    record: Mapping[str, Any],
) -> Tuple[bool, str]:
    """Bind every completed checkpoint's inputs to the trial record.

    Each ``status == "complete"`` entry must carry the exact snapshot,
    chunk-plan and config identities of ``record["identities"]`` plus a
    backend hash listed on the record. The manifest and the record are
    written seconds apart by the same run, so -- unlike the live-vs-record
    lineage used on resume -- no memory-drift tolerance applies here: any
    divergence means mixed-vintage state (partial restore, interleaved
    runs, hand editing) and the manifest must not be trusted. Entries that
    are not complete claim no reuse and are skipped. Returns ``(ok,
    reason)``; a missing/incomplete record never binds (fail-closed).
    """
    if not isinstance(manifest, dict):
        return False, "no usable stage manifest"
    identities = record.get("identities") if isinstance(record, dict) else None
    if not isinstance(identities, dict):
        return False, "record identities missing"
    for key in ("snapshot_hash", "chunk_plan_hash", "config_identity"):
        value = identities.get(key)
        if not isinstance(value, str) or not value:
            return False, "record identities incomplete"
    record_backend_hashes = _record_backend_hashes(record)
    if not record_backend_hashes:
        return False, "record backend identity missing"
    for stage in STAGE_ORDER:
        entry = manifest.get(stage)
        if not isinstance(entry, dict) or entry.get("status") != "complete":
            continue
        inputs = entry.get("inputs")
        if not isinstance(inputs, dict):
            return False, f"stage {stage} inputs missing"
        for key in ("snapshot_hash", "chunk_plan_hash", "config_identity"):
            if inputs.get(key) != identities.get(key):
                return False, (
                    f"stage {stage} input {key} does not match trial record"
                )
        if inputs.get("backend_identity_hash") not in record_backend_hashes:
            return False, (
                f"stage {stage} backend identity not in trial record"
            )
    return True, "ok"


def _manifest_fully_valid(
    manifest: Mapping[str, Any],
    record: Mapping[str, Any],
) -> Tuple[bool, str]:
    """Whether a loaded manifest is fully trustworthy for skip decisions.

    Every canonical stage entry present must be structurally valid (exact
    artifact set, exact hash map with valid digests -- see
    :func:`_manifest_entry_structure_ok`), and the completed checkpoints
    must bind to the trial record (see :func:`manifest_inputs_bind_record`).
    A manifest with no canonical stage entries at all (unknown-only) is
    invalid: it proves nothing about any real stage. Individual stages may
    still be incomplete/failed -- that is normal resume state, decided
    per-stage downstream -- but nothing malformed or unbound may pass.
    Returns ``(ok, reason)``.
    """
    if not isinstance(manifest, dict) or not manifest:
        return False, "no usable stage manifest"
    canonical = [stage for stage in STAGE_ORDER
                 if isinstance(manifest.get(stage), dict)]
    if not canonical:
        return False, "no canonical stage checkpoints"
    for stage in STAGE_ORDER:
        entry = manifest.get(stage)
        if not isinstance(entry, dict):
            continue  # absent stage = incomplete, not malformed
        if entry.get("status") != "complete":
            continue  # failed/incomplete attempts claim no reuse
        ok, reason = _manifest_entry_structure_ok(stage, entry)
        if not ok:
            return False, reason
    bound, bind_reason = manifest_inputs_bind_record(manifest, record)
    if not bound:
        return False, bind_reason
    return True, "ok"


def _validated_pair(snapshot_hash: Any, chunk_plan_hash: Any) -> Optional[Tuple[str, str]]:
    """A ``(snapshot_hash, chunk_plan_hash)`` pair, or ``None`` if malformed."""
    pair = (snapshot_hash, chunk_plan_hash)
    if not all(isinstance(part, str) and part for part in pair):
        return None
    return pair  # type: ignore[return-value]


def _listed_resumed_pairs(record: Mapping[str, Any]) -> Optional[FrozenSet[Tuple[str, str]]]:
    """Recorded ``resumed_from`` items with strict whole-list validation.

    Returns the set of well-formed pairs, or ``None`` when the value is
    absent in a usable form. "Usable" is deliberately narrow: the value
    must be a mapping (legacy single-pair shape) or a list, every item
    must be a mapping with EXACTLY the keys ``snapshot_hash`` and
    ``chunk_plan_hash``, and both values must be non-empty strings. Any
    deviation -- wrong container, non-mapping item, missing/extra keys,
    malformed values -- voids the whole key (``None``), never a subset:
    a partially trusted allowlist is worse than none, since the skipped
    junk proves the value was not writer-produced.
    """
    if not isinstance(record, dict):
        return None
    raw = record.get("resumed_from")
    if isinstance(raw, dict):
        items: object = [raw]
    elif isinstance(raw, list):
        items = list(raw)
    else:
        return None
    pairs = set()
    for item in items:
        if not isinstance(item, dict):
            return None
        if set(item.keys()) != {"snapshot_hash", "chunk_plan_hash"}:
            return None
        pair = _validated_pair(item.get("snapshot_hash"),
                               item.get("chunk_plan_hash"))
        if pair is None:
            return None
        pairs.add(pair)
    return frozenset(pairs)


def authorized_resumed_pairs(
    record: Mapping[str, Any],
    entries: Sequence[Mapping[str, Any]],
) -> FrozenSet[Tuple[str, str]]:
    """Recorded lineage pairs evidenced by journal lines on disk.

    A listed pair counts only when at least one journal entry on disk is
    stamped with it; a list containing junk, malformed, or unevidenced
    (unrecognized) pairs yields nothing at all -- partial acceptance
    would let single-artifact edits validate. Malformed journal lines are
    skipped here (callers still reject them independently through their
    own malformed-entry checks). An empty allowlist (absent key, empty
    list) is valid and yields an empty set.
    """
    listed = _listed_resumed_pairs(record)
    if not listed:
        return frozenset()
    evidenced: set = set()
    for entry in entries or ():
        if not isinstance(entry, dict):
            continue
        pair = _validated_pair(entry.get("snapshot_hash"),
                               entry.get("chunk_plan_hash"))
        if pair is not None:
            evidenced.add(pair)
    if not listed <= evidenced:
        return frozenset()
    return listed


def accumulate_resumed_from(
    prior_entries: Sequence[Mapping[str, Any]],
    *,
    live_snapshot_hash: str,
    live_plan_hash: str,
    prior_record: Optional[Mapping[str, Any]] = None,
) -> List[Dict[str, str]]:
    """Union of previously recorded pairs with non-live journal pairs.

    Called at record-write time to persist ``resumed_from`` lineage as a
    deterministically sorted list of ``{"snapshot_hash": ...,
    "chunk_plan_hash": ...}`` mappings (JSON objects round-trip; tuples
    would not). Previously recorded pairs carry forward only through
    :func:`authorized_resumed_pairs` (validated shape AND journal
    evidence), so a tampered list cannot launder itself into the next
    record; non-live journal pairs join from the append-only lines
    themselves. The union lets lineage accumulate across successive drift
    retries instead of dropping older pairs the moment a newer one
    appears. Returns ``[]`` when nothing non-live remains, in which case
    callers omit the key and records stay byte-identical.
    """
    pairs = set(authorized_resumed_pairs(prior_record or {}, prior_entries))
    for entry in prior_entries:
        if not isinstance(entry, dict):
            continue
        pair = _validated_pair(entry.get("snapshot_hash"),
                               entry.get("chunk_plan_hash"))
        if pair is not None and pair != (live_snapshot_hash, live_plan_hash):
            pairs.add(pair)
    return [{"snapshot_hash": snapshot, "chunk_plan_hash": plan}
            for snapshot, plan in sorted(pairs)]


def chapter_manifest_blocked(out_dir: Path) -> Tuple[bool, str]:
    """Whether a present-but-unusable manifest blocks promotion this run.

    Returns ``(True, reason)`` when a ``stage_manifest.json`` entry exists
    but is not fully valid -- unreadable, foreign schema, malformed
    stages, inexact artifact/hash sets, missing inputs, unknown-only
    stages, or unbound to the trial record: promotion must wait for a
    later invocation even if the chapter self-heals in this one. Returns
    ``(False, "")`` when the file is genuinely absent (legacy inference
    approved) or fully valid (per-stage completeness is decided
    separately).
    """
    if not stage_manifest_present(out_dir):
        return False, ""
    manifest = load_stage_manifest(out_dir)
    if not manifest:
        return True, "stage manifest present but unreadable/invalid"
    valid, reason = _manifest_fully_valid(
        manifest, _load_record(out_dir))
    if not valid:
        return True, f"stage manifest present but invalid: {reason}"
    return False, ""


def chapter_readiness(out_dir: Path) -> Dict[str, Any]:
    """Inspect a chapter out-dir for book-resume skip eligibility.

    "Ready" = parseable record with terminal status in ``READY_STATUSES``
    AND internally consistent identities (journal/plan/record agree) AND
    promotion inputs (``translations.json``, ``selection_results.json``)
    AND every completed stage checkpoint/artifact set validating: with a
    manifest, each recorded stage must satisfy :func:`manifest_stage_complete`
    (existence + integrity hashes re-checked on every call, so a deleted or
    corrupted artifact can never be treated as ready); without one, the
    legacy per-stage sets in :data:`STAGE_ARTIFACT_SETS` are validated
    against the record's step statuses. Returns a dict with ``ready``
    (bool), ``reason`` (str), ``terminal_status``, ``record`` and
    ``resume_stage`` (the first unsafe stage when not ready, else ``None``).

    Later shared-memory additions never make a chapter stale: memory state
    is deliberately NOT part of this check (additive semantics). Journal
    entries retained under drift are accepted via the record's explicit
    ``resumed_from`` lineage pair; source/config/backend stay fail-closed.
    """
    out_dir = Path(out_dir)

    def _not_ready(reason: str, terminal: str, record: Mapping[str, Any],
                   resume_stage: Optional[str]) -> Dict[str, Any]:
        return {"ready": False, "reason": reason,
                "terminal_status": terminal, "record": dict(record),
                "resume_stage": resume_stage}

    record = _load_record(out_dir)
    if not record:
        return _not_ready("no parseable trial record", "unknown", {}, "generation")
    terminal = str((record.get("step8") or {}).get("status", "unknown"))
    if terminal not in READY_STATUSES:
        return _not_ready(f"terminal status {terminal!r} not ready",
                          terminal, record, None)
    identities = record.get("identities") or {}
    required_identity_keys = ("snapshot_hash", "chunk_plan_hash", "config_identity")
    if not all(isinstance(identities.get(key), str) and identities.get(key)
               for key in required_identity_keys):
        return _not_ready("record identities incomplete", terminal, record, None)
    # Journal entries must agree with the record (no silent mixing).
    journal_path = out_dir / "journal.ndjson"
    ok, reason = verify_artifact(journal_path)
    if not ok:
        return _not_ready(f"journal {reason}", terminal, record, "generation")
    try:
        lines = [json.loads(line) for line in
                 journal_path.read_text(encoding="utf-8").splitlines() if line.strip()]
    except ValueError:
        return _not_ready("journal malformed", terminal, record, "generation")
    # Snapshot/plan legs accept the live record pair OR explicitly
    # recorded resumed_from pairs evidenced by journal lines on disk
    # (approved additive-memory reuse, accumulated across successive drift
    # retries; any junk/malformed/unevidenced pair voids the whole key).
    # Config stays exact and backend must be record-listed in all cases
    # (fail-closed, unchanged).
    allowed_pairs = {
        (identities.get("snapshot_hash"), identities.get("chunk_plan_hash")),
    }
    allowed_pairs |= authorized_resumed_pairs(record, lines)
    record_backend_hashes = _record_backend_hashes(record)
    for entry in lines:
        if not isinstance(entry, dict):
            return _not_ready("journal entry malformed", terminal, record, "generation")
        for key in required_identity_keys:
            if entry.get(key) != identities.get(key):
                if key in ("snapshot_hash", "chunk_plan_hash") and (
                    entry.get("snapshot_hash"), entry.get("chunk_plan_hash")
                ) in allowed_pairs:
                    continue
                return _not_ready(f"journal/record identity mismatch on {key}",
                                  terminal, record, "generation")
        if record_backend_hashes and entry.get("backend_identity_hash") not in record_backend_hashes:
            return _not_ready("journal backend identity not in record",
                              terminal, record, "generation")
    # Required promotion inputs.
    for name in ("translations.json", "selection_results.json"):
        ok, reason = verify_artifact(out_dir / name)
        if not ok:
            return _not_ready(f"{name} {reason}", terminal, record, "generation")
    # Every completed stage checkpoint/artifact set must validate; a
    # deleted or corrupted artifact routes resume to its stage instead of
    # silently promoting a terminal record.
    manifest = load_stage_manifest(out_dir)
    if manifest:
        valid, validity_reason = _manifest_fully_valid(manifest, record)
        if not valid:
            stage, _legacy_reason = first_unfinished_stage(out_dir)
            return _not_ready(
                f"stage manifest invalid: {validity_reason}; "
                f"resume from {stage}",
                terminal, record, stage)
        for stage in STAGE_ORDER:
            ok, reason = manifest_stage_complete(out_dir, stage, manifest)
            if not ok:
                return _not_ready(f"stage {stage} checkpoint invalid: {reason}",
                                  terminal, record, stage)
    elif stage_manifest_present(out_dir):
        # Present but unreadable/foreign/malformed: NEVER legacy-infer to
        # ready. Promotion is blocked; the safe resume stage still comes
        # from legacy artifact inference (which re-verifies bytes from
        # scratch and cannot return ready itself).
        stage, reason = first_unfinished_stage(out_dir)
        return _not_ready(
            f"stage manifest present but unreadable/invalid; resume from {stage}: {reason}",
            terminal, record, stage)
    else:
        ok, stage, reason = _legacy_stages_valid(
            out_dir, record,
            whole_chapter_run=_whole_chapter_journal(lines),
        )
        if not ok:
            return _not_ready(f"stage {stage} invalid: {reason}",
                              terminal, record, stage)
    return {"ready": True,
            "reason": "ready: terminal + identities + stage checkpoints + artifacts valid",
            "terminal_status": terminal, "record": record, "resume_stage": None}


def chapter_invocation_matches(
    out_dir: Path,
    *,
    config_identity: str,
    acceptable_backend_hashes: Sequence[str],
) -> Tuple[bool, str]:
    """Compare saved record/manifest identities to the current invocation.

    A promotion-only skip is safe only when the ready chapter's artifacts
    were produced under the SAME config and backend the current invocation
    resolves: ``record.identities.config_identity`` must equal the current
    ``config_identity``, and one of the record's backend hashes must be in
    the current ``acceptable_backend_hashes``. Completed stage-manifest
    entries carrying ``inputs`` are cross-checked the same way (config +
    backend legs only -- snapshot/plan legs move with additive memory and
    are covered by the replay lineage, not this gate). Any mismatch, or a
    missing/incomplete record, returns False: the caller must hard-fail,
    never silently promote foreign artifacts. Memory drift alone never
    fails this gate (additive semantics).
    """
    out_dir = Path(out_dir)
    record = _load_record(out_dir)
    if not record:
        return False, "no parseable trial record"
    identities = record.get("identities")
    if not isinstance(identities, dict):
        return False, "record identities incomplete"
    if identities.get("config_identity") != config_identity:
        return False, "record config_identity differs from current invocation"
    record_backend_hashes = _record_backend_hashes(record)
    if not record_backend_hashes:
        return False, "record backend identity missing"
    acceptable = [str(h) for h in acceptable_backend_hashes]
    if not any(h in acceptable for h in record_backend_hashes):
        return False, "record backend identity not acceptable to current invocation"
    manifest = load_stage_manifest(out_dir)
    for stage, entry in manifest.items():
        if not isinstance(entry, dict) or entry.get("status") != "complete":
            continue
        inputs = entry.get("inputs")
        if not isinstance(inputs, dict):
            continue
        entry_config = inputs.get("config_identity")
        if (isinstance(entry_config, str) and entry_config
                and entry_config != config_identity):
            return False, f"stage {stage} config_identity differs from current invocation"
        entry_backend = inputs.get("backend_identity_hash")
        if (isinstance(entry_backend, str) and entry_backend
                and entry_backend not in acceptable):
            return False, f"stage {stage} backend identity not acceptable to current invocation"
    return True, "ok"


def _record_backend_hashes(record: Mapping[str, Any]) -> List[str]:
    hashes: List[str] = []
    backend = record.get("backend")
    if isinstance(backend, dict):
        for key in ("identity_hash", "identity_hashes", "acceptable_identity_hashes"):
            value = backend.get(key)
            if isinstance(value, str) and value:
                hashes.append(value)
            elif isinstance(value, list):
                hashes.extend(str(v) for v in value if isinstance(v, str) and v)
    identities = record.get("identities")
    if isinstance(identities, dict):
        value = identities.get("backend_identity_hash")
        if isinstance(value, str) and value:
            hashes.append(value)
    return hashes


def chapter_source_matches(out_dir: Path, chapter_html: Path) -> Tuple[bool, str]:
    """Fail-closed source check for a skipped ready chapter (no model calls).

    Parses the current chapter HTML and compares its source hash with the
    hash stored in the chapter's trial record. A changed source file must
    never reuse stale artifacts: mismatch returns False (the caller reruns
    or fails instead of skipping). Unparseable source or record is also a
    mismatch (fail closed, never silent reuse).
    """
    out_dir = Path(out_dir)
    record = _load_record(out_dir)
    if not record:
        return False, "no parseable trial record"
    expected = (record.get("identities") or {}).get("source_hash")
    if not isinstance(expected, str) or not expected:
        return False, "record has no source_hash"
    try:
        from pact_v4.phase0b.source_html import load_source
        from pact_v4.runtime.snapshot_factory import build_source_artifact
        blocks, _raw_sha = load_source(Path(chapter_html))
        source = build_source_artifact(
            chapter_id=str(record.get("chapter_id") or out_dir.name),
            blocks=blocks,
        )
        actual = source.source_hash
    except Exception as exc:
        return False, f"source unparseable: {exc}"
    if actual != expected:
        return False, "chapter source changed since the ready run"
    return True, "ok"


def lineage_accepted(
    payload_snapshot: Any,
    payload_plan: Any,
    *,
    live_snapshot_hash: str,
    live_plan_hash: str,
    record_snapshot_hash: Optional[str] = None,
    record_plan_hash: Optional[str] = None,
    allow_drift: bool = False,
) -> bool:
    """Snapshot/plan component of a resume identity check (joint pair).

    The pair always matches the live run exactly. Under explicit retry
    flags (``allow_drift``) a payload stamped with the RECORDED pair of
    this chapter's own prior run also passes: later additive shared-memory
    observations move the live snapshot (and the snapshot-bound plan hash),
    but they do not invalidate already completed stages -- the recorded
    pair proves the payload belongs to this chapter's lineage, not a
    foreign run. A payload matching neither pair is foreign. Config/backend
    are still checked exactly by the caller, and source drift is never
    tolerated (see ``resolve_resume_lineage``). Without flags only the
    live pair passes.
    """
    if payload_snapshot == live_snapshot_hash and payload_plan == live_plan_hash:
        return True
    if (
        allow_drift
        and isinstance(record_snapshot_hash, str)
        and record_snapshot_hash
        and isinstance(record_plan_hash, str)
        and record_plan_hash
        and payload_snapshot == record_snapshot_hash
        and payload_plan == record_plan_hash
    ):
        return True
    return False


# Backwards-compatible alias (single-hash callers compare the snapshot leg;
# prefer lineage_accepted for snapshot/plan-bound payloads).
def snapshot_accepted(
    payload_hash: Any,
    *,
    live_snapshot_hash: str,
    record_snapshot_hash: Optional[str] = None,
    allow_drift: bool = False,
) -> bool:
    if payload_hash == live_snapshot_hash:
        return True
    if (
        allow_drift
        and isinstance(record_snapshot_hash, str)
        and record_snapshot_hash
        and payload_hash == record_snapshot_hash
    ):
        return True
    return False


def resolve_resume_lineage(
    *,
    record: Mapping[str, Any],
    source_hash: str,
    config_identity: str,
    acceptable_backend_hashes: Sequence[str],
) -> Tuple[bool, Optional[str], Optional[str]]:
    """Decide whether memory-drift tolerance applies to this resume.

    Returns ``(allowed, record_snapshot_hash, record_plan_hash)``.
    Tolerance requires proof that nothing but shared memory moved since the
    recorded run: the live source and config identities equal the record's,
    and the record's backend hash is acceptable to the current config. The
    plan hash is deliberately NOT compared here -- it binds the snapshot,
    so memory drift always moves it; plan lineage is proven per payload by
    the recorded pair (see ``lineage_accepted``) plus the caller's check
    that every replayed chunk_id still exists in the live plan. A source
    change (or a missing/incomplete record) never tolerates drift -- the
    run fails closed exactly like legacy resume.
    """
    identities = record.get("identities")
    if not isinstance(identities, dict):
        return False, None, None
    if identities.get("source_hash") != source_hash:
        return False, None, None
    if identities.get("config_identity") != config_identity:
        return False, None, None
    record_backend_hashes = _record_backend_hashes(record)
    if not record_backend_hashes:
        return False, None, None
    if not any(h in list(acceptable_backend_hashes) for h in record_backend_hashes):
        return False, None, None
    record_snapshot = identities.get("snapshot_hash")
    record_plan = identities.get("chunk_plan_hash")
    if not isinstance(record_snapshot, str) or not record_snapshot:
        return False, None, None
    if not isinstance(record_plan, str) or not record_plan:
        return False, None, None
    return True, record_snapshot, record_plan


def normalize_chapter_id(chapter_id: str) -> str:
    """Canonical chapter-id comparison (``1`` == ``0001``; else verbatim)."""
    text = str(chapter_id or "").strip()
    return str(int(text)) if text.isdigit() else text


def should_skip_chapter(
    out_dir: Path,
    *,
    resume: bool = False,
    force_rerun_ids: Sequence[str] = (),
    chapter_id: str = "",
) -> Tuple[bool, str]:
    """Book-resume skip decision for one chapter.

    Returns ``(skip, reason)``. Skip (promotion only) requires ``resume``,
    no force-rerun match, and full readiness. Anything else -> rerun in the
    chapter's existing folder from its first unfinished stage.
    """
    if not resume:
        return False, "resume not requested"
    forced = {normalize_chapter_id(item) for item in force_rerun_ids}
    if chapter_id and normalize_chapter_id(chapter_id) in forced:
        return False, f"force-rerun requested for {chapter_id}"
    readiness = chapter_readiness(out_dir)
    if readiness["ready"]:
        return True, readiness["reason"]
    return False, readiness["reason"]


def chapter_extra_args(
    extra_args: Sequence[str],
    *,
    resume: bool = False,
    force_rerun: bool = False,
) -> List[str]:
    """Forward resume flags to a chapter run.

    Automatic ``--resume`` is forwarded when resuming; ``--retry-incomplete``
    is NEVER forwarded blindly (generation rewind applies only when the
    chapter's own stage checkpoint selects it or the operator passes it
    explicitly). ``--force-rerun`` is forwarded only for forced chapters.
    """
    forwarded = list(extra_args)
    if force_rerun and "--force-rerun" not in forwarded:
        forwarded.append("--force-rerun")
    elif resume and "--resume" not in forwarded and "--force-rerun" not in forwarded:
        forwarded.append("--resume")
    return forwarded
