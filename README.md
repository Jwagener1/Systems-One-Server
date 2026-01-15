# server-infra (Ansible)

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

## Notes / conventions

- Hostnames in `production` / `staging` should match filenames in `host_vars/` (e.g. `sysone` → `host_vars/sysone.yml`).
- Group variables go into `group_vars/<groupname>.yml` (e.g. `group_vars/dbservers.yml`).
- `group_vars/vault.yml` is intentionally not auto-loaded to avoid requiring a vault password for commands like `ansible-inventory --list`.
