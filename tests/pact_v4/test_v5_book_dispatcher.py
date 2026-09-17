"""V5 slice-1: ``--book`` dispatcher contract (offline, mock backends only).

* omitted ``--book`` resolves ``pact`` through the SAME profile path as
  ``--book pact`` (identical layout/chapters/delegation);
* ``--book pale`` fails closed before output/state/model activity and
  creates no artifacts;
* unsupported pair/kind fails preflight without side effects;
* the pre-start manifest re-validation (same routine as preflight)
  refuses a run when sources change after preflight (TOCTOU).
"""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from pact_v4.runtime.runtime_config import PreflightCheck, PreflightReport


def _ok_report(kind="local_llama"):
    return PreflightReport(
        ok=True, kind=kind, identity_hash="abc123",
        public_record={"kind": kind}, model_bindings={}, effective_options={},
        checks=(PreflightCheck(name="exe", ok=True, detail="present"),),
        errors=(),
    )


@pytest.fixture()
def mini_books(tmp_path, monkeypatch):
    """Two-chapter 'pact' book in tmp: source + books dir + env overrides."""
    from pact_v4.phase0b.epub_splitter import generate_directory_manifest

    src = tmp_path / "pact_chapters"
    src.mkdir()
    for i, title in ((1, "Bonds 1.1"), (2, "Breach 3.1")):
        (src / f"{i:04d}_ch{i}.html").write_text(
            f"<html><head><title>{title}</title></head><body><h1>{title}</h1><p>x</p></body></html>",
            encoding="utf-8",
        )
    books = tmp_path / "books"
    for slug, book_id in (("pact", "1"), ("pale", "2")):
        home = books / slug
        home.mkdir(parents=True)
        (home / "book.yaml").write_text(
            "schema_version: pact-v5-book-profile/v1\n"
            f"slug: {slug}\ntitle: {slug.title()}\ncontent_kind: prose\n"
            "source_lang: en\ntarget_lang: ru\n"
            "source:\n"
            f"  rt_root: D:/pact/{slug}_chapters\n"
            f"  media_root: {src if slug == 'pact' else '/tmp/nope'}\n"
            "  manifest: manifest.json\n"
            "state:\n"
            f'  media_book_id: \"{book_id}\"\n'
            f"  rt_root: D:/pact/book_state_{slug}\n"
            f"  media_root: {tmp_path / ('mem-' + slug)}\n"
            "out:\n"
            "  rt_root: D:/pact/gate_bench_runs\n"
            f"  media_root: {tmp_path / 'outputs'}\n"
            "chapters: chapters.json\n"
            "policy:\n  hard_filters: v4-en-ru\n  editor_pass: russian-editor-v1\n",
            encoding="utf-8",
        )
    pact_home = books / "pact"
    generate_directory_manifest(src, "pact", manifest_out=pact_home / "manifest.json")
    (pact_home / "chapters.json").write_text(json.dumps({
        "schema_version": "pact-v5-approved-chapters/v1",
        "book_slug": "pact",
        "chapters": [
            {"file": "0001_ch1.html", "order": 1, "en_title": "Bonds 1.1",
             "ru_title": "Узы 1.1", "pov": {"name": "N", "gender": "male"}, "notes": ""},
            {"file": "0002_ch2.html", "order": 2, "en_title": "Breach 3.1",
             "ru_title": None, "pov": {"name": "N", "gender": "male"}, "notes": ""},
        ],
    }), encoding="utf-8")
    monkeypatch.setenv("PACT_V5_BOOKS_DIR", str(books))
    monkeypatch.setenv("PACT_V4_SOURCE_ROOT", str(src))
    monkeypatch.setenv("PACT_V4_STATE_ROOT", str(tmp_path / "mem-pact"))
    monkeypatch.setenv("PACT_V4_OUT_ROOT", str(tmp_path / "outputs"))
    (tmp_path / "mem-pact").mkdir()
    (tmp_path / "outputs").mkdir()
    return books


def _preflight_json(argv):
    from pact_full_pipeline_runner_v1 import v4_run

    with patch("pact_v4.runtime.runtime_config.run_runtime_preflight",
               return_value=_ok_report()):
        import io
        from contextlib import redirect_stdout

        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = v4_run.main(argv)
        assert rc == 0
        return json.loads(buf.getvalue())


