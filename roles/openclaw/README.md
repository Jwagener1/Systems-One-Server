# openclaw role

Installs and configures [OpenClaw](https://docs.openclaw.ai) — the AI gateway agent — as a systemd service.

## What it does

1. Installs Node.js (via NodeSource, optional)
2. Installs OpenClaw globally via npm
3. Renders `openclaw.json` config from vars
4. Optionally installs MS Teams plugin
5. Installs and starts the OpenClaw gateway systemd service

## Required vars

| Variable | Description |
|---|---|
| `openclaw_gateway_auth_token` | Gateway auth token — store in Ansible Vault |
| `openclaw_msteams_app_id` | MS Teams App ID (if `openclaw_msteams_enabled: true`) |
| `openclaw_msteams_app_password` | MS Teams App Password (vault) |
| `openclaw_msteams_tenant_id` | MS Teams Tenant ID |

## Example host_vars

```yaml
openclaw_msteams_enabled: true
openclaw_gateway_auth_token: "{{ vault_openclaw_gateway_token }}"
openclaw_msteams_app_id: "{{ vault_msteams_app_id }}"
openclaw_msteams_app_password: "{{ vault_msteams_app_password }}"
openclaw_msteams_tenant_id: "{{ vault_msteams_tenant_id }}"
```

## Notes

- The GitHub Copilot auth token is set up interactively via `openclaw auth login` after first install — it cannot be automated via config file (OAuth device flow).
- All secrets should be stored in `group_vars/vault.yml` (Ansible Vault encrypted).
