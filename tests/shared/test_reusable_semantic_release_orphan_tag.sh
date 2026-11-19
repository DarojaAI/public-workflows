#!/usr/bin/env bash
# =============================================================================
# Regression tests for orphan tag collision handling in
# reusable-semantic-release.yml.
#
# The bug being guarded against: an orphan tag (e.g. one created manually
# to "claim" a version, or by an interrupted previous run) blocks
# semantic-release with exit code 128 ("fatal: tag 'vX.Y.Z' already
# exists") on `git tag vX.Y.Z <head-sha>`.
#
# The fix: a "Detect and resolve orphan tag collisions" step that
# runs `npx semantic-release --dry-run` to discover the next version,
# checks `git ls-remote` for the corresponding tag, and either
# self-heals (deletes local + remote tag) when the caller opted in
# via the self-heal-orphan-tags input, or fails fast with a clear
# error message naming the exact commands needed to clean up.
# =============================================================================
set -uo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
REUSABLE_RELEASE_YML="${REPO_ROOT}/.github/workflows/reusable-semantic-release.yml"

PASS=0
FAIL=0

assert() {
  local msg="$1"
  local cmd="$2"
  local rc=0
  bash -c "$cmd" >/dev/null 2>&1
  rc=$?
  if [[ $rc -eq 0 ]]; then
    echo "  ✅ $msg"
    PASS=$((PASS + 1))
  else
    echo "  ❌ $msg"
    FAIL=$((FAIL + 1))
  fi
}

[[ -f "$REUSABLE_RELEASE_YML" ]] || { echo "::error::reusable-semantic-release.yml not found at $REUSABLE_RELEASE_YML"; exit 1; }

# =============================================================================
# reusable-semantic-release.yml: callers via workflow_call get this behavior.
# =============================================================================
echo ""
echo "=== Assertion 1: reusable-semantic-release.yml has the input + step ==="
assert "reusable has self-heal-orphan-tags input" "grep -q '^      self-heal-orphan-tags:' $REUSABLE_RELEASE_YML"
assert "reusable input is type: boolean" "grep -A20 '^      self-heal-orphan-tags:' $REUSABLE_RELEASE_YML | grep -q 'type: boolean'"
assert "reusable input default is false" "grep -A20 '^      self-heal-orphan-tags:' $REUSABLE_RELEASE_YML | grep -q 'default: false'"
assert "reusable has the orphan-tag detection step" "grep -q 'name: Detect and resolve orphan tag collisions' $REUSABLE_RELEASE_YML"
assert "reusable step has id: detect-orphan" "grep -A40 'name: Detect and resolve orphan tag collisions' $REUSABLE_RELEASE_YML | grep -q 'id: detect-orphan'"
assert "reusable step runs npx semantic-release --dry-run" "grep -A100 'name: Detect and resolve orphan tag collisions' $REUSABLE_RELEASE_YML | grep -q 'npx semantic-release --dry-run'"
assert "reusable step has ::error:: annotations" "grep -A100 'name: Detect and resolve orphan tag collisions' $REUSABLE_RELEASE_YML | grep -q '::error::Orphan tag'"
assert "reusable step deletes the remote tag when self-healing" "grep -A100 'name: Detect and resolve orphan tag collisions' $REUSABLE_RELEASE_YML | grep -q 'git push origin \":refs/tags/'"
assert "reusable step deletes the local tag when self-healing" "grep -A100 'name: Detect and resolve orphan tag collisions' $REUSABLE_RELEASE_YML | grep -q 'git tag -d'"

echo ""
echo "=== Orphan-tag step runs BEFORE Run semantic-release ==="
DETECT_LINE=$(grep -n 'name: Detect and resolve orphan tag collisions' "$REUSABLE_RELEASE_YML" | head -1 | cut -d: -f1)
RUN_LINE=$(grep -n 'name: Run semantic-release' "$REUSABLE_RELEASE_YML" | head -1 | cut -d: -f1)
if [[ -n "$DETECT_LINE" && -n "$RUN_LINE" && "$DETECT_LINE" -lt "$RUN_LINE" ]]; then
  echo "  ✅ orphan-tag step runs before Run semantic-release (line $DETECT_LINE < $RUN_LINE)"
  PASS=$((PASS + 1))
else
  echo "  ❌ orphan-tag step does NOT run before Run semantic-release (detect=$DETECT_LINE, run=$RUN_LINE)"
  FAIL=$((FAIL + 1))
fi

# =============================================================================
# Functional smoke test: extract the step's shell script and verify it
# parses as valid bash and contains the expected control flow.
# =============================================================================
echo ""
echo "=== Assertion 2: functional smoke test of the extracted script ==="

SCRIPT=$(awk '
  /name: Detect and resolve orphan tag collisions/ { flag=1; next }
  flag && /run: \|/ { in_run=1; next }
  in_run && /^[[:space:]]*- name:/ { exit }
  in_run { print }
' "$REUSABLE_RELEASE_YML" | sed 's/^          //')

TMP=$(mktemp --suffix=.sh)
printf '%s\n' "$SCRIPT" > "$TMP"
chmod +x "$TMP"

if bash -n "$TMP" 2>/dev/null; then
  echo "  ✅ extracted script is syntactically valid bash"
  PASS=$((PASS + 1))
else
  echo "  ❌ extracted script has syntax errors:"
  bash -n "$TMP" 2>&1 | head -5
  FAIL=$((FAIL + 1))
fi

if grep -q 'SELF_HEAL' "$TMP"; then
  echo "  ✅ extracted script references SELF_HEAL"
  PASS=$((PASS + 1))
else
  echo "  ❌ extracted script does NOT reference SELF_HEAL"
  FAIL=$((FAIL + 1))
fi

if grep -E 'SELF_HEAL.*!=.*true' "$TMP" >/dev/null; then
  echo "  ✅ extracted script gates the fail-fast branch on SELF_HEAL check"
  PASS=$((PASS + 1))
else
  echo "  ❌ extracted script does NOT gate the fail-fast branch on SELF_HEAL check"
  FAIL=$((FAIL + 1))
fi

rm -f "$TMP"

echo ""
echo "========================================"
echo "Results: $PASS passed, $FAIL failed"
echo "========================================"

if [[ "$FAIL" -gt 0 ]]; then
  exit 1
fi
exit 0
