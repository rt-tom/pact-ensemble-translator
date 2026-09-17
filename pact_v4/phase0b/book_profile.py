"""V5 slice-1: book profile (``books/<slug>/book.yaml``) — data, not code.

Every book, including Pact, is described by a profile: human-readable
``slug`` (alias), content kind + language pair gates, per-host
source/state/out roots, the reviewed source ``manifest.json``, the
approved ``chapters.json`` (title/POV authority), the snapshot
``media_book_id`` namespace, and the quality policy selectors.

Fail-closed rules enforced here (before output/state/model activity):

* slice-1 accepts only ``content_kind: prose`` and the ``en/ru`` pair;
  anything else (or an unknown policy value) fails preflight;
* a profile's manifest/chapters paths must live inside its own
  ``books/<slug>/`` directory — a Pale profile resolving Pact-owned
  metadata aborts with an isolation error instead of falling back;
* state roots and ``media_book_id`` must be pairwise distinct across all
  known books (Pale ``book_id=2`` can never inherit Pact state);
* the approved ``chapters.json`` must never live inside snapshot state
  (exact-four boundary untouched: ``chapters.json`` lives in
  ``books/<slug>/``, never in ``state/``);
* the legacy arc-names sidecar is not a runtime input — this module never
  reads it (grep-test enforced).

The approved ``chapters.json`` sha256 feeds the resolved
profile/prompt identity (tasks §3.1).
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import stat as _stat
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Optional

PROFILE_SCHEMA_VERSION = "pact-v5-book-profile/v1"
CHAPTERS_SCHEMA_VERSION = "pact-v5-approved-chapters/v1"

_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
_SNAPSHOT_DIR_NAMES = {"snapshots", "state", "incoming", "quarantine"}


class ProfileError(ValueError):
    """Fail-closed profile error (preflight/run refusal)."""


@dataclass(frozen=True)
class ChapterPov:
    name: str
    gender: Optional[str] = None  # "male" | "female" | None


@dataclass(frozen=True)
class ApprovedChapter:
    file: str
    order: int
    en_title: str
    ru_title: Optional[str]
    pov: Optional[ChapterPov]
    notes: str = ""


# Alternative B (owner decision): single derivation rule — the prompt block
# is unique arc pairs in book first-appearance order, extracted from the
# full-heading records by stripping the trailing number/designator
# (``Bonds 1.1`` -> ``Bonds``, ``Gathered Pages: 1`` /
# ``Gathered Pages (Arc 10)`` -> ``Gathered Pages``, ``Histories (Arc 2)`` /
# ``Histories 9`` -> ``Histories``, bare ``Epilogue`` unchanged; same rule
# for the RU side with ``(Арка N)``). Zero-chapter entries (Transgression,
# Sundown, bare Gathered) never appear — the drop rule is automatic.
_ARC_SUFFIX_RE = re.compile(
    r"\s*(?:\(\s*(?:Arc|Арка)\s*\d+\s*\)|:\s*\d+|\d+(?:\.\d+)?)\s*$"
)


def _strip_arc_suffix(heading: str) -> str:
    return _ARC_SUFFIX_RE.sub("", heading).strip()


@dataclass(frozen=True)
class ApprovedChapters:
    book_slug: str
    chapters: tuple
    sha256: str

    def title_map(self) -> tuple[tuple[str, str], ...]:
        """Deterministic {en_title: ru_title} map from non-empty records.

        Chapter order, first occurrence wins. The sole source for heading
        substitution (``v4_book_html``) and for the neutral prompt block
        (rendered only when non-empty).
        """
        pairs: list[tuple[str, str]] = []
        seen: set[str] = set()
        for chapter in self.chapters:
            if chapter.ru_title is not None and chapter.ru_title.strip() and chapter.en_title not in seen:
                seen.add(chapter.en_title)
                pairs.append((chapter.en_title, chapter.ru_title))
        return tuple(pairs)

    def prompt_arc_pairs(self) -> tuple[tuple[str, str], ...]:
        """Unique arc pairs for the generation prompt block (ordered).

        Single derivation (alternative B): per-chapter records in book
        order, arc extracted by stripping the number/designator suffix;
        first occurrence wins. Empty map (e.g. Pale pilot, no approved
        ``ru_title``) means no prompt block — free title translation.
        Fail-closed: a record whose heading yields an empty arc, or whose
        RU side is missing, raises (reviewed data must parse cleanly).
        """
        pairs: list[tuple[str, str]] = []
        seen: set[str] = set()
        for chapter in self.chapters:
            if chapter.ru_title is None or not chapter.ru_title.strip():
                continue
            en_arc = _strip_arc_suffix(chapter.en_title)
            ru_arc = _strip_arc_suffix(chapter.ru_title)
            if not en_arc or not ru_arc:
                raise ProfileError(
                    f"approved chapters: cannot extract arc pair from "
                    f"{chapter.file!r} ({chapter.en_title!r})"
                )
            if en_arc not in seen:
                seen.add(en_arc)
                pairs.append((en_arc, ru_arc))
        return tuple(pairs)

    def pov_by_order(self) -> dict[int, ChapterPov]:
        return {c.order: c.pov for c in self.chapters if c.pov is not None}

    def pov_for_chapter(self, chapter_id: object) -> Optional[ChapterPov]:
        """Validated per-chapter POV for a run chapter id (or None).

        Matches the manifest file stem first (``0001_pale`` ==
        ``0001_pale.html``), then the numeric order (``1``/``01`` ==
        order 1). Never raises: unknown ids yield None (the renderer then
        keeps its legacy fail-soft output).
        """
        cid = str(chapter_id or "").strip()
        if not cid:
            return None
        for chapter in self.chapters:
            if chapter.pov is None:
                continue
            if Path(chapter.file).stem == cid:
                return chapter.pov
        if re.fullmatch(r"\d+", cid):
            try:
                number = int(cid)
            except ValueError:
                return None
            for chapter in self.chapters:
                if chapter.order == number and chapter.pov is not None:
                    return chapter.pov
        return None


@dataclass(frozen=True)
class BookProfile:
    slug: str
    title: str
    content_kind: str
    source_lang: str
    target_lang: str
    profile_path: Path
    source_manifest: Path  # absolute, inside books/<slug>/
    chapters_path: Path  # absolute, inside books/<slug>/
    media_book_id: str
    policy_hard_filters: str
    policy_editor_pass: str
    # Per-host roots (selected by execution host at resolve time).
    rt_source: Path
    media_source: Path
    rt_state: Path
    media_state: Path
    rt_out: Path
    media_out: Path


@dataclass(frozen=True)
class ResolvedBook:
    profile: BookProfile
    host: str  # "rt" | "media"
    source_root: Path
    state_root: Path
    out_root: Path


def books_dir() -> Path:
    override = os.environ.get("PACT_V5_BOOKS_DIR")
    if override:
        return Path(override)
    return Path(__file__).resolve().parent.parent.parent / "books"


def _is_regular_file(path: Path) -> bool:
    try:
        st = path.lstat()
    except (FileNotFoundError, OSError):
        return False
    return _stat.S_ISREG(st.st_mode)


def _check_no_symlink_chain(path: Path) -> None:
    cur: Optional[Path] = path.absolute()
    seen: set[Path] = set()
    while cur is not None and cur not in seen:
        seen.add(cur)
        try:
            if cur.is_symlink():
                raise ProfileError(f"symlink in path chain rejected: {cur}")
        except OSError as exc:
            raise ProfileError(f"cannot stat path chain {cur}: {exc}") from exc
        parent = cur.parent
        if parent == cur:
            break
        cur = parent


def _load_yaml(path: Path) -> Any:
    try:
        import yaml  # type: ignore
    except ImportError as exc:
        raise ProfileError(f"{path}: book.yaml requires PyYAML") from exc
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ProfileError(f"cannot read {path}: {exc}") from exc
    try:
        return yaml.safe_load(raw)
    except Exception as exc:
        raise ProfileError(f"{path}: invalid YAML ({exc})") from exc


def load_profile(slug: str, *, books: Optional[Path] = None) -> BookProfile:
    """Load + validate ``books/<slug>/book.yaml`` (no I/O beyond the file)."""
    if not _SLUG_RE.match(slug):
        raise ProfileError(f"invalid book slug {slug!r}")
    root = books or books_dir()
    profile_path = root / slug / "book.yaml"
    _check_no_symlink_chain(profile_path)
    if not _is_regular_file(profile_path):
        raise ProfileError(f"book profile not found: {profile_path}")
    raw = _load_yaml(profile_path)
    if not isinstance(raw, dict):
        raise ProfileError(f"{profile_path}: profile must be a mapping")
    allowed_top = {
        "schema_version", "slug", "title", "content_kind",
        "source_lang", "target_lang", "source", "state", "out",
        "chapters", "policy",
    }
    for key in raw:
        if key not in allowed_top:
            raise ProfileError(f"{profile_path}: unexpected key {key!r}")
    if raw.get("schema_version") != PROFILE_SCHEMA_VERSION:
        raise ProfileError(
            f"{profile_path}: unsupported schema_version {raw.get('schema_version')!r}"
        )
    if raw.get("slug") != slug:
        raise ProfileError(f"{profile_path}: slug mismatch ({raw.get('slug')!r} != {slug!r})")
    title = raw.get("title")
    if not isinstance(title, str) or not title.strip():
        raise ProfileError(f"{profile_path}: title must be a non-empty string")

    content_kind = raw.get("content_kind")
    source_lang = raw.get("source_lang")
    target_lang = raw.get("target_lang")

    source = raw.get("source")
    state = raw.get("state")
    out = raw.get("out")
    if not isinstance(source, dict) or not isinstance(state, dict) or not isinstance(out, dict):
        raise ProfileError(f"{profile_path}: source/state/out must be mappings")
    for section, keys in ((source, ("rt_root", "media_root", "manifest")),
                          (state, ("media_book_id", "rt_root", "media_root")),
                          (out, ("rt_root", "media_root"))):
        for key in keys:
            if not isinstance(section.get(key), str) or not str(section.get(key)).strip():
                raise ProfileError(f"{profile_path}: {key} must be a non-empty string")

    profile_home = profile_path.parent.resolve()
    # manifest/chapters are written relative to the profile's own
    # books/<slug>/ directory; isolation requires them to stay inside it.
    manifest_rel = source["manifest"]
    manifest_path = (profile_home / manifest_rel).resolve() if not os.path.isabs(manifest_rel) else Path(manifest_rel).resolve()
    try:
        manifest_path.relative_to(profile_home)
    except ValueError:
        raise ProfileError(
            f"{profile_path}: source.manifest must live inside books/{slug}/ "
            f"(isolation; got {manifest_rel!r})"
        ) from None
    chapters_rel = raw.get("chapters")
    if not isinstance(chapters_rel, str) or not chapters_rel.strip():
        raise ProfileError(f"{profile_path}: chapters must be a non-empty string")
    chapters_path = (profile_home / chapters_rel).resolve() if not os.path.isabs(chapters_rel) else Path(chapters_rel).resolve()
    try:
        chapters_path.relative_to(profile_home)
    except ValueError:
        raise ProfileError(
            f"{profile_path}: chapters must live inside books/{slug}/ "
            f"(isolation; got {chapters_rel!r})"
        ) from None
    # chapters.json must never sit inside snapshot state (exact-four boundary).
    for part in chapters_path.parts:
        if part in _SNAPSHOT_DIR_NAMES and part != slug:
            raise ProfileError(
                f"{profile_path}: chapters must not live inside snapshot state (got {chapters_rel!r})"
            )
    # Also refuse a chapters path under any known state root.
    # (Checked fully at resolve time against sibling profiles.)

    policy = raw.get("policy")
    if not isinstance(policy, dict):
        raise ProfileError(f"{profile_path}: policy must be a mapping")
    hard_filters = policy.get("hard_filters")
    editor_pass = policy.get("editor_pass")
    if hard_filters not in ("v4-en-ru",):
        raise ProfileError(
            f"{profile_path}: unknown policy.hard_filters {hard_filters!r} (fail-closed)"
        )
    if editor_pass not in ("russian-editor-v1", "off"):
        raise ProfileError(
            f"{profile_path}: unknown policy.editor_pass {editor_pass!r} (fail-closed)"
        )

    media_book_id = str(state["media_book_id"]).strip()
    if not re.match(r"^[0-9]+$", media_book_id):
        raise ProfileError(f"{profile_path}: state.media_book_id must be numeric, got {media_book_id!r}")

    return BookProfile(
        slug=slug,
        title=str(title),
        content_kind=str(content_kind),
        source_lang=str(source_lang),
        target_lang=str(target_lang),
        profile_path=profile_path,
        source_manifest=manifest_path,
        chapters_path=chapters_path,
        media_book_id=media_book_id,
        policy_hard_filters=str(hard_filters),
        policy_editor_pass=str(editor_pass),
        rt_source=Path(str(source["rt_root"])),
        media_source=Path(str(source["media_root"])),
        rt_state=Path(str(state["rt_root"])),
        media_state=Path(str(state["media_root"])),
        rt_out=Path(str(out["rt_root"])),
        media_out=Path(str(out["media_root"])),
    )


def check_profile_gates(profile: BookProfile) -> None:
    """Slice-1 pair/kind gates: only prose + en/ru (fail-closed, no fallback)."""
    if profile.content_kind != "prose":
        raise ProfileError(
            f"book {profile.slug!r}: unsupported content_kind {profile.content_kind!r} "
            "(slice-1 accepts only 'prose')"
        )
    if (profile.source_lang, profile.target_lang) != ("en", "ru"):
        raise ProfileError(
            f"book {profile.slug!r}: unsupported pair "
            f"{profile.source_lang}/{profile.target_lang} (slice-1 accepts only en/ru; "
            "RU tables/editor never apply outside their declared pair)"
        )


def check_cross_book_isolation(slug: str, *, books: Optional[Path] = None) -> list[BookProfile]:
    """Pairwise isolation across all known books (fail-closed).

    State roots, source roots, media_book_id, manifest and chapters paths
    must be pairwise distinct; every profile's manifest/chapters must stay
    inside its own ``books/<slug>/`` (enforced at load). Returns all
    loaded profiles.
    """
    root = books or books_dir()
    profiles: list[BookProfile] = []
    if root.is_dir():
        for child in sorted(root.iterdir()):
            if child.is_dir() and (child / "book.yaml").exists():
                profiles.append(load_profile(child.name, books=root))
    wanted = [p for p in profiles if p.slug == slug]
    if not wanted:
        raise ProfileError(f"unknown book slug {slug!r}")
    seen_ids: dict[str, str] = {}
    seen_paths: dict[str, str] = {}
    for profile in profiles:
        if profile.media_book_id in seen_ids:
            raise ProfileError(
                f"isolation: media_book_id {profile.media_book_id} shared by "
                f"{seen_ids[profile.media_book_id]!r} and {profile.slug!r}"
            )
        seen_ids[profile.media_book_id] = profile.slug
        for label, path in (
            ("rt_state", profile.rt_state), ("media_state", profile.media_state),
            ("rt_source", profile.rt_source), ("media_source", profile.media_source),
            ("manifest", profile.source_manifest), ("chapters", profile.chapters_path),
        ):
            key = f"{label}:{path.resolve()}" if label in ("manifest", "chapters") else f"{label}:{path}"
            if key in seen_paths:
                raise ProfileError(
                    f"isolation: {label} {path} shared by "
                    f"{seen_paths[key]!r} and {profile.slug!r}"
                )
            seen_paths[key] = profile.slug
    # State roots must never coincide with another book's source/out roots.
    state_vals = {(p.rt_state, p.media_state) for p in profiles}
    for profile in profiles:
        for other in profiles:
            if other.slug == profile.slug:
                continue
            if profile.media_state == other.media_source or profile.rt_state == other.rt_source:
                raise ProfileError(
                    f"isolation: {profile.slug!r} state overlaps {other.slug!r} source"
                )
    _ = state_vals
    return profiles


def detect_host(host_hint: Optional[str] = None) -> str:
    if host_hint in ("rt", "media"):
        return host_hint  # type: ignore[return-value]
    env = os.environ.get("PACT_V4_HOST") or os.environ.get("PACT_EXEC_HOST")
    if env in ("rt", "media"):
        return env
    import sys as _sys

    return "rt" if _sys.platform == "win32" else "media"


def resolve_book(
    slug: str, *, host: Optional[str] = None, books: Optional[Path] = None
) -> ResolvedBook:
    """Resolve a book profile to host source/state/out roots.

    Env overrides (``PACT_V4_SOURCE_ROOT/STATE_ROOT/OUT_ROOT``, the test
    injection hooks) apply AFTER profile resolution and BEFORE isolation
    re-validation, so an override pointing Pale at Pact paths still fails
    closed.
    """
    execution_host = detect_host(host)
    check_cross_book_isolation(slug, books=books)
    profile = load_profile(slug, books=books)
    check_profile_gates(profile)
    if execution_host == "rt":
        source_root, state_root, out_root = profile.rt_source, profile.rt_state, profile.rt_out
    else:
        source_root, state_root, out_root = profile.media_source, profile.media_state, profile.media_out
    if os.environ.get("PACT_V4_SOURCE_ROOT"):
        source_root = Path(os.environ["PACT_V4_SOURCE_ROOT"])
    if os.environ.get("PACT_V4_STATE_ROOT"):
        state_root = Path(os.environ["PACT_V4_STATE_ROOT"])
    if os.environ.get("PACT_V4_OUT_ROOT"):
        out_root = Path(os.environ["PACT_V4_OUT_ROOT"])
    resolved = ResolvedBook(
        profile=profile, host=execution_host,
        source_root=source_root, state_root=state_root, out_root=out_root,
    )
    check_resolved_isolation(resolved, books=books)
    return resolved


def check_resolved_isolation(resolved: ResolvedBook, *, books: Optional[Path] = None) -> None:
    """Re-validate resolved roots against every OTHER book's paths (fail-closed)."""
    root = books or books_dir()
    others: list[BookProfile] = []
    if root.is_dir():
        for child in sorted(root.iterdir()):
            if child.is_dir() and child.name != resolved.profile.slug and (child / "book.yaml").exists():
                try:
                    others.append(load_profile(child.name, books=root))
                except ProfileError:
                    continue
    slug = resolved.profile.slug
    for other in others:
        for label, mine, theirs in (
            ("state", resolved.state_root, other.rt_state),
            ("state", resolved.state_root, other.media_state),
            ("source", resolved.source_root, other.rt_source),
            ("source", resolved.source_root, other.media_source),
        ):
            try:
                if mine.resolve() == theirs.resolve():
                    raise ProfileError(
                        f"isolation: book {slug!r} {label} {mine} belongs to book {other.slug!r}"
                    )
            except OSError:
                if mine == theirs:
                    raise ProfileError(
                        f"isolation: book {slug!r} {label} {mine} belongs to book {other.slug!r}"
                    )
        for theirs in (other.source_manifest, other.chapters_path):
            for mine in (resolved.profile.source_manifest, resolved.profile.chapters_path):
                if mine == theirs:
                    raise ProfileError(
                        f"isolation: book {slug!r} metadata {mine} belongs to book {other.slug!r}"
                    )


