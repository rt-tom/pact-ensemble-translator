## Why

The V4 whole-chapter generator currently requires a strict ordered PID-keyed JSON object. A malformed quote, comma, key, or truncated JSON can invalidate an otherwise useful full-chapter translation and trigger a costly whole-chapter retry. The owner tested a PID-prefixed, one-segment-per-line format on a full chapter that had previously suffered JSON failures; it completed without errors and followed the output contract.

## What Changes

- Change **V4 whole-chapter translator responses only** from JSON to one `PID: translated text` record per line, preserving exact TARGET PID and source order.
- Add the agreed verbatim output-contract block to the generation prompt so the implementation does not invent or paraphrase the contract.
- Parse and validate the line protocol into the same existing ordered PID-to-text translation mapping.
- Continue writing `translations_raw.json` as the validated generator snapshot in its existing JSON mapping format. Preserve all downstream stage inputs and persisted artifact formats.
- Keep bounded retry and all non-translation model JSON contracts unchanged.

## Non-goals

- No automatic repair or inference for missing, duplicated, reordered, or malformed PID records.
- No change to model selection, sampling, retries, chunking, translation quality rules, audit/repair policies, cache semantics beyond any necessary prompt identity versioning, or pipeline lifecycle.
- No pipeline run, production edit, deployment, merge, or migration.

## Capabilities

### New Capabilities
- `translator-line-output`: line-oriented PID-labelled whole-chapter translation response contract.

## Impact and approval boundary

Medium risk: this changes the V4 whole-chapter translator prompt and raw-response parser/validation path, principally `pact_v4/phase2/generation.py` and its prompt construction/call adapter. It must not change other roles' JSON parsing or response contracts. `translations_raw.json` and downstream translation mappings remain unchanged. Implementation requires owner approval of this proposal/spec/design before code changes.