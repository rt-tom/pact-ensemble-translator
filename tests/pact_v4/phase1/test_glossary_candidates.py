"""Unit tests for the B9-I1 glossary-candidate module.

Covers the three contract surfaces of
``pact_v4.phase1.glossary_candidates``:

  * generator — latin-only scanning, frequency thresholds, exclusions
    (glossary keys / book-memory characters and variants / B5 allowlist),
    proper_name vs term classification, chunk_ids, determinism;
  * consensus alignment — proper_name consensus, proper_name conflict,
    term frequency contrast, inflection merging, common-noun filter,
    no-model-call guarantee;
  * ledger — cross-chapter accumulation, merge/conflict targets,
    crash-safe partial trailing line, idempotent re-run.

All fixture text is invented placeholder content, not book text.
"""
from __future__ import annotations

import json

import pytest

from pact_v4.phase1.glossary_candidates import (
    DEFAULT_CONSENSUS_RATIO,
    GlossaryCandidateLedger,
    align_candidates,
    candidate_key,
    generate_candidates,
)


# ---------------------------------------------------------------------------
# Generator
# ---------------------------------------------------------------------------

def test_generator_proper_name_and_term_classification():
    text = (
        "The lawyer Beasley handled the case. Beasley knew the law. "
        "The contract was binding. The contract was signed. "
        "A second contract followed. The contract expired."
    )
    cands = generate_candidates(text)
    by_source = {c["source"].casefold(): c for c in cands}
    assert by_source["beasley"]["kind"] == "proper_name"
    assert by_source["beasley"]["occurrences"] == 2
    assert by_source["contract"]["kind"] == "term"
    assert by_source["contract"]["occurrences"] == 4
    assert by_source["contract"]["context"]
    assert "contract" in by_source["contract"]["context"].casefold()


def test_generator_sentence_start_not_proper_name():
    # "Master" only ever appears at sentence starts → not a proper_name,
    # and 2 occurrences < 3 → not a term either.
    text = "Master arrived. Master left."
    cands = generate_candidates(text)
    assert not [c for c in cands if c["source"].casefold() == "master"]


def test_generator_lowercase_appearance_excludes_proper_name():
    # "Bell" appears capitalized mid-sentence but also lowercase ("bell"),
    # so it is a common noun, not a proper name; still a term (3, len 4).
    text = "The Bell rang out. I heard a bell far off. Another bell answered."
    cands = generate_candidates(text)
    bell = [c for c in cands if c["source"].casefold() == "bell"]
    assert len(bell) == 1
    assert bell[0]["kind"] == "term"


def test_generator_term_frequency_and_length_thresholds():
    text = ("cob cob cob "  # 3 occurrences, len 3 → below min length
            "fen fen "      # 2 occurrences, len 3 → below frequency
            "quag quag quag quag quag")  # 5 occurrences, len 4 → term
    cands = generate_candidates(text)
    by_source = {c["source"].casefold(): c for c in cands}
    assert "cob" not in by_source
    assert "fen" not in by_source
    assert by_source["quag"]["kind"] == "term"
    assert by_source["quag"]["occurrences"] == 5


def test_generator_glossary_exclusion():
    text = "Evan came over. Evan stayed. Evan nodded."
    cands = generate_candidates(text, glossary={"Evan": "Эван"})
    assert not [c for c in cands if c["source"].casefold() == "evan"]


def test_generator_book_memory_characters_and_variants_exclusion():
    text = "Blake walked in. Blake sat down. Blake left."
    book_memory = {
        "characters": {
            "Blake Thorburn": {"variants": {"Blake": 1, "Блэйк": 1}},
        }
    }
    cands = generate_candidates(text, book_memory=book_memory)
    assert not [c for c in cands if c["source"].casefold() == "blake"]