def load_approved_chapters(chapters_path: Path, expected_slug: str) -> ApprovedChapters:
    """Load + validate the approved chapters.json (title/POV authority).

    Strict schema: top-level ``{schema_version, book_slug, chapters}`` —
    no extra keys; every record ``{file, order, en_title, ru_title|null,
    pov|null, notes}``; orders contiguous from 1; files unique bare
    ``.html`` names. Titles never merge into ``glossary.json`` — this
    artifact is the ONLY title authority.
    """
    _check_no_symlink_chain(chapters_path)
    if not _is_regular_file(chapters_path):
        raise ProfileError(f"approved chapters not found: {chapters_path}")
    try:
        raw_text = chapters_path.read_text(encoding="utf-8")
        raw = json.loads(raw_text)
    except (ValueError, OSError) as exc:
        raise ProfileError(f"approved chapters invalid JSON: {chapters_path} ({exc})") from exc
    if not isinstance(raw, dict):
        raise ProfileError("approved chapters must be a JSON object")
    for key in raw:
        if key not in ("schema_version", "book_slug", "chapters"):
            raise ProfileError(f"approved chapters: unexpected key {key!r}")
    if raw.get("schema_version") != CHAPTERS_SCHEMA_VERSION:
        raise ProfileError(
            f"approved chapters: unsupported schema_version {raw.get('schema_version')!r}"
        )
    if raw.get("book_slug") != expected_slug:
        raise ProfileError(
            f"approved chapters: book_slug {raw.get('book_slug')!r} != {expected_slug!r} (isolation)"
        )
    records = raw.get("chapters")
    if not isinstance(records, list) or not records:
        raise ProfileError("approved chapters: chapters must be a non-empty list")
    chapters: list[ApprovedChapter] = []
    seen_orders: set[int] = set()
    seen_files: set[str] = set()
    for idx, entry in enumerate(records):
        chapters.append(_parse_approved_record(entry, idx, seen_orders, seen_files))
    orders = sorted(seen_orders)
    if orders != list(range(1, len(chapters) + 1)):
        raise ProfileError(f"approved chapters: orders must be contiguous 1..{len(chapters)}")
    sha256 = hashlib.sha256(raw_text.encode("utf-8")).hexdigest()
    return ApprovedChapters(book_slug=expected_slug, chapters=tuple(chapters), sha256=sha256)


