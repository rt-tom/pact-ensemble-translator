"""Gate precedence, candidate claim handling, quarantine/duplicate/conflict rejection narrower tests."""
import pytest
from pact_v4.runtime.book_memory_gate import evaluate_gate
from pact_v4.audit.entity_extractor import EntityRecord, AnchorRef, AliasRef, EntityClaim, EvidenceRef

def make_record(entity="Blake Thorburn", memory_class="named_character", memory_worthy=True, anchor_pid="p00001", aliases=None, claims=None, glossary_worthy=True):
    anchor = AnchorRef(pid=anchor_pid, span=f"{entity} anchor", status="verified")
    al = tuple(AliasRef(surface=s, pid=p, span=s) for s,p in (aliases or []))
    cl = tuple(EntityClaim(kind=k, value=v, status=s, evidence=tuple(EvidenceRef(pid=ev, span=v) for ev in evs), evidence_windows=()) for k,v,s,evs in (claims or []))
    return EntityRecord(entity=entity, canonical_type="person", anchor=anchor, aliases=al, claims=cl, glossary_worthy=glossary_worthy, memory_class=memory_class, memory_worthy=memory_worthy)

def test_identity_first_over_explicit_allow():
    policy = {"explicit_allow": {"Bad Entity": {}}, "explicit_deny": [], "approved_terms": [], "aliases": {}}
    source_map = {"p00001": "Hello world"}
    # record with anchor pid not in source_pids -> invalid_identity even though explicitly allowed
    rec = make_record(entity="Bad Entity", anchor_pid="p99999")
    ok, code = evaluate_gate(rec, policy=policy, source_map=source_map, current_chapter="0001_bonds-1-1", source_pids={"p00001"}, duplicate_names=set(), conflicts=set(), quarantined_pids=set())
    assert not ok and code == "invalid_identity"

def test_deny_over_allow():
    policy = {"explicit_allow": {"Foo": {}}, "explicit_deny": ["Foo"], "approved_terms": [], "aliases": {}}
    source_map = {"p00001": "Foo appears"}
    rec = make_record(entity="Foo")
    ok, code = evaluate_gate(rec, policy=policy, source_map=source_map, current_chapter="0001", source_pids={"p00001"})
    assert not ok and code == "explicit_deny"

def test_allow_bypasses_model_veto_and_class_check():
    policy = {"explicit_allow": {"car": {}}, "explicit_deny": [], "approved_terms": [], "aliases": {}}
    source_map = {"p00001": "car here"}
    rec = make_record(entity="car", memory_class="chapter_local", memory_worthy=False)
    ok, code = evaluate_gate(rec, policy=policy, source_map=source_map, current_chapter="0001", source_pids={"p00001"})
    assert ok

def test_model_veto_blocks():
    policy = {"explicit_allow": {}, "explicit_deny": [], "approved_terms": [], "aliases": {}}
    source_map = {"p00001": "Blake Thorburn appears"}
    rec = make_record(entity="Blake Thorburn", memory_worthy=False)
    ok, code = evaluate_gate(rec, policy=policy, source_map=source_map, current_chapter="0001", source_pids={"p00001"})
    assert not ok and code == "model_veto"

def test_class_check_generic_object_rejected():
    policy = {"explicit_allow": {}, "explicit_deny": [], "approved_terms": [], "aliases": {}}
    source_map = {"p00001": "car is here"}
    rec = make_record(entity="car", memory_class="named_artifact", memory_worthy=True)
    ok, code = evaluate_gate(rec, policy=policy, source_map=source_map, current_chapter="0001", source_pids={"p00001"})
    # car is generic_object pattern, should be rejected
    assert not ok and code in ("generic_object", "generic_role")

def test_world_term_requires_approval():
    policy = {"explicit_allow": {}, "explicit_deny": [], "approved_terms": ["Demesnes"], "aliases": {}}
    source_map = {"p00001": "UnknownTerm appears"}
    rec = make_record(entity="UnknownTerm", memory_class="world_term", memory_worthy=True)
    ok, code = evaluate_gate(rec, policy=policy, source_map=source_map, current_chapter="0001", source_pids={"p00001"})
    assert not ok and code == "term_not_approved"

def test_quarantined_evidence_rejects():
    policy = {}
    source_map = {"p00001": "Blake Thorburn appears"}
    rec = make_record(entity="Blake Thorburn", anchor_pid="p00001")
    ok, code = evaluate_gate(rec, policy=policy, source_map=source_map, current_chapter="0001", source_pids={"p00001"}, quarantined_pids={"p00001"})
    assert not ok and code == "quarantined_evidence"

def test_duplicate_rejects():
    # Duplicate across chapters is merged via promotion, gate does not reject duplicate (handled via merge). Test conflict instead.
    policy = {}
    source_map = {"p00001": "Blake Thorburn appears"}
    rec = make_record(entity="Blake Thorburn")
    ok, code = evaluate_gate(rec, policy=policy, source_map=source_map, current_chapter="0001", source_pids={"p00001"}, conflicts={"blake thorburn"})
    assert not ok and code == "conflict"

def test_conflict_rejects():
    policy = {}
    source_map = {"p00001": "Blake Thorburn appears"}
    rec = make_record(entity="Blake Thorburn")
    ok, code = evaluate_gate(rec, policy=policy, source_map=source_map, current_chapter="0001", source_pids={"p00001"}, conflicts={"blake thorburn"})
    assert not ok and code == "conflict"

def test_candidate_claim_identity_still_eligible():
    # Candidate claims do not affect identity eligibility; they are withheld at promotion layer but gate passes identity
    policy = {}
    source_map = {"p00001": "Blake Thorburn appears"}
    rec = make_record(entity="Blake Thorburn", claims=[("alias_relation", "B. Thorburn", "candidate", ["p00002"])])
    ok, code = evaluate_gate(rec, policy=policy, source_map=source_map, current_chapter="0001", source_pids={"p00001", "p00002"})
    assert ok

def test_quarantine_alias_pid_blocks():
    policy = {}
    source_map = {"p00001": "Blake Thorburn appears", "p00002": "Blake appears"}
    rec = make_record(entity="Blake Thorburn", aliases=[("Blake", "p00002")])
    ok, code = evaluate_gate(rec, policy=policy, source_map=source_map, current_chapter="0001", source_pids={"p00001", "p00002"}, quarantined_pids={"p00002"})
    assert not ok and code == "quarantined_evidence"
