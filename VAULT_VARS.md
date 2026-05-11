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
| `vault_grafana_git_sync_token` | GitHub PAT for Grafana Git Sync (repo: Jwagener1/grafana) |
| `vault_cloudflare_tunnel_token` | Cloudflare tunnel token |
| `vault_openclaw_gateway_token` | OpenClaw gateway auth token |
| `vault_openclaw_msteams_app_password` | MS Teams bot app password |

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
| `vault_s1_reporter_smtp_user` | Gmail address used to send reports |
| `vault_s1_reporter_smtp_pass` | Gmail app password |
| `vault_s1_reporter_report_to` | Email address to send reports to |
