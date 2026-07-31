#!/usr/bin/env python3
"""Model-free, machine-readable execution probe for v3.1 model stages.

Exit codes are the contract consumed by the PowerShell runner: 0=REUSED,
20=MODEL_REQUIRED, 21=FAILED.  The JSON line is for logs and offline tools;
the runner deliberately does not parse console text.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path

from v31_common import (
    ARTIFACT_VERSION,
    TEMPORARY_LEGACY_COMPATIBILITY_POLICY,
    compatible_artifact_version,
)

REUSED = 0
MODEL_REQUIRED = 20
MODEL_REQUIRED_INVALID = 22
FAILED = 21
LEGACY_PROVENANCE_SCHEMA = "pact-v31-legacy-reuse-provenance/v1"


def emit(outcome: str, **detail: object) -> int:
    print(json.dumps({"protocol": "pact-v31-stage-execution/v1", "outcome": outcome, **detail}, ensure_ascii=False))
    if outcome == "MODEL_REQUIRED" and detail.get("reason") == "missing_partial_or_invalid_aggregate" and detail.get("has_invalid"):
        return MODEL_REQUIRED_INVALID
    return {"REUSED": REUSED, "MODEL_REQUIRED": MODEL_REQUIRED, "FAILED": FAILED}[outcome]


def valid_aggregate(path: Path, *, allow_legacy_artifact_version: bool = False) -> bool:
    try:
        value = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return False
    # All v3.1 aggregates carry a producer version.  Completion counters are
    # checked when present, so a truncated but parseable aggregate cannot be
    # promoted to REUSED.  Fine-grained cache identity remains validated by the
    # stage itself on the MODEL_REQUIRED execution path.
    if not isinstance(value, dict) or not compatible_artifact_version(
        value.get("version"), allow_legacy=allow_legacy_artifact_version
    ):
        return False
    expected, completed = value.get("expected"), value.get("completed")
    if expected is not None or completed is not None:
        if not isinstance(expected, int) or not isinstance(completed, int) or expected != completed:
            return False
    return True


def translation_cache_complete(work_dir: Path, chapter_stems: list[str]) -> tuple[bool, list[str]]:
    """Prove that every manifest chunk has a complete recursive draft cache."""
    incomplete: list[str] = []
    for stem in chapter_stems:
        chapter_dir = work_dir / stem
        try:
            manifest = json.loads((chapter_dir / "manifest.json").read_text(encoding="utf-8-sig"))
            chunks = manifest.get("chunks") if isinstance(manifest, dict) else None
            if not isinstance(chunks, list) or not chunks:
                raise ValueError("manifest has no chunks")
            for chunk in chunks:
                if not isinstance(chunk, dict):
                    raise ValueError("invalid chunk")
                chunk_id = chunk.get("chunk_id")
                pids = chunk.get("pids")
                if not isinstance(chunk_id, str) or not chunk_id or not isinstance(pids, list) or not pids:
                    raise ValueError("invalid chunk identity")
                draft_path = chapter_dir / "drafts" / f"{chunk_id}.json"
                draft = json.loads(draft_path.read_text(encoding="utf-8-sig"))
                translations = draft.get("translations") if isinstance(draft, dict) else None
                if not isinstance(translations, dict) or not all(pid in translations for pid in pids):
                    incomplete.append(str(draft_path))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError, TypeError):
            incomplete.append(str(chapter_dir))
    return not incomplete, incomplete


def write_json_atomic(path: Path, value: object) -> None:
    """Publish a complete provenance document or leave the prior document intact."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    try:
        with temporary.open("w", encoding="utf-8", newline="\n") as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def record_accepted_legacy_reuse(
    path: Path, *, work_dir: Path, stage: str, artifact_paths: list[Path]
) -> list[dict[str, object]]:
    """Atomically append accepted legacy reuse records, without duplicate resumes."""
    try:
        document = json.loads(path.read_text(encoding="utf-8-sig")) if path.exists() else {}
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"Legacy provenance is unreadable: {path}") from exc
    records = document.get("records", []) if isinstance(document, dict) else []
    if not isinstance(records, list):
        raise RuntimeError(f"Legacy provenance records are invalid: {path}")
    accepted: list[dict[str, object]] = []
    existing = {record.get("record_id") for record in records if isinstance(record, dict)}
    for artifact in artifact_paths:
        value = json.loads(artifact.read_text(encoding="utf-8-sig"))
        version = value["version"]
        if version == ARTIFACT_VERSION:
            continue
        digest = hashlib.sha256(artifact.read_bytes()).hexdigest()
        relative = artifact.relative_to(work_dir).as_posix()
        identity = "|".join((stage, relative, str(version), ARTIFACT_VERSION, digest))
        record_id = hashlib.sha256(identity.encode("utf-8")).hexdigest()
        if record_id in existing:
            continue
        record = {
            "record_id": record_id,
            "chapter": artifact.relative_to(work_dir).parts[0],
            "stage": stage,
            "artifact_path": relative,
            "artifact_version": version,
            "expected_semantic_version": ARTIFACT_VERSION,
            "compatibility_policy": TEMPORARY_LEGACY_COMPATIBILITY_POLICY,
            "reuse_decision": "legacy-compatible-reused",
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "artifact_hash": digest,
        }
        records.append(record); accepted.append(record); existing.add(record_id)
    if accepted:
        write_json_atomic(path, {"schema": LEGACY_PROVENANCE_SCHEMA, "records": records})
    return accepted


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--work-dir", type=Path, required=True)
    parser.add_argument("--chapter-stem", action="append", default=[])
    parser.add_argument("--aggregate-relative-path")
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--translation", action="store_true")
    parser.add_argument("--allow-legacy-artifact-version", action="store_true",
                        help="Allow only explicitly listed legacy artifact versions to be reused.")
    parser.add_argument("--stage", default="unspecified")
    parser.add_argument("--legacy-provenance-path", type=Path)
    args = parser.parse_args()
    if not args.chapter_stem:
        return emit("FAILED", reason="empty_chapter_selection")
    if args.translation:
        complete, incomplete = translation_cache_complete(args.work_dir, args.chapter_stem)
        if complete:
            return emit("REUSED", reason="all_manifest_chunk_drafts_complete")
        return emit("MODEL_REQUIRED", reason="translation_cache_incomplete", incomplete=incomplete)
    if args.force:
        return emit("MODEL_REQUIRED", reason="forced")
    if not args.aggregate_relative_path:
        return emit("FAILED", reason="missing_aggregate_relative_path")
    paths = [args.work_dir / stem / args.aggregate_relative_path for stem in args.chapter_stem]
    missing = [str(path) for path in paths if not path.exists()]
    invalid = [
        str(path) for path in paths
        if path.exists() and not valid_aggregate(
            path, allow_legacy_artifact_version=args.allow_legacy_artifact_version
        )
    ]
    if missing or invalid:
        return emit("MODEL_REQUIRED", reason="missing_partial_or_invalid_aggregate", missing=missing, invalid=invalid, has_invalid=bool(invalid))
    versions = {json.loads(path.read_text(encoding="utf-8-sig"))["version"] for path in paths}
    legacy_versions = sorted(version for version in versions if version != ARTIFACT_VERSION)
    provenance: dict[str, object] = {
        "compatibility_policy": (
            TEMPORARY_LEGACY_COMPATIBILITY_POLICY if legacy_versions else "strict-semantic-version"
        ),
        "reuse_decision": "legacy-compatible-reused" if legacy_versions else "semantic-version-reused",
    }
    if legacy_versions:
        provenance["legacy_version"] = legacy_versions
        if args.legacy_provenance_path is None:
            return emit("FAILED", reason="missing_legacy_provenance_path")
        provenance["accepted_records"] = record_accepted_legacy_reuse(
            args.legacy_provenance_path, work_dir=args.work_dir, stage=args.stage, artifact_paths=paths
        )
    return emit("REUSED", aggregate_count=len(paths), provenance=provenance)


if __name__ == "__main__":
    raise SystemExit(main())
