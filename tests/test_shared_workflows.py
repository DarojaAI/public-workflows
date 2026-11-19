"""Run the shell regression tests for the shared reusable workflows.

Each script in tests/shared/ asserts one reusable workflow's contract (inputs,
permissions, pinned actions, and the real bash of its validation and read steps
against a fake gh). They were written as standalone bash scripts and moved here with the
workflows. This module runs each
one so `python -m unittest discover -s tests` (the CI job) covers them.

Needs bash, jq, git and PyYAML (pinned in ci.yml).
"""

from __future__ import annotations

import subprocess
import unittest
from pathlib import Path

SHARED = Path(__file__).resolve().parent / "shared"
SCRIPTS = sorted(SHARED.glob("test_*.sh"))


class SharedWorkflowShellTests(unittest.TestCase):
    def test_scripts_are_present(self) -> None:
        names = {p.name for p in SCRIPTS}
        expected = {
            "test_reusable_check_branch_protection_app_token.sh",
            "test_reusable_enable_pr_automerge.sh",
            "test_reusable_terraform_stub_plan.sh",
        }
        self.assertTrue(expected <= names, f"missing: {sorted(expected - names)}")

    def test_each_script_passes(self) -> None:
        for script in SCRIPTS:
            with self.subTest(script=script.name):
                proc = subprocess.run(
                    ["bash", str(script)],
                    capture_output=True,
                    text=True,
                    timeout=300,
                    check=False,
                )
                self.assertEqual(
                    proc.returncode,
                    0,
                    f"{script.name} failed:\n{proc.stdout[-4000:]}\n{proc.stderr[-2000:]}",
                )


if __name__ == "__main__":
    unittest.main()
