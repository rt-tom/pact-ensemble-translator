#!/usr/bin/env python
"""V4.1 A2: deterministic per-chapter bible index builder (0 model calls).

Produces ``chapter_index.json`` in a memory-dir:

    {chapter_id: {"characters": [names], "facts": [texts], "address": [texts]}}

Rules (owner decision 2026-08-08, plan §5.2 — the same ``_term_present``
as the A1.1 glossary budgeter: ``(?<!\\w)...(?!\\w)``, IGNORECASE,
multi-word supported):

* characters/entities: included iff the name (or one of its variants) is
  present in the chapter's source text;
* address forms: included iff the ``from`` or ``to`` participant name is
  present in the chapter's source text;
* facts: included iff at least one of the fact's explicit keys
  (character/place/term from the fact entry) is present in the chapter;
  when a fact entry carries no explicit keys, keys are derived
  deterministically from book_memory — every character/entity/address
  name that appears in the fact text (still 0 model calls);
* ALWAYS included (fail-closed, like A1.1 ``always_include``): the
  narrator (gender + name), locked glossary terms, glossary conflicts;
* B9 frequency thresholds are NOT applied (an index is not a glossary).

The result is deterministic and rebuilt by this script; the owner edits
the source ``book_memory``/rules, never the index by hand.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Mapping, Sequence

from pact_v4.phase0b.source_html import load_source
from pact_v4.phase1.memory import MemoryManager, atomic_write, load_json
from pact_v4.phase2.risk import GlossaryEntry, _term_present


def _iter_names(book_memory: Mapping, section: str) -> List[str]:
    """Character/entity names from the ``characters``/``entities`` sections.

    Tolerant of both the dict shape (``{name: attrs}``) and the list
    shape (``[{name/source/english: ...}]``).
    """
    data = book_memory.get(section)
    names: List[str] = []
    if isinstance(data, Mapping):
        names.extend(str(name) for name in data if name)
    elif isinstance(data, list):
        for entry in data:
            if not isinstance(entry, Mapping):
                continue
            name = (
                entry.get("name") or entry.get("source") or entry.get("english")
            )
            if name:
                names.append(str(name))
    return names


def _variants_for(book_memory: Mapping, section: str, name: str) -> List[str]:
    """The surface forms to scan for: the canonical name plus its variants."""
    data = book_memory.get(section)
    forms = [name]
    if isinstance(data, Mapping):
        attrs = data.get(name)
        if isinstance(attrs, Mapping):
            variants = attrs.get("variants")
            if isinstance(variants, Mapping):
                forms.extend(str(v) for v in variants if v)
    elif isinstance(data, list):
        for entry in data:
            if not isinstance(entry, Mapping):
                continue
            if str(entry.get("name") or entry.get("source") or entry.get("english")) != name:
                continue
            variants = entry.get("variants")
            if isinstance(variants, Mapping):
                forms.extend(str(v) for v in variants if v)
    # De-dup preserving order.
    seen = set()
    out: List[str] = []
    for form in forms:
        key = form.casefold()
        if key not in seen:
            seen.add(key)
            out.append(form)
    return out


def _name_present(source_text: str, book_memory: Mapping, section: str, name: str) -> bool:
    return any(
        _term_present(source_text, form)
        for form in _variants_for(book_memory, section, name)
    )


def _fact_keys(fact: Mapping, book_memory: Mapping, character_names: Sequence[str]) -> List[str]:
    """Explicit keys from the fact entry, else a deterministic derivation.

    ``characters``/``places``/``terms``/``keys`` fields on the fact entry
    are authoritative when present. Otherwise every book_memory
    character/entity/address name that appears in the fact text becomes a
    key — still deterministic and 0 model calls.
    """
    for key in ("characters", "places", "terms", "keys"):
        explicit = fact.get(key)
        if explicit:
            if isinstance(explicit, str):
                return [explicit]
            if isinstance(explicit, (list, tuple)):
                return [str(item) for item in explicit if item]
    fact_text = (
        str(fact.get("fact") or fact.get("text") or fact.get("description") or "")
    )
    keys: List[str] = []
    for name in character_names:
        # Scan every surface form (canonical name + variants) so a fact
        # mentioning "Blake" still binds to the canonical "Blake Thorburn".
        forms = _variants_for(book_memory, "characters", name) + _variants_for(
            book_memory, "entities", name
        )
        if any(_term_present(fact_text, form) for form in forms):
            keys.append(name)
    return keys


def _glossary_conflict_sources(glossary: Sequence[GlossaryEntry]) -> set[str]:
    """Source terms that map to more than one distinct target (locked)."""
    by_source: Dict[str, set[str]] = {}
    for entry in glossary:
        key = entry.source_term.casefold().strip()
        if key:
            by_source.setdefault(key, set()).update(
                target.casefold().strip() for target in entry.target_terms
            )
    return {key for key, targets in by_source.items() if len(targets) > 1}


def build_chapter_index(
    *,
    chapter_id: str,
    source_text: str,
    book_memory: Mapping,
    glossary: Sequence[GlossaryEntry] = (),
) -> Dict[str, Any]:
    """Deterministic per-chapter bible index entry (0 model calls).

    Returns ``{"characters": [...], "facts": [...], "address": [...]}``
    for one chapter — the value stored under ``chapter_id`` in
    ``chapter_index.json``.
    """
    character_names = _iter_names(book_memory, "characters")
    entity_names = _iter_names(book_memory, "entities")

    narrator_name = ""
    pov = book_memory.get("pov") if isinstance(book_memory, Mapping) else None
    if isinstance(pov, Mapping):
        narrator_name = str(pov.get("source_name") or "")

    conflict_sources = _glossary_conflict_sources(glossary)

    # --- characters/entities: present in the chapter, or always (narrator) ---
    characters: List[str] = []
    for section in ("characters", "entities"):
        for name in _iter_names(book_memory, section):
            present = _name_present(source_text, book_memory, section, name)
            locked = bool(
                narrator_name
                and (
                    name.casefold() == narrator_name.casefold()
                    or any(
                        form.casefold() == narrator_name.casefold()
                        for form in _variants_for(book_memory, section, name)
                    )
                )
                or name.casefold() in conflict_sources
            )
            if present or locked:
                characters.append(name)
    # The narrator is ALWAYS present, even when never named in the chapter.
    if narrator_name and narrator_name not in characters:
        characters.append(narrator_name)
    characters = sorted(set(characters), key=str.casefold)

    # --- facts: at least one key present in the chapter, or locked ---
    all_names = sorted(set(character_names) | set(entity_names), key=str.casefold)
    facts: List[str] = []
    raw_facts = book_memory.get("facts") if isinstance(book_memory, Mapping) else None
    if isinstance(raw_facts, list):
        for fact in raw_facts:
            if not isinstance(fact, Mapping):
                continue
            fact_text = (
                str(fact.get("fact") or fact.get("text") or fact.get("description") or "")
            )
            if not fact_text:
                continue
            keys = _fact_keys(fact, book_memory, all_names)
            present_keys = []
            for key in keys:
                # A fact key is present when any of its surface forms
                # (canonical name + variants) appears in the chapter — the
                # same variant-aware rule as character presence.
                forms = _variants_for(book_memory, "characters", key) + _variants_for(
                    book_memory, "entities", key
                )
                if any(_term_present(source_text, form) for form in forms) or (
                    narrator_name and key.casefold() == narrator_name.casefold()
                ):
                    present_keys.append(key)
            if present_keys:
                facts.append(fact_text)
    facts = sorted(set(facts), key=str.casefold)

    # --- address forms: a NON-narrator participant present in the chapter ---
    # The narrator is always in the index, so checking "from OR to present"
    # would leak every narrator-rooted address form into every chapter.
    # The address register is only actionable when the ADDRESSED party is
    # actually in the scene: include the form when at least one participant
    # other than the narrator is present in the chapter source.
    address: List[str] = []
    raw_address = (
        book_memory.get("address_register") if isinstance(book_memory, Mapping) else None
    )
    if isinstance(raw_address, list):
        for entry in raw_address:
            if not isinstance(entry, Mapping):
                continue
            text = str(entry.get("text") or "")
            if not text:
                continue
            frm = str(entry.get("from") or "")
            to = str(entry.get("to") or "")
            if not frm and not to:
                continue
            participants = [name for name in (frm, to) if name]
            narrator_forms = (
                _variants_for(book_memory, "characters", narrator_name)
                if narrator_name else []
            )
            relevant = [
                name for name in participants
                if not (narrator_name and any(
                    name.casefold() == form.casefold() for form in narrator_forms
                ))
            ]
            present = any(_term_present(source_text, name) for name in relevant)
            if present:
                address.append(text)
    address = sorted(set(address), key=str.casefold)

    return {
        "characters": characters,
        "facts": facts,
        "address": address,
    }


def load_glossary(memory_dir: str) -> List[GlossaryEntry]:
    """Load ``glossary.json`` as ``GlossaryEntry`` list (tolerant)."""
    manager = MemoryManager(memory_dir)
    glossary = load_json(manager.glossary_path, {})
    entries: List[GlossaryEntry] = []
    if isinstance(glossary, list):
        raw_entries = glossary
    elif isinstance(glossary, Mapping):
        raw_entries = glossary.get("entries", []) if isinstance(glossary.get("entries"), list) else []
    else:
        raw_entries = []
    for entry in raw_entries:
        if not isinstance(entry, Mapping):
            continue
        source = str(entry.get("source") or entry.get("source_term") or entry.get("term") or "")
        targets = entry.get("targets") or entry.get("target_terms") or []
        if isinstance(targets, str):
            targets = [targets]
        if source and targets:
            entries.append(
                GlossaryEntry(
                    source_term=source,
                    target_terms=tuple(str(t) for t in targets),
                )
            )
    return entries


def build_index_file(
    *,
    memory_dir: str,
    chapter_html: str,
    chapter_id: str,
    out_path: str,
) -> Dict[str, Any]:
    """Build one chapter's index entry and merge it into ``chapter_index.json``."""
    memory_dir_path = Path(memory_dir)
    memory_dir_path.mkdir(parents=True, exist_ok=True)
    manager = MemoryManager(memory_dir)
    book_memory = load_json(manager.book_memory_path, {})
    glossary = load_glossary(memory_dir)

    blocks, _raw_sha = load_source(Path(chapter_html))
    source_text = "\n".join(block.text for block in blocks)

    entry = build_chapter_index(
        chapter_id=chapter_id, source_text=source_text,
        book_memory=book_memory, glossary=glossary,
    )

    index_path = Path(out_path) if out_path else manager.chapter_index_path
    existing = load_json(index_path, {})
    if not isinstance(existing, dict):
        existing = {}
    existing[chapter_id] = entry
    atomic_write(str(index_path), existing)
    return entry


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Build a deterministic per-chapter bible index (0 model calls).",
    )
    parser.add_argument("--memory-dir", required=True, help="Directory with book_memory.json/glossary.json")
    parser.add_argument("--chapter-html", required=True, help="Path to the chapter's source HTML")
    parser.add_argument("--chapter-id", required=True, help="Chapter id (key in chapter_index.json)")
    parser.add_argument("--out", default="", help="Output path (default: <memory-dir>/chapter_index.json)")
    args = parser.parse_args(argv)

    entry = build_index_file(
        memory_dir=args.memory_dir, chapter_html=args.chapter_html,
        chapter_id=args.chapter_id, out_path=args.out,
    )
    print(
        json.dumps(
            {args.chapter_id: entry}, ensure_ascii=False, indent=2
        )
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
