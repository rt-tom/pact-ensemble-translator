"""Glossary resolver — batched LLM canonical nominative for proper names.

Implements Requirement: Batched LLM resolver after repair on unified post-processing path
with sidecar identity and strict validation. Uses reviewer transport (russian_selector
primary, fidelity_reviewer fallback, qwen_audit local), single batch per chapter (≤3 attempts),
deterministic lemma_v1 token-wise validation, allowed_evidence_pids binding, quarantined
plumbing, atomic sidecar (tmp+rename, regular non-symlink), strict validation, and observability.

Design decisions per openspec glossary-model-resolver:
- D2: unified post-processing path including early B3 cache hit (0 calls when valid sidecar)
- D3: allowed_evidence_pids from source containing entity or VERIFIED alias (word-boundary)
- D4: sidecar identity with candidate_input_hash + translation_hash (in-memory)
- D8: reuse reviewer transport, no separate 3072 budget, fail-closed
"""
from __future__ import annotations

import json
import logging
import os
import re
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from pact_v4.phase1.models import canonical_json_hash
from pact_v4.runtime.backend_protocol import CompletionBackend, CompletionRequest, Message, JSON_OBJECT_SCHEMA
from pact_v4.runtime.json_resilience import JsonRetryPolicy, parse_json_response, retry_json_call, EmptyResponseError, TruncatedJSONError
from pact_v4.runtime.prompts_runtime import ReviewerPrompt

LOG = logging.getLogger(__name__)

GLOSSARY_PROPOSAL_SCHEMA = "glossary-proposal/v1"
RESOLVER_VERSION = "pact-v4-glossary-resolver/v1-lemma_v1"
PROMPT_VERSION = "pact-v4-glossary-resolver-prompt/v1"
RESPONSE_SCHEMA = "glossary_proposal/v1"
CACHE_MISS_POLICY_RECOMPUTE = "recompute"
CACHE_MISS_POLICY_FAIL_CLOSED = "fail_closed"

# Reuse Russian stemmer from glossary_candidates (deterministic)
_RU_ENDINGS = sorted({
    "иями", "ями", "ами", "ого", "ему", "ому", "ыми", "ими",
    "иях", "ах", "ях", "ой", "ей", "ый", "ий", "ая", "яя", "ую",
    "юю", "ом", "ем", "ым", "им", "ов", "ев", "ам", "ям", "а", "я",
    "у", "ю", "ы", "и", "е", "о", "ь",
}, key=len, reverse=True)

def _ru_stem(word: str) -> str:
    core = re.sub(r"[^А-Яа-яЁё]", "", word).casefold().replace("ё", "е")
    if len(core) <= 2:
        return core
    for ending in _RU_ENDINGS:
        if core.endswith(ending) and len(core) - len(ending) >= 3:
            return core[:-len(ending)]
    return core

def lemma_v1_match(surface_forms: Sequence[str], proposed_ru: str) -> bool:
    """Versioned token-wise lemma link: each proposed token stem == some surface token stem?
    Multi-word: each token of proposed_ru must have stem-equal surface token (order preserved).
    Actually per spec: surface_forms[] → proposed_ru token-wise stem equivalence, order preserved.
    For each surface form (may be multi-word), we compare tokenwise.
    Simplified: split proposed_ru and surface_forms[0] by whitespace? But spec says
    surface_forms[] are as in evidence (inflected), proposed_ru is nominative lemma.
    We check: tokens of proposed_ru each have stem equal to corresponding token of any surface_form.
    For single surface, token count must match; for multiple surfaces, at least one matches.
    """
    if not proposed_ru or not surface_forms:
        return False
    proposed_tokens = proposed_ru.strip().split()
    if not proposed_tokens:
        return False
    # Build stems for proposed
    proposed_stems = [_ru_stem(t) for t in proposed_tokens]
    # For each surface_form, check token-wise stems
    for surf in surface_forms:
        surf_tokens = str(surf).strip().split()
        if len(surf_tokens) != len(proposed_tokens):
            continue
        surf_stems = [_ru_stem(t) for t in surf_tokens]
        if surf_stems == proposed_stems:
            return True
        # Also allow case where surface has extra? But spec says token-wise, order preserved, len must match.
    # Fallback: if single-token proposed and single-token surfaces, check any surface stem == proposed stem
    if len(proposed_tokens) == 1:
        prop_stem = proposed_stems[0]
        for surf in surface_forms:
            # surface may be multi-token? Take each token separately?
            for tok in str(surf).split():
                if _ru_stem(tok) == prop_stem:
                    return True
        return False
    return False

