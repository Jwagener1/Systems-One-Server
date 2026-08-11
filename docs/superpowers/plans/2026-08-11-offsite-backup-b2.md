# Offsite Backup to Backblaze B2 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a disabled-by-default `backup` role that nightly-dumps MSSQL (`S1_Remote_Monitoring`) and archives Mosquitto's persistence volume to Backblaze B2 via restic, and a pre-deploy gate in `webservers.yml`/`dbservers.yml` that refuses to apply Ansible changes unless a successful backup has completed recently.

**Architecture:** One new role, `roles/backup`. Everything runs through ephemeral `docker run --rm` containers (restic) or `docker exec` (MSSQL `sqlcmd`), matching the existing pattern in `roles/mqtt` — no new host packages. A cron job runs the script nightly. A separate `gate.yml` task file in the same role is imported into both `webservers.yml` and `dbservers.yml`'s existing `pre_tasks` blocks, so every deploy path (manual today, `deploy.yml` later) goes through it. The master toggle `backup_enabled` lives in `group_vars/all.yml` so it's visible to the gate regardless of role/task ordering.

**Tech Stack:** Ansible (`cron`, `template`, `stat`, `slurp`, `assert`/`fail` modules — all already used elsewhere in this repo), restic (via `restic/restic` Docker image), Backblaze B2 (restic's native `b2:` backend), bash.

## Global Constraints

- `backup_enabled` defaults to `false` everywhere — this plan must not turn backups on for production or staging. Enabling is a manual follow-up once real B2 credentials exist.
- No real secrets get committed. The three new vaulted variables (`vault_backup_b2_account_id`, `vault_backup_b2_account_key`, `vault_backup_restic_password`) are added to `group_vars/vault.yml` (encrypted) by the user directly via `ansible-vault edit` — flagged explicitly in Task 4, not performed by the implementer.
- Everything runs via `docker run --rm` or `docker exec` — no restic/B2 CLI installed on the host, consistent with how `roles/mqtt` already shells out to `docker run --rm eclipse-mosquitto:2 ...mosquitto_passwd`.
- Follow existing repo conventions exactly: `assert`/`fail_msg` blocks for required-variable validation (see `roles/mssql/tasks/main.yml`, `roles/mqtt/tasks/main.yml`), `{{ docker_shared_network | default('infra') }}`-style defaults, `no_log: true` on any task that touches secrets.
- Molecule CI only converges the `docker` role (`molecule/default/converge.yml`) — the new `backup` role does not need Molecule coverage and must not be added to that converge play.
- **Local verification note:** `ansible-playbook` does not run on this Windows development machine (a pre-existing, documented repo limitation — see `docs/superpowers/specs/2026-08-11-cicd-resilience-deploy-rollback-design.md`: "there has never been a working external Ansible control node — not this Windows machine, not WSL"). Every task's verification step therefore relies on `python -c "import yaml,...; yaml.safe_load(...)"` for YAML validity, which does work locally. Steps that also list an `ansible-playbook --syntax-check` command are aspirational/for-the-record (they will run cleanly in CI, which is Linux) — do not treat a local inability to run them as a task failure; note it in the report instead.

---

### Task 1: Add the backup feature toggle to `group_vars/all.yml`

**Files:**
- Modify: `group_vars/all.yml`

**Interfaces:**
- Produces: `backup_enabled` (bool, default `false`) and `backup_gate_max_age_hours` (int, default `30`) — consumed by Task 2's role tasks and Task 3's gate tasks.

- [ ] **Step 1: Add the toggle vars**

Append to `group_vars/all.yml`:

```yaml

# Offsite backup gate — see roles/backup and
# docs/superpowers/specs/2026-08-11-offsite-backup-b2-design.md.
# Flip to true (per-host, in host_vars) once B2 credentials are set in
# group_vars/vault.yml and roles/backup/run-backup.sh has been run manually
# at least once on that host.
backup_enabled: false
backup_gate_max_age_hours: 30
```

- [ ] **Step 2: Validate YAML**

Run: `python -c "import yaml,sys; yaml.safe_load(open('group_vars/all.yml'))"`
Expected: no output, exit code 0.

- [ ] **Step 3: Commit**

```bash
git add group_vars/all.yml
git commit -m "feat(backup): add disabled-by-default backup toggle vars"
```

---

### Task 2: Create the `backup` role — script, install, cron

**Files:**
- Create: `roles/backup/defaults/main.yml`
- Create: `roles/backup/templates/run-backup.sh.j2`
- Create: `roles/backup/tasks/main.yml`
- Modify: `dbservers.yml`

**Interfaces:**
- Consumes: `backup_enabled`, `backup_gate_max_age_hours` (Task 1); `mssql_sa_password`, `mssql_rm_database` (existing, from `roles/mssql`/host_vars); `backup_b2_account_id`, `backup_b2_account_key`, `backup_restic_password` (defined in Task 4 — this role references them but they don't need to resolve to real values until `backup_enabled` is actually flipped to `true`).
- Produces: `/opt/backup/run-backup.sh` (rendered script) and a root cron entry running it nightly, only when `backup_enabled` is `true`. `/opt/backup/status/last_success` (UTC ISO8601 timestamp file) — consumed by Task 3's gate.

- [ ] **Step 1: Write `roles/backup/defaults/main.yml`**

```yaml
---
# Master toggle (backup_enabled) and gate window (backup_gate_max_age_hours)
# live in group_vars/all.yml, not here — the pre-deploy gate in
# webservers.yml/dbservers.yml pre_tasks must see them before this role's
# tasks (which install the script/cron) ever run.

backup_mssql_database: "{{ mssql_rm_database | default('S1_Remote_Monitoring') }}"
backup_mssql_container: mssql
backup_mosquitto_volume: mosquitto_data

backup_root_dir: /opt/backup
backup_staging_dir: /opt/backup/staging
backup_status_dir: /opt/backup/status
backup_script_path: /opt/backup/run-backup.sh
backup_log_path: /opt/backup/backup.log

backup_schedule_hour: "2"
backup_schedule_minute: "30"

backup_restic_image_tag: "0.17"
backup_retention_daily: 7
backup_retention_weekly: 4
backup_retention_monthly: 6

backup_b2_bucket: "systems-one-backups"
backup_b2_path: "sysone"
```

- [ ] **Step 2: Write `roles/backup/templates/run-backup.sh.j2`**

```bash
#!/usr/bin/env bash
# Managed by Ansible (roles/backup) - do not edit by hand, changes will be overwritten.
set -euo pipefail

STAGING="{{ backup_staging_dir }}"
STATUS_DIR="{{ backup_status_dir }}"
RESTIC_REPOSITORY="b2:{{ backup_b2_bucket }}:{{ backup_b2_path }}"
RESTIC_IMAGE="restic/restic:{{ backup_restic_image_tag }}"

export RESTIC_REPOSITORY
export RESTIC_PASSWORD="{{ backup_restic_password }}"
export B2_ACCOUNT_ID="{{ backup_b2_account_id }}"
export B2_ACCOUNT_KEY="{{ backup_b2_account_key }}"

mkdir -p "$STAGING" "$STATUS_DIR"

log() {
  echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] $*"
}

fail() {
  log "FAILED: $*"
  echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) $*" >> "$STATUS_DIR/last_failure"
  exit 1
}

restic_run() {
  docker run --rm \
    -e RESTIC_REPOSITORY -e RESTIC_PASSWORD -e B2_ACCOUNT_ID -e B2_ACCOUNT_KEY \
    -v "$STAGING:/data:ro" \
    "$RESTIC_IMAGE" "$@"
}

log "Ensuring MSSQL backup directory exists inside the container"
docker exec "{{ backup_mssql_container }}" mkdir -p /var/opt/mssql/backup \
  || fail "could not create /var/opt/mssql/backup inside container"

log "Running MSSQL BACKUP DATABASE for {{ backup_mssql_database }}"
docker exec "{{ backup_mssql_container }}" /opt/mssql-tools18/bin/sqlcmd \
  -S localhost -U sa -P "{{ mssql_sa_password }}" -C \
  -Q "BACKUP DATABASE [{{ backup_mssql_database }}] TO DISK = N'/var/opt/mssql/backup/{{ backup_mssql_database }}.bak' WITH COMPRESSION, INIT, STATS = 25;" \
  || fail "MSSQL BACKUP DATABASE failed"

docker cp "{{ backup_mssql_container }}:/var/opt/mssql/backup/{{ backup_mssql_database }}.bak" \
  "$STAGING/{{ backup_mssql_database }}.bak" \
  || fail "docker cp of MSSQL .bak file failed"

log "Archiving Mosquitto persistence data"
MOSQUITTO_MOUNTPOINT="$(docker volume inspect "{{ backup_mosquitto_volume }}" --format '{{ '{{' }} .Mountpoint {{ '}}' }}')" \
  || fail "could not resolve {{ backup_mosquitto_volume }} volume mountpoint"
tar -C "$MOSQUITTO_MOUNTPOINT" -czf "$STAGING/mosquitto_data.tar.gz" . \
  || fail "tar of mosquitto data failed"

log "Ensuring restic repository exists"
if ! restic_run snapshots >/dev/null 2>&1; then
  log "No existing restic repository found, initializing"
  restic_run init || fail "restic init failed"
fi

log "Pushing backup to Backblaze B2"
restic_run backup /data --host "{{ inventory_hostname }}" --tag nightly \
  || fail "restic backup failed"

log "Pruning old snapshots (keep {{ backup_retention_daily }}d / {{ backup_retention_weekly }}w / {{ backup_retention_monthly }}m)"
restic_run forget --prune \
  --host "{{ inventory_hostname }}" \
  --keep-daily {{ backup_retention_daily }} \
  --keep-weekly {{ backup_retention_weekly }} \
  --keep-monthly {{ backup_retention_monthly }} \
  || log "WARNING: prune failed (non-fatal - new backup is already safe offsite)"

rm -f "$STAGING/{{ backup_mssql_database }}.bak" "$STAGING/mosquitto_data.tar.gz"

date -u +%Y-%m-%dT%H:%M:%SZ > "$STATUS_DIR/last_success"
log "Backup completed successfully"
```

- [ ] **Step 3: Write `roles/backup/tasks/main.yml`**

```yaml
---
- name: Ensure backup directories exist
  file:
    path: "{{ item }}"
    state: directory
    owner: root
    group: root
    mode: "0700"
  loop:
    - "{{ backup_root_dir }}"
    - "{{ backup_staging_dir }}"
    - "{{ backup_status_dir }}"
  when: backup_enabled | default(false)

- name: Validate required B2/restic variables
  assert:
    that:
      - backup_b2_account_id is defined
      - backup_b2_account_id | length > 0
      - backup_b2_account_key is defined
      - backup_b2_account_key | length > 0
      - backup_restic_password is defined
      - backup_restic_password | length > 0
    fail_msg: >-
      backup_enabled is true but backup_b2_account_id / backup_b2_account_key /
      backup_restic_password are missing. Set them via Ansible Vault
      (group_vars/vault.yml) before enabling backups.
  when: backup_enabled | default(false)

- name: Render backup script
  template:
    src: run-backup.sh.j2
    dest: "{{ backup_script_path }}"
    owner: root
    group: root
    mode: "0700"
  when: backup_enabled | default(false)
  no_log: true

- name: Install nightly backup cron job
  cron:
    name: "systems-one offsite backup"
    user: root
    minute: "{{ backup_schedule_minute }}"
    hour: "{{ backup_schedule_hour }}"
    job: "{{ backup_script_path }} >> {{ backup_log_path }} 2>&1"
    state: present
  when: backup_enabled | default(false)

- name: Remove backup script and cron job when disabled
  block:
    - name: Remove nightly backup cron job
      cron:
        name: "systems-one offsite backup"
        state: absent

    - name: Remove backup script
      file:
        path: "{{ backup_script_path }}"
        state: absent
  when: not (backup_enabled | default(false))
```

- [ ] **Step 4: Wire the role into `dbservers.yml`**

In `dbservers.yml`, change:

```yaml
  roles:
    - docker
    - mssql
```

to:

```yaml
  roles:
    - docker
    - mssql
    - backup
```

- [ ] **Step 5: Validate YAML and Ansible syntax**

```bash
python -c "import yaml,sys; yaml.safe_load(open('roles/backup/defaults/main.yml'))"
python -c "import yaml,sys; yaml.safe_load(open('roles/backup/tasks/main.yml'))"
python -c "import yaml,sys; yaml.safe_load(open('dbservers.yml'))"
echo "ci-placeholder" > .vault_pass
ansible-playbook site.yml --syntax-check -i production \
  -e "mssql_sa_password=test mssql_app_login_password=test grafana_admin_password=test \
      grafana_jonathan_password=test grafana_avi_password=test grafana_chris_password=test \
      grafana_pkluser_password=test mqtt_password=test grafana_git_sync_token=test \
      cloudflare_tunnel_token=test backup_b2_account_id=test backup_b2_account_key=test \
      backup_restic_password=test"
rm .vault_pass
```

Expected: all three `yaml.safe_load` calls produce no output/exit 0. The `ansible-playbook --syntax-check` command is not runnable on this Windows machine (see Global Constraints) — run the three `yaml.safe_load` checks and report that the syntax-check command was skipped for that reason. (`run-backup.sh.j2` is a Jinja template, not valid bash on its own — `bash -n` can't check it directly; its correctness is verified when Task 3's gate and a real staging run exercise it, per this plan's Testing note in Task 5.)

- [ ] **Step 6: Commit**

```bash
git add roles/backup/defaults/main.yml roles/backup/templates/run-backup.sh.j2 \
        roles/backup/tasks/main.yml dbservers.yml
git commit -m "feat(backup): add disabled-by-default MSSQL+Mosquitto backup role (B2 via restic)"
```

---

### Task 3: Add the pre-deploy backup-freshness gate

**Files:**
- Create: `roles/backup/tasks/gate.yml`
- Modify: `webservers.yml`
- Modify: `dbservers.yml`

**Interfaces:**
- Consumes: `backup_enabled`, `backup_gate_max_age_hours` (Task 1); `backup_status_dir`, `backup_script_path` (Task 2's `roles/backup/defaults/main.yml`); `/opt/backup/status/last_success` (written by Task 2's script at runtime).
- Produces: a hard `fail` in both plays' `pre_tasks` whenever `backup_enabled` is `true` and no backup has succeeded within `backup_gate_max_age_hours`.

- [ ] **Step 1: Write `roles/backup/tasks/gate.yml`**

```yaml
---
- name: Check for a successful-backup status file
  stat:
    path: "{{ backup_status_dir }}/last_success"
  register: s1_backup_status

- name: Fail if backups are enabled but none has ever succeeded
  fail:
    msg: >-
      backup_enabled is true but {{ backup_status_dir }}/last_success does not
      exist yet. Run {{ backup_script_path }} manually on the box once, confirm
      it succeeds, then re-run this playbook.
  when: not s1_backup_status.stat.exists

- name: Read last successful backup timestamp
  slurp:
    src: "{{ backup_status_dir }}/last_success"
  register: s1_backup_last_success
  when: s1_backup_status.stat.exists

- name: Compute time since last successful backup
  set_fact:
    s1_backup_age_timedelta: >-
      {{ (ansible_date_time.iso8601 | to_datetime('%Y-%m-%dT%H:%M:%SZ'))
         - (s1_backup_last_success.content | b64decode | trim | to_datetime('%Y-%m-%dT%H:%M:%SZ')) }}
  when: s1_backup_status.stat.exists

- name: Compute backup age in hours
  set_fact:
    s1_backup_age_hours: "{{ (s1_backup_age_timedelta.days * 24) + (s1_backup_age_timedelta.seconds / 3600) }}"
  when: s1_backup_status.stat.exists

- name: Fail if the last successful backup is too old
  fail:
    msg: >-
      Last successful backup was {{ s1_backup_age_hours | round(1) }} hours ago,
      exceeding backup_gate_max_age_hours ({{ backup_gate_max_age_hours }}).
      Refusing to apply changes until a fresh backup completes - run
      {{ backup_script_path }} manually, or wait for the nightly cron, then
      re-run this playbook.
  when:
    - s1_backup_status.stat.exists
    - (s1_backup_age_hours | float) > (backup_gate_max_age_hours | int)
```

- [ ] **Step 2: Import the gate into `webservers.yml`'s `pre_tasks`**

In `webservers.yml`, after the existing `Validate required variables` pre_task and before `roles:`, add:

```yaml
    - name: Enforce recent successful backup before applying changes
      import_role:
        name: backup
        tasks_from: gate.yml
      when: backup_enabled | default(false)
```

- [ ] **Step 3: Import the gate into `dbservers.yml`'s `pre_tasks`**

Same block, added after `dbservers.yml`'s existing `Validate required variables` pre_task and before `roles:`.

- [ ] **Step 4: Validate YAML and Ansible syntax**

```bash
python -c "import yaml,sys; yaml.safe_load(open('roles/backup/tasks/gate.yml'))"
python -c "import yaml,sys; yaml.safe_load(open('webservers.yml'))"
python -c "import yaml,sys; yaml.safe_load(open('dbservers.yml'))"
echo "ci-placeholder" > .vault_pass
ansible-playbook site.yml --syntax-check -i production \
  -e "mssql_sa_password=test mssql_app_login_password=test grafana_admin_password=test \
      grafana_jonathan_password=test grafana_avi_password=test grafana_chris_password=test \
      grafana_pkluser_password=test mqtt_password=test grafana_git_sync_token=test \
      cloudflare_tunnel_token=test backup_b2_account_id=test backup_b2_account_key=test \
      backup_restic_password=test"
rm .vault_pass
```

Expected: all `yaml.safe_load` checks succeed. `ansible-playbook --syntax-check` is not runnable locally (see Global Constraints) — note it was skipped for that reason. `backup_enabled` stays `false` (Task 1's default), so this gate is inert everywhere until an operator flips it on.

- [ ] **Step 5: Commit**

```bash
git add roles/backup/tasks/gate.yml webservers.yml dbservers.yml
git commit -m "feat(backup): gate Ansible runs on a recent successful backup when enabled"
```

---

### Task 4: Wire vault variables and enable the feature end to end

**Files:**
- Modify: `host_vars/sysone.yml`
- Modify: `host_vars/sysone_staging.yml`
- Modify: `.github/workflows/ci.yml`

**Interfaces:**
- Consumes: `vault_backup_b2_account_id`, `vault_backup_b2_account_key`, `vault_backup_restic_password` — **manual step, not performed by the implementer** (see Step 1).
- Produces: `backup_b2_account_id`, `backup_b2_account_key`, `backup_restic_password`, `backup_b2_path` host-scoped vars consumed by `roles/backup` (Task 2).

- [ ] **Step 1 (manual, flag to the user — do not attempt to run this):**

The user needs to add three real secrets to the encrypted vault before this feature can ever be turned on:

```bash
ansible-vault edit group_vars/vault.yml --vault-password-file .vault_pass
```

Add:

```yaml
vault_backup_b2_account_id: "<Backblaze B2 application key ID>"
vault_backup_b2_account_key: "<Backblaze B2 application key>"
vault_backup_restic_password: "<a long random passphrase - this encrypts the backups, store it somewhere outside this repo too>"
```

This requires a Backblaze B2 account, a bucket (e.g. `systems-one-backups`), and an application key scoped to it — created once in the B2 console, outside this repo's scope.

- [ ] **Step 2: Add host var references to `host_vars/sysone.yml`**

```yaml

# Offsite backup (Backblaze B2) — see roles/backup. backup_enabled itself is
# a shared default in group_vars/all.yml; flip it here once Step 1's vault
# vars are populated and you've verified {{ backup_script_path }} runs clean.
backup_b2_account_id: "{{ vault_backup_b2_account_id }}"
backup_b2_account_key: "{{ vault_backup_b2_account_key }}"
backup_restic_password: "{{ vault_backup_restic_password }}"
backup_b2_path: "sysone"
```

- [ ] **Step 3: Add host var references to `host_vars/sysone_staging.yml`**

```yaml

# Offsite backup (Backblaze B2) — see roles/backup. Same B2 account/bucket as
# production, distinct in-bucket path so snapshots never collide.
backup_b2_account_id: "{{ vault_backup_b2_account_id }}"
backup_b2_account_key: "{{ vault_backup_b2_account_key }}"
backup_restic_password: "{{ vault_backup_restic_password }}"
backup_b2_path: "sysone-staging"
```

- [ ] **Step 4: Add the three new vars to `.github/workflows/ci.yml`'s syntax-check `-e` list**

In `.github/workflows/ci.yml`, the `Syntax check site.yml` step currently ends with:

```yaml
                grafana_git_sync_token=test \
                cloudflare_tunnel_token=test"
```

Change to:

```yaml
                grafana_git_sync_token=test \
                cloudflare_tunnel_token=test \
                backup_b2_account_id=test \
                backup_b2_account_key=test \
                backup_restic_password=test"
```

- [ ] **Step 5: Validate YAML**

```bash
python -c "import yaml,sys; yaml.safe_load(open('host_vars/sysone.yml'))"
python -c "import yaml,sys; yaml.safe_load(open('host_vars/sysone_staging.yml'))"
python -c "import yaml,sys; yaml.safe_load(open('.github/workflows/ci.yml'))"
```

Expected: no output, exit 0 for all three.

- [ ] **Step 6: Commit**

```bash
git add host_vars/sysone.yml host_vars/sysone_staging.yml .github/workflows/ci.yml
git commit -m "feat(backup): wire B2/restic vault variables for sysone and sysone_staging"
```

**This commit does not turn backups on.** `backup_enabled` is still `false` (Task 1). Turning it on for real, once Step 1's vault secrets exist, is:

```bash
# on the box, after deploying with this plan merged:
ssh s1_server
cd /home/s1/Systems-One-Server
# edit host_vars/sysone.yml: backup_enabled: true
ansible-playbook -i production dbservers.yml   # installs script + cron
/opt/backup/run-backup.sh                      # run once by hand, confirm it succeeds
cat /opt/backup/status/last_success             # confirms the gate will now pass
```

---

### Task 5: Restore runbook

**Files:**
- Create: `roles/backup/README.md`

**Interfaces:** none — documentation only.

- [ ] **Step 1: Write `roles/backup/README.md`**

```markdown
# backup

Nightly offsite backup of MSSQL (`S1_Remote_Monitoring`) and Mosquitto's
persistence volume to Backblaze B2, via restic. Disabled by default
(`backup_enabled: false` in `group_vars/all.yml`).

See `docs/superpowers/specs/2026-08-11-offsite-backup-b2-design.md` for the
full design rationale.

## Enabling

1. Create a Backblaze B2 bucket and an application key scoped to it.
2. `ansible-vault edit group_vars/vault.yml` and add `vault_backup_b2_account_id`,
   `vault_backup_b2_account_key`, `vault_backup_restic_password`.
3. Set `backup_enabled: true` in the target host's `host_vars/<host>.yml`.
4. `ansible-playbook -i production dbservers.yml` — installs the script and cron job.
5. Run `/opt/backup/run-backup.sh` by hand once and confirm it exits 0 and
   `/opt/backup/status/last_success` appears. From this point on, every
   `webservers.yml`/`dbservers.yml` run will refuse to proceed if the last
   successful backup is older than `backup_gate_max_age_hours` (default 30).

## Disabling

Set `backup_enabled: false` and re-run `dbservers.yml` — this removes the
cron job and the script (status files under `/opt/backup/status` are left in
place, harmless, and picked back up if re-enabled).

## Restoring after a disaster

1. Provision a fresh box, run Ansible through `webservers.yml`/`dbservers.yml`
   with `backup_enabled: false` — brings up empty `mssql`/`mosquitto` containers.
2. Restore the latest snapshot from B2 into a scratch directory:

   ```bash
   mkdir -p /opt/backup/restore
   docker run --rm \
     -e RESTIC_REPOSITORY="b2:<bucket>:<path>" \
     -e RESTIC_PASSWORD="<restic password from vault>" \
     -e B2_ACCOUNT_ID="<id from vault>" -e B2_ACCOUNT_KEY="<key from vault>" \
     -v /opt/backup/restore:/restore \
     restic/restic:0.17 restore latest --target /restore
   ```

3. Restore MSSQL:

   ```bash
   docker cp /opt/backup/restore/data/S1_Remote_Monitoring.bak mssql:/var/opt/mssql/backup/
   docker exec mssql /opt/mssql-tools18/bin/sqlcmd \
     -S localhost -U sa -P "<mssql_sa_password>" -C \
     -Q "RESTORE DATABASE [S1_Remote_Monitoring] FROM DISK = N'/var/opt/mssql/backup/S1_Remote_Monitoring.bak' WITH REPLACE;"
   ```

4. Restore Mosquitto (container must be stopped first so nothing is writing
   to the volume while it's being replaced):

   ```bash
   cd /opt/mqtt && docker compose stop mosquitto
   MOSQUITTO_MOUNTPOINT=$(docker volume inspect mosquitto_data --format '{{ .Mountpoint }}')
   tar -C "$MOSQUITTO_MOUNTPOINT" -xzf /opt/backup/restore/data/mosquitto_data.tar.gz
   docker compose start mosquitto
   ```

5. Re-run the full site (`ansible-playbook -i production site.yml`) to
   reconcile everything else (Grafana, Node-RED, etc.) from this repo.

**Test this runbook on staging before you ever need it for real** — a
snapshot in B2 that's never been restored is not a verified backup.
```

- [ ] **Step 2: Commit**

```bash
git add roles/backup/README.md
git commit -m "docs(backup): add restore runbook"
```

---

## Self-Review

**Spec coverage:**
- Nightly encrypted offsite backup of both data stores to B2 → Task 2.
- Disabled-by-default, enable-when-ready toggle → Task 1 + Task 2's `when: backup_enabled` guards throughout.
- Pre-deploy gate requiring a successful backup → Task 3.
- Restore path documented → Task 5.
- Vault/secrets wiring, CI syntax-check parity → Task 4.

**Placeholder scan:** no TBD/TODO markers; every step has concrete file content. The one deliberately unfilled input is real B2 credentials (Task 4, Step 1) — that's a manual, out-of-repo action, explicitly flagged as such rather than a placeholder to fill in later.

**Type/name consistency:** `backup_script_path`, `backup_status_dir`, `backup_enabled`, `backup_gate_max_age_hours`, `backup_b2_account_id`, `backup_b2_account_key`, `backup_restic_password` are defined once (Task 1 or Task 2's defaults) and referenced identically in Tasks 2-5.
