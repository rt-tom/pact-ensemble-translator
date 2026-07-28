import pytest
from pact_v4.phase1.models import (
    Provenance, Snapshot, ChunkPlan, Candidate, Finding, Repair, TerminalState, validate_json_complete
)

def test_provenance_validation():
    with pytest.raises(ValueError, match="Foreign identity cannot be empty"):
        Provenance(source_system="test", foreign_id="", timestamp=123.0)
    
    p = Provenance(source_system="test", foreign_id="ext-123", timestamp=123.0)
    assert p.foreign_id == "ext-123"

def test_snapshot_duplicate_pids():
    with pytest.raises(ValueError, match="Duplicate PIDs are not allowed"):
        Snapshot(pids=["p1", "p1"], context="ctx")

def test_chunk_plan_duplicate_pids():
    with pytest.raises(ValueError, match="Duplicate PIDs are not allowed"):
        ChunkPlan(chunk_id="c1", pids=["p1", "p2", "p1"])

def test_terminal_state_monotonicity():
    prov = Provenance(source_system="sys", foreign_id="123", timestamp=1.0)
    state = TerminalState(state_id="s1", status="pending", provenance=prov)
    
    # Valid
    state.transition_to("in_progress")
    assert state.status == "in_progress"
    
    # Invalid
    with pytest.raises(ValueError, match="Non-monotonic terminal transition"):
        state.transition_to("pending")

def test_json_completeness():
    with pytest.raises(ValueError, match="Reject partial or invalid JSON"):
        validate_json_complete('{"key": "value"') # partial
        
    res = validate_json_complete('{"key": "value"}')
    assert res == {"key": "value"}
