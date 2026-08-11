# Offsite Backup to Backblaze B2 — Design

## Context

`sysone` (and `sysone_staging`) is a single production box — Ansible manages it with `ansible_connection: local`, and both the `webservers` and `dbservers` inventory groups point at the same host. Two data stores live there, both as Docker volumes on that one disk:

- **MSSQL** (`roles/mssql`) — database `S1_Remote_Monitoring`, the durable telemetry/report data. Named volume `mssql_data`.
- **Mosquitto** (`roles/mqtt`) — MQTT retained-message/subscription persistence. Named volume `mosquitto_data`.

There is currently **no backup mechanism at all** — no dumps, no snapshots, no offsite copy. If `sysone` is lost (disk failure, provider incident, accidental `docker volume rm`, etc.), both data stores are gone permanently. Everything else (containers, config, Grafana dashboards, Node-RED flows via the `grafana`/`nodered` roles) is already reproducible from this repo; the two data stores are the only state that isn't.

Deploys today are manual: SSH to the box, `git merge`, run `ansible-playbook -i production webservers.yml` or `dbservers.yml` by hand, often scoped with `--tags <role>`. A `deploy.yml` GitHub Actions workflow is designed (`docs/superpowers/specs/2026-08-11-cicd-resilience-deploy-rollback-design.md`) but not yet built — so a backup gate cannot live in CI; it must live in the Ansible plays themselves, since those are the only thing every deploy path (manual today, `deploy.yml` later) has in common.

## Goals

1. Nightly, automated, encrypted, offsite backup of both data stores to Backblaze B2.
2. Ship as a disabled-by-default feature (`backup_enabled: false`) so it can be reviewed and deployed without immediately going live — flipped on once B2 credentials exist and the operator is ready.
3. Once enabled, refuse to apply further Ansible changes to the box unless a successful backup has completed recently — "no updates against unprotected data."
4. A documented, tested restore path — a backup nobody has restored from is not a backup.

## Non-goals

- Point-in-time / continuous replication. Nightly RPO is acceptable for this workload (analytics/reporting DB, not a transactional system of record with sub-day RPO requirements).
- Backing up MSSQL system databases (`master`, `msdb`) — the container and its bootstrap are fully reproducible from `roles/mssql`; only the application database (`S1_Remote_Monitoring`) has state that isn't in this repo.
- A second live server / hot standby. This is backup, not HA.

## Design

### What gets backed up, and how

**MSSQL** — native `BACKUP DATABASE ... WITH COMPRESSION` inside the `mssql` container, run via `sqlcmd` (mirrors the pattern already used for bootstrap in `roles/mssql/tasks/main.yml`). This is the SQL-Server-correct way to get a consistent, restorable backup — never copy `mssql_data`'s raw files while the engine is running.

**Mosquitto** — `tar` of the Mosquitto persistence volume's mountpoint, taken live without stopping the container. The volume is declared as `mosquitto_data` in `roles/mqtt/templates/docker-compose.mqtt.yml.j2`, but Docker Compose prefixes volume names with its project name when no explicit `name:` override exists (none does here), so the actual runtime volume is `mqtt_mosquitto_data`. The script resolves it dynamically via `docker volume ls -q --filter "name=mosquitto_data"` (substring match, with zero/multi-match guards) rather than assuming the exact name, then `docker volume inspect --format '{{ .Mountpoint }}'` on whatever that resolves to. Mosquitto's persistence file is autosaved via rename-on-write, so a live copy is safe in practice for this use case (retained-message/subscription state, not transactional data); noted here as a deliberate tradeoff against briefly stopping MQTT ingest for every nightly backup.