_CYRILLIC = re.compile(r"[А-Яа-яЁё]")
_SOURCE_BOUNDARY = r"A-Za-z0-9_"

def _escaped_term(term: str) -> str:
    escaped = re.escape(term)
    escaped = escaped.replace("\\ ", "\\s+")
    escaped = escaped.replace("'", f"['{chr(39)}{chr(8217)}]")
    escaped = escaped.replace(chr(8217), f"['{chr(39)}{chr(8217)}]")
    # re.escape does not escape curly, handle
    if chr(8217) in term:
        escaped = escaped.replace(chr(8217), f"['{chr(39)}{chr(8217)}]")
    return escaped

def _term_in_source_word_boundary(text: str, term: str) -> bool:
    if not term:
        return False
    escaped = _escaped_term(term)
    return bool(re.search(rf"(?<![{_SOURCE_BOUNDARY}]){escaped}(?![{_SOURCE_BOUNDARY}])", text or "", flags=re.I))

def is_cyrillic(text: str) -> bool:
    return bool(_CYRILLIC.search(text))

# Blocklist incident regression (Roxanne→Бабуль)
_GLOSSARY_BLOCKLIST = {"бабуль"}

RU_STOP_WORDS: frozenset[str] = frozenset("""
и в во не что он на я с со как а то все она так его но да ты к у же вы за
бы по только ее мне было вот от меня еще нет о из ему теперь когда даже ну
вдруг ли если уже или ни быть был него до вас опять уж вам ведь там потом
себя ничего ей может они тут где есть надо ней для мы тебя их чем была сам
чтоб без будто чего раз тоже себе под будет ж тогда кто этот того потому
этого какой совсем ним здесь этом один почти мой тем чтобы нее сейчас
были куда зачем никогда можно при наконец два об другой хоть после над
больше тот через эти нас про всего них какая много разве три эту моя
впрочем хорошо свою этой перед иногда лучше чуть том нельзя такой им более
всегда конечно всю между нибудь собою очень однако причем притом либо
нежели ежели коль покуда доколе оттого откуда отколь
""".split())

GLOSSARY_RESOLVER_PROMPT = ReviewerPrompt(
    role="glossary_resolver",
    version=PROMPT_VERSION,
    instructions=(
        "You are a glossary resolver for Russian translations of English fiction. "
        "You are given a list of entities (proper-name persons/places/groups/nicknames) "
        "with their source evidence and the chapter's Russian translation map (pid -> text). "
        "For each entity, return its canonical Russian form in nominative case (Именительный падеж), "
        "the surface forms as they appear in the translation evidence, the evidence pid, type, confidence, decision. "
        "Return STRICT JSON, no markdown, with exactly this schema: "
        "{\"proposals\": [{\"entity\": string, \"proposed_ru\": string (nominative, Cyrillic), "
        "\"surface_forms\": [string], \"evidence_pid\": string, \"type\": string (person/place/group/nickname), "
        "\"confidence\": number 0-1, \"decision\": \"accept\" | \"reject\"}]}. "
        "Rules: proposed_ru MUST be nominative (e.g. Дионис not Диониса, Сандра not Сандре, Завоеватель not Завоевателю); "
        "surface_forms MUST be exact substrings of the Russian evidence_pid text; "
        "evidence_pid MUST be one of the allowed PIDs; type must be person/place/group/nickname; "
        "if unsure, decision reject; do not hallucinate; for multi-word entities return phrase whole (e.g. Рыцари Подвала)."
    ),
)

def _model_ref_for_resolver(backend: CompletionBackend) -> Optional[str]:
    """Resolve reviewer transport: russian_selector -> fidelity_reviewer -> qwen_audit -> default, else None."""
    bindings = getattr(backend.descriptor, "model_bindings", {}) or {}
    for role in ("russian_selector", "fidelity_reviewer", "qwen_audit", "qwen_fidelity", "default"):
        ref = bindings.get(role)
        if ref:
            return ref
    return None

