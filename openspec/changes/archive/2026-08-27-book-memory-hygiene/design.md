## Context

See `proposal.md` and delta specs. Live state has 45 `characters`, 169 `entities`, two durable facts, cross-section duplicates, stale metadata, and many chapter-local objects/descriptions. Current indexing flattens `characters` and `entities` into a character list. B1.2 chapter evidence is still required by B3 for object identity. The resolver has identity-bearing `off`, `shadow`, and `promote`, but `v4_book_run` currently couples memory promotion to glossary sidecar handling, and `MemoryManager.promote()` can write categories sequentially.

Production authority is not the mutable worker directory: it is a Media snapshot revision containing exactly four canonical state files and accepted through lease/parent/CAS validation. This design therefore treats both migration and runtime promotion as four-file transactions.

## Goals / Non-Goals

**Goals:**

- Make durable memory a compact causal story bible rather than an entity dump.
- Preserve complete current-chapter B1.2 evidence for audit.
- Define implementable classification, policy, provenance, reports, and persistent schemas.
- Prevent backward leakage of later aliases/attributes.
- Make glossary `promote` default while preserving explicit `off`/`shadow`.
- Make book-memory policy independent and default it to `promote_verified`.
- Stage glossary and memory changes transactionally and migrate via Media revision publication.

**Non-Goals:**

- No second model call for book-memory approval.
- No change to translation output, resolver reviewer transport/configuration, or model budgets.
- No deletion or mutation of historical snapshots, raw B1.2 caches, source evidence, or audit artifacts.
- No inference that two English entities are identical merely because they share a Russian target.
- No production migration or pipeline execution during implementation without separate owner approval.

## Decisions

### 1. Separate chapter evidence from durable memory

B1.2 cache remains the complete source-validated chapter record, including ordinary objects and `candidate` claims. A new durable gate consumes it but does not alter it. Thus `motorcycle → bike` can help B3 even when neither surface becomes a durable entity.

### 2. Version the B1.2 classification contract

Each entity contains:

```json
{
  "memory_class": "named_character|named_place|named_group|named_artifact|named_creature|world_term|chapter_local",
  "memory_worthy": true
}
```

Both fields are required. `memory_worthy=false` is a model veto; `true` only admits the record to code checks. The entity extractor prompt/schema/version, validation report, cache schema/key, and run identity are bumped together. Old/missing/unknown fields cause cache miss/recompute and are never interpreted as false, true, or `chapter_local` silently.

A named identity needs only a validated anchor. Additional alias/gender/fact data require their own `verified` claims. `candidate` claims remain B3-only.

### 3. Define deterministic gate precedence and policy location

The gate order is fixed:

1. exact current chapter/source/extractor identity;
2. explicit deny override;
3. explicit allow/alias override;
4. model veto;
5. class-specific code checks;
6. duplicate/conflict/quarantine checks.

The per-book policy is stored inside the canonical `book_memory.json` policy block so it travels in the four-file snapshot:

```json
{
  "schema": "pact-v4-book-memory/v2",
  "book_memory_policy_version": "book-memory-policy/v1",
  "policy": {
    "explicit_deny": [],
    "explicit_allow": {},
    "aliases": {},
    "approved_terms": [],
    "generic_patterns_version": "generic-memory-reject/v1"
  }
}
```

Code-owned generic patterns reject common objects, generic roles, possessive scene objects, and anonymous descriptions. Policy lists are exact normalized surfaces. `world_term` requires approved-term membership; model output cannot add a term to policy. Every rejection has a stable code from the spec.

This is deliberately conservative. The alternative—title-case-only or free-form model approval—would re-admit `Bathroom Mirror`, `Hatchet`, or `Little Boy`.

### 4. Use per-field and per-alias provenance

Durable records retain compatibility fields but add classification and provenance:

```json
{
  "type": "person",
  "memory_class": "named_character",
  "first_seen_chapter": "0001_bonds-1-1",
  "chapters": ["0001_bonds-1-1"],
  "variants": {
    "Steph": {"chapter": "0001_bonds-1-1", "source_pids": ["p00026"]}
  },
  "field_provenance": {
    "gender": {"chapter": "0001_bonds-1-1", "source_pids": ["p00036"]}
  }
}
```

Loaders tolerate legacy scalar variant counts only during migration; v2 writers emit provenance objects. For historical fields whose exact provenance cannot be reconstructed from immutable source/entity artifacts, migration fails closed: preserve identity, but omit that alias/attribute from historical prompt indexes until it has verified provenance. The authoritative glossary remains the source for Russian forms.

### 5. Define chapter-index v2 exactly

The file keeps chapter IDs as top-level keys for tolerant existing lookups and reserves metadata keys:

```json
{
  "$schema": "pact-v4-chapter-index/v2",
  "$book_memory_policy_version": "book-memory-policy/v1",
  "0002_bonds-1-2": {
    "characters": [{"name": "Blake Thorburn", "gender": "male"}],
    "named_entities": ["Hillsglade House"],
    "terms": ["Demesnes"],
    "facts": ["..."],
    "address": []
  }
}
```

`characters` may contain strings or `{name, gender, role}` snapshots for compatibility with the renderer; attributes are included only when their field provenance is strictly earlier than the target chapter. `named_entities` and `terms` are separate. Alias presence matching uses only aliases learned strictly before the target chapter. Facts and addresses preserve existing causal rules.

Snapshot/cache identity binds to selected entry, schema, and policy version. New readers reject unsupported schema/policy to narrator+seed fail-soft. Existing code that only performs `.get(chapter_id)` may continue, but implementation tests all consumers; any reader that would flatten new scopes is updated or explicitly rejects v2.

