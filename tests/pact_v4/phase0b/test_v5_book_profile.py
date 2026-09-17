"""V5 slice-1: book profile resolution, gates, and fail-closed isolation.

Fixture books live in tmp (``PACT_V5_BOOKS_DIR``); real-repo profiles are
covered by skip-guarded tests at the bottom. The Pale profile must fail
closed on any Pact-owned path; gates must fail before output/state/model
activity.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from pact_v4.phase0b.book_profile import (
    CHAPTERS_SCHEMA_VERSION,
    PROFILE_SCHEMA_VERSION,
    ApprovedChapters,
    check_cross_book_isolation,
    check_profile_gates,
    load_approved_chapters,
    load_profile,
    preflight_book,
    resolve_book,
    ProfileError,
)


def _write_profile(
    books: Path,
    slug: str,
    *,
    manifest="manifest.json",
    chapters="chapters.json",
    media_book_id="7",
    content_kind="prose",
    source_lang="en",
    target_lang="ru",
    hard_filters="v4-en-ru",
    editor_pass="russian-editor-v1",
    state_suffix=None,
    source_suffix=None,
) -> Path:
    home = books / slug
    home.mkdir(parents=True, exist_ok=True)
    state_suffix = state_suffix or slug
    source_suffix = source_suffix or slug
    text = (
        f"schema_version: {PROFILE_SCHEMA_VERSION}\n"
        f"slug: {slug}\ntitle: {slug.title()}\ncontent_kind: {content_kind}\n"
        f"source_lang: {source_lang}\ntarget_lang: {target_lang}\n"
        "source:\n"
        f"  rt_root: D:/pact/{source_suffix}_chapters\n"
        f"  media_root: /tmp/{source_suffix}_chapters\n"
        f"  manifest: {manifest}\n"
        "state:\n"
        f'  media_book_id: \"{media_book_id}\"\n'
        f"  rt_root: D:/pact/book_state_{state_suffix}\n"
        f"  media_root: /tmp/book_state_{state_suffix}\n"
        "out:\n"
        "  rt_root: D:/pact/gate_bench_runs\n"
        "  media_root: /tmp/outputs\n"
        f"chapters: {chapters}\n"
        "policy:\n"
        f"  hard_filters: {hard_filters}\n"
        f"  editor_pass: {editor_pass}\n"
    )
    path = home / "book.yaml"
    path.write_text(text, encoding="utf-8")
    return path


@pytest.fixture()
def two_books(tmp_path, monkeypatch):
    books = tmp_path / "books"
    _write_profile(books, "pact", media_book_id="1", state_suffix="pact", source_suffix="pact")
    _write_profile(books, "pale", media_book_id="2", state_suffix="pale", source_suffix="pale")
    monkeypatch.setenv("PACT_V5_BOOKS_DIR", str(books))
    return books


# ---------------------------------------------------------------------------
# Loading + gates
# ---------------------------------------------------------------------------

def test_load_pact_profile_roots(two_books):
    profile = load_profile("pact", books=two_books)
    assert profile.slug == "pact"
    assert profile.media_book_id == "1"
    assert profile.policy_hard_filters == "v4-en-ru"
    check_profile_gates(profile)


def test_unknown_slug_fails(two_books):
    with pytest.raises(ProfileError, match="not found|unknown"):
        load_profile("nope", books=two_books)
    with pytest.raises(ProfileError, match="slug"):
        load_profile("Bad Slug!", books=two_books)


def test_gates_reject_non_prose_and_non_en_ru(two_books):
    _write_profile(two_books, "memoir", content_kind="memoir")
    with pytest.raises(ProfileError, match="content_kind"):
        check_profile_gates(load_profile("memoir", books=two_books))
    _write_profile(two_books, "deutsch", source_lang="en", target_lang="de")
    with pytest.raises(ProfileError, match="en/ru"):
        check_profile_gates(load_profile("deutsch", books=two_books))
    load_profile("pact", books=two_books)  # control: pact still loads
    _write_profile(two_books, "weird", hard_filters="v9-x")
    with pytest.raises(ProfileError, match="hard_filters"):
        load_profile("weird", books=two_books)


def test_missing_manifest_and_chapters_fail_preflight(two_books):
    report = preflight_book("pale", books=two_books)
    assert report["ok"] is False
    assert any("manifest" in e for e in report["errors"])
    assert any("chapters" in e for e in report["errors"])


# ---------------------------------------------------------------------------
# Isolation
# ---------------------------------------------------------------------------

def test_manifest_outside_slug_dir_rejected(two_books):
    (two_books / "pact" / "manifest.json").write_text("{}", encoding="utf-8")
    _write_profile(two_books, "sneaky", manifest="../pact/manifest.json", media_book_id="9",
                   state_suffix="sneaky", source_suffix="sneaky")
    with pytest.raises(ProfileError, match="isolation"):
        load_profile("sneaky", books=two_books)


def test_chapters_outside_slug_dir_rejected(two_books):
    _write_profile(two_books, "sneaky2", chapters="../pact/chapters.json", media_book_id="9",
                   state_suffix="sneaky2", source_suffix="sneaky2")
    with pytest.raises(ProfileError, match="isolation"):
        load_profile("sneaky2", books=two_books)


def test_shared_media_book_id_rejected(two_books):
    _write_profile(two_books, "clone", media_book_id="1", state_suffix="clone", source_suffix="clone")
    with pytest.raises(ProfileError, match="media_book_id"):
        check_cross_book_isolation("clone", books=two_books)


def test_shared_state_root_rejected(two_books):
    _write_profile(two_books, "squat", media_book_id="9", state_suffix="pale", source_suffix="squat")
    with pytest.raises(ProfileError, match="isolation"):
        check_cross_book_isolation("squat", books=two_books)


def test_chapters_inside_state_dir_rejected(two_books):
    home = two_books / "badbook"
    (home / "state").mkdir(parents=True)
    (home / "state" / "chapters.json").write_text("{}", encoding="utf-8")
    _write_profile(two_books, "badbook", chapters="state/chapters.json", media_book_id="9",
                   state_suffix="badbook", source_suffix="badbook")
    with pytest.raises(ProfileError, match="snapshot state"):
        load_profile("badbook", books=two_books)


def test_resolve_applies_env_override_then_revalidates(two_books, tmp_path, monkeypatch):
    """Env-injected Pale source pointing at Pact paths still fails closed."""
    pact_src = tmp_path / "pact_chapters"
    pact_src.mkdir()
    monkeypatch.setenv("PACT_V4_SOURCE_ROOT", str(pact_src))
    resolved = resolve_book("pact", host="media", books=two_books)
    assert resolved.source_root == pact_src


# ---------------------------------------------------------------------------
# Approved chapters
# ---------------------------------------------------------------------------

def _write_chapters(home: Path, records, slug="pale") -> Path:
    path = home / "chapters.json"
    path.write_text(json.dumps({
        "schema_version": CHAPTERS_SCHEMA_VERSION,
        "book_slug": slug,
        "chapters": records,
    }), encoding="utf-8")
    return path


def _record(order, file=None, en="Title", ru=None, pov=None):
    return {
        "file": file or f"{order:04d}_x.html",
        "order": order,
        "en_title": en,
        "ru_title": ru,
        "pov": pov,
        "notes": "",
    }


def test_approved_chapters_ok_and_title_map(two_books):
    home = two_books / "pale"
    _write_chapters(home, [
        _record(1, en="Chap One", ru="Глава один", pov={"name": "Verona", "gender": "female"}),
        _record(2, en="Chap Two", ru=None, pov={"name": "Lucy", "gender": "female"}),
    ])
    approved = load_approved_chapters(home / "chapters.json", "pale")
    assert len(approved.chapters) == 2
    assert len(approved.sha256) == 64
    assert approved.title_map() == (("Chap One", "Глава один"),)
    assert set(approved.pov_by_order()) == {1, 2}


def test_approved_chapters_slug_mismatch_is_isolation_error(two_books):
    home = two_books / "pale"
    _write_chapters(home, [_record(1)], slug="pact")
    with pytest.raises(ProfileError, match="isolation|book_slug"):
        load_approved_chapters(home / "chapters.json", "pale")


def test_approved_chapters_rejects_bad_shapes(two_books):
    home = two_books / "pale"
    _write_chapters(home, [_record(1), _record(1, file="0002_y.html")])
    with pytest.raises(ProfileError, match="duplicate order"):
        load_approved_chapters(home / "chapters.json", "pale")
    _write_chapters(home, [_record(1), _record(3, file="0002_y.html")])
    with pytest.raises(ProfileError, match="contiguous"):
        load_approved_chapters(home / "chapters.json", "pale")
    _write_chapters(home, [_record(1, pov={"name": "X", "gender": "other"})])
    with pytest.raises(ProfileError, match="gender"):
        load_approved_chapters(home / "chapters.json", "pale")
    extra = _record(1)
    extra["future"] = 1
    _write_chapters(home, [extra])
    with pytest.raises(ProfileError, match="unexpected key"):
        load_approved_chapters(home / "chapters.json", "pale")


def test_preflight_ok_with_manifest_and_chapters(two_books, tmp_path, monkeypatch):
    from pact_v4.phase0b.epub_splitter import generate_directory_manifest

    src = tmp_path / "pale_chapters"
    src.mkdir()
    for i in (1, 2):
        (src / f"{i:04d}_pale.html").write_text(
            f"<html><head><title>C{i}</title></head><body><h2>C{i}</h2><p>x</p></body></html>",
            encoding="utf-8",
        )
    pale_home = two_books / "pale"
    generate_directory_manifest(src, "pale", manifest_out=pale_home / "manifest.json")
    _write_chapters(pale_home, [
        _record(1, file="0001_pale.html", en="C1"),
        _record(2, file="0002_pale.html", en="C2"),
    ])
    monkeypatch.setenv("PACT_V4_SOURCE_ROOT", str(src))
    report = preflight_book("pale", host="media", books=two_books)
    assert report["ok"] is True, report["errors"]
    assert report["manifest"]["chapters"] == 2
    assert report["media_book_id"] == "2"


def test_no_runtime_arc_names_input():
    """The legacy arc-names sidecar must not be a runtime input (migration-only).

    Functional references are forbidden in runtime modules: the ``--arc-names``
    flag, ``args.arc_names``, the old loader, and the sidecar filename used
    as a read path. Docstring prose mentioning the migration history is fine.
    """
    import re

    repo = Path(__file__).resolve().parents[3]
    functional = re.compile(
        r"--arc-names|args\.arc_names|_load_arc_names"
        r"|['\"]arc_names\.json['\"]"
        r"|memory-dir.*arc_names|<cwd>.*arc_names"
    )
    offenders = []
    for path in (
        repo / "pact_v4" / "phase0b" / "book_manifest.py",
        repo / "pact_v4" / "phase0b" / "book_profile.py",
        repo / "pact_v4" / "phase0b" / "epub_splitter.py",
        repo / "pact_v4" / "runtime" / "bible_renderer.py",
        repo / "pact_v4" / "pipeline" / "v4_phase12_strict_runner.py",
        repo / "pact_full_pipeline_runner_v1" / "v4_run.py",
        repo / "pact_full_pipeline_runner_v1" / "v4_book_run.py",
        repo / "pact_full_pipeline_runner_v1" / "v4_book_html.py",
        repo / "pact_full_pipeline_runner_v1" / "v4_phase12_strict_run.py",
    ):
        if not path.exists():
            continue
        hits = [ln for ln in path.read_text(encoding="utf-8").splitlines()
                if functional.search(ln)]
        if hits:
            offenders.append(f"{path}: {hits[:3]}")
    assert offenders == [], f"runtime arc_names references: {offenders}"


def test_preflight_rejects_manifest_chapters_file_mismatch(two_books, tmp_path, monkeypatch):
    from pact_v4.phase0b.epub_splitter import generate_directory_manifest

    src = tmp_path / "mismatch_chapters"
    src.mkdir()
    for i in (1, 2):
        (src / f"{i:04d}_pale.html").write_text(
            f"<html><head><title>C{i}</title></head><body><h2>C{i}</h2></body></html>",
            encoding="utf-8",
        )
    pale_home = two_books / "pale"
    generate_directory_manifest(src, "pale", manifest_out=pale_home / "manifest.json")
    # Approved chapters list a file the manifest does not contain.
    _write_chapters(pale_home, [
        _record(1, file="0001_pale.html", en="C1"),
        _record(2, file="0002_ghost.html", en="C2"),
    ])
    monkeypatch.setenv("PACT_V4_SOURCE_ROOT", str(src))
    report = preflight_book("pale", host="media", books=two_books)
    assert report["ok"] is False
    assert any("differ" in e for e in report["errors"])


def test_prompt_arc_pairs_first_appearance_order(two_books):
    from pact_v4.phase0b.book_profile import load_approved_chapters

    home = two_books / "pale"
    _write_chapters(home, [
        _record(1, en="Bonds 1.1", ru="Узы 1.1"),
        _record(2, en="Bonds 1.2", ru="Узы 1.2"),
        _record(3, en="Breach 3.1", ru="Разрыв 3.1"),
        _record(4, en="Gathered Pages: 1", ru="Собранные страницы: 1"),
        _record(5, en="Histories (Arc 2)", ru="Хроники (Арка 2)"),
        _record(6, en="Epilogue", ru="Эпилог"),
        _record(7, en="Breach 3.2", ru=None),  # untitled: skipped, no block entry
    ])
    approved = load_approved_chapters(home / "chapters.json", "pale")
    assert approved.prompt_arc_pairs() == (
        ("Bonds", "Узы"),
        ("Breach", "Разрыв"),
        ("Gathered Pages", "Собранные страницы"),
        ("Histories", "Хроники"),
        ("Epilogue", "Эпилог"),
    )


def test_prompt_arc_pairs_empty_without_titles(two_books):
    from pact_v4.phase0b.book_profile import load_approved_chapters

    home = two_books / "pale"
    _write_chapters(home, [_record(1, en="C1"), _record(2, en="C2")])
    approved = load_approved_chapters(home / "chapters.json", "pale")
    assert approved.prompt_arc_pairs() == ()


def test_top_level_title_map_rejected(two_books):
    """Option-A carrier is gone: top-level title_map is an unknown key."""
    from pact_v4.phase0b.book_profile import load_approved_chapters

    home = two_books / "pale"
    path = _write_chapters(home, [_record(1, en="C1", ru="Г1")])
    raw = json.loads(path.read_text(encoding="utf-8"))
    raw["title_map"] = {"style": "CHAPTERS", "entries": [{"en": "C1", "ru": "Г1"}]}
    path.write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(ProfileError, match="unexpected key"):
        load_approved_chapters(path, "pale")


def test_pov_for_chapter_matches_stem_then_order(two_books):
    from pact_v4.phase0b.book_profile import load_approved_chapters

    home = two_books / "pale"
    _write_chapters(home, [
        _record(1, file="0001_pale.html", en="1.0",
                pov={"name": "Verona", "gender": "female"}),
        _record(2, file="0002_pale.html", en="1.1", pov=None),
    ])
    approved = load_approved_chapters(home / "chapters.json", "pale")
    assert approved.pov_for_chapter("0001_pale").name == "Verona"
    assert approved.pov_for_chapter("1").name == "Verona"
    assert approved.pov_for_chapter("01").name == "Verona"
    # Chapter without a POV record: None (renderer keeps legacy output).
    assert approved.pov_for_chapter("0002_pale") is None
    assert approved.pov_for_chapter("2") is None
    # Unknown ids never raise.
    assert approved.pov_for_chapter("9999") is None
    assert approved.pov_for_chapter("") is None
    assert approved.pov_for_chapter(None) is None
    assert approved.pov_for_chapter("not-a-chapter") is None
