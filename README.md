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