def test_generator_allowlist_exclusion():
    text = "Corvidae handled the papers. Corvidae signed. Corvidae left."
    cands = generate_candidates(text, allowlist=("corvidae",))
    assert not [c for c in cands if c["source"].casefold() == "corvidae"]


def test_generator_possessive_normalization():
    # "Irene's" counts toward "Irene" (possessive suffix stripped).
    text = ("Irene walked in. I greeted Irene warmly. "
            "Irene's coat hung by the door.")
    cands = generate_candidates(text)
    irene = [c for c in cands if c["source"].casefold() == "irene"]
    assert len(irene) == 1
    assert irene[0]["kind"] == "proper_name"
    assert irene[0]["occurrences"] == 3


def test_generator_html_input():
    html = (
        "<html><body><h1>Title</h1>"
        "<p>Beasley handled the case. Beasley knew the law.</p>"
        "<script>var x = 'Beasley';</script>"
        "</body></html>"
    )
    cands = generate_candidates(html)
    assert [c for c in cands if c["source"].casefold() == "beasley"]
    # script body stripped: "Beasley" appears twice in the text, not thrice.
    beasley = [c for c in cands if c["source"].casefold() == "beasley"][0]
    assert beasley["occurrences"] == 2


def test_generator_chunk_ids_from_pid_map():
    source_by_pid = {
        "p00001": "Corvidae handled the case. Corvidae signed the papers.",
        "p00002": "Corvidae left the office.",
    }
    pid_to_chunk = {"p00001": "chunk0001", "p00002": "chunk0002"}
    cands = generate_candidates(source_by_pid, pid_to_chunk=pid_to_chunk)
    corvidae = [c for c in cands if c["source"].casefold() == "corvidae"][0]
    assert corvidae["chunk_ids"] == ["chunk0001", "chunk0002"]
    assert corvidae["occurrences"] == 3


def test_generator_deterministic_order():
    text = (
        "Beasley handled the case. Beasley knew the law. "
        "The contract was binding. The contract was signed. "
        "A second contract followed. The contract expired."
    )
    first = generate_candidates(text)
    second = generate_candidates(text)
    assert first == second
    sources = [c["source"] for c in first]
    assert sources == sorted(sources, key=str.casefold)


def test_generator_empty_and_short_input():
    assert generate_candidates("") == []
    assert generate_candidates("a b c") == []


def test_generator_pid_duplicates_merged():
    source = [("p00001", "Corvidae arrived."), ("p00001", "Corvidae left."),
              ("p00002", "Corvidae stayed.")]
    cands = generate_candidates(source)
    corvidae = [c for c in cands if c["source"].casefold() == "corvidae"][0]
    assert corvidae["occurrences"] == 3


# ---------------------------------------------------------------------------
# Consensus alignment
# ---------------------------------------------------------------------------

_SOURCE_BY_PID = {
    "p00001": "Blake walked to the house. Blake knew the way.",
    "p00002": "The house loomed over Blake.",
    "p00003": "Blake paused at the gate.",
    "p00004": "She waited for Blake inside.",
    "p00005": "The gate was heavy.",
}

_BLAKE_CANDIDATE = {
    "source": "Blake", "kind": "proper_name", "occurrences": 4,
    "chunk_ids": [], "context": "Blake walked to the house.",
}


def test_alignment_proper_name_consensus():
    translations = {
        "p00001": "Блэйк подошёл к дому. Блэйк знал дорогу.",
        "p00002": "Дом нависал над Блэйком.",
        "p00003": "Блэйк замер у ворот.",
        "p00004": "Она ждала Блэйка внутри.",
        "p00005": "Ворота были тяжёлыми.",
    }
    aligned = align_candidates([_BLAKE_CANDIDATE], _SOURCE_BY_PID, translations)
    record = aligned[0]
    # Inflections (Блэйк/Блэйком/Блэйка) merge into one variant; the common
    # noun "Дом" (capitalized only by sentence position) is filtered out.
    assert record["target"] == "Блэйк"
    assert record["consensus_share"] == 1.0
    assert record["conflicts"] == []
    assert record["variants"]["Блэйк"] == 4