def test_omitted_book_equals_explicit_pact(mini_books, capsys):
    a = _preflight_json(["book", "--chapters", "1-2", "--local", "--preflight", "--json"])
    b = _preflight_json(["book", "--book", "pact", "--chapters", "1-2", "--local", "--preflight", "--json"])
    assert a["book"] == b["book"]
    assert a["layout"] == b["layout"]
    assert a["resolved_chapters"] == {"0001": "0001_ch1.html", "0002": "0002_ch2.html"}
    assert a["book"]["slug"] == "pact" and a["book"]["media_book_id"] == "1"


def test_delegated_argv_identical_for_pact_and_omitted(mini_books):
    from pact_full_pipeline_runner_v1 import v4_run

    def _delegated(extra):
        with patch("pact_v4.runtime.runtime_config.run_runtime_preflight",
                   return_value=_ok_report()):
            with patch("pact_full_pipeline_runner_v1.v4_book_run.main") as mock_book:
                mock_book.return_value = 0
                rc = v4_run.main(["book", "--chapters", "1-2", "--local", *extra])
                assert rc == 0
                delegated = list(mock_book.call_args[0][0])
                # Auto-allocated --out-base carries a timestamp: compare the
                # stable prefix, then normalize for the equality check.
                idx = delegated.index("--out-base")
                out_base = Path(delegated[idx + 1])
                assert out_base.name.startswith("book_0001-0002_local_")
                delegated[idx + 1] = "<out-base>"
                return delegated

    assert _delegated([]) == _delegated(["--book", "pact"])


def test_delegation_carries_chapters_authority(mini_books):
    """Book runs forward the approved chapters.json + slug to every chapter."""
    from pact_full_pipeline_runner_v1 import v4_run

    from pact_full_pipeline_runner_v1.v4_phase12_strict_run import _load_title_map
    import argparse as _argparse

    with patch("pact_v4.runtime.runtime_config.run_runtime_preflight",
               return_value=_ok_report()):
        with patch("pact_full_pipeline_runner_v1.v4_book_run.main") as mock_book:
            mock_book.return_value = 0
            rc = v4_run.main(["book", "--chapters", "1", "--local"])
            assert rc == 0
            delegated = mock_book.call_args[0][0]
            assert "--chapters-json" in delegated
            chapters_json = delegated[delegated.index("--chapters-json") + 1]
            assert chapters_json.endswith("chapters.json")
            assert "--book-slug" in delegated
            slug = delegated[delegated.index("--book-slug") + 1]
            assert slug == "pact"
            # The forwarded authority loads through the strict loader:
            # unique arc pairs in book order for the CHAPTERS: block.
            pairs = _load_title_map(_argparse.Namespace(
                chapters_json=chapters_json, book_slug=slug))
            assert pairs == (("Bonds", "Узы"),)


def test_pale_preflight_fails_closed_without_artifacts(mini_books, tmp_path, capsys):
    from pact_full_pipeline_runner_v1 import v4_run

    out_root = tmp_path / "outputs"
    before = sorted(p.parent.name + "/" + p.name for p in out_root.rglob("*")) if out_root.exists() else []
    with pytest.raises(SystemExit) as exc:
        v4_run.main(["book", "--book", "pale", "--chapters", "1", "--local", "--preflight"])
    assert exc.value.code != 0
    after = sorted(p.parent.name + "/" + p.name for p in out_root.rglob("*")) if out_root.exists() else []
    assert before == after  # no output/state/model side effects


def test_unsupported_pair_fails_before_activity(mini_books, tmp_path, monkeypatch):
    from pact_full_pipeline_runner_v1 import v4_run

    bad = mini_books / "pale" / "book.yaml"
    text = bad.read_text(encoding="utf-8").replace("target_lang: ru", "target_lang: de")
    bad.write_text(text, encoding="utf-8")
    with pytest.raises(SystemExit) as exc:
        v4_run.main(["book", "--book", "pale", "--chapters", "1", "--local", "--preflight"])
    assert exc.value.code != 0


def test_unknown_book_slug_fails(mini_books):
    from pact_full_pipeline_runner_v1 import v4_run

    with pytest.raises(SystemExit) as exc:
        v4_run.main(["book", "--book", "nope", "--chapters", "1", "--local", "--preflight"])
    assert exc.value.code != 0


