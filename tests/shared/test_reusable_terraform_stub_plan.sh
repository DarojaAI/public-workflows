#!/usr/bin/env bash
# Guards reusable-terraform-stub-plan.yml's fail-closed contract: the
# "Terraform Plan (stub, no refresh)" step must not set continue-on-error, and
# must propagate terraform's own exit status, so a failed plan (for example
# "No value for required variable") fails the job. The templatefile check must
# still run after it (if: always()) so its diagnostics are reported too.
set -uo pipefail
REPO_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
WF="${WF:-${REPO_ROOT}/.github/workflows/reusable-terraform-stub-plan.yml}"
[[ -f "$WF" ]] || { echo "::error::missing $WF"; exit 1; }
python3 - "$WF" <<'PY'
import sys, yaml
d = yaml.safe_load(open(sys.argv[1]))
steps = d["jobs"]["stub-plan"]["steps"]
fail = 0
def check(ok, msg):
    global fail
    print(("  PASS " if ok else "  FAIL ") + msg)
    fail += 0 if ok else 1

plan = [s for s in steps if s.get("name") == "Terraform Plan (stub, no refresh)"]
check(len(plan) == 1, "plan step present exactly once")
if plan:
    plan = plan[0]
    check("continue-on-error" not in plan,
          "plan step does not set continue-on-error")
    check(plan.get("continue-on-error") in (None, False, "false"),
          "plan step continue-on-error is unset or false")
    check('exit "${PIPESTATUS[0]}"' in plan["run"],
          "plan step propagates terraform exit status")

check(not any(s.get("continue-on-error") for s in steps),
      "no step in stub-plan job sets continue-on-error")

tf = [s for s in steps if s.get("name") == "Check for templatefile errors"]
check(len(tf) == 1 and tf[0].get("if") == "always()",
      "templatefile check runs with if: always()")

sys.exit(1 if fail else 0)
PY
