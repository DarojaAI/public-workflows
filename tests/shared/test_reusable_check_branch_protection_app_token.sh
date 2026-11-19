#!/usr/bin/env bash
# =============================================================================
# Regression tests for the optional GitHub App token on
#   reusable-check-branch-protection.yml
# (reusable-verify-required-checks.yml is not public; it keeps its own test.)
#
# Contract under test (mirrors reusable-security-gate.yml):
#   - Input app-client-id (string, optional), secret app-private-key (optional),
#     github-token secret kept and now optional.
#   - Both set:    mint in the same job with actions/create-github-app-token
#                  (pinned SHA), owner = caller owner, repositories = the audited
#                  repo name only, permission-administration: read and nothing else.
#   - Neither set: github-token as before. Needs a github-token.
#   - Only one set: fail with a clear ::error::, no fallback.
#   - repo input must be owner/name with owner == repository owner on the App path.
#   - App path fails closed on 401/403/non-trivial 404 reads. The github-token
#     path keeps its previous behaviour.
#   - The key and token are never echoed; the token never crosses job outputs.
# The validation and read steps are real bash, extracted and run against a fake gh.
# =============================================================================
set -uo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
WFDIR="${REPO_ROOT}/.github/workflows"
BP="${WFDIR}/reusable-check-branch-protection.yml"
MINT_SHA="64ab729a6294000f5aa4a4bc0b73d1f32bc45510"
# Fixture baseline for these tests only (the workflow has no built-in baseline).
TEST_BASE='{"enforce_admins":true,"required_linear_history":true,"allow_force_pushes":false,"allow_deletions":false,"block_creations":false,"required_conversation_resolution":true,"required_pull_request_reviews":{"required_approving_review_count":2,"dismiss_stale_reviews":true,"require_code_owner_reviews":false,"require_last_push_approval":true}}'
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

PASS=0
FAIL=0
ok() { echo "  PASS $1"; PASS=$((PASS + 1)); }
bad() { echo "  FAIL $1"; FAIL=$((FAIL + 1)); }
assert() {
  if eval "$2" >/dev/null 2>&1; then ok "$1"; else bad "$1"; echo "     CMD: $2"; fi
}

# extract <workflow> <job> <step name> <out file>
extract() {
  python3 - "$@" <<'PYEOF'
import sys, yaml
wf, job, name, out = sys.argv[1:5]
for st in yaml.safe_load(open(wf))["jobs"][job]["steps"]:
    if st.get("name") == name:
        open(out, "w").write(st["run"]); sys.exit(0)
sys.exit(1)
PYEOF
}

# yaml_check <workflow> <job> <python expr on `steps`>
yaml_check() {
  python3 - "$1" "$2" "$3" <<'PYEOF'
import sys, yaml
wf, job, expr = sys.argv[1:4]
steps = yaml.safe_load(open(wf))["jobs"][job]["steps"]
sys.exit(0 if eval(expr, {"steps": steps}) else 1)
PYEOF
}

FILES="bp:$BP:check"

echo "=== 1: inputs and secrets ==="
for entry in $FILES; do
  tag="${entry%%:*}"; rest="${entry#*:}"; wf="${rest%%:*}"
  assert "$tag: app-client-id is an optional string input defaulting to empty" \
    "python3 -c \"
import yaml,sys
i=yaml.safe_load(open('$wf'))[True]['workflow_call']['inputs']['app-client-id']
sys.exit(0 if i['type']=='string' and i['required'] is False and i['default']=='' else 1)\""
  assert "$tag: app-private-key and github-token are both optional secrets" \
    "python3 -c \"
import yaml,sys
s=yaml.safe_load(open('$wf'))[True]['workflow_call']['secrets']
sys.exit(0 if s['app-private-key']['required'] is False and s['github-token']['required'] is False else 1)\""
  assert "$tag: no secret is named GITHUB_TOKEN" \
    "python3 -c \"
import yaml,sys
s=yaml.safe_load(open('$wf'))[True]['workflow_call']['secrets']
sys.exit(1 if any(k.upper()=='GITHUB_TOKEN' for k in s) else 0)\""
done