def test_pre_start_revalidation_refuses_mutated_sources(mini_books, tmp_path):
    """TOCTOU at the dispatcher: preflight passes, sources change, the run
    refuses BEFORE output/state/model activity (book_main never called)."""
    from pact_full_pipeline_runner_v1 import v4_run

    src = Path(__import__("os").environ["PACT_V4_SOURCE_ROOT"])
    with patch("pact_v4.runtime.runtime_config.run_runtime_preflight",
               return_value=_ok_report()):
        with patch("pact_full_pipeline_runner_v1.v4_book_run.main") as mock_book:
            mock_book.return_value = 0
            real_validate = __import__(
                "pact_v4.phase0b.book_manifest", fromlist=["validate_source_manifest"],
            ).validate_source_manifest

            calls = {"n": 0}

            def _flaky(root, manifest):
                calls["n"] += 1
                if calls["n"] == 1:
                    return real_validate(root, manifest)  # preflight: ok
                # Simulate a post-preflight mutation of chapter 1.
                (src / "0001_ch1.html").write_bytes(b"<html>mutated</html>")
                return real_validate(root, manifest)

            with patch("pact_v4.phase0b.book_manifest.validate_source_manifest",
                       side_effect=_flaky):
                with pytest.raises(SystemExit) as exc:
                    v4_run.main(["book", "--book", "pact", "--chapters", "1-2", "--local"])
                assert exc.value.code != 0
            assert not mock_book.called


# ---------------------------------------------------------------------------
# Strict title loader (approved chapters.json only, never the legacy sidecar)
# ---------------------------------------------------------------------------

# Owner-approved B1 derived block (alternative B): unique arc pairs in book
# first-appearance order; Transgression/Sundown/bare Gathered dropped
# (zero chapters, underivable). Mirrors the spec scenario.
_EXPECTED_ARC_PAIRS = [
    ("Bonds", "Узы"),
    ("Gathered Pages", "Собранные страницы"),
    ("Damages", "Ущерб"),
    ("Histories", "Хроники"),
    ("Breach", "Разрыв"),
    ("Collateral", "Залог"),
    ("Conviction", "Обвинение"),
    ("Subordination", "Подчинение"),
    ("Void", "Пустота"),
    ("Signature", "Подпись"),
    ("Null", "Нуль"),
    ("Mala Fide", "Mala Fide"),
    ("Malfeasance", "Злоупотребление"),
    ("Duress", "Принуждение"),
    ("Execution", "Казнь"),
    ("Sine Die", "Sine Die"),
    ("Possession", "Одержимость"),
    ("Judgment", "Суд"),
    ("Epilogue", "Эпилог"),
]


def _repo_pact_chapters():
    return Path(__file__).resolve().parents[2] / "books" / "pact" / "chapters.json"


def test_strict_loader_reads_migrated_pact_block():
    import argparse as _argparse

    from pact_full_pipeline_runner_v1.v4_phase12_strict_run import _load_title_map

    path = str(_repo_pact_chapters())
    if not Path(path).is_file():
        pytest.skip("requires migrated books/pact/chapters.json")
    pairs = _load_title_map(_argparse.Namespace(chapters_json=path, book_slug="pact"))
    assert pairs == tuple(_EXPECTED_ARC_PAIRS)


def test_strict_loader_slug_mismatch_fails_closed():
    import argparse as _argparse

    from pact_v4.phase0b.book_profile import ProfileError
    from pact_full_pipeline_runner_v1.v4_phase12_strict_run import _load_title_map

    path = str(_repo_pact_chapters())
    if not Path(path).is_file():
        pytest.skip("requires migrated books/pact/chapters.json")
    with pytest.raises(ProfileError):
        _load_title_map(_argparse.Namespace(chapters_json=path, book_slug="pale"))


def test_strict_loader_missing_file_is_soft():
    import argparse as _argparse

    from pact_full_pipeline_runner_v1.v4_phase12_strict_run import _load_title_map

    assert _load_title_map(_argparse.Namespace(chapters_json=None)) == ()
    assert _load_title_map(
        _argparse.Namespace(chapters_json="/tmp/does-not-exist.json")) == ()


# ---------------------------------------------------------------------------
# Strict per-chapter POV loader (same authority as the title map)
# ---------------------------------------------------------------------------

