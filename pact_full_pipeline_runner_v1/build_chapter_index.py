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

CHAPTER_INDEX_V2_SCHEMA = "pact-v4-chapter-index/v2"
BOOK_MEMORY_POLICY_VERSION = "book-memory-policy/v1"


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


def _variants_with_provenance(book_memory: Mapping, section: str, name: str, chapter_id: str | None = None) -> List[str]:
    """Surface forms filtered by provenance: only aliases with chapter < target.""" 
    data = book_memory.get(section)
    forms = [name]
    if isinstance(data, Mapping):
        attrs = data.get(name)
        if isinstance(attrs, Mapping):
            variants = attrs.get("variants")
            if isinstance(variants, Mapping):
                for var, prov in variants.items():
                    # If chapter_id given, filter by provenance strictly earlier
                    if chapter_id is not None and isinstance(prov, Mapping):
                        v_ch = str(prov.get("chapter") or "")
                        if v_ch and not _chapter_before(v_ch, chapter_id):
                            continue
                    forms.append(str(var))
    elif isinstance(data, list):
        for entry in data:
            if not isinstance(entry, Mapping):
                continue
            if str(entry.get("name") or entry.get("source") or entry.get("english")) != name:
                continue
            variants = entry.get("variants")
            if isinstance(variants, Mapping):
                for var, prov in variants.items():
                    if chapter_id is not None and isinstance(prov, Mapping):
                        v_ch = str(prov.get("chapter") or "")
                        if v_ch and not _chapter_before(v_ch, chapter_id):
                            continue
                    forms.append(str(var))
    seen = set()
    out: List[str] = []
    for form in forms:
        key = form.casefold()
        if key not in seen:
            seen.add(key)
            out.append(form)
    return out

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


def _chapter_before(a: str, b: str) -> bool:
    """Numeric-aware ``a < b`` for chapter ids (``0001``..``0148``).

    Plain string comparison is safe for zero-padded ids of equal width, but
    a tolerant compare keeps the boundary correct if ids are ever
    unpadded/mixed (``2`` < ``10`` must hold numerically).
    """
    try:
        return int(a) < int(b)
    except (TypeError, ValueError):
        return str(a).casefold() < str(b).casefold()


def _entry_chapters(entry: Any) -> List[str]:
    """The chapter provenance list of a book_memory entry (``chapters``)."""
    if isinstance(entry, Mapping):
        chapters = entry.get("chapters")
        if isinstance(chapters, (list, tuple)):
            return [str(c) for c in chapters if c]
    return []


def pre_chapter_book_memory(
    book_memory: Mapping,
    chapter_id: str,
) -> Dict[str, Any]:
    """Return the PRE-chapter view of ``book_memory`` for chapter ``N``.

    RV finding 1 (SAFE-MEMORY, 2026-08-14): the per-chapter index entry for
    chapter N must be built from PRE-chapter memory — ONLY facts and
    entities that were known before chapter N (provenance chapter < N) —
    never from the post-promotion state of chapter N itself. A rerun of an
    already-accepted chapter must not show that chapter its own promoted
    facts (causal ``< N`` boundary).

    Rules (fail-closed; global knowledge always survives):

    * facts: kept when the entry is a global seed fact (``seed: true``), has
      NO ``chapter`` provenance (manual/global), or its ``chapter`` is
      strictly before ``chapter_id``; a fact attributed to chapter N or a
      LATER chapter is dropped;
    * characters/entities: kept when the entry has NO ``chapters``
      provenance (seed/manual) or its FIRST chapter is strictly before
      ``chapter_id``; an entity first seen in chapter N or later is dropped;
    * ``pov`` (narrator) and ``address_register`` are kept verbatim — the
      narrator is always known, and address forms are already
      participant-presence-filtered by ``build_chapter_index``.

    Returns a shallow copy; list sections are rebuilt, dict sections are
    copied per-entry, everything else is shared.
    """
    out: Dict[str, Any] = {}
    for key, value in book_memory.items():
        if key == "facts":
            kept_facts: List[Any] = []
            if isinstance(value, list):
                for fact in value:
                    if not isinstance(fact, Mapping):
                        kept_facts.append(fact)
                        continue
                    if fact.get("seed") is True:
                        kept_facts.append(fact)
                        continue
                    chapter = str(fact.get("chapter") or "")
                    if not chapter or _chapter_before(chapter, chapter_id):
                        kept_facts.append(fact)
            out[key] = kept_facts
        elif key in ("characters", "entities"):
            if isinstance(value, Mapping):
                kept_entries: Dict[str, Any] = {}
                for name, entry in value.items():
                    chapters = _entry_chapters(entry)
                    if not chapters or any(
                        _chapter_before(ch, chapter_id) for ch in chapters
                    ):
                        kept_entries[name] = entry
                out[key] = kept_entries
            else:
                out[key] = value
        else:
            out[key] = value
    return out


