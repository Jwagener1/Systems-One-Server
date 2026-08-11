# Vault Variables Reference

All secrets are stored in `group_vars/vault.yml` (Ansible Vault encrypted).
To edit: `ansible-vault edit group_vars/vault.yml`

## Required Variables

| Variable | Description |
|---|---|
| `vault_mssql_sa_password` | MSSQL SA password |
| `vault_grafana_admin_password` | Grafana `admin` user password |
| `vault_grafana_jonathan_password` | Grafana `jonathan` user password |
| `vault_grafana_avi_password` | Grafana `avi` user password |
| `vault_grafana_chris_password` | Grafana `chris` user password |
| `vault_grafana_pkluser_password` | Grafana `pkluser` (PKL User Group) password |
| `vault_grafana_cust_bex_password` | Grafana `cust_bex` (BEX customer login) password |
| `vault_grafana_cust_dcb_password` | Grafana `cust_dcb` (DCB customer login) password |
| `vault_grafana_cust_madibana_password` | Grafana `cust_madibana` (MADIBANA customer login) password |
| `vault_grafana_cust_pep_password` | Grafana `cust_pep` (PEP customer login) password |
| `vault_grafana_cust_pep_africa_password` | Grafana `cust_pep_africa` (PEP AFRICA customer login) password |
| `vault_grafana_cust_snowsoft_password` | Grafana `cust_snowsoft` (SNOWSOFT customer login) password |
| `vault_grafana_git_sync_token` | GitHub PAT for Grafana Git Sync (repo: Jwagener1/grafana) |
| `vault_cloudflare_tunnel_token` | Cloudflare tunnel token |

Note: a `cust_pepkor` Grafana account also exists on the live server but is
intentionally left out of Ansible management (not vault-backed, password
untouched) — see the 2026-08-11 vault rotation for context.

## Grafana Git Sync Notes

- Dashboard repo: https://github.com/Jwagener1/grafana
- Branch: `main`
- Path: `grafana/`
- Folder structure:
  - `grafana/Admin/` — internal dashboards (admin_panel, Machine Detail)
  - `grafana/PEPKOR/` — PEPKOR client dashboards

## Grafana Org/User Notes

- Single org: `Main Org.`
- `pkluser` = PKL User Group shared login (Viewer, PEPKOR folder only)
- `avi`, `chris` = Viewers (all folders)
- `jonathan` = Admin

## s1_reporter

| Variable | Description |
|---|---|
| `vault_s1_reporter_teams_webhook_url` | Power Automate Workflow webhook URL that posts Adaptive Cards into the Systems-One → Reporting Teams channel |

The legacy `vault_s1_reporter_smtp_user`/`_smtp_pass`/`_report_to` vars have
been fully removed — no code references them and they're gone from vault.yml
as of the 2026-08-11 vault rotation.

## Vault Recovery (2026-08-11)

The vault password was lost. All secrets above were recovered from the live
server's running containers (MSSQL, Cloudflare tunnel, existing Grafana
users) or rotated to new values (Grafana user/customer passwords, git-sync
token) and re-encrypted under a new vault password. If you need the current
vault password, ask Jonathan — it is not stored in this repo (only
`.vault_pass`, gitignored, holds it on each machine that needs to run
Ansible non-interactively).
