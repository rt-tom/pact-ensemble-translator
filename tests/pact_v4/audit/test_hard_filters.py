"""B1.1 contract tests for pact_v4.audit.hard_filters (Tier A hard filters).

Acceptance (card t_05c04093):
- p00132-type exact adjacent duplicate -> CONFIRMED
- «Две минуты первого = 1:02»-FP -> REJECTED (number/time normalization)
- nurse-issue with explicit current-source fact (Rich male) -> REJECTED
- entity-relation issue (bike=motorcycle) -> NOT Tier A (TIER_B, §5.3)
- compact regression tests; full suite passes
"""
from __future__ import annotations

import pytest

from pact_v4.audit.hard_filters import (
    CONFIRMED,
    REJECTED,
    TIER_B,
    apply_hard_filters,
    find_adjacent_duplicate,
    normalized_numeric_values,
)


def _issue(pid, category, note="", excerpt="", severity="major", confidence="high"):
    return {
        "id": pid,
        "category": category,
        "severity": severity,
        "confidence": confidence,
        "note": note,
        "excerpt": excerpt,
    }


def _verdicts(issues, source, translation, **kwargs):
    return {
        f.issue["id"]: f
        for f in apply_hard_filters(issues, source=source, translation=translation, **kwargs)
    }


# --- acceptance: duplicates -------------------------------------------------


def test_adjacent_duplicate_confirmed_tier_a():
    issue = _issue(
        "p00132", "addition",
        note="в гости в гости — повторение",
        excerpt="в гости в гости",
    )
    result = _verdicts(
        [issue],
        source={"p00132": "They went to visit their aunt."},
        translation={"p00132": "Они пошли в гости в гости к тёте."},
    )
    assert result["p00132"].verdict == CONFIRMED
    assert result["p00132"].filter_name == "adjacent_duplicate"


def test_duplicate_detector_finds_bigram_and_ignores_hyphen_reduplication():
    assert find_adjacent_duplicate("Они пошли в гости в гости.") == ("в", "гости")
    assert find_adjacent_duplicate("он он") == ("он",)
    # hyphenated Russian reduplication is legitimate, not an error
    assert find_adjacent_duplicate("Да-да, конечно.") is None
    assert find_adjacent_duplicate("Нет ошибок.") is None


# --- acceptance: numbers/time normalization ---------------------------------


def test_time_normalization_fp_rejected():
    # «Две минуты первого = 1:02»-FP: source and translation are the same
    # time (00:02); the auditor's numeric claim is a false positive.
    issue = _issue(
        "p00285", "changed_fact",
        note="Two past twelve translated as Две минуты первого (12:58), "
             "materially changing the time from 12:02.",
        excerpt="Две минуты первого",
    )
    result = _verdicts(
        [issue],
        source={"p00285": "It was two past twelve."},
        translation={"p00285": "Было две минуты первого."},
    )
    assert result["p00285"].verdict == REJECTED
    assert result["p00285"].filter_name == "number_time"


def test_number_word_normalization_ru_genitive():
    # девяти/десяти: genitive number words normalize to the same value.
    assert normalized_numeric_values("Десяти.", "ru") == ((), (10,))
    assert normalized_numeric_values("девяти", "ru") == ((), (9,))
    assert normalized_numeric_values("Ten.", "en") == ((), (10,))


def test_time_normalization_equivalence():
    assert normalized_numeric_values("two past twelve", "en") == ((2,), ())
    assert normalized_numeric_values("две минуты первого", "ru") == ((2,), ())
    assert normalized_numeric_values("half past ten", "en") == ((630,), ())
    assert normalized_numeric_values("половина одиннадцатого", "ru") == ((630,), ())
    assert normalized_numeric_values("five to three", "en") == ((175,), ())
    assert normalized_numeric_values("без пяти три", "ru") == ((175,), ())