def test_alignment_proper_name_chapter_wide_common_noun_filter():
    # P2: "Дом" is capitalized only by sentence position in the
    # candidate-matching pids, but "дом" occurs lowercase in a NON-matching
    # pid of the same chapter. Chapter-wide evidence must stop it from
    # becoming a proper-name target (with matching-only evidence it would
    # reach share 1.0 and wrongly become the target).
    source_by_pid = {
        "p00001": "Blake walked to the house.",
        "p00002": "Blake paused at the gate.",
        "p00003": "The house loomed over Blake.",
        "p00004": "Blake stayed outside.",
        "p00005": "The gate was heavy.",
    }
    translations = {
        "p00001": "Дом стоял у дороги.",
        "p00002": "Дом ждал его у ворот.",
        "p00003": "Дом нависал над ним.",
        "p00004": "Дом стоял в глубине.",
        "p00005": "Тяжёлый дом стоял у ворот.",
    }
    aligned = align_candidates([_BLAKE_CANDIDATE], source_by_pid, translations)
    record = aligned[0]
    assert record["target"] is None
    assert record["variants"] == {}
    assert record["consensus_share"] == 0.0


def test_alignment_proper_name_conflict_no_target():
    translations = {
        "p00001": "Блэйк подошёл к дому.",
        "p00002": "Дом нависал над Блейком.",
        "p00003": "Блэйк замер у ворот.",
        "p00004": "Она ждала Блейка внутри.",
        "p00005": "Ворота были тяжёлыми.",
    }
    aligned = align_candidates([_BLAKE_CANDIDATE], _SOURCE_BY_PID, translations)
    record = aligned[0]
    assert record["target"] is None
    assert record["consensus_share"] == 0.5
    assert set(record["conflicts"]) == {"Блэйк", "Блейком"}


def test_alignment_no_matching_pids():
    translations = {
        "p00001": "Совсем другой текст.",
        "p00002": "Ещё один другой текст.",
    }
    cand = dict(_BLAKE_CANDIDATE, source="NobodyElse")
    aligned = align_candidates([cand], _SOURCE_BY_PID, translations)
    record = aligned[0]
    assert record["matching_pid_count"] == 0
    assert record["target"] is None
    assert record["variants"] == {}
    assert record["consensus_share"] == 0.0


def test_alignment_term_consensus_and_contrast():
    source_by_pid = {
        **{f"p{i:05d}": "The pact bound them all together." for i in range(1, 4)},
        **{f"p{i:05d}": "The others watched from afar." for i in range(4, 9)},
    }
    translations = {
        **{f"p{i:05d}": "Пакт связывал их всех вместе." for i in range(1, 4)},
        **{f"p{i:05d}": "Остальные наблюдали издалека." for i in range(4, 9)},
    }
    cand = {"source": "pact", "kind": "term", "occurrences": 3,
            "chunk_ids": [], "context": "The pact bound them."}
    aligned = align_candidates([cand], source_by_pid, translations)
    record = aligned[0]
    assert record["target"] == "пакт"
    assert record["consensus_share"] == 1.0
    assert record["conflicts"] == []
    # Only contrast words pass the filter — "связывал"/"всех"/"вместе" appear
    # only in term-pids, "пакт" in all three.
    assert set(record["variants"]) == {"пакт", "связывал", "всех", "вместе"}


def test_alignment_term_contrast_rejects_common_word():
    # "наблюдали" appears in term-pids too, so its contrast is below the
    # threshold and it must not become a variant.
    source_by_pid = {
        **{f"p{i:05d}": "The pact bound them all together." for i in range(1, 4)},
        **{f"p{i:05d}": "The others watched from afar." for i in range(4, 9)},
    }
    translations = {
        **{f"p{i:05d}": "Пакт связывал их, наблюдали все." for i in range(1, 4)},
        **{f"p{i:05d}": "Остальные наблюдали издалека." for i in range(4, 9)},
    }
    cand = {"source": "pact", "kind": "term", "occurrences": 3,
            "chunk_ids": [], "context": "The pact bound them."}
    aligned = align_candidates([cand], source_by_pid, translations)
    record = aligned[0]
    assert "наблюдали" not in record["variants"]
    assert record["target"] == "пакт"


