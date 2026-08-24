#!/bin/bash
set -euo pipefail
REPO="/home/rt/projects/pact-ensemble-translator"
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
if [[ $FAIL -eq 0 ]]; then echo "[pact-fidelity-lint] PASS (static, no pipeline)"; else echo "[pact-fidelity-lint] FAIL"; exit 1; fi
