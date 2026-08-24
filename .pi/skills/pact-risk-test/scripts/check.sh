#!/bin/bash
# pact-risk-test — classify and run narrowest tests
set -euo pipefail
REPO="/home/rt/projects/pact-ensemble-translator"
echo "[pact-risk-test] classifying diff..."
files=$(git -C "$REPO" diff --name-only origin/main...HEAD 2>/dev/null || git -C "$REPO" diff --name-only 2>/dev/null || echo "")
if [[ -z "$files" ]]; then echo "No diff — Low (no changes)"; exit 0; fi
echo "$files" | sed 's/^/  /'
risk="Low"
if echo "$files" | grep -qE "pact_v4/(pipeline|runtime|repair|phase|audit)"; then risk="High"
elif echo "$files" | grep -qE "pact_v4/(prompts|select|config|models)"; then risk="Medium"
elif echo "$files" | grep -qE "\.py$"; then risk="Medium"
fi
echo "Risk: $risk"
if [[ "$risk" == "High" ]]; then
  echo "High: inspect callers, contracts, failure paths, caches, determinism before change"
elif [[ "$risk" == "Medium" ]]; then
  echo "Medium: inspect localized contracts and run targeted tests"
else
  echo "Low: mechanical/docs — minimal checks"
fi
# run narrow suite hint
echo "Hint: python -m pytest pact_v4/tests/test_<relevant> -q"
# try running relevant tests if exist
if [[ "$risk" != "Low" ]]; then
  if ls "$REPO/pact_v4/tests/"*.py 1>/dev/null 2>&1; then
    echo "[pact-risk-test] not auto-running full suite — run targeted tests manually"
  fi
fi
echo "[pact-risk-test] DONE"
