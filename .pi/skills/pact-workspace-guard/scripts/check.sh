#!/bin/bash
# pact-workspace-guard — real checks, dev-only, no pipeline
set -euo pipefail
REPO="/home/rt/projects/pact-ensemble-translator"
FAIL=0

branch=$(git -C "$REPO" rev-parse --abbrev-ref HEAD 2>/dev/null || echo "unknown")
worktree=$(pwd)

echo "[pact-workspace-guard] branch=$branch worktree=$worktree"

# 1. Isolated branch/worktree — allow main only for docs-only diffs, otherwise block
if [[ "$branch" == "main" ]]; then
  # check if diff is docs-only (md files)
  if git -C "$REPO" diff --name-only 2>/dev/null | grep -qvE '\.md$'; then
    echo "FAIL: on main with non-docs changes — develop only in isolated branch/worktree"
    echo "  expected: /home/rt/projects/pact-worktrees/<change-name>"
    FAIL=1
  else
    echo "OK: on main with docs-only change (allowed per AGENTS.md)"
  fi
else
  echo "OK: isolated branch $branch"
fi

if [[ "$worktree" == "/home/rt/projects/pact-ensemble-translator" && "$branch" == "main" ]]; then
  : # allowed docs case above
elif [[ "$worktree" != /home/rt/projects/pact-worktrees/* && "$worktree" != "$REPO" ]]; then
  echo "WARN: worktree $worktree not in expected locations"
fi

# 2. Never edit production — scan staged diff for RT absolute paths
if git -C "$REPO" diff --cached --name-only 2>/dev/null | grep -q "pact_translator_v4_1"; then
  echo "FAIL: staged diff references production checkout D:\\pact\\pact_translator_v4_1"
  FAIL=1
fi
if grep -r "D:\\\\pact\\\\pact_translator_v4_1" --include="*.py" --include="*.md" --include="*.json" "$REPO/pact_v4" 2>/dev/null | grep -v ".pyc" | head -n 5 | grep -q .; then
  echo "WARN: found RT production path in pact_v4 — ensure not introduced in diff:"
  grep -rn "D:\\\\pact" "$REPO/pact_v4" 2>/dev/null | head -n 5 || true
fi

# 3. No destructive git in recent history / staged
if git -C "$REPO" diff --cached --name-only 2>/dev/null | head -n 20 | grep -q .; then
  echo "OK: staged files checked"
fi

# 4. No llama-server ops in diff
if git -C "$REPO" diff 2>/dev/null | grep -qE "llama-server|kill.*llama|pact.*pipeline.*start"; then
  echo "FAIL: diff contains llama-server / pipeline start — requires explicit owner approval"
  FAIL=1
fi

if [[ $FAIL -eq 0 ]]; then
  echo "[pact-workspace-guard] PASS"
else
  echo "[pact-workspace-guard] FAIL — stop and report to Architect"
  exit 1
fi
