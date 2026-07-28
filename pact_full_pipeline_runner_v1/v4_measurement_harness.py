#!/usr/bin/env python3
"""v4 Phase 0A read-only measurement import/export tooling.

The importer intentionally records identities, counts, and hashes instead of
embedding full chapter source or translation text in measurement outputs.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import re
import sys
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass, fields
from pathlib import Path
from typing import Any


SCHEMA_VERSION = "v4.measurement_record.v0a.1"
COMPARISON_SCHEMA_VERSION = "v4.measurement_comparison.v0a.1"
UNKNOWN = "unknown"
TEXT_OUTPUT_FILES = (
    "repaired_translations.json",
    "final_translations.json",
    "selected_translations.json",
    "draft_translations.json",
)
CSV_LIST_SEPARATOR = "|"
WORD_RE = re.compile(
    r"[A-Za-zА-Яа-яЁё0-9]+(?:[’'\-][A-Za-zА-Яа-яЁё0-9]+)*",
)


JsonScalar = str | int | float | bool


@dataclass(frozen=True)
class MeasurementRecord:
    schema_version: str = SCHEMA_VERSION
    run_label: str = UNKNOWN
    pipeline_family: str = UNKNOWN
    pipeline_version: str = UNKNOWN
    run_identity: str = UNKNOWN
    run_root_name: str = UNKNOWN
    chapter_id: str = UNKNOWN
    chapter_name: str = UNKNOWN
    pid: str = UNKNOWN
    pid_index: int | str = UNKNOWN
    tag: str = UNKNOWN
    chunk_id: str = UNKNOWN
    candidate_id: str = UNKNOWN
    candidate_role: str = UNKNOWN
    source_identity: str = UNKNOWN
    memory_snapshot_identity: str = UNKNOWN
    output_identity: str = UNKNOWN
    source_text_sha256: str = UNKNOWN
    output_text_sha256: str = UNKNOWN
    source_word_count: int | str = UNKNOWN
    output_word_count: int | str = UNKNOWN
    source_char_count: int | str = UNKNOWN
    output_char_count: int | str = UNKNOWN
    final_stage: str = UNKNOWN
    raw_issue_count: int | str = UNKNOWN
    confirmed_issue_count: int | str = UNKNOWN
    rejected_issue_count: int | str = UNKNOWN
    uncertain_issue_count: int | str = UNKNOWN
    repair_action: str = UNKNOWN
    repair_accepted: bool | str = UNKNOWN
    post_repair_verdict: str = UNKNOWN
    deterministic_integrity: str = UNKNOWN
    formatting_integrity: str = UNKNOWN
    semantic_residual: str = UNKNOWN
    bad_repair: str = UNKNOWN
    russian_quality: str = UNKNOWN
    ltcr: str = UNKNOWN
    cost_tokens: int | str = UNKNOWN
    latency_seconds: float | str = UNKNOWN
    model_reloads: int | str = UNKNOWN
    quarantine_status: str = UNKNOWN


@dataclass(frozen=True)
class ComparisonRecord:
    schema_version: str = COMPARISON_SCHEMA_VERSION
    left_run_label: str = UNKNOWN
    right_run_label: str = UNKNOWN
    chapter_id: str = UNKNOWN
    chapter_name: str = UNKNOWN
    pid: str = UNKNOWN
    left_output_text_sha256: str = UNKNOWN
    right_output_text_sha256: str = UNKNOWN
    output_text_same: bool | str = UNKNOWN
    left_final_stage: str = UNKNOWN
    right_final_stage: str = UNKNOWN
    left_confirmed_issue_count: int | str = UNKNOWN
    right_confirmed_issue_count: int | str = UNKNOWN
    confirmed_issue_delta: int | str = UNKNOWN
    left_repair_action: str = UNKNOWN
    right_repair_action: str = UNKNOWN
    left_quarantine_status: str = UNKNOWN
    right_quarantine_status: str = UNKNOWN


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as file:
        for chunk in iter(lambda: file.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def canonical_json_hash(value: Any) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return sha256_text(encoded)


def read_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    with path.open("r", encoding="utf-8-sig") as file:
        return json.load(file)


def natural_key(value: str) -> list[Any]:
    return [
        int(piece) if piece.isdigit() else piece.casefold()
        for piece in re.split(r"(\d+)", value)
    ]


def safe_int(value: Any, fallback: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return fallback


def count_words(value: str) -> int:
    return len(WORD_RE.findall(value or ""))


def scalar_or_unknown(value: Any) -> JsonScalar:
    if value is None or value == "":
        return UNKNOWN
    if isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


def resolved(path: Path) -> Path:
    return path.resolve(strict=False)


def is_relative_to(child: Path, parent: Path) -> bool:
    try:
        child.relative_to(parent)
        return True
    except ValueError:
        return False


def ensure_export_target_allowed(path: Path, input_roots: list[Path]) -> None:
    target = resolved(path)
    for root in input_roots:
        if is_relative_to(target, resolved(root)):
            raise ValueError(f"Refusing to write export inside read-only input root: {target}")


def discover_chapter_dirs(run_root: Path) -> list[Path]:
    if (run_root / "manifest.json").exists():
        return [run_root]
    work = run_root / "work"
    if work.exists():
        return sorted(
            [path for path in work.iterdir() if path.is_dir() and (path / "manifest.json").exists()],
            key=lambda path: natural_key(path.name),
        )
    return sorted(
        [path for path in run_root.iterdir() if path.is_dir() and (path / "manifest.json").exists()],
        key=lambda path: natural_key(path.name),
    )


def chunk_map(manifest: dict[str, Any]) -> dict[str, str]:
    result: dict[str, str] = {}
    for chunk in manifest.get("chunks") or []:
        if not isinstance(chunk, dict):
            continue
        chunk_id = str(chunk.get("chunk_id") or UNKNOWN)
        for pid in chunk.get("pids") or []:
            result.setdefault(str(pid), chunk_id)
    return result


def load_output_map(chapter_dir: Path) -> tuple[dict[str, str], str]:
    for name in TEXT_OUTPUT_FILES:
        path = chapter_dir / name
        data = read_json(path, None)
        if isinstance(data, dict):
            return {str(key): str(value) for key, value in data.items()}, path.stem
    return {}, UNKNOWN


def load_issue_counts(
    chapter_dir: Path,
) -> tuple[dict[str, int], bool, dict[str, int], bool, Counter[str]]:
    raw = read_json(chapter_dir / "issues.qwen_raw.json", None)
    if raw is None:
        raw = read_json(chapter_dir / "issues.json", None)
    deterministic = read_json(chapter_dir / "deterministic_issues.json", None)
    raw_known = isinstance(raw, list) or isinstance(deterministic, list)
    raw_by_pid: dict[str, int] = defaultdict(int)
    confirmed_by_pid: dict[str, int] = defaultdict(int)
    verdicts: Counter[str] = Counter()

    for source in (raw, deterministic):
        if isinstance(source, list):
            for issue in source:
                if isinstance(issue, dict) and issue.get("pid"):
                    raw_by_pid[str(issue["pid"])] += 1

    confirmed = read_json(chapter_dir / "verified_issues.json", None)
    if confirmed is None:
        confirmed = read_json(chapter_dir / "issues.json", None)
    confirmed_known = isinstance(confirmed, list)
    if isinstance(confirmed, list):
        for issue in confirmed:
            if isinstance(issue, dict) and issue.get("pid"):
                confirmed_by_pid[str(issue["pid"])] += 1

    report = read_json(chapter_dir / "verifier_report.json", {})
    decisions = report.get("decisions") if isinstance(report, dict) else None
    if isinstance(decisions, list):
        for item in decisions:
            if not isinstance(item, dict):
                continue
            pid = str(item.get("pid") or (item.get("issue") or {}).get("pid") or "")
            if not pid:
                continue
            decision = str(item.get("decision") or item.get("verdict") or UNKNOWN).casefold()
            verdicts[f"{pid}:{decision}"] += 1
    return dict(raw_by_pid), raw_known, dict(confirmed_by_pid), confirmed_known, verdicts


def load_repair_by_pid(chapter_dir: Path) -> dict[str, dict[str, Any]]:
    data = read_json(chapter_dir / "repair_records.json", [])
    result: dict[str, dict[str, Any]] = {}
    if not isinstance(data, list):
        return result
    for item in data:
        if isinstance(item, dict) and item.get("pid"):
            result.setdefault(str(item["pid"]), item)
    return result


def memory_snapshot_identity(run_root: Path, chapter_dir: Path) -> str:
    candidates = [
        run_root / "book_bible.json",
        run_root / "book_memory.json",
        chapter_dir / "chapter_bible.json",
        chapter_dir / "chapter_memory.json",
    ]
    found = {str(path.relative_to(run_root) if is_relative_to(path, run_root) else path.name): sha256_file(path)
             for path in candidates if path.exists() and path.is_file()}
    return canonical_json_hash(found) if found else UNKNOWN


def import_run(run_root: Path, run_label: str, pipeline_family: str = UNKNOWN) -> list[MeasurementRecord]:
    run_root = run_root.resolve()
    config = read_json(run_root / "config.full_pipeline.v31.json", None)
    if config is None:
        config = read_json(run_root / "config.full_pipeline.json", {})
    pipeline_version = scalar_or_unknown(config.get("artifact_version") if isinstance(config, dict) else None)
    run_identity = canonical_json_hash({
        "root": run_root.name,
        "config": sha256_file(run_root / "config.full_pipeline.v31.json")
        if (run_root / "config.full_pipeline.v31.json").exists()
        else sha256_file(run_root / "config.full_pipeline.json")
        if (run_root / "config.full_pipeline.json").exists()
        else UNKNOWN,
    })

    records: list[MeasurementRecord] = []
    for chapter_dir in discover_chapter_dirs(run_root):
        manifest = read_json(chapter_dir / "manifest.json", {})
        if not isinstance(manifest, dict):
            continue
        blocks = [block for block in (manifest.get("blocks") or []) if isinstance(block, dict)]
        by_chunk = chunk_map(manifest)
        output_map, final_stage = load_output_map(chapter_dir)
        output_identity = canonical_json_hash(output_map) if output_map else UNKNOWN
        (
            raw_counts,
            raw_counts_known,
            confirmed_counts,
            confirmed_counts_known,
            verifier_verdicts,
        ) = load_issue_counts(chapter_dir)
        repair_by_pid = load_repair_by_pid(chapter_dir)
        quality = read_json(chapter_dir / "quality_report.json", {})
        integrity = quality.get("integrity") if isinstance(quality, dict) else {}
        deterministic_integrity = (
            "pass" if isinstance(integrity, dict) and integrity.get("ok") is True
            else "fail" if isinstance(integrity, dict) and integrity.get("ok") is False
            else UNKNOWN
        )
        formatting_counts = (
            integrity.get("formatting_incident_counts")
            if isinstance(integrity, dict) else {}
        )
        formatting_integrity = (
            "pass" if isinstance(formatting_counts, dict)
            and int(formatting_counts.get("unresolved_required") or 0) == 0
            else UNKNOWN
        )
        post_report = read_json(chapter_dir / "post_repair_report.json", {})
        post_by_pid = {
            str(item.get("pid")): item
            for item in (post_report.get("decisions") or [])
            if isinstance(item, dict) and item.get("pid")
        } if isinstance(post_report, dict) else {}
        chapter_name = str(manifest.get("chapter") or chapter_dir.name)
        source_identity = scalar_or_unknown(manifest.get("source_sha256"))
        if source_identity == UNKNOWN and (chapter_dir / "source.normalized.html").exists():
            source_identity = sha256_file(chapter_dir / "source.normalized.html")
        memory_identity = memory_snapshot_identity(run_root, chapter_dir)

        indexed_blocks = [
            (safe_int(block.get("index"), fallback_index), block)
            for fallback_index, block in enumerate(blocks)
        ]
        for fallback_index, block in sorted(
            indexed_blocks, key=lambda item: (item[0], natural_key(str(item[1].get("pid") or "")))
        ):
            pid = str(block.get("pid") or UNKNOWN)
            source_text = str(block.get("source_text") or block.get("text") or "")
            output_text = output_map.get(pid)
            repair = repair_by_pid.get(pid, {})
            post = post_by_pid.get(pid, {})
            records.append(MeasurementRecord(
                run_label=run_label or UNKNOWN,
                pipeline_family=pipeline_family or UNKNOWN,
                pipeline_version=pipeline_version,
                run_identity=run_identity,
                run_root_name=run_root.name,
                chapter_id=str(manifest.get("chapter_id") or chapter_dir.name or UNKNOWN),
                chapter_name=chapter_name,
                pid=pid,
                pid_index=scalar_or_unknown(block.get("index", fallback_index)),
                tag=scalar_or_unknown(block.get("tag")),
                chunk_id=by_chunk.get(pid, UNKNOWN),
                source_identity=str(source_identity),
                memory_snapshot_identity=memory_identity,
                output_identity=output_identity,
                source_text_sha256=sha256_text(source_text) if source_text else UNKNOWN,
                output_text_sha256=sha256_text(output_text) if output_text else UNKNOWN,
                source_word_count=count_words(source_text) if source_text else UNKNOWN,
                output_word_count=count_words(output_text) if output_text else UNKNOWN,
                source_char_count=len(source_text) if source_text else UNKNOWN,
                output_char_count=len(output_text) if output_text else UNKNOWN,
                final_stage=final_stage,
                raw_issue_count=raw_counts.get(pid, 0) if raw_counts_known else UNKNOWN,
                confirmed_issue_count=(
                    confirmed_counts.get(pid, 0) if confirmed_counts_known else UNKNOWN
                ),
                rejected_issue_count=verifier_verdicts.get(f"{pid}:reject", UNKNOWN),
                uncertain_issue_count=verifier_verdicts.get(f"{pid}:uncertain", UNKNOWN),
                repair_action=scalar_or_unknown(repair.get("action")),
                repair_accepted=scalar_or_unknown(repair.get("accepted")),
                post_repair_verdict=scalar_or_unknown(post.get("verdict")),
                deterministic_integrity=deterministic_integrity,
                formatting_integrity=formatting_integrity,
            ))
    return sorted(
        records,
        key=lambda item: (
            item.run_label,
            item.chapter_id,
            item.chapter_name,
            natural_key(str(item.pid)),
        ),
    )


def records_to_payload(records: list[MeasurementRecord]) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "record_count": len(records),
        "records": [asdict(record) for record in records],
    }


def load_measurement_payload(path: Path) -> list[dict[str, Any]]:
    data = read_json(path, {})
    records = data.get("records") if isinstance(data, dict) else data
    if not isinstance(records, list):
        raise ValueError(f"{path} does not contain measurement records")
    return [item for item in records if isinstance(item, dict)]


def compare_records(left_path: Path, right_path: Path) -> list[ComparisonRecord]:
    left = load_measurement_payload(left_path)
    right = load_measurement_payload(right_path)
    def key(item: dict[str, Any]) -> tuple[str, str, str]:
        return (
            str(item.get("chapter_id") or UNKNOWN),
            str(item.get("chapter_name") or UNKNOWN),
            str(item.get("pid") or UNKNOWN),
        )
    left_by_key = {key(item): item for item in left}
    right_by_key = {key(item): item for item in right}
    result: list[ComparisonRecord] = []
    for item_key in sorted(set(left_by_key) | set(right_by_key), key=lambda value: (value[0], value[1], natural_key(value[2]))):
        left_item = left_by_key.get(item_key, {})
        right_item = right_by_key.get(item_key, {})
        left_hash = str(left_item.get("output_text_sha256") or UNKNOWN)
        right_hash = str(right_item.get("output_text_sha256") or UNKNOWN)
        left_count = left_item.get("confirmed_issue_count", UNKNOWN)
        right_count = right_item.get("confirmed_issue_count", UNKNOWN)
        delta: int | str = UNKNOWN
        if isinstance(left_count, int) and isinstance(right_count, int):
            delta = right_count - left_count
        result.append(ComparisonRecord(
            left_run_label=str(left_item.get("run_label") or UNKNOWN),
            right_run_label=str(right_item.get("run_label") or UNKNOWN),
            chapter_id=item_key[0],
            chapter_name=item_key[1],
            pid=item_key[2],
            left_output_text_sha256=left_hash,
            right_output_text_sha256=right_hash,
            output_text_same=(
                left_hash == right_hash
                if left_hash != UNKNOWN and right_hash != UNKNOWN
                else UNKNOWN
            ),
            left_final_stage=str(left_item.get("final_stage") or UNKNOWN),
            right_final_stage=str(right_item.get("final_stage") or UNKNOWN),
            left_confirmed_issue_count=left_count,
            right_confirmed_issue_count=right_count,
            confirmed_issue_delta=delta,
            left_repair_action=str(left_item.get("repair_action") or UNKNOWN),
            right_repair_action=str(right_item.get("repair_action") or UNKNOWN),
            left_quarantine_status=str(left_item.get("quarantine_status") or UNKNOWN),
            right_quarantine_status=str(right_item.get("quarantine_status") or UNKNOWN),
        ))
    return result


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as file:
        json.dump(payload, file, ensure_ascii=False, indent=2, sort_keys=True)
        file.write("\n")


def write_csv(path: Path, rows: list[Any], row_type: type[Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = [field.name for field in fields(row_type)]
    with path.open("w", encoding="utf-8", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            data = asdict(row)
            writer.writerow({
                key: CSV_LIST_SEPARATOR.join(value) if isinstance(value, list) else value
                for key, value in data.items()
            })


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="v4 Phase 0A measurement harness")
    sub = parser.add_subparsers(dest="command", required=True)

    import_parser = sub.add_parser("import", help="read-only import finished run outputs")
    import_parser.add_argument("--run-root", type=Path, required=True)
    import_parser.add_argument("--label", default=UNKNOWN)
    import_parser.add_argument("--pipeline", default=UNKNOWN)
    import_parser.add_argument("--json-out", type=Path)
    import_parser.add_argument("--csv-out", type=Path)

    compare_parser = sub.add_parser("compare", help="compare two measurement JSON exports")
    compare_parser.add_argument("--left", type=Path, required=True)
    compare_parser.add_argument("--right", type=Path, required=True)
    compare_parser.add_argument("--json-out", type=Path)
    compare_parser.add_argument("--csv-out", type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == "import":
        run_root = args.run_root.resolve()
        outputs = [path for path in (args.json_out, args.csv_out) if path is not None]
        for output in outputs:
            ensure_export_target_allowed(output, [run_root])
        records = import_run(run_root, args.label, args.pipeline)
        if args.json_out:
            write_json(args.json_out, records_to_payload(records))
        if args.csv_out:
            write_csv(args.csv_out, records, MeasurementRecord)
        if not outputs:
            json.dump(records_to_payload(records), sys.stdout, ensure_ascii=False, indent=2, sort_keys=True)
            sys.stdout.write("\n")
        return 0

    left = args.left.resolve()
    right = args.right.resolve()
    outputs = [path for path in (args.json_out, args.csv_out) if path is not None]
    for output in outputs:
        target = resolved(output)
        if target in {left, right}:
            raise ValueError(f"Refusing to overwrite comparison input: {target}")
    comparisons = compare_records(left, right)
    payload = {
        "schema_version": COMPARISON_SCHEMA_VERSION,
        "record_count": len(comparisons),
        "records": [asdict(record) for record in comparisons],
    }
    if args.json_out:
        write_json(args.json_out, payload)
    if args.csv_out:
        write_csv(args.csv_out, comparisons, ComparisonRecord)
    if not outputs:
        json.dump(payload, sys.stdout, ensure_ascii=False, indent=2, sort_keys=True)
        sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
