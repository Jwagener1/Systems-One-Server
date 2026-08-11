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
   MOSQUITTO_VOLUME=$(docker volume ls -q --filter "name=mosquitto_data")
   MOSQUITTO_MOUNTPOINT=$(docker volume inspect "$MOSQUITTO_VOLUME" --format '{{ .Mountpoint }}')
   tar -C "$MOSQUITTO_MOUNTPOINT" -xzf /opt/backup/restore/data/mosquitto_data.tar.gz
   docker compose start mosquitto
   ```

5. Re-run the full site (`ansible-playbook -i production site.yml`) to
   reconcile everything else (Grafana, Node-RED, etc.) from this repo.

**Test this runbook on staging before you ever need it for real** — a
snapshot in B2 that's never been restored is not a verified backup.
