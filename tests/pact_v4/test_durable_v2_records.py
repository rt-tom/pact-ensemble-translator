"""Durable v2 record fields: memory_class, first_seen, variants provenance, field_provenance."""
import json, tempfile
from pathlib import Path
from pact_v4.audit.entity_extractor import ChapterEntityContext, EntityRecord, AnchorRef, AliasRef, EntityClaim, EvidenceRef
from pact_v4.pipeline.b3_audit_repair import book_memory_observations_from_entity_context

def test_durable_record_has_v2_fields():
    ctx = ChapterEntityContext(
        schema="pact-v4-chapter-entity-context/v2",
        extractor_version="pact-v4-entity-extractor/v3",
        chapter_id="0001_bonds-1-1",
        source_hash="abc",
        entities=(
            EntityRecord(
                entity="Blake Thorburn",
                canonical_type="person",
                anchor=AnchorRef(pid="p00001", span="Blake Thorburn is here"),
                aliases=(AliasRef(surface="Blake", pid="p00002", span="Blake walked"),),
                claims=(EntityClaim(kind="gender", value="male", status="verified", evidence=(EvidenceRef(pid="p00003", span="he is"),), evidence_windows=()),),
                glossary_worthy=True,
                memory_class="named_character",
                memory_worthy=True,
            ),
        ),
    )
    obs = book_memory_observations_from_entity_context(ctx, chapter_id="0001_bonds-1-1")
    bm = obs["book_memory"]
    key = "characters:Blake Thorburn"
    assert key in bm
    rec = bm[key]
    assert rec["memory_class"] == "named_character"
    assert rec["first_seen_chapter"] == "0001_bonds-1-1"
    assert "variants" in rec and "Blake" in rec["variants"]
    prov = rec["variants"]["Blake"]
    assert prov["chapter"] == "0001_bonds-1-1"
    assert prov["source_pids"] == ["p00002"]
    assert "field_provenance" in rec and "gender" in rec["field_provenance"]
    gp = rec["field_provenance"]["gender"]
    assert gp["chapter"] == "0001_bonds-1-1"
    assert gp["source_pids"] == ["p00003"]

def test_durable_world_term_record():
    ctx = ChapterEntityContext(
        schema="pact-v4-chapter-entity-context/v2",
        extractor_version="pact-v4-entity-extractor/v3",
        chapter_id="0002_bonds-1-2",
        source_hash="abc",
        entities=(
            EntityRecord(
                entity="Demesnes",
                canonical_type="term",
                anchor=AnchorRef(pid="p00010", span="Demesnes are places"),
                aliases=(),
                claims=(),
                glossary_worthy=False,
                memory_class="world_term",
                memory_worthy=True,
            ),
        ),
    )
    obs = book_memory_observations_from_entity_context(ctx, chapter_id="0002_bonds-1-2")
    # world_term goes to entities section per promotion logic
    assert "entities:Demesnes" in obs["book_memory"]
    rec = obs["book_memory"]["entities:Demesnes"]
    assert rec["memory_class"] == "world_term"
