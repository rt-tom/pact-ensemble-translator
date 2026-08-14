"""V4 book HTML renderer: final book.html from translations + source chapters.

Read-only consumer of already-finished artifacts (no model calls, no
pipeline access). For every chapter it takes the original EN chapter HTML
(parsed with ``parse_source_html`` — stable PID-indexed blocks in source
order) and the chapter's final translations and renders the chapter body:
each block keeps its source wrapper tag and attributes, its inner content
is replaced by the translated text, so paragraphs/lists/blockquotes keep
their structure and the translation's ``<em>`` italics survive. Blocks
whose pid is absent from translations are skipped with a warning in the
report — the renderer never crashes on missing data.

Two input layouts are supported:

* v4 chunked (legacy): translations live at ``<out-base>/chapter_<id>/
  translations.json`` — pass ``--chapters`` with the book order.
* v4.1 whole-chapter: each run dir ``<out-base>/run_<label>/`` holds its
  own ``translations.json`` (``{pid: text}``) plus
  ``strict_chapter_trial_record.json`` carrying the run's ``chapter_id``.
  Pass ``--run-dirs`` (literal dirs and/or ``run_*`` glob patterns);
  ``chapter_id`` is resolved from the record (fallback: ``chapter_id``
  metadata inside ``translations.json``, then the run dir name). Chapter
  order = ``--chapters`` order when given, otherwise the resolved
  ``chapter_id`` order (natural sort).

The book is assembled as a single ``book.html``: chapters in the given
order, each inside a ``<section id="chapter-<id>">``, with a table of
contents built from the source ``h1``..``h6`` headings (nested by heading
level, anchored to the rendered headings).

CLI::

    python -m pact_full_pipeline_runner_v1.v4_book_html \\
        --out-base <dir> --chapters 0001 0002 0003 \\
        --chapter-html-pattern 'chapters/{chapter_id}.html' \\
        [--output book.html] [--title 'Книга'] [--report book_html_report.json]

    python -m pact_full_pipeline_runner_v1.v4_book_html \\
        --out-base <runs-dir> --run-dirs 'run_*' \\
        --chapter-html-pattern 'chapters/{chapter_id}.html'

Exit code is 0 when the book was written (missing pids are warnings);
non-zero only when a chapter could not be read at all.
"""
from __future__ import annotations

import argparse
import glob
import html
import json
import logging
import re
import sys
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from bs4 import BeautifulSoup, Tag

from pact_v4.phase0b.source_html import SourceBlock, parse_source_html

LOG = logging.getLogger(__name__)

BOOK_HTML_SCHEMA = "pact-v4-book-html/v1"

_HEADING_TAGS = {"h1", "h2", "h3", "h4", "h5", "h6"}

# The only inline markup the pipeline's Phase 5 formatting restores
# (``_INLINE_TAG_RE`` in ``pact_v4._integrity_checks``: em/strong/i/b/a).
# Any other tag in a translation is unwrapped to its visible text, and
# attributes outside the safe subset (or unsafe URLs) are dropped — the
# renderer must carry the book's italics, never arbitrary executable
# markup from a translations.json value.
_ALLOWED_INLINE_TAGS = frozenset({"em", "strong", "i", "b", "a"})
_ALLOWED_ATTRS = frozenset({"href", "title", "lang", "class"})
_SAFE_URL_SCHEMES = ("http:", "https:", "mailto:")


def natural_key(value: str) -> list:
    """Natural (numeric-aware) sort key, same pattern as elsewhere in the
    repo (v4_measurement_harness, compare_pipeline_review, ...).

    ``"0002"`` sorts before ``"0010"`` (numeric), and mixed ids like
    ``"run_2"`` vs ``"run_10"`` order by their numeric tail rather than
    lexicographically.
    """
    return [
        int(piece) if piece.isdigit() else piece.casefold()
        for piece in re.split(r"(\d+)", value)
    ]


def _sanitize_translation(text: str) -> str:
    """Allowlist-clean a translation fragment for safe HTML output.

    Keeps only the inline tags Phase 5 restores (``<em>``, ``<strong>``,
    ``<i>``, ``<b>``, ``<a>``) with a safe attribute subset; every other
    tag is unwrapped to its visible text (so ``<script>alert(1)</script>``
    becomes inert text) and event-handler / unsafe-href attributes are
    dropped. Visible text is otherwise unchanged.
    """
    soup = BeautifulSoup(text, "html.parser")
    for tag in soup.find_all(True):
        if tag.name not in _ALLOWED_INLINE_TAGS:
            tag.unwrap()
            continue
        for attr in list(tag.attrs):
            if attr not in _ALLOWED_ATTRS:
                del tag.attrs[attr]
                continue
            if attr == "href":
                value = str(tag.attrs[attr]).strip().lower()
                if not value.startswith(_SAFE_URL_SCHEMES):
                    del tag.attrs[attr]
    return str(soup)


