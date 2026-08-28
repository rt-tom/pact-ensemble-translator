"""Book-memory role views — bounded, causal, role-specific book-memory contexts.

Part of the ``book-memory-role-views`` capability (v4.2 dev). This module is
PURE and STATELESS (no model calls, no disk I/O): it turns the single
authoritative pre-chapter ``book_memory`` + glossary into a per-role prompt
block used by the real default whole-chapter consumers (generation, R-stage
Russian editor, B3 audit, selective repair/re-audit, glossary resolver).

Two public pure functions:

* ``select_relevant(authoritative_state, source_map)`` — computed ONCE per
  chapter, reusing the existing causal source-relevance logic in
  ``build_chapter_index``. Every role view is a projection of the single
  ``RelevanceResult`` so consumers cannot drift apart.
* ``render_book_context(role, relevance, authoritative_glossary,
  current_b1_2, glossary_candidates)`` — produces a bounded, deterministic,
  identity-bearing ``RenderedContext`` for one role.

Boundary hardening (project standing rule) for the four-file memory state,
candidate reports, and glossary is enforced in ``pact_v4.phase1.memory`` and
``pact_v4.phase1.book_memory_candidates``; this module only reads already-
validated, regular, non-symlink inputs passed by the caller.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

# Reuse the existing causal source-relevance logic (build_chapter_index) and
# the frozen-authoritative-glossary entry type. Imported lazily inside helper
# functions to keep this module importable in isolation (unit tests).
try:  # pragma: no cover - import path differs between package layouts
    from pact_full_pipeline_runner_v1.build_chapter_index import (
        build_chapter_index,
        pre_chapter_book_memory,
    )
    from pact_v4.phase2.risk import GlossaryEntry, _term_present
except Exception:  # pragma: no cover
    build_chapter_index = None  # type: ignore
    pre_chapter_book_memory = None  # type: ignore
    GlossaryEntry = None  # type: ignore
    _term_present = None  # type: ignore

__all__ = [
    "ROLE_VIEW_SCHEMA_VERSION",
    "ROLE_VIEW_ROLES",
    "AuthoritativeState",
    "CanonicalRecord",
    "RelevanceResult",
    "RenderedContext",
    "RoleCardProvenance",
    "select_relevant",
    "render_book_context",
    "resolve_canonical_ru",
    "estimate_tokens",
]

# ---------------------------------------------------------------------------
# Named constants — "bounded" must be reproducible, so budgets/priorities are
# code constants, never prose (Decision 2 / task 3.5).
# ---------------------------------------------------------------------------

ROLE_VIEW_SCHEMA_VERSION = "pact-v4-book-memory-role-views/v1"

# The four real default whole-chapter consumers (spec: Requirement
# "Default whole-chapter role routing").
ROLE_VIEW_ROLES: Tuple[str, ...] = (
    "translator",
    "audit_repair",
    "russian_editor",
    "glossary",
)

# Concrete per-role token budgets (deterministic char/~4 estimate). The
# translator budget covers the causal durable BIBLE only; the current-chapter
# verified B1.2 block is a SEPARATE labelled section (see render_book_context
# 'translator') and is not counted here.
ROLE_TOKEN_BUDGET: Dict[str, int] = {
    "translator": 1400,
    "audit_repair": 700,
    "russian_editor": 400,
    "glossary": 220,
}

# Concrete per-role card (canonical-constraint record) budgets.
ROLE_CARD_BUDGET: Dict[str, int] = {
    "translator": 40,
    "audit_repair": 25,
    "russian_editor": 15,
    "glossary": 12,
}

# Fixed record (card) priority order, highest first. Narrator/global-voice
# constraints are an EXPLICIT exception (always included, independent of
# source presence) and therefore rank first.
RECORD_PRIORITY: Tuple[str, ...] = (
    "narrator",        # global voice / narrator (explicit exception)
    "global_voice",    # explicit global-voice / seed facts (explicit exception)
    "seed_fact",       # explicit seed fact (explicit exception)
    "character",       # source-relevant named character
    "entity",          # source-relevant named entity (place/group/...)
    "term",            # source-relevant world_term
    "fact",            # source-relevant verified fact
    "address",         # address/register form bound to a present participant
)

# Fixed field priority order WITHIN a card, highest first. The source-prevails
# instruction is rendered as a single leading line, not a per-card field.
FIELD_PRIORITY: Tuple[str, ...] = (
    "name",          # canonical EN name (identity)
    "canonical_ru",  # established RU form (glossary > book_memory.canonical_ru)
    "gender",        # verified gender (agreement)
    "address",       # address/register
    "fact",          # verified fact (grammar/consistency only)
)

# Approximate token estimate: 1 token ~ 4 characters (deterministic, no model).
_CHARS_PER_TOKEN = 4


def estimate_tokens(text: str) -> int:
    """Deterministic token estimate (~4 chars/token)."""
    if not text:
        return 0
    return (len(text) + _CHARS_PER_TOKEN - 1) // _CHARS_PER_TOKEN


def _canonical_hash(*parts: Any) -> str:
    encoded = json.dumps(parts, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _norm(s: str) -> str:
    return (s or "").strip().casefold().replace("\u2019", "'")


# ---------------------------------------------------------------------------
# Authoritative state (frozen, pre-chapter) + source map
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AuthoritativeState:
    """Frozen pre-chapter authoritative state for role-view rendering.

    ``book_memory`` MUST already be pre-chapter (provenance strictly earlier
    than the target chapter). ``glossary`` is the FROZEN authoritative
    glossary (glossary > ``book_memory.canonical_ru``). The caller is
    responsible for providing regular, non-symlink, validated inputs.
    """

    book_memory: Mapping[str, Any]
    glossary: Sequence[Any]
    chapter_id: str
    state_hash: str = ""
    glossary_hash: str = ""
    index_hash: str = ""


@dataclass(frozen=True)
class CanonicalRecord:
    """A source-relevant canonical record projected from the pre-chapter state.

    Carries only the stable, consistency-relevant attributes (name, kind,
    gender, the raw ``canonical_ru`` carrier, address/register, memory_class,
    and any attached consistency facts). The resolved ``canonical_ru`` is
    produced later by ``resolve_canonical_ru`` (glossary > ``book_memory``);
    this record holds the raw value for the renderer to resolve.
    """

    name: str
    kind: str  # character | entity | term | narrator | seed_fact | global_voice
    gender: str = ""
    canonical_ru: str = ""
    address: str = ""
    memory_class: str = ""
    facts: Tuple[str, ...] = ()
    exception: bool = False  # explicit narrator/seed/global-voice exception


# ---------------------------------------------------------------------------
# Relevance selector (computed once per chapter)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RelevanceResult:
    """The single causal source-relevance decision for one chapter.

    Every role view is a projection of this object; the glossary view
    additionally intersects it with the resolver's candidate set. The
    ``relevance_hash`` covers the index entry, the explicit exceptions, and
    the state/glossary/source identity so any drift is detectable.
    """

    chapter_id: str
    selected_characters: Tuple[str, ...]
    selected_entities: Tuple[str, ...]
    selected_terms: Tuple[str, ...]
    selected_facts: Tuple[str, ...]
    selected_address: Tuple[str, ...]
    # Explicit exception set: narrator name + seed/global-voice facts, ALWAYS
    # included independent of source presence (Decision 2 / task 3.9).
    exceptions: Tuple[str, ...]
    # The projected, source-relevant canonical records (with attributes) pulled
    # from the pre-chapter book_memory. render_book_context stays pure over
    # (relevance, authoritative_glossary, current_b1_2, glossary_candidates).
    selected_records: Tuple[CanonicalRecord, ...]
    index_entry: Mapping[str, Any]
    relevance_hash: str
    source_hash: str
    state_hash: str
    glossary_hash: str


def _record_for(bm: Mapping[str, Any], section: str, name: str) -> Optional[Mapping[str, Any]]:
    data = bm.get(section) if isinstance(bm, Mapping) else None
    if isinstance(data, Mapping):
        rec = data.get(name)
        return rec if isinstance(rec, Mapping) else None
    if isinstance(data, list):
        for entry in data:
            if isinstance(entry, Mapping):
                en = str(entry.get("name") or entry.get("source") or entry.get("english") or "")
                if _norm(en) == _norm(name):
                    return entry
    return None


def _is_excluded(pre_bm: Mapping[str, Any], name: str) -> bool:
    """Durable conflict exclusion: a record still in an ambiguous conflict state
    is excluded from every role view until an explicitly approved resolution."""
    if not isinstance(pre_bm, Mapping):
        return False
    conflicts = pre_bm.get("_conflicts")
    if isinstance(conflicts, Mapping) and _norm(name) in {_norm(str(k)) for k in conflicts.keys()}:
        return True
    # Also check per-record _excluded_conflict flag
    for section in ("characters", "entities", "terms"):
        rec = _record_for(pre_bm, section, name)
        if isinstance(rec, Mapping) and rec.get("_excluded_conflict"):
            return True
    return False


def _project_records(
    pre_bm: Mapping[str, Any],
    entry: Mapping[str, Any],
    exceptions: Sequence[str],
) -> Tuple[CanonicalRecord, ...]:
    """Project source-relevant canonical records (with attributes) from the
    pre-chapter book_memory, plus the explicit exception records.
    Records in a durable conflict state are excluded (finding 6)."""
    records: List[CanonicalRecord] = []
    seen: set = set()

    def _collect(section: str, kind: str, names: Sequence[str]) -> None:
        for name in names:
            if _is_excluded(pre_bm, name):
                continue
            key = (kind, _norm(name))
            if key in seen:
                continue
            seen.add(key)
            rec = _record_for(pre_bm, section, name) or {}
            # Skip if the underlying record is durably excluded
            if isinstance(rec, Mapping) and rec.get("_excluded_conflict"):
                continue
            records.append(CanonicalRecord(
                name=str(name),
                kind=kind,
                gender=str(rec.get("gender") or "").strip(),
                canonical_ru=str(rec.get("canonical_ru") or rec.get("ru") or "").strip(),
                address=str(rec.get("address") or rec.get("register") or "").strip(),
                memory_class=str(rec.get("memory_class") or ""),
                facts=tuple(str(f) for f in (rec.get("facts") or []) if isinstance(f, (str, Mapping))),
            ))

    _collect("characters", "character", entry.get("characters", []))
    _collect("entities", "entity", entry.get("named_entities", []))
    # world_term entities appear as 'term' candidates from either section.
    _collect("characters", "term", entry.get("terms", []))
    _collect("entities", "term", entry.get("terms", []))

    for exc in exceptions:
        kind, _, value = exc.partition(":")
        records.append(CanonicalRecord(name=value, kind=kind, exception=True))

    out: List[CanonicalRecord] = []
    seen_exc: set = set()
    for r in records:
        if r.exception:
            k = (r.kind, _norm(r.name))
            if k in seen_exc:
                continue
            seen_exc.add(k)
        out.append(r)
    return tuple(out)


def _source_hash(source_map: Mapping[str, str]) -> str:
    norm = {str(k): str(v) for k, v in source_map.items()}
    return _canonical_hash(norm)


def select_relevant(
    authoritative_state: AuthoritativeState,
    source_map: Mapping[str, str],
) -> RelevanceResult:
    """Compute the single causal source-relevance decision for one chapter.

    Reuses the existing causal source-relevance logic in ``build_chapter_index``
    (presence of a canonical name or a verified lexical variant in the chapter
    source). The result is computed ONCE per chapter and reused by every role
    view projection.

    Narrator / seed / global-voice constraints are an EXPLICIT exception: they
    are always included from the explicit existing policy, independent of source
    presence (Decision 2 / task 3.9). They are returned in ``exceptions`` and
    included by every role view.

    Pure: no I/O, no model calls, deterministic.
    """
    bm = authoritative_state.book_memory
    chapter_id = authoritative_state.chapter_id
    source_text = "\n".join(str(v) for v in source_map.values())

    pre_bm = pre_chapter_book_memory(bm, chapter_id) if pre_chapter_book_memory else dict(bm)
    glossary = authoritative_state.glossary

    entry: Mapping[str, Any]
    if build_chapter_index is not None:
        entry = build_chapter_index(
            chapter_id=chapter_id,
            source_text=source_text,
            book_memory=pre_bm,
            glossary=list(glossary),
        )
    else:  # pragma: no cover - defensive fallback
        entry = {
            "characters": [],
            "named_entities": [],
            "terms": [],
            "facts": [],
            "address": [],
        }

    exceptions: List[str] = []
    pov = bm.get("pov") if isinstance(bm, Mapping) else None
    if isinstance(pov, Mapping):
        narrator_name = str(pov.get("source_name") or "")
        if narrator_name:
            exceptions.append(f"narrator:{narrator_name}")
    for fact in bm.get("facts", []) if isinstance(bm, Mapping) else []:
        if not isinstance(fact, Mapping):
            continue
        if fact.get("seed") is True:
            t = str(fact.get("fact") or fact.get("text") or "")
            if t:
                exceptions.append(f"seed_fact:{t}")
        if fact.get("global_voice") is True:
            t = str(fact.get("fact") or fact.get("text") or "")
            if t:
                exceptions.append(f"global_voice:{t}")

    rel_hash = _canonical_hash(
        entry,
        sorted(exceptions),
        authoritative_state.state_hash,
        authoritative_state.glossary_hash,
        _source_hash(source_map),
        ROLE_VIEW_SCHEMA_VERSION,
    )
    return RelevanceResult(
        chapter_id=chapter_id,
        selected_characters=tuple(sorted({str(c) for c in entry.get("characters", [])}, key=str.casefold)),
        selected_entities=tuple(sorted({str(e) for e in entry.get("named_entities", [])}, key=str.casefold)),
        selected_terms=tuple(sorted({str(t) for t in entry.get("terms", [])}, key=str.casefold)),
        selected_facts=tuple(sorted({str(f) for f in entry.get("facts", [])}, key=str.casefold)),
        selected_address=tuple(sorted({str(a) for a in entry.get("address", [])}, key=str.casefold)),
        exceptions=tuple(sorted(set(exceptions), key=str.casefold)),
        selected_records=_project_records(pre_bm, entry, exceptions),
        index_entry=entry,
        relevance_hash=rel_hash,
        source_hash=_source_hash(source_map),
        state_hash=authoritative_state.state_hash,
        glossary_hash=authoritative_state.glossary_hash,
    )


# ---------------------------------------------------------------------------
# Glossary conflict resolution (glossary > book_memory.canonical_ru)
# ---------------------------------------------------------------------------


def resolve_canonical_ru(
    name: str,
    record: Mapping[str, Any],
    glossary: Sequence[Any],
) -> Tuple[Optional[str], bool]:
    """Resolve the established Russian form for ``name``.

    The FROZEN authoritative glossary wins over ``book_memory.canonical_ru``.
    When both exist but disagree, the form is EXCLUDED from the view and a
    conflict is reported (the caller records the diagnostic rather than
    silently resolving in favor of either source).

    Returns ``(resolved_ru, conflict)`` where ``resolved_ru`` is ``None`` when
    excluded (either no source at all, or a conflict).
    """
    bm_ru = ""
    if isinstance(record, Mapping):
        bm_ru = str(record.get("canonical_ru") or record.get("ru") or "").strip()
    gl_ru = ""
    folded = _norm(name)
    for entry in glossary or []:
        src = _norm(str(getattr(entry, "source_term", "") or ""))
        targets = getattr(entry, "target_terms", ()) or ()
        if src == folded and targets:
            gl_ru = str(targets[0]).strip()
            break
    # Also allow a flat glossary mapping shape {source: target}.
    if not gl_ru and isinstance(glossary, Mapping):
        for k, v in glossary.items():
            if _norm(str(k)) == folded:
                gl_ru = str(v[0] if isinstance(v, (list, tuple)) and v else v)
                break

    if gl_ru and bm_ru and _norm(gl_ru) != _norm(bm_ru):
        return None, True  # conflict: exclude form, record diagnostic
    if gl_ru:
        return gl_ru, False
    if bm_ru:
        return bm_ru, False
    return None, False


# ---------------------------------------------------------------------------
# Rendered context (bounded, deterministic, identity-bearing)
# ---------------------------------------------------------------------------


@dataclass
class RenderedContext:
    """A bounded, deterministic role view plus its identity/provenance."""

    role: str
    schema_version: str
    text: str
    included_canonical_ids: Tuple[str, ...]
    resolved_term_map: Dict[str, str]  # EN name -> resolved RU form ("" if excluded/unknown)
    canonical_hash: str  # hash of (role, schema, selected causal entry, resolved glossary slice)
    conflicts: Tuple[Dict[str, str], ...]  # glossary/memory conflict diagnostics
    empty_reason: str = ""  # "" when non-empty; "no_relevant"/"disabled"/"invalid" otherwise

    def is_empty(self) -> bool:
        return not self.text.strip()


def _build_cards(
    relevance: RelevanceResult,
    glossary: Sequence[Any],
    *,
    include_kinds: Sequence[str],
    include_facts: bool,
    include_address: bool,
) -> List[Dict[str, Any]]:
    """Build candidate cards from the projected records (record-priority order)."""
    records = [r for r in relevance.selected_records if r.kind in include_kinds]
    cards: List[Dict[str, Any]] = []
    for rec in records:
        if rec.exception:
            continue  # exceptions are emitted separately as explicit policy lines
        resolved_ru, conflict = resolve_canonical_ru(rec.name, dict(rec.__dict__), glossary)
        cards.append({
            "kind": rec.kind,
            "name": rec.name,
            "record": {
                "gender": rec.gender,
                "address": rec.address,
                "canonical_ru": rec.canonical_ru,
                "facts": list(rec.facts),
            },
            "fact": None,
            "resolved_ru": resolved_ru,
            "conflict": conflict,
        })
    if include_facts:
        for fact in relevance.selected_facts:
            cards.append({"kind": "fact", "name": "", "record": {}, "fact": fact,
                          "resolved_ru": None, "conflict": False})
    if include_address:
        for addr in relevance.selected_address:
            cards.append({"kind": "address", "name": "", "record": {}, "fact": addr,
                          "resolved_ru": None, "conflict": False})

    def _kind_rank(kind: str) -> int:
        try:
            return RECORD_PRIORITY.index(kind)
        except ValueError:
            return len(RECORD_PRIORITY)

    cards.sort(key=lambda c: (_kind_rank(c["kind"]), _norm(c["name"])))
    return cards


def _render_card(name: str, record: Mapping[str, Any], resolved_ru: Optional[str],
                 include_fields: Sequence[str]) -> Tuple[str, int]:
    """Render one bounded canonical constraint card (name + selected fields)."""
    parts: List[str] = [f"- {name}"]
    shown: List[str] = []
    for fld in FIELD_PRIORITY:
        if fld not in include_fields:
            continue
        if fld == "canonical_ru" and resolved_ru:
            shown.append(f"  ru: {resolved_ru}")
        elif fld == "gender":
            g = str(record.get("gender") or "").strip()
            if g:
                shown.append(f"  gender: {g}")
        elif fld == "address":
            a = str(record.get("address") or "").strip()
            if a:
                shown.append(f"  address: {a}")
        elif fld == "fact":
            facts = record.get("facts") or []
            if isinstance(facts, list):
                for fct in facts:
                    ft = str(fct if isinstance(fct, str) else fct.get("fact") if isinstance(fct, Mapping) else "").strip()
                    if ft:
                        shown.append(f"  fact: {ft}")
    if shown:
        parts.extend(shown)
    text = "\n".join(parts)
    return text, estimate_tokens(text)


def _trim_to_budget(
    cards: List[Dict[str, Any]],
    role: str,
    include_fields: Sequence[str],
) -> Tuple[List[Dict[str, Any]], Dict[str, str]]:
    """Deterministic overflow: drop lowest-priority extras when over budget.

    Applies the fixed RECORD_PRIORITY / FIELD_PRIORITY order. Highest-priority
    cards and fields are retained; lowest-priority extras are trimmed. Returns
    the retained cards and a dict of conflict diagnostics keyed by name.
    """
    token_budget = ROLE_TOKEN_BUDGET.get(role, 400)
    card_budget = ROLE_CARD_BUDGET.get(role, 20)
    conflicts: Dict[str, str] = {}

    ordered = list(cards)
    kept: List[Dict[str, Any]] = []
    used_tokens = 0
    for card in ordered:
        if len(kept) >= card_budget:
            break
        text, tok = _render_card(card["name"], card["record"], card["resolved_ru"], include_fields)
        if card.get("conflict"):
            conflicts[card["name"]] = (
                f"glossary/memory conflict for {card['name']}: excluded from view"
            )
        # Keep at least one card even if it alone exceeds budget (fail-soft);
        # otherwise skip lowest-priority cards that would overflow.
        if used_tokens + tok > token_budget and kept:
            continue
        kept.append(card)
        used_tokens += tok
    return kept, conflicts


def _glossary_identity(glossary: Sequence[Any]) -> str:
    """Stable identity of the authoritative glossary actually used by the view.

    Any change to the glossary invalidates the rendered-view hash (replay guard).
    """
    items = []
    for e in glossary or []:
        if isinstance(e, Mapping):
            for k, v in e.items():
                items.append((str(k), str(v)))
        else:
            src = str(getattr(e, "source_term", "") or "")
            tgt = tuple(str(t) for t in (getattr(e, "target_terms", ()) or ()))
            items.append((src, tgt))
    return _canonical_hash(sorted(items))


def render_book_context(
    role: str,
    relevance: RelevanceResult,
    authoritative_glossary: Sequence[Any],
    current_b1_2: Optional[str] = None,
    glossary_candidates: Optional[Sequence[str]] = None,
) -> RenderedContext:
    """Render a bounded role view for one role (pure, deterministic).

    ``role`` is one of ``ROLE_VIEW_ROLES``. ``authoritative_glossary`` is the
    frozen authoritative glossary (glossary > ``book_memory.canonical_ru``).
    ``current_b1_2`` is the SEPARATE current-chapter verified B1.2 block, used
    ONLY for the ``translator`` role (generation) and carried with its own
    identity. ``glossary_candidates`` limits the ``glossary`` role to the
    resolver's current candidate set.

    The rendered hash includes the role schema/version, the selected causal
    entry, and the resolved glossary slice, so a glossary change (or a
    relevance change) invalidates replay.
    """
    if role not in ROLE_VIEW_ROLES:
        raise ValueError(f"unknown role view: {role!r}")

    glossary = authoritative_glossary
    included_ids: List[str] = []
    resolved_map: Dict[str, str] = {}
    conflict_records: List[Dict[str, str]] = []

    source_prevails = (
        "SOURCE PREVAILS: current chapter source evidence overrides memory; "
        "treat any memory/source disagreement as a consistency issue to verify, "
        "never as proof the source is wrong."
    )

    if role == "translator":
        # Causal durable BIBLE (pre-chapter facts only) — bounded.
        cards = _build_cards(
            relevance, glossary,
            include_kinds=("character", "entity", "term", "narrator", "seed_fact", "global_voice"),
            include_facts=True, include_address=True,
        )
        kept, conflicts = _trim_to_budget(cards, role, ("name", "canonical_ru", "gender", "address", "fact"))
        lines: List[str] = ["BOOK MEMORY (established, pre-chapter):"]
        for exc in relevance.exceptions:
            lines.append(f"- {exc}")
        for card in kept:
            if card["kind"] in ("fact", "address"):
                prefix = "fact" if card["kind"] == "fact" else "address"
                lines.append(f"- {prefix}: {card['fact']}")
                continue
            text, _ = _render_card(card["name"], card["record"], card["resolved_ru"],
                                   ("name", "canonical_ru", "gender", "address", "fact"))
            lines.append(text)
            if card["name"]:
                included_ids.append(card["name"])
            if card["resolved_ru"]:
                resolved_map[card["name"]] = card["resolved_ru"]
        for name, diag in conflicts.items():
            conflict_records.append({"name": name, "diagnostic": diag})
        causal_text = "\n".join(lines)
        # Separate labelled current-chapter verified B1.2 block (its own id).
        b12_block = ""
        if current_b1_2:
            b12_block = "\n\nCURRENT CHAPTER VERIFIED ENTITY FACTS (B1.2, source-derived):\n" + current_b1_2
        text = causal_text + b12_block
        empty_reason = "" if text.strip() else "no_relevant"

    elif role == "audit_repair":
        cards = _build_cards(
            relevance, glossary,
            include_kinds=("character", "entity", "term", "narrator", "seed_fact", "global_voice"),
            include_facts=True, include_address=True,
        )
        kept, conflicts = _trim_to_budget(cards, role, ("name", "canonical_ru", "gender", "address", "fact"))
        lines = [source_prevails, "", "ESTABLISHED CONSTRAINTS (verify against current source):"]
        for exc in relevance.exceptions:
            lines.append(f"- {exc}")
        for card in kept:
            if card["kind"] in ("fact", "address"):
                prefix = "fact" if card["kind"] == "fact" else "address"
                lines.append(f"- {prefix}: {card['fact']}")
                continue
            text, _ = _render_card(card["name"], card["record"], card["resolved_ru"],
                                   ("name", "canonical_ru", "gender", "address", "fact"))
            lines.append(text)
            if card["name"]:
                included_ids.append(card["name"])
            if card["resolved_ru"]:
                resolved_map[card["name"]] = card["resolved_ru"]
        for name, diag in conflicts.items():
            conflict_records.append({"name": name, "diagnostic": diag})
        text = "\n".join(lines)
        empty_reason = "" if (kept or relevance.exceptions) else "no_relevant"

    elif role == "russian_editor":
        # Grammar-relevant only: established RU forms + verified gender/address/
        # register. NO source text, NO plot/relationship facts.
        cards = _build_cards(
            relevance, glossary,
            include_kinds=("character", "entity", "narrator", "seed_fact", "global_voice"),
            include_facts=False, include_address=True,
        )
        kept, conflicts = _trim_to_budget(cards, role, ("name", "canonical_ru", "gender", "address"))
        lines = ["ESTABLISHED RUSSIAN FORMS (constrain realization only; no source text):"]
        for exc in relevance.exceptions:
            lines.append(f"- {exc}")
        for card in kept:
            if card["kind"] in ("fact", "address"):
                continue
            text, _ = _render_card(card["name"], card["record"], card["resolved_ru"],
                                   ("name", "canonical_ru", "gender", "address"))
            lines.append(text)
            if card["name"]:
                included_ids.append(card["name"])
            if card["resolved_ru"]:
                resolved_map[card["name"]] = card["resolved_ru"]
        for name, diag in conflicts.items():
            conflict_records.append({"name": name, "diagnostic": diag})
        text = "\n".join(lines)
        empty_reason = "" if kept else "no_relevant"

    elif role == "glossary":
        # Only current resolver candidates (intersect with glossary_candidates).
        # Distinguish "not supplied" (None) from "supplied empty" ([]):
        # an explicit empty list means "include none", not "include all".
        cards = _build_cards(
            relevance, glossary,
            include_kinds=("character", "entity", "term"),
            include_facts=False, include_address=False,
        )
        if glossary_candidates is not None:
            cand_set = {_norm(c) for c in glossary_candidates}
            cards = [c for c in cards if _norm(c["name"]) in cand_set]
        kept, conflicts = _trim_to_budget(cards, role, ("name", "canonical_ru"))
        lines = [source_prevails, "", "ESTABLISHED EN→RU FORMS (candidates only):"]
        for exc in relevance.exceptions:
            lines.append(f"- {exc}")
        for card in kept:
            if card["resolved_ru"]:
                lines.append(f"- {card['name']} → {card['resolved_ru']}")
                included_ids.append(card["name"])
                resolved_map[card["name"]] = card["resolved_ru"]
        for name, diag in conflicts.items():
            conflict_records.append({"name": name, "diagnostic": diag})
        text = "\n".join(lines)
        empty_reason = "" if kept else "no_relevant"
    else:  # pragma: no cover - guarded above
        text = ""
        empty_reason = "invalid"

    canonical_hash = _canonical_hash(
        ROLE_VIEW_SCHEMA_VERSION,
        role,
        relevance.relevance_hash,
        _glossary_identity(authoritative_glossary),  # resolved glossary slice identity
        {k: resolved_map[k] for k in sorted(resolved_map)},
        [c for c in conflict_records],
        estimate_tokens(text),
    )
    return RenderedContext(
        role=role,
        schema_version=ROLE_VIEW_SCHEMA_VERSION,
        text=text,
        included_canonical_ids=tuple(included_ids),
        resolved_term_map=resolved_map,
        canonical_hash=canonical_hash,
        conflicts=tuple(conflict_records),
        empty_reason=empty_reason,
    )


# ---------------------------------------------------------------------------
# Provenance (inspectable per-role diagnostics)
# ---------------------------------------------------------------------------


@dataclass
class RoleCardProvenance:
    """Inspectable per-role provenance record (task 5.1)."""

    role: str
    schema_version: str
    rendered_hash: str
    included_ids: Tuple[str, ...]
    included_count: int
    relevance_hash: str
    state_hash: str
    glossary_slice_hash: str
    conflicts: Tuple[Dict[str, str], ...]
    empty_reason: str

    def to_payload(self) -> Dict[str, Any]:
        return {
            "role": self.role,
            "schema_version": self.schema_version,
            "rendered_hash": self.rendered_hash,
            "included_canonical_ids": list(self.included_ids),
            "included_count": self.included_count,
            "relevance_hash": self.relevance_hash,
            "state_hash": self.state_hash,
            "glossary_slice_hash": self.glossary_slice_hash,
            "conflicts": [dict(c) for c in self.conflicts],
            "empty_reason": self.empty_reason,
        }


def build_role_provenance(
    role: str,
    rendered: RenderedContext,
    relevance: RelevanceResult,
) -> RoleCardProvenance:
    """Build a per-role provenance record from a rendered view."""
    return RoleCardProvenance(
        role=role,
        schema_version=rendered.schema_version,
        rendered_hash=rendered.canonical_hash,
        included_ids=rendered.included_canonical_ids,
        included_count=len(rendered.included_canonical_ids),
        relevance_hash=relevance.relevance_hash,
        state_hash=relevance.state_hash,
        glossary_slice_hash=relevance.glossary_hash,
        conflicts=rendered.conflicts,
        empty_reason=rendered.empty_reason,
    )


def compute_role_views(
    book_memory: Mapping[str, Any],
    glossary: Sequence[Any],
    chapter_id: str,
    source_map: Mapping[str, str],
    *,
    current_b1_2_block: Optional[str] = None,
    glossary_candidates: Optional[Sequence[str]] = None,
    state_hash: str = "",
    glossary_hash: str = "",
) -> Dict[str, Any]:
    """Compute the single causal relevance result ONCE and project every role view.

    Returns ``{"relevance": RelevanceResult, "views": {role: RenderedContext},
    "provenance": {role: dict}}``. The whole-chapter runner calls this once per
    chapter and threads the views into every consumer (generation, R-editor,
    B3 audit/repair/re-audit, glossary resolver) so they cannot drift apart.
    """
    state = AuthoritativeState(
        book_memory=book_memory, glossary=glossary, chapter_id=chapter_id,
        state_hash=state_hash, glossary_hash=glossary_hash,
    )
    relevance = select_relevant(state, source_map)
    views: Dict[str, RenderedContext] = {}
    provenance: Dict[str, Any] = {}
    for role in ROLE_VIEW_ROLES:
        rc = render_book_context(
            role, relevance, glossary,
            current_b1_2=current_b1_2_block if role == "translator" else None,
            glossary_candidates=glossary_candidates if role == "glossary" else None,
        )
        views[role] = rc
        provenance[role] = build_role_provenance(role, rc, relevance).to_payload()
    return {"relevance": relevance, "views": views, "provenance": provenance}