def test_alignment_co_occurring_terms_cannot_share_unrelated_target():
    # B9-F2 (review PR #128): the frequency-contrast heuristic cannot tell
    # "the candidate's translation" from "a word that merely co-occurs with
    # the candidate in the same pids" — when the term-pids are identical for
    # several candidates and there is no contrasting out-group, every
    # co-occurring Russian word qualifies (contrast is vacuous) and the
    # first-seen tie-break hands ALL of pact/bound/together the target
    # "пакт". That would auto-promote unrelated source terms as the same
    # unrelated target and corrupt the glossary. The conservative rule must
    # strip the shared target from every competing candidate.
    source_by_pid = {
        **{f"p{i:05d}": "The pact bound them all together." for i in range(1, 4)},
        **{f"p{i:05d}": "The others watched from afar." for i in range(4, 9)},
    }
    translations = {
        **{f"p{i:05d}": "Пакт связывал их всех вместе." for i in range(1, 4)},
        **{f"p{i:05d}": "Остальные наблюдали издалека." for i in range(4, 9)},
    }
    cands = [
        {"source": source, "kind": "term", "occurrences": 3,
         "chunk_ids": [], "context": "The pact bound them."}
        for source in ("pact", "bound", "together")
    ]
    aligned = align_candidates(cands, source_by_pid, translations)
    by_source = {a["source"]: a for a in aligned}
    # "пакт" is co-occurrence evidence for all three and unambiguous for
    # none: no candidate keeps it as a target, and it stays visible in the
    # conflicts for the human reviewer.
    for name in ("pact", "bound", "together"):
        record = by_source[name]
        assert record["target"] is None
        assert record["consensus_share"] == 0.0
        assert "пакт" in record["conflicts"]


def test_alignment_consensus_ratio_parameter():
    translations = {
        "p00001": "Блэйк подошёл к дому.",
        "p00002": "Дом нависал над Блэйком.",
        "p00003": "Блэйк замер у ворот.",
        "p00004": "Она ждала Блэйка внутри.",
        "p00005": "Ворота были тяжёлыми.",
    }
    # With a looser ratio 0.5 the same data reaches consensus.
    aligned = align_candidates(
        [_BLAKE_CANDIDATE], _SOURCE_BY_PID, translations,
        consensus_ratio=0.5,
    )
    assert aligned[0]["target"] == "Блэйк"
    assert aligned[0]["conflicts"] == []


def test_alignment_no_model_calls_uses_only_arguments():
    # The function must be a pure function of its arguments: calling it with
    # the same inputs twice yields identical results.
    translations = {
        "p00001": "Блэйк подошёл к дому.",
        "p00002": "Дом нависал над Блэйком.",
        "p00003": "Блэйк замер у ворот.",
        "p00004": "Она ждала Блэйка внутри.",
        "p00005": "Ворота были тяжёлыми.",
    }
    first = align_candidates([_BLAKE_CANDIDATE], _SOURCE_BY_PID, translations)
    second = align_candidates([_BLAKE_CANDIDATE], _SOURCE_BY_PID, translations)
    assert first == second


# ---------------------------------------------------------------------------
# B9-fix (t_800fedaf): false positives from the run_005 dry-run
# ---------------------------------------------------------------------------