def test_strict_loader_chapter_pov_hit_and_miss(mini_books):
    import argparse as _argparse

    from pact_full_pipeline_runner_v1.v4_phase12_strict_run import _load_chapter_pov

    books = Path(__import__("os").environ["PACT_V5_BOOKS_DIR"])
    chapters = str(books / "pact" / "chapters.json")
    assert _load_chapter_pov(_argparse.Namespace(
        chapters_json=chapters, book_slug="pact", chapter_id="0001_ch1")) == ("N", "male")
    assert _load_chapter_pov(_argparse.Namespace(
        chapters_json=chapters, book_slug="pact", chapter_id="2")) == ("N", "male")
    assert _load_chapter_pov(_argparse.Namespace(
        chapters_json=chapters, book_slug="pact", chapter_id="9999")) == (None, None)
    assert _load_chapter_pov(_argparse.Namespace(
        chapters_json=None, book_slug="pact", chapter_id="0001_ch1")) == (None, None)


def test_strict_loader_chapter_pov_slug_mismatch_fails_closed(mini_books):
    import argparse as _argparse

    from pact_v4.phase0b.book_profile import ProfileError
    from pact_full_pipeline_runner_v1.v4_phase12_strict_run import _load_chapter_pov

    books = Path(__import__("os").environ["PACT_V5_BOOKS_DIR"])
    chapters = str(books / "pact" / "chapters.json")
    with pytest.raises(ProfileError):
        _load_chapter_pov(_argparse.Namespace(
            chapters_json=chapters, book_slug="pale", chapter_id="0001_ch1"))


# ---------------------------------------------------------------------------
# Advanced mode profile authority (HIGH fix: no legacy bypass)
# ---------------------------------------------------------------------------

def _add_pale_manifest(tmp_path, books, monkeypatch):
    """Pale source + manifest + approved chapters (Verona/Lucy POV)."""
    from pact_v4.phase0b.epub_splitter import generate_directory_manifest

    src = tmp_path / "pale_chapters"
    src.mkdir(exist_ok=True)
    for i, title in ((1, "1.0"), (2, "1.1")):
        (src / f"{i:04d}_pale.html").write_text(
            f"<html><head><title>{title}</title></head><body><h2>{title}</h2><p>x</p></body></html>",
            encoding="utf-8",
        )
    pale_home = books / "pale"
    generate_directory_manifest(src, "pale", manifest_out=pale_home / "manifest.json")
    (pale_home / "chapters.json").write_text(json.dumps({
        "schema_version": "pact-v5-approved-chapters/v1",
        "book_slug": "pale",
        "chapters": [
            {"file": "0001_pale.html", "order": 1, "en_title": "1.0",
             "ru_title": None,
             "pov": {"name": "Verona", "gender": "female"}, "notes": ""},
            {"file": "0002_pale.html", "order": 2, "en_title": "1.1",
             "ru_title": None,
             "pov": {"name": "Lucy", "gender": "female"}, "notes": ""},
        ],
    }), encoding="utf-8")
    return src


def _advanced_cfg(tmp_path):
    cfg = tmp_path / "remote.yaml"
    cfg.write_text(Path("configs/runtime_remote.example.yaml").read_text(), encoding="utf-8")
    return str(cfg)


def test_advanced_book_pale_fails_closed_without_manifest(mini_books, tmp_path, capsys):
    """Advanced --book pale without approved artifacts: isolation error,
    no output/state/model side effects (same fail-closed as simple)."""
    from pact_full_pipeline_runner_v1 import v4_run

    out_root = Path(__import__("os").environ["PACT_V4_OUT_ROOT"])
    before = sorted(str(p) for p in out_root.rglob("*")) if out_root.exists() else []
    with pytest.raises(SystemExit) as exc:
        v4_run.main(["book", "--book", "pale", "--chapters", "1",
                     "--runtime-config", _advanced_cfg(tmp_path), "--preflight"])
    assert exc.value.code != 0
    after = sorted(str(p) for p in out_root.rglob("*")) if out_root.exists() else []
    assert before == after