echo "=== 2: validation step ==="
run_validate() { # script client key input_repo output
  env -i PATH="$PATH" GITHUB_REPOSITORY="DarojaAI/example-repo" \
    REPO_OWNER="DarojaAI" DEFAULT_REPO="DarojaAI/example-repo" \
    APP_CLIENT_ID="$2" APP_PRIVATE_KEY="$3" INPUT_REPO="$4" INPUT_BRANCH="${IBR-}" \
    GITHUB_TOKEN_SECRET="${GHT-LEAK-TOKEN-VALUE}" GITHUB_OUTPUT="$5" bash "$1"
}
for entry in $FILES; do
  tag="${entry%%:*}"; rest="${entry#*:}"; wf="${rest%%:*}"; job="${rest##*:}"
  script="$WORK/$tag.validate.sh"
  if ! extract "$wf" "$job" "Validate App token inputs" "$script"; then bad "$tag has a validation step"; continue; fi
  ok "$tag has a validation step"

  out="$WORK/$tag.o1"; : > "$out"
  if run_validate "$script" "Iv1.x" "FAKE-KEY-VALUE" "" "$out" >/dev/null 2>&1 \
     && grep -qx "use-app=true" "$out" && grep -qx "repo=DarojaAI/example-repo" "$out" \
     && grep -qx "repo-name=example-repo" "$out"; then ok "$tag: both set -> use-app=true, caller repo"
  else bad "$tag: both set"; fi

  out="$WORK/$tag.o2"; : > "$out"
  if run_validate "$script" "Iv1.x" "FAKE-KEY-VALUE" "DarojaAI/other-repo" "$out" >/dev/null 2>&1 \
     && grep -qx "repo-name=other-repo" "$out"; then ok "$tag: repo input in same owner -> scoped to that repo name"
  else bad "$tag: same-owner repo input"; fi

  out="$WORK/$tag.o2b"; : > "$out"
  if run_validate "$script" "Iv1.x" "FAKE-KEY-VALUE" "darojaai/other-repo" "$out" >/dev/null 2>&1; then
    ok "$tag: owner comparison ignores case"; else bad "$tag: owner case"; fi

  for badrepo in "Other/repo" "DarojaAI/a/b" "justname" "DarojaAI/.." "DarojaAI/x y" 'DarojaAI/x;id'; do
    out="$WORK/$tag.o3"; : > "$out"; err="$WORK/$tag.e3"
    if run_validate "$script" "Iv1.x" "FAKE-KEY-VALUE" "$badrepo" "$out" >"$err" 2>&1; then
      bad "$tag: App path accepted bad repo '$badrepo'"
    elif grep -q "::error::" "$err" && ! grep -q "use-app=" "$out"; then ok "$tag: App path rejects repo '$badrepo'"
    else bad "$tag: bad repo '$badrepo' wrong error"; fi
  done

  out="$WORK/$tag.o4"; : > "$out"
  if run_validate "$script" "" "" "Other/repo" "$out" >/dev/null 2>&1 \
     && grep -qx "use-app=false" "$out" && grep -qx "repo=Other/repo" "$out"; then
    ok "$tag: neither set -> use-app=false, repo input unchanged (backward compatible)"
  else bad "$tag: neither set"; fi

  out="$WORK/$tag.o5"; : > "$out"; err="$WORK/$tag.e5"
  if GHT="" run_validate "$script" "" "" "" "$out" >"$err" 2>&1; then bad "$tag: no credentials -> expected failure"
  elif grep -q "::error::No credentials" "$err"; then ok "$tag: no credentials at all -> clear error"
  else bad "$tag: no credentials wrong error"; fi

  out="$WORK/$tag.o6"; : > "$out"; err="$WORK/$tag.e6"
  if run_validate "$script" "Iv1.x" "" "" "$out" >"$err" 2>&1; then bad "$tag: id only -> expected failure"
  elif grep -q "::error::app-client-id is set" "$err" && ! grep -q "use-app=" "$out"; then ok "$tag: id only -> error, no fallback"
  else bad "$tag: id only wrong"; fi

  out="$WORK/$tag.o7"; : > "$out"; err="$WORK/$tag.e7"
  if run_validate "$script" "" "FAKE-KEY-VALUE" "" "$out" >"$err" 2>&1; then bad "$tag: key only -> expected failure"
  elif grep -q "::error::app-private-key is set" "$err" && ! grep -q "use-app=" "$out"; then ok "$tag: key only -> error, no fallback"
  else bad "$tag: key only wrong"; fi

  for badrepo in $'DarojaAI/x\nuse-app=true' $'DarojaAI/x\r' $'Daroja AI/x' 'Daroja.AI/x'; do
    for creds in "|" "Iv1.x|FAKE-KEY-VALUE"; do
      out="$WORK/$tag.inj"; : > "$out"
      if run_validate "$script" "${creds%%|*}" "${creds##*|}" "$badrepo" "$out" >/dev/null 2>&1; then
        bad "$tag: accepted repo with newline/CR/space/dot-owner (creds '${creds%%|*}')"
      elif [ ! -s "$out" ]; then ok "$tag: rejects bad repo, GITHUB_OUTPUT untouched (creds '${creds%%|*}')"
      else bad "$tag: bad repo wrote to GITHUB_OUTPUT"; fi
    done
  done
  if [ "$tag" = bp ]; then
    for badbr in $'main\nuse-app=true' $'a/../b' 'a b' $'x\r'; do
      out="$WORK/$tag.injb"; : > "$out"
      if IBR="$badbr" run_validate "$script" "" "" "" "$out" >/dev/null 2>&1; then
        bad "bp: token path accepted branch '$badbr'"
      elif [ ! -s "$out" ]; then ok "bp: token path rejects bad branch, GITHUB_OUTPUT untouched"
      else bad "bp: bad branch wrote to GITHUB_OUTPUT"; fi
    done
    out="$WORK/$tag.okb"; : > "$out"
    if IBR="release/v1.2_x" run_validate "$script" "" "" "" "$out" >/dev/null 2>&1; then ok "bp: valid branch accepted"; else bad "bp: valid branch rejected"; fi
  fi
  run_validate "$script" "" "" "" "$WORK/$tag.o9" >"$WORK/$tag.leak2" 2>&1
  if grep -q "LEAK-TOKEN-VALUE" "$WORK/$tag.leak2" "$WORK/$tag.o9"; then bad "$tag: github-token leaked by validation"; else ok "$tag: github-token never printed by validation"; fi
  run_validate "$script" "Iv1.x" "FAKE-KEY-VALUE" "" "$WORK/$tag.o8" >"$WORK/$tag.leak" 2>&1
  if grep -q "FAKE-KEY-VALUE\|tok" "$WORK/$tag.leak" && grep -q "FAKE-KEY-VALUE" "$WORK/$tag.leak"; then bad "$tag: key leaked"; else ok "$tag: key never printed"; fi