def _parse_approved_record(
    entry: Any, idx: int, seen_orders: set[int], seen_files: set[str]
) -> ApprovedChapter:
    if not isinstance(entry, dict):
        raise ProfileError(f"approved chapters[{idx}] must be an object")
    for key in entry:
        if key not in ("file", "order", "en_title", "ru_title", "pov", "notes"):
            raise ProfileError(f"approved chapters[{idx}]: unexpected key {key!r}")
    fname = entry.get("file")
    if not isinstance(fname, str) or not fname.strip() or "/" in fname or "\\" in fname:
        raise ProfileError(f"approved chapters[{idx}].file must be a bare filename")
    if not fname.endswith(".html"):
        raise ProfileError(f"approved chapters[{idx}].file must end with .html")
    if fname in seen_files:
        raise ProfileError(f"approved chapters: duplicate file {fname!r}")
    seen_files.add(fname)
    order = entry.get("order")
    if not isinstance(order, int) or isinstance(order, bool) or order < 1:
        raise ProfileError(f"approved chapters[{idx}].order must be a positive int")
    if order in seen_orders:
        raise ProfileError(f"approved chapters: duplicate order {order}")
    seen_orders.add(order)
    en_title = entry.get("en_title")
    if not isinstance(en_title, str) or not en_title.strip():
        raise ProfileError(f"approved chapters[{idx}].en_title must be non-empty")
    ru_title = entry.get("ru_title")
    if ru_title is not None and not isinstance(ru_title, str):
        raise ProfileError(f"approved chapters[{idx}].ru_title must be a string or null")
    if isinstance(ru_title, str) and not ru_title.strip():
        ru_title = None
    pov = entry.get("pov")
    parsed_pov: Optional[ChapterPov] = None
    if pov is not None:
        if not isinstance(pov, dict) or set(pov) - {"name", "gender"}:
            raise ProfileError(f"approved chapters[{idx}].pov must be {{name, gender?}} or null")
        name = pov.get("name")
        if not isinstance(name, str) or not name.strip():
            raise ProfileError(f"approved chapters[{idx}].pov.name must be non-empty")
        gender = pov.get("gender")
        if gender is not None and gender not in ("male", "female"):
            raise ProfileError(
                f"approved chapters[{idx}].pov.gender must be 'male'|'female'|null"
            )
        parsed_pov = ChapterPov(name=name.strip(), gender=gender)
    notes = entry.get("notes", "")
    if not isinstance(notes, str):
        raise ProfileError(f"approved chapters[{idx}].notes must be a string")
    return ApprovedChapter(
        file=fname, order=order, en_title=en_title,
        ru_title=ru_title, pov=parsed_pov, notes=notes,
    )


