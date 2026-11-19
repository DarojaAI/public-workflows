#!/usr/bin/env bash
# =============================================================================
# Regression tests for the optional GitHub App token on
# reusable-enable-pr-automerge.yml (reusable-pr-automerge.yml is not public; it keeps its own test).
#
# Contract under test (this reusable):
#   - Inputs: app-client-id (string, optional, not secret).
#     Secret:  app-private-key (optional).
#   - Both set:    the reusable mints a token with
#                  actions/create-github-app-token (pinned SHA), owned by the
#                  caller's owner, scoped to the caller's repo name only, with
#                  contents: write and pull-requests: write. That token is used
#                  for the merge calls.
#   - Neither set: fall back to GITHUB_TOKEN, as before.
#   - Only one set: fail with a clear error. No silent fallback.
#   - The private key is never echoed.
#   - No workflow_call secret is named GITHUB_TOKEN (reserved name).
#   - The enable workflow keeps its pull_request trigger.
#
# The validation step is real bash. It is extracted from each workflow and run
# for each case. The rest is asserted on the workflow text, because GitHub
# expressions and the mint action cannot run outside Actions.
# =============================================================================
set -uo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
ENABLE="${REPO_ROOT}/.github/workflows/reusable-enable-pr-automerge.yml"
MINT_SHA="64ab729a6294000f5aa4a4bc0b73d1f32bc45510"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

PASS=0
FAIL=0

ok() { echo "  ✅ $1"; PASS=$((PASS + 1)); }
bad() { echo "  ❌ $1"; FAIL=$((FAIL + 1)); }

assert() {
  local desc="$1"
  local cmd="$2"
  if eval "$cmd" >/dev/null 2>&1; then ok "$desc"; else bad "$desc"; echo "     CMD: $cmd"; fi
}

# Extract the run: script of the step named "Validate App token inputs" from a
# workflow into $2. Prints nothing. Exits non-zero if the step is missing.
extract_validate() {
  python3 - "$1" "$2" <<'PYEOF'
import sys, yaml
wf, out = sys.argv[1], sys.argv[2]
doc = yaml.safe_load(open(wf))
steps = []
for job in doc["jobs"].values():
    steps += job.get("steps", [])
for step in steps:
    if step.get("name") == "Validate App token inputs":
        open(out, "w").write(step["run"])
        sys.exit(0)
sys.exit(1)
PYEOF
}

# Run the extracted validation script with the given App inputs.
# $1 = script path, $2 = client id, $3 = private key, $4 = output file.
run_validate() {
  env -i PATH="$PATH" GITHUB_REPOSITORY="DarojaAI/example-repo" \
    APP_CLIENT_ID="$2" APP_PRIVATE_KEY="$3" GITHUB_OUTPUT="$4" \
    bash "$1"
}

echo "=== Assertion 1: the workflow declares the inputs and secret ==="
for wf in "$ENABLE"; do
  name="$(basename "$wf")"
  assert "$name declares app-client-id as an optional string input" \
    "python3 -c \"
import yaml,sys
d=yaml.safe_load(open('$wf'))
on=d.get(True, d.get('on'))
i=on['workflow_call']['inputs']['app-client-id']
sys.exit(0 if (i['type']=='string' and i['required'] is False) else 1)
\""
  assert "$name declares app-private-key as an optional secret" \
    "python3 -c \"
import yaml,sys
d=yaml.safe_load(open('$wf'))
on=d.get(True, d.get('on'))
s=on['workflow_call']['secrets']['app-private-key']
sys.exit(0 if s['required'] is False else 1)
\""
  assert "$name does not declare a github-token secret" \
    "! grep -q 'github-token' '$wf'"
  assert "$name does not declare a secret named GITHUB_TOKEN" \
    "! grep -qE '^      GITHUB_TOKEN:\$' '$wf'"
done