### 6. Independent mode state machine

Glossary modes:

- `promote` (default): call resolver, validate sidecar, stage valid observations;
- `shadow`: call resolver and write proposal sidecar/report, no glossary mutation;
- `off`: no resolver call or proposal sidecar; write only a disabled status artifact.

Book-memory policies:

- `promote_verified` (default): classify/report and stage eligible verified observations;
- `observe`: classify/report without book-memory mutation;
- `off`: no durable-memory mutation; B1.2 may still run for B3 and the report records disabled policy.

Each policy controls only its resource. The full 3×3 matrix is tested, especially glossary `promote` + memory `off`, and glossary `off` + memory `promote_verified`.

### 7. Transactional runtime promotion

A state transaction stages all four canonical files in a private same-filesystem directory, validates exact regular-file set, JSON, schemas, hashes, and expected starting hashes, then writes a durable marker containing transaction ID, pre/post hashes, backup paths, and replacement progress.

Replacement order is deterministic: `glossary`, `book_memory`, `chapter_index`, `observations`; observations are cleared only in the staged committed version. After every replacement, marker progress is fsynced. After all four, post-hashes are verified and the marker is removed. Startup and pre-publish recovery check the marker: any incomplete or hash-mismatched transaction restores all four backups, verifies pre-hashes, and only then clears the marker. Tests inject interruption before/after every replacement and verification step.

This local protocol protects the worker directory. It does not itself confer production authority.

### 8. Media-authoritative migration and publication

Migration never edits a historical snapshot or production state in place. It:

1. reads/freeze-hashes current accepted four-file revision;
2. builds a publishable candidate directory containing exactly the four canonical files — no metadata or extra file is allowed inside this directory — per owner clarification (2026-08-27):
   - `glossary.json` is copied byte-for-byte from the accepted parent unless a separately approved glossary change is explicitly part of the same candidate; `book_memory.json` `canonical_ru` is reconciled *to* that parent glossary, never vice versa;
   - `book_memory.json` is the migrated v2 memory (policy block, per-field/per-alias provenance or conservative omission, deterministic duplicate handling);
   - `chapter_index.json` MUST be rebuilt from the migrated v2 book_memory/policy (not copied from rev-0011), because copying retains contaminated v1 flattened-character prompt content and stale hashes;
   - `observations.json` is preserved byte-for-byte from the accepted parent; if it is nonempty/pending/incompatible with the migrated state, migration fails closed and requires explicit owner-approved reconciliation — it is not silently cleared or regenerated;
3. emits a separate non-publishable transaction envelope containing the complete decision manifest, expected parent revision, approval identity, and candidate hashes;
4. waits for explicit owner approval of that exact manifest/hash set;
5. acquires lease and publishes through existing parent/CAS/exact-file boundary as one new Media revision;
6. verifies Media current points at the accepted revision and exact hashes.

Rollback uses the retained pre-migration snapshot to construct and publish a new approved revision; it never rewrites history or copies files directly into the authoritative current snapshot.

### 9. Migration classification

Dry-run accounts for every input record exactly once as `retain`, `merge`, `move_to_term`, or `reject`. It preserves named people and persistent named entities; moves approved world vocabulary into term scope; rejects generic/chapter-local objects and descriptions; merges normalized cross-section duplicates; and reconciles stale `canonical_ru` only from glossary authority.

`Dowght`/`Dowghty` are an explicit alias-policy exception with an intentionally shared Russian form. A second dry run over migrated input produces identical canonical JSON and manifest.

### 10. Versioned observability

`book_memory_candidates_report.json` includes its schema, chapter/source/snapshot/config hashes, extractor/cache/policy versions, requested/effective modes, terminal status, entity count, and all decisions/evidence. Glossary `off` writes a separate status artifact with `disabled` and zero calls; it does not fabricate `glossary_proposals.json`.

Policy changes are identity-bearing: `book_memory_policy_version` changes, stale reports/indexes are rejected, and the causal index is rebuilt.

## Risks / Trade-offs

- [Risk] Conservative classification misses useful entities. → Keep complete rejected reports/evidence; use explicit reviewed allow/term policy rather than weakening generic gates.
- [Risk] Historical alias provenance is unavailable. → Preserve canonical identity but omit unprovable aliases/attributes from earlier indexes; never guess a first-seen chapter.
- [Risk] v2 index affects prompts/readers. → Exact schema, fail-soft behavior, consumer inventory, compatibility tests, and representative prompt snapshots.
- [Risk] Default glossary mutation introduces bad entries. → Existing resolver safety gates remain unchanged; explicit `shadow`/`off`; inspect candidate/status reports.
- [Risk] Local crash causes partial state. → Marker/backups/fsync/recovery with fault injection at every boundary.
- [Risk] Migration races current state. → Media lease/parent/CAS and candidate expected-parent hash reject stale publication.

## Migration Plan

1. Freeze the accepted current four-file Media revision and immutable hashes; never modify it.
2. Run the migration classifier on a copy and produce a complete retain/merge/move/reject manifest plus post-state hashes.
3. Independently review code, migration manifest, schema compatibility, and rollback candidate procedure.
4. Obtain explicit owner approval for the exact manifest and candidate hash set.
5. Publish the candidate through Media lease/parent/CAS as a new revision; verify current revision and all four hashes.
6. Stop before any pipeline run if verification differs. If rollback is approved, publish the retained pre-migration state as another new revision.
7. Run a small controlled chapter set with defaults; inspect glossary and memory deltas before continuing.

## Open Questions

None. Classification, schema, mode semantics, transaction boundaries, production authority, and approval gates are resolved above.
