#!/bin/bash
set -euo pipefail
REPO="/home/rt/projects/pact-ensemble-translator"
WORKTREE_ROOT="$(git rev-parse --show-toplevel 2>/dev/null || pwd)"
echo "[pact-fidelity-lint] static checks (no model calls)..."
FAIL=0
diff=$(git -C "$REPO" diff 2>/dev/null || true)
# 1. Hard audit filters bypass
if echo "$diff" | grep -qE "hard.*filter.*false|bypass.*audit|skip.*audit"; then
  echo "FAIL: possible hard audit filter bypass"; FAIL=1
fi
# 2. Hidden model HTTP calls in model-free stages
if echo "$diff" | grep -qE "requests\.(get|post).*llama|openai|qwen|gemma" && echo "$diff" | grep -q "phase0\|phase.*audit"; then
  echo "FAIL: model HTTP call in model-free stage"; FAIL=1
fi
# 3. Glossary bulk overwrite
if echo "$diff" | grep -qE "glossary.*=.*\{\}|memory.*clear|bulk.*overwrite"; then
  echo "WARN: possible glossary/memory bulk overwrite — verify"; fi
# 4. Non-deterministic ordering
if echo "$diff" | grep -qE "set\(\)|dict\.keys\(\)" && ! echo "$diff" | grep -q "sorted"; then
  echo "WARN: set/dict iteration without sorted — determinism risk"; fi
# 5. Prompt JSON contract
if git -C "$REPO" diff --name-only 2>/dev/null | grep -q "prompt"; then
  echo "INFO: prompt touched — verify JSON contract preserved"
fi

# 6. Glossary resolver pair-based lint (deterministic link, not suffix)
# Suffix а/я/у/ю/ом and transliteration H->Х hard checks are REMOVED (per glossary-model-resolver spec).
# Instead, check that glossary_proposals.json pair validation uses surface_forms∈evidence, lemma_v1 and blocklist.
if echo "$diff" | grep -q "glossary_resolver" || echo "$diff" | grep -q "glossary_proposal"; then
  echo "INFO: glossary resolver touched — verify pair-based lint (surface∈evidence + lemma_v1 + blocklist), not suffix"
  # Ensure blocklist still present for incident regression (Roxanne->Babula)
  if ! echo "$diff" | grep -q "blocklist\|Бабуль" && ! git -C "$REPO" diff --name-only | grep -q "glossary_resolver"; then
    echo "WARN: glossary resolver change without blocklist check — verify"
  fi
fi
# Suffix hard fail is removed: Кристоффа/Диониса stem-equal to Кристофф/Дионис, so deterministic lint must NOT fail on suffix а/я
if echo "$diff" | grep -qE "suffix.*а/я|translit.*H.*Х" && echo "$diff" | grep -q "hard.*lint"; then
  echo "WARN: suffix/translit hard lint is deprecated — pair-based lint only, shadow quality evaluation for case errors"
fi

# 7. Pair-based fidelity lint on proposals (surface_forms∈evidence, lemma_v1, blocklist)
# Validates glossary_proposals.json and resolver fixtures: each proposal must have
# surface_forms non-empty, each surface contained in its evidence text when available,
# lemma_v1 token-wise stem match, not blocklisted, not RU_STOP. Fails the lint on violation.
echo "[pact-fidelity-lint] pair-based fidelity lint (proposals)..."
PAIR_FAIL=0
# Collect candidate files: staged/changed glossary_proposals.json and test fixtures
mapfile -t PROPOSAL_FILES < <(git -C "$WORKTREE_ROOT" diff --name-only HEAD 2>/dev/null | grep -E "glossary_proposals\.json|fixture.*glossary|glossary.*fixture" || true)
# Also scan tracked files for glossary_proposals.json under tests
while IFS= read -r -d '' f; do
  # avoid duplicates
  skip=0
  for existing in "${PROPOSAL_FILES[@]:-}"; do [[ "$f" == "$existing" ]] && skip=1 && break; done
  [[ $skip -eq 1 ]] || PROPOSAL_FILES+=("$f")
done < <(find "$WORKTREE_ROOT/tests" -name "*glossary*proposal*.json" -print0 2>/dev/null || true)
# Also include any glossary_proposals.json in tests directory
while IFS= read -r -d '' f; do
  skip=0
  for existing in "${PROPOSAL_FILES[@]:-}"; do [[ "$f" == "$existing" ]] && skip=1 && break; done
  [[ $skip -eq 1 ]] || PROPOSAL_FILES+=("$f")
done < <(find "$WORKTREE_ROOT" -maxdepth 4 -name "glossary_proposals.json" -print0 2>/dev/null || true)

