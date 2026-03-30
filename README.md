# Systems-One-Server (Ansible)

This repository contains Ansible inventories, variables, and playbooks for provisioning and deploying services via roles.

## Current state (what this repo does)

Playbooks are split into two tiers:

- **Web tier**: installs Docker and deploys Cloudflare Tunnel (`cloudflared`) via Docker.
- **DB tier**: installs Docker and deploys Microsoft SQL Server (Developer) via Docker.

A top-level `site.yml` playbook runs both tiers.

## Repository layout

- `ansible.cfg`
  - Default Ansible config for this repo.
  - Defaults `inventory` to `production`.

- `production`
  - Production inventory (INI-style).
  - Defines `webservers` and `dbservers` host groups.

- `staging`
  - Staging inventory (INI-style).
  - Same group structure as production.

- `site.yml`
  - Master playbook.
  - Imports `webservers.yml` and `dbservers.yml`.

- `webservers.yml`
  - Runs against the `webservers` group.
  - Applies roles: `docker`, `cloudflared`.

- `dbservers.yml`
  - Runs against the `dbservers` group.
  - Applies roles: `docker`, `mssql`.

- `group_vars/`
  - Group-scoped variables.
  - `dbservers.yml`: database-tier variables (e.g. `mssql_port`).
  - `vault.yml`: encrypted variables file (Ansible Vault). It is **not** auto-loaded; it is included explicitly by the tier playbooks.

- `host_vars/`
  - Per-host variables.
  - Example: `sysone.yml` contains host connection settings and host-specific values.

- `roles/`
  - Roles used by the playbooks.
  - `docker`: Docker installation.
  - `cloudflared`: Cloudflare Tunnel deployment (Docker Compose template).
  - `mssql`: MSSQL deployment (Docker Compose template).

## Prerequisites

- Ansible installed on your control machine.
- SSH connectivity to target hosts.
- If using Vaulted variables: a vault password available via `--ask-vault-pass` or `--vault-password-file`.

## How to use

### 1) Edit inventory

Add hosts to `production` and/or `staging` under the appropriate groups:

```ini
[webservers]
my-web-1

[dbservers]
my-db-1
```

### 2) Add host connection details

Create or update files in `host_vars/` matching each inventory hostname.

Example: `host_vars/my-web-1.yml`

```yaml
ansible_host: 192.168.1.10
ansible_user: ubuntu
ansible_ssh_private_key_file: /home/you/.ssh/id_ed25519
```

### 3) Run the playbooks

Run everything (web + db) in **production** (default inventory):

```bash
ansible-playbook site.yml
```

Run everything in **staging**:

```bash
ansible-playbook -i staging site.yml
```

## Grafana: dashboards + orgs/users as code

This repo provisions Grafana via Docker Compose and supports two approaches:

- **Dashboards/datasources**: file provisioning from this repo (portable across machines).
- **Orgs/users/teams**: optional API provisioning via Ansible (portable across machines).

You can also enable Grafana's experimental Git Sync feature (Grafana v12+) to sync **dashboards and folders** with a GitHub repository.

### Git Sync (Grafana v12 experimental)

To enable the Provisioning UI required for Git Sync in this deployment, set in host/group vars:

```yaml
grafana_git_sync_enabled: true

# Optional but recommended if you plan to use webhooks / preview links:
# grafana_root_url: "https://grafana.example.com/"
```

Then redeploy Grafana.

After Grafana restarts, configure Git Sync in the UI:

- Administration -> Provisioning -> Configure Git Sync

Note: When `grafana_git_sync_enabled` is true, this role skips the legacy file-based dashboard provisioning (dashboard JSON copy + provider) to avoid confusion. Datasource provisioning remains enabled.

### Dashboards (file provisioning)

Dashboard JSON files live under:

- [roles/grafana/files/dashboards](roles/grafana/files/dashboards)

They are mounted read-only into Grafana and loaded via the provisioning file template.

#### Export dashboards from Grafana back into the repo

Grafana does not automatically push UI changes back into Git. The typical workflow is:

1) Build/update dashboards in the Grafana UI
2) Export dashboards to JSON
3) Commit the JSON into this repo

To make step (2) fast, use the export script:

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -r tools/requirements.txt

# Example (through SSH port-forward):
python tools/grafana_export_dashboards.py \
  --url http://127.0.0.1:3000 \
  --username admin \
  --password '***' \
  --out-dir roles/grafana/files/dashboards \
  --overwrite
