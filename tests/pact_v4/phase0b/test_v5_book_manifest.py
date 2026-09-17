"""V5 slice-1: source-manifest contract + boundary negative matrix.

Model-free, fixture-only. Covers the shared validation routine
(``validate_source_manifest``) used identically at preflight and at the
pre-start re-check, plus ``verify_set_hash`` and ``load_manifest``.
"""
from __future__ import annotations

import json
import os
import socket
import stat
from pathlib import Path

import pytest

from pact_v4.phase0b.book_manifest import (
    MANIFEST_SCHEMA_VERSION,
    SourceManifest,
    deterministic_set_hash,
    load_manifest,
    parse_manifest_dict,
    validate_source_manifest,
    verify_set_hash,
    ManifestError,
)


def _chapter(file="0001_test.html", order=1, title="Test 1", pov=None):
    entry = {"file": file, "order": order, "en_title": title, "illustrations": []}
    if pov is not None:
        entry["pov"] = pov
    return entry


def _manifest_payload(files=("0001_test.html", "0002_test.html"), kind="directory", **over):
    payload = {
        "schema_version": MANIFEST_SCHEMA_VERSION,
        "book_slug": "testbook",
        "source_kind": kind,
        "splitter_version": "pact-v5-splitter/v1",
        "set_hash": "deadbeef",
        "chapters": [
            _chapter(file=f, order=i + 1, title=f"Title {i + 1}")
            for i, f in enumerate(files)
        ],
    }
    payload.update(over)
    return payload


def _write_source(root: Path, names=("0001_test.html", "0002_test.html")) -> list[Path]:
    root.mkdir(parents=True, exist_ok=True)
    out = []
    for i, name in enumerate(names):
        p = root / name
        p.write_text(f"<html><head><title>Title {i + 1}</title></head><body><p>text {i}</p></body></html>\n", encoding="utf-8")
        out.append(p)
    return out


def _valid_manifest(tmp_path: Path, names=("0001_test.html", "0002_test.html")) -> SourceManifest:
    files = _write_source(tmp_path / "src", names)
    set_hash = deterministic_set_hash(files)
    payload = _manifest_payload(files=names, set_hash=set_hash)
    (tmp_path / "manifest.json").write_text(json.dumps(payload), encoding="utf-8")
    return load_manifest(tmp_path / "manifest.json")


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------

def test_validate_ok_and_set_hash(tmp_path):
    manifest = _valid_manifest(tmp_path)
    resolved = validate_source_manifest(tmp_path / "src", manifest)
    assert sorted(resolved) == [1, 2]
    assert resolved[1].name == "0001_test.html"
    verify_set_hash(tmp_path / "src", manifest)


def test_deterministic_set_hash_stable(tmp_path):
    files = _write_source(tmp_path / "src")
    assert deterministic_set_hash(files) == deterministic_set_hash(list(reversed(files)))


def test_unlisted_extra_file_is_ignored_never_read(tmp_path):
    """Read allow-list: only manifest-listed paths are ever opened.

    Unlisted files in the source dir (owner notes, previous manifests)
    are ignored — the boundary governs what the pipeline READS.
    """
    manifest = _valid_manifest(tmp_path)
    (tmp_path / "src" / "notes.txt").write_text("owner notes", encoding="utf-8")
    (tmp_path / "src" / "9999_extra.html").write_text("<html></html>", encoding="utf-8")
    resolved = validate_source_manifest(tmp_path / "src", manifest)
    assert sorted(resolved) == [1, 2]


# ---------------------------------------------------------------------------
# Structural rejection (no disk reads needed)
# ---------------------------------------------------------------------------

def test_reject_extra_top_level_key():
    payload = _manifest_payload()
    payload["future_field"] = {}
    with pytest.raises(ManifestError, match="unexpected top-level"):
        parse_manifest_dict(payload)


def test_reject_unknown_schema_version():
    with pytest.raises(ManifestError, match="schema_version"):
        parse_manifest_dict(_manifest_payload(schema_version="pact-v5-book-manifest/v9"))


def test_reject_wrong_hash_key_for_kind():
    with pytest.raises(ManifestError, match="must not carry set_hash"):
        parse_manifest_dict(_manifest_payload(kind="epub", epub_hash="ab"))
    payload = _manifest_payload(kind="epub")
    del payload["set_hash"]
    payload["epub_hash"] = "ab"
    manifest = parse_manifest_dict(payload)
    assert manifest.source_kind == "epub"
    with pytest.raises(ManifestError, match="must not carry"):
        parse_manifest_dict(_manifest_payload(kind="directory", set_hash="ab", epub_hash="cd"))


def test_reject_duplicate_order_and_file():
    payload = _manifest_payload(files=("0001_a.html", "0001_a.html"))
    with pytest.raises(ManifestError, match="duplicate file"):
        parse_manifest_dict(payload)
    payload = _manifest_payload()
    payload["chapters"][1]["order"] = 1
    with pytest.raises(ManifestError, match="duplicate order"):
        parse_manifest_dict(payload)


def test_reject_non_contiguous_orders():
    payload = _manifest_payload()
    payload["chapters"][1]["order"] = 7
    with pytest.raises(ManifestError, match="contiguous"):
        parse_manifest_dict(payload)


def test_reject_path_traversal_filename():
    for bad in ("../evil.html", "sub/ch.html", ".hidden.html", "noext", "0001.txt"):
        payload = _manifest_payload(files=("0001_test.html", "0002_test.html"))
        payload["chapters"][0]["file"] = bad
        with pytest.raises(ManifestError, match="bare filename|\\.html"):
            parse_manifest_dict(payload)


