"""Phase 3: assembled-chapter audit.

Canonical source: docs/architecture/V4_FINAL_REVIEW_AND_IMPLEMENTATION_PLAN_RU_v2.md
("Исполнимые контракты до реализации" item 6, "## Phase 3 — assembled-chapter
audit").

  * 3A (``pact_v4.phase3.findings``, ``pact_v4.phase3.region_resolver``):
    the immutable finding store and deterministic region resolver.
  * 3B (``pact_v4.phase3.assembly``, ``pact_v4.phase3.audit``): the
    assembled-chapter artifact and the actual Qwen EN<->RU / Gemma
    Russian-only / deterministic-integrity audit ("Step 6" in
    V4_MVP_SPEC_RU.md §2) that produces findings into the 3A store.

Explicitly out of scope here (later phases): formatting-contract / HTML-
structure checks (no v4 runtime formatting artifact exists yet — Phase 5,
not built), repair execution, challenge/dispute flow, convergence loop,
terminal-state transitions, and candidate selection/scoring (that last one
is Phase 2C, already implemented separately).
"""