def preflight_book(
    slug: str, *, host: Optional[str] = None, books: Optional[Path] = None
) -> dict[str, Any]:
    """Offline profile preflight: gates + isolation + manifest/chapters readiness.

    No output/state/model side effects. Returns a JSON-serializable report
    dict with ``ok``, ``errors``, ``profile`` identity (including the
    approved chapters sha256) and resolved roots.
    """
    errors: list[str] = []
    report: dict[str, Any] = {"book": slug, "ok": False, "errors": errors}
    try:
        resolved = resolve_book(slug, host=host, books=books)
    except ProfileError as exc:
        errors.append(str(exc))
        return report
    profile = resolved.profile
    report["host"] = resolved.host
    report["roots"] = {
        "source": str(resolved.source_root),
        "state": str(resolved.state_root),
        "output": str(resolved.out_root),
    }
    report["media_book_id"] = profile.media_book_id
    report["policy"] = {
        "hard_filters": profile.policy_hard_filters,
        "editor_pass": profile.policy_editor_pass,
    }
    # Manifest readiness (validated, not just present).
    try:
        from pact_v4.phase0b.book_manifest import load_manifest, validate_source_manifest

        manifest = load_manifest(profile.source_manifest)
        if manifest.book_slug != slug:
            raise ProfileError(
                f"manifest book_slug {manifest.book_slug!r} != {slug!r} (isolation)"
            )
        chapter_paths = validate_source_manifest(resolved.source_root, manifest)
        report["manifest"] = {
            "path": str(profile.source_manifest),
            "chapters": len(manifest.chapters),
            "source_kind": manifest.source_kind,
            "source_hash": manifest.source_hash,
        }
    except (ProfileError, Exception) as exc:
        errors.append(f"manifest: {exc}")
        chapter_paths = {}
    # Approved chapters readiness (title/POV authority).
    try:
        approved = load_approved_chapters(profile.chapters_path, slug)
        report["approved_chapters"] = {
            "path": str(profile.chapters_path),
            "chapters": len(approved.chapters),
            "sha256": approved.sha256,
            "titled": sum(1 for c in approved.chapters if c.ru_title),
        }
    except ProfileError as exc:
        errors.append(f"chapters: {exc}")
    # Cross-check: manifest files == approved files (same chapter set).
    if not errors and chapter_paths:
        try:
            manifest_files = {c.file for c in manifest.chapters}
            approved_files = {c.file for c in approved.chapters}
            if manifest_files != approved_files:
                errors.append(
                    f"manifest/chapters file sets differ "
                    f"(manifest-only: {sorted(manifest_files - approved_files)[:3]}, "
                    f"chapters-only: {sorted(approved_files - manifest_files)[:3]})"
                )
        except ProfileError as exc:
            errors.append(str(exc))
    report["ok"] = not errors
    return report
