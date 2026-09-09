## Why

In book run `book_0002-0003_remote_20260828_132450_710464` (chapter `0003_bonds-1-3`), the entity-context validation dropped **14 of 15 claims** — not because the chapter lacked evidence, but because the model wrote `evidence_windows` as bare integers (`[[86,86]]`, `[[28,28]]`, `[[248,248]]` …) while the real PID set uses `p`-prefixed keys (`p00001`..`p00279`). The §8.3 point-2 "dead PID" guard scans window endpoints as PID references, so `86 ≠ p00086` → every claim dropped. The cached chapter-3 entity context therefore carried no accepted claims, so almost nothing from chapter 3 reached durable `book_memory` even though the entities (Hillsglade House, Blake, Rose, Molly Walker, Rosalyn D. Thorburn, The Index, Essentials/Famulus/Implementum/Demesnes/Dramatis Personae) are genuinely present and the model supplied correct `p`-prefixed PIDs in `anchor.pid` / `evidence[].pid`.

Owner decision (2026-08-28): accept format-variant PID references. If the model wrote `p00086`, `00086`, or `86`, all are legitimate and must resolve to the same canonical `p00086`.

## What Changes

- Normalize every model-supplied PID reference **before** the existence check: `p00086`, `00086`, `p86`, and `86` all canonicalize to `p00086` when the integer matches a real source PID.
- Preserve the dead-PID guard: a reference whose integer does **not** match any real PID (e.g. `999` when the chapter stops at `279`) still fails as dead — the guard still rejects genuinely unknown PIDs.
- Normalize stored `anchor` / `alias` / `claim evidence` / `evidence_windows` PIDs to canonical form so cached context and downstream promotion see consistent `p`-prefixed PIDs.

## Capabilities

### Added Capabilities
- `entity-context-validation`: the §8.3 entity-context validation SHALL accept format-variant PID references (the `p` prefix and leading zeros are optional) by canonicalizing to the real source PID before the existence/verbatim checks, while still rejecting PIDs whose integer is absent from the chapter's real PID set.

## Impact

Localized change to `pact_v4/audit/entity_extractor.py` validation only. Medium risk (alters which claims reach durable memory). No model call, provider/runtime change, persistent-format change, or pipeline run. Owner-evaluated; test on the dev branch; no merge to `main` without approval.