# ---------------------------------------------------------------------------
# Chapter rendering
# ---------------------------------------------------------------------------


def render_block_html(
    block: SourceBlock,
    translation: str,
    *,
    element_id: Optional[str] = None,
) -> str:
    """Swap the source block's inner content for the translated text.

    The block's wrapper tag and attributes come from the source markup
    (``block.html``), so the structural role (paragraph, list item,
    blockquote, heading) is preserved; the translation is inserted as HTML
    after allowlist sanitizing, so its own ``<em>`` italics survive while
    arbitrary/executable markup from a translations.json value cannot reach
    the output.
    """
    soup = BeautifulSoup(block.html, "html.parser")
    root = next((t for t in soup.children if isinstance(t, Tag)), None)
    if root is None:
        root = soup.new_tag(block.tag)
    root.clear()
    fragment = BeautifulSoup(_sanitize_translation(translation), "html.parser")
    root.extend(fragment.contents)
    if element_id:
        root["id"] = element_id
    return str(root)


def _heading_level(block: SourceBlock) -> Optional[int]:
    if block.structural_role == "heading" and block.tag in _HEADING_TAGS:
        return int(block.tag[1])
    return None


def render_chapter_body(
    source_html_text: str,
    translations: Mapping[str, str],
    *,
    chapter_id: str = "",
    arc_names: Optional[Mapping[str, str]] = None,
) -> Tuple[str, Dict[str, Any]]:
    """Render one chapter body from its source HTML and translations.

    Returns ``(body_html, report)`` where report carries ``blocks_total``,
    ``rendered``, ``missing_pids`` (skipped, never fatal) and ``headings``
    (``{level, text, anchor}`` for the rendered heading blocks, in source
    order).

    P1 АРКИ (owner decision 2026-08-14): when ``arc_names`` (the
    ``arc_names.json`` mapping, e.g. ``{"Bonds": "Узы", ...}``) is given, a
    heading whose text starts with an arc key gets the deterministic Russian
    arc name substituted — «Узы 1.3» instead of whatever the model produced
    for «Bonds 1.3». 0 tokens, 0 stochasticity, 100% consistency.
    """
    blocks = parse_source_html(source_html_text)
    body_parts: List[str] = []
    headings: List[Dict[str, Any]] = []
    missing_pids: List[str] = []
    for block in blocks:
        text = translations.get(block.pid)
        if text is None or not text.strip():
            missing_pids.append(block.pid)
            continue
        level = _heading_level(block)
        element_id: Optional[str] = None
        if level is not None:
            anchor = f"ch-{chapter_id}-h{len(headings) + 1}" if chapter_id else \
                f"h{len(headings) + 1}"
            element_id = anchor
            heading_text = BeautifulSoup(text, "html.parser").get_text(" ", strip=True)
            substituted = _substitute_arc_name(heading_text, arc_names)
            headings.append({
                "level": level,
                "text": substituted,
                "anchor": anchor,
            })
            # RV finding 3 (MEDIUM): the RENDERED heading body must carry
            # the SAME deterministic arc substitution as the TOC metadata —
            # never a divergent raw model text. The substitution is applied
            # markup-preservingly to the raw heading text (inline <em>/<a>
            # survive) and render_block_html below sanitizes it as usual, so
            # body and TOC agree on one substitution and the inline-markup
            # contract is unchanged.
            text = _substitute_arc_name_html(text, arc_names)
        body_parts.append(render_block_html(block, text, element_id=element_id))
    report = {
        "blocks_total": len(blocks),
        "rendered": len(body_parts),
        "missing_pids": missing_pids,
        "headings": headings,
    }
    return "\n".join(body_parts), report


