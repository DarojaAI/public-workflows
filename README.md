# public-workflows

DarojaAI's GitHub Actions workflows that need to be readable by **public**
repositories. It holds the `agent-guardrails` workflow that the DarojaAI org
ruleset requires, and is meant to hold other public workflows and actions
later.

## Everything here is public

Anyone on the internet can read this repo and its full history. Never commit:

- secrets, tokens, keys or passwords, even expired ones or in test fixtures;
- internal URLs, hostnames, IP addresses, cloud project or account IDs;
- customer, client or personal data.

Workflows here get their credentials at run time, from the calling repo's
`GITHUB_TOKEN` or secrets, never from this repo. CI runs gitleaks over the
whole history on every PR and push. The repo owner should also turn on
**GitHub secret scanning** and **push protection** (Settings, Advanced
Security): they are free for public repos, and push protection blocks a
secret before it lands.

## Why this repo exists

GitHub runs a ruleset-required workflow only in repositories that are no
more public than the repository holding it. A public workflow can run in any
repo in the org, an internal one only in internal and private repos, and a
private one only in private repos. The guardrail used to live in the org's
internal `.github` repo, so from 2026-11-04 no public DarojaAI repo got an
`agent-guardrails` run, and every PR in those repos stayed blocked waiting
for a required check that never came. Keeping the workflow here, in a public
repo, lets it run in public, internal and private repos alike.

A ruleset can only require a workflow from a repo in its own org. Another org
needs its own public repo (for example `ElasticaApp/public-workflows`).

## Workflows

### `.github/workflows/agent-guardrails.yml`

This is the `agent-guardrails` required check. The org ruleset "agent-coding:
default branch" requires it on the default branch of every repo whose
`agent-coding` custom property is true. It applies only to agent PRs (head
branch `agent/**`, or author `darojaai-agents[bot]`). Other PRs pass at once.

- **G1, gate files (tier 1):** the PR touches a file that controls the
  guardrails themselves: `CODEOWNERS`, `**agent-guardrails*`,
  `**claude-guardrails*`, `**claude-run*`, merge lanes (`**automerge*`,
  `**auto-merge*`, `**ready-merge*`), agent-coding setup and token minting
  (`**agent-coding*`, `**agent-gh*`), rulesets and branch protection
  (`**ruleset*`, `**branch-protection*`, `**branch_protection*`), the Semgrep
  gate and its baseline (`.semgrep/**`, `**.semgrepignore`, `**semgrep_gate*`),
  or `tests/canaries/**`. Ordinary workflows, pre-commit and coverage config are
  not gated here.
- **G2, test tampering:** the PR deletes a test file, or adds a skip to one
  (pytest or unittest skip/xfail, JS `.skip`/`.todo`/`x`-tests, bats `skip`,
  Go `t.Skip`, or `|| true`).

An approver named in the file's `APPROVERS` overrides a finding by adding the
`gate-approved` label **after** the PR's latest commit. Required workflows
only run on the default `pull_request` activity types, which don't include
`labeled`. So when the check finds something, it waits up to 60 minutes for
the label and passes within about 30 seconds of it being added. If the wait
runs out it fails: add the label, then re-run the check.

The file is generated, not hand-edited. Its source is the agent-coding
template in DarojaAI's setup repository (`linux-desktop-seed`,
`config/agent-coding/agent-guardrails.yml`), rendered with the approver list
from that repo's `config/agent-coding.json`. The org setup checks that the
release the ruleset points at holds exactly that rendering, and refuses to
point the ruleset at a release that doesn't. To change the guardrail, change
the template there first. Then land the same rendered file here and cut a
release (below).

The workflow also runs on this repo's own PRs, as an ordinary (not required)
check.

### This repo is never covered by the agent-coding rulesets

`agent-coding` is `false` here, permanently: no agent gets a token for the
repo that holds the gate, and the org rulesets never cover it. (It was set
before `main` existed, because ruleset "agent-coding: default branch" has no
bypass and also enforces on branch creation.) Instead, `main` is protected by
this repo's own ruleset: PRs only, and the `ci.yml` checks (`actionlint`,
`gitleaks`, `guardrail tests`) must pass.

### `.github/workflows/ci.yml`

CI for this repo: actionlint (with shellcheck on every `run:` block),
gitleaks over the full history, and `tests/`. The tests run the guardrail's
real script against throwaway git repos standing in for PRs (agent and
non-agent, gate file or not, approved before or after the latest commit,
deleted test, added skip), plus `bash -n` and `py_compile` of the embedded
code. Tools are pinned by version and sha256, actions by commit SHA (the
guardrail itself pins `actions/checkout` by SHA too).

Run the tests locally with `pip install pyyaml==6.0.2`, then
`python3 -m unittest discover -s tests -v` (needs git, jq and bash).

## Releases and pinning

Releases are semver tags `vMAJOR.MINOR.PATCH`, cut from `main` after a PR
merges and CI is green.

- **Rulesets pin a tag.** The DarojaAI ruleset requires
  `.github/workflows/agent-guardrails.yml` at `refs/tags/v1.0.0`; GitHub's
  rulesets REST API describes the workflow `ref` as "The ref (branch or tag)
  of the workflow file to use". Merging to `main` changes nothing for
  consumers until a new tag is cut and the setup repo's
  `config/agent-coding.json` (`guardrails.workflow_homes.DarojaAI.ref`) points
  at it.
- **Other consumers** (`uses:` of a reusable workflow or action added here
  later) pin a tag, or a full commit SHA for the strongest pin.
- **Never move or delete a released tag.** Fix forward with a new version.
  When the guardrail changes, cut a new tag and bump
  `guardrails.workflow_homes.DarojaAI.ref` in the setup repo; the setup
  fails with DRIFT (and leaves the ruleset as it is) until both are done.
  The owner should protect `v*` tags with a tag ruleset (block update and
  deletion, restrict creation to maintainers) and can turn on immutable
  releases.
- **Version bumps:** PATCH for comment or doc-only changes to a workflow,
  MINOR for behaviour that is stricter or adds options, MAJOR for anything a
  consumer must adapt to (a renamed check, a removed input).

Cutting a release (maintainer, after the PR merges):

```bash
git switch main && git pull --ff-only
git tag -a v1.0.0 -m "agent-guardrails v1.0.0"
git push origin v1.0.0
```

Then, for the guardrail: bump the ref in the setup repo's config if it
changed, and dispatch its "Agent Coding Setup" workflow so the ruleset points
at the new tag.