if [[ ${#PROPOSAL_FILES[@]} -eq 0 ]]; then
  echo "  no proposal files to lint (skipped)"
else
  for pf in "${PROPOSAL_FILES[@]}"; do
    [[ -f "$pf" ]] || continue
    echo "  linting $pf"
    if ! python3 - "$pf" <<'PYEOF'
import json, re, sys, pathlib
path = pathlib.Path(sys.argv[1])
try:
    payload = json.loads(path.read_text(encoding="utf-8"))
except Exception as e:
    print(f"FAIL: {path}: unreadable JSON: {e}")
    sys.exit(1)
# Support both sidecar payload and raw fixture list
proposals = None
translation_map = None
if isinstance(payload, dict) and "proposals" in payload:
    proposals = payload["proposals"]
    # try to load translation map if present in payload or beside file
    translation_map = payload.get("translations") or payload.get("translation_map")
    if translation_map is None:
        # Look for translations.json beside proposal file
        cand = path.parent / "translations.json"
        if cand.exists():
            try:
                tm = json.loads(cand.read_text(encoding="utf-8"))
                if isinstance(tm, dict) and "translations" in tm:
                    translation_map = tm["translations"]
                elif isinstance(tm, dict):
                    translation_map = tm
            except Exception:
                pass
elif isinstance(payload, list):
    proposals = payload
else:
    proposals = payload.get("proposals", []) if isinstance(payload, dict) else []

_RU_ENDINGS = sorted({"иями","ями","ами","ого","ему","ому","ыми","ими","иях","ах","ях","ой","ей","ый","ий","ая","яя","ую","юю","ом","ем","ым","им","ов","ев","ам","ям","а","я","у","ю","ы","и","е","о","ь"}, key=len, reverse=True)
def _ru_stem(w):
    core = re.sub(r"[^А-Яа-яЁё]", "", w).casefold().replace("ё","е")
    if len(core) <= 2:
        return core
    for e in _RU_ENDINGS:
        if core.endswith(e) and len(core)-len(e) >=3:
            return core[:-len(e)]
    return core
def lemma_match(surfaces, proposed):
    if not proposed or not surfaces:
        return False
    ptoks = proposed.strip().split()
    if not ptoks:
        return False
    pstems = [_ru_stem(t) for t in ptoks]
    for surf in surfaces:
        stoks = str(surf).strip().split()
        if len(stoks) != len(ptoks):
            continue
        sstems = [_ru_stem(t) for t in stoks]
        if sstems == pstems:
            return True
    if len(ptoks)==1:
        ps = pstems[0]
        for surf in surfaces:
            for tok in str(surf).split():
                if _ru_stem(tok)==ps:
                    return True
        return False
    return False

_BLOCKLIST = {"бабуль"}
_RU_STOP = set("""и в во не что он на я с со как а то все она так его но да ты к у же вы за бы по только ее мне было вот от меня еще нет о из ему теперь когда даже ну вдруг ли если уже или ни быть был него до вас опять уж вам ведь там потом себя ничего ей может они тут где есть надо ней для мы тебя их чем была сам чтоб без будто чего раз тоже себе под будет ж тогда кто этот того потому этого какой совсем ним здесь этом один почти мой тем чтобы нее сейчас были куда зачем никогда можно при наконец два об другой хоть после над больше тот через эти них какая много разве три эту моя впрочем хорошо свою этой перед иногда лучше чуть том нельзя такой им более всегда конечно всю между нибудь собою очень однако причем притом либо нежели ежели коль покуда доколе оттого откуда отколь""".split())
CYR = re.compile(r"[А-Яа-яЁё]")

fail=0
for idx, prop in enumerate(proposals or []):
    if not isinstance(prop, dict):
        print(f"FAIL: {path} proposal {idx} not object")
        fail=1; continue
    entity = prop.get("entity","")
    proposed = prop.get("proposed_ru","")
    surfaces = prop.get("surface_forms",[])
    evidence_pid = prop.get("evidence_pid","")
    # surface_forms must be non-empty strings
    if not isinstance(surfaces, list) or not surfaces or not all(isinstance(s,str) and s for s in surfaces):
        print(f"FAIL: {path} proposal {idx} ({entity}): surface_forms invalid {surfaces!r}")
        fail=1; continue
    # evidence check if translation_map available
    if translation_map is not None and evidence_pid:
        ev_text = translation_map.get(evidence_pid, "")
        for sf in surfaces:
            if sf not in ev_text:
                print(f"FAIL: {path} proposal {idx} ({entity}): surface_forms {sf!r} not in evidence {evidence_pid!r}")
                fail=1
    # lemma_v1 link
    if not lemma_match(surfaces, proposed):
        print(f"FAIL: {path} proposal {idx} ({entity}): lemma_v1 mismatch {surfaces!r} -> {proposed!r}")
        fail=1
    # blocklist
    if isinstance(proposed, str) and proposed.casefold() in _BLOCKLIST:
        print(f"FAIL: {path} proposal {idx} ({entity}): blocklisted {proposed!r}")
        fail=1
    # RU_STOP
    if isinstance(proposed, str) and proposed.casefold() in _RU_STOP:
        print(f"FAIL: {path} proposal {idx} ({entity}): RU_STOP {proposed!r}")
        fail=1
    # cyrillic check
    if isinstance(proposed, str) and not CYR.search(proposed):
        print(f"FAIL: {path} proposal {idx} ({entity}): not cyrillic {proposed!r}")
        fail=1
sys.exit(fail)
PYEOF
    then
      echo "  FAIL: pair-based lint failed for $pf"
      PAIR_FAIL=1
    else
      echo "  PASS: $pf"
    fi
  done
fi
# Also validate in-repo Python fixtures that embed proposals (test files) via import checks
# Run embedded pair lint on known fixture logic if files exist
if [[ $PAIR_FAIL -ne 0 ]]; then
  echo "[pact-fidelity-lint] pair-based lint FAIL"
  FAIL=1
else
  echo "[pact-fidelity-lint] pair-based lint PASS"
fi

if [[ $FAIL -eq 0 ]]; then echo "[pact-fidelity-lint] PASS (static, no pipeline)"; else echo "[pact-fidelity-lint] FAIL"; exit 1; fi