def test_alignment_proper_name_target_established_value_rejected():
    # B9-fix HIGH 1: "Master" is a capitalized title used with Blake's name
    # ("Master Blake"); in the matching translations the capitalized word is
    # Блэйк — the ESTABLISHED glossary value of the key "Blake". Aligning
    # Master -> Блэйк would auto-promote a wrong pair (Master is not Blake).
    # The guard must drop the target (conflict) so the pair never promotes.
    source_by_pid = {
        "p00001": "Are you accusing me of being a liar, Master Blake?",
        "p00002": "She even said 'Master Blake'.",
        "p00003": "I am a lawyer, Master Blake.",
    }
    translations = {
        "p00001": "Вы обвиняете меня во лжи, мастер Блэйк?",
        "p00002": "Она даже сказала «мастер Блэйк».",
        "p00003": "Я юрист, мастер Блэйк.",
    }
    cand = {"source": "Master", "kind": "proper_name", "occurrences": 3,
            "chunk_ids": [], "context": "Master Blake"}
    glossary = {"Blake": "Блэйк", "Blake Thorburn": "Блэйк Торбёрн"}
    record = align_candidates([cand], source_by_pid, translations,
                              glossary=glossary)[0]
    assert record["target"] is None
    assert "Блэйк" in record["conflicts"]
    assert record["consensus_share"] == 0.0


def test_alignment_term_collocation_verb_loses_to_true_translation():
    # B9-fix HIGH 2: "получить" (to get) co-occurs with "advantage" in the
    # phrase "get an advantage" and appears in ALL matching pids, so raw
    # pid-presence made it the dominant variant (3/3) over the true
    # translation "преимущество" (2/3). Candidate-specificity (how few
    # NON-matching pids contain the word) must demote the collocation verb
    # and keep the true translation; with преимущество at 2/3 < consensus
    # the candidate has no single target — it must NOT promote.
    source_by_pid = {
        **{f"p{i:05d}": "Trying to get an advantage." for i in range(1, 4)},
        **{f"p{i:05d}": "They wanted to get the money." for i in range(4, 9)},
    }
    translations = {
        "p00001": "Пытаясь получить преимущество.",
        "p00002": "Он хотел получить преимущество.",
        "p00003": "Он хотел получить шанс.",
        **{f"p{i:05d}": "Они хотели получить деньги." for i in range(4, 9)},
    }
    cand = {"source": "advantage", "kind": "term", "occurrences": 3,
            "chunk_ids": [], "context": "Trying to get an advantage."}
    record = align_candidates([cand], source_by_pid, translations)[0]
    assert record["target"] is None
    assert "получить" not in record["variants"] or record["target"] != "получить"
    # преимущество is the true translation and stays candidate-specific.
    assert record["variants"].get("преимущество") == 2


def test_alignment_term_verb_collocation_loses_to_noun():
    # B9-fix HIGH 2: "чувствовал" (felt) co-occurs with "anger" ("I could
    # feel the anger" -> "чувствовал злость") and outranks the true
    # translation "злость" under raw pid-presence (tie 2/2, first-seen
    # wins). Candidate-specificity must prefer "злость" (only in matching
    # pids) over "чувствовал" (also in non-matching pids).
    source_by_pid = {
        **{f"p{i:05d}": "I could feel the anger stirring." for i in range(1, 3)},
        **{f"p{i:05d}": "He felt the cold air." for i in range(3, 7)},
    }
    translations = {
        **{f"p{i:05d}": "Я чувствовал, как злость закипает." for i in range(1, 3)},
        **{f"p{i:05d}": "Он чувствовал холодный воздух." for i in range(3, 7)},
    }
    cand = {"source": "anger", "kind": "term", "occurrences": 3,
            "chunk_ids": [], "context": "I could feel the anger."}
    record = align_candidates([cand], source_by_pid, translations)[0]
    assert record["target"] == "злость"
    assert "чувствовал" not in record["conflicts"]