def test_real_numeric_mismatch_confirmed():
    issue = _issue(
        "p00099", "changed_fact",
        note="source says twelve chairs but translation says десять (10)",
        excerpt="десять стульев",
    )
    result = _verdicts(
        [issue],
        source={"p00099": "There were twelve chairs in the hall."},
        translation={"p00099": "В зале стояло десять стульев."},
    )
    assert result["p00099"].verdict == CONFIRMED
    assert result["p00099"].filter_name == "number_time"


# --- acceptance: direct current-source gender fact ---------------------------


def test_nurse_issue_with_explicit_source_fact_rejected():
    # The auditor (poisoned narrator context "The Nurse: female") flagged the
    # translation's masculine forms; the CURRENT source explicitly establishes
    # Rich as male (he/him/his) — the finding is a deterministic false positive.
    issue = _issue(
        "p00184", "invented_gender",
        note="the nurse is referred to with masculine forms but should be female",
        excerpt="медбрат",
    )
    result = _verdicts(
        [issue],
        source={"p00184": "Rich was a nurse. He had trained at the city hospital. "
                          "His hands were steady."},
        translation={"p00184": "Рич был медбратом. Он учился в городской больнице. "
                               "Его руки не дрожали."},
    )
    assert result["p00184"].verdict == REJECTED
    assert result["p00184"].filter_name == "source_gender"


def test_invented_gender_without_explicit_source_trace_stays_tier_b():
    # p00010-class: source has no explicit gender marker — needs semantics.
    issue = _issue(
        "p00010", "invented_gender",
        note="source does not specify gender of the wannabe-architect",
        excerpt="девушкой, мечтавшей стать архитектором",
    )
    result = _verdicts(
        [issue],
        source={"p00010": "A wannabe-architect from the neighborhood."},
        translation={"p00010": "Девушкой, мечтавшей стать архитектором, из соседнего квартала."},
    )
    assert result["p00010"].verdict == TIER_B


# --- acceptance: chapter entity context is NEVER Tier A (§5.3) ----------------


def test_entity_relation_issue_never_tier_a():
    # bike=motorcycle depends on chapter entity facts — always Tier B, even
    # though the finding is a changed_fact about an object.
    issue = _issue(
        "p00097", "changed_fact",
        note="bike translated as велосипед (bicycle), but CHAPTER ENTITY FACTS "
             "establish Blake's vehicle as a motorcycle",
        excerpt="Велосипед?",
    )
    result = _verdicts(
        [issue],
        source={"p00097": "He looked at the bike."},
        translation={"p00097": "Он посмотрел на велосипед."},
    )
    assert result["p00097"].verdict == TIER_B
    assert result["p00097"].filter_name == "entity_context"


def test_entity_context_argument_forces_tier_b_by_pid():
    # Even without note markers, an issue whose PID is an anchor/alias of an
    # entity-context claim is forced to Tier B (presence of verified spans
    # does not promote the relation to Tier A).
    issue = _issue(
        "p00097", "changed_fact",
        note="object identity differs",
        excerpt="велосипед",
    )
    entity_context = [
        {
            "entity": "Blake",
            "claims": [
                {
                    "kind": "alias_relation",
                    "value": "bike = motorcycle",
                    "status": "candidate",
                    "evidence_windows": [["p00007", "p00097"]],
                }
            ],
        }
    ]
    result = _verdicts(
        [issue],
        source={"p00097": "He looked at the bike."},
        translation={"p00097": "Он посмотрел на велосипед."},
        entity_context=entity_context,
    )
    assert result["p00097"].verdict == TIER_B
    assert result["p00097"].filter_name == "entity_context"


# --- structure: PID outside chunk / invalid category -> REJECT ----------------


def test_pid_outside_chunk_rejected():
    issue = _issue("p00400", "changed_fact", note="x")
    result = _verdicts(
        [issue],
        source={"p00400": "text"},
        translation={"p00400": "текст"},
        chunk_pids={"p00001", "p00002"},
    )
    assert result["p00400"].verdict == REJECTED
    assert result["p00400"].filter_name == "structure"


