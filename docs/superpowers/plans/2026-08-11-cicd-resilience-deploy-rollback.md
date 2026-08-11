# CI/CD Resilience, Deploy & Rollback Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fix the currently-broken CI, harden it against recurrence, and add one-click deploy and rollback for `s1_server` triggered from this GitHub repo.

**Architecture:** `ci.yml` gets a placeholder-vault fix plus resilience hardening (pinned tool versions, concurrency, timeouts, caching, surfaced lint results) and branch protection requiring its checks. Two new `workflow_dispatch` workflows (`deploy.yml`, `rollback.yml`) run their final step on a self-hosted GitHub Actions runner installed on `s1_server` itself — matching the existing "Ansible runs on the box" architecture, since there's no working external control node and no inbound access to the box. Both gate on a `production` GitHub Environment requiring manual approval. Every successful deploy creates a `deploy-<timestamp>` git tag as a rollback point.

**Tech Stack:** GitHub Actions (`workflow_dispatch`, self-hosted runners, Environments, branch protection), Ansible (`ansible-playbook`, `--syntax-check`), `gh` CLI, bash.

## Global Constraints

- Repo is `Jwagener1/Systems-One-Server`, default branch `master`.
- The placeholder `.vault_pass` written in CI must never be a real secret — CI never decrypts vaulted variables, only parses structure (syntax-check) or runs unit tests that don't touch them.
- `deploy.yml`/`rollback.yml`'s box-side steps operate on the existing persistent checkout at `/home/s1/Systems-One-Server`, never a fresh `actions/checkout` — that path already has the real `.vault_pass` and doesn't need one shipped through GitHub.
- Both workflows are `workflow_dispatch` only — no auto-deploy on merge.
- `deploy.yml` and `rollback.yml` share `concurrency: group: deploy-s1server` (no `cancel-in-progress`) so they queue instead of racing.
- Self-hosted runner labels: `self-hosted`, `s1-server`.
- GitHub Environment name: `production`, required reviewer: the repo owner (`Jwagener1`).
- SSH to `s1_server` from this Windows machine must use `/c/Windows/System32/OpenSSH/ssh.exe s1_server "<command>"` — Git Bash's bundled `ssh.exe` cannot load the required key.

---

### Task 1: Fix & harden `ci.yml`

**Files:**
- Modify: `.github/workflows/ci.yml`

**Interfaces:**
- Produces: a `Lint`, `Syntax Check`, `Molecule (docker role)` job trio that passes on a clean push, used by Task 2's branch protection rule (exact job names must match).

- [ ] **Step 1: Rewrite `.github/workflows/ci.yml`**

Replace the full file contents with:

```yaml
name: CI

on:
  push:
    branches: [master, main]
  pull_request:
    branches: [master, main]

concurrency:
  group: ci-${{ github.ref }}
  cancel-in-progress: true

env:
  FORCE_JAVASCRIPT_ACTIONS_TO_NODE24: true

jobs:
  lint:
    name: Lint
    runs-on: ubuntu-latest
    timeout-minutes: 10
    steps:
      - uses: actions/checkout@v4

      - name: Set up Python
        uses: actions/setup-python@v5
        with:
          python-version: "3.12"
          cache: pip

      - name: Install linting tools
        run: pip install "ansible-lint~=25.0" "yamllint~=1.35"

      - name: Run yamllint
        run: yamllint -d relaxed .

      - name: Run s1_dashboard unit tests
        run: python roles/s1_dashboard/tests/test_dashboard.py

      - name: Run s1_reporter unit tests
        run: python -m unittest discover -s roles/s1_reporter/tests

      - name: Create placeholder vault password file
        run: echo "ci-placeholder" > .vault_pass

      - name: Run ansible-lint
        id: ansible_lint
        run: ansible-lint --profile=min site.yml
        continue-on-error: true

      - name: Surface ansible-lint result
        if: steps.ansible_lint.outcome == 'failure'
        run: echo "::warning::ansible-lint reported issues — see the 'Run ansible-lint' step log above for details"

  syntax-check:
    name: Syntax Check
    runs-on: ubuntu-latest
    timeout-minutes: 10
    steps:
      - uses: actions/checkout@v4

      - name: Set up Python
        uses: actions/setup-python@v5
        with:
          python-version: "3.12"
          cache: pip

      - name: Install Ansible
        run: pip install "ansible~=14.3"

      - name: Create placeholder vault password file
        run: echo "ci-placeholder" > .vault_pass

      - name: Syntax check site.yml
        run: |
          ansible-playbook site.yml --syntax-check \
            -i production \
            -e "mssql_sa_password=test \
                mssql_app_login_password=test \
                grafana_admin_password=test \
                grafana_jonathan_password=test \
                grafana_avi_password=test \
                grafana_chris_password=test \
                grafana_pkluser_password=test \
                mqtt_password=test \
                grafana_git_sync_token=test \
                cloudflare_tunnel_token=test"

  molecule:
    name: Molecule (docker role)
    runs-on: ubuntu-latest
    timeout-minutes: 15
    steps:
      - uses: actions/checkout@v4

      - name: Set up Python
        uses: actions/setup-python@v5
        with:
          python-version: "3.12"
          cache: pip

      - name: Install dependencies
        run: pip install "ansible~=14.3" molecule molecule-plugins[docker] docker

      - name: Run Molecule
        run: molecule test
        env:
          PY_COLORS: "1"
          ANSIBLE_FORCE_COLOR: "1"
          ANSIBLE_ROLES_PATH: "${{ github.workspace }}/roles"
```