def test_alignment_term_side_collocation_loses_to_adjective():
    # B9-fix HIGH 2: "стороны" (sides, from "on one side") co-occurs with
    # "blonde" in every matching pid and is a common word across the
    # chapter; the true translation "блондинка" is candidate-specific.
    # Raw pid-presence handed the target to "стороны" (tie 3/3, first-seen);
    # candidate-specificity must prefer "блондинка".
    source_by_pid = {
        **{f"p{i:05d}": "On one side, all the women were blonde." for i in range(1, 4)},
        **{f"p{i:05d}": "On the other side stood the men." for i in range(4, 10)},
        **{f"p{i:05d}": "The men were silent." for i in range(10, 14)},
    }
    translations = {
        "p00001": "С одной стороны была блондинка.",
        "p00002": "С другой стороны стояла блондинка.",
        "p00003": "С третьей стороны появилась блондинка.",
        **{f"p{i:05d}": "С другой стороны стояли мужчины." for i in range(4, 8)},
        **{f"p{i:05d}": "Мужчины молчали." for i in range(8, 14)},
    }
    cand = {"source": "blonde", "kind": "term", "occurrences": 3,
            "chunk_ids": [], "context": "all the women were blonde"}
    record = align_candidates([cand], source_by_pid, translations)[0]
    assert record["target"] == "блондинка"
    assert "стороны" not in record["conflicts"] or record["target"] is not None


def test_alignment_proper_name_unaffected_by_unrelated_glossary_values():
    # B9-fix positive case: a genuine proper name (Ivy) whose translation is
    # NOT an established glossary value keeps its target even when the
    # glossary is supplied — the established-value guard must only fire on
    # real collisions.
    source_by_pid = {
        **{f"p{i:05d}": "Ivy said hello." for i in range(1, 4)},
        **{f"p{i:05d}": "The others watched." for i in range(4, 6)},
    }
    translations = {
        **{f"p{i:05d}": "Айви поздоровалась." for i in range(1, 4)},
        **{f"p{i:05d}": "Остальные смотрели." for i in range(4, 6)},
    }
    cand = {"source": "Ivy", "kind": "proper_name", "occurrences": 3,
            "chunk_ids": [], "context": "Ivy said hello."}
    glossary = {"Blake": "Блэйк", "Blake Thorburn": "Блэйк Торбёрн"}
    record = align_candidates([cand], source_by_pid, translations,
                              glossary=glossary)[0]
    assert record["target"] == "Айви"
    assert record["consensus_share"] == 1.0
    assert record["conflicts"] == []


def test_alignment_term_specific_translation_still_promotes():
    # B9-fix positive case: a term whose true translation is genuinely
    # candidate-specific (ботинки, out=0) still reaches consensus and
    # promotes even with a co-occurring common word (дверь appears widely
    # across non-matching pids) — the specificity ranking must not starve
    # correct candidates.
    source_by_pid = {
        **{f"p{i:05d}": "He wore heavy boots." for i in range(1, 4)},
        **{f"p{i:05d}": "The door was heavy too." for i in range(4, 9)},
        **{f"p{i:05d}": "She wore a scarf." for i in range(9, 13)},
    }
    translations = {
        "p00001": "Он носил тяжёлые ботинки.",
        "p00002": "Он носил свои ботинки.",
        "p00003": "Он носил новые ботинки.",
        **{f"p{i:05d}": "Дверь тоже была тяжёлой." for i in range(4, 9)},
        **{f"p{i:05d}": "Она носила шарф." for i in range(9, 13)},
    }
    cand = {"source": "boots", "kind": "term", "occurrences": 3,
            "chunk_ids": [], "context": "He wore heavy boots."}
    record = align_candidates([cand], source_by_pid, translations)[0]
    assert record["target"] == "ботинки"
    assert record["consensus_share"] == 1.0
    assert record["conflicts"] == []


# ---------------------------------------------------------------------------
# Ledger
# ---------------------------------------------------------------------------

def _aligned(translations, source=_SOURCE_BY_PID):
    return align_candidates([_BLAKE_CANDIDATE], source, translations)


