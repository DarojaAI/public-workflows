"""The semantic-release install must be an exact, compatible set, and `type!:` must cut a major.

Background: commit-analyzer ^11 cannot read conventional-changelog-conventionalcommits
^9 (the preset exports `parser`, the analyzer reads `parserOpts`), so a `feat!:`
header silently produces no release. These tests install the package set that
the reusable workflow really uses (parsed from the YAML, not copied here), run
the real commit-analyzer and release-notes-generator with this repo's
`.releaserc.json`, and check the bump for sample commits.

To see them fail on other ranges, point SEMANTIC_RELEASE_WORKFLOW at a copy of
the workflow with the old ranges.

Needs node/npm and network access (npm registry). Skipped without npm unless CI is set.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[3]
WORKFLOWS = ROOT / ".github" / "workflows"
REUSABLE = Path(
    os.environ.get(
        "SEMANTIC_RELEASE_WORKFLOW", WORKFLOWS / "reusable-semantic-release.yml"
    )
)
RELEASERC = ROOT / ".releaserc.json"
ANALYZER = Path(__file__).with_name("semantic_release_analyze.mjs")

HAVE_NPM = shutil.which("npm") is not None and shutil.which("node") is not None
needs_npm = pytest.mark.skipif(
    not HAVE_NPM and not os.environ.get("CI"), reason="node/npm not available"
)

COMMITS = {
    "fix: a bug": "patch",
    "feat: a feature": "minor",
    "feat!: drop the old input": "major",
    "fix!: change a default": "major",
    "feat(scope)!: scoped breaking": "major",
    "feat: new thing\n\nBREAKING CHANGE: the old thing is gone": "major",
    "fix: tweak\n\nBREAKING CHANGE: behaviour changed": "major",
    "docs: update readme": None,
    "chore: tidy": None,
    "Merge pull request #12 from example-org/some-branch": None,
}


def step(workflow: Path, job: str, name_prefix: str) -> dict:
    steps = yaml.safe_load(workflow.read_text())["jobs"][job]["steps"]
    return next(s for s in steps if s.get("name", "").startswith(name_prefix))


def install_command(workflow: Path, job: str = "release") -> list[str]:
    """Tokens of the `npm install ...` command, with the extra-packages expression dropped."""
    script = step(workflow, job, "Install semantic-release")["run"]
    script = re.sub(r"^\s*#.*$", "", script, flags=re.MULTILINE)
    script = re.sub(r"\\\n", " ", script)
    line = next(ln for ln in script.splitlines() if "npm install" in ln)
    line = re.sub(r"\$\{\{.*?\}\}", "", line)
    line = re.sub(r"\$\{EXTRA\[@\]\S*", "", line)
    return line.split()


def packages(tokens: list[str]) -> dict[str, str]:
    specs = [t for t in tokens[2:] if not t.startswith("-")]
    return {s.rsplit("@", 1)[0]: s.rsplit("@", 1)[1] for s in specs}


_installs: dict[tuple, Path] = {}


@pytest.fixture(scope="module")
def installed(tmp_path_factory) -> Path:
    """Install the reusable's exact package set into a scratch dir."""
    tokens = install_command(REUSABLE)
    key = tuple(tokens)
    if key not in _installs:
        d = tmp_path_factory.mktemp("sr-install")
        (d / "package.json").write_text("{}")
        flags = [t for t in tokens[2:] if t.startswith("-")]
        specs = [t for t in tokens[2:] if not t.startswith("-")]
        subprocess.run(
            ["npm", "install", "--no-audit", "--no-fund", *flags, *specs],
            cwd=d,
            check=True,
            capture_output=True,
            text=True,
            timeout=280,
        )
        _installs[key] = d
    return _installs[key]


def analyze(installed: Path, messages: list[str]) -> list[dict]:
    proc = subprocess.run(
        ["node", str(ANALYZER), str(installed), str(RELEASERC)],
        input=json.dumps(messages),
        check=True,
        capture_output=True,
        text=True,
    )
    return json.loads(proc.stdout)


# --- static checks (no network) ------------------------------------------------


