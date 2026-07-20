# TTY1 Dashboard Overhaul — Design

**Date:** 2026-07-20
**Target:** `roles/s1_dashboard/` (script shown full-screen on the server's physical console via getty@tty1 autologin)

## Goal

Replace the current `docker-dashboard.py` content with a display focused on:

1. **Performance summary** — items scanned and good-read % for *today*, *this week*, *this year*
2. **Machine metrics** — CPU %, RAM, swap, disk, load, uptime (as today)
3. **Docker service health** — compact when healthy, detailed only for problems
4. **Problems-only log pane** — recent error/warn lines across all containers; quiet when all is well

The dead OpenClaw panel is removed (the service it polled no longer exists).

## Non-goals

- No changes to the marketing web display.
- No TUI framework / pip dependencies — stays a single stdlib-only Python file.
- No historical charts on the console; totals only.

## Architecture

One Python file, stdlib only, rendered with raw ANSI as today. All external data
comes from subprocess calls (`docker …`), matching the existing pattern.

| Data | Source | Refresh |
|---|---|---|
| System stats (CPU/MEM/SWAP/DISK/load/uptime) | `/proc`, `statvfs` | every render (5 s) |
| Container list + status | `docker ps -a` | every render |
| Container CPU/MEM | `docker stats --no-stream` | every render |
| Performance totals | `docker exec mssql sqlcmd` (see below) | cached 60 s |
| Problems scan | `docker logs --since 30m` + healthcheck logs | cached 30 s |

Renderer keeps the existing box-drawing style (`╔═╗` frame, colour scheme, `bar()` gauges).
Each fetcher is wrapped in try/except and returns a sentinel on failure; the render
loop never crashes (existing behaviour, preserved).

## Performance panel

Single query against `S1_Remote_Monitoring.dbo.device_statistics`, executed as:

```
docker exec mssql /opt/mssql-tools18/bin/sqlcmd -S localhost \
  -U <db_user> -P <db_pass> -d S1_Remote_Monitoring -C \
  -h -1 -W -s "|" -Q "SET NOCOUNT ON; <query>"
```

- Period boundaries (today / Monday of current week / Jan 1) are computed **in
  Python** in SAST (UTC+2, matching the existing marketing-display queries which
  use `DATEADD(HOUR, 2, ts_datetime)`) and injected as literal `YYYY-MM-DD`
  dates — no user input is involved.
- One scan returns six numbers via filtered aggregates:
  `SUM(CASE WHEN day >= @period THEN total_items END)` etc. for
  (items, good_read) × (today, week, year). Good-read % is computed in Python.
- **Credentials:** the script uses the Remote Monitoring app login
  (`mssql_rm_admin_login` / `mssql_rm_admin_password` — already plain text in
  `group_vars/dbservers.yml`), not the SA password. To inject them,
  `docker-dashboard.py` moves from `files/` to `templates/docker-dashboard.py.j2`
  and the role templates it out; the two credentials are the only Jinja
  substitutions in the file.
- On query failure the panel shows `—` values and a dim `DB unavailable` note.

Display: three rows (TODAY / THIS WEEK / THIS YEAR), thousands-separated item
counts, good-read % coloured green ≥ 97, orange ≥ 90, red below.

## Services panel

- Compact grid of `● name` entries (packed to terminal width) for containers
  that are healthy, or running without a healthcheck.
- Colours: green = healthy, cyan = up (no healthcheck), orange = up but
  unhealthy/starting, red = exited.
- Header shows `N/M healthy`.
- Any container that is *not* green/cyan gets a full detail row beneath the
  grid: name, status string, and last healthcheck output (truncated), as the
  current dashboard does.

## Problems panel

- For every container: `docker logs --since 30m --timestamps`, filtered in
  Python for `error|warn|exception|traceback|fail` (case-insensitive),
  excluding the dashboard's own noise if any.
- Merged chronologically, tagged `[service]`, newest lines win the available
  space (pane fills the remaining terminal rows, like the old MQTT pane).
- Unhealthy healthcheck outputs also appear here.
- When nothing matches: single green line `✓ all services healthy — no errors in last 30 min`.
- Scan runs at most every 30 s (cached between renders) to keep 11×`docker logs`
  calls off the 5 s render path.

## Layout (top to bottom)

```
╔ header: ⚙ S1 SERVER + timestamp ═══════════════╗
║ 📊 PERFORMANCE  (3 rows: today/week/year)      ║
╠════════════════════════════════════════════════╣
║ CPU/MEM/SWAP/DISK gauge rows (as today)        ║
╠════════════════════════════════════════════════╣
║ 🐳 SERVICES  n/m healthy + dot grid (+details) ║
╠════════════════════════════════════════════════╣
║ ⚠ PROBLEMS (last 30 min) — fills remainder     ║
╠════════════════════════════════════════════════╣
║ footer                                         ║
╚════════════════════════════════════════════════╝
```

## Error handling

- Every fetcher: try/except → sentinel (`None` / empty), panel renders a dim
  placeholder instead of crashing.
- `start-dashboard.sh` already restarts the script on exit; unchanged.
- Terminal resize handled per render via `os.get_terminal_size()` (existing).

## Testing / verification

1. `python -m py_compile` locally.
2. Run the exact `sqlcmd` query manually on the server; confirm numbers match
   `/api/stats` (today/year) from marketing-display.
3. Run the script over SSH (`python3 docker-dashboard.py`) to eyeball layout at
   a couple of terminal sizes. Verify the DB-down path by running a copy that
   points at a nonexistent container name — do not stop the production mssql.
4. Deploy, then `sudo pkill -f docker-dashboard.py` — the start-dashboard.sh
   loop relaunches the new version on TTY1.

## Deployment

- Role change: `files/docker-dashboard.py` → `templates/docker-dashboard.py.j2`;
  task switches from `copy` to `template`. `start-dashboard.sh`, autologin
  drop-in and `.profile` hook unchanged.
- Live rollout (no full Ansible run needed): render/copy the file to
  `/opt/s1-dashboard/docker-dashboard.py` with the two credentials filled in,
  then kill the running python process so the wrapper loop restarts it.
- Commit + push; pull on the server clone.