- [ ] **Step 2: Validate YAML locally**

Run: `python -c "import yaml,sys; yaml.safe_load(open('.github/workflows/ci.yml'))" `
Expected: no output, exit code 0 (confirms the YAML parses).

- [ ] **Step 3: Commit**

```bash
git add .github/workflows/ci.yml
git commit -m "fix(ci): unbreak syntax-check/lint on vault_password_file, add resilience hardening"
```

- [ ] **Step 4: Push and verify on GitHub**

```bash
git push origin master
gh run watch --exit-status
```

Expected: `Lint`, `Syntax Check`, and `Molecule (docker role)` all complete with conclusion `success`. If `Syntax Check` still fails, re-check the failure log with `gh run view --log-failed` — do not proceed to Task 2 until this is green, since Task 2 makes these checks required for every future merge.

---

### Task 2: Require CI checks before merging to `master`

**Files:** none (GitHub repo setting via API)

**Interfaces:**
- Consumes: the exact job names `Lint`, `Syntax Check`, `Molecule (docker role)` from Task 1.

- [ ] **Step 1: Apply branch protection**

```bash
gh api -X PUT repos/Jwagener1/Systems-One-Server/branches/master/protection \
  -H "Accept: application/vnd.github+json" \
  --input - <<'EOF'
{
  "required_status_checks": {
    "strict": true,
    "contexts": ["Lint", "Syntax Check", "Molecule (docker role)"]
  },
  "enforce_admins": true,
  "required_pull_request_reviews": null,
  "restrictions": null
}
EOF
```

- [ ] **Step 2: Verify**

```bash
gh api repos/Jwagener1/Systems-One-Server/branches/master/protection --jq '.required_status_checks.contexts'
```

Expected: `["Lint", "Syntax Check", "Molecule (docker role)"]`

---

### Task 3: Create the `production` GitHub Environment with a required reviewer

**Files:** none (GitHub repo setting via API)

**Interfaces:**
- Produces: an environment named `production` that Task 4 and Task 5's workflow jobs reference via `environment: production`.

- [ ] **Step 1: Look up the reviewer's numeric GitHub user ID**

```bash
gh api users/Jwagener1 --jq .id
```

Note the returned integer — used as `<OWNER_ID>` below.

- [ ] **Step 2: Create the environment with that reviewer required**

```bash
gh api -X PUT repos/Jwagener1/Systems-One-Server/environments/production \
  -H "Accept: application/vnd.github+json" \
  --input - <<EOF
{
  "reviewers": [
    { "type": "User", "id": <OWNER_ID> }
  ],
  "deployment_branch_policy": null
}
EOF
```