def test_invalid_category_rejected():
    issue = _issue("p00001", "stylistic", note="x")
    result = _verdicts(
        [issue],
        source={"p00001": "text"},
        translation={"p00001": "текст"},
    )
    assert result["p00001"].verdict == REJECTED
    assert result["p00001"].filter_name == "structure"


def test_missing_pid_from_source_rejected():
    issue = _issue("p99999", "changed_fact", note="x")
    result = _verdicts(
        [issue],
        source={"p00001": "text"},
        translation={"p00001": "текст"},
    )
    assert result["p99999"].verdict == REJECTED


def test_invalid_severity_rejected():
    issue = _issue("p00001", "changed_fact", note="x", severity="critical")
    result = _verdicts(
        [issue],
        source={"p00001": "text"},
        translation={"p00001": "текст"},
    )
    assert result["p00001"].verdict == REJECTED


# --- default: semantic verification -------------------------------------------


def test_semantic_negation_issue_defaults_to_tier_b():
    # p00093-class (negation scope) — no hard filter decides it.
    issue = _issue(
        "p00093", "negation",
        note="didn't know the story already vs уже не знал",
        excerpt="уже не знал этой истории",
    )
    result = _verdicts(
        [issue],
        source={"p00093": "someone who didn't already know the story"},
        translation={"p00093": "с кем-то, кто уже не знал этой истории"},
    )
    assert result["p00093"].verdict == TIER_B


def test_changed_fact_without_numeric_hint_defaults_to_tier_b():
    # p00013-class (printed -> вышито): no numeric/time content, no duplicate,
    # no gender category — must stay Tier B, never rejected by a coincidental
    # digit in another field.
    issue = _issue(
        "p00013", "changed_fact",
        note="source states the word was printed on the mat",
        excerpt="вышито «добро пожаловать»",
    )
    result = _verdicts(
        [issue],
        source={"p00013": "The word 'welcome' was printed on the mat."},
        translation={"p00013": "На коврике было вышито «добро пожаловать»."},
    )
    assert result["p00013"].verdict == TIER_B


def test_result_order_preserves_input_order():
    issues = [
        _issue("p00001", "changed_fact", note="x"),
        _issue("p00002", "addition", note="в гости в гости"),
    ]
    results = apply_hard_filters(
        issues,
        source={"p00001": "text", "p00002": "They went to visit."},
        translation={"p00001": "текст", "p00002": "Они пошли в гости в гости."},
    )
    assert [r.issue["id"] for r in results] == ["p00001", "p00002"]
    assert results[1].verdict == CONFIRMED


# --- RV fixes (t_6c4d53f6): entity-aware/fail-safe source gender ------------


def test_unrelated_object_pronoun_does_not_reject_nurse():
    # `him` in "The nurse spoke to him." refers to a DIFFERENT participant,
    # so the current source does not establish the nurse's gender — the
    # invented_gender finding must NOT be rejected by the source_gender
    # filter (fail-safe TIER_B, §5.1).
    issue = _issue(
        "p00190", "invented_gender",
        note="the nurse is referred to with masculine forms but the source does not establish the nurse's gender",
        excerpt="медбрат",
    )
    result = _verdicts(
        [issue],
        source={"p00190": "The nurse spoke to him."},
        translation={"p00190": "Медбрат поговорил с ним."},
    )
    assert result["p00190"].verdict == TIER_B


def test_unrelated_role_marker_does_not_reject_nurse():
    # Role/status markers (man, brother, mr, ...) are not provably the
    # target entity without entity resolution (B1.2) — "The man" is a
    # different character, so the nurse's gender stays unproven -> TIER_B.
    issue = _issue(
        "p00191", "invented_gender",
        note="the nurse is referred to with masculine forms but the source only mentions a man who is not the nurse",
        excerpt="медбрат",
    )
    result = _verdicts(
        [issue],
        source={"p00191": "The man next door saw the nurse."},
        translation={"p00191": "Соседний мужчина увидел медбрата."},
    )
    assert result["p00191"].verdict == TIER_B


