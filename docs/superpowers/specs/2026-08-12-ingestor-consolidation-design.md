# MQTT Ingestor Consolidation — Design

## Context

`sysone` currently runs three independent processes that all consume MQTT traffic from the same `mosquitto` broker and write to the same `S1_Remote_Monitoring` MSSQL database:

- **`systems_one_ingest`** — the only one of the three that's Ansible-managed and in this repo (`roles/systems_one_ingest`). Subscribes to `systems-one/#` and `systemsone/#`. Writes to `dbo.devices`, `dbo.device_status`, `dbo.device_os_status`, `dbo.device_storage_status`, `dbo.device_statistics`. No durability: messages are processed synchronously in the MQTT callback, and a DB failure just logs and drops the message.
- **`mqtt-ingestor`** — a separate GitHub repo (`Jwagener1/mqtt-ingestor`), hand-deployed via `docker-compose` at `/home/s1/mqtt-ingestor`, never brought into Ansible. Subscribes to `systems-one/#` (the dash variant only — it misses `systemsone/#`, which `systems_one_ingest` does catch). Writes to the same `dbo.devices` family of tables **plus** three more that `systems_one_ingest` doesn't touch at all: `dbo.device_application_status`, `dbo.device_uptime_status`, `dbo.device_os_metrics`. Has real production-grade resilience: a durable SQLite WAL spool, a batched async writer with retry/backoff, dead-lettering to `ingest.telemetry_deadletter`, Prometheus metrics, and a health endpoint.
- **`broker-ingestor`** — no git repo anywhere; its source only exists on the server at `/home/s1/broker-ingestor`. Subscribes to Mosquitto's own `$SYS/#` health-stats topic space (a genuinely different concern — broker health, not device telemetry) and writes periodic snapshots to `broker.broker_stats`. Same architectural shape as `mqtt-ingestor` (spool-less version) but simpler.

This was discovered and investigated in a previous session (see `[[ingestor-consolidation-plan]]` project memory) and paused deliberately: `broker-ingestor` does a genuinely different job and isn't a duplicate, but `mqtt-ingestor` and `systems_one_ingest` do overlap — same topic space, same database, same tables for the overlapping concerns — flagged as an open question needing real design work rather than a rushed fix.

Discovered in this session, while auditing the server after a production deploy: `mqtt-ingestor`'s device identity model is also more robust than `systems_one_ingest`'s. It keys devices by `(customer, location, machine_name)` and auto-registers a placeholder serial (`UNKNOWN-<machine>-<location>`) immediately, upgrading it once a real serial arrives — so a device that reports status before its first serial-bearing message still gets tracked. `systems_one_ingest` requires a valid serial up front.

## Goals

1. One Ansible-managed ingest service instead of three independently-running processes with overlapping/duplicated consumption of the same topics.
2. Keep `mqtt-ingestor`'s resilience (spool, retry, dead-letter, metrics, health) as the foundation — it's the more production-grade of the three, not `systems_one_ingest`.
3. Extend that foundation to also consume `systemsone/#` (the coverage gap `mqtt-ingestor` currently has) and `$SYS/#` (folding in `broker-ingestor`'s job), so a single process covers everything the three separate ones do today.
4. Preserve the `INGEST_ALLOWED_CUSTOMERS`/`INGEST_ALLOWED_LOCATIONS` allowlist fix (`roles/systems_one_ingest/files/app/mqtt_ingest/validation.py`, shipped 2026-08-12) that stopped `systems_one_ingest` from silently dropping unknown customers — this must not regress in the merge.
5. Cut over safely: no window where telemetry is silently dropped, no duplicate-write corruption, an unsubscribe of the retired `mqtt-ingestor` client ID so Mosquitto doesn't keep queuing for a dead persistent session.

## Non-goals

- Rewriting the resilience architecture itself (spool/retry/dead-letter design) — reuse `mqtt-ingestor`'s as-is.
- Touching `broker-ingestor`'s actual `$SYS/#` parsing logic beyond moving it into the shared process — its existing logic is correct, just under-resourced (no spool).
- Any change to `dbo.*`/`broker.*` table schemas beyond what's needed to route through the shared spool.

## Architecture

One new Ansible role, `roles/mqtt_ingestor` (replacing the never-committed vendored copy sitting uncommitted in a prior worktree), deploying **one process, one MQTT connection, one container**, built on `mqtt-ingestor`'s pipeline shell.

**Topic subscriptions:** `systems-one/#`, `systemsone/#`, `$SYS/#` — all three, in one client.

**Routing:** each inbound message is classified by topic prefix into one of two logical pipelines, both sharing the same spool → batched writer → retry → dead-letter path:

- **Device telemetry** (`systems-one(-)/#`) → `dbo.*` tables, via `mqtt-ingestor`'s existing dispatcher (`db.py`'s `_dispatch_entry`), which already covers every table `systems_one_ingest` writes to plus three more (`device_application_status`, `device_uptime_status`, `device_os_metrics`). `systems_one_ingest`'s own store modules are not ported — they're a strict subset of what's already here.
- **Broker health** (`$SYS/#`) → `broker.broker_stats`, via `broker-ingestor`'s existing writer, now riding the shared spool instead of writing directly (closing its current lack of durability).