def test_every_package_is_an_exact_pin():
    tokens = install_command(REUSABLE)
    pkgs = packages(tokens)
    assert {
        "semantic-release",
        "@semantic-release/commit-analyzer",
        "@semantic-release/release-notes-generator",
        "@semantic-release/github",
        "conventional-changelog-conventionalcommits",
    } <= set(pkgs)
    for name, version in pkgs.items():
        assert re.fullmatch(
            r"\d+\.\d+\.\d+", version
        ), f"{name}@{version} is not an exact version"
    assert "--ignore-scripts" in tokens
    assert "--no-save" in tokens


def test_root_package_json_matches_the_pinned_set():
    """The reusable's exact semantic-release and plugin versions equal this repo's own."""
    dev = json.loads((ROOT / "package.json").read_text())["devDependencies"]
    pinned = packages(install_command(REUSABLE))
    assert dev, "package.json has no devDependencies"
    # The reusable also installs @semantic-release/changelog, which this repo's own
    # release does not use, so compare every package.json entry against the reusable.
    assert {name: pinned.get(name) for name in dev} == dev


def test_extra_packages_input_is_still_appended():
    st = step(REUSABLE, "release", "Install semantic-release")
    # Passed through env (never interpolated into the script) and appended to npm install.
    assert st["env"]["EXTRA_PACKAGES"] == "${{ inputs.extra-packages }}"
    assert "${{ inputs.extra-packages }}" not in st["run"]
    assert '${EXTRA[@]+"${EXTRA[@]}"}' in st["run"]


def test_repo_releaserc_sets_the_conventionalcommits_preset():
    rc = json.loads(RELEASERC.read_text())
    cfg = {p[0]: p[1] for p in rc["plugins"] if isinstance(p, list)}
    assert cfg["@semantic-release/commit-analyzer"]["preset"] == "conventionalcommits"
    assert (
        cfg["@semantic-release/release-notes-generator"]["preset"]
        == "conventionalcommits"
    )


# --- real analyzer on the real install ----------------------------------------


@needs_npm
@pytest.mark.parametrize(
    "message,expected", COMMITS.items(), ids=lambda v: str(v).splitlines()[0]
)
def test_commit_analyzer_bump(installed, message, expected):
    (row,) = analyze(installed, [message])
    assert row["release"] == expected


@needs_npm
def test_release_notes_generator_reads_the_preset(installed):
    rows = analyze(installed, ["feat!: drop the old input", "fix: a bug"])
    for row in rows:
        assert row["notesError"] is None
    assert "BREAKING CHANGES" in rows[0]["notes"]


@needs_npm
def test_initial_version_patch_still_applies(installed, tmp_path):
    """Run the workflow's own sed against the installed semantic-release."""
    run = step(REUSABLE, "release", "Patch FIRST_RELEASE")["run"]
    script = run.replace("${{ inputs.initial-version }}", "0.1.0")
    work = tmp_path / "work"
    shutil.copytree(installed / "node_modules", work / "node_modules", symlinks=True)
    subprocess.run(
        ["bash", "-e", "-c", script],
        cwd=work,
        check=True,
        capture_output=True,
        text=True,
    )
    constants = (
        work / "node_modules/semantic-release/lib/definitions/constants.js"
    ).read_text()
    assert 'export const FIRST_RELEASE = "0.1.0";' in constants


BASE_GUARD_CASES = [
    ("@semantic-release/commit-analyzer@^11.0.0", 1),
    ("semantic-release@23.1.1", 1),
    ("conventional-changelog-conventionalcommits", 1),
    ("@semantic-release/git@10.0.1 @semantic-release/github@9.2.6", 1),
    ("@semantic-release/git@10.0.1", 0),
    ("", 0),
]


@pytest.mark.parametrize("extra,expected_rc", BASE_GUARD_CASES)
def test_extra_packages_cannot_override_the_base_set(
    tmp_path: Path, extra: str, expected_rc: int
) -> None:
    """The reusable's own guard runs before npm; npm is stubbed so nothing installs."""
    script = step(REUSABLE, "release", "Install semantic-release")["run"]
    bindir = tmp_path / "bin"
    bindir.mkdir()
    (bindir / "npm").write_text("#!/bin/bash\nexit 0\n")
    (bindir / "npm").chmod(0o755)
    proc = subprocess.run(
        ["bash", "-c", script],
        env={"PATH": f"{bindir}:{os.environ['PATH']}", "EXTRA_PACKAGES": extra},
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == expected_rc, proc.stdout + proc.stderr
    if expected_rc:
        assert "must not override the pinned base package" in proc.stdout + proc.stderr
