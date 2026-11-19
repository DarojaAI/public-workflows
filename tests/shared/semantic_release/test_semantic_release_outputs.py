"""The release outputs must say whether this run published, not what the latest tag is.

These tests run the reusable workflow's real "Run semantic-release" and "Extract
version from tag" steps in a throwaway git repo. A stand-in npx plays semantic-release,
except in the last test, which runs the real thing if REAL_SEMANTIC_RELEASE points at
its binary.
"""

from __future__ import annotations

import os
import re
import stat
import subprocess
from pathlib import Path

import pytest
import yaml

WORKFLOWS = Path(__file__).resolve().parents[3] / ".github" / "workflows"
SOURCES = {
    "reusable": WORKFLOWS / "reusable-semantic-release.yml",
}
IDENTITY = {
    "GIT_AUTHOR_NAME": "t",
    "GIT_AUTHOR_EMAIL": "t@example.invalid",
    "GIT_COMMITTER_NAME": "t",
    "GIT_COMMITTER_EMAIL": "t@example.invalid",
}


def git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
        env={**os.environ, **IDENTITY},
    ).stdout.strip()


def commit(repo: Path, message: str) -> None:
    git(repo, "commit", "--allow-empty", "-m", message)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    path = tmp_path / "repo"
    path.mkdir()
    git(path, "init", "-q", "-b", "main")
    commit(path, "chore: first")
    return path


def release_steps(source: Path) -> list[dict]:
    steps = yaml.safe_load(source.read_text())["jobs"]["release"]["steps"]
    wanted = ("Run semantic-release", "Extract version from tag")
    return [s for s in steps if s.get("name") in wanted]


def run(repo: Path, source: Path, npx_body: str) -> dict[str, str]:
    """Run the two steps with `npx_body` as npx. Returns the Extract step's outputs."""
    sandbox = repo.parent
    bin_dir = sandbox / "bin"
    temp = sandbox / "runner_temp"
    bin_dir.mkdir(exist_ok=True)
    temp.mkdir(exist_ok=True)
    npx = bin_dir / "npx"
    npx.write_text(f"#!/usr/bin/env bash\nset -e\n{npx_body}\n")
    npx.chmod(npx.stat().st_mode | stat.S_IEXEC)
    outputs = sandbox / "github_output"

    for step in release_steps(source):
        script = re.sub(r"\$\{\{.*?\}\}", "true", step["run"])
        outputs.write_text("")
        subprocess.run(
            ["bash", "-e", "-c", script],
            cwd=repo,
            env={
                **os.environ,
                **IDENTITY,
                "PATH": f"{bin_dir}:{os.environ['PATH']}",
                "RUNNER_TEMP": str(temp),
                "GITHUB_OUTPUT": str(outputs),
            },
            check=True,
            capture_output=True,
            text=True,
        )
    return dict(
        line.split("=", 1) for line in outputs.read_text().splitlines() if "=" in line
    )


both = pytest.mark.parametrize("source", SOURCES.values(), ids=SOURCES.keys())


@both
def test_a_new_tag_is_reported(repo, source):
    git(repo, "tag", "v1.2.2", "HEAD")
    commit(repo, "fix: thing")

    outputs = run(repo, source, 'git tag v1.2.3 HEAD; echo "Published release 1.2.3"')

    assert outputs == {"version": "1.2.3", "new_release_published": "true"}


@both
def test_nothing_published_with_an_older_tag_in_history(repo, source):
    """The reported bug: this used to say published=true, version=1.2.2."""
    git(repo, "tag", "v1.2.2", "HEAD")
    commit(repo, "chore: no release")

    outputs = run(repo, source, 'echo "There are no relevant changes"')

    assert outputs == {"new_release_published": "false"}


@both
def test_nothing_published_when_the_commit_was_already_tagged(repo, source):
    """A re-run of a commit that was released earlier."""
    git(repo, "tag", "v1.2.2", "HEAD")

    outputs = run(repo, source, 'echo "There are no relevant changes"')

    assert outputs == {"new_release_published": "false"}


@both
def test_nothing_published_in_a_repo_with_no_tags(repo, source):
    outputs = run(repo, source, 'echo "There are no relevant changes"')

    assert outputs == {"new_release_published": "false"}


@both
def test_a_tag_on_another_commit_is_not_this_runs_release(repo, source):
    """semantic-release fetches tags, so a tag pushed elsewhere can appear mid-run."""
    commit(repo, "chore: older")
    parent = git(repo, "rev-parse", "HEAD")
    commit(repo, "chore: head")

    outputs = run(repo, source, f"git tag v9.9.9 {parent}")

    assert outputs == {"new_release_published": "false"}


@both
def test_a_channel_tag_alone_is_not_a_release(repo, source):
    git(repo, "tag", "v1.2.2", "HEAD")

    outputs = run(repo, source, "git tag v1.2.2@beta HEAD")

    assert outputs == {"new_release_published": "false"}


def test_job_outputs_read_the_step_that_writes_them():
    for source in SOURCES.values():
        job = yaml.safe_load(source.read_text())["jobs"]["release"]
        assert "steps.version.outputs.new_release_published" in str(job["outputs"])
        assert "steps.version.outputs.version" in str(job["outputs"])


REAL = os.environ.get("REAL_SEMANTIC_RELEASE", "")


@both
@pytest.mark.skipif(not REAL, reason="REAL_SEMANTIC_RELEASE is not set")
def test_real_semantic_release(tmp_path, source):
    origin = tmp_path / "origin.git"
    repo = tmp_path / "repo"
    subprocess.run(
        ["git", "init", "-q", "--bare", "-b", "main", str(origin)], check=True
    )
    subprocess.run(["git", "clone", "-q", str(origin), str(repo)], check=True)
    git(repo, "checkout", "-q", "-b", "main")
    commit(repo, "feat: first")
    git(repo, "tag", "v1.0.0", "HEAD")
    git(repo, "push", "-q", "origin", "main", "--tags")
    (repo / ".releaserc.json").write_text(
        '{"branches": ["main"], "plugins": ["@semantic-release/commit-analyzer",'
        ' "@semantic-release/release-notes-generator"]}'
    )
    git(repo, "add", ".releaserc.json")
    commit(repo, "chore: config")
    git(repo, "push", "-q", "origin", "main")
    body = f'"{REAL}" --no-ci --repository-url "file://{origin}"'

    assert run(repo, source, body) == {"new_release_published": "false"}

    commit(repo, "fix: a real change")
    git(repo, "push", "-q", "origin", "main")

    assert run(repo, source, body) == {
        "version": "1.0.1",
        "new_release_published": "true",
    }
    assert "v1.0.1" in git(repo, "ls-remote", "--tags", "origin")

    assert run(repo, source, body) == {"new_release_published": "false"}