def test_unrelated_possessive_pronoun_does_not_reject():
    # "His brother" — `his` is the sibling's possessor, not the nurse.
    issue = _issue(
        "p00192", "invented_gender",
        note="the nurse is referred to with masculine forms but the source does not establish the nurse's gender",
        excerpt="медбрат",
    )
    result = _verdicts(
        [issue],
        source={"p00192": "His brother was a nurse."},
        translation={"p00192": "Его брат был медбратом."},
    )
    assert result["p00192"].verdict == TIER_B


def test_explicit_source_subject_pronoun_still_rejects():
    # Subject pronoun `he` grammatically binds to the clause subject (Rich,
    # the nurse) — the deterministic case from the acceptance set must keep
    # REJECTING even with the narrowed evidence.
    issue = _issue(
        "p00184", "invented_gender",
        note="the nurse is referred to with masculine forms but should be female",
        excerpt="медбрат",
    )
    result = _verdicts(
        [issue],
        source={"p00184": "Rich was a nurse. He had trained at the city hospital. "
                          "His hands were steady."},
        translation={"p00184": "Рич был медбратом. Он учился в городской больнице. "
                               "Его руки не дрожали."},
    )
    assert result["p00184"].verdict == REJECTED
    assert result["p00184"].filter_name == "source_gender"


# --- RV fixes (t_6c4d53f6): positional numeric comparison --------------------


def test_reordered_quantities_are_not_rejected():
    # Two cats and three dogs vs Три кошки и две собаки: the same multiset
    # (2,3) but the quantities are attached to DIFFERENT facts. The
    # correspondence is not deterministically provable -> TIER_B, never
    # REJECTED (and never CONFIRMED).
    issue = _issue(
        "p00193", "changed_fact",
        note="source says 2 cats and 3 dogs but translation has 3 cats and 2 dogs",
        excerpt="Три кошки и две собаки",
    )
    result = _verdicts(
        [issue],
        source={"p00193": "Two cats and three dogs."},
        translation={"p00193": "Три кошки и две собаки."},
    )
    assert result["p00193"].verdict == TIER_B
    assert result["p00193"].filter_name == "number_time"


def test_same_order_quantities_still_rejected():
    # Same values in the SAME order = faithful translation of the numeric
    # content -> deterministic FP (REJECTED), unchanged by the fix.
    issue = _issue(
        "p00194", "changed_fact",
        note="source says 2 cats and 3 dogs",
        excerpt="Две кошки и три собаки",
    )
    result = _verdicts(
        [issue],
        source={"p00194": "Two cats and three dogs."},
        translation={"p00194": "Две кошки и три собаки."},
    )
    assert result["p00194"].verdict == REJECTED
    assert result["p00194"].filter_name == "number_time"


def test_genuine_numeric_mismatch_still_confirmed():
    # 12 vs 10 is a real difference — CONFIRMED, unchanged by the fix.
    issue = _issue(
        "p00195", "changed_fact",
        note="source says 12 chairs but translation says 10",
        excerpt="десять стульев",
    )
    result = _verdicts(
        [issue],
        source={"p00195": "There were twelve chairs in the hall."},
        translation={"p00195": "В зале стояло десять стульев."},
    )
    assert result["p00195"].verdict == CONFIRMED
    assert result["p00195"].filter_name == "number_time"


# --- RV fixes (t_6c4d53f6): explicit name/string/object contract ------------


def test_explicit_quoted_string_preserved_rejected():
    # An explicitly quoted string in the CURRENT source pair preserved
    # verbatim in the translation refutes the changed-string claim
    # deterministically (REJECTED).
    issue = _issue(
        "p00196", "changed_fact",
        note='the sign text "STOP" was supposedly changed',
        excerpt='"STOP"',
    )
    result = _verdicts(
        [issue],
        source={"p00196": 'The sign read "STOP".'},
        translation={"p00196": 'На табличке было написано "STOP".'},
    )
    assert result["p00196"].verdict == REJECTED
    assert result["p00196"].filter_name == "explicit_string"