def compute_allowed_evidence_pids(
    source_map: Mapping[str, str],
    entity_records: Sequence[Any],
) -> Dict[str, set]:
    """Deterministic allowed_evidence_pids per entity: source PIDs containing entity or VERIFIED alias (word-boundary)."""
    allowed: Dict[str, set] = {}
    for rec in entity_records:
        name = str(getattr(rec, "entity", "") or "")
        if not name:
            continue
        pids = set()
        for pid, text in source_map.items():
            if _term_in_source_word_boundary(text, name):
                pids.add(pid)
                continue
            # Check VERIFIED aliases
            for alias in getattr(rec, "aliases", ()):
                # alias status must be verified
                if getattr(alias, "status", "verified") != "verified":
                    continue
                surf = str(getattr(alias, "surface", "") or "")
                if surf and _term_in_source_word_boundary(text, surf):
                    pids.add(pid)
                    break
        allowed[name] = pids
    return allowed

def candidate_input_hash(entity_records: Sequence[Any]) -> str:
    """Hash of ordered candidate input (deterministic)."""
    # Sort by entity name for determinism
    payload = []
    for rec in sorted(entity_records, key=lambda r: str(getattr(r, "entity", ""))):
        payload.append({
            "entity": str(getattr(rec, "entity", "")),
            "canonical_type": str(getattr(rec, "canonical_type", "")),
            "anchor": {"pid": rec.anchor.pid, "span": rec.anchor.span} if hasattr(rec, "anchor") else {},
            "aliases": sorted([{"surface": a.surface, "pid": a.pid} for a in getattr(rec, "aliases", ())], key=lambda x: (x["pid"], x["surface"])),
            "glossary_worthy": bool(getattr(rec, "glossary_worthy", False)),
        })
    return canonical_json_hash(payload)

def translation_hash(translations: Mapping[str, str]) -> str:
    """Hash of in-memory translations_repaired (sorted pid->text)."""
    return canonical_json_hash(dict(sorted(translations.items())))

def semantic_translation_hash(translations: Mapping[str, str]) -> str:
    """Semantic hash without formatting tags (strip <i>, <em>, etc)."""
    stripped = {}
    tag_re = re.compile(r"<[^>]+>")
    for pid, text in translations.items():
        # Remove HTML tags for semantic comparison
        no_tags = tag_re.sub("", text or "")
        # Normalize whitespace
        no_tags = " ".join(no_tags.split())
        stripped[pid] = no_tags
    return canonical_json_hash(dict(sorted(stripped.items())))

def render_resolver_prompt(
    entity_records: Sequence[Any],
    allowed_pids: Mapping[str, set],
    translations: Mapping[str, str],
    source_map: Mapping[str, str],
    role_view_card: Optional[str] = None,
) -> str:
    """Render resolver prompt with candidates and translations."""
    lines = [GLOSSARY_RESOLVER_PROMPT.instructions, "", "ENTITIES:"]
    for rec in sorted(entity_records, key=lambda r: str(getattr(r, "entity", ""))):
        name = str(getattr(rec, "entity", ""))
        allowed = sorted(allowed_pids.get(name, ()))
        lines.append(f"- entity: {name} canonical_type: {getattr(rec, 'canonical_type', '')} allowed_pids: {allowed} aliases: {[a.surface for a in getattr(rec, 'aliases', ())]}")
    lines.append("")
    lines.append("TRANSLATIONS (pid -> Russian text):")
    for pid in sorted(translations):
        lines.append(f"  {pid}: {translations[pid]}")
    lines.append("")
    lines.append("SOURCE (for allowed check, English):")
    for pid in sorted(source_map):
        lines.append(f"  {pid}: {source_map[pid]}")
    if role_view_card:
        lines.append("")
        lines.append(
            "ESTABLISHED EN→RU FORMS (authoritative glossary; source prevails "
            "on disagreement):"
        )
        lines.append(str(role_view_card))
    return "\n".join(lines)

# Sidecar paths
def sidecar_path(out_dir: Path) -> Path:
    return Path(out_dir) / "glossary_proposals.json"

def _is_regular_non_symlink(path: Path) -> bool:
    try:
        # Must be regular file, not symlink, not dir, not fifo, etc.
        if path.is_symlink():
            return False
        if not path.is_file():
            return False
        # Ensure it's not a special file: check file type via lstat
        st = path.lstat()
        # On POSIX, regular file check: S_ISREG
        import stat
        if not stat.S_ISREG(st.st_mode):
            return False
        return True
    except Exception:
        return False