_CONSENSUS_TRANSLATIONS = {
    "p00001": "Блэйк подошёл к дому. Блэйк знал дорогу.",
    "p00002": "Дом нависал над Блэйком.",
    "p00003": "Блэйк замер у ворот.",
    "p00004": "Она ждала Блэйка внутри.",
    "p00005": "Ворота были тяжёлыми.",
}

_CONFLICT_TRANSLATIONS = {
    "p00001": "Блэйк подошёл к дому.",
    "p00002": "Дом нависал над Блейком.",
    "p00003": "Блэйк замер у ворот.",
    "p00004": "Она ждала Блейка внутри.",
    "p00005": "Ворота были тяжёлыми.",
}


def test_ledger_cross_chapter_accumulation(tmp_path):
    ledger = GlossaryCandidateLedger(str(tmp_path / "glossary_candidates.json"))
    ledger.append_chapter("ch0001", _aligned(_CONSENSUS_TRANSLATIONS))
    ledger.append_chapter("ch0002", _aligned(_CONSENSUS_TRANSLATIONS))
    records = ledger.load()
    key = candidate_key("Blake", "proper_name")
    record = records[key]
    assert record["source"] == "Blake"
    assert record["kind"] == "proper_name"
    assert record["total_occurrences"] == 8
    assert record["variants"]["Блэйк"] == 8
    assert [c["chapter_id"] for c in record["chapters"]] == ["ch0001", "ch0002"]
    assert record["first_context"] == _BLAKE_CANDIDATE["context"]


def test_ledger_target_conflict_across_chapters(tmp_path):
    ledger = GlossaryCandidateLedger(str(tmp_path / "glossary_candidates.json"))
    ledger.append_chapter("ch0001", _aligned(_CONSENSUS_TRANSLATIONS))
    ledger.append_chapter("ch0002", _aligned(_CONFLICT_TRANSLATIONS))
    record = ledger.load()[candidate_key("Blake", "proper_name")]
    # Chapter 2 reached no consensus → merged target stays the single
    # distinct non-None chapter target, and the conflict variant is recorded.
    assert record["target"] == "Блэйк"
    assert "Блейком" in record["conflicts"]


def _targeted_obs(target, conflicts=()):
    """An aligned-candidate-shaped observation with an explicit target."""
    return [{
        "source": "Blake", "kind": "proper_name", "occurrences": 2,
        "chunk_ids": [], "context": "Blake walked.",
        "variants": {target: 2} if target else {},
        "target": target,
        "conflicts": list(conflicts),
    }]


def test_ledger_cross_chapter_target_disagreement_is_irreversible(tmp_path):
    # P1: chapter targets Альфа, Бета, Альфа — the third chapter must NOT
    # resurrect "Альфа": the earlier disagreement is irreversible, and both
    # distinct chapter targets (plus any per-chapter conflicts) land in
    # conflicts.
    ledger = GlossaryCandidateLedger(str(tmp_path / "glossary_candidates.json"))
    ledger.append_chapter("ch0001", _targeted_obs("Альфа"))
    ledger.append_chapter("ch0002", _targeted_obs("Бета", conflicts=("Вариант2",)))
    ledger.append_chapter("ch0003", _targeted_obs("Альфа"))
    record = ledger.load()[candidate_key("Blake", "proper_name")]
    assert record["target"] is None
    assert set(record["conflicts"]) == {"Альфа", "Бета", "Вариант2"}
    # Idempotent repeat of a chapter (re-run) must not change the state.
    stats = ledger.append_chapter("ch0003", _targeted_obs("Альфа"))
    assert stats == {"appended": 0, "new_candidates": 0, "updated": 1}
    record = ledger.load()[candidate_key("Blake", "proper_name")]
    assert record["target"] is None
    assert set(record["conflicts"]) == {"Альфа", "Бета", "Вариант2"}
    assert len(record["chapters"]) == 3
    assert record["total_occurrences"] == 6  # not 8