def test_translated_quoted_string_stays_tier_b():
    # The quoted string is NOT preserved verbatim (translated) — that is a
    # semantic edge, never CONFIRMED by the hard filter -> TIER_B.
    issue = _issue(
        "p00197", "changed_fact",
        note='the sign text "STOP" was changed',
        excerpt='"STOP"',
    )
    result = _verdicts(
        [issue],
        source={"p00197": 'The sign read "STOP".'},
        translation={"p00197": "На табличке было написано «СТОП»."},
    )
    assert result["p00197"].verdict == TIER_B


def test_unquoted_name_change_stays_tier_b():
    # Unquoted proper names are OUT of the deterministic contract (they need
    # transliteration/entity knowledge) -> TIER_B, never guessed.
    issue = _issue(
        "p00198", "changed_fact",
        note="the character name was changed",
        excerpt="Рич",
    )
    result = _verdicts(
        [issue],
        source={"p00198": "Rich walked in."},
        translation={"p00198": "Рич вошёл."},
    )
    assert result["p00198"].verdict == TIER_B


# --- RV2 fixes (t_e9815310): issue-scoped explicit strings --------------------


def test_prose_apostrophe_does_not_trigger_string_filter():
    # Finding 1 reproduction: the note's only quote is a prose apostrophe
    # (character's) — that is NOT an explicit quoted string. The unrelated
    # source string "STOP" is preserved in the translation, but the finding
    # does not cite it, so the explicit-string filter must not fire and the
    # issue fails safe to TIER_B (previously REJECTED/explicit_string).
    issue = _issue(
        "p00199", "changed_fact",
        note="character's name changed",
        excerpt="имя персонажа",
    )
    result = _verdicts(
        [issue],
        source={"p00199": 'The sign read "STOP"; Rich entered.'},
        translation={"p00199": 'На табличке было написано "STOP"; Рич вошёл.'},
    )
    assert result["p00199"].verdict == TIER_B


def test_unrelated_quoted_content_does_not_reject():
    # Finding 1: the finding quotes "GO", but the source's only quoted string
    # is the unrelated preserved "STOP". There is no provable match between
    # the issue's quoted content and a current-source quoted string -> the
    # issue fails safe to TIER_B, never REJECTED (previously REJECTED because
    # "STOP" happened to be preserved).
    issue = _issue(
        "p00200", "changed_fact",
        note='the sign text "GO" was supposedly changed',
        excerpt='"GO"',
    )
    result = _verdicts(
        [issue],
        source={"p00200": 'The sign read "STOP".'},
        translation={"p00200": 'На табличке было написано "STOP".'},
    )
    assert result["p00200"].verdict == TIER_B


def test_explicit_string_reject_requires_issue_to_cite_source_string():
    # The reject path still works when the issue DOES cite the preserved
    # source string, and stays TIER_B when the cited string is translated
    # (matched -> not preserved verbatim -> semantic edge, never CONFIRMED).
    issue = _issue(
        "p00202", "changed_fact",
        note='the sign text "STOP" was supposedly changed',
        excerpt='"STOP"',
    )
    result = _verdicts(
        [issue],
        source={"p00202": 'The sign read "STOP".'},
        translation={"p00202": 'На табличке было написано "STOP".'},
    )
    assert result["p00202"].verdict == REJECTED
    assert result["p00202"].filter_name == "explicit_string"

    translated = _issue(
        "p00203", "changed_fact",
        note='the sign text "STOP" was supposedly changed',
        excerpt='"STOP"',
    )
    result = _verdicts(
        [translated],
        source={"p00203": 'The sign read "STOP".'},
        translation={"p00203": "На табличке было написано «СТОП»."},
    )
    assert result["p00203"].verdict == TIER_B


# --- RV3 fixes (t_2829fb4c): fail-safe complete-set explicit strings ---------