echo "=== Assertion 2: validation step behaves correctly for each input case ==="
for wf in "$ENABLE"; do
  name="$(basename "$wf")"
  script="$WORK/$name.validate.sh"
  if ! extract_validate "$wf" "$script"; then
    bad "$name has a 'Validate App token inputs' step"
    continue
  fi
  ok "$name has a 'Validate App token inputs' step"

  # Both set: passes, use-app=true, repo name only.
  out="$WORK/$name.both.out"; : > "$out"
  if run_validate "$script" "Iv1.abc" "FAKE-KEY-VALUE" "$out" >/dev/null 2>&1 \
     && grep -qx "use-app=true" "$out" && grep -qx "repo-name=example-repo" "$out"; then
    ok "$name: both set -> use-app=true, repo-name=example-repo"
  else
    bad "$name: both set -> expected use-app=true and repo-name=example-repo"
  fi

  # Neither set: passes, use-app=false (GITHUB_TOKEN fallback).
  out="$WORK/$name.neither.out"; : > "$out"
  if run_validate "$script" "" "" "$out" >/dev/null 2>&1 \
     && grep -qx "use-app=false" "$out"; then
    ok "$name: neither set -> use-app=false (GITHUB_TOKEN fallback)"
  else
    bad "$name: neither set -> expected use-app=false"
  fi

  # Only client id set: must fail, must not fall back silently.
  out="$WORK/$name.idonly.out"; : > "$out"
  err="$WORK/$name.idonly.err"
  if run_validate "$script" "Iv1.abc" "" "$out" >"$err" 2>&1; then
    bad "$name: only app-client-id set -> expected failure"
  elif grep -q "::error::app-client-id is set" "$err" && ! grep -q "use-app=" "$out"; then
    ok "$name: only app-client-id set -> fails with clear error, no fallback"
  else
    bad "$name: only app-client-id set -> wrong error or fallback happened"
  fi

  # Only private key set: must fail, must not fall back silently.
  out="$WORK/$name.keyonly.out"; : > "$out"
  err="$WORK/$name.keyonly.err"
  if run_validate "$script" "" "FAKE-KEY-VALUE" "$out" >"$err" 2>&1; then
    bad "$name: only app-private-key set -> expected failure"
  elif grep -q "::error::app-private-key is set" "$err" && ! grep -q "use-app=" "$out"; then
    ok "$name: only app-private-key set -> fails with clear error, no fallback"
  else
    bad "$name: only app-private-key set -> wrong error or fallback happened"
  fi

  # The key value must never be printed (stdout or stderr).
  run_validate "$script" "Iv1.abc" "FAKE-KEY-VALUE" "$WORK/$name.leak.out" \
    > "$WORK/$name.leak.log" 2>&1
  if grep -q "FAKE-KEY-VALUE" "$WORK/$name.leak.log"; then
    bad "$name: validation output leaks the key value"
  else
    ok "$name: validation output never contains the key value"
  fi
done

echo "=== Assertion 3: mint step uses the pinned action with the required settings ==="
for wf in "$ENABLE"; do
  name="$(basename "$wf")"
  assert "$name mints with actions/create-github-app-token at SHA $MINT_SHA (# v3.3.0)" \
    "grep -qF 'uses: actions/create-github-app-token@$MINT_SHA # v3.3.0' '$wf'"
  assert "$name mint step runs only when use-app is true" \
    "grep -qF \"if: steps.app-inputs.outputs.use-app == 'true'\" '$wf'"
  assert "$name mint owner is the repository owner" \
    "grep -qF 'owner: \${{ github.repository_owner }}' '$wf'"
  assert "$name mint repositories is the caller repo name only" \
    "grep -qF 'repositories: \${{ steps.app-inputs.outputs.repo-name }}' '$wf'"
  assert "$name mint requests contents: write and pull-requests: write" \
    "grep -qF 'permission-contents: write' '$wf' && grep -qF 'permission-pull-requests: write' '$wf'"
done

echo "=== Assertion 4: merge calls use the App token, else GITHUB_TOKEN ==="
assert "enable workflow GH_TOKEN is minted token || GITHUB_TOKEN" \
  "grep -qF 'GH_TOKEN: \${{ steps.app-token.outputs.token || secrets.GITHUB_TOKEN }}' '$ENABLE'"

echo "=== Assertion 5: the key is never echoed and is only read from the secret ==="
assert "no echo or set -x line expands the private key" \
  "! grep -E '^[^#]*(echo|printf|set -x).*(\\\$\\{?APP_PRIVATE_KEY|secrets\\.app-private-key)' '$ENABLE'"

echo "=== Assertion 6: enable workflow keeps its pull_request trigger ==="
assert "enable workflow is still workflow_call and not pull_request_target" \
  "! grep -q 'pull_request_target' '$ENABLE' && grep -q 'workflow_call' '$ENABLE'"

echo
echo "Results: $PASS passed, $FAIL failed"
[ "$FAIL" -eq 0 ]
