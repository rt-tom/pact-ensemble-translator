"""V5 slice-1: one-time Pact metadata migration (owner-reviewed, NOT runtime).

Derives the Pact profile artifacts from the current 150 HTML chapters,
``arc_names.json`` (15 legacy entries, UNCHANGED) plus the owner-approved
B1 RU arc table, and the established single narrator (Blake Thorburn,
male, first person — from the approved ``book_memory.json`` pov):

* ``books/pact/manifest.json`` — schema-versioned directory-input
  manifest (deterministic set hash), via ``epub_splitter.
  generate_directory_manifest``;
* ``books/pact/chapters.json`` — approved chapter records
  ``{file, order, en_title, ru_title, pov, notes}``. ``en_title`` is the
  source ``<title>``/heading; ``ru_title`` is the full RU heading (RU arc
  + original number format, e.g. ``Bonds 1.1`` → ``Узы 1.1``,
  ``Gathered Pages: 1`` → ``Собранные страницы: 1``,
  ``Histories (Arc 2)`` → ``Хроники (Арка 2)``). Every chapter matches —
  the extended table covers all arcs present in the book.

Single derivation rule (alternative B, owner decision): the prompt block
is derived ONLY from these records — unique arc pairs in book
first-appearance order, legacy ``CHAPTERS:`` label kept. Zero-chapter
entries (Transgression, Sundown, bare Gathered) are dropped: they cannot
be derived from records and have no chapters. No byte-regression is
required; the derived identity becomes the baseline (B4 waived).

Uncertain RU picks are flagged in the record ``notes`` for owner review:
Breach→Разрыв (alt. Пролом/Нарушение), Null→Нуль (alt. Ничто),
Malfeasance→Злоупотребление (alt. Злодеяние).

``arc_names.json`` is read here, at migration time, and NEVER by
runtime code afterwards.

Usage (media, worktree-only writes)::

    python -m pact_v4.phase0b.migrate_pact_metadata \
        --source-dir /home/rt/pact_chapters \
        --arc-names arc_names.json \
        --books-dir books

The owner reviews the diff before the artifacts take effect. Re-running
on unchanged input reproduces both files byte-for-byte.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Optional

from pact_v4.phase0b.book_profile import CHAPTERS_SCHEMA_VERSION
from pact_v4.phase0b.epub_splitter import SplitterError, generate_directory_manifest

PACT_POV_NAME = "Blake Thorburn"
PACT_POV_GENDER = "male"

# Owner-approved B1 RU arc table (alternative B). The 15 legacy
# arc_names.json entries are used UNCHANGED; these extend them. Data, not
# code — strings are trivially correctable at review.
B1_RU_ARC_TABLE: dict[str, str] = {
    "Breach": "Разрыв",
    "Signature": "Подпись",
    "Null": "Нуль",
    "Mala Fide": "Mala Fide",
    "Malfeasance": "Злоупотребление",
    "Duress": "Принуждение",
    "Sine Die": "Sine Die",
    "Gathered Pages": "Собранные страницы",
    "Epilogue": "Эпилог",
}

# Uncertain picks flagged in record notes for owner review.
B1_REVIEW_NOTES: dict[str, str] = {
    "Breach": "review: Breach→Разрыв uncertain (alt. Пролом/Нарушение)",
    "Null": "review: Null→Нуль uncertain (alt. Ничто)",
    "Malfeasance": "review: Malfeasance→Злоупотребление uncertain (alt. Злодеяние)",
}

_ARC_REMAINDER_RE = re.compile(r"\(Arc (\d+)\)")


def _load_arc_map(arc_names_path: Path) -> dict[str, str]:
    try:
        raw = json.loads(arc_names_path.read_text(encoding="utf-8"))
    except (ValueError, OSError) as exc:
        raise SplitterError(f"cannot read arc_names {arc_names_path}: {exc}") from exc
    if not isinstance(raw, dict):
        raise SplitterError(f"arc_names {arc_names_path} is not an object")
    pairs = {str(k): str(v) for k, v in raw.items() if isinstance(v, str) and str(k)}
    if not pairs:
        raise SplitterError(f"arc_names {arc_names_path} carries no pairs")
    for en, ru in B1_RU_ARC_TABLE.items():
        if en in pairs and pairs[en] != ru:
            raise SplitterError(
                f"B1 table conflicts with legacy arc_names entry {en!r}: "
                f"{pairs[en]!r} != {ru!r}"
            )
        pairs.setdefault(en, ru)
    return pairs


def _match_arc_key(en_title: str, arc_map: dict[str, str]) -> Optional[str]:
    """Longest leading arc key (case-insensitive) or None.

    The key must be followed by a boundary (space, colon, open paren, or
    end of string) so ``Gathered Pages`` wins over legacy ``Gathered`` on
    ``Gathered Pages: 1`` (colon boundary).
    """
    lowered = en_title.casefold()
    for key in sorted(arc_map, key=len, reverse=True):
        if not key:
            continue
        folded = key.casefold()
        if lowered == folded:
            return key
        rest = lowered[len(folded):]
        if lowered.startswith(folded) and rest[:1] in (" ", ":", "("):
            return key
    return None


def _substitute_arc_prefix(en_title: str, arc_map: dict[str, str]) -> Optional[tuple[str, str]]:
    """Full RU heading + matched arc key, applied once at migration time.

    Leading arc key (case-insensitive, longest wins so ``Gathered Pages``
    beats legacy ``Gathered``) -> RU arc + original remainder, with
    ``(Arc N)`` -> ``(Арка N)`` in the remainder. Returns
    ``(ru_title, arc_key)`` or ``None`` when no key matches.
    """
    key = _match_arc_key(en_title, arc_map)
    if key is None:
        return None
    remainder = _ARC_REMAINDER_RE.sub(r"(Арка \1)", en_title[len(key):])
    return f"{arc_map[key]}{remainder}", key


def migrate(
    source_dir: Path,
    arc_names_path: Path,
    books_dir: Path,
    *,
    slug: str = "pact",
    pov_name: str = PACT_POV_NAME,
    pov_gender: str = PACT_POV_GENDER,
) -> tuple[Path, Path]:
    arc_map = _load_arc_map(arc_names_path)
    pact_dir = books_dir / slug
    if not pact_dir.is_dir():
        raise SplitterError(f"book dir missing: {pact_dir}")
    manifest = generate_directory_manifest(
        source_dir, slug, pov_for_all=pov_name,
        manifest_out=pact_dir / "manifest.json",
    )
    records = []
    unmatched: list[str] = []
    for chapter in manifest.chapters:
        substituted = _substitute_arc_prefix(chapter.en_title, arc_map)
        if substituted is None:
            unmatched.append(f"{chapter.file}: {chapter.en_title!r}")
            ru_title = None
            notes = "review: no arc key matches this heading"
        else:
            ru_title, arc_key = substituted
            notes = B1_REVIEW_NOTES.get(arc_key, "")
        records.append({
            "file": chapter.file,
            "order": chapter.order,
            "en_title": chapter.en_title,
            "ru_title": ru_title,
            "pov": {"name": pov_name, "gender": pov_gender},
            "notes": notes,
        })
    if unmatched:
        raise SplitterError(
            "extended arc table does not cover all headings:\n" + "\n".join(unmatched)
        )
    payload = {
        "schema_version": CHAPTERS_SCHEMA_VERSION,
        "book_slug": slug,
        "chapters": records,
    }
    chapters_path = pact_dir / "chapters.json"
    chapters_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    return pact_dir / "manifest.json", chapters_path


def build_argparser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-dir", type=Path, required=True)
    parser.add_argument("--arc-names", type=Path, required=True)
    parser.add_argument("--books-dir", type=Path, required=True)
    parser.add_argument("--slug", default="pact")
    return parser


def main(argv: Optional[list] = None) -> int:
    args = build_argparser().parse_args(argv)
    try:
        manifest_path, chapters_path = migrate(
            args.source_dir, args.arc_names, args.books_dir, slug=args.slug
        )
    except SplitterError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print(f"wrote {manifest_path} + {chapters_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