def _substitute_arc_name(
    heading_text: str,
    arc_names: Optional[Mapping[str, str]],
) -> str:
    """Replace a leading arc key in ``heading_text`` with its Russian name.

    Deterministic metadata (P1 АРКИ, owner decision 2026-08-14): a heading
    like ``Bonds 1.3`` becomes ``Узы 1.3`` when ``arc_names`` maps
    ``Bonds -> Узы``. Case-insensitive on the arc key; the rest of the
    heading (chapter number, subtitle) is preserved. No mapping / no match
    => the text is returned unchanged.
    """
    if not arc_names or not heading_text:
        return heading_text
    lowered = heading_text.casefold()
    for key, russian in arc_names.items():
        if not key:
            continue
        # Match the arc key as a leading token (followed by a space or the
        # end of the string), case-insensitive — "Bonds 1.3" or "Bonds".
        if lowered == key.casefold() or lowered.startswith(key.casefold() + " "):
            return f"{russian}{heading_text[len(key):]}"
    return heading_text


def _substitute_arc_name_html(
    raw_text: str,
    arc_names: Optional[Mapping[str, str]],
) -> str:
    """Apply the arc substitution to RAW heading text (markup-preserving).

    Same deterministic match as ``_substitute_arc_name`` (leading arc key,
    case-insensitive), but applied to the raw translation string so inline
    markup survives: ``<em>Bonds</em> 1.3`` -> ``<em>Узы</em> 1.3``. The
    result still flows through ``render_block_html``'s sanitizer, so the
    sanitization/inline-markup contract is unchanged. No mapping / no match
    => the text is returned unchanged.
    """
    if not arc_names or not raw_text:
        return raw_text
    lowered = BeautifulSoup(raw_text, "html.parser").get_text(" ", strip=True).casefold()
    for key, russian in arc_names.items():
        if not key:
            continue
        if lowered == key.casefold() or lowered.startswith(key.casefold() + " "):
            # Replace the leading arc key token in the RAW text (allowing
            # leading whitespace / inline tags before it), preserving the
            # rest of the markup: "<em>Bonds</em> 1.3" -> "<em>Узы</em> 1.3".
            pattern = re.compile(
                r"^(\s*(?:<[^>]*>\s*)*)" + re.escape(key) + r"(?=\s|$|<)",
                re.IGNORECASE,
            )
            match = pattern.match(raw_text)
            if match:
                return f"{match.group(1)}{russian}{raw_text[match.end():]}"
            # The key is not a literal leading token in the raw text (e.g.
            # it is entity-encoded or nested oddly) — degrade to the plain
            # substitution so body and TOC still agree deterministically.
            plain = BeautifulSoup(raw_text, "html.parser").get_text(" ", strip=True)
            return f"{russian}{plain[len(key):]}"
    return raw_text


