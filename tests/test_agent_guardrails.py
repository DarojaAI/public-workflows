"""Tests for .github/workflows/agent-guardrails.yml.

They run the workflow's real `run:` body (bash plus its embedded Python)
against throwaway git repositories that stand in for pull requests, with a
fake `gh` that serves the PR's issue events. Nothing talks to GitHub.

The only change made to the script under test is the wait: WAIT_MINUTES and
POLL_SECONDS are shortened so a "no approval" case fails at once instead of
after an hour. A separate test pins the real values (60 and 30).

Run: python3 -m unittest discover -s tests -v   (needs PyYAML, git, jq, bash)
"""

from __future__ import annotations

import json
import os
import py_compile
import re
import subprocess
import tempfile
import textwrap
import unittest
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
WORKFLOW = ROOT / ".github" / "workflows" / "agent-guardrails.yml"
STEP_NAME = "G1 gate-file backstop + G2 test tampering"
HEAD_DATE = "2026-11-01T10:00:00+00:00"
BEFORE = "2026-11-01T09:00:00Z"
AFTER = "2026-11-01T11:00:00Z"
APPROVER = "patelmm79"
# Mirrors linux-desktop-seed config/agent-coding.json guardrails.reviewed_path_groups.
REVIEWED_PATH_GROUPS = [["CODEOWNERS", "*agent-guardrails*", "*claude-guardrails*", "*claude-run*", "*automerge*", "*auto-merge*", "*ready-merge*", "*agent-coding*", "*agent-gh*", "*open-agent-pr*", "*ruleset*", "*branch-protection*", "*branch_protection*", ".semgrep/", "*semgrep_gate*"], ["tests/canaries/**", "**/CODEOWNERS", "**/.semgrepignore", ".semgrep/**", "**/*agent-guardrails*/**", "**/*claude-guardrails*/**", "**/*claude-run*/**", "**/*automerge*/**", "**/*auto-merge*/**", "**/*ready-merge*/**", "**/*agent-coding*/**", "**/*agent-gh*/**", "**/*ruleset*/**", "**/*branch-protection*/**", "**/*branch_protection*/**"], ["**/*agent-guardrails*", "**/*claude-guardrails*", "**/*claude-run*", "**/*automerge*", "**/*auto-merge*", "**/*ready-merge*", "**/*agent-coding*", "**/*agent-gh*", "**/*open-agent-pr*", "**/*ruleset*", "**/*branch-protection*", "**/*branch_protection*", "**/*semgrep_gate*", "**/*semgrep_gate*/**"]]


def rules_with(groups, team_type="Team", approvals=1):
    return [{"type": "pull_request", "parameters": {"required_reviewers": [
        {"minimum_approvals": approvals, "file_patterns": g, "reviewer": {"id": 1, "type": team_type}}
        for g in groups]}}]

FAKE_GH = textwrap.dedent(
    """\
    #!/usr/bin/env python3
    # Fake `gh api`. Every call is logged to calls.jsonl (argv and stdin).
    # - issues/N/events: one JSON event per line from events.json.
    # - GET issues/N/comments: the ids in comment_ids.txt (one per line), or none.
    # - pulls/N/reviews: one JSON review per line from reviews.json, or none.
    # - rules/branches/B: the JSON array in rules.json, or [].
    # - -X POST/PATCH: recorded only; fails when comment_write_fails exists.
    import json, os, pathlib, sys
    d = pathlib.Path(os.environ["FAKE_GH_DIR"])
    args = sys.argv[1:]
    stdin = sys.stdin.read() if "--input" in args else ""
    with open(d / "calls.jsonl", "a") as f:
        f.write(json.dumps({"args": args, "stdin": stdin}) + "\\n")
    if "-X" in args:
        if (d / "comment_write_fails").exists():
            sys.exit("HTTP 403: Resource not accessible by integration")
        sys.exit(0)
    if any("/rules/branches/" in a for a in args):
        f = d / "rules.json"
        print(f.read_text() if f.exists() else "[]")
        sys.exit(0)
    if any(a.endswith("/reviews") for a in args):
        f = d / "reviews.json"
        for review in (json.loads(f.read_text()) if f.exists() else []):
            print(json.dumps(review))
        sys.exit(0)
    if any(a.endswith("/comments") for a in args):
        ids = d / "comment_ids.txt"
        if ids.exists():
            print(ids.read_text().strip())
        sys.exit(0)
    for event in json.loads((d / "events.json").read_text()):
        print(json.dumps(event))
    """
)