def _field_provenance_before(entry: Mapping, field: str, chapter_id: str) -> bool:
    fp = entry.get("field_provenance", {})
    if not isinstance(fp, Mapping):
        return False
    prov = fp.get(field)
    if not isinstance(prov, Mapping):
        return False
    ch = str(prov.get("chapter") or "")
    return bool(ch) and _chapter_before(ch, chapter_id)

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
    # Fail-soft to narrator+seed when schema/policy missing or unknown (spec requirement) — FINDING 3
    def _fail_soft(reason: str):
        import logging as _log
        _log.getLogger(__name__).warning("chapter_index fail-soft %s for %s — rendering narrator+seed only", reason, chapter_id)
        narrator_name_fs = ""
        pov_fs = book_memory.get("pov") if isinstance(book_memory, Mapping) else None
        if isinstance(pov_fs, Mapping):
            narrator_name_fs = str(pov_fs.get("source_name") or "")
        chars_fs = [narrator_name_fs] if narrator_name_fs else []
        raw_facts_fs = book_memory.get("facts") if isinstance(book_memory, Mapping) else None
        facts_fs = []
        if isinstance(raw_facts_fs, list):
            for fact in raw_facts_fs:
                if isinstance(fact, Mapping) and fact.get("seed") is True:
                    t2 = str(fact.get("fact") or fact.get("text") or "")
                    if t2:
                        facts_fs.append(t2)
        return {"characters": sorted(set(chars_fs), key=str.casefold), "named_entities": [], "terms": [], "facts": sorted(set(facts_fs), key=str.casefold), "address": [], "_fail_soft_reason": reason}
    bm_schema = book_memory.get("schema") if isinstance(book_memory, Mapping) else None
    bm_policy_ver = book_memory.get("book_memory_policy_version") if isinstance(book_memory, Mapping) else None
    if bm_schema != "pact-v4-book-memory/v2":
        return _fail_soft(f"unsupported schema {bm_schema!r}")
    if bm_policy_ver != "book-memory-policy/v1":
        return _fail_soft(f"unsupported policy version {bm_policy_ver!r}")
    character_names = _iter_names(book_memory, "characters")
    entity_names = _iter_names(book_memory, "entities")

    narrator_name = ""
    pov = book_memory.get("pov") if isinstance(book_memory, Mapping) else None
    if isinstance(pov, Mapping):
        narrator_name = str(pov.get("source_name") or "")

    conflict_sources = _glossary_conflict_sources(glossary)

    # --- characters/entities: present in the chapter, or always (narrator) --- causal v2 split
    characters: List[Any] = []
    named_entities: List[str] = []
    for section in ("characters", "entities"):
        for name in _iter_names(book_memory, section):
            # Determine memory_class for split
            entry = book_memory.get(section, {}).get(name) if isinstance(book_memory.get(section), dict) else None
            mc = ""
            if isinstance(entry, Mapping):
                mc = str(entry.get("memory_class") or "")
            # Use provenance-filtered variants for presence
            present = any(
                _term_present(source_text, form)
                for form in _variants_with_provenance(book_memory, section, name, chapter_id)
            )
            locked = bool(
                narrator_name
                and (
                    name.casefold() == narrator_name.casefold()
                    or any(
                        form.casefold() == narrator_name.casefold()
                        for form in _variants_with_provenance(book_memory, section, name, chapter_id)
                    )
                )
                or name.casefold() in conflict_sources
            )
            if present or locked:
                # Split per memory_class: named_character -> characters, others -> named_entities
                # v2 split: only when memory_class is explicitly v2, otherwise flatten for backward compat (v1)
                if mc in ("named_character", "") or section == "characters":
                    # For v1 (mc == ""), keep flattened into characters to preserve existing tests
                    # For v2 named_character, also characters
                    # If v2 and non-character, go to named_entities
                    if mc == "" or mc == "named_character":
                        characters.append(name)
                    else:
                        named_entities.append(name)
                else:
                    named_entities.append(name)
    if narrator_name and narrator_name not in [str(c) if isinstance(c, str) else c.get("name") for c in characters]:
        characters.append(narrator_name)
    # Deduplicate: dicts by name, strs by value
    seen_chars = {}
    for item in characters:
        key = (item if isinstance(item, str) else item.get("name","")).casefold()
        if key not in seen_chars:
            seen_chars[key] = item
    characters = sorted(seen_chars.values(), key=lambda x: (x if isinstance(x, str) else x.get("name","")).casefold())
    named_entities = sorted(set(named_entities), key=str.casefold)

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

    # v2 type-aware terms: world_term memory_class entities that are approved terms and present in source, causal filtered
    terms: List[str] = []
    # First collect world_term entities that are present
    for section in ("characters", "entities"):
        for name in _iter_names(book_memory, section):
            entry = book_memory.get(section, {}).get(name) if isinstance(book_memory.get(section), dict) else None
            mc = str(entry.get("memory_class") or "") if isinstance(entry, Mapping) else ""
            if mc == "world_term":
                # Causal: must be learned strictly before target chapter
                chs = _entry_chapters(entry) if isinstance(entry, Mapping) else []
                if chs and not any(_chapter_before(ch, chapter_id) for ch in chs):
                    continue
                if any(_term_present(source_text, form) for form in _variants_with_provenance(book_memory, section, name, chapter_id)):
                    terms.append(name)
    # Also include approved_terms from policy that may not be stored as entities (legacy)
    approved_terms = []
    if isinstance(book_memory, dict):
        policy_terms = book_memory.get("policy", {}).get("approved_terms", []) if isinstance(book_memory.get("policy"), dict) else []
        approved_terms = [str(t) for t in policy_terms]
    for term in approved_terms:
        # Avoid duplicate if already added via world_term entity
        if term.casefold() in {t.casefold() for t in terms}:
            continue
        if _term_present(source_text, term):
            # If term is not yet in world_term entities but approved, still expose as term when present (conservative)
            terms.append(term)
    terms = sorted(set(terms), key=str.casefold)
    # Facts already filtered via pre_chapter_book_memory (chapter < N) and variant-aware presence above which uses provenance-filtered forms
    # Ensure per-field provenance strictly earlier is enforced for fact keys that are aliases (already via _variants_with_provenance)
    return {
        "characters": characters,
        "named_entities": named_entities,
        "terms": terms,
        "facts": facts,
        "address": address,
    }