def atomic_write_sidecar(out_dir: Path, payload: Dict[str, Any]) -> Path:
    """Atomic write tmp+rename, only regular non-symlink."""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    target = sidecar_path(out_dir)
    tmp = target.with_suffix(target.suffix + ".tmp")
    # Ensure tmp is not symlink either
    if tmp.exists():
        try:
            if tmp.is_symlink():
                tmp.unlink()
        except Exception:
            pass
    data = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True)
    with open(tmp, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())
    # Ensure target parent not symlink?
    tmp.replace(target)
    return target

def validate_sidecar_payload(
    payload: Any,
    *,
    expected_chapter_id: Optional[str] = None,
    expected_snapshot_hash: Optional[str] = None,
    expected_config_identity: Optional[str] = None,
    expected_candidate_input_hash: Optional[str] = None,
    expected_translation_hash: Optional[str] = None,
    expected_model_ref: Optional[str] = None,
    expected_backend_identity: Optional[str] = None,
    expected_glossary_view_hash: Optional[str] = None,
    expected_glossary_view_version: Optional[str] = None,
    allowed_pids: Optional[Mapping[str, set]] = None,
    translation_map: Optional[Mapping[str, str]] = None,
    quarantined_pids: Optional[set] = None,
) -> Optional[str]:
    """Strict validation of sidecar payload. Returns None if valid, else reason string."""
    if not isinstance(payload, dict):
        return "payload must be object"
    # Exact schema/keys check — v4.2 adds rendered glossary-card identity so a
    # changed established-term card cannot reuse a sidecar built under different
    # prompt constraints (blocker finding 2).
    expected_keys = {"schema", "chapter_id", "snapshot_hash", "config_identity", "resolver_version", "prompt_version", "response_schema", "model_ref", "backend_identity", "candidate_input_hash", "translation_hash", "proposals", "glossary_view_hash", "glossary_view_version"}
    extra = set(payload.keys()) - expected_keys
    if extra:
        return f"extra fields {sorted(extra)}"
    missing = expected_keys - set(payload.keys())
    if missing:
        return f"missing fields {sorted(missing)}"
    if payload.get("schema") != GLOSSARY_PROPOSAL_SCHEMA:
        return f"schema mismatch {payload.get('schema')!r}"
    if payload.get("resolver_version") != RESOLVER_VERSION:
        return f"resolver_version mismatch {payload.get('resolver_version')!r}"
    if payload.get("prompt_version") != PROMPT_VERSION:
        return f"prompt_version mismatch {payload.get('prompt_version')!r}"
    if payload.get("response_schema") != RESPONSE_SCHEMA:
        return f"response_schema mismatch {payload.get('response_schema')!r}"
    if expected_chapter_id and payload.get("chapter_id") != expected_chapter_id:
        return f"chapter_id mismatch {payload.get('chapter_id')!r} != {expected_chapter_id!r}"
    if expected_snapshot_hash and payload.get("snapshot_hash") != expected_snapshot_hash:
        return f"snapshot_hash mismatch"
    if expected_config_identity and payload.get("config_identity") != expected_config_identity:
        return f"config_identity mismatch"
    if expected_candidate_input_hash and payload.get("candidate_input_hash") != expected_candidate_input_hash:
        return f"candidate_input_hash mismatch"
    if expected_translation_hash and payload.get("translation_hash") != expected_translation_hash:
        return f"translation_hash mismatch"
    if expected_model_ref and payload.get("model_ref") != expected_model_ref:
        return f"model_ref mismatch {payload.get('model_ref')!r} != {expected_model_ref!r}"
    if expected_backend_identity and payload.get("backend_identity") != expected_backend_identity:
        return f"backend_identity mismatch {payload.get('backend_identity')!r} != {expected_backend_identity!r}"
    if expected_glossary_view_hash is not None and payload.get("glossary_view_hash") != expected_glossary_view_hash:
        return f"glossary_view_hash mismatch {payload.get('glossary_view_hash')!r} != {expected_glossary_view_hash!r}"
    if expected_glossary_view_version is not None and payload.get("glossary_view_version") != expected_glossary_view_version:
        return f"glossary_view_version mismatch {payload.get('glossary_view_version')!r} != {expected_glossary_view_version!r}"
    proposals = payload.get("proposals")
    if not isinstance(proposals, list):
        return "proposals must be list"
    # Size checks
    if len(proposals) > 100:
        return "proposals too large"
    seen_ru: set = set()
    seen_entity: set = set()
    allowed_set = allowed_pids or {}
    for idx, prop in enumerate(proposals):
        if not isinstance(prop, dict):
            return f"proposal {idx} not object"
        # Exact proposal keys
        exp_prop_keys = {"entity", "proposed_ru", "surface_forms", "evidence_pid", "type", "confidence", "decision"}
        if set(prop.keys()) != exp_prop_keys:
            return f"proposal {idx} keys mismatch {sorted(prop.keys())} != {sorted(exp_prop_keys)}"
        entity = prop.get("entity")
        if not isinstance(entity, str) or not entity:
            return f"proposal {idx} entity invalid"
        if entity in seen_entity:
            return f"duplicate entity {entity!r}"
        seen_entity.add(entity)
        proposed_ru = prop.get("proposed_ru")
        if not isinstance(proposed_ru, str) or not proposed_ru:
            return f"proposal {idx} proposed_ru invalid"
        # Check cyrillic, not empty, not RU_STOP, not blocklist
        if not is_cyrillic(proposed_ru):
            return f"proposal {idx} proposed_ru not cyrillic {proposed_ru!r}"
        if proposed_ru.casefold() in RU_STOP_WORDS:
            return f"proposal {idx} proposed_ru is RU_STOP {proposed_ru!r}"
        if proposed_ru.casefold() in _GLOSSARY_BLOCKLIST:
            return f"proposal {idx} proposed_ru blocklisted {proposed_ru!r}"
        # Size: proposed_ru length reasonable
        if len(proposed_ru) > 100:
            return f"proposal {idx} proposed_ru too long"
        surface_forms = prop.get("surface_forms")
        if not isinstance(surface_forms, list) or not surface_forms:
            return f"proposal {idx} surface_forms invalid"
        if len(surface_forms) > 10:
            return f"proposal {idx} surface_forms too many"
        for sf in surface_forms:
            if not isinstance(sf, str) or not sf:
                return f"proposal {idx} surface_form invalid {sf!r}"
            if len(sf) > 100:
                return f"proposal {idx} surface_form too long"
        evidence_pid = prop.get("evidence_pid")
        if not isinstance(evidence_pid, str) or not evidence_pid:
            return f"proposal {idx} evidence_pid invalid"
        # Check allowed_evidence_pids
        allowed_for_entity = allowed_set.get(entity)
        if allowed_for_entity is not None:
            if evidence_pid not in allowed_for_entity:
                return f"proposal {idx} evidence_pid {evidence_pid!r} not in allowed {sorted(allowed_for_entity)}"
        # Quarantined check
        if quarantined_pids and evidence_pid in quarantined_pids:
            return f"proposal {idx} evidence_pid quarantined {evidence_pid!r}"
        # Check surface_forms ∈ evidence text
        if translation_map is not None:
            ev_text = translation_map.get(evidence_pid, "")
            for sf in surface_forms:
                if sf not in ev_text:
                    return f"proposal {idx} surface_form {sf!r} not in evidence {evidence_pid!r} text"
        # Check lemma link
        if not lemma_v1_match(surface_forms, proposed_ru):
            return f"proposal {idx} lemma mismatch surface_forms {surface_forms!r} -> {proposed_ru!r}"
        # Check duplicate ru between different entities (allowed within alias-group handled outside)
        # Here we check global duplicate ru
        ru_cf = proposed_ru.casefold()
        if ru_cf in seen_ru:
            return f"duplicate ru {proposed_ru!r} between different entities"
        seen_ru.add(ru_cf)
        # Type check
        if prop.get("type") not in ("person", "place", "group", "nickname"):
            return f"proposal {idx} type invalid {prop.get('type')!r}"
        if prop.get("decision") not in ("accept", "reject"):
            return f"proposal {idx} decision invalid"
        conf = prop.get("confidence")
        if not isinstance(conf, (int, float)) or not (0 <= conf <= 1):
            return f"proposal {idx} confidence invalid"
    return None