def load_workflow() -> dict:
    return yaml.safe_load(WORKFLOW.read_text())


def guardrail_step() -> dict:
    steps = load_workflow()["jobs"]["agent-guardrails"]["steps"]
    return next(s for s in steps if s.get("name") == STEP_NAME)


def embedded_python(run: str) -> str:
    match = re.search(r"<<'PY'\n(.*?)\nPY\s*$", run, re.S)
    assert match, "embedded python heredoc not found"
    return match.group(1)


def label(name: str, actor: str, when: str) -> dict:
    return {"event": "labeled", "label": {"name": name}, "actor": {"login": actor}, "created_at": when}


class Fixture:
    """A git repo with a base commit and a head commit (the PR)."""

    def __init__(self, tmp: Path):
        self.repo = tmp / "repo"
        self.repo.mkdir()
        self.env = {
            **os.environ,
            "GIT_AUTHOR_NAME": "t",
            "GIT_AUTHOR_EMAIL": "t@example.invalid",
            "GIT_COMMITTER_NAME": "t",
            "GIT_COMMITTER_EMAIL": "t@example.invalid",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_NOSYSTEM": "1",
        }
        self.git("init", "-q", "-b", "main")
        self.write("README.md", "hello\n")
        self.write("src/app.py", "x = 1\n")
        self.write("tests/test_app.py", "def test_x():\n    assert True\n")
        self.write("tests/flow.bats", "@test 'a' {\n  true\n}\n")
        self.base = self.commit("base", "2026-10-31T10:00:00+00:00")

    def git(self, *args: str, date: str | None = None) -> str:
        env = dict(self.env)
        if date:
            env["GIT_AUTHOR_DATE"] = env["GIT_COMMITTER_DATE"] = date
        return subprocess.run(
            ["git", *args], cwd=self.repo, env=env, check=True, capture_output=True, text=True
        ).stdout.strip()

    def write(self, path: str, text: str) -> None:
        p = self.repo / path
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)

    def delete(self, path: str) -> None:
        (self.repo / path).unlink()

    def commit(self, msg: str, date: str) -> str:
        self.git("add", "-A")
        self.git("commit", "-q", "--allow-empty", "-m", msg, date=date)
        return self.git("rev-parse", "HEAD")


class GuardrailScriptTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.fx = Fixture(self.tmp)
        self.gh_dir = self.tmp / "gh"
        self.gh_dir.mkdir()
        bindir = self.tmp / "bin"
        bindir.mkdir()
        gh = bindir / "gh"
        gh.write_text(FAKE_GH)
        gh.chmod(0o755)
        self.path = f"{bindir}{os.pathsep}{os.environ['PATH']}"
        self.step = guardrail_step()

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def run_check(self, *, head_ref="agent/fix-1", author="someone", events=(), reviews=(),
                  fx=None) -> subprocess.CompletedProcess:
        fx = fx or self.fx
        head = fx.commit("head", HEAD_DATE)
        (self.gh_dir / "events.json").write_text(json.dumps(list(events)))
        (self.gh_dir / "reviews.json").write_text(json.dumps(
            [dict(r, commit_id=head) if r.get("commit_id") == "HEAD" else r for r in reviews]))
        run = self.step["run"]
        env = {
            **fx.env,
            "PATH": self.path,
            "FAKE_GH_DIR": str(self.gh_dir),
            "GH_TOKEN": "unused",
            "REPO": "example/repo",
            "PR_NUMBER": "1",
            "HEAD_REF": head_ref,
            "PR_AUTHOR": author,
            "BASE_SHA": fx.base,
            "BASE_REF": "main",
            "HEAD_SHA": head,
            "APPROVERS": self.step["env"]["APPROVERS"],
            "RUN_URL": "https://github.com/example/repo/actions/runs/42",
            "GITHUB_STEP_SUMMARY": str(self.gh_dir / "summary.md"),
        }
        return subprocess.run(["bash", "-c", run], cwd=fx.repo, env=env,
                              capture_output=True, text=True, timeout=120)

    def _subdir(self, path: str) -> Path:
        d = self.tmp / ("fx_" + path.replace("/", "_"))
        d.mkdir()
        return d

    def assertPass(self, r, needle="agent-guardrails: PASS"):
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn(needle, r.stdout)

    def assertFail(self, r, needle):
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn(needle, r.stdout)
        self.assertIn("agent-guardrails: FAIL", r.stdout)

    # non-agent PRs
    def test_non_agent_pr_passes_even_with_a_deleted_test(self):
        self.fx.delete("tests/test_app.py")
        r = self.run_check(head_ref="feature/x", author="a-human")
        self.assertPass(r, "not an agent PR")

    # agent PRs
    def test_agent_branch_without_findings_passes(self):
        self.fx.write("src/app.py", "x = 2\n")
        self.assertPass(self.run_check(), "PASS (no uncovered gate files")

    def test_agent_app_author_on_any_branch_is_checked(self):
        self.fx.delete("tests/test_app.py")
        r = self.run_check(head_ref="feature/x", author="darojaai-agents[bot]")
        self.assertFail(r, "G2: deleted test file: tests/test_app.py")

    TIER1 = ["CODEOWNERS", ".github/CODEOWNERS", "docs/CODEOWNERS",
                 ".github/workflows/agent-guardrails.yml", "config/agent-coding/agent-guardrails.yml",
                 ".github/workflows/claude-run.yml", ".claude-run/x.sh", ".github/workflows/enable-automerge.yml",
                 ".github/workflows/reusable-auto-merge.yml", "config/agent-coding.json", "agent-coding.md",
                 ".github/workflows/agent-coding-setup.yml",
                 "scripts/maintenance/agent-coding-org-setup.sh",
                 "scripts/agent-coding/container/gh", "scripts/agent-coding/agent-gh-mint.sh",
                 ".github/rulesets/main.json", "tests/canaries/c.py", "tests/canaries/deep/d.py",
                 ".github/workflows/ready-merge.yml", "src/automerge/x.py", "src/branch-protection/x.py",
                 ".github/workflows/install-agent-gh-minter.yml",
                 "etc/systemd/system/agent-gh-mint.timer",
                 ".github/workflows/reusable-claude-guardrails.yml",
                 "tool/check/claude-guardrails/scripts/guardrails.py",
                 ".github/workflows/apply-branch-protection.yml",
                 "scripts/sync_branch_protection.py", "scripts/apply-rulesets.sh",
                 ".semgrep/baseline.json", ".semgrep/rules/x.yml", ".semgrepignore", "api/.semgrepignore",
                 "scripts/semgrep_gate.py", "tests/test_semgrep_gate.py",
                 ".github/workflows/open-agent-pr.yml"]

    def test_gate_files_covered_by_live_required_reviewers_defer_to_github(self):
        (self.gh_dir / "rules.json").write_text(json.dumps(rules_with(REVIEWED_PATH_GROUPS)))
        for path in self.TIER1:
            with self.subTest(path=path):
                fx = Fixture(self._subdir(path))
                fx.write(path, "x\n")
                r = self.run_check(fx=fx)
                self.assertPass(r, "PASS (no uncovered gate files")
                self.assertIn(f"{path}: approval required natively", r.stdout)

    def test_gate_files_fail_g1_when_no_live_rule_covers_them(self):
        cases = {"no rules": [], "no reviewers": [{"type": "pull_request", "parameters": {}}],
                 "zero approvals": rules_with(REVIEWED_PATH_GROUPS, approvals=0),
                 "not a team": rules_with(REVIEWED_PATH_GROUPS, team_type="User"),
                 "other paths only": rules_with([["docs/**"]])}
        for name, rules in cases.items():
            with self.subTest(name):
                self.tearDown(); self.setUp()
                (self.gh_dir / "rules.json").write_text(json.dumps(rules))
                self.fx.write("config/agent-coding.json", "{}\n")
                self.assertFail(self.run_check(),
                                "G1: gate file not covered by the ruleset's required reviewers: config/agent-coding.json")

    def test_uncovered_g1_is_cleared_by_an_approving_review(self):
        self.fx.write("CODEOWNERS", "* @x\n")
        r = self.run_check(reviews=[self.review()])
        self.assertPass(r, "PASS (approved by an approver")

    def test_non_ascii_paths_are_still_matched(self):
        self.fx.write("tests/canaries/é.txt", "x\n")
        self.assertFail(self.run_check(), "G1: gate file not covered by the ruleset's required reviewers: tests/canaries/é.txt")
        self.tearDown(); self.setUp()
        (self.gh_dir / "rules.json").write_text(json.dumps(rules_with(REVIEWED_PATH_GROUPS)))
        self.fx.write("tests/canaries/é.txt", "x\n")
        self.assertPass(self.run_check(), "PASS (no uncovered gate files")
        self.tearDown(); self.setUp()
        self.fx.delete("tests/test_app.py"); self.fx.write("tests/é_test.py", "def test_a():\n    pass\n")
        self.fx.git("add", "-A"); self.fx.base = self.fx.commit("b3", "2026-10-31T12:00:00+00:00")
        self.fx.delete("tests/é_test.py")
        self.assertFail(self.run_check(), "G2: deleted test file: tests/é_test.py")

    def test_ordinary_files_are_not_gate_files(self):
        for path in [".github/workflows/ci.yml", ".github/dependabot.yml", ".pre-commit-config.yaml",
                     ".coveragerc", "codecov.yml", ".github/workflows/semgrep.yml", "src/app2.py"]:
            with self.subTest(path=path):
                fx = Fixture(self._subdir(path))
                fx.write(path, "x\n")
                self.assertPass(self.run_check(fx=fx), "PASS (no uncovered gate files")

    def test_g2_without_approval_fails_at_once(self):
        self.fx.delete("tests/test_app.py")
        r = self.run_check()
        self.assertFail(r, "G2: deleted test file: tests/test_app.py")
        self.assertIn("::error title=agent-guardrails::Waiting for approval from @patelmm79", r.stdout)

    def review(self, who=APPROVER, state="APPROVED", commit="HEAD", at=AFTER):
        return {"user": {"login": who}, "state": state, "commit_id": commit, "submitted_at": at}

    def test_approving_review_on_the_head_commit_passes(self):
        self.fx.delete("tests/test_app.py")
        self.assertPass(self.run_check(reviews=[self.review()]), "PASS (approved by an approver")

    def test_approving_review_on_an_older_commit_fails(self):
        self.fx.delete("tests/test_app.py")
        r = self.run_check(reviews=[self.review(commit="0" * 40)])
        self.assertFail(r, "G2")

    def test_review_from_a_non_approver_does_not_count(self):
        self.fx.delete("tests/test_app.py")
        self.assertFail(self.run_check(reviews=[self.review(who="random-user")]), "G2")

    def test_a_later_changes_requested_or_dismissal_cancels_the_approval(self):
        for state in ("CHANGES_REQUESTED", "DISMISSED"):
            with self.subTest(state=state):
                self.tearDown(); self.setUp()
                self.fx.delete("tests/test_app.py")
                r = self.run_check(reviews=[self.review(at=BEFORE),
                                            self.review(state=state, at=AFTER)])
                self.assertFail(r, "G2")

    def test_a_comment_review_does_not_cancel_the_approval(self):
        self.fx.delete("tests/test_app.py")
        r = self.run_check(reviews=[self.review(at=BEFORE), self.review(state="COMMENTED", at=AFTER)])
        self.assertPass(r, "PASS (approved by an approver")

    def test_gate_approved_label_after_latest_commit_still_works(self):
        self.fx.delete("tests/test_app.py")
        r = self.run_check(events=[label("gate-approved", APPROVER, AFTER)])
        self.assertPass(r, "PASS (approved by an approver")

    def test_label_before_latest_commit_or_from_non_approver_fails(self):
        for ev in (label("gate-approved", APPROVER, BEFORE), label("gate-approved", "random-user", AFTER),
                   label("lgtm", APPROVER, AFTER)):
            with self.subTest(ev=ev):
                self.tearDown(); self.setUp()
                self.fx.delete("tests/test_app.py")
                self.assertFail(self.run_check(events=[ev]), "G2")

    # status comment and summary
    def calls(self):
        f = self.gh_dir / "calls.jsonl"
        return [json.loads(l) for l in f.read_text().splitlines()] if f.exists() else []

    def writes(self):
        return [c for c in self.calls() if "-X" in c["args"]]

    def test_waiting_posts_one_comment_with_files_and_rerun_link(self):
        self.fx.delete("tests/test_app.py")
        self.assertFail(self.run_check(), "G2")
        w = self.writes()
        self.assertEqual(len(w), 1, w)
        self.assertIn("POST", w[0]["args"])
        self.assertTrue(w[0]["args"][-3].endswith("repos/example/repo/issues/1/comments"), w[0]["args"])
        body = json.loads(w[0]["stdin"])["body"]
        self.assertTrue(body.startswith("<!-- agent-guardrails -->"))
        self.assertIn("waiting for approval from @patelmm79", body)
        self.assertIn("```text\nG2: deleted test file: tests/test_app.py\n```", body)
        self.assertIn("(https://github.com/example/repo/actions/runs/42)", body)
        self.assertIn("waiting for approval", (self.gh_dir / "summary.md").read_text())

    def test_existing_comment_is_updated_not_duplicated(self):
        (self.gh_dir / "comment_ids.txt").write_text("777\n")
        self.fx.delete("tests/test_app.py")
        self.assertFail(self.run_check(), "G2")
        w = self.writes()
        self.assertEqual(len(w), 1, w)
        self.assertIn("PATCH", w[0]["args"])
        self.assertIn("repos/example/repo/issues/comments/777", w[0]["args"])

    def test_clean_pr_creates_no_comment(self):
        self.fx.write("src/app.py", "x = 2\n")
        self.assertPass(self.run_check(), "PASS (no uncovered gate files")
        self.assertEqual(self.writes(), [])

    def test_clean_pr_updates_an_earlier_waiting_comment_to_passed(self):
        (self.gh_dir / "comment_ids.txt").write_text("777\n")
        self.fx.write("src/app.py", "x = 2\n")
        self.assertPass(self.run_check(), "PASS (no uncovered gate files")
        w = self.writes()
        self.assertEqual(len(w), 1, w)
        self.assertIn("agent-guardrails: passed", json.loads(w[0]["stdin"])["body"])

    def test_approved_updates_the_comment(self):
        (self.gh_dir / "comment_ids.txt").write_text("777\n")
        self.fx.delete("tests/test_app.py")
        self.assertPass(self.run_check(events=[label("gate-approved", APPROVER, AFTER)]), "PASS (approved by an approver")
        self.assertIn("agent-guardrails: approved", json.loads(self.writes()[0]["stdin"])["body"])

    def test_a_comment_failure_never_changes_the_verdict(self):
        (self.gh_dir / "comment_write_fails").write_text("")
        self.fx.delete("tests/test_app.py")
        r = self.run_check()
        self.assertFail(r, "G2")
        self.assertIn("couldn't update the PR comment", r.stdout)
        self.fx.write("src/app.py", "x = 3\n")
        (self.gh_dir / "comment_ids.txt").write_text("777\n")
        self.assertPass(self.run_check(events=[label("gate-approved", APPROVER, "2026-11-01T12:00:00Z")]),
                        "PASS (approved by an approver")

    def test_comment_cannot_be_injected_through_a_path(self):
        # A path with backticks would close a single-backtick span; the fence must outgrow it.
        self.fx.write("tests/x```` @someone [a](https://evil.example)_test.py",
                      "import pytest\n\n@pytest.mark.skip\ndef test_y():\n    pass\n")
        self.assertFail(self.run_check(), "G2")
        body = json.loads(self.writes()[0]["stdin"])["body"]
        self.assertIn("`````text\n", body)
        self.assertEqual(body.count("`````"), 2, body)

    def test_comment_never_shows_the_added_line_text(self):
        self.fx.write("tests/test_app.py",
                      "import pytest\n\n@pytest.mark.skip  # @someone https://evil.example\n"
                      "def test_x():\n    assert True\n")
        r = self.run_check()
        self.assertFail(r, "evil.example")  # still in the job log
        body = json.loads(self.writes()[0]["stdin"])["body"]
        self.assertIn("added pytest skip/xfail marker (the line is in the job log)", body)
        self.assertNotIn("evil.example", body)
        self.assertNotIn("evil.example", (self.gh_dir / "summary.md").read_text())

    def test_only_the_bots_own_comment_is_edited(self):
        self.fx.delete("tests/test_app.py")
        self.assertFail(self.run_check(), "G2")
        get = [c for c in self.calls() if "repos/example/repo/issues/1/comments" in c["args"] and "--jq" in c["args"]]
        self.assertEqual(len(get), 1, self.calls())
        self.assertIn('.user.login == "github-actions[bot]"', " ".join(get[0]["args"]))

    def test_a_summary_failure_never_fails_an_approved_pr(self):
        (self.gh_dir / "summary.md").mkdir()  # writing to a directory raises
        self.fx.delete("tests/test_app.py")
        r = self.run_check(events=[label("gate-approved", APPROVER, AFTER)])
        self.assertPass(r, "PASS (approved by an approver")
        self.assertIn("couldn't write the job summary", r.stdout)

    def test_gh_calls_have_a_timeout(self):
        self.assertIn("timeout=30", embedded_python(self.step["run"]))

    def test_deleted_test_file_fails(self):
        self.fx.delete("tests/test_app.py")
        self.assertFail(self.run_check(), "G2: deleted test file: tests/test_app.py")

    def test_added_pytest_skip_fails(self):
        self.fx.write("tests/test_app.py",
                      "import pytest\n\n@pytest.mark.skip\ndef test_x():\n    assert True\n")
        self.assertFail(self.run_check(), "G2: tests/test_app.py: added pytest skip/xfail marker")

    def test_added_bats_skip_fails(self):
        self.fx.write("tests/flow.bats", "@test 'a' {\n  skip\n  true\n}\n")
        self.assertFail(self.run_check(), "G2: tests/flow.bats: added bats skip")

    def test_added_or_true_in_a_test_fails(self):
        self.fx.write("tests/flow.bats", "@test 'a' {\n  false || true\n}\n")
        self.assertFail(self.run_check(), "G2: tests/flow.bats: added `|| true`")

    def test_skip_pattern_outside_tests_is_not_g2(self):
        self.fx.write("src/app.py", "x = 1 or True  # || true\n")
        self.assertPass(self.run_check(), "PASS (no uncovered gate files")

    def test_approved_g2_finding_passes(self):
        self.fx.delete("tests/test_app.py")
        r = self.run_check(events=[label("gate-approved", APPROVER, AFTER)])
        self.assertIn("G2: deleted test file: tests/test_app.py", r.stdout)
        self.assertPass(r, "PASS (approved by an approver")


