# Design: Normalize format-variant PID references in entity-context validation

## Current behavior

`validate_entity_context` (and `_validate_claim`) check every PID reference —
`anchor.pid`, `aliases[].pid`, `evidence[].pid`, and the two endpoints of each
`evidence_windows` range — with a strict `pid not in source_map` membership test
against the chapter's real PID keys (`p00001`..`p00279`). The harness lists the
valid PIDs explicitly and the model is told a PID not in that list does not exist
(dead-PID guard, hardened after book-run 1-3 PID invention).

The model wrote `evidence_windows` as bare integers (`[[86,86]]`) even though it
used correct `p00086` strings in `anchor.pid` / `evidence[].pid`. The strict
membership test therefore flagged every window endpoint as a dead PID and dropped
the enclosing claim.

## New behavior

Map every model-supplied PID reference to the canonical `pNNNNN` key before the
existence check, using the chapter's real PID set as the authority:

- Build a reverse map `int(value) -> canonical_key` from `source_map` keys
  (`p00086` → `86 -> p00086`).
- `_normalize_pid(pid, int_map)` extracts the digits of any reference and, if
  the integer exists in the map, returns the canonical key; otherwise it returns
  the reference unchanged so a genuinely unknown PID still fails the existence
  check.
- Apply normalization to `anchor.pid`, each `alias.pid`, each
  `evidence[].pid`, and both endpoints of each `evidence_windows` range, at the
  point of the existence/verbatim checks **and** when reconstructing the stored
  `EntityClaim` / `AnchorRef` / `AliasRef` so the cached context stays canonical.

This preserves the dead-PID guard's intent (unverifiable evidence is never
silently accepted) while accepting the observed format variants.

## Implementation sites

1. `pact_v4/audit/entity_extractor.py`
   - Add `_build_pid_int_map(source_map)` and `_normalize_pid(pid, int_map)`.
   - In `validate_entity_context`, build the int map once from `source_map`;
     normalize `record.anchor.pid` and each `alias.pid` before their checks and
     when building `verified_anchor` / kept aliases.
   - Pass the int map into `_validate_claim`; normalize `claim_pids` (evidence +
     window endpoints) and each `evidence[].pid`; return the claim with canonical
     PIDs in `evidence` and `evidence_windows`.
   - Normalize PID references used by the gender-evidence referent-link check.

## Tests

- Unit test: `_normalize_pid` maps `p00086`/`00086`/`p86`/`86` → `p00086` for a
  chapter whose PIDs are `p00001`..`p00279`, and leaves `999` unchanged.
- End-to-end: feed the chapter-3 style payload (bare-int `evidence_windows`) and
  assert the claims are **accepted** (no dead-PID drop) while a reference whose
  integer is outside the real PID set is still dropped.
- Run the focused `tests/pact_v4/audit/` (entity-extraction) suite and
  `pact-fidelity-lint`; no model call or pipeline.

## Risks (accepted)

- A model reference that is a real PID integer but attached to the wrong passage
  is still normalized to that PID and may then pass the verbatim check if the
  span happens to be present there. This is acceptable: the guard still requires
  a verbatim span in the (canonical) PID, so a fabricated PID cannot pass, and a
  mislabeled-but-real PID is a milder, self-correcting error than the current
  total-drop behavior. No provider/runtime/persistent-format change.