def load_glossary(memory_dir: str) -> List[GlossaryEntry]:
    """Load ``glossary.json`` as ``GlossaryEntry`` list (tolerant).

    Accepts all production shapes:

    * a flat mapping ``{source: target}`` (the current production
      ``D:/pact/pact_chapters/glossary.json`` — 137 entries, values are
      plain target strings);
    * a flat mapping with target LISTS ``{source: [target, ...]}``;
    * the wrapped list form ``{"entries": [{"source": ..., "targets":
      [...]}, ...]}``;
    * a bare list of such entry mappings.

    A2 review fix (RV, commit 4ab250b): the flat production glossary was
    silently ignored (the loader only accepted a list or a mapping with an
    ``entries`` list), so locked/conflict entries were absent from
    ``chapter_index.json``. Every source term in any shape becomes a
    ``GlossaryEntry``; target values may be a string or a list of strings.
    """
    manager = MemoryManager(memory_dir)
    glossary = load_json(manager.glossary_path, {})
    entries: List[GlossaryEntry] = []
    raw_entries: Sequence[Any] = []
    if isinstance(glossary, list):
        raw_entries = glossary
    elif isinstance(glossary, Mapping):
        wrapped = glossary.get("entries")
        if isinstance(wrapped, list):
            raw_entries = wrapped
        else:
            # Flat production glossary {source: target} / {source: [targets]}.
            raw_entries = [
                {"source": str(source), "targets": target}
                for source, target in glossary.items()
            ]
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
    book_memory: Optional[Mapping] = None,
) -> Dict[str, Any]:
    """Build one chapter's index entry and merge it into ``chapter_index.json``.

    RV finding 1 (SAFE-MEMORY, 2026-08-14): the entry for chapter N is
    ALWAYS built from PRE-chapter memory — ``pre_chapter_book_memory``
    keeps only facts/entities with provenance ``chapter < N`` (plus
    seeds/narrator). The strict runner reads ``chapter_index[N]`` before
    generating N, so the entry under N must never contain N's own
    post-promotion facts: a rerun of N would otherwise leak N's own
    memory into its prompt. Pass ``book_memory`` to build from a caller
    snapshot; default loads the on-disk ``book_memory.json`` (the filter
    still applies to it).
    """
    memory_dir_path = Path(memory_dir)
    memory_dir_path.mkdir(parents=True, exist_ok=True)
    manager = MemoryManager(memory_dir)
    if book_memory is None:
        book_memory = load_json(manager.book_memory_path, {})
    if not isinstance(book_memory, Mapping):
        book_memory = {}
    book_memory = pre_chapter_book_memory(book_memory, chapter_id)
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
    # ensure v2 metadata
    existing["$schema"] = CHAPTER_INDEX_V2_SCHEMA
    existing["$book_memory_policy_version"] = BOOK_MEMORY_POLICY_VERSION
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
