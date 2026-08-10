"""Pact v4.1 audit package (B-phase).

Submodules (task cards in docs/plans/V4_1_AUDIT_B1_RU.md §10):

* ``chunked_audit`` — B1: ChunkedAuditEvaluator (chunked Qwen audit, prompt
  v4.1, overlap, RetryShrink, fail-closed validation).
* ``hard_filters`` — B1.1: Tier A hard deterministic filters applied to
  findings BEFORE repair (0 model calls).
* ``entity_extractor`` — B1.2: ChapterEntityContext extractor (Qwen
  source-only prepass).

This package deliberately imports nothing at package import time: the
submodules are wired by the pipeline, not by ``import pact_v4.audit``, so a
partially-landed B-phase (e.g. only B1.1 merged) never breaks unrelated
imports.
"""