def test_ledger_differing_targets_across_chapters_gives_no_target(tmp_path):
    # Chapter 2 reaches a different consensus (Блейк instead of Блэйк) →
    # the merged record has no single distinct target, and both disagreeing
    # chapter targets are recorded in conflicts.
    other = {pid: text.replace("Блэйк", "Блейк")
             for pid, text in _CONSENSUS_TRANSLATIONS.items()}
    ledger = GlossaryCandidateLedger(str(tmp_path / "glossary_candidates.json"))
    ledger.append_chapter("ch0001", _aligned(_CONSENSUS_TRANSLATIONS))
    ledger.append_chapter("ch0002", _aligned(other))
    record = ledger.load()[candidate_key("Blake", "proper_name")]
    assert record["target"] is None
    assert set(record["conflicts"]) == {"Блэйк", "Блейк"}


def test_ledger_append_only_no_duplicate_on_rerun(tmp_path):
    ledger = GlossaryCandidateLedger(str(tmp_path / "glossary_candidates.json"))
    stats1 = ledger.append_chapter("ch0001", _aligned(_CONSENSUS_TRANSLATIONS))
    stats2 = ledger.append_chapter("ch0001", _aligned(_CONSENSUS_TRANSLATIONS))
    assert stats1["appended"] == 1
    assert stats2 == {"appended": 0, "new_candidates": 0, "updated": 1}
    record = ledger.load()[candidate_key("Blake", "proper_name")]
    assert record["total_occurrences"] == 4  # not 8
    assert len(record["chapters"]) == 1


def test_ledger_crash_safe_partial_trailing_line(tmp_path):
    path = str(tmp_path / "glossary_candidates.json")
    ledger = GlossaryCandidateLedger(path)
    ledger.append_chapter("ch0001", _aligned(_CONSENSUS_TRANSLATIONS))
    # Simulate an interrupted append: a torn, unterminated JSON line.
    with open(path, "a", encoding="utf-8") as f:
        f.write('{"chapter_id": "ch0002", "source": "Broke')
    records = ledger.load()  # must not raise
    key = candidate_key("Blake", "proper_name")
    assert records[key]["total_occurrences"] == 4
    # Appending after the torn line repairs the file.
    ledger.append_chapter("ch0002", _aligned(_CONSENSUS_TRANSLATIONS))
    records = ledger.load()
    assert len(records[key]["chapters"]) == 2


def test_ledger_middle_corruption_raises(tmp_path):
    path = str(tmp_path / "glossary_candidates.json")
    ledger = GlossaryCandidateLedger(path)
    ledger.append_chapter("ch0001", _aligned(_CONSENSUS_TRANSLATIONS))
    ledger.append_chapter("ch0002", _aligned(_CONSENSUS_TRANSLATIONS))
    # Insert a corrupt line in the MIDDLE (a trailing torn line is legal and
    # must be skipped — only middle corruption is fatal).
    with open(path, "r", encoding="utf-8") as f:
        lines = f.readlines()
    lines.insert(1, "{not json}\n")
    with open(path, "w", encoding="utf-8") as f:
        f.writelines(lines)
    with pytest.raises(ValueError):
        ledger.load()


def test_ledger_merge_is_idempotent_for_overlapping_snapshots(tmp_path):
    # Merging the same observation set twice must equal merging it once
    # (per-(key, chapter) collapse prevents double counting).
    aligned = _aligned(_CONSENSUS_TRANSLATIONS)
    obs_by_key = {candidate_key(c["source"], c["kind"]): [c] for c in aligned}
    merged = GlossaryCandidateLedger.merge(obs_by_key, obs_by_key)
    single = GlossaryCandidateLedger.merge({}, obs_by_key)
    assert merged == single
    record = merged[candidate_key("Blake", "proper_name")]
    assert record["total_occurrences"] == 4  # not 8