def test_mixed_unmatched_issue_quotes_stay_tier_b():
    # HIGH finding 1: the issue cites TWO quoted strings, but only one
    # ("STOP") exists in the current source. The cited set is not complete
    # in the source ("GO" is unmatched), so even though "STOP" is preserved
    # in the translation the claim is not fully provable -> TIER_B
    # (previously REJECTED/explicit_string on the subset match).
    issue = _issue(
        "p00208", "changed_fact",
        note='the sign texts "STOP" and "GO" changed',
        excerpt='"STOP" and "GO"',
    )
    result = _verdicts(
        [issue],
        source={"p00208": 'The sign read "STOP".'},
        translation={"p00208": 'На табличке было написано "STOP".'},
    )
    assert result["p00208"].verdict == TIER_B
    assert result["p00208"].filter_name == "semantic"


def test_mixed_translated_issue_quotes_stay_tier_b():
    # HIGH finding 1: the issue cites "STOP" and "GO", both exist in the
    # source, but the translation preserves only "STOP" ("GO" is translated).
    # The cited set is not preserved verbatim -> TIER_B (previously
    # REJECTED on the preserved subset).
    issue = _issue(
        "p00209", "changed_fact",
        note='the sign texts "STOP" and "GO" changed',
        excerpt='"STOP" and "GO"',
    )
    result = _verdicts(
        [issue],
        source={"p00209": 'The signs read "STOP" and "GO".'},
        translation={"p00209": 'На табличках было написано "STOP" и «ГО».'},
    )
    assert result["p00209"].verdict == TIER_B
    assert result["p00209"].filter_name == "semantic"


def test_unrelated_source_quote_does_not_block_valid_rejection():
    # HIGH finding 1 acceptance: the source may carry OTHER quoted strings
    # the issue does not cite; a valid complete single-string rejection must
    # still fire (the cited set is complete in source and preserved).
    issue = _issue(
        "p00210", "changed_fact",
        note='the sign text "STOP" was supposedly changed',
        excerpt='"STOP"',
    )
    result = _verdicts(
        [issue],
        source={"p00210": 'The sign read "STOP"; the door said "EXIT".'},
        translation={"p00210": 'На табличке было написано "STOP"; на двери — "EXIT".'},
    )
    assert result["p00210"].verdict == REJECTED
    assert result["p00210"].filter_name == "explicit_string"


def test_single_quoted_source_string_after_prose_apostrophe_rejected():
    # MEDIUM finding 2: a prose apostrophe (character's) in the note must
    # not be paired with the later single-quoted 'STOP'. The note cites
    # 'STOP', the current source quotes 'STOP' and the translation preserves
    # it -> REJECTED/explicit_string (previously TIER_B/semantic because the
    # apostrophe swallowed the quote pair).
    issue = _issue(
        "p00211", "changed_fact",
        note="character's name changed; source text 'STOP' was changed",
        excerpt="'STOP'",
    )
    result = _verdicts(
        [issue],
        source={"p00211": "The sign read 'STOP'."},
        translation={"p00211": "На табличке было написано 'STOP'."},
    )
    assert result["p00211"].verdict == REJECTED
    assert result["p00211"].filter_name == "explicit_string"


# --- RV3.1 fixes (t_bc65b9c7): unmatched/ambiguous delimiter fail-safe -------


def test_unmatched_single_quote_in_note_fails_safe_to_tier_b():
    # HIGH finding: the note cites a valid preserved 'STOP' pair AND carries
    # a stray unmatched single quote ("malformed quote '"). The extracted
    # set may be incomplete, so explicit_string must NOT reject — the
    # representative claim fails safe to TIER_B (previously
    # REJECTED/explicit_string on the valid subset).
    issue = _issue(
        "p00212", "changed_fact",
        note="the sign text 'STOP' changed; malformed quote '",
        excerpt="'STOP'",
    )
    result = _verdicts(
        [issue],
        source={"p00212": "The sign read 'STOP'."},
        translation={"p00212": "На табличке было написано 'STOP'."},
    )
    assert result["p00212"].verdict == TIER_B
    assert result["p00212"].filter_name == "semantic"