def test_reject_malformed_json_and_non_object(tmp_path):
    bad = tmp_path / "bad.json"
    bad.write_text("{not json", encoding="utf-8")
    with pytest.raises(ManifestError, match="invalid JSON"):
        load_manifest(bad)
    bad.write_text("[1,2]", encoding="utf-8")
    with pytest.raises(ManifestError, match="must be a JSON object"):
        load_manifest(bad)


def test_reject_illustration_shape():
    payload = _manifest_payload()
    payload["chapters"][0]["illustrations"] = [{"id": "a"}]
    with pytest.raises(ManifestError, match="id, anchor"):
        parse_manifest_dict(payload)
    payload["chapters"][0]["illustrations"] = [{"id": "a", "anchor": "x"}, {"id": "a", "anchor": "y"}]
    with pytest.raises(ManifestError, match="duplicate illustration"):
        parse_manifest_dict(payload)


# ---------------------------------------------------------------------------
# On-disk type boundary (TYPE layer: regular file only)
# ---------------------------------------------------------------------------

def test_reject_missing_file(tmp_path):
    manifest = _valid_manifest(tmp_path)
    (tmp_path / "src" / "0002_test.html").unlink()
    with pytest.raises(ManifestError):
        validate_source_manifest(tmp_path / "src", manifest)


def test_reject_symlink_chapter_file(tmp_path):
    manifest = _valid_manifest(tmp_path)
    target = tmp_path / "src" / "0002_test.html"
    target.unlink()
    target.symlink_to(tmp_path / "src" / "0001_test.html")
    with pytest.raises(ManifestError, match="[Ss]ymlink"):
        validate_source_manifest(tmp_path / "src", manifest)


def test_reject_fifo_chapter_file(tmp_path):
    manifest = _valid_manifest(tmp_path)
    target = tmp_path / "src" / "0002_test.html"
    target.unlink()
    os.mkfifo(target)
    with pytest.raises(ManifestError, match="not a regular file"):
        validate_source_manifest(tmp_path / "src", manifest)


def test_reject_socket_chapter_file(tmp_path):
    manifest = _valid_manifest(tmp_path)
    target = tmp_path / "src" / "0002_test.html"
    target.unlink()
    sock = socket.socket(socket.AF_UNIX)
    try:
        sock.bind(str(target))
        with pytest.raises(ManifestError, match="not a regular file"):
            validate_source_manifest(tmp_path / "src", manifest)
    finally:
        sock.close()


def test_reject_directory_named_as_chapter(tmp_path):
    manifest = _valid_manifest(tmp_path)
    target = tmp_path / "src" / "0002_test.html"
    target.unlink()
    target.mkdir()
    with pytest.raises(ManifestError, match="not a regular file"):
        validate_source_manifest(tmp_path / "src", manifest)


def test_device_files_fail_regular_check(tmp_path):
    """Primitive-level proof: devices are not regular files (no mknod needed)."""
    from pact_v4.phase0b.book_manifest import _is_regular_file

    assert _is_regular_file(Path("/dev/null")) is False


def test_reject_symlink_source_root(tmp_path):
    manifest = _valid_manifest(tmp_path)
    link = tmp_path / "linkroot"
    link.symlink_to(tmp_path / "src", target_is_directory=True)
    with pytest.raises(ManifestError, match="[Ss]ymlink"):
        validate_source_manifest(link, manifest)


def test_reject_symlink_in_ancestor_chain(tmp_path):
    manifest = _valid_manifest(tmp_path)
    real = tmp_path / "real"
    real.mkdir()
    (real / "inner").mkdir()
    link = tmp_path / "chainlink"
    link.symlink_to(real, target_is_directory=True)
    nested = link / "inner"
    # validate against a root under a symlinked ancestor
    with pytest.raises(ManifestError, match="[Ss]ymlink"):
        validate_source_manifest(nested / ".." / "inner", manifest)


def test_reject_symlink_manifest_file(tmp_path):
    _valid_manifest(tmp_path)
    real = tmp_path / "manifest.json"
    moved = tmp_path / "manifest-real.json"
    real.rename(moved)
    real.symlink_to(moved)
    with pytest.raises(ManifestError, match="[Ss]ymlink"):
        load_manifest(real)


# ---------------------------------------------------------------------------
# Hash layer + TOCTOU (same routine before lock and before move)
# ---------------------------------------------------------------------------

def test_set_hash_mismatch_rejected(tmp_path):
    manifest = _valid_manifest(tmp_path)
    (tmp_path / "src" / "0001_test.html").write_text("<html>mutated</html>", encoding="utf-8")
    with pytest.raises(ManifestError, match="set_hash mismatch"):
        verify_set_hash(tmp_path / "src", manifest)


def test_post_validation_mutation_caught_by_revalidation(tmp_path):
    """TOCTOU: validate -> mutate -> the IDENTICAL re-validation fails."""
    manifest = _valid_manifest(tmp_path)

    def _pre_start_recheck():
        # Same pair the dispatcher runs before delegation.
        validate_source_manifest(tmp_path / "src", manifest)
        verify_set_hash(tmp_path / "src", manifest)

    _pre_start_recheck()  # pre-lock: ok
    (tmp_path / "src" / "0001_test.html").write_bytes(b"<html>attacker</html>")
    with pytest.raises(ManifestError):  # pre-move recheck: fail-closed
        _pre_start_recheck()


def test_empty_chapter_file_rejected(tmp_path):
    manifest = _valid_manifest(tmp_path)
    (tmp_path / "src" / "0001_test.html").write_text("", encoding="utf-8")
    with pytest.raises(ManifestError, match="empty"):
        validate_source_manifest(tmp_path / "src", manifest)