def load_and_validate_sidecar(
    out_dir: Path,
    *,
    expected_chapter_id: Optional[str] = None,
    expected_snapshot_hash: Optional[str] = None,
    expected_config_identity: Optional[str] = None,
    expected_candidate_input_hash: Optional[str] = None,
    expected_translation_hash: Optional[str] = None,
    expected_model_ref: Optional[str] = None,
    expected_backend_identity: Optional[str] = None,
    expected_glossary_view_hash: Optional[str] = None,
    expected_glossary_view_version: Optional[str] = None,
    allowed_pids: Optional[Mapping[str, set]] = None,
    translation_map: Optional[Mapping[str, str]] = None,
    quarantined_pids: Optional[set] = None,
) -> tuple[Optional[Dict[str, Any]], Optional[str]]:
    """Load sidecar with file-type checks and strict validation. Returns (payload, error_reason)."""
    path = sidecar_path(out_dir)
    if not path.exists():
        return None, "missing"
    # File type checks: regular non-symlink, no duplicate etc.
    # TOCTOU: check before read and after
    if not _is_regular_non_symlink(path):
        return None, "not regular non-symlink"
    try:
        # Use lstat to detect symlink, then read
        data = path.read_text(encoding="utf-8")
        payload = json.loads(data)
    except Exception as exc:
        return None, f"read/parse error {exc}"
    # Re-check file type after read (TOCTOU)
    if not _is_regular_non_symlink(path):
        return None, "TOCTOU symlink after read"
    # Validate payload
    err = validate_sidecar_payload(
        payload,
        expected_chapter_id=expected_chapter_id,
        expected_snapshot_hash=expected_snapshot_hash,
        expected_config_identity=expected_config_identity,
        expected_candidate_input_hash=expected_candidate_input_hash,
        expected_translation_hash=expected_translation_hash,
        expected_model_ref=expected_model_ref,
        expected_backend_identity=expected_backend_identity,
        expected_glossary_view_hash=expected_glossary_view_hash,
        expected_glossary_view_version=expected_glossary_view_version,
        allowed_pids=allowed_pids,
        translation_map=translation_map,
        quarantined_pids=quarantined_pids,
    )
    if err:
        return None, err
    return payload, None