def _load_arc_names(path: Optional[Path]) -> Optional[Dict[str, str]]:
    """Load ``arc_names.json`` (P1 АРКИ) or ``None`` when unavailable.

    ``path`` is the explicit ``--arc-names`` argument; when None, the
    current working directory's ``arc_names.json`` is tried. A missing or
    unreadable file yields ``None`` (headings are rendered unchanged — the
    arc substitution is an enhancement, never a failure). Non-str values
    are dropped; a malformed payload yields ``None``.
    """
    candidates = [path] if path is not None else [Path.cwd() / "arc_names.json"]
    for candidate in candidates:
        if candidate is None or not candidate.exists():
            continue
        try:
            raw = json.loads(candidate.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            continue
        if not isinstance(raw, dict):
            continue
        return {str(k): str(v) for k, v in raw.items() if isinstance(v, str)}
    return None


# ---------------------------------------------------------------------------
# Book assembly
# ---------------------------------------------------------------------------


def _toc_html(headings: Sequence[Dict[str, Any]]) -> str:
    """Nested ``<ul>`` TOC from heading records (``level``/``text``/``anchor``).

    Levels are the source h-tag levels (h1 = 1, ...); deeper headings nest
    under shallower ones, mirroring the source heading hierarchy. Each entry
    anchors to the rendered heading's id.
    """
    out: List[str] = []
    stack: List[int] = []
    for h in headings:
        level = int(h["level"])
        while stack and stack[-1] >= level:
            out.append("</li></ul>")
            stack.pop()
        if not stack:
            out.append("<ul>")
            stack.append(level)
        elif stack[-1] < level:
            out.append("<ul>")
            stack.append(level)
        else:
            out.append("</li>")
        out.append(
            f'<li><a href="#{h["anchor"]}">{html.escape(str(h["text"]))}</a>'
        )
    while stack:
        out.append("</li></ul>")
        stack.pop()
    return "".join(out)


def build_book_html(
    chapters: Sequence[Dict[str, Any]],
    *,
    title: str = "Книга",
) -> str:
    """Assemble the full ``book.html`` document.

    ``chapters`` is a list of ``{chapter_id, body_html, headings}`` records
    (the per-chapter render report). The TOC is included only when at least
    one heading exists.
    """
    toc_entries = [h for ch in chapters for h in ch.get("headings", [])]
    toc_block = ""
    if toc_entries:
        toc_block = f'<nav id="toc"><h2>Оглавление</h2>{_toc_html(toc_entries)}</nav>'

    sections = []
    for ch in chapters:
        body = ch.get("body_html", "")
        if not body:
            continue
        sections.append(
            f'<section id="chapter-{html.escape(str(ch["chapter_id"]))}">\n'
            f"{body}\n</section>"
        )

    return (
        "<!DOCTYPE html>\n"
        '<html lang="ru">\n<head>\n'
        '<meta charset="utf-8">\n'
        f"<title>{html.escape(title)}</title>\n"
        "<style>body{font-family:Georgia,serif;max-width:800px;margin:2em auto;"
        "line-height:1.6}section{margin-bottom:2.5em}</style>\n"
        "</head>\n<body>\n"
        f"<h1>{html.escape(title)}</h1>\n"
        f"{toc_block}\n"
        "<main>\n"
        + "\n".join(sections)
        + "\n</main>\n</body>\n</html>\n"
    )


# ---------------------------------------------------------------------------
# Book assembly from disk artifacts
# ---------------------------------------------------------------------------


def _load_translations(path: Path) -> Tuple[Dict[str, str], Optional[str]]:
    """Load ``translations.json``; ``(map, error)`` — never raises.

    Accepts the flat v4 form ``{pid: text}`` and the v4.1 whole-chapter
    envelope ``{"chapter_id": ..., "translations": {pid: text}}`` (metadata
    keys are ignored). Missing or corrupt file yields ``({}, error)`` so
    the caller can render what exists and record the warning instead of
    crashing.
    """
    if not path.exists():
        return {}, f"translations.json отсутствует: {path}"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (ValueError, OSError) as exc:
        return {}, f"translations.json повреждён: {path} ({exc})"
    if not isinstance(data, dict):
        return {}, f"translations.json не является объектом {{pid: text}}: {path}"
    payload = data.get("translations") if isinstance(data.get("translations"), dict) \
        else data
    translations = {
        str(pid): str(text) for pid, text in payload.items()
        if isinstance(text, str) and str(text).strip()
    }
    return translations, None


def _chapter_id_from_run_metadata(run_dir: Path) -> Optional[str]:
    """Resolve ``chapter_id`` for a v4.1 run dir.

    Reads ``strict_chapter_trial_record.json`` (authoritative) and falls
    back to ``chapter_id`` metadata inside ``translations.json``. Returns
    ``None`` when neither is available/carrying a non-empty id.
    """
    record_path = run_dir / "strict_chapter_trial_record.json"
    if record_path.exists():
        try:
            record = json.loads(record_path.read_text(encoding="utf-8"))
            if isinstance(record, dict) and record.get("chapter_id"):
                return str(record["chapter_id"])
        except (ValueError, OSError):
            pass
    translations_path = run_dir / "translations.json"
    if translations_path.exists():
        try:
            data = json.loads(translations_path.read_text(encoding="utf-8"))
            if isinstance(data, dict) and data.get("chapter_id"):
                return str(data["chapter_id"])
        except (ValueError, OSError):
            pass
    return None


def _resolve_run_dirs(out_base: Path, run_dirs: Sequence[Any]) -> List[Path]:
    """Expand ``run_dirs`` entries into concrete directories.

    Each entry is either a literal path (absolute, or relative to
    ``out_base``) or a glob pattern (``run_*``); patterns are expanded and
    sorted. Missing literal entries stay in the list — the caller turns
    them into per-chapter errors, mirroring the legacy behavior for a
    missing chapter dir. Duplicates (e.g. the same dir matched by two
    patterns) are de-duplicated, first occurrence wins.
    """
    resolved: List[Path] = []
    seen: set = set()
    for entry in run_dirs:
        pattern = Path(str(entry))
        if not pattern.is_absolute():
            pattern = out_base / pattern
        glob_chars = any(ch in str(pattern) for ch in "*?[")
        candidates = sorted(glob.glob(str(pattern))) if glob_chars else [str(pattern)]
        for candidate in candidates:
            candidate_path = Path(candidate)
            if candidate_path in seen:
                continue
            seen.add(candidate_path)
            resolved.append(candidate_path)
    return resolved


def render_book(
    *,
    out_base: Path,
    chapter_ids: Sequence[str],
    chapter_html_pattern: str,
    output: Optional[Path] = None,
    title: str = "Книга",
    report_path: Optional[Path] = None,
    run_dirs: Optional[Sequence[Any]] = None,
    arc_names: Optional[Mapping[str, str]] = None,
) -> Dict[str, Any]:
    """Assemble ``book.html`` from per-chapter artifacts on disk.

    Two input layouts (see module docstring):

    * legacy v4 chunked: reads ``<out-base>/chapter_<id>/translations.json``
      for each ``chapter_ids`` entry (book order);
    * v4.1 whole-chapter (``run_dirs`` given): each run dir's
      ``translations.json`` is the chapter's translation; ``chapter_id`` is
      resolved from ``strict_chapter_trial_record.json`` (fallback:
      ``chapter_id`` metadata in ``translations.json``, then the run dir
      name). Chapter order = ``chapter_ids`` order when provided (book
      order), otherwise the resolved ``chapter_id`` order (natural sort —
      independent of the run-dir glob/insertion order).

    Source HTML is resolved from ``chapter_html_pattern`` (``{chapter_id}``
    placeholder). Returns the report dict (also written to ``report_path``
    if given / default ``<out-base>/book_html_report.json``).
    """
    out_base = Path(out_base)
    book_path = Path(output) if output else out_base / "book.html"
    chapters: List[Dict[str, Any]] = []
    warnings: List[str] = []
    errors: List[str] = []

    if run_dirs is not None:
        runs = _resolve_run_dirs(out_base, run_dirs)
        # chapter_id -> run dir, with a fallback to the dir name and a
        # warning when neither the record nor translations metadata carry it.
        id_to_run: Dict[str, Path] = {}
        for run_dir in runs:
            if not run_dir.is_dir():
                errors.append(f"run-директория отсутствует: {run_dir}")
                continue
            chapter_id = _chapter_id_from_run_metadata(run_dir)
            if chapter_id is None:
                chapter_id = run_dir.name
                warnings.append(
                    f"run {run_dir.name}: chapter_id отсутствует в "
                    "strict_chapter_trial_record.json/translations.json — "
                    f"использую имя директории ({chapter_id})"
                )
            id_to_run.setdefault(chapter_id, run_dir)
        # Book order: chapter_ids when given, else the resolved chapter_id
        # order (natural sort — NOT the run-dir glob/insertion order, which
        # can disagree with the ids when a run dir name is unrelated).
        ordered_ids = list(chapter_ids) if chapter_ids else \
            sorted(id_to_run, key=natural_key)
        for chapter_id in ordered_ids:
            run_dir = id_to_run.get(chapter_id)
            if run_dir is None:
                chapter_record: Dict[str, Any] = {
                    "chapter_id": chapter_id,
                    "source": str(Path(
                        chapter_html_pattern.format(chapter_id=chapter_id)
                    )),
                }
                chapter_record["error"] = (
                    f"нет run-директории с chapter_id {chapter_id}"
                )
                errors.append(chapter_record["error"])
                chapters.append(chapter_record)
                continue
            _append_rendered_chapter(
                chapters, warnings, errors,
                chapter_id=chapter_id,
                source_path=Path(
                    chapter_html_pattern.format(chapter_id=chapter_id)
                ),
                translations_path=run_dir / "translations.json",
                translations_label=str(run_dir / "translations.json"),
                arc_names=arc_names,
            )
    else:
        for chapter_id in chapter_ids:
            _append_rendered_chapter(
                chapters, warnings, errors,
                chapter_id=chapter_id,
                source_path=Path(
                    chapter_html_pattern.format(chapter_id=chapter_id)
                ),
                translations_path=out_base / f"chapter_{chapter_id}" / "translations.json",
                translations_label=str(
                    out_base / f"chapter_{chapter_id}" / "translations.json"
                ),
                arc_names=arc_names,
            )

    book_html = build_book_html(chapters, title=title)
    book_path.parent.mkdir(parents=True, exist_ok=True)
    book_path.write_text(book_html, encoding="utf-8")

    report = {
        "schema": BOOK_HTML_SCHEMA,
        "book_path": str(book_path),
        "title": title,
        "chapters": [
            {k: v for k, v in ch.items() if k != "body_html"} for ch in chapters
        ],
        "warnings": warnings,
        "errors": errors,
    }
    report_target = Path(report_path) if report_path else \
        out_base / "book_html_report.json"
    report_target.parent.mkdir(parents=True, exist_ok=True)
    report_target.write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8",
    )
    return report