Replace `<OWNER_ID>` with the value from Step 1 before running.

- [ ] **Step 3: Verify**

```bash
gh api repos/Jwagener1/Systems-One-Server/environments/production --jq '.protection_rules[].reviewers[].reviewer.login'
```

Expected: `Jwagener1`

---

### Task 4: Create `deploy.yml`

**Files:**
- Create: `.github/workflows/deploy.yml`

**Interfaces:**
- Consumes: `production` environment (Task 3), self-hosted runner labeled `self-hosted, s1-server` (Task 6 — this workflow can be committed and its `validate` job tested before the runner exists; the `deploy` job will simply stay queued until Task 6 is done).
- Produces: on success, a `deploy-<UTC timestamp>` git tag pushed to origin — consumed by Task 5 (`rollback.yml`)'s `tag` input.

- [ ] **Step 1: Write `.github/workflows/deploy.yml`**

```yaml
name: Deploy

on:
  workflow_dispatch:
    inputs:
      ref:
        description: "Branch, tag, or SHA to deploy"
        required: false
        default: master
      tags:
        description: "Ansible role tag(s) to scope the deploy (blank = whole site)"
        required: false
        default: ""

concurrency:
  group: deploy-s1server

jobs:
  validate:
    name: Validate
    runs-on: ubuntu-latest
    timeout-minutes: 10
    steps:
      - uses: actions/checkout@v4
        with:
          ref: ${{ inputs.ref }}

      - name: Set up Python
        uses: actions/setup-python@v5
        with:
          python-version: "3.12"
          cache: pip

      - name: Install Ansible
        run: pip install "ansible~=14.3"

      - name: Create placeholder vault password file
        run: echo "ci-placeholder" > .vault_pass

      - name: Syntax check site.yml
        run: |
          ansible-playbook site.yml --syntax-check \
            -i production \
            -e "mssql_sa_password=test \
                mssql_app_login_password=test \
                grafana_admin_password=test \
                grafana_jonathan_password=test \
                grafana_avi_password=test \
                grafana_chris_password=test \
                grafana_pkluser_password=test \
                mqtt_password=test \
                grafana_git_sync_token=test \
                cloudflare_tunnel_token=test"

  deploy:
    name: Deploy to s1_server
    needs: validate
    runs-on: [self-hosted, s1-server]
    environment: production
    timeout-minutes: 20
    steps:
      - name: Checkout target ref on the box
        working-directory: /home/s1/Systems-One-Server
        env:
          REF: ${{ inputs.ref }}
        run: |
          git fetch origin --tags
          git checkout "$REF"
          git merge --ff-only "origin/$REF" 2>/dev/null || true

      - name: Run ansible-playbook
        working-directory: /home/s1/Systems-One-Server
        env:
          ROLE_TAGS: ${{ inputs.tags }}
        run: |
          if [ -n "$ROLE_TAGS" ]; then
            ansible-playbook -i production webservers.yml --tags "$ROLE_TAGS"
          else
            ansible-playbook -i production webservers.yml
          fi

      - name: Tag this deploy
        working-directory: /home/s1/Systems-One-Server
        env:
          REF: ${{ inputs.ref }}
          ROLE_TAGS: ${{ inputs.tags }}
        run: |
          tag="deploy-$(date -u +%Y%m%d-%H%M%S)"
          git tag -a "$tag" -m "Deploy of $REF (tags: ${ROLE_TAGS:-all})"
          git push origin "$tag"
          echo "Deployed and tagged $tag"
```

**Security note:** inputs are passed via `env:` and referenced as shell variables (`$REF`, `$ROLE_TAGS`), never interpolated directly as `${{ inputs.* }}` inside a `run:` script. Direct interpolation lets a crafted `workflow_dispatch` input (e.g. containing `` $(...) `` or `;`) be substituted as literal shell syntax before the shell runs — GitHub Actions script injection (CWE-78). Passing through `env:` treats the value as inert data instead.

- [ ] **Step 2: Validate YAML locally**

Run: `python -c "import yaml,sys; yaml.safe_load(open('.github/workflows/deploy.yml'))"`
Expected: no output, exit code 0.