def test_advanced_book_pale_uses_profile_roots_and_authority(mini_books, tmp_path, monkeypatch):
    """Advanced --book pale resolves pale roots + manifest + chapters
    authority (no legacy pact source/state) and delegates chapter stems."""
    from pact_full_pipeline_runner_v1 import v4_run

    books = Path(__import__("os").environ["PACT_V5_BOOKS_DIR"])
    pale_src = _add_pale_manifest(tmp_path, books, monkeypatch)
    monkeypatch.setenv("PACT_V4_SOURCE_ROOT", str(pale_src))
    # The shared STATE_ROOT override (mem-pact) belongs to pact: pale runs
    # use pale's own state dir (the guard fails closed otherwise).
    (tmp_path / "mem-pale").mkdir(exist_ok=True)
    monkeypatch.setenv("PACT_V4_STATE_ROOT", str(tmp_path / "mem-pale"))
    with patch("pact_v4.runtime.runtime_config.run_runtime_preflight",
               return_value=_ok_report(kind="opencode_server")):
        with patch("pact_full_pipeline_runner_v1.v4_book_run.main") as mock_book:
            mock_book.return_value = 0
            rc = v4_run.main(["book", "--book", "pale", "--chapters", "1-2",
                              "--runtime-config", _advanced_cfg(tmp_path)])
            assert rc == 0
            delegated = mock_book.call_args[0][0]
            assert "0001_pale" in delegated and "0002_pale" in delegated
            assert "--chapters-json" in delegated
            assert delegated[delegated.index("--book-slug") + 1] == "pale"
            # Profile state root (book-2), never the legacy pact default.
            mem_idx = delegated.index("--memory-dir") + 1
            assert "book-2" in delegated[mem_idx] or "mem-pale" in delegated[mem_idx]
            assert "book-1" not in delegated[mem_idx]
            assert "pact_chapters" not in delegated[mem_idx]


def test_advanced_explicit_overrides_win_over_profile(mini_books, tmp_path, monkeypatch):
    """Explicit --memory-dir/--chapter-html-pattern win; metadata authority
    (chapters-json/slug) still flows to every chapter."""
    from pact_full_pipeline_runner_v1 import v4_run

    books = Path(__import__("os").environ["PACT_V5_BOOKS_DIR"])
    pale_src = _add_pale_manifest(tmp_path, books, monkeypatch)
    monkeypatch.setenv("PACT_V4_SOURCE_ROOT", str(pale_src))
    (tmp_path / "mem-pale").mkdir(exist_ok=True)
    monkeypatch.setenv("PACT_V4_STATE_ROOT", str(tmp_path / "mem-pale"))
    custom_mem = tmp_path / "custom-mem"
    custom_mem.mkdir()
    with patch("pact_v4.runtime.runtime_config.run_runtime_preflight",
               return_value=_ok_report(kind="opencode_server")):
        with patch("pact_full_pipeline_runner_v1.v4_book_run.main") as mock_book:
            mock_book.return_value = 0
            rc = v4_run.main(["book", "--book", "pale", "--chapters", "1-2",
                              "--runtime-config", _advanced_cfg(tmp_path),
                              "--memory-dir", str(custom_mem),
                              "--chapter-html-pattern", str(tmp_path / "{chapter_id}.html")])
            assert rc == 0
            delegated = mock_book.call_args[0][0]
            assert delegated[delegated.index("--memory-dir") + 1] == str(custom_mem)
            assert delegated[delegated.index("--chapter-html-pattern") + 1] == \
                str(tmp_path / "{chapter_id}.html")
            # Pattern override keeps zero-padded ids...
            assert "0001" in delegated and "0002" in delegated
            # ...while the chapters authority still applies.
            assert "--chapters-json" in delegated
            assert delegated[delegated.index("--book-slug") + 1] == "pale"


def test_advanced_book_pale_missing_chapter_fails(mini_books, tmp_path, monkeypatch):
    """Advanced manifest enforcement: chapters outside the manifest refuse
    before output/state/model activity."""
    from pact_full_pipeline_runner_v1 import v4_run

    books = Path(__import__("os").environ["PACT_V5_BOOKS_DIR"])
    pale_src = _add_pale_manifest(tmp_path, books, monkeypatch)
    monkeypatch.setenv("PACT_V4_SOURCE_ROOT", str(pale_src))
    (tmp_path / "mem-pale").mkdir(exist_ok=True)
    monkeypatch.setenv("PACT_V4_STATE_ROOT", str(tmp_path / "mem-pale"))
    with pytest.raises(SystemExit) as exc:
        v4_run.main(["book", "--book", "pale", "--chapters", "9",
                     "--runtime-config", _advanced_cfg(tmp_path), "--preflight"])
    assert exc.value.code != 0