done

echo "=== 3: mint step ==="
for entry in $FILES; do
  tag="${entry%%:*}"; rest="${entry#*:}"; wf="${rest%%:*}"; job="${rest##*:}"
  assert "$tag: exactly one mint step, pinned SHA $MINT_SHA, only when use-app" \
    "yaml_check $wf $job \"len([s for s in steps if s.get('uses','').startswith('actions/create-github-app-token@')])==1 and any(s.get('uses')=='actions/create-github-app-token@$MINT_SHA' and s.get('if')==\\\"steps.app-inputs.outputs.use-app == 'true'\\\" for s in steps)\""
  assert "$tag: mint owner = repository owner, repositories = validated repo name" \
    "yaml_check $wf $job \"any(s.get('uses','').startswith('actions/create-github-app-token@') and s['with']['owner']=='\\\${{ github.repository_owner }}' and s['with']['repositories']=='\\\${{ steps.app-inputs.outputs.repo-name }}' for s in steps)\""
  assert "$tag: mint requests administration read and nothing else" \
    "yaml_check $wf $job \"any(s.get('uses','').startswith('actions/create-github-app-token@') and {k:v for k,v in s['with'].items() if k.startswith('permission-')}=={'permission-administration':'read'} for s in steps)\""
  assert "$tag: mint comes before every step that reads GH_TOKEN" \
    "yaml_check $wf $job \"[i for i,s in enumerate(steps) if s.get('id')=='app-token'][0] < min(i for i,s in enumerate(steps) if 'GH_TOKEN' in s.get('env',{}))\""
  assert "$tag: every GH_TOKEN is minted token || github-token" \
    "yaml_check $wf $job \"all(s['env']['GH_TOKEN']=='\\\${{ steps.app-token.outputs.token || secrets.github-token }}' for s in steps if 'GH_TOKEN' in s.get('env',{}))\""
  assert "$tag: the token is never exposed through job outputs" \
    "! grep -E 'outputs:.*app-token|value:.*app-token' $wf && ! grep -E '^\s+[a-z_]+: \\\$\{\{ steps\.app-token\.outputs\.token' $wf | grep -v GH_TOKEN"
  assert "$tag: no write permission requested" "! grep -E 'permission-[a-z-]+: write' $wf"
  assert "$tag: no echo/printf/set -x of the key or token" \
    "! grep -E '^[^#]*(echo|printf|set -x).*(\\\$\{?APP_PRIVATE_KEY|secrets\.app-private-key|steps\.app-token|\\\$\{?GITHUB_TOKEN_SECRET|\\\$\{?GH_TOKEN|secrets\.github-token)' $wf"
