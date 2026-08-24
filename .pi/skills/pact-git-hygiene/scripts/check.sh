#!/bin/bash
set -euo pipefail
REPO="/home/rt/projects/pact-ensemble-translator"
FAIL=0
echo "[pact-git-hygiene] checking diff..."
stat=$(git -C "$REPO" diff --stat 2>/dev/null | tail -n 20)
echo "$stat"
# 1. Focused diff — warn if >15 files or mixed docs+code
nfiles=$(git -C "$REPO" diff --name-only 2>/dev/null | wc -l)
if [[ $nfiles -gt 15 ]]; then echo "WARN: $nfiles files changed — diff not focused, split change"; fi
if git -C "$REPO" diff --name-only 2>/dev/null | grep -q "\.py$" && git -C "$REPO" diff --name-only 2>/dev/null | grep -q "\.md$"; then
  echo "WARN: mixed code+docs in one diff — prefer separate commits"
fi
# 2. Secrets
if git -C "$REPO" diff 2>/dev/null | grep -qiE "sk-|api_key|secret|token.*[A-Za-z0-9]{20}"; then
  echo "FAIL: possible secret in diff"; FAIL=1
fi
# 3. Data boundaries — no source text / .docx committed
if git -C "$REPO" diff --name-only --cached 2>/dev/null | grep -qE "\.(docx|epub|pdf)$"; then
  echo "FAIL: book artifacts staged"; FAIL=1
fi
# 4. merge != deploy
if git -C "$REPO" log --oneline -5 2>/dev/null | grep -qi "deploy"; then echo "INFO: deploy keyword in log — ensure owner approved"; fi
if [[ $FAIL -eq 0 ]]; then echo "[pact-git-hygiene] PASS"; else echo "[pact-git-hygiene] FAIL"; exit 1; fi