```

Then re-run the playbook to sync dashboards onto another host.

### Orgs/users/teams (API provisioning)

Grafana does not support declarative provisioning of orgs/users purely via provisioning files. To make orgs/users portable, this repo includes an optional Ansible step that calls the Grafana HTTP API after Grafana starts.

Configure in host/group vars:

```yaml
grafana_api_provision_enabled: true

grafana_orgs:
  - name: "Main"

grafana_users:
  - login: "alice"
    email: "alice@example.com"
    name: "Alice"
    password: "{{ vault_grafana_alice_password }}"
    orgs:
      - name: "Main"
        role: "Editor"

grafana_teams:
  - org: "Main"
    name: "SRE"
    members: ["alice"]
```

Notes:

- Keep user passwords in Vault (recommended).
- If you use SSO (Google/GitHub/OIDC/LDAP), it is usually better to manage access there rather than in Grafana local users.

Run only the web tier:

```bash
ansible-playbook webservers.yml
# or staging:
ansible-playbook -i staging webservers.yml
```

Run only the DB tier:

```bash
ansible-playbook dbservers.yml
# or staging:
ansible-playbook -i staging dbservers.yml
```

### 4) Using Ansible Vault

This repo uses an encrypted vars file at `group_vars/vault.yml`.

To run playbooks that reference vaulted variables:

```bash
ansible-playbook site.yml --ask-vault-pass
```

Or with a password file:

```bash
ansible-playbook site.yml --vault-password-file /path/to/vault-pass.txt
```

### 5) Validate what Ansible sees

List inventory and variables:

```bash
ansible-inventory --list -y
```

Inspect one host:

```bash
ansible-inventory --host <hostname> -y
```

## Keeping flows & dashboards in sync

Ansible pushes configuration *to* the server. Changes made in the Node-RED or Grafana UI need to be pulled *back* into this repo before the next playbook run overwrites them.

### Option A — Pull script (works always)

```bash
# Pull Node-RED flows only
python3 tools/sync_nodered_flows.py --host 192.168.1.110 --user s1

# Pull flows + Grafana dashboards and auto-commit
python3 tools/sync_nodered_flows.py \
    --host 192.168.1.110 --user s1 \
    --grafana-url http://127.0.0.1:3000 --grafana-password '<admin-password>' \
    --commit
```

### Option B — Node-RED Projects mode (git-native)

Enable in host/group vars:

```yaml
nodered_projects_enabled: true
nodered_credential_secret: "{{ vault_nodered_credential_secret }}"
nodered_git_user_name: "Jonathan"
nodered_git_user_email: "jonathan@example.com"
```

Then in the Node-RED UI:
1. You will see a **Projects** screen on first load
2. Create a new project or clone from GitHub
3. Point it to `https://github.com/Jwagener1/Systems-One-Server`
4. Enter your GitHub credentials/token
5. Node-RED will commit and push flow changes automatically

**Note:** When `nodered_projects_enabled: true`, Ansible skips copying `flows.json` — the git project is the source of truth.

### Option C — Grafana Git Sync (Grafana v12+)

Grafana dashboards live in a **dedicated repo**: [https://github.com/Jwagener1/grafana](https://github.com/Jwagener1/grafana)

The repo layout is:
```
grafana/
  admin_panel.json
  machine_detail.json
  PEPKOR/
    device_drill_down.json
    pepkor_overview.json
  Provisioned/
```

The defaults in this repo already point Git Sync at the correct repo and path. Just supply a GitHub token in vault and enable it:

```yaml
grafana_git_sync_enabled: true
grafana_git_sync_token: "{{ vault_github_token }}"
```

Ansible will configure Git Sync automatically via the Grafana API after deploy. Dashboard changes in the Grafana UI are committed and pushed to `https://github.com/Jwagener1/grafana` automatically.

> **Note:** `grafana_dashboard_folders_from_files: true` is set by default so subfolders like `PEPKOR/` become Grafana folders automatically.

## Notes / conventions

- Hostnames in `production` / `staging` should match filenames in `host_vars/` (e.g. `sysone` → `host_vars/sysone.yml`).
- Group variables go into `group_vars/<groupname>.yml` (e.g. `group_vars/dbservers.yml`).
- `group_vars/vault.yml` is intentionally not auto-loaded to avoid requiring a vault password for commands like `ansible-inventory --list`.