done

echo "=== 4: read steps against a fake gh ==="
mkdir -p "$WORK/bin"
cat > "$WORK/bin/gh" <<'GHEOF'
#!/usr/bin/env bash
# Fake gh. FAKE_MODE: ok | forbidden | notfound | unprotected | rulesets_forbidden | detail_forbidden
# Like the real gh api: no -o / -w flags (they exit 1); -i prints status line + headers + body.
[ "$1" = "api" ] || exit 2
shift
inc=0; path=""; jq=""
while [ $# -gt 0 ]; do
  case "$1" in
    -i|--include) inc=1; shift ;;
    -q|--jq) jq="$2"; shift 2 ;;
    -o|-w) echo "gh: unknown shorthand flag: '$1'" >&2; exit 1 ;;
    -*) shift ;;
    *) path="$1"; shift ;;
  esac
done
emit() { # status reason body
  if [ "$inc" = 1 ]; then printf 'HTTP/%s %s %s\r\nContent-Type: application/json\r\n\r\n%s\n' "${FAKE_HTTP:-2.0}" "$1" "$2" "$3"; else
    [ "$1" = 200 ] && printf '%s\n' "$3" || { printf '%s\n' "$3"; echo "gh: $2 (HTTP $1)" >&2; return 1; }; fi
}
case "$path" in
  */branches/*/protection)
    case "$FAKE_MODE" in
      forbidden) emit 403 Forbidden '{"message":"Resource not accessible by integration"}' ;;
      notfound) emit 404 "Not Found" '{"message":"Not Found"}' ;;
      unprotected) emit 404 "Not Found" '{"message":"Branch not protected"}' ;;
      *) PROT='{"enforce_admins":{"enabled":true},"required_status_checks":{"contexts":["ci"]}}'
         emit 200 OK "${FAKE_PROT:-$PROT}" ;;
    esac ;;
  */rulesets/*)
    [ "$FAKE_MODE" = detail_forbidden ] && { emit 403 Forbidden '{"message":"no"}'; exit $?; }
    emit 200 OK '{"id":1,"rules":[]}' ;;
  */rulesets)
    case "$FAKE_MODE" in
      rulesets_forbidden) emit 403 Forbidden '{"message":"no"}' ;;
      rulesets_notfound) emit 404 "Not Found" '{"message":"Not Found"}' ;;
      *) [ -n "$jq" ] && echo "1 2" || echo '[{"id":1},{"id":2}]' ;;
    esac ;;
  repos/*)
    case "$FAKE_MODE" in
      repo_notfound) emit 404 "Not Found" '{"message":"Not Found"}' ;;
      *) [ -n "$jq" ] && echo "trunk" || echo '{"default_branch":"trunk"}' ;;
    esac ;;
  *) exit 1 ;;
esac
GHEOF
chmod +x "$WORK/bin/gh"

# run_step <script> <mode> <use_app> [extra env...]; output in $WORK/step.log / step.out
run_step() {
  local script="$1" mode="$2" app="$3"; shift 3
  : > "$WORK/step.out"; : > "$WORK/step.sum"
  env -i PATH="$WORK/bin:$PATH" FAKE_MODE="$mode" USE_APP="$app" REPO="DarojaAI/example-repo" \
    BRANCH="trunk" INPUT_BRANCH="" IDS="1 2" GH_TOKEN="LEAK-TOKEN-VALUE" \
    GITHUB_OUTPUT="$WORK/step.out" GITHUB_STEP_SUMMARY="$WORK/step.sum" "$@" \
    bash -e "$script" >"$WORK/step.log" 2>&1
  local rc=$?
  cat "$WORK/step.log" "$WORK/step.out" "$WORK/step.sum" >> "$WORK/all.log"
  return $rc
}

# --- check-branch-protection: Read current protection ---
extract "$BP" check "Read current protection" "$WORK/bp.read.sh" && ok "bp read step extractable"
if run_step "$WORK/bp.read.sh" ok true && grep -qx "status=200" "$WORK/step.out"; then ok "bp App: 200 -> status=200"; else bad "bp App: 200"; fi
if run_step "$WORK/bp.read.sh" unprotected true && grep -qx "status=404" "$WORK/step.out"; then ok "bp App: 'Branch not protected' 404 -> reported, not an error"; else bad "bp App: unprotected"; fi
for m in forbidden notfound; do
  if run_step "$WORK/bp.read.sh" $m true; then bad "bp App: $m -> expected failure"
  elif grep -q "::error::Cannot read branch protection" "$WORK/step.log" && ! grep -q "^status=" "$WORK/step.out"; then ok "bp App: $m fails closed"
  else bad "bp App: $m wrong error"; fi
done
# github-token path reads the real status via gh api -i; it never fails here.
if run_step "$WORK/bp.read.sh" forbidden false && grep -qx "status=403" "$WORK/step.out" \
   && grep -q '^current={"message":"Resource not accessible by integration"}$' "$WORK/step.out"; then
  ok "bp GITHUB_TOKEN: 403 -> status=403, body only, no failure"; else bad "bp GITHUB_TOKEN 403"; fi
if run_step "$WORK/bp.read.sh" ok false && grep -qx "status=200" "$WORK/step.out"; then ok "bp GITHUB_TOKEN: 200 -> status=200"; else bad "bp GITHUB_TOKEN 200"; fi
if run_step "$WORK/bp.read.sh" unprotected false && grep -qx "status=404" "$WORK/step.out"; then ok "bp GITHUB_TOKEN: 404 -> status=404, no failure"; else bad "bp GITHUB_TOKEN 404"; fi
assert "bp GITHUB_TOKEN path no longer uses gh api -o/-w" "! grep -Eq -- ' -o /dev/null|-w \"' '$WORK/bp.read.sh'"

# HTTP/1.1 status line (proxies and some gh versions)
if run_step "$WORK/bp.read.sh" ok true FAKE_HTTP=1.1 && grep -qx "status=200" "$WORK/step.out"; then ok "bp App: HTTP/1.1 status line parsed"; else bad "bp App: HTTP/1.1"; fi
if run_step "$WORK/bp.read.sh" forbidden true FAKE_HTTP=1.1; then bad "bp App: HTTP/1.1 403 -> expected failure"; else ok "bp App: HTTP/1.1 403 fails closed"; fi

# App path, unprotected branch: run the audit Python and check the outputs the
# sticky comment reads.
extract "$BP" check "Audit against baseline" "$WORK/bp.audit.sh" && ok "bp audit step extractable"
run_audit() { # mode ; sets $WORK/audit.out
  run_step "$WORK/bp.read.sh" "$1" true || return 1
  local st cur
  st=$(sed -n 's/^status=//p' "$WORK/step.out"); cur=$(sed -n 's/^current=//p' "$WORK/step.out")
  : > "$WORK/audit.out"
  env -i PATH="$PATH" REPO="DarojaAI/example-repo" BRANCH=trunk STATUS="$st" CURRENT="$cur" \
    BASELINE_INPUT="$TEST_BASE" GITHUB_OUTPUT="$WORK/audit.out" bash "$WORK/bp.audit.sh" >"$WORK/audit.log" 2>&1
}
if run_audit unprotected && grep -qx "protected=false" "$WORK/audit.out" \
   && grep -qx "readable=true" "$WORK/audit.out" && grep -qx "http_status=404" "$WORK/audit.out"; then
  ok "bp App: unprotected 404 -> audit emits protected=false, readable=true, http_status=404"
else bad "bp App: unprotected audit outputs"; fi
if run_audit ok && grep -qx "readable=true" "$WORK/audit.out" && grep -qx "http_status=200" "$WORK/audit.out"; then
  ok "bp App: 200 -> audit emits readable=true, http_status=200"
else bad "bp App: 200 audit outputs"; fi
# --- audit: baseline keys ---
DEF7='enforce_admins required_linear_history allow_force_pushes allow_deletions block_creations required_conversation_resolution required_pull_request_reviews'
FULL_PROT='{"enforce_admins":{"enabled":true},"required_linear_history":{"enabled":true},"allow_force_pushes":{"enabled":false},"allow_deletions":{"enabled":false},"block_creations":{"enabled":false},"required_conversation_resolution":{"enabled":true},"required_pull_request_reviews":{"required_approving_review_count":2,"dismiss_stale_reviews":true,"require_code_owner_reviews":false,"require_last_push_approval":true}}'
NOREV_PROT='{"allow_deletions":{"enabled":false},"allow_force_pushes":{"enabled":false},"block_creations":{"enabled":false},"enforce_admins":{"enabled":true},"required_conversation_resolution":{"enabled":true},"required_linear_history":{"enabled":true}}'
NOREV_BASE='{"enforce_admins":true,"required_linear_history":true,"allow_force_pushes":false,"allow_deletions":false,"block_creations":false,"required_conversation_resolution":true}'
# audit_with <status> <current json> <baseline json>; result in $WORK/audit.out / audit.log
audit_with() {
  : > "$WORK/audit.out"
  env -i PATH="$PATH" REPO="DarojaAI/example-repo" BRANCH=trunk STATUS="$1" CURRENT="$2" \
    BASELINE_INPUT="$3" GITHUB_OUTPUT="$WORK/audit.out" bash -e "$WORK/bp.audit.sh" >"$WORK/audit.log" 2>&1
}
checks_keys() { python3 -c "
import json,sys
a=json.loads([l for l in open('$WORK/audit.out') if l.startswith('audit_json=')][0][len('audit_json='):])
print(' '.join(a.get('checks',{}).keys()))"; }

if audit_with 200 "$FULL_PROT" "$TEST_BASE" && grep -qx "protected=true" "$WORK/audit.out" && grep -qxF "missing=[]" "$WORK/audit.out" \
   && [ "$(checks_keys)" = "$DEF7" ]; then ok "audit: full baseline checks its 7 keys, fully protected"; else bad "audit: full baseline"; fi
if audit_with 200 '{"enforce_admins":{"enabled":true}}' "$TEST_BASE" && grep -qx "protected=false" "$WORK/audit.out" \
   && grep -qxF 'missing=["required_linear_history", "allow_force_pushes", "allow_deletions", "block_creations", "required_conversation_resolution", "required_pull_request_reviews"]' "$WORK/audit.out"; then
  ok "audit: sparse protection -> missing keys in baseline order"; else bad "audit: sparse protection"; fi
if audit_with 200 "${FULL_PROT/\"required_approving_review_count\":2/\"required_approving_review_count\":1}" "$TEST_BASE" && grep -qxF 'missing=["required_pull_request_reviews"]' "$WORK/audit.out"; then
  ok "audit: review count below baseline -> missing"; else bad "audit: review count below baseline"; fi
# No built-in baseline: empty input is an error on every path, never a pass.
for st in 200 403 404; do
  if audit_with "$st" "$FULL_PROT" ""; then bad "audit: empty baseline accepted (status $st)"
  elif grep -q "::error::baseline_json is required" "$WORK/audit.log" && ! grep -q "^protected=" "$WORK/audit.out"; then ok "audit: empty baseline -> ::error:: (status $st)"
  else bad "audit: empty baseline wrong failure (status $st)"; fi
done
assert "workflow declares baseline_json required with no default" "python3 -c \"
import yaml,sys
i=yaml.safe_load(open('$BP'))[True]['workflow_call']['inputs']['baseline_json']
sys.exit(0 if i['required'] is True and 'default' not in i else 1)\""
assert "workflow holds no built-in baseline" "! grep -q 'default_baseline' '$BP'"

if audit_with 200 "$NOREV_PROT" "$NOREV_BASE" && grep -qx "protected=true" "$WORK/audit.out" && grep -qxF "missing=[]" "$WORK/audit.out" \
   && [ "$(checks_keys)" = "enforce_admins required_linear_history allow_force_pushes allow_deletions block_creations required_conversation_resolution" ]; then
  ok "audit: baseline without reviews -> only its 6 keys checked, protected=true, missing=[]"; else bad "audit: baseline without reviews"; cat "$WORK/audit.log"; fi
if audit_with 200 "${NOREV_PROT/\"allow_force_pushes\":\{\"enabled\":false\}/\"allow_force_pushes\":\{\"enabled\":true\}}" "$NOREV_BASE" \
   && grep -qx "protected=false" "$WORK/audit.out" && grep -qxF 'missing=["allow_force_pushes"]' "$WORK/audit.out"; then
  ok "audit: allow_force_pushes enabled -> missing contains allow_force_pushes"; else bad "audit: force pushes enabled"; fi
# boolean keys compare against the baseline value, not a hard-coded one
if audit_with 200 "$NOREV_PROT" '{"enforce_admins":false}' && grep -qxF 'missing=["enforce_admins"]' "$WORK/audit.out" \
   && audit_with 200 '{"enforce_admins":{"enabled":false}}' '{"enforce_admins":false}' && grep -qx "protected=true" "$WORK/audit.out"; then
  ok "audit: boolean baseline value false is honoured (enforce_admins:false)"; else bad "audit: bool false baseline"; fi
if audit_with 200 "$FULL_PROT" '{"allow_force_pushes":true}' && grep -qxF 'missing=["allow_force_pushes"]' "$WORK/audit.out"; then
  ok "audit: baseline allow_force_pushes:true requires enabled"; else bad "audit: allow_force_pushes true"; fi

# reviews, when present in the baseline
RB='{"required_pull_request_reviews":{"required_approving_review_count":2,"dismiss_stale_reviews":true,"require_code_owner_reviews":false,"require_last_push_approval":true}}'
RV='"required_pull_request_reviews":{"required_approving_review_count":%s,"dismiss_stale_reviews":%s,"require_code_owner_reviews":false,"require_last_push_approval":%s}'
rv() { printf "{$RV}" "$1" "$2" "$3"; }
if audit_with 200 "$(rv 2 true true)" "$RB" && grep -qx "protected=true" "$WORK/audit.out"; then ok "audit: reviews meet baseline -> protected"; else bad "audit: reviews ok"; fi
if audit_with 200 "$(rv 3 true true)" "$RB" && grep -qx "protected=true" "$WORK/audit.out"; then ok "audit: reviews count above baseline -> protected"; else bad "audit: reviews above"; fi
if audit_with 200 "$(rv 1 true true)" "$RB" && grep -qxF 'missing=["required_pull_request_reviews"]' "$WORK/audit.out"; then ok "audit: review count below baseline -> missing"; else bad "audit: reviews low"; fi
if audit_with 200 "$(rv 2 false true)" "$RB" && grep -qxF 'missing=["required_pull_request_reviews"]' "$WORK/audit.out"; then ok "audit: dismiss_stale required by baseline -> missing when off"; else bad "audit: dismiss_stale"; fi
if audit_with 200 "$(rv 2 true false)" "$RB" && grep -qxF 'missing=["required_pull_request_reviews"]' "$WORK/audit.out"; then ok "audit: last_push_approval required by baseline -> missing when off"; else bad "audit: last_push"; fi
if audit_with 200 '{"enforce_admins":{"enabled":true}}' "$RB" && grep -qxF 'missing=["required_pull_request_reviews"]' "$WORK/audit.out"; then ok "audit: reviews in baseline but absent from protection -> missing"; else bad "audit: reviews absent"; fi

# invalid baselines are errors, not silent passes
for badbase in '{"required_pull_request_reviews":{"required_approving_review_count":-1}}' '{"required_pull_request_reviews":{"required_approving_review_count":1.5}}' '{"enforce_admin":true}' '{}' '[]' 'not json' '{"enforce_admins":"yes"}' '{"required_pull_request_reviews":{"dismiss_stale":true}}' '{"required_pull_request_reviews":{"required_approving_review_count":"1"}}'; do
  if audit_with 200 "$FULL_PROT" "$badbase"; then bad "audit: accepted bad baseline '$badbase'"
  elif grep -q "::error::" "$WORK/audit.log" && ! grep -q "^protected=" "$WORK/audit.out"; then ok "audit: bad baseline '$badbase' -> ::error:: and exit 1"
  else bad "audit: bad baseline '$badbase' wrong failure"; fi
done
# same validation even when the read was not a 200
if audit_with 404 '{"message":"Branch not protected"}' '{"typo":true}'; then bad "audit: unknown key on 404 accepted"; else ok "audit: unknown key errors on the 404 path too"; fi

# 404 path: missing is the baseline's keys only
if audit_with 404 '{"message":"Branch not protected"}' "$NOREV_BASE" && grep -qx "protected=false" "$WORK/audit.out" \
   && grep -qx "readable=true" "$WORK/audit.out" && grep -qx "http_status=404" "$WORK/audit.out" \
   && ! grep -q "^missing=.*required_pull_request_reviews" "$WORK/audit.out"; then ok "audit: 404 -> not protected, missing limited to baseline keys"; else bad "audit: 404 baseline keys"; fi

# github-token path end to end: fake 403 -> readable=false (not a failure), 200 -> status 200
tok_audit() { # mode [protection json]
  run_step "$WORK/bp.read.sh" "$1" false ${2:+FAKE_PROT="$2"} || return 1
  local st cur
  st=$(sed -n 's/^status=//p' "$WORK/step.out"); cur=$(sed -n 's/^current=//p' "$WORK/step.out")
  audit_with "$st" "$cur" "$TEST_BASE"
}
if tok_audit forbidden && grep -qx "readable=false" "$WORK/audit.out" && grep -qx "http_status=403" "$WORK/audit.out" \
   && grep -qx "protected=false" "$WORK/audit.out" && grep -qxF "missing=[]" "$WORK/audit.out"; then
  ok "github-token path: 403 -> readable=false http_status=403 (cannot read, not a failure)"; else bad "github-token 403 end to end"; fi
if tok_audit ok "$FULL_PROT" && grep -qx "http_status=200" "$WORK/audit.out" && grep -qx "readable=true" "$WORK/audit.out" && grep -qx "protected=true" "$WORK/audit.out"; then
  ok "github-token path: 200 -> http_status=200, protected"; else bad "github-token 200 end to end"; fi
if tok_audit unprotected && grep -qx "readable=true" "$WORK/audit.out" && grep -qx "protected=false" "$WORK/audit.out" && grep -qx "http_status=404" "$WORK/audit.out"; then
  ok "github-token path: 404 Branch not protected -> readable, not protected"; else bad "github-token 404 end to end"; fi

# github-token path, other statuses: cannot read, never "unprotected", no step failure
for st in 500 000 502; do
  if audit_with "$st" 'upstream error' "$TEST_BASE" && grep -qx "readable=false" "$WORK/audit.out" && grep -qx "protected=false" "$WORK/audit.out" \
     && grep -qx "http_status=$((10#$st))" "$WORK/audit.out" && grep -qxF "missing=[]" "$WORK/audit.out" && ! grep -q "NO branch protection" "$WORK/audit.out"; then
    ok "audit: status $st -> readable=false, http_status set, not reported unprotected"; else bad "audit: status $st"; fi
done
extract "$BP" check "Read current protection" "$WORK/bp.read2.sh"
mkdir -p "$WORK/bin500"; printf '#!/usr/bin/env bash\nprintf "HTTP/2.0 500 Internal Server Error\\r\\n\\r\\n{\\"message\\":\\"boom\\"}\\n"\n' > "$WORK/bin500/gh"; chmod +x "$WORK/bin500/gh"
: > "$WORK/step.out"
if env -i PATH="$WORK/bin500:$PATH" USE_APP=false REPO=a/b BRANCH=main GH_TOKEN=x GITHUB_OUTPUT="$WORK/step.out" bash -e "$WORK/bp.read2.sh" >/dev/null 2>&1 \
   && grep -qx "status=500" "$WORK/step.out"; then ok "github-token path: 500 -> status=500, step does not fail"; else bad "github-token 500 read"; fi
printf '#!/usr/bin/env bash\nexit 1\n' > "$WORK/bin500/gh"; : > "$WORK/step.out"
if env -i PATH="$WORK/bin500:$PATH" USE_APP=false REPO=a/b BRANCH=main GH_TOKEN=x GITHUB_OUTPUT="$WORK/step.out" bash -e "$WORK/bp.read2.sh" >/dev/null 2>&1 \
   && grep -qx "status=000" "$WORK/step.out"; then ok "github-token path: no response -> status=000, step does not fail"; else bad "github-token 000 read"; fi
assert "audit step removes stale output files first" "grep -q 'rm -f /tmp/audit_github_output /tmp/audit_result.json' '$WORK/bp.audit.sh'"
assert "audit step writes readable and http_status outputs" "grep -q 'readable=' '$WORK/bp.audit.sh' && grep -q 'http_status=' '$WORK/bp.audit.sh'"

if grep -q "LEAK-TOKEN-VALUE" "$WORK/all.log"; then bad "github-token / GH_TOKEN value leaked into step logs or outputs"; else ok "token value never appears in any step log or output"; fi

echo
echo "Results: $PASS passed, $FAIL failed"
[ "$FAIL" -eq 0 ]