- [ ] **Step 3: Commit and push**

```bash
git add .github/workflows/deploy.yml
git commit -m "feat(ci): add one-click deploy workflow for s1_server"
git push origin master
```

- [ ] **Step 4: Test the `validate` job (runner-independent)**

```bash
gh workflow run deploy.yml -f ref=master -f tags=s1_reporter
gh run watch --exit-status
```

Expected: `validate` job succeeds. The `deploy` job will show `queued` indefinitely until Task 6 registers the self-hosted runner — that's expected at this point in the plan. Cancel the run afterward with `gh run cancel <run-id>` so it doesn't sit queued.

---

### Task 5: Create `rollback.yml`

**Files:**
- Create: `.github/workflows/rollback.yml`

**Interfaces:**
- Consumes: a `deploy-*` tag produced by Task 4's `deploy` job, `production` environment (Task 3), self-hosted runner (Task 6).

- [ ] **Step 1: Write `.github/workflows/rollback.yml`**

```yaml
name: Rollback

on:
  workflow_dispatch:
    inputs:
      tag:
        description: "Previous deploy-* tag to roll back to (see the Actions > Deploy run history or `git tag -l 'deploy-*'`)"
        required: true
      tags:
        description: "Ansible role tag(s) to scope the rollback (blank = whole site)"
        required: false
        default: ""

concurrency:
  group: deploy-s1server

jobs:
  rollback:
    name: Rollback s1_server
    runs-on: [self-hosted, s1-server]
    environment: production
    timeout-minutes: 20
    steps:
      - name: Checkout target tag on the box
        working-directory: /home/s1/Systems-One-Server
        env:
          ROLLBACK_TAG: ${{ inputs.tag }}
        run: |
          git fetch origin --tags
          git checkout "$ROLLBACK_TAG"

      - name: Run ansible-playbook
        working-directory: /home/s1/Systems-One-Server
        env:
          ROLE_TAGS: ${{ inputs.tags }}
        run: |
          if [ -n "$ROLE_TAGS" ]; then
            ansible-playbook -i production webservers.yml --tags "$ROLE_TAGS"
          else
            ansible-playbook -i production webservers.yml
          fi

      - name: Record rollback
        env:
          ROLLBACK_TAG: ${{ inputs.tag }}
          ROLE_TAGS: ${{ inputs.tags }}
        run: echo "Rolled back to $ROLLBACK_TAG (role scope: ${ROLE_TAGS:-all})"
```

**Security note:** same rationale as `deploy.yml` — inputs are passed via `env:` and referenced as shell variables, never interpolated directly as `${{ inputs.* }}` inside a `run:` script, to avoid GitHub Actions script injection (CWE-78).

- [ ] **Step 2: Validate YAML locally**

Run: `python -c "import yaml,sys; yaml.safe_load(open('.github/workflows/rollback.yml'))"`
Expected: no output, exit code 0.

- [ ] **Step 3: Commit and push**

```bash
git add .github/workflows/rollback.yml
git commit -m "feat(ci): add one-click rollback workflow for s1_server"
git push origin master
```