def _append_rendered_chapter(
    chapters: List[Dict[str, Any]],
    warnings: List[str],
    errors: List[str],
    *,
    chapter_id: str,
    source_path: Path,
    translations_path: Path,
    translations_label: str,
    arc_names: Optional[Mapping[str, str]] = None,
) -> None:
    """Render one chapter from disk and append its record to ``chapters``.

    Shared by the legacy chunked and v4.1 whole-chapter paths: missing
    source HTML is a per-chapter error (the book still renders the rest);
    missing/corrupt translations are a warning with the chapter body
    skipped block-by-block.
    """
    chapter_record: Dict[str, Any] = {
        "chapter_id": chapter_id,
        "source": str(source_path),
        "translations": translations_label,
    }
    if not source_path.exists():
        chapter_record["error"] = f"исходный HTML отсутствует: {source_path}"
        errors.append(chapter_record["error"])
        chapters.append(chapter_record)
        return
    try:
        source_text = source_path.read_text(encoding="utf-8-sig")
    except OSError as exc:
        chapter_record["error"] = f"не удалось прочитать исходный HTML: {exc}"
        errors.append(chapter_record["error"])
        chapters.append(chapter_record)
        return

    translations, load_error = _load_translations(translations_path)
    if load_error:
        warnings.append(f"глава {chapter_id}: {load_error}")
        chapter_record["warnings"] = [load_error]

    body_html, render_report = render_chapter_body(
        source_text, translations, chapter_id=chapter_id, arc_names=arc_names,
    )
    chapter_record.update(render_report)
    for pid in render_report["missing_pids"]:
        warnings.append(
            f"глава {chapter_id}: pid {pid} отсутствует в translations.json "
            "— блок пропущен"
        )
    chapter_record["body_html"] = body_html
    chapters.append(chapter_record)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def build_argparser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="V4 book HTML renderer: book.html из translations.json "
                    "+ исходные главы",
    )
    parser.add_argument("--out-base", required=True, type=Path,
                        help="база артефактов (chapter_<id>/ или run_*/)")
    parser.add_argument("--chapters", nargs="+", default=None,
                        help="список id глав в порядке книги (v4 chunked; "
                             "для run-режима — порядок глав)")
    parser.add_argument("--run-dirs", nargs="+", default=None,
                        help="v4.1 whole-chapter: run-директории или паттерн "
                             "run_* (chapter_id из strict_chapter_trial_record.json)")
    parser.add_argument("--chapter-html-pattern", required=True,
                        help="шаблон пути к исходному HTML с {chapter_id}")
    parser.add_argument("--output", type=Path, default=None,
                        help="путь к book.html (default: <out-base>/book.html)")
    parser.add_argument("--title", default="Книга",
                        help="заголовок книги (default: Книга)")
    parser.add_argument("--report", type=Path, default=None,
                        help="путь к отчёту (default: <out-base>/book_html_report.json)")
    parser.add_argument("--arc-names", type=Path, default=None,
                        help="arc_names.json (P1 АРКИ): детерминированная "
                             "подстановка русских названий арков в заголовки "
                             "(default: <cwd>/arc_names.json если существует)")
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_argparser().parse_args(argv)
    if args.run_dirs is None and not args.chapters:
        print("нужно указать --chapters (v4) или --run-dirs (v4.1)",
              file=sys.stderr)
        return 2
    arc_names = _load_arc_names(args.arc_names)
    report = render_book(
        out_base=args.out_base,
        chapter_ids=args.chapters or [],
        chapter_html_pattern=args.chapter_html_pattern,
        output=args.output,
        title=args.title,
        report_path=args.report,
        run_dirs=args.run_dirs,
        arc_names=arc_names,
    )
    rendered = sum(1 for ch in report["chapters"] if ch.get("rendered", 0) > 0)
    print(
        f"book.html: {report['book_path']} "
        f"(глав: {len(report['chapters'])}, отрендерено: {rendered}, "
        f"предупреждений: {len(report['warnings'])}, ошибок: {len(report['errors'])})"
    )
    for warning in report["warnings"]:
        print(f"  warning: {warning}", file=sys.stderr)
    for error in report["errors"]:
        print(f"  error: {error}", file=sys.stderr)
    return 1 if report["errors"] else 0


if __name__ == "__main__":
    sys.exit(main())
