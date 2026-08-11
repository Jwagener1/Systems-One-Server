# CI/CD Resilience, Deploy & Rollback — Design

## Context

`ci.yml` currently runs three jobs (`Lint`, `Syntax Check`, `Molecule`) on push/PR to `master`/`main`. The last 6 runs on `master` have failed.

**Root cause:** commit `6f87fda` ("feat(vault): recover and rotate all secrets under a new vault password") added `vault_password_file = .vault_pass` to `ansible.cfg`. `.vault_pass` is gitignored and doesn't exist in the CI runner. Ansible's CLI checks that the configured vault password file exists at startup — before it ever needs to decrypt anything — so `Syntax Check` fails immediately even though `--syntax-check` never touches decrypted secret values. Before this commit, no `vault_password_file` was configured, so CI never needed a vault password and passed fine.

Beyond that break, CI/CD has no resilience or deployment automation:
- No branch protection on `master` — the failing CI didn't block the merge that broke it.
- No CD pipeline at all. Deploys are 100% manual: SSH to `s1_server`, `git merge origin/master`, run `ansible-playbook` by hand, scoped with `--tags <role>` to limit blast radius.
- No GitHub Environments, secrets, or variables configured.
- No release/deploy tags — no way to identify or return to a previous known-good state.
- `s1_server` self-manages (Ansible runs on the box itself); there has never been a working external Ansible control node (not this Windows machine, not WSL).

## Goals

1. Fix the currently-broken CI and harden it against the same class of failure recurring.
2. Make deploying from this repo a one-click action, without requiring inbound network access to `s1_server`.
3. Make rolling back to a previous deploy equally easy.
4. Add safety gates appropriate for a single production box with no staging environment.

## Section 1: Fix & harden `ci.yml`

**Fix:** add a step before the `Syntax Check` / `Lint` jobs' Ansible-touching steps that writes a throwaway placeholder vault password file (e.g. `echo "ci-placeholder" > .vault_pass`). This satisfies the CLI startup check. Neither job decrypts real secret values (syntax-check only validates structure; unit tests don't touch vaulted vars), so a placeholder is sufficient and never needs to be a real secret.

**Hardening, same file:**
- Pin tool versions (`ansible==`, `ansible-lint==`, `yamllint==`) instead of unpinned `pip install` — prevents a new upstream release from breaking CI unexpectedly, the same failure class (unreviewed change silently breaks a working pipeline) as the vault password file issue.
- `concurrency: { group: ci-${{ github.ref }}, cancel-in-progress: true }` on the workflow — stop wasting runner time on superseded pushes.
- `timeout-minutes` on each job (e.g. 10 for Lint/Syntax Check, 15 for Molecule) instead of the default 6h cap.
- `cache: pip` on `actions/setup-python` in each job — cuts redundant install time.
- Surface `ansible-lint` output instead of silently swallowing it: keep `continue-on-error: true` (repo isn't lint-clean yet) but emit findings as `::warning::` annotations or a job summary, so a real regression is visible without blocking merges.

**Branch protection:** require `Lint`, `Syntax Check`, `Molecule` status checks to pass before merging to `master`. This is what should have stopped today's broken merge from landing.

## Section 2: Deploy workflow (`deploy.yml`)

### Self-hosted runner

A GitHub Actions self-hosted runner installed as a service on `s1_server` (labels: `self-hosted`, `s1-server`). It makes outbound-only connections to GitHub to pick up jobs — no inbound access to the box is required, and it matches the existing "Ansible runs on the box itself" architecture. **Manual step, not automated by this design**: installing the runner service and confirming it comes online is a one-time action taken directly on the box.

### Workflow

Trigger: `workflow_dispatch` with inputs:
- `ref` (string, default `master`) — branch, tag, or SHA to deploy.
- `tags` (string, optional) — Ansible role tag(s) to scope the run (e.g. `s1_reporter`), matching current manual practice of limiting blast radius. Blank = whole site.

Two jobs:

1. **`validate`** — runs on a standard GitHub-hosted runner (`ubuntu-latest`). Checks out `inputs.ref` explicitly and re-runs the syntax-check step from `ci.yml` against that exact ref. This is a safety net independent of branch protection, since an arbitrary tag/SHA passed to `ref` isn't necessarily covered by branch protection.
2. **`deploy`** (`needs: validate`) — runs on the self-hosted runner. Operates directly on the persistent checkout at `/home/s1/Systems-One-Server` rather than a fresh `actions/checkout`, so it reuses the `.vault_pass` file that already lives there (never committed, never touches GitHub):
   - `git fetch origin && git checkout <inputs.ref>`
   - `ansible-playbook -i production webservers.yml` (`--tags <inputs.tags>` appended when provided)
   - On success: create and push an annotated tag `deploy-<UTC timestamp>` marking this as a rollback point.

`concurrency: { group: deploy-s1server }` (no `cancel-in-progress`) on the `deploy` job — queues overlapping runs rather than letting two `ansible-playbook` invocations race against the same docker-compose state.

Deploy is manual-trigger only (no auto-deploy on merge to `master`), since the only pre-deploy validation is lint/syntax-check/molecule — thin enough that auto-pushing every merge straight to the only production box isn't warranted yet.

## Section 3: Rollback workflow (`rollback.yml`)

Same shape as `deploy.yml`, self-hosted runner, `workflow_dispatch` with:
- `tag` (string, required) — a previous `deploy-*` tag to return to.
- `tags` (string, optional) — role scope, same semantics as deploy.

Steps: `git fetch --tags && git checkout <inputs.tag>`, then `ansible-playbook -i production webservers.yml` (scoped as above). Shares the `deploy-s1server` concurrency group with `deploy.yml` so a rollback can't race an in-flight deploy.

## Section 4: Safety gates

- **Branch protection on `master`** (see Section 1) — required status checks before merge.
- **GitHub Environment `production`**, attached to the `deploy` job in both `deploy.yml` and `rollback.yml`, with a required reviewer (the repo owner). Every deploy/rollback pauses for a one-click approval, and the Environments tab gives a visible deploy history independent of the commit log.

## Error handling

- If `validate` fails, `deploy` never starts (job dependency) — a bad ref can't reach the box.
- If `ansible-playbook` fails mid-run in `deploy`/`rollback`, the workflow fails loudly (no swallowed exit codes) and no deploy tag is created — a failed run is never mistaken for a rollback point.
- Concurrency queueing (not cancellation) on the deploy group prevents two Ansible runs from interleaving writes to the same docker-compose files.

## Testing

- `ci.yml` changes: push to a branch, confirm `Syntax Check` goes green with the placeholder vault file, confirm pinned versions still resolve.
- `deploy.yml`/`rollback.yml`: cannot be fully tested until the self-hosted runner is online; validate the `validate` job (GitHub-hosted, no dependency on the runner) independently first, then do one end-to-end dry run against a low-risk single role (e.g. `--tags s1_reporter`) once the runner is registered.
- Branch protection / Environment reviewer settings: verified by inspection in the GitHub UI (`gh api repos/.../branches/master/protection`, `gh api repos/.../environments`) after configuring.

## Out of scope

- Auto-deploy on merge to `master`.
- Exposing SSH to `s1_server` via Cloudflare Tunnel (rejected in favor of the self-hosted runner — no new inbound attack surface).
- Blue-green or canary deploys — single production box, not warranted.
- Expanding Molecule coverage beyond the `docker` role.