(No standalone test here — `rollback.yml` has only the runner-dependent job, so it's exercised end-to-end in Task 7.)

---

### Task 6: Install the self-hosted runner on `s1_server`

**Files:**
- Create: `scripts/install-github-actions-runner.sh`

**Interfaces:**
- Produces: a running `actions.runner.*` systemd service on `s1_server` with labels `self-hosted, s1-server`, satisfying `deploy.yml`/`rollback.yml`'s `runs-on: [self-hosted, s1-server]`.

> **This task touches production infrastructure — confirm with the user before running Step 4.** Registering a self-hosted runner gives GitHub Actions workflow_dispatch triggers the ability to execute code on `s1_server`.

- [ ] **Step 1: Write `scripts/install-github-actions-runner.sh`**

```bash
#!/usr/bin/env bash
set -euo pipefail

REPO="Jwagener1/Systems-One-Server"
RUNNER_DIR="$HOME/actions-runner"
LABELS="self-hosted,s1-server"
RUNNER_NAME="s1-server"

if [ -z "${RUNNER_TOKEN:-}" ]; then
  echo "RUNNER_TOKEN env var required. Get one with:" >&2
  echo "  gh api -X POST repos/$REPO/actions/runners/registration-token --jq .token" >&2
  exit 1
fi

VERSION=$(curl -fsSL https://api.github.com/repos/actions/runner/releases/latest \
  | grep -oP '"tag_name": "v\K[0-9.]+' | head -1)

mkdir -p "$RUNNER_DIR"
cd "$RUNNER_DIR"
curl -fsSL -o actions-runner-linux-x64.tar.gz \
  "https://github.com/actions/runner/releases/download/v${VERSION}/actions-runner-linux-x64-${VERSION}.tar.gz"
tar xzf actions-runner-linux-x64.tar.gz

./config.sh --url "https://github.com/$REPO" \
  --token "$RUNNER_TOKEN" \
  --labels "$LABELS" \
  --name "$RUNNER_NAME" \
  --work "_work" \
  --unattended

sudo ./svc.sh install
sudo ./svc.sh start
```

- [ ] **Step 2: Commit**

```bash
git add scripts/install-github-actions-runner.sh
git commit -m "chore: add self-hosted runner install script for s1_server"
git push origin master
```

- [ ] **Step 3: Fetch a registration token**

```bash
gh api -X POST repos/Jwagener1/Systems-One-Server/actions/runners/registration-token --jq .token
```

Registration tokens expire after 1 hour — run Step 4 promptly after this.

- [ ] **Step 4: Run the installer on `s1_server`** *(confirm with the user first — see note above)*

```bash
/c/Windows/System32/OpenSSH/ssh.exe s1_server "cd /home/s1/Systems-One-Server && git pull --ff-only && RUNNER_TOKEN='<token from Step 3>' bash scripts/install-github-actions-runner.sh"
```

- [ ] **Step 5: Verify the runner is online**

```bash
gh api repos/Jwagener1/Systems-One-Server/actions/runners --jq '.runners[] | {name, status, labels: [.labels[].name]}'
```

Expected: one runner named `s1-server`, `status: "online"`, labels including `self-hosted` and `s1-server`.

---

### Task 7: End-to-end verification

**Files:** none — verification only.

- [ ] **Step 1: Run a scoped deploy dry run**

```bash
gh workflow run deploy.yml -f ref=master -f tags=s1_reporter
```

Approve the `production` environment gate when prompted (Actions tab → the running workflow → Review deployments).

```bash
gh run watch --exit-status
```

Expected: both `validate` and `deploy` jobs succeed, and a new `deploy-<timestamp>` tag appears:

```bash
git ls-remote --tags origin | grep deploy-
```

- [ ] **Step 2: Confirm the deployed service is healthy**

```bash
/c/Windows/System32/OpenSSH/ssh.exe s1_server "docker ps --filter name=s1-reporter"
```

Expected: the container is `Up` and healthy.

- [ ] **Step 3: Run a rollback to that same tag (proves the path works end-to-end)**

```bash
git fetch --tags
LATEST_TAG=$(git tag -l 'deploy-*' --sort=-creatordate | head -1)
gh workflow run rollback.yml -f tag="$LATEST_TAG" -f tags=s1_reporter
```

Approve the `production` environment gate when prompted.

```bash
gh run watch --exit-status
```

Expected: `rollback` job succeeds.

- [ ] **Step 4: Confirm branch protection actually blocks a broken merge**

```bash
git checkout -b test/verify-branch-protection
echo "this: is not valid yaml: [" >> roles/mqtt/tasks/main.yml
git add roles/mqtt/tasks/main.yml
git commit -m "test: intentionally broken yaml to verify branch protection"
git push origin test/verify-branch-protection
gh pr create --title "test: verify branch protection" --body "Temporary PR to confirm required checks block merge." --base master
gh pr checks --watch
```

Expected: `Lint` and/or `Syntax Check` fail, and the PR's merge button is blocked ("Required statuses must pass"). Then clean up:

```bash
gh pr close --delete-branch
git checkout master
git branch -D test/verify-branch-protection
```