class GlossaryResolver:
    """Batched LLM resolver on reviewer transport."""

    def __init__(self, backend: CompletionBackend, *, progress: Optional[Any] = None, usage_sink: Optional[Any] = None):
        self._backend = backend
        self._progress = progress
        self._usage_sink = usage_sink

    def resolve(
        self,
        *,
        chapter_id: str,
        entity_records: Sequence[Any],
        source_map: Mapping[str, str],
        translations: Mapping[str, str],
        allowed_pids: Mapping[str, set],
        out_dir: Optional[Path] = None,
        role_view_card: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        """Run one logical batch (≤3 attempts) to produce proposals. Returns sidecar payload or None on failure."""
        if not entity_records:
            return None
        model_ref = _model_ref_for_resolver(self._backend)
        if not model_ref:
            LOG.warning("glossary_resolver: no reviewer binding, fail-closed")
            return None
        prompt = render_resolver_prompt(entity_records, allowed_pids, translations, source_map, role_view_card=role_view_card)
        # Reuse reviewer role's configured max_output_tokens unchanged (no hard-code, no clamp)
        # Spec D8: inherit reviewer max_output_tokens as is; do not introduce separate 4096/16384 budget
        _reviewer_max_tokens = None
        _reviewer_reasoning = None
        _reviewer_temperature = 0.0
        _reviewer_seed = None
        try:
            desc = getattr(self._backend, "descriptor", None)
            eff = getattr(desc, "effective_options", {}) if desc is not None else {}
            # effective_options may be MappingProxyType (not dict), so check Mapping
            try:
                _reviewer_max_tokens = eff.get("max_output_tokens") or eff.get("default_max_output_tokens") or eff.get("reviewer_max_output_tokens")  # type: ignore[attr-defined]
            except Exception:
                _reviewer_max_tokens = None
            if _reviewer_max_tokens is None:
                _reviewer_max_tokens = getattr(self._backend, "_max_tokens", None) or getattr(self._backend, "max_output_tokens", None)
            # No hard-coded fallback (4096/16384) and no clamp — use reviewer budget as is
        except Exception:
            _reviewer_max_tokens = None
        if _reviewer_max_tokens is None:
            LOG.warning("glossary_resolver: reviewer max_output_tokens unknown, fail-closed (reuse unchanged)")
            return None
        try:
            tok = int(_reviewer_max_tokens)
        except Exception:
            LOG.warning("glossary_resolver: invalid reviewer max_output_tokens %r", _reviewer_max_tokens)
            return None
        # Inherit reasoning if backend supports it (bounded 0-3)
        try:
            _reviewer_reasoning = getattr(self._backend, "_reasoning", None)
            if _reviewer_reasoning is None and hasattr(self._backend, "descriptor"):
                _reviewer_reasoning = getattr(self._backend.descriptor, "reasoning", None)
        except Exception:
            _reviewer_reasoning = None
        req_kwargs: Dict[str, Any] = dict(
            model_ref=model_ref,
            messages=(Message(role="user", content=prompt),),
            max_output_tokens=tok,
            temperature=float(_reviewer_temperature),
            response_schema=JSON_OBJECT_SCHEMA,
            label="glossary_resolver",
        )
        # Prompt-only structured output mode (reuse reviewer setting)
        # CompletionRequest validates response_schema implies prompt_only; keep as is.
        if _reviewer_reasoning is not None:
            try:
                ri = int(_reviewer_reasoning)
                if 0 <= ri <= 3:
                    req_kwargs["request_options"] = {"reasoning": ri}  # type: ignore[assignment]
            except Exception:
                pass
        if _reviewer_seed is not None:
            try:
                req_kwargs["seed"] = int(_reviewer_seed)  # type: ignore[assignment]
            except Exception:
                pass
        request = CompletionRequest(**req_kwargs)  # type: ignore[arg-type]
        # Bounded retry: 3 attempts for JSON parse / truncation
        attempts = []
        def _complete_once() -> str:
            resp = self._backend.complete(request)
            raw = resp.text or ""
            # Progress: backend's usage sink will automatically log usage.ndjson with label glossary_resolver
            if self._progress and hasattr(self._progress, "emit"):
                try:
                    self._progress.emit("glossary_resolver_attempt", chapter_id=chapter_id, attempt=len(attempts)+1)
                except Exception:
                    pass
            attempts.append(raw)
            return raw

        retry_policy = JsonRetryPolicy(max_retries=2, base_delay_seconds=0.0)
        try:
            raw = retry_json_call(_complete_once, retry_policy, label="glossary_resolver")
        except (EmptyResponseError, TruncatedJSONError, ValueError, Exception) as exc:
            LOG.warning("glossary_resolver failed for %s after %d attempts: %s", chapter_id, len(attempts), exc)
            if self._progress and hasattr(self._progress, "emit"):
                try:
                    self._progress.emit("glossary_resolver_failed", chapter_id=chapter_id, error=str(exc))
                except Exception:
                    pass
            return None

        # Parse raw into proposals
        try:
            payload = parse_json_response(raw)
            # Expect {"proposals": [...]}
            if not isinstance(payload, dict) or "proposals" not in payload:
                raise ValueError("response must contain proposals")
            proposals = payload["proposals"]
            if not isinstance(proposals, list):
                raise ValueError("proposals must be list")
            # Normalize each proposal: ensure required fields, fill defaults?
            # For now, assume model returns correct shape; we will validate later
            # Build sidecar payload skeleton without identity (caller will stamp)
            return {"raw_proposals": proposals, "raw_payload": payload}
        except Exception as exc:
            LOG.warning("glossary_resolver parse failed for %s: %s", chapter_id, exc)
            return None

def build_sidecar_payload(
    *,
    chapter_id: str,
    snapshot_hash: str,
    config_identity: str,
    candidate_input_hash: str,
    translation_hash_val: str,
    model_ref: str,
    backend_identity: str,
    proposals: List[Dict[str, Any]],
    glossary_view_hash: str = "",
    glossary_view_version: str = "",
) -> Dict[str, Any]:
    return {
        "schema": GLOSSARY_PROPOSAL_SCHEMA,
        "chapter_id": chapter_id,
        "snapshot_hash": snapshot_hash,
        "config_identity": config_identity,
        "resolver_version": RESOLVER_VERSION,
        "prompt_version": PROMPT_VERSION,
        "response_schema": RESPONSE_SCHEMA,
        "model_ref": model_ref,
        "backend_identity": backend_identity,
        "candidate_input_hash": candidate_input_hash,
        "translation_hash": translation_hash_val,
        "glossary_view_hash": glossary_view_hash,
        "glossary_view_version": glossary_view_version,
        "proposals": proposals,
    }