**Offsite transport** — [restic](https://restic.net/) run as an ephemeral `docker run --rm restic/restic ...` container (same one-off-container pattern the `mqtt` role already uses for `mosquitto_passwd`), targeting B2 natively via `restic -r b2:<bucket>:<path>`. Restic encrypts client-side with a repository password independent of B2's own access controls, dedupes, and prunes on a retention policy. No restic binary gets installed on the host — everything runs through Docker, consistent with how this repo runs every other piece of software.

### Feature toggle

`backup_enabled` (default `false`) lives in `group_vars/all.yml`, alongside the other shared toggles like `docker_shared_network`. When `false`:
- The `backup` role installs nothing (no script, no cron entry — and removes them if previously installed, so toggling off is a clean rollback).
- The pre-deploy gate (below) is skipped entirely.

Turning it on is a two-step operator action: add real B2 credentials to `group_vars/vault.yml` (`ansible-vault edit`), then set `backup_enabled: true` in `host_vars/sysone.yml` (and separately for staging). This repo change ships the capability; it does not turn it on.

### Pre-deploy gate

Both `webservers.yml` and `dbservers.yml` already have a `pre_tasks` block (vault load + `assert` on required vars) — every deploy path, manual or future-CI, goes through one of these two plays. The gate is added to both, as an `import_role: {name: backup, tasks_from: gate.yml}` guarded by `when: backup_enabled | default(false)`.

The gate checks **freshness of the last successful backup** (a status file the backup script writes only after the MSSQL backup, the mosquitto archive, and the restic push all succeed) rather than running a live backup synchronously on every deploy:
- No status file yet → fail with a clear message ("run the script manually once").
- Status file older than `backup_gate_max_age_hours` (default 30 — covers the nightly cadence plus slack) → fail, telling the operator to run a fresh backup or wait for cron.
- Fresh → pass silently.

**Why freshness-check instead of "run a backup right now on every deploy":** deploys here are frequently scoped (`--tags s1_reporter`) and can happen several times during active work on a single feature. Forcing a live `BACKUP DATABASE` + offsite push before every one of those would slow down routine config changes for no safety benefit beyond what a nightly cadence already provides. A freshness gate is simple and matches the existing `assert`-based fail pattern in this codebase.

**Two correctness fixes made after the final whole-branch review, both worth recording:**
- **Tag scoping bypassed the gate entirely.** `pre_tasks` without `tags: always` are skipped when a run is scoped with `--tags` — exactly this repo's normal deploy shape. The gate's `import_role` now carries `tags: always`, matching the existing "Load vaulted variables" pre_task in the same files, which already needed this for the same reason.
- **The gate deadlocked its own bootstrap.** The original "no status file" check failed unconditionally, including on the very first run that enables `backup_enabled` — but that first run is also the one that installs `run-backup.sh` in the first place (via the `roles:` list, which executes after `pre_tasks`). The gate now checks whether the script is already installed (`stat` on `backup_script_path`) before deciding whether "no status file yet" is a hard failure (script installed, should have run by now) or just informational (script not installed yet, this run will install it).
- Also: the age computation must not round-trip a `timedelta` object through an intermediate `set_fact` — Ansible's classic (non-native) Jinja templating stringifies it, breaking `.days`/`.seconds` attribute access on the next task. Compute hours in one expression via `.total_seconds() / 3600` instead.

### Layout on disk

```
/opt/backup/
├── run-backup.sh        # rendered from template, invoked by cron
├── staging/              # local .bak + .tar.gz land here before the restic push
├── status/
│   ├── last_success       # UTC ISO8601 timestamp, written only on full success
│   └── last_failure        # appended on every failure, for diagnosis
└── backup.log             # cron stdout/stderr
```

### Restore runbook (disaster recovery)

1. Provision a fresh box, run Ansible through `webservers.yml`/`dbservers.yml` with `backup_enabled: false` — this brings up empty `mssql`/`mosquitto` containers via the existing roles.
2. Restore from B2:
   ```bash
   docker run --rm \
     -e RESTIC_REPOSITORY="b2:<bucket>:<path>" \
     -e RESTIC_PASSWORD="<restic password>" \
     -e B2_ACCOUNT_ID="<id>" -e B2_ACCOUNT_KEY="<key>" \
     -v /opt/backup/restore:/restore \
     restic/restic:0.17 restore latest --target /restore
   ```
3. MSSQL: `docker cp /opt/backup/restore/data/S1_Remote_Monitoring.bak mssql:/var/opt/mssql/backup/` then
   `RESTORE DATABASE [S1_Remote_Monitoring] FROM DISK = N'/var/opt/mssql/backup/S1_Remote_Monitoring.bak' WITH REPLACE;` via `sqlcmd`.
4. Mosquitto: stop the `mosquitto` container, resolve the real volume name via `docker volume ls -q --filter "name=mosquitto_data"` (see note above — it won't be the exact string `mosquitto_data`), extract `mosquitto_data.tar.gz` into that volume's mountpoint, restart.
5. Re-run the full site to reconcile everything else (Grafana, Node-RED, etc.) from this repo as usual.

This is captured in full, copy-pasteable form in `roles/backup/README.md` (Task in the implementation plan), not just here, so it's discoverable at the point someone actually needs it under pressure.

## Testing

- `bash -n` the rendered script template for syntax validity (no live B2/MSSQL in CI).
- `ansible-playbook --syntax-check` continues to pass with the three new vaulted vars added as `-e` test overrides, matching the existing pattern for `mssql_sa_password` etc.
- Manual, on staging first: flip `backup_enabled: true`, run the play, confirm `/opt/backup/status/last_success` appears, confirm a snapshot exists in the B2 bucket (`restic snapshots`), then do one full restore-runbook dry run against a scratch directory to prove the backup is actually restorable — not just present.

## Out of scope

- Auto-provisioning the B2 bucket/application key (operator does this once in the B2 console).
- Encrypting data in transit beyond what B2's HTTPS API and restic's client-side encryption already provide.
- Alerting on backup failure beyond the pre-deploy gate and `/opt/backup/status/last_failure` (a future Teams-notification hook is a natural follow-up, given `s1_reporter`'s existing Teams integration, but is separate scope).
