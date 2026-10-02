## Decision frame

The relevant path is the **V4 whole-chapter generator**, not the legacy `pact_translate_v3.py` chunk translator. V4 currently validates model output with `validate_whole_chapter_raw` in `pact_v4/phase2/generation.py`, which requires an ordered JSON PID map and participates in bounded retries. A successful candidate is converted to the existing ordered PID-to-text map, then `pact_v4/pipeline/v4_phase12_strict_runner.py` writes that validated map to `translations_raw.json` before QA/repair. The V4 runner separately preserves each unparsed wire response in `whole_chapter_attempt{N}_raw.txt` for diagnostics.

The owner has tested the proposed line format on a full chapter that previously exposed JSON failures and reports no errors and excellent contract adherence. This is strong operational evidence for the format; focused automated parser/prompt tests remain required.

## 1. Verbatim prompt contract

The shared generation prompt already has a section headed `OUTPUT CONTRACT — MANDATORY AND AUTHORITATIVE:` and is used by both whole-chapter and chunked JSON generation. The implementation SHALL create a separate whole-chapter-only template/version derived from the shared template, replacing that block in the whole-chapter variant in place with the following text. It SHALL NOT change the shared chunked template or its JSON contract. Use example PIDs consistent with the request. The whole-chapter rendered input SHALL identify the existing OWNED_SOURCE PID map as TARGET (e.g. `TARGET (OWNED_SOURCE):`) so the verbatim contract's TARGET reference is explicit; do not rewrite the approved block to say OWNED_SOURCE:

```text
OUTPUT CONTRACT — MANDATORY:

Return only the translations for the PIDs in TARGET, in the exact order shown there.
Output exactly one line per TARGET PID, using this format:

p00001: Первая переведённая строка.
p00002: Вторая переведённая строка.

Preserve each TARGET PID exactly and output it exactly once.
Put exactly one space after the colon.
Keep each translation on a single line; do not merge or split segments.
Do not output context-only PIDs.
Do not output JSON, Markdown, headings, blank lines, notes, or commentary.
Do not add wrapper quotation marks around PIDs or translations.
Preserve punctuation and quotation marks that belong in the Russian translation.
```

The implementation SHALL not paraphrase, shorten, or independently redesign this block. Since the final pre-output checklist currently has a JSON-specific instruction, the whole-chapter variant SHALL retain its existing fidelity/risk checks but replace only the contradictory JSON output-contract check with checks for the approved line contract. The chunked prompt's JSON checklist remains unchanged. No other prompt instructions change.

## 2. Parser and validation

Use the separate whole-chapter template/version only when the generation request is the V4 whole-chapter call. Replace only that call's model-response JSON parsing requirement with a dedicated line-protocol parser; chunked generation continues using the shared JSON template, JSON response mode, and JSON parser unchanged.

- Parse each physical line as one record. Split only at the first colon; all later colons belong to translation text.
- Require the parsed PID sequence to equal the exact whole-chapter PID sequence in source order. Reject missing, extra, duplicate, malformed, or reordered records. Never silently discard, reorder, infer, or merge records.
- Require non-empty translation text after the one-space separator. Reject blank or continuation lines; do not attach malformed lines to a neighboring segment.
- Produce the same ordered PID-to-text tuple/map that the current downstream candidate code consumes. Keep existing source/PID integrity, translation-quality validation, cache candidate validation, retry bounds, journal outcomes, and failure taxonomy semantics as far as possible; classify line-contract violations through the existing invalid-output/PID-mismatch paths.

## 3. Downstream and artifact contracts

No downstream stage should receive the model's line-formatted raw string as its translation map. The parser converts a valid response into the same in-memory PID-to-text mapping currently produced from JSON.

- `translations_raw.json` SHALL remain a JSON object/map of PID keys to validated Russian text values, written by the strict runner before QA/repair. Its schema and role as the raw (pre-QA/repair) validated generator snapshot do not change.
- `translations.json`, repaired/final translation files, candidate/cache/journal structures, monitoring snapshots, and subsequent audit/repair/formatting steps keep their existing contracts.
- Per-attempt `whole_chapter_attempt{N}_raw.txt` diagnostic files SHALL continue to contain the exact model response text as received; after the change this text is the PID-colon line format, not JSON. Do not replace these diagnostic files with normalized JSON.

## 4. API response mode and change isolation

The whole-chapter translator must no longer request or depend on JSON-constrained response mode. Identify the relevant V4 backend adapter/structured-output path and disable JSON mode for this generator only. Preserve JSON response constraints and parsers for audit, repair, formatting, and all other JSON-contract roles. If removing JSON mode requires a shared adapter extension, make it an explicit call-scoped option; do not mutate persistent/shared client state.

Bind the changed prompt/response contract into the existing generation identity/cache mechanism so a prior JSON-contract cached outcome is not reused as if it were produced under the line contract. Do not change any unrelated model or runtime settings.

## 5. Verification and boundaries

Focused offline tests SHALL cover valid lines, colons and punctuation within translated text, exact full PID sequence, and malformed/missing/extra/duplicate/reordered PIDs, blank/continuation lines, empty segment text, and retry classification. Test that valid lines produce the same candidate translation mapping and that `translations_raw.json` remains the same JSON PID-to-text object shape. Verify the chunked template/prompt and its JSON contract remain unchanged, the whole-chapter template carries the exact approved replacement block and TARGET alias, and its final pre-output checklist does not contradict the line contract. Verify other roles' JSON contracts remain unchanged.

Run focused V4 generation/strict-runner tests through the worktree `.venv`, `pact-risk-test` guidance, `pact-fidelity-lint` for the translator prompt, OpenSpec strict validation, and `git diff --check` after owner approval and implementation. Do not execute a pipeline or model server, use source text externally, edit RT, deploy, merge, or change production artifacts under this change. Each requires a separate owner decision.