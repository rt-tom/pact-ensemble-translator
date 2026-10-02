## Approval gate

- [x] 0.1 Owner reviews and approves proposal/spec/design before implementation. The owner-reported full-chapter trial supports the selected protocol; this approval does not authorize a pipeline run or deployment.

## Implementation (after approval only)

- [x] 1.1 Add a separate V4 whole-chapter-only template/version derived from the shared template; replace its existing JSON output-contract block in place with the exact verbatim block in `design.md`. Keep the shared chunked template unchanged, label the whole-chapter OWNED_SOURCE map as TARGET, and scope its final pre-output checklist to the line contract while preserving fidelity/risk checks.
- [x] 1.2 Replace whole-chapter raw JSON parsing with a dedicated line-response parser that preserves punctuation after the first colon and validates the exact ordered PID sequence, uniqueness, and non-empty translations.
- [x] 1.3 Disable JSON-constrained response mode for the whole-chapter translation call only. Preserve non-translation JSON roles and their parsing/response constraints.
- [x] 1.4 Ensure valid parsed output feeds the unchanged candidate/PID map and pipeline stages. Keep `translations_raw.json` as a JSON PID-to-text map and per-attempt raw diagnostic files as exact response text.
- [x] 1.5 Bind the new prompt/response contract to generation identity/cache so old JSON-contract outcomes cannot be reused under the line contract.

## Offline validation

- [x] 2.1 Add focused tests for valid lines, punctuation/colons within text, and missing/extra/duplicate/reordered/malformed PID lines, blank/continuation lines, and empty segment text; verify retry/error classification.
- [x] 2.2 Test valid output produces the same candidate translation mapping and `translations_raw.json` retains its JSON PID-to-text shape; verify per-attempt raw files preserve exact line response.
- [x] 2.3 Verify the exact prompt block/TARGET alias, unchanged chunked JSON template and parser, non-translation JSON requests/parsers, and absence of contradictory JSON wording in the whole-chapter final checklist.
- [x] 2.4 Run the narrowest relevant V4 generation/strict-runner tests through the worktree `.venv`, fidelity lint, `openspec validate translator-line-output --strict`, and `git diff --check`. Record the owner-reported full-chapter trial as prior manual evidence, not as a test run by the implementation agent.
- [x] 2.5 Obtain independent `pact-rev` review. Do not run a pipeline, model server, deployment, merge, or production artifact operation without separate owner approval.