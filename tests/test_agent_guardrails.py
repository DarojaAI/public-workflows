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
STEP_NAME = "G1 gate files + G2 test tampering"
HEAD_DATE = "2026-11-01T10:00:00+00:00"
BEFORE = "2026-11-01T09:00:00Z"
AFTER = "2026-11-01T11:00:00Z"
APPROVER = "patelmm79"

FAKE_GH = textwrap.dedent(
    """\
    #!/usr/bin/env python3
    # Fake `gh api ... issues/N/events --jq '.[]'`: one JSON event per line.
    # The first call serves events.json; later calls serve events_later.json
    # when it exists (an approval that arrives while the check waits).
    import json, os, pathlib
    d = pathlib.Path(os.environ["FAKE_GH_DIR"])
    count = d / "count"
    n = int(count.read_text()) if count.exists() else 0
    count.write_text(str(n + 1))
    src = d / "events_later.json"
    if n == 0 or not src.exists():
        src = d / "events.json"
    for event in json.loads(src.read_text()):
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

    def run_check(self, *, head_ref="agent/fix-1", author="someone", events=(), later_events=None,
                  wait_minutes="0", poll_seconds="0", fx=None) -> subprocess.CompletedProcess:
        fx = fx or self.fx
        head = fx.commit("head", HEAD_DATE)
        (self.gh_dir / "events.json").write_text(json.dumps(list(events)))
        if later_events is not None:
            (self.gh_dir / "events_later.json").write_text(json.dumps(list(later_events)))
        run = self.step["run"]
        run = run.replace("WAIT_MINUTES = 60", f"WAIT_MINUTES = {wait_minutes}", 1)
        run = run.replace("POLL_SECONDS = 30", f"POLL_SECONDS = {poll_seconds}", 1)
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
            "HEAD_SHA": head,
            "APPROVERS": self.step["env"]["APPROVERS"],
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
    def test_non_agent_pr_passes_even_with_gate_file(self):
        self.fx.write(".github/workflows/ci.yml", "on: push\n")
        r = self.run_check(head_ref="feature/x", author="a-human")
        self.assertPass(r, "not an agent PR")

    # agent PRs
    def test_agent_branch_without_findings_passes(self):
        self.fx.write("src/app.py", "x = 2\n")
        self.assertPass(self.run_check(), "PASS (no gate files")

    def test_agent_app_author_on_any_branch_is_checked(self):
        self.fx.write(".github/CODEOWNERS", "* @x\n")
        r = self.run_check(head_ref="feature/x", author="darojaai-agents[bot]")
        self.assertFail(r, "G1: gate file touched: .github/CODEOWNERS")

    def test_gate_file_without_label_fails_after_the_wait(self):
        self.fx.write("CODEOWNERS", "* @someone\n")
        r = self.run_check()
        self.assertFail(r, "G1: gate file touched: CODEOWNERS")
        self.assertIn("waiting up to 0 min", r.stdout)

    def test_gate_file_approved_after_latest_commit_passes(self):
        self.fx.write(".github/workflows/enable-automerge.yml", "on: push\n")
        r = self.run_check(events=[label("gate-approved", APPROVER, AFTER)])
        self.assertPass(r, "PASS (gate-approved by an approver after the latest commit)")

    def test_gate_file_approved_before_latest_commit_fails(self):
        self.fx.write(".github/workflows/agent-guardrails.yml", "on: push\n")
        r = self.run_check(events=[label("gate-approved", APPROVER, BEFORE)])
        self.assertFail(r, "G1: gate file touched: .github/workflows/agent-guardrails.yml")

    # G1 is tier 1 only
    def test_tier1_files_are_gated(self):
        for path in ["CODEOWNERS", ".github/CODEOWNERS", "docs/CODEOWNERS",
                     ".github/workflows/agent-guardrails.yml", "config/agent-coding/agent-guardrails.yml",
                     ".github/workflows/claude-run.yml", ".github/workflows/enable-automerge.yml",
                     ".github/workflows/reusable-auto-merge.yml", "config/agent-coding.json",
                     ".github/workflows/agent-coding-setup.yml",
                     "scripts/maintenance/agent-coding-org-setup.sh",
                     ".github/rulesets/main.json", "tests/canaries/c.py",
                     ".github/workflows/ready-merge.yml",
                     ".github/workflows/install-agent-gh-minter.yml",
                     "etc/systemd/system/agent-gh-mint.timer",
                     ".github/workflows/reusable-claude-guardrails.yml",
                     "tool/check/claude-guardrails/scripts/guardrails.py",
                     ".github/workflows/apply-branch-protection.yml",
                     "scripts/sync_branch_protection.py", "scripts/apply-rulesets.sh"]:
            with self.subTest(path=path):
                fx = Fixture(self._subdir(path))
                fx.write(path, "x\n")
                r = self.run_check(fx=fx)
                self.assertFail(r, f"G1: gate file touched: {path}")

    def test_ordinary_workflow_and_config_changes_are_not_gated(self):
        for path in [".github/workflows/ci.yml", ".github/dependabot.yml",
                     ".pre-commit-config.yaml", ".coveragerc", "codecov.yml",
                     ".github/workflows/pre-merge-check.yml", ".semgrep/baseline.json"]:
            with self.subTest(path=path):
                fx = Fixture(self._subdir(path))
                fx.write(path, "x\n")
                self.assertPass(self.run_check(fx=fx), "PASS (no gate files")

    def test_label_from_a_non_approver_does_not_count(self):
        self.fx.write("CODEOWNERS", "* @x\n")
        r = self.run_check(events=[label("gate-approved", "random-user", AFTER)])
        self.assertFail(r, "G1: gate file touched: CODEOWNERS")

    def test_another_label_does_not_count(self):
        self.fx.write("CODEOWNERS", "* @x\n")
        r = self.run_check(events=[label("lgtm", APPROVER, AFTER)])
        self.assertFail(r, "G1")

    def test_approval_arriving_during_the_wait_passes(self):
        self.fx.write("CODEOWNERS", "* @x\n")
        r = self.run_check(events=[], later_events=[label("gate-approved", APPROVER, AFTER)],
                           wait_minutes="0.05", poll_seconds="0")
        self.assertPass(r, "PASS (gate-approved by an approver after the latest commit)")
        self.assertIn("waiting up to 0.05 min", r.stdout)

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
        self.assertPass(self.run_check(), "PASS (no gate files")

    def test_approved_g2_finding_passes(self):
        self.fx.delete("tests/test_app.py")
        r = self.run_check(events=[label("gate-approved", APPROVER, AFTER)])
        self.assertIn("G2: deleted test file: tests/test_app.py", r.stdout)
        self.assertPass(r, "PASS (gate-approved")


class WorkflowStaticTest(unittest.TestCase):
    def test_bash_body_parses(self):
        subprocess.run(["bash", "-n", "-c", guardrail_step()["run"]], check=True)

    def test_embedded_python_compiles(self):
        with tempfile.TemporaryDirectory() as d:
            src = Path(d) / "guardrail.py"
            src.write_text(embedded_python(guardrail_step()["run"]))
            py_compile.compile(str(src), doraise=True)

    def test_wait_is_sixty_minutes_polling_every_thirty_seconds(self):
        body = embedded_python(guardrail_step()["run"])
        self.assertRegex(body, r"(?m)^WAIT_MINUTES = 60$")
        self.assertRegex(body, r"(?m)^POLL_SECONDS = 30$")
        self.assertEqual(load_workflow()["jobs"]["agent-guardrails"]["timeout-minutes"], 70)

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