def test_unmatched_double_quote_in_note_fails_safe_to_tier_b():
    # The double-quote equivalent of the HIGH finding: a valid preserved
    # "STOP" pair plus a stray unmatched double quote in the note. Same
    # fail-safe contract -> TIER_B, never REJECTED/explicit_string.
    issue = _issue(
        "p00213", "changed_fact",
        note='the sign text "STOP" changed; malformed quote "',
        excerpt='"STOP"',
    )
    result = _verdicts(
        [issue],
        source={"p00213": 'The sign read "STOP".'},
        translation={"p00213": 'На табличке было написано "STOP".'},
    )
    assert result["p00213"].verdict == TIER_B
    assert result["p00213"].filter_name == "semantic"


def test_valid_single_quote_pair_still_rejects_without_stray_quote():
    # Guard: the fail-safe must not weaken the valid rejection path — a
    # clean single-quoted citation of a preserved current-source string is
    # still REJECTED/explicit_string (no stray delimiter in the note).
    issue = _issue(
        "p00214", "changed_fact",
        note="the sign text 'STOP' was supposedly changed",
        excerpt="'STOP'",
    )
    result = _verdicts(
        [issue],
        source={"p00214": "The sign read 'STOP'."},
        translation={"p00214": "На табличке было написано 'STOP'."},
    )
    assert result["p00214"].verdict == REJECTED
    assert result["p00214"].filter_name == "explicit_string"


# --- RV2 fixes (t_e9815310): B1.2 evidence dict PIDs -------------------------


def test_entity_context_evidence_dict_pid_forces_tier_b():
    # Finding 2: the actual B1.2 schema carries claim evidence as
    # [{"pid": ..., "span": ...}] dicts. A PID that appears ONLY in evidence
    # (no evidence_windows) must still force entity_context/TIER_B — the
    # dict must not be stringified into a fake PID (previously the issue
    # slipped through to Tier A).
    issue = _issue(
        "p00204", "changed_fact",
        note="object identity differs",
        excerpt="велосипед",
    )
    entity_context = [
        {
            "entity": "Blake",
            "claims": [
                {
                    "kind": "object_identity",
                    "value": "bike = motorcycle",
                    "status": "candidate",
                    "evidence": [{"pid": "p00204", "span": "He looked at the bike."}],
                }
            ],
        }
    ]
    result = _verdicts(
        [issue],
        source={"p00204": "He looked at the bike."},
        translation={"p00204": "Он посмотрел на велосипед."},
        entity_context=entity_context,
    )
    assert result["p00204"].verdict == TIER_B
    assert result["p00204"].filter_name == "entity_context"


def test_entity_context_evidence_dict_and_windows_combined():
    # Both actual B1.2 evidence forms in one claim: dict evidence PIDs and
    # evidence_windows PID ranges force TIER_B together; a span-only dict
    # (no pid) contributes nothing.
    issues = [
        _issue("p00205", "changed_fact", note="alias relation", excerpt="велосипед"),
        _issue("p00206", "changed_fact", note="alias relation", excerpt="велосипед"),
        _issue("p00207", "changed_fact", note="alias relation", excerpt="велосипед"),
    ]
    entity_context = [
        {
            "entity": "Blake",
            "claims": [
                {
                    "kind": "alias_relation",
                    "value": "bike = motorcycle",
                    "status": "candidate",
                    "evidence": [
                        {"pid": "p00205", "span": "He looked at the bike."},
                        {"span": "no pid here"},
                    ],
                    "evidence_windows": [["p00206", "p00207"]],
                }
            ],
        }
    ]
    source = {p: "He looked at the bike." for p in ("p00205", "p00206", "p00207")}
    translation = {p: "Он посмотрел на велосипед." for p in ("p00205", "p00206", "p00207")}
    result = _verdicts(issues, source=source, translation=translation, entity_context=entity_context)
    for pid in ("p00205", "p00206", "p00207"):
        assert result[pid].verdict == TIER_B
        assert result[pid].filter_name == "entity_context"
