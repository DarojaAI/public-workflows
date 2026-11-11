"""Static checks for the automatic release: .github/workflows/release.yml and
.releaserc.json. They pin what must never drift in a public repo that cuts the
tags rulesets depend on: least-privilege token, pinned actions and npm
packages, a GitHub-hosted runner, no tag deletion, and the release rules.

Run: python3 -m unittest discover -s tests -v   (needs PyYAML)
"""

from __future__ import annotations

import json
import re
import unittest
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
RELEASE = ROOT / ".github" / "workflows" / "release.yml"
RELEASERC = ROOT / ".releaserc.json"
# Written split so this file does not itself contain the private repo's name.
PRIVATE_REPO = "infra" + "-actions"


def load_release():
    return yaml.safe_load(RELEASE.read_text())


def all_steps(wf):
    return [s for job in wf["jobs"].values() for s in job.get("steps", [])]


class ReleaseWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.text = RELEASE.read_text()
        self.wf = load_release()

    def test_triggers_are_push_to_main_and_manual_only(self):
        on = self.wf[True] if True in self.wf else self.wf["on"]  # YAML 1.1 reads bare `on` as True
        self.assertEqual(set(on), {"push", "workflow_dispatch"})
        self.assertEqual(on["push"], {"branches": ["main"]})

    def test_workflow_default_is_no_permissions_and_job_gets_contents_write_only(self):
        self.assertEqual(self.wf["permissions"], {})
        for name, job in self.wf["jobs"].items():
            self.assertEqual(job.get("permissions"), {"contents": "write"}, name)

    def test_only_github_token_is_used(self):
        refs = set(re.findall(r"secrets\.([A-Za-z0-9_]+)", self.text))
        self.assertEqual(refs, {"GITHUB_TOKEN"})

    def test_runner_is_github_hosted_literal(self):
        for name, job in self.wf["jobs"].items():
            self.assertEqual(job["runs-on"], "ubuntu-latest", name)
        self.assertNotIn("vars.", self.text)

    def test_every_action_is_pinned_by_full_sha(self):
        uses = [s["uses"] for s in all_steps(self.wf) if "uses" in s]
        self.assertTrue(uses)
        for u in uses:
            self.assertRegex(u, r"^[\w.-]+/[\w./-]+@[0-9a-f]{40}$", u)

    def test_install_is_locked_with_npm_ci_and_scripts_off(self):
        runs = [s.get("run", "") for s in all_steps(self.wf)]
        installs = [r for r in runs if re.search(r"\bnpm\s+(ci|install|i)\b", r)]
        self.assertEqual(len(installs), 1, installs)
        self.assertRegex(installs[0], r"\bnpm ci\b")
        self.assertIn("--ignore-scripts", installs[0])
        for bad in ("--no-save", "--no-package-lock", "npm install", "npm i "):
            self.assertNotIn(bad, installs[0], bad)
        self.assertIsNone(re.search(r"\bnpx\b", self.text), "no npx: it can fetch unpinned code")

    def test_lockfile_pins_the_five_packages_exactly(self):
        pkg = json.loads((ROOT / "package.json").read_text())
        lock = json.loads((ROOT / "package-lock.json").read_text())
        self.assertGreaterEqual(lock["lockfileVersion"], 3)
        deps = pkg["devDependencies"]
        self.assertEqual(set(deps), {"semantic-release", "@semantic-release/commit-analyzer",
                                     "@semantic-release/release-notes-generator",
                                     "@semantic-release/github",
                                     "conventional-changelog-conventionalcommits"})
        self.assertFalse(pkg.get("dependencies"))
        for name, ver in deps.items():
            self.assertRegex(ver, r"^\d+\.\d+\.\d+$", name)
            self.assertEqual(lock["packages"][f"node_modules/{name}"]["version"], ver, name)
            self.assertEqual(lock["packages"][""]["devDependencies"][name], ver, name)
        for path, meta in lock["packages"].items():
            # Entries bundled inside another package's tarball (npm's own
            # dependencies) ship with that parent, which is checked here.
            if path and not meta.get("inBundle"):
                self.assertRegex(meta["resolved"], r"^https://registry\.npmjs\.org/", path)
                self.assertTrue(meta.get("integrity", "").startswith("sha"), path)

    def test_dry_run_guard_fails_closed(self):
        self.assertIn("no relevant changes, so no new version is released", self.text)
        self.assertRegex(self.text, r"Failing closed")

    def test_releases_cannot_race_or_be_cancelled(self):
        self.assertEqual(self.wf["concurrency"], {"group": "release", "cancel-in-progress": False})

    def test_runs_only_on_main(self):
        for job in self.wf["jobs"].values():
            self.assertIn("refs/heads/main", job["if"])

    def test_never_deletes_moves_or_force_pushes_tags(self):
        for bad in (r"git tag -d", r"git tag -f", r"--force", r"push[^\n]*--delete",
                    r"push\s+\S+\s+:", r"--tags\s+--force", r"self-heal"):
            self.assertIsNone(re.search(bad, self.text), bad)

    def test_existing_tag_fails_loudly(self):
        self.assertRegex(self.text, r"ls-remote --tags origin")
        self.assertRegex(self.text, r"already exists on origin")
        self.assertRegex(self.text, r"exit 1")

    def test_no_private_repo_reference(self):
        for path in (RELEASE, RELEASERC, ROOT / "README.md", ROOT / ".github" / "workflows" / "ci.yml"):
            self.assertNotIn(PRIVATE_REPO, path.read_text(), path.name)


class ReleaseRulesTests(unittest.TestCase):
    def setUp(self):
        self.rc = json.loads(RELEASERC.read_text())

    def plugin(self, name):
        for p in self.rc["plugins"]:
            if (p[0] if isinstance(p, list) else p) == name:
                return p[1] if isinstance(p, list) else {}
        self.fail(f"plugin {name} missing")

    def test_tag_format_and_branch(self):
        self.assertEqual(self.rc["tagFormat"], "v${version}")
        self.assertEqual(self.rc["branches"], ["main"])

    def test_explicit_plugins_only(self):
        names = [p[0] if isinstance(p, list) else p for p in self.rc["plugins"]]
        self.assertEqual(names, ["@semantic-release/commit-analyzer",
                                 "@semantic-release/release-notes-generator",
                                 "@semantic-release/github"])

    def test_release_rules(self):
        cfg = self.plugin("@semantic-release/commit-analyzer")
        self.assertEqual(cfg["preset"], "conventionalcommits")
        rules = cfg["releaseRules"]
        self.assertEqual(rules[0], {"breaking": True, "release": "major"})  # first match wins
        by_type = {r["type"]: r["release"] for r in rules if "type" in r}
        self.assertEqual({t: by_type[t] for t in ("ci", "fix", "feat")},
                         {"ci": "patch", "fix": "patch", "feat": "minor"})
        for t in ("docs", "test", "chore", "refactor", "style", "build", "perf", "revert"):
            self.assertIs(by_type[t], False, t)

    def test_github_plugin_needs_no_issue_or_pr_permissions(self):
        cfg = self.plugin("@semantic-release/github")
        for key in ("successComment", "failComment", "failTitle", "releasedLabels"):
            self.assertIs(cfg[key], False, key)


if __name__ == "__main__":
    unittest.main()
