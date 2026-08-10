"""Pact v4.1 audit package (B-phase).

Submodules (task cards in docs/plans/V4_1_AUDIT_B1_RU.md §10):

* ``chunked_audit`` — B1: ChunkedAuditEvaluator (chunked Qwen audit, prompt
  v4.1, overlap, RetryShrink, fail-closed validation).
* ``hard_filters`` — B1.1: Tier A hard deterministic filters applied to
  findings BEFORE repair (0 model calls).
* ``entity_extractor`` — B1.2: ChapterEntityContext extractor (Qwen
  source-only prepass).
"""

from pact_v4.audit.entity_extractor import (
    BackendEntityExtractor,
    BackendEntityExtractorConfig,
    ChapterEntityContext,
    ENTITY_CONTEXT_SCHEMA,
    ENTITY_EXTRACTION_V1,
    EXTRACTOR_VERSION,
    EntityClaim,
    EntityContextCache,
    EntityExtractionResult,
    EntityRecord,
    ValidationEntry,
    ValidationReport,
    entity_context_cache_key,
    extract_entity_context,
    parse_model_output,
    render_entity_extraction_prompt,
    validate_entity_context,
    with_entity_context_metadata,
)

__all__ = [
    "BackendEntityExtractor",
    "BackendEntityExtractorConfig",
    "ChapterEntityContext",
    "ENTITY_CONTEXT_SCHEMA",
    "ENTITY_EXTRACTION_V1",
    "EXTRACTOR_VERSION",
    "EntityClaim",
    "EntityContextCache",
    "EntityExtractionResult",
    "EntityRecord",
    "ValidationEntry",
    "ValidationReport",
    "entity_context_cache_key",
    "extract_entity_context",
    "parse_model_output",
    "render_entity_extraction_prompt",
    "validate_entity_context",
    "with_entity_context_metadata",
]