**Device identity:** `mqtt-ingestor`'s `(customer, location, machine_name)` key with placeholder-then-upgrade serial resolution carries over unchanged — it's strictly more robust than `systems_one_ingest`'s serial-first approach.

**Validation:** `validation.py`'s `INGEST_ALLOWED_CUSTOMERS`/`INGEST_ALLOWED_LOCATIONS` allowlist becomes an early gate in `db.py`'s `_dispatch_entry`, run *before* `_resolve_device` — if either allowlist is set and the message's customer/location isn't in it, the message is dropped (allow-all-by-default semantics preserved, matching today's fixed behavior). The serial-shape rejection (`_MACHINE_NAME_RE` in `mqtt-ingestor`, functionally identical to `validation.py`'s `MACHINE_NAME_ONLY_RE`) is consolidated into one implementation instead of two near-duplicates.

**Implementation normalization:** `broker-ingestor` uses paho's `CallbackAPIVersion.VERSION2` API; `mqtt-ingestor`/`systems_one_ingest` use the older implicit API. The merged service standardizes on one paho calling convention for its single shared MQTT client.

## Deployment & cutover

Retiring:
- `systems_one_ingest`'s current app code (`roles/systems_one_ingest/files/app/mqtt_ingest/`) — the Ansible role itself is either repurposed or replaced by `roles/mqtt_ingestor`, TBD at plan time.
- The two hand-run deployments at `/home/s1/mqtt-ingestor` and `/home/s1/broker-ingestor` — never in Ansible, no tracked `docker-compose` project files anywhere in this repo.

Staged rollout, `sysone_staging` first:

1. Deploy `roles/mqtt_ingestor` alongside the three existing services under a distinct MQTT client ID — read-only in the sense that it doesn't touch the other three's state, just adds a fourth consumer.
2. Verify it's consuming all three topic spaces correctly and writing to the right tables. Compare row counts and timestamps against the existing three services over a representative window (covering at least one full statistics/status cycle).
3. Stop and remove the three old services: `docker rm` for the two unmanaged ones, role removal for `systems_one_ingest`'s old code.
4. `mqtt-ingestor` runs with `MQTT_CLEAN_SESSION=false` (a persistent session) — explicitly unsubscribe/clean up its client ID on Mosquitto when decommissioning it, so the broker doesn't keep queuing messages for a session nobody will ever reconnect to.
5. Repeat steps 1–4 on `sysone` (production) only after staging has run clean for a full day-night cycle (covering both business-hours device traffic and the nightly-adjacent batch/statistics jobs).

## Testing

TDD for the pure-function pieces: topic → table dispatch routing, and the ported allowlist gate (`validation.py` already has a test suite as precedent — extend it, don't replace it).

The DB-writing and MQTT-connection code is not meaningfully unit-testable without a real MSSQL instance and broker — correctness there is validated by the staged-rollout comparison in the cutover plan (step 2 above), the same way the three existing services have effectively been validated so far (by running in production).

## Open questions for the implementation plan

- Exact fate of `roles/systems_one_ingest` — renamed to `roles/mqtt_ingestor` in place, or a fresh role with the old one removed? Affects how much Ansible history/tagging carries forward.
- Whether the merged service's Docker container name/role should live under `webservers.yml` (where `systems_one_ingest` sits today) unchanged, or move given it now also serves a `dbservers.yml`-adjacent concern (broker health).