class WorkflowStaticTest(unittest.TestCase):
    def test_bash_body_parses(self):
        subprocess.run(["bash", "-n", "-c", guardrail_step()["run"]], check=True)

    def test_embedded_python_compiles(self):
        with tempfile.TemporaryDirectory() as d:
            src = Path(d) / "guardrail.py"
            src.write_text(embedded_python(guardrail_step()["run"]))
            py_compile.compile(str(src), doraise=True)

    def test_never_waits_and_times_out_fast(self):
        body = embedded_python(guardrail_step()["run"])
        self.assertNotIn("time.sleep", body)
        self.assertNotIn("WAIT_MINUTES", body)
        job = load_workflow()["jobs"]["agent-guardrails"]
        self.assertLessEqual(job["timeout-minutes"], 5)

    def test_permissions_allow_only_the_status_comment(self):
        self.assertEqual(load_workflow()["permissions"],
                         {"contents": "read", "pull-requests": "read", "issues": "write"})

    def test_approvers_are_rendered_github_logins(self):
        approvers = guardrail_step()["env"]["APPROVERS"]
        self.assertNotIn("@@", approvers)
        for login in approvers.split(","):
            self.assertRegex(login, r"^[A-Za-z0-9]([A-Za-z0-9-]{0,37}[A-Za-z0-9])?$")

    def test_check_name_and_trigger(self):
        wf = load_workflow()
        on = wf.get(True, wf.get("on"))
        self.assertIn("pull_request", on)
        self.assertEqual(wf["jobs"]["agent-guardrails"]["name"], "agent-guardrails")
        self.assertNotIn("concurrency", wf)  # ruleset workflows must not cancel-in-progress


if __name__ == "__main__":
    unittest.main()
