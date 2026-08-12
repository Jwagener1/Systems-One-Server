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
4. `ansible-playbook -i production dbservers.yml -vv` — installs the script and
   cron job (the pre-deploy gate will only warn, not block, on this first run,
   since the script doesn't exist yet for it to have run). Check the `-vv`
   output for a non-skipped "Render backup script" task to confirm the script
   was actually installed, not silently skipped by a `when:` guard.
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

   Replace <bucket>/<path> with the real values — systems-one-backups / sysone for production, sysone-staging for staging.
   ```bash
   mkdir -p /opt/backup/restore
   docker run --rm \
     -e RESTIC_REPOSITORY="b2:<bucket>:<path>" \
     -e RESTIC_PASSWORD="<restic password from vault>" \
     -e B2_ACCOUNT_ID="<id from vault>" -e B2_ACCOUNT_KEY="<key from vault>" \
     -v /opt/backup/restore:/restore \
     restic/restic:0.17 restore latest --host sysone --target /restore
   ```

3. Restore MSSQL. The backup directory inside the container only exists
   because the nightly script creates it (`mkdir -p /var/opt/mssql/backup`)
   — a fresh container doesn't have it yet, so create it first:

   ```bash
   docker exec mssql mkdir -p /var/opt/mssql/backup
   docker cp /opt/backup/restore/data/S1_Remote_Monitoring.bak mssql:/var/opt/mssql/backup/
   docker exec mssql /opt/mssql-tools18/bin/sqlcmd \
     -S localhost -U sa -P "<mssql_sa_password>" -C \
     -Q "RESTORE DATABASE [S1_Remote_Monitoring] FROM DISK = N'/var/opt/mssql/backup/S1_Remote_Monitoring.bak' WITH REPLACE;"
   ```

   The restored database carries the old server's login SIDs. `roles/mssql`'s
   bootstrap creates fresh logins on the new box with new SIDs, so the restored
   database's users are now orphaned — Grafana, Node-RED, and
   `systems_one_ingest` will fail to authenticate until you remap them:

   ```bash
   docker exec mssql /opt/mssql-tools18/bin/sqlcmd \
     -S localhost -U sa -P "<mssql_sa_password>" -C \
     -Q "USE [S1_Remote_Monitoring]; ALTER USER [<mssql_rm_admin_login>] WITH LOGIN = [<mssql_rm_admin_login>];"
   ```

   (Repeat for any other application login the restored database has users for.)

4. Restore Mosquitto (container must be stopped first so nothing is writing
   to the volume while it's being replaced). The volume name is resolved by
   filter, not assumed exact, matching `backup_mosquitto_volume` and the guard
   logic in `run-backup.sh.j2`:

   ```bash
   cd /opt/mqtt && docker compose stop mosquitto
   MOSQUITTO_VOLUME=$(docker volume ls -q --filter "name=mosquitto_data")
   if [ -z "$MOSQUITTO_VOLUME" ] || [ "$(echo "$MOSQUITTO_VOLUME" | wc -l)" -gt 1 ]; then
     echo "could not uniquely resolve the mosquitto volume: '$MOSQUITTO_VOLUME'" >&2
     exit 1
   fi
   MOSQUITTO_MOUNTPOINT=$(docker volume inspect "$MOSQUITTO_VOLUME" --format '{{ .Mountpoint }}')
   tar -C "$MOSQUITTO_MOUNTPOINT" -xzf /opt/backup/restore/data/mosquitto_data.tar.gz
   docker compose start mosquitto
   ```

5. Re-run the full site (`ansible-playbook -i production site.yml`) to
   reconcile everything else (Grafana, Node-RED, etc.) from this repo.

**Test this runbook on staging before you ever need it for real** — a
snapshot in B2 that's never been restored is not a verified backup.
