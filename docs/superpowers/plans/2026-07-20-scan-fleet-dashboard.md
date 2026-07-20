# Scan Fleet Monitoring Dashboard Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship a new `scan_fleet_dashboard` Ansible role deploying a fleet-monitoring web app (spec: `scan_fleet_dashboard_spec.md`) on port **8091**, running **side-by-side** with the existing marketing_display (port 8090), which stays **untouched** until cutover is explicitly approved.

**Architecture:** FastAPI + pyodbc backend (all bucketing/percentages server-side) serving a no-build vanilla-JS SPA with a vendored Chart.js. Deployed exactly like marketing_display: Docker image built on the server from an Ansible-copied source tree, compose stack on the shared `infra` network, loopback-bound port. All business logic lives in small pure-Python builder modules that take the query function as a parameter, so unit tests inject fakes — no DB needed to test.

**Tech Stack:** Python 3.12 (container) / FastAPI 0.115.5 / uvicorn 0.32.1 / pyodbc 5.2.0 (matching marketing_display pins), Chart.js 4 (vendored UMD file, no CDN at runtime), Ansible role + docker compose, pytest for tests.

## Global Constraints

- `dbo.*` tables are **read-only**. No schema changes, ever.
- `roles/marketing_display/**` is **not modified** by any task in this plan. Cutover is a documented runbook (Task 13), not an executed step.
- New role name `scan_fleet_dashboard`, dir `/opt/scan-fleet-dashboard`, host port **8091**, bind **127.0.0.1** (exposure via Cloudflare tunnel only, same as marketing_display).
- Palette (exact values, CSS variables): `--teal #2f8fa0` (good_read/primary), `--orange #e07b39` (no_read), `--plum #8a4b74` (no_dimension), `--slate #5b6b8c` (no_weight), `--red #c0392b` (out_of_spec/target line), `--navy #14213d` (sidebar), `--blue #2563eb`, `--text #3a3a3a`, `--muted #8a8e94`, `--border #dee1e5`, `--card #ffffff`, `--page #f3f4f6`. Sidebar active item `#2154bf`, tab underline `#2f8fa0`, tab text `#0d7969`.
- Series colours are **fixed by metric**, never by position.
- Symlog Y axis: linthresh **10**, ticks `[0, 2, 5, 10, 50, 100]`.
- Good read default target **93%**. Expected packets: **288/day**, **12/hour**, 5-min interval.
- Shift windows (local): Shift 1 `06:00–14:00`, Shift 2 `14:00–22:00`, Overnight `22:00–06:00`.
- Grid: 12 mini-charts per page (3×4), **paginated**.
- No node/npm build step. Frontend is static files served by FastAPI; Chart.js is vendored into `static/vendor/`.
- Timestamps in DB are UTC; display timezone is a fixed offset `TZ_OFFSET_HOURS` (default **2**, matching the live marketing_display SQL which uses `DATEADD(HOUR, 2, …)`). ALL timezone conversion lives in `timeutil.py` — nowhere else.
- Unit tests run with **no environment variables set** (they assert the defaults above).

## Resolved DECIDEs (from the spec)

| Spec DECIDE | Decision | Where |
|---|---|---|
| Throughput "parcels" metric | `total_items`, env-overridable `THROUGHPUT_METRIC` (allowed: total_items, good_read, complete) | `config.py` + TODO comment |
| Timezone | DB is UTC; fixed display offset `TZ_OFFSET_HOURS=2`; TODO for per-customer IANA zones | `config.py`/`timeutil.py` |
| Paginate vs infinite scroll | Paginate, 12/page | `performance.js` |
| Target line at warn vs bad | `bad_value` (hard limit); TODO to switch | `thresholds.py` |
| Charting library | Chart.js 4 vendored; symlog done by transforming values onto a linear axis (`symlog.js`) | Task 7/8 |
| Shift windows | Spec defaults, constant `SHIFTS` | `config.py` + TODO |
| Throughput capacity line | None; hook exists via `thresholds.resolve_target(..., "throughput")` | TODO in `throughput.py` |
| Auth / row-level security | Off by default (`AUTH_ENABLED=0`). When on: identity from `X-Auth-User` header (Cloudflare Access injects this), mapped via `dbo.customer_login_map`; `ADMIN_USERS` env list sees all. TODO: verify CF Access JWT instead of trusting the header | `auth.py` |

**Real-data note (differs from spec):** the live `alert_thresholds` table uses metric name **`good_read_pct`** (see `roles/marketing_display/files/app/main.py:406`), while the spec says `good_read`. Threshold resolution accepts **both** spellings. The table also has a `location` column; resolution matches on it when present.

## File Structure

```
roles/scan_fleet_dashboard/
  defaults/main.yml                  # ports, DB creds, tuning vars
  handlers/main.yml                  # restart handler
  tasks/main.yml                     # copy source, render compose, up -d --build
  templates/docker-compose.scan_fleet_dashboard.yml.j2
  files/Dockerfile                   # same ODBC-18 multi-stage build as marketing_display
  files/app/requirements.txt
  files/app/main.py                  # FastAPI wiring only (routes, cache, static)
  files/app/config.py                # env config + constants (single source)
  files/app/db.py                    # CONN_STR + query() (lazy pyodbc import)
  files/app/timeutil.py              # ALL tz conversion + bucketing SQL fragments
  files/app/auth.py                  # customer scoping (row-level security)
  files/app/thresholds.py            # §6 target resolution
  files/app/perf.py                  # Tab 1 builders (+ customer_scope helper)
  files/app/throughput.py            # Tab 2 builders
  files/app/device.py                # drill-down + fleet-health builders
  files/app/static/index.html        # SPA shell
  files/app/static/css/app.css       # palette + layout
  files/app/static/js/symlog.js      # symlog transform + ticks
  files/app/static/js/charts.js      # chart factories (fixed metric colours)
  files/app/static/js/performance.js # Tab 1
  files/app/static/js/throughput.js  # Tab 2
  files/app/static/js/health.js      # Tab 3
  files/app/static/js/device.js      # drill-down
  files/app/static/js/app.js         # router, filters, shared helpers
  files/app/static/vendor/chart.umd.js  # vendored Chart.js 4 (committed)
  tests/conftest.py                  # sys.path + FakeQuery
  tests/requirements-dev.txt
  tests/test_role_files.py           # Ansible YAML sanity
  tests/test_health.py
  tests/test_timeutil.py
  tests/test_thresholds.py
  tests/test_perf.py
  tests/test_throughput.py
  tests/test_device.py
  tests/test_auth.py
webservers.yml                       # + role entry (tagged)
docs/runbooks/scan-fleet-cutover.md  # Task 13: cutover runbook (documented, NOT executed)
```

Run all tests from repo root with: `python -m pytest roles/scan_fleet_dashboard/tests -v`
(first: `pip install -r roles/scan_fleet_dashboard/tests/requirements-dev.txt`)

Deployment model: the Ansible playbook runs **on the server itself** (`staging` inventory, `ansible_connection: local`, user `s1`, passwordless sudo). Server: `ssh s1@192.168.1.16`, repo checkout at `~/Systems-One-Server` (verify with `ssh s1@192.168.1.16 'ls ~/Systems-One-Server'` before first deploy). Browser access from the dev machine: `ssh -L 8091:localhost:8091 s1@192.168.1.16`, then open `http://localhost:8091/`.

---

### Task 1: Ansible role scaffold + skeleton app + deploy wiring

**Files:**
- Create: `roles/scan_fleet_dashboard/defaults/main.yml`
- Create: `roles/scan_fleet_dashboard/handlers/main.yml`
- Create: `roles/scan_fleet_dashboard/tasks/main.yml`
- Create: `roles/scan_fleet_dashboard/templates/docker-compose.scan_fleet_dashboard.yml.j2`
- Create: `roles/scan_fleet_dashboard/files/Dockerfile`
- Create: `roles/scan_fleet_dashboard/files/app/requirements.txt`
- Create: `roles/scan_fleet_dashboard/files/app/main.py`
- Create: `roles/scan_fleet_dashboard/tests/conftest.py`
- Create: `roles/scan_fleet_dashboard/tests/requirements-dev.txt`
- Create: `roles/scan_fleet_dashboard/tests/test_health.py`
- Create: `roles/scan_fleet_dashboard/tests/test_role_files.py`
- Modify: `webservers.yml:19-28` (roles list)

**Interfaces:**
- Consumes: group_vars `docker_shared_network` (=`infra`), host_vars `mssql_rm_admin_login`/`mssql_rm_admin_password` (same vars marketing_display uses).
- Produces: FastAPI `app` object in `main.py` with `GET /health` → `{"status": "ok"}`; role deployable via `--tags scan_fleet_dashboard`. Later tasks add modules next to `main.py` and import them by bare name (flat module layout, like marketing_display).

- [ ] **Step 1: Write the failing tests**

`roles/scan_fleet_dashboard/tests/conftest.py`:

```python
"""Make the flat app modules importable and provide a fake DB query layer."""
import os
import sys

sys.path.insert(
    0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "files", "app")
)


class FakeQuery:
    """Callable standing in for db.query. Routes canned rows by SQL substring."""

    def __init__(self):
        self.routes = []  # (substring, rows) — checked in insertion order
        self.calls = []   # (sql, params)

    def add(self, substring, rows):
        self.routes.append((substring, rows))
        return self

    def __call__(self, sql, params=()):
        self.calls.append((sql, params))
        for sub, rows in self.routes:
            if sub in sql:
                return rows
        raise AssertionError(f"FakeQuery: no route matches SQL: {sql[:150]}")
```

`roles/scan_fleet_dashboard/tests/requirements-dev.txt`:

```
pytest==8.3.4
pyyaml==6.0.2
fastapi==0.115.5
httpx==0.28.1
```

`roles/scan_fleet_dashboard/tests/test_health.py`:

```python
from fastapi.testclient import TestClient

import main


def test_health_ok():
    client = TestClient(main.app)
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}
```

`roles/scan_fleet_dashboard/tests/test_role_files.py`:

```python
import glob
import os

import yaml

ROLE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")


def _load(relpath):
    with open(os.path.join(ROLE, relpath), encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def test_role_yaml_parses():
    files = []
    for sub in ("defaults", "tasks", "handlers"):
        files += glob.glob(os.path.join(ROLE, sub, "*.yml"))
    assert len(files) >= 3
    for f in files:
        with open(f, encoding="utf-8") as fh:
            assert yaml.safe_load(fh) is not None, f


def test_defaults_pin_side_by_side_port_and_loopback():
    d = _load("defaults/main.yml")
    assert d["scan_fleet_dashboard_port"] == 8091          # NOT 8090 (marketing_display)
    assert d["scan_fleet_dashboard_bind_address"] == "127.0.0.1"
    assert d["scan_fleet_dashboard_dir"] == "/opt/scan-fleet-dashboard"


def test_webservers_playbook_includes_tagged_role():
    with open(os.path.join(ROLE, "..", "..", "webservers.yml"), encoding="utf-8") as fh:
        play = yaml.safe_load(fh)[0]
    entry = [r for r in play["roles"] if isinstance(r, dict) and r.get("role") == "scan_fleet_dashboard"]
    assert entry and "scan_fleet_dashboard" in entry[0]["tags"]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pip install -r roles/scan_fleet_dashboard/tests/requirements-dev.txt` then `python -m pytest roles/scan_fleet_dashboard/tests -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'main'` and missing YAML files.

- [ ] **Step 3: Create the role files**

`roles/scan_fleet_dashboard/files/app/main.py`:

```python
"""
Systems One — Scan Fleet Monitoring Dashboard
=============================================
Fleet monitoring web app over S1_Remote_Monitoring (read-only).
Runs side-by-side with marketing_display until cutover.
"""
from fastapi import FastAPI

app = FastAPI(title="S1 Scan Fleet Dashboard")


@app.get("/health")
async def health():
    return {"status": "ok"}
```

`roles/scan_fleet_dashboard/files/app/requirements.txt`:

```
fastapi==0.115.5
uvicorn[standard]==0.32.1
pyodbc==5.2.0
```

`roles/scan_fleet_dashboard/files/Dockerfile` — identical build recipe to marketing_display (ODBC Driver 18, multi-stage, non-root):

```dockerfile
# syntax=docker/dockerfile:1

FROM python:3.12-slim-bookworm AS builder

ARG DEBIAN_FRONTEND=noninteractive

# Install MS ODBC 18 + build deps
RUN apt-get update && apt-get install -y --no-install-recommends \
        curl gnupg ca-certificates \
    && curl -fsSL https://packages.microsoft.com/keys/microsoft.asc \
       | gpg --dearmor -o /usr/share/keyrings/microsoft-prod.gpg \
    && curl -fsSL https://packages.microsoft.com/config/debian/12/prod.list \
       > /etc/apt/sources.list.d/mssql-release.list \
    && apt-get update \
    && ACCEPT_EULA=Y apt-get install -y --no-install-recommends \
        msodbcsql18 unixodbc unixodbc-dev gcc \
    && apt-get purge -y --auto-remove curl gnupg \
    && rm -rf /var/lib/apt/lists/*

COPY app/requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir --prefix=/install -r /tmp/requirements.txt


FROM python:3.12-slim-bookworm AS runtime

ARG DEBIAN_FRONTEND=noninteractive

# Install MS ODBC 18 runtime + unixodbc (needed at runtime by pyodbc)
RUN apt-get update && apt-get install -y --no-install-recommends \
        curl gnupg ca-certificates \
    && curl -fsSL https://packages.microsoft.com/keys/microsoft.asc \
       | gpg --dearmor -o /usr/share/keyrings/microsoft-prod.gpg \
    && curl -fsSL https://packages.microsoft.com/config/debian/12/prod.list \
       > /etc/apt/sources.list.d/mssql-release.list \
    && apt-get update \
    && ACCEPT_EULA=Y apt-get install -y --no-install-recommends \
        msodbcsql18 unixodbc \
    && apt-get purge -y --auto-remove curl gnupg \
    && rm -rf /var/lib/apt/lists/*

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1

COPY --from=builder /install /usr/local

RUN groupadd --gid 1001 appuser \
    && useradd --uid 1001 --gid appuser --no-create-home appuser

WORKDIR /app
COPY app/ .
RUN chown -R appuser:appuser /app
USER appuser

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --retries=3 --start-period=20s \
    CMD python -c "import urllib.request,sys; r=urllib.request.urlopen('http://localhost:8000/health',timeout=4); sys.exit(0 if r.status==200 else 1)"

ENTRYPOINT ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
```

`roles/scan_fleet_dashboard/defaults/main.yml`:

```yaml
scan_fleet_dashboard_image: "scan-fleet-dashboard:latest"
scan_fleet_dashboard_dir:   "/opt/scan-fleet-dashboard"
scan_fleet_dashboard_port:  8091
# Bind to loopback — expose publicly via Cloudflare tunnel only
scan_fleet_dashboard_bind_address: "127.0.0.1"

scan_fleet_dashboard_db_host: "mssql"
scan_fleet_dashboard_db_port: 1433
scan_fleet_dashboard_db_name: "S1_Remote_Monitoring"
scan_fleet_dashboard_db_user: "{{ mssql_rm_admin_login | default('admin') }}"
scan_fleet_dashboard_db_pass: "{{ mssql_rm_admin_password | default('') }}"

scan_fleet_dashboard_cache_ttl: 45
scan_fleet_dashboard_tz_offset_hours: 2
scan_fleet_dashboard_throughput_metric: "total_items"
scan_fleet_dashboard_good_read_target: 93
scan_fleet_dashboard_auth_enabled: false
scan_fleet_dashboard_admin_users: ""
```

`roles/scan_fleet_dashboard/handlers/main.yml`:

```yaml
- name: restart scan-fleet-dashboard
  command: docker compose up -d --build --force-recreate
  args:
    chdir: "{{ scan_fleet_dashboard_dir }}"
```

`roles/scan_fleet_dashboard/tasks/main.yml`:

```yaml
- name: Ensure scan-fleet-dashboard directory exists
  file:
    path: "{{ scan_fleet_dashboard_dir }}"
    state: directory
    owner: root
    group: root
    mode: "0755"

- name: Copy application source
  copy:
    src: "{{ item.src }}"
    dest: "{{ scan_fleet_dashboard_dir }}/{{ item.dest }}"
    owner: root
    group: root
    mode: "{{ item.mode | default('0644') }}"
  loop:
    - { src: Dockerfile, dest: Dockerfile }
    - { src: app/,       dest: app/ }
  notify: restart scan-fleet-dashboard

- name: Render docker-compose file
  template:
    src: docker-compose.scan_fleet_dashboard.yml.j2
    dest: "{{ scan_fleet_dashboard_dir }}/docker-compose.yml"
    owner: root
    group: root
    mode: "0644"
  notify: restart scan-fleet-dashboard

- name: Build and start scan-fleet-dashboard container
  command: docker compose up -d --build
  args:
    chdir: "{{ scan_fleet_dashboard_dir }}"
  register: compose_result
  changed_when: "'Started' in compose_result.stdout or 'Recreated' in compose_result.stdout or compose_result.rc == 0"
```

`roles/scan_fleet_dashboard/templates/docker-compose.scan_fleet_dashboard.yml.j2`:

```yaml
services:
  scan-fleet-dashboard:
    build:
      context: .
      dockerfile: Dockerfile
    image: "{{ scan_fleet_dashboard_image }}"
    container_name: scan_fleet_dashboard
    restart: unless-stopped
    environment:
      DB_HOST: "{{ scan_fleet_dashboard_db_host }}"
      DB_PORT: "{{ scan_fleet_dashboard_db_port }}"
      DB_NAME: "{{ scan_fleet_dashboard_db_name }}"
      DB_USER: "{{ scan_fleet_dashboard_db_user }}"
      DB_PASS: "{{ scan_fleet_dashboard_db_pass }}"
      CACHE_TTL_SECONDS: "{{ scan_fleet_dashboard_cache_ttl }}"
      TZ_OFFSET_HOURS: "{{ scan_fleet_dashboard_tz_offset_hours }}"
      THROUGHPUT_METRIC: "{{ scan_fleet_dashboard_throughput_metric }}"
      GOOD_READ_DEFAULT_TARGET: "{{ scan_fleet_dashboard_good_read_target }}"
      AUTH_ENABLED: "{{ '1' if scan_fleet_dashboard_auth_enabled else '0' }}"
      ADMIN_USERS: "{{ scan_fleet_dashboard_admin_users }}"
    ports:
      - "{{ scan_fleet_dashboard_bind_address }}:{{ scan_fleet_dashboard_port }}:8000"
    healthcheck:
      test: ["CMD-SHELL", "python -c \"import urllib.request,sys; r=urllib.request.urlopen('http://localhost:8000/health',timeout=4); sys.exit(0 if r.status==200 else 1)\""]
      interval: 30s
      timeout: 5s
      retries: 3
      start_period: 20s
    networks:
      - {{ docker_shared_network | default('infra') }}

networks:
  {{ docker_shared_network | default('infra') }}:
    external: true
```

- [ ] **Step 4: Wire the role into the play (tagged, so it deploys independently)**

In `webservers.yml`, change the roles list tail from:

```yaml
    - s1_reporter
    - marketing_display
```

to:

```yaml
    - s1_reporter
    - marketing_display
    - role: scan_fleet_dashboard
      tags: [scan_fleet_dashboard]
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `python -m pytest roles/scan_fleet_dashboard/tests -v`
Expected: 4 tests PASS.

- [ ] **Step 6: Commit**

```bash
git add roles/scan_fleet_dashboard webservers.yml
git commit -m "feat(scan_fleet_dashboard): role scaffold + skeleton app, side-by-side on 8091"
```

---

### Task 2: Config, DB layer, time utilities, auth stub

**Files:**
- Create: `roles/scan_fleet_dashboard/files/app/config.py`
- Create: `roles/scan_fleet_dashboard/files/app/db.py`
- Create: `roles/scan_fleet_dashboard/files/app/timeutil.py`
- Create: `roles/scan_fleet_dashboard/files/app/auth.py`
- Test: `roles/scan_fleet_dashboard/tests/test_timeutil.py`

**Interfaces:**
- Consumes: env vars only.
- Produces: `config.*` constants; `db.query(sql: str, params: tuple = ()) -> list[dict]` (date/datetime values coerced to ISO **strings**, Decimals to float — all builders operate on strings); `timeutil.local_expr(col)`, `timeutil.day_expr(col="s.ts_datetime") -> str`, `timeutil.hour_of_day_expr(col) -> str`, `timeutil.parse_range(date_from, date_to) -> (date, date)` (raises `ValueError`), `timeutil.utc_bounds(local_from, local_to) -> (datetime, datetime)` half-open, `timeutil.today_local() -> date`; `auth.allowed_customers(q, user, enabled=None, admins=None) -> list[str] | None` (None = unrestricted; full logic in Task 12 — the stub here already returns None when disabled).

- [ ] **Step 1: Write the failing tests**

`roles/scan_fleet_dashboard/tests/test_timeutil.py`:

```python
import datetime

import pytest

import timeutil


def test_parse_range_ok():
    f, t = timeutil.parse_range("2026-07-01", "2026-07-20")
    assert f == datetime.date(2026, 7, 1)
    assert t == datetime.date(2026, 7, 20)


def test_parse_range_rejects_bad_format():
    with pytest.raises(ValueError):
        timeutil.parse_range("07/01/2026", "2026-07-20")


def test_parse_range_rejects_reversed():
    with pytest.raises(ValueError):
        timeutil.parse_range("2026-07-20", "2026-07-01")


def test_parse_range_rejects_huge_span():
    with pytest.raises(ValueError):
        timeutil.parse_range("2020-01-01", "2026-07-20")


def test_utc_bounds_shifts_back_by_display_offset():
    start, end = timeutil.utc_bounds(datetime.date(2026, 7, 1), datetime.date(2026, 7, 1))
    # local midnight − 2h; half-open end at next local midnight − 2h
    assert start == datetime.datetime(2026, 6, 30, 22, 0)
    assert end == datetime.datetime(2026, 7, 1, 22, 0)


def test_sql_fragments_are_the_single_conversion_point():
    assert timeutil.local_expr("s.ts_datetime") == "DATEADD(HOUR, 2, s.ts_datetime)"
    assert timeutil.day_expr() == "CAST(DATEADD(HOUR, 2, s.ts_datetime) AS date)"
    assert timeutil.hour_of_day_expr() == "DATEPART(HOUR, DATEADD(HOUR, 2, s.ts_datetime))"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest roles/scan_fleet_dashboard/tests/test_timeutil.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'timeutil'`.

- [ ] **Step 3: Implement the modules**

`roles/scan_fleet_dashboard/files/app/config.py`:

```python
"""Central configuration. Env-driven; defaults match docker-compose defaults."""
import os

DB_HOST = os.getenv("DB_HOST", "mssql")
DB_PORT = int(os.getenv("DB_PORT", "1433"))
DB_NAME = os.getenv("DB_NAME", "S1_Remote_Monitoring")
DB_USER = os.getenv("DB_USER", "admin")
DB_PASS = os.getenv("DB_PASS", "")

# TODO(DECIDE): "parcels" = everything seen. Alternatives: "good_read", "complete".
THROUGHPUT_METRIC = os.getenv("THROUGHPUT_METRIC", "total_items")
_ALLOWED_THROUGHPUT_METRICS = ("total_items", "good_read", "complete")
if THROUGHPUT_METRIC not in _ALLOWED_THROUGHPUT_METRICS:
    raise ValueError(f"THROUGHPUT_METRIC must be one of {_ALLOWED_THROUGHPUT_METRICS}")

# TODO(DECIDE): DB stores UTC; display uses a fixed offset. Replace with
# per-customer IANA zones if the fleet ever spans timezones.
TZ_OFFSET_HOURS = int(os.getenv("TZ_OFFSET_HOURS", "2"))

GOOD_READ_DEFAULT_TARGET = float(os.getenv("GOOD_READ_DEFAULT_TARGET", "93"))
CACHE_TTL = int(os.getenv("CACHE_TTL_SECONDS", "45"))

EXPECTED_PACKETS_PER_DAY = 288  # 1440 min / 5-min packets
PACKETS_PER_HOUR = 12

# TODO(DECIDE): shift windows (name, start hour inclusive, end hour exclusive; local time)
SHIFTS = (("Shift 1", 6, 14), ("Shift 2", 14, 22), ("Overnight", 22, 6))

AUTH_ENABLED = os.getenv("AUTH_ENABLED", "0") == "1"
ADMIN_USERS = frozenset(
    u.strip() for u in os.getenv("ADMIN_USERS", "").split(",") if u.strip()
)
```

`roles/scan_fleet_dashboard/files/app/db.py`:

```python
"""Single DB access point. pyodbc is imported lazily so unit tests (and a
static-only dev server) run on machines without the ODBC stack."""
import datetime
import decimal

import config

CONN_STR = (
    "DRIVER={ODBC Driver 18 for SQL Server};"
    f"SERVER={config.DB_HOST},{config.DB_PORT};"
    f"DATABASE={config.DB_NAME};"
    f"UID={config.DB_USER};"
    f"PWD={config.DB_PASS};"
    "Encrypt=optional;"
    "TrustServerCertificate=yes;"
    "Connection Timeout=5;"
)


def _coerce(v):
    if isinstance(v, decimal.Decimal):
        return float(v)
    if isinstance(v, (datetime.datetime, datetime.date)):
        return v.isoformat()
    return v


def query(sql: str, params: tuple = ()) -> list[dict]:
    import pyodbc

    with pyodbc.connect(CONN_STR, timeout=8) as conn:
        cur = conn.cursor()
        if params:
            cur.execute(sql, params)
        else:
            cur.execute(sql)
        cols = [c[0] for c in cur.description]
        return [{k: _coerce(v) for k, v in zip(cols, row)} for row in cur.fetchall()]
```

`roles/scan_fleet_dashboard/files/app/timeutil.py`:

```python
"""ALL timezone conversion and time bucketing lives here (spec §2: one place)."""
import datetime

import config

_OFFSET = datetime.timedelta(hours=config.TZ_OFFSET_HOURS)


def local_expr(col: str = "s.ts_datetime") -> str:
    """SQL expression converting a UTC datetime column to display-local time."""
    return f"DATEADD(HOUR, {config.TZ_OFFSET_HOURS}, {col})"


def day_expr(col: str = "s.ts_datetime") -> str:
    """Daily bucket expression (local date)."""
    return f"CAST({local_expr(col)} AS date)"


def hour_of_day_expr(col: str = "s.ts_datetime") -> str:
    """Local hour-of-day (0-23)."""
    return f"DATEPART(HOUR, {local_expr(col)})"


def parse_range(date_from: str, date_to: str, max_days: int = 366):
    try:
        f = datetime.date.fromisoformat(date_from)
        t = datetime.date.fromisoformat(date_to)
    except (TypeError, ValueError):
        raise ValueError("dates must be YYYY-MM-DD")
    if f > t:
        raise ValueError("date_from must be <= date_to")
    if (t - f).days + 1 > max_days:
        raise ValueError(f"range limited to {max_days} days")
    return f, t


def utc_bounds(local_from: datetime.date, local_to: datetime.date):
    """Half-open UTC [start, end) covering the inclusive local-date range.

    WHERE clauses stay raw range scans on ts_datetime (index-friendly) instead
    of wrapping the indexed column in DATEADD.
    """
    start = datetime.datetime.combine(local_from, datetime.time.min) - _OFFSET
    end = (
        datetime.datetime.combine(local_to + datetime.timedelta(days=1), datetime.time.min)
        - _OFFSET
    )
    return start, end


def today_local() -> datetime.date:
    return (datetime.datetime.utcnow() + _OFFSET).date()
```

`roles/scan_fleet_dashboard/files/app/auth.py` (stub — completed in Task 12):

```python
"""Row-level security: map the caller to the customers they may see.

Returns None for unrestricted access (auth disabled, or admin user).
Full mapping via dbo.customer_login_map lands in Task 12.
"""
import config


def resolve_user(request) -> str | None:
    # TODO: verify the Cloudflare Access JWT instead of trusting the header.
    return request.headers.get("X-Auth-User")


def allowed_customers(q, user, enabled=None, admins=None):
    enabled = config.AUTH_ENABLED if enabled is None else enabled
    if not enabled:
        return None
    raise NotImplementedError("auth mapping implemented in Task 12")
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest roles/scan_fleet_dashboard/tests -v`
Expected: all PASS (Task 1's tests still green).

- [ ] **Step 5: Commit**

```bash
git add roles/scan_fleet_dashboard
git commit -m "feat(scan_fleet_dashboard): config, lazy-pyodbc db layer, timezone/bucketing utils"
```

---

### Task 3: Threshold resolution (spec §6)

**Files:**
- Create: `roles/scan_fleet_dashboard/files/app/thresholds.py`
- Test: `roles/scan_fleet_dashboard/tests/test_thresholds.py`

**Interfaces:**
- Consumes: `config.GOOD_READ_DEFAULT_TARGET`; a query callable `q`.
- Produces: `thresholds.load_thresholds(q) -> list[dict]` (rows with keys customer, machine_name, location, metric, direction, warn_value, bad_value); `thresholds.resolve_target(rows, customer, machine_name, location, metrics, default=None) -> float | None`; `thresholds.good_read_target(rows, customer, machine_name, location) -> float`; `thresholds.warn_bad(rows, customer, machine_name, location, metrics) -> (warn, bad) | (None, None)`.

- [ ] **Step 1: Write the failing tests**

`roles/scan_fleet_dashboard/tests/test_thresholds.py`:

```python
import thresholds

ROWS = [
    {"customer": "ACME", "machine_name": "Line-01", "location": "DC-1",
     "metric": "good_read_pct", "direction": "low", "warn_value": 95.0, "bad_value": 91.0},
    {"customer": "ACME", "machine_name": None, "location": None,
     "metric": "good_read_pct", "direction": "low", "warn_value": 96.0, "bad_value": 92.0},
]


def test_machine_specific_row_wins():
    assert thresholds.good_read_target(ROWS, "ACME", "Line-01", "DC-1") == 91.0


def test_customer_wide_fallback():
    assert thresholds.good_read_target(ROWS, "ACME", "Line-02", "DC-1") == 92.0


def test_global_default_when_no_rows_match():
    assert thresholds.good_read_target(ROWS, "Other", "X", "Y") == 93.0


def test_accepts_both_metric_spellings():
    rows = [dict(ROWS[0], metric="good_read")]
    assert thresholds.good_read_target(rows, "ACME", "Line-01", "DC-1") == 91.0


def test_location_mismatch_does_not_match_machine_row():
    # Same machine name at a different location must not inherit DC-1's row.
    assert thresholds.good_read_target(ROWS, "ACME", "Line-01", "DC-9") == 92.0


def test_warn_bad_pair():
    assert thresholds.warn_bad(ROWS, "ACME", "Line-01", "DC-1", ("good_read_pct",)) == (95.0, 91.0)
    assert thresholds.warn_bad(ROWS, "Other", "X", "Y", ("good_read_pct",)) == (None, None)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest roles/scan_fleet_dashboard/tests/test_thresholds.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'thresholds'`.

- [ ] **Step 3: Implement**

`roles/scan_fleet_dashboard/files/app/thresholds.py`:

```python
"""Target/threshold resolution (spec §6).

Resolution order: machine-specific row -> customer-wide row (machine_name NULL)
-> caller-supplied default. The dashed line uses bad_value (the hard limit).
TODO(DECIDE): switch to warn_value if the softer line is preferred.

Live-data note: the deployed alert_thresholds table stores the good-read metric
as 'good_read_pct' (spec says 'good_read') — we accept both spellings.
"""
import config

GOOD_READ_METRICS = ("good_read", "good_read_pct")

_SQL = """
    SELECT customer, machine_name, location, metric, direction, warn_value, bad_value
    FROM dbo.alert_thresholds
"""


def load_thresholds(q) -> list[dict]:
    return q(_SQL)


def _best_row(rows, customer, machine_name, location, metrics):
    metrics = (metrics,) if isinstance(metrics, str) else tuple(metrics)
    fallback = None
    for r in rows:
        if r["metric"] not in metrics or r["customer"] != customer:
            continue
        if r["machine_name"] == machine_name and r.get("location") in (None, location):
            return r
        if r["machine_name"] is None and fallback is None:
            fallback = r
    return fallback


def resolve_target(rows, customer, machine_name, location, metrics, default=None):
    row = _best_row(rows, customer, machine_name, location, metrics)
    if row is not None and row["bad_value"] is not None:
        return float(row["bad_value"])
    return default


def warn_bad(rows, customer, machine_name, location, metrics):
    row = _best_row(rows, customer, machine_name, location, metrics)
    if row is None:
        return None, None
    return (
        float(row["warn_value"]) if row["warn_value"] is not None else None,
        float(row["bad_value"]) if row["bad_value"] is not None else None,
    )


def good_read_target(rows, customer, machine_name, location) -> float:
    return resolve_target(
        rows, customer, machine_name, location,
        GOOD_READ_METRICS, config.GOOD_READ_DEFAULT_TARGET,
    )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest roles/scan_fleet_dashboard/tests -v`
Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add roles/scan_fleet_dashboard
git commit -m "feat(scan_fleet_dashboard): threshold/target resolution with live metric-name compat"
```

---

### Task 4: `/api/machines`, `/api/performance`, `/api/customers`

**Files:**
- Create: `roles/scan_fleet_dashboard/files/app/perf.py`
- Modify: `roles/scan_fleet_dashboard/files/app/main.py` (add API endpoints; keep `/health`)
- Test: `roles/scan_fleet_dashboard/tests/test_perf.py`

**Interfaces:**
- Consumes: `timeutil.*`, `thresholds.load_thresholds/good_read_target`, `db.query`, `auth.allowed_customers`/`auth.resolve_user`.
- Produces:
  - `perf.customer_scope(customer, allowed) -> (sql_fragment, params_list)` — reused by throughput/device builders. `allowed=None` → unrestricted; `allowed=[]` → `AND 1=0` (deny all).
  - `perf.pct(numer, denom) -> float | None` (None when denom falsy — gap, not 0).
  - `perf.build_performance(q, date_from, date_to, customer=None, allowed=None) -> list[dict]`, each: `{device_id, display_name, customer, target_pct, current_good_read_pct, below_target, series: [{date, total_items, good_read_pct, no_read_pct, no_dimension_pct, no_weight_pct, item_out_of_spec_pct}]}` sorted by (customer, display_name).
  - `perf.build_machines(...)` — same args, projection without `series`.
  - `main._exec(fn)` async helper: ValueError→400, HTTPException passthrough, else→503; `main._allowed(request)`; `main._default_range(days) -> (from_iso, to_iso)`.
  - Endpoints: `GET /api/customers`, `GET /api/machines`, `GET /api/performance` (query params `customer`, `date_from`, `date_to`; defaults last 30 days).

- [ ] **Step 1: Write the failing tests**

`roles/scan_fleet_dashboard/tests/test_perf.py`:

```python
from fastapi.testclient import TestClient

import perf
from conftest import FakeQuery


def _row(dev=1, day="2026-07-01", total=1000, good=950, **kw):
    base = {
        "device_id": dev, "customer": "ACME", "location": "DC-1",
        "machine_name": f"Line-0{dev}", "day": day, "total_items": total,
        "good_read": good, "no_read": 30, "no_dimension": 10,
        "no_weight": 5, "item_out_of_spec": 5,
    }
    base.update(kw)
    return base


def fake(rows, thresholds=()):
    return (FakeQuery()
            .add("dbo.alert_thresholds", list(thresholds))
            .add("dbo.device_statistics", rows))


def test_series_pct_headline_and_default_target():
    q = fake([_row(day="2026-07-01"), _row(day="2026-07-02", good=900)])
    out = perf.build_performance(q, "2026-07-01", "2026-07-02")
    assert len(out) == 1
    d = out[0]
    assert d["display_name"] == "DC-1 / Line-01"
    assert d["series"][0]["good_read_pct"] == 95.0
    assert d["series"][0]["no_read_pct"] == 3.0
    assert d["current_good_read_pct"] == 92.5   # (950+900)/2000
    assert d["target_pct"] == 93.0              # global default
    assert d["below_target"] is True


def test_threshold_row_overrides_default():
    th = [{"customer": "ACME", "machine_name": "Line-01", "location": "DC-1",
           "metric": "good_read_pct", "direction": "low",
           "warn_value": 95.0, "bad_value": 90.0}]
    d = perf.build_performance(fake([_row()], th), "2026-07-01", "2026-07-01")[0]
    assert d["target_pct"] == 90.0
    assert d["below_target"] is False           # 95.0 >= 90


def test_zero_total_bucket_is_gap_not_zero():
    q = fake([_row(total=0, good=0, no_read=0, no_dimension=0,
                   no_weight=0, item_out_of_spec=0)])
    d = perf.build_performance(q, "2026-07-01", "2026-07-01")[0]
    assert d["series"][0]["good_read_pct"] is None
    assert d["current_good_read_pct"] is None
    assert d["below_target"] is False


def test_customer_scope_fragments():
    assert perf.customer_scope(None, None) == ("", [])
    assert perf.customer_scope("ACME", None) == (" AND d.customer = ?", ["ACME"])
    sql, params = perf.customer_scope(None, ["A", "B"])
    assert sql == " AND d.customer IN (?,?)"
    assert params == ["A", "B"]
    sql, _ = perf.customer_scope(None, [])
    assert "1=0" in sql                          # mapped to no customers -> sees nothing


def test_api_performance_endpoint(monkeypatch):
    import db
    import main
    monkeypatch.setattr(db, "query", fake([_row()]))
    client = TestClient(main.app)
    r = client.get("/api/performance", params={
        "date_from": "2026-07-01", "date_to": "2026-07-01"})
    assert r.status_code == 200
    assert r.json()[0]["device_id"] == 1


def test_api_performance_bad_range_is_400(monkeypatch):
    import db
    import main
    monkeypatch.setattr(db, "query", fake([]))
    client = TestClient(main.app)
    r = client.get("/api/performance", params={
        "date_from": "2026-07-02", "date_to": "2026-07-01"})
    assert r.status_code == 400


def test_api_machines_has_no_series(monkeypatch):
    import db
    import main
    monkeypatch.setattr(db, "query", fake([_row()]))
    client = TestClient(main.app)
    r = client.get("/api/machines", params={
        "date_from": "2026-07-01", "date_to": "2026-07-01"})
    assert r.status_code == 200
    assert "series" not in r.json()[0]
    assert r.json()[0]["current_good_read_pct"] == 95.0
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest roles/scan_fleet_dashboard/tests/test_perf.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'perf'`.

- [ ] **Step 3: Implement `perf.py`**

`roles/scan_fleet_dashboard/files/app/perf.py`:

```python
"""Tab 1 — scan performance builders (spec §5)."""
import thresholds
import timeutil

PCT_METRICS = ("good_read", "no_read", "no_dimension", "no_weight", "item_out_of_spec")


def customer_scope(customer, allowed):
    """WHERE fragment + params for the customer filter and row-level scoping.

    customer: the UI filter value (None/'' = all customers).
    allowed:  None for unrestricted callers, else the caller's permitted list.
    """
    sql, params = "", []
    if allowed is not None:
        if not allowed:
            return " AND 1=0", []
        sql += " AND d.customer IN (" + ",".join("?" for _ in allowed) + ")"
        params += list(allowed)
    if customer:
        sql += " AND d.customer = ?"
        params.append(customer)
    return sql, params


def pct(numer, denom):
    if not denom:
        return None  # bucket with 0 items renders as a gap, not 0 (spec §2)
    return round(100.0 * numer / denom, 2)


def _daily_rows(q, start_utc, end_utc, scope_sql, scope_params):
    sql = f"""
        SELECT d.id AS device_id, d.customer, d.location, d.machine_name,
               {timeutil.day_expr()} AS day,
               SUM(s.total_items)      AS total_items,
               SUM(s.good_read)        AS good_read,
               SUM(s.no_read)          AS no_read,
               SUM(s.no_dimension)     AS no_dimension,
               SUM(s.no_weight)        AS no_weight,
               SUM(s.item_out_of_spec) AS item_out_of_spec
        FROM dbo.device_statistics s
        JOIN dbo.devices d ON d.id = s.device_id
        WHERE s.ts_datetime >= ? AND s.ts_datetime < ?{scope_sql}
        GROUP BY d.id, d.customer, d.location, d.machine_name, {timeutil.day_expr()}
        ORDER BY d.id, day
    """
    return q(sql, tuple([start_utc, end_utc] + scope_params))


def build_performance(q, date_from, date_to, customer=None, allowed=None):
    f, t = timeutil.parse_range(date_from, date_to)
    start, end = timeutil.utc_bounds(f, t)
    scope_sql, scope_params = customer_scope(customer, allowed)
    th = thresholds.load_thresholds(q)
    rows = _daily_rows(q, start, end, scope_sql, scope_params)

    devices: dict[int, dict] = {}
    sums: dict[int, list] = {}  # device_id -> [sum_good, sum_total]
    for r in rows:
        dev = devices.setdefault(r["device_id"], {
            "device_id": r["device_id"],
            "display_name": f"{r['location']} / {r['machine_name']}",
            "customer": r["customer"],
            "target_pct": thresholds.good_read_target(
                th, r["customer"], r["machine_name"], r["location"]),
            "series": [],
        })
        total = r["total_items"] or 0
        point = {"date": r["day"], "total_items": total}
        for m in PCT_METRICS:
            point[f"{m}_pct"] = pct(r[m] or 0, total)
        dev["series"].append(point)
        acc = sums.setdefault(r["device_id"], [0, 0])
        acc[0] += r["good_read"] or 0
        acc[1] += total

    for dev_id, dev in devices.items():
        good, total = sums[dev_id]
        current = pct(good, total)
        dev["current_good_read_pct"] = current
        dev["below_target"] = current is not None and current < dev["target_pct"]

    return sorted(devices.values(), key=lambda d: (d["customer"], d["display_name"]))


def build_machines(q, date_from, date_to, customer=None, allowed=None):
    return [
        {k: v for k, v in d.items() if k != "series"}
        for d in build_performance(q, date_from, date_to, customer, allowed)
    ]
```

- [ ] **Step 4: Extend `main.py`**

Replace the whole of `roles/scan_fleet_dashboard/files/app/main.py` with:

```python
"""
Systems One — Scan Fleet Monitoring Dashboard
=============================================
Fleet monitoring web app over S1_Remote_Monitoring (read-only).
Runs side-by-side with marketing_display until cutover.
"""
import asyncio
import datetime

from fastapi import FastAPI, HTTPException, Request

import auth
import db
import perf
import timeutil

app = FastAPI(title="S1 Scan Fleet Dashboard")


async def _exec(fn):
    """Run a builder in a thread; map errors to HTTP codes."""
    try:
        return await asyncio.get_event_loop().run_in_executor(None, fn)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=503, detail=str(exc))


def _allowed(request: Request):
    return auth.allowed_customers(db.query, auth.resolve_user(request))


def _default_range(days: int):
    t = timeutil.today_local()
    return (t - datetime.timedelta(days=days - 1)).isoformat(), t.isoformat()


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.get("/api/customers")
async def api_customers(request: Request):
    allowed = _allowed(request)

    def run():
        rows = db.query("SELECT DISTINCT customer FROM dbo.devices ORDER BY customer")
        return [r["customer"] for r in rows
                if allowed is None or r["customer"] in allowed]

    return await _exec(run)


@app.get("/api/machines")
async def api_machines(request: Request, customer: str = "",
                       date_from: str = "", date_to: str = ""):
    allowed = _allowed(request)
    if not (date_from and date_to):
        date_from, date_to = _default_range(30)
    return await _exec(lambda: perf.build_machines(
        db.query, date_from, date_to, customer or None, allowed))


@app.get("/api/performance")
async def api_performance(request: Request, customer: str = "",
                          date_from: str = "", date_to: str = ""):
    allowed = _allowed(request)
    if not (date_from and date_to):
        date_from, date_to = _default_range(30)
    return await _exec(lambda: perf.build_performance(
        db.query, date_from, date_to, customer or None, allowed))
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `python -m pytest roles/scan_fleet_dashboard/tests -v`
Expected: all PASS.

- [ ] **Step 6: Commit**

```bash
git add roles/scan_fleet_dashboard
git commit -m "feat(scan_fleet_dashboard): performance/machines/customers API"
```

---

### Task 5: Throughput API (spec §7)

**Files:**
- Create: `roles/scan_fleet_dashboard/files/app/throughput.py`
- Modify: `roles/scan_fleet_dashboard/files/app/main.py` (add endpoints + KPI cache)
- Test: `roles/scan_fleet_dashboard/tests/test_throughput.py`

**Interfaces:**
- Consumes: `perf.customer_scope`, `timeutil.*`, `config.THROUGHPUT_METRIC/SHIFTS/EXPECTED_PACKETS_PER_DAY`.
- Produces:
  - `throughput.build_kpis(q, date_from, date_to, customer=None, allowed=None) -> {fleet_parcels_per_hour, parcels_today, peak_hour, peak_value, busiest_machine, lowest_machine, shifts: {shifts: [{name, window, parcels}], packets_received, packets_missed, expected_per_day, interval_minutes}}`
  - `throughput.build_intraday(q, device_id=None, date=None, customer=None, allowed=None) -> {date, today: [{hour, parcels}]×24, yesterday: [{hour, parcels}]×24}` (zero-filled hours)
  - `throughput.build_by_machine(q, date_from, date_to, customer=None, allowed=None) -> [{device_id, display_name, customer, avg_per_hour, series: [{date, parcels_per_hour}]}]`
  - Endpoints: `GET /api/throughput/kpis` (TTL-cached `config.CACHE_TTL`s), `GET /api/throughput/intraday?device_id=&date=`, `GET /api/throughput/by-machine`. Defaults: last 14 days.
  - Note: shift summary is folded into the kpis response (one fetch for the KPI strip + shift card) — deliberate deviation from the spec's suggested endpoint list.

- [ ] **Step 1: Write the failing tests**

`roles/scan_fleet_dashboard/tests/test_throughput.py`:

```python
import throughput
from conftest import FakeQuery


def _h(dev=1, day="2026-07-01", hour=8, parcels=600, packets=12, name="Line-01"):
    return {"device_id": dev, "customer": "ACME", "location": "DC-1",
            "machine_name": name, "day": day, "hour_of_day": hour,
            "parcels": parcels, "packet_count": packets}


def fake(hourly, today_parcels=0):
    return (FakeQuery()
            .add("/* parcels_today */", [{"parcels": today_parcels}])
            .add("AS hour_of_day", hourly))


def test_kpis_rates_peak_and_extremes():
    rows = [
        _h(hour=8, parcels=600),
        _h(hour=9, parcels=1200),
        _h(dev=2, hour=9, parcels=300, name="Line-02"),
    ]
    k = throughput.build_kpis(fake(rows, today_parcels=4500),
                              "2026-07-01", "2026-07-01")
    # fleet per (day,hour): h8=600, h9=1500 -> avg 1050
    assert k["fleet_parcels_per_hour"] == 1050.0
    assert k["parcels_today"] == 4500
    assert k["peak_hour"] == "09:00"
    assert k["peak_value"] == 1500.0
    # dev1 avg/hr = (600+1200)/2 = 900; dev2 = 300/1
    assert k["busiest_machine"] == "DC-1 / Line-01"
    assert k["lowest_machine"] == "DC-1 / Line-02"


def test_kpis_shift_summary_and_missed_packets():
    rows = [
        _h(hour=8, parcels=600),                # Shift 1
        _h(hour=15, parcels=400),               # Shift 2
        _h(hour=23, parcels=100),               # Overnight
        _h(dev=2, hour=2, parcels=50, name="Line-02"),  # Overnight
    ]
    s = throughput.build_kpis(fake(rows), "2026-07-01", "2026-07-01")["shifts"]
    by_name = {x["name"]: x["parcels"] for x in s["shifts"]}
    assert by_name == {"Shift 1": 600, "Shift 2": 400, "Overnight": 150}
    # dev1: 36 packets -> missed 252; dev2: 12 -> missed 276
    assert s["packets_received"] == 48
    assert s["packets_missed"] == 252 + 276
    assert s["expected_per_day"] == 288
    assert s["interval_minutes"] == 5


def test_kpis_empty_range():
    k = throughput.build_kpis(fake([]), "2026-07-01", "2026-07-01")
    assert k["fleet_parcels_per_hour"] == 0
    assert k["peak_hour"] is None
    assert k["busiest_machine"] is None


def test_intraday_zero_fills_24_hours():
    out = throughput.build_intraday(fake([_h(hour=8, parcels=600)]),
                                    date="2026-07-01")
    assert len(out["today"]) == 24
    assert out["today"][8] == {"hour": 8, "parcels": 600}
    assert out["today"][0] == {"hour": 0, "parcels": 0}
    assert len(out["yesterday"]) == 24


def test_by_machine_daily_rate():
    rows = [
        _h(day="2026-07-01", hour=8, parcels=600),
        _h(day="2026-07-01", hour=9, parcels=1200),
        _h(day="2026-07-02", hour=8, parcels=500),
    ]
    out = throughput.build_by_machine(fake(rows), "2026-07-01", "2026-07-02")
    assert len(out) == 1
    d = out[0]
    # 2026-07-01: 1800 parcels over 2 observed hours -> 900/hr; 07-02: 500/1
    assert d["series"] == [
        {"date": "2026-07-01", "parcels_per_hour": 900.0},
        {"date": "2026-07-02", "parcels_per_hour": 500.0},
    ]
    assert d["avg_per_hour"] == 766.7           # 2300 parcels / 3 observed hours
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest roles/scan_fleet_dashboard/tests/test_throughput.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'throughput'`.

- [ ] **Step 3: Implement `throughput.py`**

`roles/scan_fleet_dashboard/files/app/throughput.py`:

```python
"""Tab 2 — throughput builders (spec §7).

Rates are per *observed* hour (hours that produced at least one packet), so a
machine idle half the day still shows its true running rate.
TODO(DECIDE): no capacity/target line yet; add via
thresholds.resolve_target(rows, customer, machine, location, ("throughput",)).
"""
import datetime

import config
import timeutil
from perf import customer_scope

_METRIC = config.THROUGHPUT_METRIC  # validated in config.py


def _display_name(r):
    return f"{r['location']} / {r['machine_name']}"


def _hourly_rows(q, start_utc, end_utc, scope_sql, scope_params):
    sql = f"""
        SELECT s.device_id, d.customer, d.location, d.machine_name,
               {timeutil.day_expr()} AS day,
               {timeutil.hour_of_day_expr()} AS hour_of_day,
               SUM(s.{_METRIC}) AS parcels,
               COUNT(*)         AS packet_count
        FROM dbo.device_statistics s
        JOIN dbo.devices d ON d.id = s.device_id
        WHERE s.ts_datetime >= ? AND s.ts_datetime < ?{scope_sql}
        GROUP BY s.device_id, d.customer, d.location, d.machine_name,
                 {timeutil.day_expr()}, {timeutil.hour_of_day_expr()}
        ORDER BY s.device_id, day, hour_of_day
    """
    return q(sql, tuple([start_utc, end_utc] + scope_params))


def _parcels_today(q, scope_sql, scope_params):
    start, end = timeutil.utc_bounds(timeutil.today_local(), timeutil.today_local())
    sql = f"""
        SELECT /* parcels_today */ COALESCE(SUM(s.{_METRIC}), 0) AS parcels
        FROM dbo.device_statistics s
        JOIN dbo.devices d ON d.id = s.device_id
        WHERE s.ts_datetime >= ? AND s.ts_datetime < ?{scope_sql}
    """
    rows = q(sql, tuple([start, end] + scope_params))
    return int(rows[0]["parcels"] or 0) if rows else 0


def _shift_of(hour):
    for name, lo, hi in config.SHIFTS:
        if lo < hi and lo <= hour < hi:
            return name
        if lo > hi and (hour >= lo or hour < hi):  # overnight wrap
            return name
    return config.SHIFTS[-1][0]


def build_kpis(q, date_from, date_to, customer=None, allowed=None):
    f, t = timeutil.parse_range(date_from, date_to)
    start, end = timeutil.utc_bounds(f, t)
    scope_sql, scope_params = customer_scope(customer, allowed)
    rows = _hourly_rows(q, start, end, scope_sql, scope_params)

    fleet_hours: dict[tuple, float] = {}      # (day, hour) -> fleet parcels
    machines: dict[int, dict] = {}            # device -> {name, parcels, hours}
    shift_parcels = {name: 0 for name, _, _ in config.SHIFTS}
    device_day_packets: dict[tuple, int] = {}  # (device, day) -> packets

    for r in rows:
        p = r["parcels"] or 0
        key = (r["day"], r["hour_of_day"])
        fleet_hours[key] = fleet_hours.get(key, 0) + p
        m = machines.setdefault(r["device_id"],
                                {"name": _display_name(r), "parcels": 0, "hours": 0})
        m["parcels"] += p
        m["hours"] += 1
        shift_parcels[_shift_of(r["hour_of_day"])] += p
        dd = (r["device_id"], r["day"])
        device_day_packets[dd] = device_day_packets.get(dd, 0) + (r["packet_count"] or 0)

    fleet_rate = (round(sum(fleet_hours.values()) / len(fleet_hours), 1)
                  if fleet_hours else 0)

    # Peak hour-of-day: average fleet parcels/hour per hour-of-day, take max.
    hod: dict[int, list] = {}
    for (day, hour), p in fleet_hours.items():
        hod.setdefault(hour, []).append(p)
    peak_hour, peak_value = None, None
    if hod:
        h = max(hod, key=lambda h: sum(hod[h]) / len(hod[h]))
        peak_hour = f"{h:02d}:00"
        peak_value = round(sum(hod[h]) / len(hod[h]), 1)

    rates = {dev: m["parcels"] / m["hours"]
             for dev, m in machines.items() if m["hours"]}
    busiest = machines[max(rates, key=rates.get)]["name"] if rates else None
    lowest = machines[min(rates, key=rates.get)]["name"] if rates else None

    received = sum(device_day_packets.values())
    missed = sum(max(0, config.EXPECTED_PACKETS_PER_DAY - c)
                 for c in device_day_packets.values())

    windows = {name: f"{lo:02d}:00–{hi:02d}:00" for name, lo, hi in config.SHIFTS}
    return {
        "fleet_parcels_per_hour": fleet_rate,
        "parcels_today": _parcels_today(q, scope_sql, scope_params),
        "peak_hour": peak_hour,
        "peak_value": peak_value,
        "busiest_machine": busiest,
        "lowest_machine": lowest,
        "shifts": {
            "shifts": [{"name": name, "window": windows[name],
                        "parcels": shift_parcels[name]}
                       for name, _, _ in config.SHIFTS],
            "packets_received": received,
            "packets_missed": missed,
            "expected_per_day": config.EXPECTED_PACKETS_PER_DAY,
            "interval_minutes": 5,
        },
    }


def _profile_for_date(q, day, device_id, scope_sql, scope_params):
    start, end = timeutil.utc_bounds(day, day)
    dev_sql = " AND s.device_id = ?" if device_id else ""
    params = [start, end] + scope_params + ([device_id] if device_id else [])
    rows = q(f"""
        SELECT {timeutil.hour_of_day_expr()} AS hour_of_day,
               SUM(s.{_METRIC}) AS parcels,
               COUNT(*)         AS packet_count
        FROM dbo.device_statistics s
        JOIN dbo.devices d ON d.id = s.device_id
        WHERE s.ts_datetime >= ? AND s.ts_datetime < ?{scope_sql}{dev_sql}
        GROUP BY {timeutil.hour_of_day_expr()}
        ORDER BY hour_of_day
    """, tuple(params))
    by_hour = {r["hour_of_day"]: int(r["parcels"] or 0) for r in rows}
    return [{"hour": h, "parcels": by_hour.get(h, 0)} for h in range(24)]


def build_intraday(q, device_id=None, date=None, customer=None, allowed=None):
    day = (datetime.date.fromisoformat(date) if date else timeutil.today_local())
    scope_sql, scope_params = customer_scope(customer, allowed)
    return {
        "date": day.isoformat(),
        "today": _profile_for_date(q, day, device_id, scope_sql, scope_params),
        "yesterday": _profile_for_date(
            q, day - datetime.timedelta(days=1), device_id, scope_sql, scope_params),
    }


def build_by_machine(q, date_from, date_to, customer=None, allowed=None):
    f, t = timeutil.parse_range(date_from, date_to)
    start, end = timeutil.utc_bounds(f, t)
    scope_sql, scope_params = customer_scope(customer, allowed)
    rows = _hourly_rows(q, start, end, scope_sql, scope_params)

    machines: dict[int, dict] = {}
    days: dict[tuple, list] = {}  # (device, day) -> [parcels, hours]
    for r in rows:
        machines.setdefault(r["device_id"], {
            "device_id": r["device_id"],
            "display_name": _display_name(r),
            "customer": r["customer"],
            "_parcels": 0, "_hours": 0,
        })
        m = machines[r["device_id"]]
        p = r["parcels"] or 0
        m["_parcels"] += p
        m["_hours"] += 1
        acc = days.setdefault((r["device_id"], r["day"]), [0, 0])
        acc[0] += p
        acc[1] += 1

    out = []
    for dev_id, m in machines.items():
        series = [
            {"date": day, "parcels_per_hour": round(v[0] / v[1], 1)}
            for (d_id, day), v in sorted(days.items()) if d_id == dev_id
        ]
        out.append({
            "device_id": m["device_id"],
            "display_name": m["display_name"],
            "customer": m["customer"],
            "avg_per_hour": round(m["_parcels"] / m["_hours"], 1) if m["_hours"] else 0,
            "series": series,
        })
    return sorted(out, key=lambda d: (d["customer"], d["display_name"]))
```

- [ ] **Step 4: Add endpoints + KPI cache to `main.py`**

Add to the imports in `roles/scan_fleet_dashboard/files/app/main.py`:

```python
import time

import config
import throughput
```

Append at the end of the file:

```python
# ---------------------------------------------------------------------------
# Throughput (spec §7). KPI responses cached CACHE_TTL seconds (spec §9).
# ---------------------------------------------------------------------------
_kpi_cache: dict = {}


@app.get("/api/throughput/kpis")
async def api_throughput_kpis(request: Request, customer: str = "",
                              date_from: str = "", date_to: str = ""):
    allowed = _allowed(request)
    if not (date_from and date_to):
        date_from, date_to = _default_range(14)
    key = (customer, date_from, date_to,
           tuple(sorted(allowed)) if allowed is not None else None)
    hit = _kpi_cache.get(key)
    if hit and time.monotonic() - hit[0] < config.CACHE_TTL:
        return hit[1]
    data = await _exec(lambda: throughput.build_kpis(
        db.query, date_from, date_to, customer or None, allowed))
    _kpi_cache[key] = (time.monotonic(), data)
    return data


@app.get("/api/throughput/intraday")
async def api_throughput_intraday(request: Request, device_id: int = 0,
                                  date: str = "", customer: str = ""):
    allowed = _allowed(request)
    return await _exec(lambda: throughput.build_intraday(
        db.query, device_id or None, date or None, customer or None, allowed))


@app.get("/api/throughput/by-machine")
async def api_throughput_by_machine(request: Request, customer: str = "",
                                    date_from: str = "", date_to: str = ""):
    allowed = _allowed(request)
    if not (date_from and date_to):
        date_from, date_to = _default_range(14)
    return await _exec(lambda: throughput.build_by_machine(
        db.query, date_from, date_to, customer or None, allowed))
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `python -m pytest roles/scan_fleet_dashboard/tests -v`
Expected: all PASS.

- [ ] **Step 6: Commit**

```bash
git add roles/scan_fleet_dashboard
git commit -m "feat(scan_fleet_dashboard): throughput API (KPIs, shifts, intraday, per-machine)"
```

---

### Task 6: Device drill-down API (spec §8)

**Files:**
- Create: `roles/scan_fleet_dashboard/files/app/device.py`
- Modify: `roles/scan_fleet_dashboard/files/app/main.py` (add endpoints)
- Test: `roles/scan_fleet_dashboard/tests/test_device.py`

**Interfaces:**
- Consumes: `perf.pct`, `thresholds.*`, `timeutil.*`, `config.EXPECTED_PACKETS_PER_DAY`.
- Produces:
  - `device.build_summary(q, device_id) -> {device: {id, serial_number, customer, location, machine_name, display_name, os_version}, status: {online, offline_since, app_running, stopped_since, badge}, kpis: {good_read_pct, target_pct, below_target, uptime_pct, cpu_percent, mem_usage_pct, temp_celsius, max_disk_pct}, storage: [drive rows]}`. `badge` is `"offline"` | `"below target"` | `"online"` (that priority). Raises `ValueError("device not found")` for unknown id (→ 400 is acceptable; the UI treats any error as not-found).
  - `device.build_outcomes(q, device_id, date_from, date_to, bucket="day") -> {bucket, totals: {…9 outcome fields + total_items + pcts}, series: [{date|ts, total_items, good_read, no_read, no_dimension, no_weight, item_out_of_spec, more_than_1_item, hand_scanned, image_sent, image_not_sent, good_read_pct}]}` — `bucket="5min"` returns raw packet rows (local ts) and is meant for a single picked day.
  - `device.build_health(q, device_id, date_from, date_to) -> {series: [{ts, cpu_percent, mem_usage_pct, temp_celsius}], storage: [{drive, total_gb, free_gb, used_gb, usage_percent}]}`
  - `device.build_alerts(q, device_id, date_from, date_to) -> [{time, metric, detail, severity}]` — derived from daily good_read threshold breaches (warn/bad). TODO: replace with the real alerting pipeline.
  - Endpoints: `GET /api/device/{id}/summary|outcomes|health|alerts`, `POST /api/device/{id}/actions/{action}` → 501 stub.

- [ ] **Step 1: Write the failing tests**

`roles/scan_fleet_dashboard/tests/test_device.py`:

```python
import pytest

import device
from conftest import FakeQuery


def _base_fake():
    return (FakeQuery()
        .add("FROM dbo.devices WHERE", [{
            "id": 7, "serial_number": "SN-777", "customer": "ACME",
            "location": "DC-1", "machine_name": "Line-07"}])
        .add("dbo.device_status", [{
            "status": "online", "offline_since": None,
            "ts_datetime": "2026-07-20T08:00:00"}])
        .add("dbo.device_application_status", [{
            "application_running": True, "stopped_since": None,
            "ts_datetime": "2026-07-20T08:00:00"}])
        .add("dbo.device_os_metrics", [{
            "cpu_percent": 21.5, "mem_usage_pct": 63.0, "temp_celsius": 48.0,
            "ts_datetime": "2026-07-20T08:00:00"}])
        .add("dbo.device_os_status", [{"os_version": "Win11 23H2"}])
        .add("dbo.device_storage_status", [
            {"drive": "C:", "total_gb": 256, "free_gb": 100, "used_gb": 156,
             "usage_percent": 61.0, "ts_datetime": "2026-07-20T08:00:00"}])
        .add("dbo.alert_thresholds", [])
        .add("/* today_stats */", [{"total_items": 1000, "good_read": 940,
                                    "packet_count": 144}]))


def test_summary_composes_latest_state():
    s = device.build_summary(_base_fake(), 7)
    assert s["device"]["display_name"] == "DC-1 / Line-07"
    assert s["device"]["os_version"] == "Win11 23H2"
    assert s["kpis"]["good_read_pct"] == 94.0
    assert s["kpis"]["target_pct"] == 93.0
    assert s["kpis"]["uptime_pct"] == 50.0     # 144 of 288 packets
    assert s["kpis"]["max_disk_pct"] == 61.0
    assert s["status"]["badge"] == "online"


def test_summary_badge_priority_offline_beats_below_target():
    q = _base_fake()
    q.routes = [("dbo.device_status", [{"status": "offline",
                 "offline_since": "2026-07-20T06:00:00",
                 "ts_datetime": "2026-07-20T06:00:00"}])] + \
               [r for r in q.routes if r[0] != "dbo.device_status"]
    assert device.build_summary(q, 7)["status"]["badge"] == "offline"


def test_summary_unknown_device_raises():
    q = FakeQuery().add("FROM dbo.devices WHERE", [])
    with pytest.raises(ValueError):
        device.build_summary(q, 999)


def test_outcomes_daily_pcts():
    q = FakeQuery().add("dbo.device_statistics", [{
        "bucket": "2026-07-01", "total_items": 1000, "good_read": 950,
        "no_read": 30, "no_dimension": 10, "no_weight": 5,
        "item_out_of_spec": 5, "more_than_1_item": 2, "hand_scanned": 8,
        "image_sent": 990, "image_not_sent": 10}])
    out = device.build_outcomes(q, 7, "2026-07-01", "2026-07-01")
    assert out["bucket"] == "day"
    assert out["series"][0]["good_read_pct"] == 95.0
    assert out["totals"]["good_read_pct"] == 95.0
    assert out["totals"]["hand_scanned"] == 8


def test_outcomes_5min_requires_single_day():
    q = FakeQuery().add("dbo.device_statistics", [])
    with pytest.raises(ValueError):
        device.build_outcomes(q, 7, "2026-07-01", "2026-07-02", bucket="5min")


def test_alerts_derived_from_breaches():
    q = (FakeQuery()
         .add("dbo.alert_thresholds", [{
             "customer": "ACME", "machine_name": "Line-07", "location": "DC-1",
             "metric": "good_read_pct", "direction": "low",
             "warn_value": 95.0, "bad_value": 91.0}])
         .add("FROM dbo.devices WHERE", [{
             "id": 7, "serial_number": "SN-777", "customer": "ACME",
             "location": "DC-1", "machine_name": "Line-07"}])
         .add("dbo.device_statistics", [
             {"bucket": "2026-07-01", "total_items": 1000, "good_read": 960,
              "no_read": 0, "no_dimension": 0, "no_weight": 0,
              "item_out_of_spec": 0, "more_than_1_item": 0, "hand_scanned": 0,
              "image_sent": 0, "image_not_sent": 0},   # 96% — fine
             {"bucket": "2026-07-02", "total_items": 1000, "good_read": 930,
              "no_read": 0, "no_dimension": 0, "no_weight": 0,
              "item_out_of_spec": 0, "more_than_1_item": 0, "hand_scanned": 0,
              "image_sent": 0, "image_not_sent": 0},   # 93% — warn
             {"bucket": "2026-07-03", "total_items": 1000, "good_read": 900,
              "no_read": 0, "no_dimension": 0, "no_weight": 0,
              "item_out_of_spec": 0, "more_than_1_item": 0, "hand_scanned": 0,
              "image_sent": 0, "image_not_sent": 0},   # 90% — bad
         ]))
    alerts = device.build_alerts(q, 7, "2026-07-01", "2026-07-03")
    assert [(a["time"], a["severity"]) for a in alerts] == [
        ("2026-07-03", "bad"), ("2026-07-02", "warn")]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest roles/scan_fleet_dashboard/tests/test_device.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'device'`.

- [ ] **Step 3: Implement `device.py`**

`roles/scan_fleet_dashboard/files/app/device.py`:

```python
"""Single-device drill-down builders (spec §8) + fleet health (Task 11)."""
import config
import thresholds
import timeutil
from perf import pct

OUTCOME_FIELDS = (
    "good_read", "no_read", "no_dimension", "no_weight", "item_out_of_spec",
    "more_than_1_item", "hand_scanned", "image_sent", "image_not_sent",
)

_DEVICE_SQL = """
    SELECT id, serial_number, customer, location, machine_name
    FROM dbo.devices WHERE id = ?
"""
_STATUS_SQL = """
    SELECT TOP 1 status, offline_since, ts_datetime
    FROM dbo.device_status WHERE device_id = ? ORDER BY ts_datetime DESC
"""
_APP_SQL = """
    SELECT TOP 1 application_running, stopped_since, ts_datetime
    FROM dbo.device_application_status WHERE device_id = ? ORDER BY ts_datetime DESC
"""
_OS_METRICS_SQL = """
    SELECT TOP 1 cpu_percent, mem_usage_pct, temp_celsius, ts_datetime
    FROM dbo.device_os_metrics WHERE device_id = ? ORDER BY ts_datetime DESC
"""
_OS_VERSION_SQL = "SELECT TOP 1 os_version FROM dbo.device_os_status WHERE device_id = ?"
_STORAGE_SQL = """
    SELECT drive, total_gb, free_gb, used_gb, usage_percent, ts_datetime
    FROM dbo.device_storage_status WHERE device_id = ? ORDER BY drive
"""


def _one(rows):
    return rows[0] if rows else None


def _get_device(q, device_id):
    d = _one(q(_DEVICE_SQL, (device_id,)))
    if d is None:
        raise ValueError("device not found")
    d["display_name"] = f"{d['location']} / {d['machine_name']}"
    return d


def build_summary(q, device_id):
    d = _get_device(q, device_id)
    status = _one(q(_STATUS_SQL, (device_id,))) or {}
    app_st = _one(q(_APP_SQL, (device_id,))) or {}
    metrics = _one(q(_OS_METRICS_SQL, (device_id,))) or {}
    os_ver = _one(q(_OS_VERSION_SQL, (device_id,))) or {}
    storage = q(_STORAGE_SQL, (device_id,))
    th = thresholds.load_thresholds(q)

    start, end = timeutil.utc_bounds(timeutil.today_local(), timeutil.today_local())
    today = _one(q("""
        SELECT /* today_stats */
               COALESCE(SUM(total_items), 0) AS total_items,
               COALESCE(SUM(good_read), 0)   AS good_read,
               COUNT(*)                      AS packet_count
        FROM dbo.device_statistics
        WHERE device_id = ? AND ts_datetime >= ? AND ts_datetime < ?
    """, (device_id, start, end))) or {"total_items": 0, "good_read": 0, "packet_count": 0}

    target = thresholds.good_read_target(th, d["customer"], d["machine_name"], d["location"])
    good_pct = pct(today["good_read"], today["total_items"])
    below = good_pct is not None and good_pct < target
    online = (status.get("status") == "online")

    if status.get("status") == "offline":
        badge = "offline"
    elif below:
        badge = "below target"
    else:
        badge = "online"

    # Uptime proxy: packets received today vs the 288 expected.
    # TODO: replace with a real uptime source if one lands in the schema.
    uptime = round(100.0 * today["packet_count"] / config.EXPECTED_PACKETS_PER_DAY, 1)

    return {
        "device": {**d, "os_version": os_ver.get("os_version")},
        "status": {
            "online": online,
            "offline_since": status.get("offline_since"),
            "app_running": bool(app_st.get("application_running", False)),
            "stopped_since": app_st.get("stopped_since"),
            "badge": badge,
        },
        "kpis": {
            "good_read_pct": good_pct,
            "target_pct": target,
            "below_target": below,
            "uptime_pct": min(uptime, 100.0),
            "cpu_percent": metrics.get("cpu_percent"),
            "mem_usage_pct": metrics.get("mem_usage_pct"),
            "temp_celsius": metrics.get("temp_celsius"),
            "max_disk_pct": max((s["usage_percent"] for s in storage), default=None),
        },
        "storage": storage,
    }


def build_outcomes(q, device_id, date_from, date_to, bucket="day"):
    f, t = timeutil.parse_range(date_from, date_to)
    if bucket not in ("day", "5min"):
        raise ValueError("bucket must be day or 5min")
    if bucket == "5min" and f != t:
        raise ValueError("5min bucket requires a single day (date_from == date_to)")
    start, end = timeutil.utc_bounds(f, t)

    sums = ", ".join(f"SUM(s.{c}) AS {c}" for c in OUTCOME_FIELDS)
    if bucket == "day":
        sql = f"""
            SELECT {timeutil.day_expr()} AS bucket,
                   SUM(s.total_items) AS total_items, {sums}
            FROM dbo.device_statistics s
            WHERE s.device_id = ? AND s.ts_datetime >= ? AND s.ts_datetime < ?
            GROUP BY {timeutil.day_expr()}
            ORDER BY bucket
        """
    else:
        cols = ", ".join(f"s.{c}" for c in OUTCOME_FIELDS)
        sql = f"""
            SELECT {timeutil.local_expr()} AS bucket, s.total_items, {cols}
            FROM dbo.device_statistics s
            WHERE s.device_id = ? AND s.ts_datetime >= ? AND s.ts_datetime < ?
            ORDER BY bucket
        """
    rows = q(sql, (device_id, start, end))

    series, totals = [], {c: 0 for c in OUTCOME_FIELDS}
    totals["total_items"] = 0
    for r in rows:
        total = r["total_items"] or 0
        point = {"date": r["bucket"], "total_items": total}
        for c in OUTCOME_FIELDS:
            point[c] = r[c] or 0
            totals[c] += r[c] or 0
        point["good_read_pct"] = pct(point["good_read"], total)
        totals["total_items"] += total
        series.append(point)
    totals["good_read_pct"] = pct(totals["good_read"], totals["total_items"])
    return {"bucket": bucket, "totals": totals, "series": series}


def build_health(q, device_id, date_from, date_to):
    f, t = timeutil.parse_range(date_from, date_to)
    start, end = timeutil.utc_bounds(f, t)
    series = q(f"""
        SELECT {timeutil.local_expr("m.ts_datetime")} AS ts,
               m.cpu_percent, m.mem_usage_pct, m.temp_celsius
        FROM dbo.device_os_metrics m
        WHERE m.device_id = ? AND m.ts_datetime >= ? AND m.ts_datetime < ?
        ORDER BY ts
    """, (device_id, start, end))
    return {"series": series, "storage": q(_STORAGE_SQL, (device_id,))}


def build_alerts(q, device_id, date_from, date_to):
    """Derive alerts from daily good_read threshold breaches, newest first.

    TODO: replace with the real alerting pipeline when one exists.
    """
    d = _get_device(q, device_id)
    th = thresholds.load_thresholds(q)
    warn, bad = thresholds.warn_bad(
        th, d["customer"], d["machine_name"], d["location"],
        thresholds.GOOD_READ_METRICS)
    if warn is None and bad is None:
        warn = bad = thresholds.good_read_target(
            th, d["customer"], d["machine_name"], d["location"])

    out = []
    daily = build_outcomes(q, device_id, date_from, date_to)["series"]
    for p in daily:
        g = p["good_read_pct"]
        if g is None:
            continue
        if bad is not None and g < bad:
            sev = "bad"
        elif warn is not None and g < warn:
            sev = "warn"
        else:
            continue
        limit = bad if sev == "bad" else warn
        out.append({"time": p["date"], "metric": "good_read_pct",
                    "detail": f"good read {g}% below {sev} threshold {limit}%",
                    "severity": sev})
    return sorted(out, key=lambda a: a["time"], reverse=True)
```

- [ ] **Step 4: Add endpoints to `main.py`**

Add `import device` to the imports, then append:

```python
# ---------------------------------------------------------------------------
# Single-device drill-down (spec §8)
# ---------------------------------------------------------------------------
@app.get("/api/device/{device_id}/summary")
async def api_device_summary(device_id: int):
    return await _exec(lambda: device.build_summary(db.query, device_id))


@app.get("/api/device/{device_id}/outcomes")
async def api_device_outcomes(device_id: int, date_from: str = "",
                              date_to: str = "", bucket: str = "day"):
    if not (date_from and date_to):
        date_from, date_to = _default_range(30)
    return await _exec(lambda: device.build_outcomes(
        db.query, device_id, date_from, date_to, bucket))


@app.get("/api/device/{device_id}/health")
async def api_device_health(device_id: int, date_from: str = "", date_to: str = ""):
    if not (date_from and date_to):
        date_from, date_to = _default_range(7)
    return await _exec(lambda: device.build_health(
        db.query, device_id, date_from, date_to))


@app.get("/api/device/{device_id}/alerts")
async def api_device_alerts(device_id: int, date_from: str = "", date_to: str = ""):
    if not (date_from and date_to):
        date_from, date_to = _default_range(30)
    return await _exec(lambda: device.build_alerts(
        db.query, device_id, date_from, date_to))


@app.post("/api/device/{device_id}/actions/{action}")
async def api_device_action(device_id: int, action: str):
    # TODO: wire Acknowledge alert / Restart agent / Export diagnostics to real
    # endpoints (MQTT command topic or agent API) once they exist.
    raise HTTPException(status_code=501, detail=f"action '{action}' not implemented yet")
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `python -m pytest roles/scan_fleet_dashboard/tests -v`
Expected: all PASS.

- [ ] **Step 6: Commit**

```bash
git add roles/scan_fleet_dashboard
git commit -m "feat(scan_fleet_dashboard): device drill-down API (summary, outcomes, health, derived alerts)"
```

---

### Task 7: Frontend shell — layout, router, filters, palette, static serving

**Files:**
- Create: `roles/scan_fleet_dashboard/files/app/static/index.html`
- Create: `roles/scan_fleet_dashboard/files/app/static/css/app.css`
- Create: `roles/scan_fleet_dashboard/files/app/static/js/app.js`
- Create: `roles/scan_fleet_dashboard/files/app/static/vendor/chart.umd.js` (vendored)
- Create: empty placeholders so index.html's script tags resolve: `static/js/symlog.js`, `static/js/charts.js`, `static/js/performance.js`, `static/js/throughput.js`, `static/js/health.js`, `static/js/device.js` — each containing only a comment header naming its task (they get real content in Tasks 8–11; the router calls their render functions, which until then are defined as stubs in the placeholder file, e.g. `async function renderPerformance() { content().innerHTML = '<p class="muted">Tab lands in Task 8.</p>'; }`)
- Modify: `roles/scan_fleet_dashboard/files/app/main.py` (static mount, redirect, SPA catch-all)
- Test: `roles/scan_fleet_dashboard/tests/test_static.py` (create)

**Interfaces:**
- Consumes: all `/api/*` endpoints from Tasks 4–6.
- Produces (globals used by later JS tasks — plain `<script>` load order: vendor → symlog → charts → tabs → app):
  - `State` object `{preset, from, to, customer, page}`; `rangeFor(tab) -> {from, to}` (tab defaults: performance 30d, throughput 14d, health 30d).
  - `api(path, params) -> Promise<json>`; `navigate(path)`; `content() -> el`; `esc(s)` HTML-escaper.
  - `pagerHtml(pages)` / `bindPager(pages, rerender)`; `PAGE_SIZE = 12`.
  - Route table: `/machines/performance|throughput|health` → `renderPerformance()|renderThroughput()|renderHealth()`; `/machines/<digits>` → `renderDevice(id)` (tab bar hidden); `/` redirects.
- Server: `GET /` → 302 `/machines/performance`; `GET /machines/{rest}` serves `index.html` (no-cache headers); `/static/*` mounted.

- [ ] **Step 1: Write the failing test**

`roles/scan_fleet_dashboard/tests/test_static.py`:

```python
import os

from fastapi.testclient import TestClient

import main

APP_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "files", "app")
client = TestClient(main.app)


def test_root_redirects_to_performance_tab():
    r = client.get("/", follow_redirects=False)
    assert r.status_code == 307 or r.status_code == 302
    assert r.headers["location"] == "/machines/performance"


def test_spa_routes_serve_index_uncached():
    for path in ("/machines/performance", "/machines/throughput",
                 "/machines/health", "/machines/42"):
        r = client.get(path)
        assert r.status_code == 200, path
        assert "Scan performance" in r.text
        assert "no-store" in r.headers["cache-control"]


def test_vendored_chartjs_is_committed_and_real():
    p = os.path.join(APP_DIR, "static", "vendor", "chart.umd.js")
    assert os.path.getsize(p) > 100_000  # a stub/empty download would be tiny


def test_palette_variables_present():
    with open(os.path.join(APP_DIR, "static", "css", "app.css"), encoding="utf-8") as fh:
        css = fh.read()
    for var in ("--teal: #2f8fa0", "--orange: #e07b39", "--plum: #8a4b74",
                "--slate: #5b6b8c", "--red: #c0392b", "--navy: #14213d"):
        assert var in css, var
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest roles/scan_fleet_dashboard/tests/test_static.py -v`
Expected: FAIL — redirect route missing (404) and files missing.

- [ ] **Step 3: Vendor Chart.js**

```bash
curl -fsSL -o roles/scan_fleet_dashboard/files/app/static/vendor/chart.umd.js https://cdn.jsdelivr.net/npm/chart.js@4.4.9/dist/chart.umd.min.js
```

Verify: `python -c "print(__import__('os').path.getsize('roles/scan_fleet_dashboard/files/app/static/vendor/chart.umd.js'))"` prints > 100000.

- [ ] **Step 4: Create `index.html`**

`roles/scan_fleet_dashboard/files/app/static/index.html`:

```html
<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Systems One — Scan Fleet</title>
<link rel="stylesheet" href="/static/css/app.css">
</head>
<body>
<div class="shell">
  <aside class="sidebar">
    <div class="logo">Systems One</div>
    <nav>
      <a class="nav-item" href="#">Dashboard</a>
      <a class="nav-item active" href="/machines/performance" data-link>Machines</a>
      <a class="nav-item" href="#">Scan Log</a>
      <a class="nav-item" href="#">Alerts</a>
      <a class="nav-item" href="#">Thresholds</a>
      <a class="nav-item" href="#">Reports</a>
      <a class="nav-item" href="#">Admin</a>
    </nav>
    <div class="sidebar-footer">s1 · scan fleet</div>
  </aside>
  <div class="main">
    <header class="topbar">
      <h1 id="page-title">Machines</h1>
      <input class="search" type="search" placeholder="Search…">
      <div class="avatar">S1</div>
    </header>
    <div class="tabrow" id="tabrow">
      <nav class="tabs">
        <a href="/machines/performance" data-link data-tab="performance">Scan performance</a>
        <a href="/machines/throughput" data-link data-tab="throughput">Throughput</a>
        <a href="/machines/health" data-link data-tab="health">Device health</a>
      </nav>
      <div class="filters">
        <select id="range-preset" title="Date range">
          <option value="">Default for tab</option>
          <option value="today">Today</option>
          <option value="7">Last 7 days</option>
          <option value="14">Last 14 days</option>
          <option value="30">Last 30 days</option>
          <option value="custom">Custom</option>
        </select>
        <span id="custom-range" class="hidden">
          <input type="date" id="date-from"> – <input type="date" id="date-to">
        </span>
        <select id="customer-filter" title="Customer">
          <option value="">All customers</option>
        </select>
      </div>
    </div>
    <main id="content"></main>
  </div>
</div>
<script src="/static/vendor/chart.umd.js"></script>
<script src="/static/js/symlog.js"></script>
<script src="/static/js/charts.js"></script>
<script src="/static/js/performance.js"></script>
<script src="/static/js/throughput.js"></script>
<script src="/static/js/health.js"></script>
<script src="/static/js/device.js"></script>
<script src="/static/js/app.js"></script>
</body>
</html>
```

- [ ] **Step 5: Create `app.css`**

`roles/scan_fleet_dashboard/files/app/static/css/app.css`:

```css
/* Palette — spec §4. Series colours are fixed by metric, never by position. */
:root {
  --teal: #2f8fa0;
  --orange: #e07b39;
  --plum: #8a4b74;
  --slate: #5b6b8c;
  --red: #c0392b;
  --navy: #14213d;
  --blue: #2563eb;
  --text: #3a3a3a;
  --muted: #8a8e94;
  --border: #dee1e5;
  --card: #ffffff;
  --page: #f3f4f6;
  --sidebar-active: #2154bf;
  --tab-text: #0d7969;
}

* { box-sizing: border-box; margin: 0; padding: 0; }
body { font: 14px/1.45 system-ui, "Segoe UI", sans-serif; color: var(--text); background: var(--page); }
a { color: inherit; text-decoration: none; }
.hidden { display: none !important; }
.muted { color: var(--muted); }
.small { font-size: 12px; }
.error { color: var(--red); padding: 16px; }

.shell { display: flex; min-height: 100vh; }

/* Sidebar */
.sidebar { width: 220px; flex: none; background: var(--navy); color: #dfe4ef; display: flex; flex-direction: column; }
.logo { font-weight: 700; font-size: 17px; color: #fff; padding: 20px 18px; }
.sidebar nav { flex: 1; }
.nav-item { display: block; padding: 10px 18px; font-size: 13.5px; opacity: .85; }
.nav-item:hover { background: rgba(255,255,255,.06); }
.nav-item.active { background: var(--sidebar-active); color: #fff; opacity: 1; }
.sidebar-footer { padding: 14px 18px; font-size: 12px; opacity: .6; }

/* Header */
.main { flex: 1; min-width: 0; display: flex; flex-direction: column; }
.topbar { height: 76px; flex: none; background: #fff; border-bottom: 1px solid var(--border); display: flex; align-items: center; gap: 16px; padding: 0 24px; }
.topbar h1 { font-size: 19px; }
.search { margin-left: auto; width: 220px; padding: 7px 12px; border: 1px solid var(--border); border-radius: 6px; }
.avatar { width: 34px; height: 34px; border-radius: 50%; background: var(--blue); color: #fff; display: flex; align-items: center; justify-content: center; font-size: 12px; font-weight: 600; }

/* Tab bar + filters */
.tabrow { display: flex; align-items: center; background: #fff; border-bottom: 1px solid var(--border); padding: 0 24px; }
.tabs { display: flex; gap: 4px; }
.tabs a { padding: 12px 14px; font-size: 13.5px; border-bottom: 2.5px solid transparent; }
.tabs a.active { color: var(--tab-text); border-bottom-color: var(--teal); font-weight: 600; }
.filters { margin-left: auto; display: flex; align-items: center; gap: 8px; padding: 8px 0; }
.filters select, .filters input { padding: 6px 8px; border: 1px solid var(--border); border-radius: 6px; background: #fff; font-size: 13px; }

/* Content */
#content { padding: 20px 24px; }
.card { background: var(--card); border: 1px solid var(--border); border-radius: 8px; padding: 12px; }

/* Legend (shared, above the grid — spec §5) */
.chart-legend { display: flex; flex-wrap: wrap; gap: 14px; margin-bottom: 12px; font-size: 12.5px; }
.legend-item { display: inline-flex; align-items: center; gap: 6px; }
.legend-item i { width: 14px; height: 3px; border-radius: 2px; display: inline-block; }
.legend-item i.dash { background: repeating-linear-gradient(90deg, var(--red) 0 4px, transparent 4px 7px); height: 2px; }

/* Small-multiples grid: 3 × 4 = 12 per page */
.grid { display: grid; grid-template-columns: repeat(3, 1fr); gap: 14px; }
@media (max-width: 1100px) { .grid { grid-template-columns: repeat(2, 1fr); } }
.card.mini { cursor: pointer; transition: border-color .12s, box-shadow .12s; position: relative; }
.card.mini:hover { border-color: var(--blue); box-shadow: 0 1px 6px rgba(37,99,235,.18); }
.card.mini:hover::after { content: "open device ›"; position: absolute; right: 10px; bottom: 8px; font-size: 11px; color: var(--blue); }
.card-head { display: flex; justify-content: space-between; align-items: flex-start; gap: 8px; margin-bottom: 6px; }
.card-head b { font-size: 13px; }
.pct { font-weight: 700; font-size: 14px; }
.pct.good { color: var(--teal); }
.pct.bad { color: var(--red); }
.mini-plot { height: 150px; }
.plot-lg { height: 300px; }
.plot-sm { height: 120px; }

/* KPI cards */
.kpi-row { display: grid; grid-template-columns: repeat(5, 1fr); gap: 14px; margin-bottom: 16px; }
.kpi .kpi-value { font-size: 21px; font-weight: 700; }
.kpi.bad .kpi-value { color: var(--red); }
.two-col { display: grid; grid-template-columns: 2fr 1fr; gap: 14px; margin-bottom: 16px; }
@media (max-width: 1100px) { .kpi-row { grid-template-columns: repeat(2, 1fr); } .two-col { grid-template-columns: 1fr; } }

/* Pagination */
.pager { display: flex; justify-content: center; align-items: center; gap: 14px; margin: 16px 0; }
.pager button { padding: 6px 14px; border: 1px solid var(--border); background: #fff; border-radius: 6px; cursor: pointer; }
.pager button:disabled { opacity: .4; cursor: default; }

/* Tables */
.table { width: 100%; border-collapse: collapse; font-size: 13px; }
.table th { text-align: left; color: var(--muted); font-weight: 600; padding: 6px 8px; border-bottom: 1px solid var(--border); }
.table td { padding: 6px 8px; border-bottom: 1px solid var(--border); }
.table tr.click { cursor: pointer; }
.table tr.click:hover { background: #f6f8fb; }

/* Drill-down */
.crumb { color: var(--muted); font-size: 13px; margin-bottom: 4px; }
.device-head { display: flex; align-items: center; gap: 12px; margin-bottom: 16px; }
.device-head h2 { font-size: 18px; }
.badge { padding: 3px 10px; border-radius: 999px; font-size: 12px; font-weight: 600; }
.badge.online { background: #e2f2ee; color: var(--tab-text); }
.badge.offline { background: #f7e3e0; color: var(--red); }
.badge.below { background: #fdf1e4; color: var(--orange); }
.actions { margin-left: auto; display: flex; gap: 8px; }
.actions button { padding: 7px 12px; border: 1px solid var(--border); background: #fff; border-radius: 6px; cursor: pointer; font-size: 13px; }
h3 { font-size: 14.5px; margin: 6px 0 10px; }
.section { margin-bottom: 16px; }
```

- [ ] **Step 6: Create `app.js` and placeholder tab files**

`roles/scan_fleet_dashboard/files/app/static/js/app.js`:

```js
/* Router + shared filter state. Loaded last — tab renderers already defined. */
const State = {
  preset: '',            // '' = use the tab's default
  from: null, to: null,  // custom range (YYYY-MM-DD)
  customer: '',
  page: 0,
};
const TAB_DEFAULT_PRESET = { performance: '30', throughput: '14', health: '30' };
const PAGE_SIZE = 12;    // 3 × 4 grid (spec §5)

const content = () => document.getElementById('content');

function esc(s) {
  return String(s ?? '').replace(/[&<>"']/g,
    c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}

function fmtLocalDate(d) {
  const p = n => String(n).padStart(2, '0');
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}`;
}

function rangeFor(tab) {
  const preset = State.preset || TAB_DEFAULT_PRESET[tab] || '30';
  if (preset === 'custom' && State.from && State.to)
    return { from: State.from, to: State.to };
  const now = new Date();
  const to = fmtLocalDate(now);
  if (preset === 'today') return { from: to, to };
  const from = new Date(now);
  from.setDate(from.getDate() - (Number(preset) - 1));
  return { from: fmtLocalDate(from), to };
}

async function api(path, params) {
  const qs = params
    ? '?' + new URLSearchParams(
        Object.entries(params).filter(([, v]) => v !== '' && v != null)).toString()
    : '';
  const r = await fetch(path + qs);
  if (!r.ok) {
    let detail = `HTTP ${r.status}`;
    try { detail = (await r.json()).detail || detail; } catch (e) { /* keep */ }
    throw new Error(detail);
  }
  return r.json();
}

function navigate(path) { history.pushState(null, '', path); route(); }

function pagerHtml(pages) {
  if (pages <= 1) return '';
  return `<div class="pager">
    <button id="pg-prev" ${State.page === 0 ? 'disabled' : ''}>‹ Prev</button>
    <span>Page ${State.page + 1} / ${pages}</span>
    <button id="pg-next" ${State.page >= pages - 1 ? 'disabled' : ''}>Next ›</button>
  </div>`;
}

function bindPager(pages, rerender) {
  const prev = document.getElementById('pg-prev');
  const next = document.getElementById('pg-next');
  if (prev) prev.addEventListener('click', () => { State.page--; rerender(); });
  if (next) next.addEventListener('click', () => { State.page++; rerender(); });
}

function setActiveTab(tab) {
  document.getElementById('tabrow').classList.remove('hidden');
  document.querySelectorAll('.tabs a').forEach(a =>
    a.classList.toggle('active', a.dataset.tab === tab));
}

function route() {
  const p = location.pathname;
  let m;
  if (p === '/' || p === '/machines') return navigate('/machines/performance');
  if (p === '/machines/performance') { setActiveTab('performance'); return renderPerformance(); }
  if (p === '/machines/throughput')  { setActiveTab('throughput');  return renderThroughput(); }
  if (p === '/machines/health')      { setActiveTab('health');      return renderHealth(); }
  if ((m = p.match(/^\/machines\/(\d+)$/))) {
    document.getElementById('tabrow').classList.add('hidden');
    return renderDevice(Number(m[1]));
  }
  content().innerHTML = '<p class="muted">Not found.</p>';
}

document.addEventListener('click', e => {
  const a = e.target.closest('a[data-link]');
  if (!a) return;
  e.preventDefault();
  State.page = 0;
  navigate(a.getAttribute('href'));
});
window.addEventListener('popstate', route);

async function initFilters() {
  document.getElementById('range-preset').addEventListener('change', e => {
    State.preset = e.target.value;
    State.page = 0;
    document.getElementById('custom-range')
      .classList.toggle('hidden', State.preset !== 'custom');
    if (State.preset !== 'custom') route();
  });
  for (const id of ['date-from', 'date-to']) {
    document.getElementById(id).addEventListener('change', () => {
      State.from = document.getElementById('date-from').value;
      State.to = document.getElementById('date-to').value;
      State.page = 0;
      if (State.from && State.to) route();
    });
  }
  document.getElementById('customer-filter').addEventListener('change', e => {
    State.customer = e.target.value;
    State.page = 0;
    route();
  });
  try {
    const customers = await api('/api/customers');
    const sel = document.getElementById('customer-filter');
    for (const c of customers)
      sel.insertAdjacentHTML('beforeend',
        `<option value="${esc(c)}">${esc(c)}</option>`);
  } catch (e) { console.error('customers load failed:', e); }
}

initFilters().then(route);
```

Placeholder files (each replaced by its real task later) — e.g. `static/js/performance.js`:

```js
/* Tab 1 — implemented in Task 8. */
async function renderPerformance() {
  content().innerHTML = '<p class="muted">Scan performance lands in Task 8.</p>';
}
```

…and equivalently `throughput.js` (`renderThroughput`, Task 9), `device.js` (`renderDevice`, Task 10), `health.js` (`renderHealth`, Task 11), `symlog.js` and `charts.js` (comment only, Task 8).

- [ ] **Step 7: Serve the SPA from `main.py`**

Add to imports in `main.py`:

```python
import os

from fastapi.responses import FileResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
```

Append at the **end** of `main.py` (after all API routes, so `/api/*` wins):

```python
# ---------------------------------------------------------------------------
# Static frontend (SPA). API routes above take precedence.
# ---------------------------------------------------------------------------
STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
_NO_CACHE = {
    "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
    "Pragma": "no-cache",
    "Expires": "0",
}

app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.get("/")
async def index():
    return RedirectResponse("/machines/performance")


@app.get("/machines/{rest:path}")
async def spa(rest: str):
    return FileResponse(os.path.join(STATIC_DIR, "index.html"), headers=_NO_CACHE)
```

- [ ] **Step 8: Run tests, then eyeball the shell locally**

Run: `python -m pytest roles/scan_fleet_dashboard/tests -v` — Expected: all PASS.

Then (no DB needed — API calls will 503, shell must still render):

```bash
pip install "uvicorn[standard]==0.32.1"
cd roles/scan_fleet_dashboard/files/app && python -m uvicorn main:app --port 8091
```

Open `http://localhost:8091/` — verify: redirect to `/machines/performance`; navy sidebar with active "Machines"; header; three tabs with teal underline on the active one; filter row; tab clicks swap content without a full page load (watch the network tab); browser back/forward works. Ctrl-C the server.

- [ ] **Step 9: Commit**

```bash
git add roles/scan_fleet_dashboard
git commit -m "feat(scan_fleet_dashboard): SPA shell — layout, router, filters, vendored Chart.js"
```

---

### Task 8: Tab 1 — Scan performance small multiples (the centrepiece)

**Files:**
- Replace: `roles/scan_fleet_dashboard/files/app/static/js/symlog.js`
- Replace: `roles/scan_fleet_dashboard/files/app/static/js/charts.js`
- Replace: `roles/scan_fleet_dashboard/files/app/static/js/performance.js`

**Interfaces:**
- Consumes: `GET /api/performance` (Task 4 shape), `State`/`rangeFor`/`api`/`navigate`/`pagerHtml`/`bindPager`/`esc`/`PAGE_SIZE` (Task 7), global `Chart` (vendored).
- Produces: `SYMLOG` global `{T, TICKS, fwd(v), inv(y), label(y)}`; `METRICS` array (fixed metric→colour map); `fmtDay(iso) -> "M/D"`; `symlogScaleOpts()`; `perfMiniChart(canvas, device)`; `areaChart(canvas, labels, datasets, yTitle)`; `renderPerformance()`. Tasks 9–10 reuse `METRICS`, `fmtDay`, `areaChart`, `symlogScaleOpts`, `perfMiniChart`.

- [ ] **Step 1: Implement `symlog.js`**

```js
/* Symmetric-log Y axis (spec §5): linear below linthresh 10, log10 above.
   Chart.js has no symlog scale, so values are transformed onto a linear axis
   and ticks/tooltips map back to real percentages. fwd(100) = 20. */
const SYMLOG = (() => {
  const T = 10; // linthresh
  const TICKS = [0, 2, 5, 10, 50, 100];
  const fwd = v => (v == null ? null : (v <= T ? v : T * (1 + Math.log10(v / T))));
  const inv = y => (y <= T ? y : T * Math.pow(10, y / T - 1));
  const label = y => {
    const hit = TICKS.find(t => Math.abs(fwd(t) - y) < 1e-6);
    return hit == null ? '' : `${hit}%`;
  };
  return { T, TICKS, fwd, inv, label };
})();
```

- [ ] **Step 2: Implement `charts.js`**

```js
/* Chart factories. Series colours are FIXED per metric (spec §4) — filtering
   or pagination must never recolour a line. */
const METRICS = [
  { key: 'good_read_pct',        label: 'Good read %',    color: '#2f8fa0', width: 2.5 },
  { key: 'no_read_pct',          label: 'No read %',      color: '#e07b39', width: 1.5 },
  { key: 'no_dimension_pct',     label: 'No dimension %', color: '#8a4b74', width: 1.5 },
  { key: 'no_weight_pct',        label: 'No weight %',    color: '#5b6b8c', width: 1.5 },
  { key: 'item_out_of_spec_pct', label: 'Out of spec %',  color: '#c0392b', width: 1.5 },
];

function fmtDay(iso) {
  const [, m, d] = iso.split('-');
  return `${Number(m)}/${Number(d)}`;
}

function symlogScaleOpts() {
  return {
    type: 'linear',
    min: 0,
    max: SYMLOG.fwd(100),
    afterBuildTicks: scale => {
      scale.ticks = SYMLOG.TICKS.map(t => ({ value: SYMLOG.fwd(t) }));
    },
    ticks: { callback: v => SYMLOG.label(v), font: { size: 10 } },
    grid: { color: '#eef0f3' },
  };
}

function perfMiniChart(canvas, device) {
  const labels = device.series.map(p => fmtDay(p.date));
  const datasets = METRICS.map(m => ({
    label: m.label,
    data: device.series.map(p => SYMLOG.fwd(p[m.key])),
    rawKey: m.key,
    borderColor: m.color,
    backgroundColor: m.color,
    borderWidth: m.width,
    pointRadius: 1.5,
    tension: 0.2,
    spanGaps: false,             // 0-item buckets stay gaps (spec §2)
  }));
  if (device.target_pct != null) {
    datasets.push({
      label: `Target ${device.target_pct}%`,
      data: device.series.map(() => SYMLOG.fwd(device.target_pct)),
      rawKey: null,
      borderColor: '#c0392b',
      borderDash: [5, 4],
      borderWidth: 1.2,
      pointRadius: 0,
    });
  }
  return new Chart(canvas, {
    type: 'line',
    data: { labels, datasets },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      animation: false,
      plugins: {
        legend: { display: false },     // shared legend above the grid (spec §5)
        tooltip: {
          callbacks: {
            label: ctx => {
              if (!ctx.dataset.rawKey) return ctx.dataset.label;
              const raw = device.series[ctx.dataIndex][ctx.dataset.rawKey];
              return raw == null ? `${ctx.dataset.label}: —`
                                 : `${ctx.dataset.label}: ${raw}%`;
            },
          },
        },
      },
      scales: {
        y: symlogScaleOpts(),
        x: { ticks: { maxTicksLimit: 4, font: { size: 10 } }, grid: { display: false } },
      },
    },
  });
}

function areaChart(canvas, labels, datasets, yTitle = '') {
  return new Chart(canvas, {
    type: 'line',
    data: { labels, datasets },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      animation: false,
      plugins: { legend: { display: datasets.length > 1, labels: { boxWidth: 12 } } },
      scales: {
        y: { beginAtZero: true, title: { display: !!yTitle, text: yTitle } },
        x: { ticks: { maxTicksLimit: 8, font: { size: 10 } }, grid: { display: false } },
      },
    },
  });
}
```

- [ ] **Step 3: Implement `performance.js`**

```js
/* Tab 1 — scan performance small multiples (spec §5). */
async function renderPerformance() {
  const range = rangeFor('performance');
  content().innerHTML = '<p class="muted">Loading…</p>';
  let devices;
  try {
    devices = await api('/api/performance', {
      customer: State.customer, date_from: range.from, date_to: range.to });
  } catch (e) {
    content().innerHTML = `<p class="error">${esc(e.message)}</p>`;
    return;
  }
  if (!devices.length) {
    content().innerHTML = '<p class="muted">No data in this range.</p>';
    return;
  }

  const pages = Math.max(1, Math.ceil(devices.length / PAGE_SIZE));
  State.page = Math.min(State.page, pages - 1);
  const slice = devices.slice(State.page * PAGE_SIZE, (State.page + 1) * PAGE_SIZE);

  const legend = METRICS.map(m =>
    `<span class="legend-item"><i style="background:${m.color}"></i>${m.label}</span>`
  ).join('') + '<span class="legend-item"><i class="dash"></i>Target</span>';

  content().innerHTML = `
    <div class="chart-legend">${legend}</div>
    <div class="grid" id="perf-grid">
      ${slice.map(d => `
        <div class="card mini" data-device="${d.device_id}">
          <div class="card-head">
            <div><b>${esc(d.display_name)}</b>
              <div class="muted small">${esc(d.customer)}</div></div>
            <div class="pct ${d.below_target ? 'bad' : 'good'}">
              ${d.current_good_read_pct ?? '–'}%</div>
          </div>
          <div class="mini-plot"><canvas id="perf-${d.device_id}"></canvas></div>
        </div>`).join('')}
    </div>
    ${pagerHtml(pages)}`;

  slice.forEach(d =>
    perfMiniChart(document.getElementById(`perf-${d.device_id}`), d));
  bindPager(pages, renderPerformance);
  document.querySelectorAll('#perf-grid .card').forEach(c =>
    c.addEventListener('click', () => navigate(`/machines/${c.dataset.device}`)));
}
```

- [ ] **Step 4: Regression-check the test suite**

Run: `python -m pytest roles/scan_fleet_dashboard/tests -v` — Expected: all PASS (no backend change; test_static still green).

- [ ] **Step 5: Commit, deploy to staging, verify with real data**

```bash
git add roles/scan_fleet_dashboard
git commit -m "feat(scan_fleet_dashboard): Tab 1 small multiples with symlog axis + target line"
git push
ssh s1@192.168.1.16 "cd ~/Systems-One-Server && git pull && ansible-playbook -i staging webservers.yml --tags scan_fleet_dashboard"
ssh s1@192.168.1.16 "curl -s http://localhost:8091/health && sudo docker ps --filter name=scan_fleet_dashboard"
```

Expected: `{"status":"ok"}`, container healthy. Then `ssh -L 8091:localhost:8091 s1@192.168.1.16`, open `http://localhost:8091/machines/performance` and verify against spec §11:
- one mini-chart per machine, ≤12 per page, pagination if more;
- good_read (~90–99%) AND error lines (~0.5–6%) both clearly readable (symlog);
- dashed red target line labelled; below-target machines show a red %;
- clicking a card navigates to `/machines/<id>` (placeholder page for now);
- changing date range/customer refetches; colours never change per series.

Also confirm marketing_display is untouched: `ssh s1@192.168.1.16 "curl -s http://localhost:8090/health"` → `{"status":"ok"}`.

- [ ] **Step 6: Commit any visual fixes found**

```bash
git add roles/scan_fleet_dashboard && git commit -m "fix(scan_fleet_dashboard): tab 1 polish from staging review" || echo "nothing to fix"
```

---

### Task 9: Tab 2 — Throughput UI

**Files:**
- Replace: `roles/scan_fleet_dashboard/files/app/static/js/throughput.js`

**Interfaces:**
- Consumes: `GET /api/throughput/kpis|intraday|by-machine` (Task 5 shapes), `areaChart`/`fmtDay` (Task 8), Task 7 globals.
- Produces: `renderThroughput()`.

- [ ] **Step 1: Implement `throughput.js`**

```js
/* Tab 2 — throughput (spec §7). */
let _intradayDevice = 0;   // 0 = fleet total
let _intradayChart = null;

async function renderThroughput() {
  const range = rangeFor('throughput');
  content().innerHTML = '<p class="muted">Loading…</p>';
  const params = { customer: State.customer, date_from: range.from, date_to: range.to };
  let kpis, machines;
  try {
    [kpis, machines] = await Promise.all([
      api('/api/throughput/kpis', params),
      api('/api/throughput/by-machine', params),
    ]);
  } catch (e) {
    content().innerHTML = `<p class="error">${esc(e.message)}</p>`;
    return;
  }

  const kpiCards = [
    ['Fleet parcels/hour', kpis.fleet_parcels_per_hour.toLocaleString()],
    ['Parcels today', kpis.parcels_today.toLocaleString()],
    ['Peak hour', kpis.peak_hour ?? '–'],
    ['Busiest machine', esc(kpis.busiest_machine ?? '–')],
    ['Lowest throughput', esc(kpis.lowest_machine ?? '–')],
  ].map(([t, v]) =>
    `<div class="card kpi"><div class="kpi-value">${v}</div>
     <div class="muted small">${t}</div></div>`).join('');

  const s = kpis.shifts;
  const pages = Math.max(1, Math.ceil(machines.length / PAGE_SIZE));
  State.page = Math.min(State.page, pages - 1);
  const slice = machines.slice(State.page * PAGE_SIZE, (State.page + 1) * PAGE_SIZE);

  const deviceOptions = ['<option value="0">Fleet total</option>']
    .concat(machines.map(m =>
      `<option value="${m.device_id}" ${m.device_id === _intradayDevice ? 'selected' : ''}>
       ${esc(m.display_name)}</option>`)).join('');

  content().innerHTML = `
    <div class="kpi-row">${kpiCards}</div>
    <div class="two-col">
      <div class="card">
        <h3>Intraday profile — today vs yesterday
          <select id="intraday-device" style="float:right">${deviceOptions}</select></h3>
        <div class="plot-lg"><canvas id="intraday"></canvas></div>
      </div>
      <div class="card">
        <h3>Shift summary</h3>
        <table class="table">
          <tr><th>Shift</th><th>Window</th><th>Parcels</th></tr>
          ${s.shifts.map(x => `<tr><td>${x.name}</td><td>${x.window}</td>
            <td>${x.parcels.toLocaleString()}</td></tr>`).join('')}
        </table>
        <p class="muted small" style="margin-top:8px">
          Packets: ${s.packets_received.toLocaleString()} received,
          ${s.packets_missed.toLocaleString()} missed
          (${s.expected_per_day}/device/day expected, ${s.interval_minutes}-min interval)</p>
      </div>
    </div>
    <h3>Per-machine throughput (avg parcels/hour per day)</h3>
    <div class="grid" id="tp-grid">
      ${slice.map(m => `
        <div class="card mini" data-device="${m.device_id}">
          <div class="card-head">
            <div><b>${esc(m.display_name)}</b>
              <div class="muted small">${esc(m.customer)}</div></div>
            <div class="pct good">${m.avg_per_hour.toLocaleString()}/hr</div>
          </div>
          <div class="mini-plot"><canvas id="tp-${m.device_id}"></canvas></div>
        </div>`).join('')}
    </div>
    ${pagerHtml(pages)}`;

  await drawIntraday();
  document.getElementById('intraday-device').addEventListener('change', async e => {
    _intradayDevice = Number(e.target.value);
    await drawIntraday();
  });

  slice.forEach(m => areaChart(
    document.getElementById(`tp-${m.device_id}`),
    m.series.map(p => fmtDay(p.date)),
    [{ label: 'parcels/hr', data: m.series.map(p => p.parcels_per_hour),
       borderColor: '#2f8fa0', backgroundColor: 'rgba(47,143,160,.15)',
       fill: true, pointRadius: 1.5, tension: 0.2, borderWidth: 1.8 }]));
  bindPager(pages, renderThroughput);
  document.querySelectorAll('#tp-grid .card').forEach(c =>
    c.addEventListener('click', () => navigate(`/machines/${c.dataset.device}`)));
}

async function drawIntraday() {
  let data;
  try {
    data = await api('/api/throughput/intraday', {
      customer: State.customer,
      device_id: _intradayDevice || '' });
  } catch (e) { console.error(e); return; }
  const labels = data.today.map(p => `${String(p.hour).padStart(2, '0')}:00`);
  if (_intradayChart) _intradayChart.destroy();
  _intradayChart = areaChart(document.getElementById('intraday'), labels, [
    { label: 'Today', data: data.today.map(p => p.parcels),
      borderColor: '#2f8fa0', backgroundColor: 'rgba(47,143,160,.15)',
      fill: true, pointRadius: 0, tension: 0.25, borderWidth: 2 },
    { label: 'Yesterday', data: data.yesterday.map(p => p.parcels),
      borderColor: '#8a8e94', borderDash: [5, 4], fill: false,
      pointRadius: 0, tension: 0.25, borderWidth: 1.5 },
  ], 'parcels/hour');
}
```

- [ ] **Step 2: Regression-check the test suite**

Run: `python -m pytest roles/scan_fleet_dashboard/tests -v` — Expected: all PASS.

- [ ] **Step 3: Commit, deploy, verify on staging**

```bash
git add roles/scan_fleet_dashboard
git commit -m "feat(scan_fleet_dashboard): Tab 2 throughput UI (KPIs, intraday overlay, shifts, per-machine grid)"
git push
ssh s1@192.168.1.16 "cd ~/Systems-One-Server && git pull && ansible-playbook -i staging webservers.yml --tags scan_fleet_dashboard"
```

Via `ssh -L 8091:...`, open `/machines/throughput` and check: 5 KPI cards populated; intraday chart shows today (solid teal + fill) vs yesterday (dashed grey) over `00:00…23:00`; machine picker switches the profile; shift card shows parcels per shift + packets received/missed; per-machine grid shows weekday/weekend rhythm; clicking a card opens the drill-down route.

---

### Task 10: Single-device drill-down UI (spec §8)

**Files:**
- Replace: `roles/scan_fleet_dashboard/files/app/static/js/device.js`

**Interfaces:**
- Consumes: `GET /api/device/{id}/summary|outcomes|health|alerts`, `POST /api/device/{id}/actions/{action}` (Task 6 shapes), `SYMLOG`/`symlogScaleOpts`/`areaChart`/`fmtDay` (Task 8).
- Produces: `renderDevice(id)`.

- [ ] **Step 1: Implement `device.js`**

```js
/* Single-device drill-down (spec §8). */
function fmtNum(v, suffix = '') {
  return v == null ? '–' : `${Math.round(v * 10) / 10}${suffix}`;
}

let _grChart = null;

async function renderDevice(id) {
  content().innerHTML = '<p class="muted">Loading…</p>';
  const range = rangeFor('performance');
  const rp = { date_from: range.from, date_to: range.to };
  let summary, outcomes, health, alerts;
  try {
    [summary, outcomes, health, alerts] = await Promise.all([
      api(`/api/device/${id}/summary`),
      api(`/api/device/${id}/outcomes`, rp),
      api(`/api/device/${id}/health`, rp),
      api(`/api/device/${id}/alerts`, rp),
    ]);
  } catch (e) {
    content().innerHTML = `<p class="error">Device ${id}: ${esc(e.message)}</p>`;
    return;
  }

  const d = summary.device, k = summary.kpis, st = summary.status;
  const badgeCls = st.badge === 'offline' ? 'offline'
                 : st.badge === 'below target' ? 'below' : 'online';

  const kpiCards = [
    ['Good read %', k.good_read_pct == null ? '–' : `${k.good_read_pct}%`,
     `target ${k.target_pct}%`, k.below_target],
    ['Uptime today', `${k.uptime_pct}%`, 'packets vs 288', false],
    ['CPU', fmtNum(k.cpu_percent, '%'), '', false],
    ['Memory', fmtNum(k.mem_usage_pct, '%'), '', false],
    ['Temperature', fmtNum(k.temp_celsius, '°C'), '', false],
    ['Storage (worst)', fmtNum(k.max_disk_pct, '%'), '', (k.max_disk_pct ?? 0) > 85],
  ].map(([t, v, sub, bad]) =>
    `<div class="card kpi ${bad ? 'bad' : ''}"><div class="kpi-value">${v}</div>
     <div class="muted small">${t}${sub ? ' · ' + sub : ''}</div></div>`).join('');

  const outcomeRows = [
    ['Good read', 'good_read'], ['No read', 'no_read'],
    ['No dimension', 'no_dimension'], ['No weight', 'no_weight'],
    ['Out of spec', 'item_out_of_spec'], ['Multiple items', 'more_than_1_item'],
    ['Hand scanned', 'hand_scanned'], ['Image sent', 'image_sent'],
    ['Image not sent', 'image_not_sent'],
  ].map(([label, key]) => {
    const n = outcomes.totals[key] ?? 0;
    const p = outcomes.totals.total_items
      ? Math.round(1000.0 * n / outcomes.totals.total_items) / 10 : null;
    return `<tr><td>${label}</td><td>${n.toLocaleString()}</td>
            <td>${p == null ? '–' : p + '%'}</td></tr>`;
  }).join('');

  content().innerHTML = `
    <div class="crumb"><a href="/machines/performance" data-link>Machines</a>
      / ${esc(d.customer)} / ${esc(d.display_name)}</div>
    <div class="device-head">
      <h2>${esc(d.display_name)}</h2>
      <span class="badge ${badgeCls}">${st.badge}</span>
      <div class="actions">
        <button data-action="acknowledge">Acknowledge alert</button>
        <button data-action="restart-agent">Restart agent</button>
        <button data-action="export-diagnostics">Export diagnostics</button>
      </div>
    </div>
    <div class="kpi-row" style="grid-template-columns:repeat(6,1fr)">${kpiCards}</div>

    <div class="section card">
      <h3>Good read % vs threshold
        <span style="float:right" class="small">
          <input type="date" id="detail-day" value="${range.to}">
          <button id="btn-5min">5-min view</button>
          <button id="btn-daily" class="hidden">back to daily</button>
        </span></h3>
      <div class="plot-lg"><canvas id="gr-chart"></canvas></div>
    </div>

    <div class="two-col">
      <div class="card"><h3>System health</h3>
        <div class="plot-sm"><canvas id="h-cpu"></canvas></div>
        <div class="plot-sm"><canvas id="h-mem"></canvas></div>
        <div class="plot-sm"><canvas id="h-temp"></canvas></div>
      </div>
      <div>
        <div class="card section"><h3>Read outcomes (${esc(range.from)} → ${esc(range.to)})</h3>
          <table class="table"><tr><th>Outcome</th><th>Count</th><th>% of items</th></tr>
          ${outcomeRows}</table></div>
        <div class="card section"><h3>Storage</h3>
          <table class="table"><tr><th>Drive</th><th>Used</th><th>Free</th><th>%</th></tr>
          ${summary.storage.map(x => `<tr><td>${esc(x.drive)}</td>
            <td>${fmtNum(x.used_gb)} GB</td><td>${fmtNum(x.free_gb)} GB</td>
            <td>${fmtNum(x.usage_percent, '%')}</td></tr>`).join('')}</table></div>
      </div>
    </div>

    <div class="section card"><h3>Recent alerts</h3>
      ${alerts.length ? `<table class="table">
        <tr><th>Time</th><th>Metric</th><th>Detail</th><th>Severity</th></tr>
        ${alerts.map(a => `<tr><td>${a.time}</td><td>${a.metric}</td>
          <td>${esc(a.detail)}</td><td>${a.severity}</td></tr>`).join('')}
      </table>` : '<p class="muted">No threshold breaches in this range.</p>'}</div>

    <div class="section card muted small">
      Serial ${esc(d.serial_number)} · OS ${esc(d.os_version ?? '–')} ·
      App ${st.app_running ? 'running' : 'STOPPED' +
        (st.stopped_since ? ' since ' + esc(st.stopped_since) : '')} ·
      ${st.online ? 'online' : 'offline' +
        (st.offline_since ? ' since ' + esc(st.offline_since) : '')}
    </div>`;

  drawGoodRead(outcomes.series, k.target_pct, outcomes.bucket);

  document.getElementById('btn-5min').addEventListener('click', async () => {
    const day = document.getElementById('detail-day').value;
    if (!day) return;
    try {
      const o5 = await api(`/api/device/${id}/outcomes`,
        { date_from: day, date_to: day, bucket: '5min' });
      drawGoodRead(o5.series, k.target_pct, '5min');
      document.getElementById('btn-daily').classList.remove('hidden');
    } catch (e) { alert(e.message); }
  });
  document.getElementById('btn-daily').addEventListener('click', () => {
    drawGoodRead(outcomes.series, k.target_pct, 'day');
    document.getElementById('btn-daily').classList.add('hidden');
  });

  document.querySelectorAll('.actions button').forEach(b =>
    b.addEventListener('click', async () => {
      try {
        const r = await fetch(`/api/device/${id}/actions/${b.dataset.action}`,
                              { method: 'POST' });
        const body = await r.json();
        alert(body.detail || 'done');
      } catch (e) { alert(e.message); }
    }));

  const hs = health.series;
  const hLabels = hs.map(p => p.ts.slice(11, 16));
  const mk = (cid, key, label, color) => areaChart(
    document.getElementById(cid), hLabels,
    [{ label, data: hs.map(p => p[key]), borderColor: color,
       pointRadius: 0, tension: 0.25, borderWidth: 1.5 }]);
  mk('h-cpu', 'cpu_percent', 'CPU %', '#2563eb');
  mk('h-mem', 'mem_usage_pct', 'Memory %', '#8a4b74');
  mk('h-temp', 'temp_celsius', 'Temp °C', '#e07b39');
}

function drawGoodRead(series, target, bucket) {
  const labels = series.map(p =>
    bucket === '5min' ? p.date.slice(11, 16) : fmtDay(p.date));
  const below = series.map(p =>
    p.good_read_pct != null && target != null && p.good_read_pct < target
      ? SYMLOG.fwd(100) : null);
  if (_grChart) _grChart.destroy();
  _grChart = new Chart(document.getElementById('gr-chart'), {
    data: {
      labels,
      datasets: [
        { type: 'bar', label: 'breach', data: below,          // breach shading
          backgroundColor: 'rgba(192,57,43,.08)',
          barPercentage: 1, categoryPercentage: 1, order: 3 },
        { type: 'line', label: 'Good read %',
          data: series.map(p => SYMLOG.fwd(p.good_read_pct)),
          borderColor: '#2f8fa0', borderWidth: 2, pointRadius: 1.5,
          tension: 0.2, spanGaps: false, order: 1 },
        { type: 'line', label: `Target ${target}%`,
          data: series.map(() => SYMLOG.fwd(target)),
          borderColor: '#c0392b', borderDash: [5, 4], borderWidth: 1.2,
          pointRadius: 0, order: 2 },
      ],
    },
    options: {
      responsive: true, maintainAspectRatio: false, animation: false,
      plugins: {
        legend: { display: false },
        tooltip: { filter: c => c.dataset.label !== 'breach',
          callbacks: { label: c => c.dataset.label.startsWith('Good')
            ? `Good read: ${series[c.dataIndex].good_read_pct ?? '—'}%`
            : c.dataset.label } },
      },
      scales: { y: symlogScaleOpts(),
                x: { ticks: { maxTicksLimit: 10, font: { size: 10 } },
                     grid: { display: false } } },
    },
  });
}
```

- [ ] **Step 2: Regression-check the test suite**

Run: `python -m pytest roles/scan_fleet_dashboard/tests -v` — Expected: all PASS.

- [ ] **Step 3: Commit, deploy, verify on staging**

```bash
git add roles/scan_fleet_dashboard
git commit -m "feat(scan_fleet_dashboard): device drill-down UI with 5-min detail toggle"
git push
ssh s1@192.168.1.16 "cd ~/Systems-One-Server && git pull && ansible-playbook -i staging webservers.yml --tags scan_fleet_dashboard"
```

Verify via tunnel: click any Tab 1 mini-chart → drill-down loads; breadcrumb + status badge correct; 6 KPI cards; big good-read chart with dashed target and shaded breach days; pick a day + "5-min view" switches to packet resolution; CPU/mem/temp trends and storage table populated; alerts table lists breach days; action buttons pop the 501 stub message.

---

### Task 11: Tab 3 — Device health fleet table

**Files:**
- Modify: `roles/scan_fleet_dashboard/files/app/device.py` (add `build_fleet_health`)
- Modify: `roles/scan_fleet_dashboard/files/app/main.py` (add endpoint)
- Replace: `roles/scan_fleet_dashboard/files/app/static/js/health.js`
- Test: `roles/scan_fleet_dashboard/tests/test_device.py` (extend)

**Interfaces:**
- Consumes: `perf.customer_scope`, `perf.pct`, `timeutil`.
- Produces: `device.build_fleet_health(q, customer=None, allowed=None) -> [{device_id, display_name, customer, status, app_running, cpu_percent, mem_usage_pct, temp_celsius, max_disk_pct, items_today, good_read_pct_today}]`; `GET /api/fleet-health?customer=`; `renderHealth()`.

- [ ] **Step 1: Write the failing test** (append to `tests/test_device.py`)

```python
def test_fleet_health_rows():
    q = (FakeQuery()
         .add("/* fleet_health */", [{
             "device_id": 1, "customer": "ACME", "location": "DC-1",
             "machine_name": "Line-01", "status": "online",
             "application_running": True, "cpu_percent": 12.0,
             "mem_usage_pct": 55.0, "temp_celsius": 41.0, "max_disk_pct": 61.0,
             "total_items": 500, "good_read": 480}]))
    rows = device.build_fleet_health(q)
    assert rows[0]["display_name"] == "DC-1 / Line-01"
    assert rows[0]["good_read_pct_today"] == 96.0
    assert rows[0]["items_today"] == 500


def test_fleet_health_applies_scope():
    q = FakeQuery().add("/* fleet_health */", [])
    device.build_fleet_health(q, customer="ACME", allowed=["ACME", "B"])
    sql, params = q.calls[0]
    assert "d.customer IN (?,?)" in sql and "d.customer = ?" in sql
    assert params[-3:] == ("ACME", "B", "ACME")
```

- [ ] **Step 2: Run to verify it fails**

Run: `python -m pytest roles/scan_fleet_dashboard/tests/test_device.py -v`
Expected: FAIL — `AttributeError: module 'device' has no attribute 'build_fleet_health'`.

- [ ] **Step 3: Implement**

Append to `roles/scan_fleet_dashboard/files/app/device.py` (add `from perf import customer_scope, pct` — replace the existing `from perf import pct` import):

```python
def build_fleet_health(q, customer=None, allowed=None):
    """Tab 3 — one row per device: latest state + today's totals."""
    scope_sql, scope_params = customer_scope(customer, allowed)
    start, end = timeutil.utc_bounds(timeutil.today_local(), timeutil.today_local())
    sql = f"""
        SELECT /* fleet_health */
               d.id AS device_id, d.customer, d.location, d.machine_name,
               ds.status, das.application_running,
               m.cpu_percent, m.mem_usage_pct, m.temp_celsius,
               st.max_disk_pct, t.total_items, t.good_read
        FROM dbo.devices d
        LEFT JOIN (SELECT device_id, status,
                          ROW_NUMBER() OVER (PARTITION BY device_id ORDER BY ts_datetime DESC) rn
                   FROM dbo.device_status) ds ON ds.device_id = d.id AND ds.rn = 1
        LEFT JOIN (SELECT device_id, application_running,
                          ROW_NUMBER() OVER (PARTITION BY device_id ORDER BY ts_datetime DESC) rn
                   FROM dbo.device_application_status) das ON das.device_id = d.id AND das.rn = 1
        LEFT JOIN (SELECT device_id, cpu_percent, mem_usage_pct, temp_celsius,
                          ROW_NUMBER() OVER (PARTITION BY device_id ORDER BY ts_datetime DESC) rn
                   FROM dbo.device_os_metrics) m ON m.device_id = d.id AND m.rn = 1
        LEFT JOIN (SELECT device_id, MAX(usage_percent) AS max_disk_pct
                   FROM dbo.device_storage_status GROUP BY device_id) st ON st.device_id = d.id
        LEFT JOIN (SELECT device_id, SUM(total_items) AS total_items, SUM(good_read) AS good_read
                   FROM dbo.device_statistics
                   WHERE ts_datetime >= ? AND ts_datetime < ?
                   GROUP BY device_id) t ON t.device_id = d.id
        WHERE 1=1{scope_sql}
        ORDER BY d.customer, d.location, d.machine_name
    """
    rows = q(sql, tuple([start, end] + scope_params))
    out = []
    for r in rows:
        items = r["total_items"] or 0
        out.append({
            "device_id": r["device_id"],
            "display_name": f"{r['location']} / {r['machine_name']}",
            "customer": r["customer"],
            "status": r["status"],
            "app_running": bool(r["application_running"]),
            "cpu_percent": r["cpu_percent"],
            "mem_usage_pct": r["mem_usage_pct"],
            "temp_celsius": r["temp_celsius"],
            "max_disk_pct": r["max_disk_pct"],
            "items_today": items,
            "good_read_pct_today": pct(r["good_read"] or 0, items),
        })
    return out
```

Add the endpoint to `main.py`:

```python
@app.get("/api/fleet-health")
async def api_fleet_health(request: Request, customer: str = ""):
    allowed = _allowed(request)
    return await _exec(lambda: device.build_fleet_health(
        db.query, customer or None, allowed))
```

`roles/scan_fleet_dashboard/files/app/static/js/health.js`:

```js
/* Tab 3 — device health fleet table (spec: lowest priority). */
async function renderHealth() {
  content().innerHTML = '<p class="muted">Loading…</p>';
  let rows;
  try {
    rows = await api('/api/fleet-health', { customer: State.customer });
  } catch (e) {
    content().innerHTML = `<p class="error">${esc(e.message)}</p>`;
    return;
  }
  const cell = (v, suffix, bad) =>
    `<td class="${bad ? 'pct bad' : ''}">${v == null ? '–' : v + suffix}</td>`;
  content().innerHTML = `
    <div class="card"><table class="table">
      <tr><th>Machine</th><th>Customer</th><th>Status</th><th>App</th>
          <th>CPU</th><th>Mem</th><th>Temp</th><th>Disk</th>
          <th>Items today</th><th>Good read</th></tr>
      ${rows.map(r => `<tr class="click" data-device="${r.device_id}">
        <td><b>${esc(r.display_name)}</b></td>
        <td>${esc(r.customer)}</td>
        <td class="${r.status === 'offline' ? 'pct bad' : ''}">${esc(r.status ?? '–')}</td>
        <td class="${r.app_running ? '' : 'pct bad'}">${r.app_running ? 'running' : 'stopped'}</td>
        ${cell(r.cpu_percent, '%', r.cpu_percent > 90)}
        ${cell(r.mem_usage_pct, '%', r.mem_usage_pct > 90)}
        ${cell(r.temp_celsius, '°C', r.temp_celsius > 70)}
        ${cell(r.max_disk_pct, '%', r.max_disk_pct > 85)}
        <td>${r.items_today.toLocaleString()}</td>
        ${cell(r.good_read_pct_today, '%', false)}
      </tr>`).join('')}
    </table></div>`;
  document.querySelectorAll('tr.click').forEach(tr =>
    tr.addEventListener('click', () => navigate(`/machines/${tr.dataset.device}`)));
}
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest roles/scan_fleet_dashboard/tests -v` — Expected: all PASS.

- [ ] **Step 5: Commit and deploy**

```bash
git add roles/scan_fleet_dashboard
git commit -m "feat(scan_fleet_dashboard): device health fleet table (Tab 3)"
git push
ssh s1@192.168.1.16 "cd ~/Systems-One-Server && git pull && ansible-playbook -i staging webservers.yml --tags scan_fleet_dashboard"
```

---

### Task 12: Row-level security (customer_login_map)

**Files:**
- Replace: `roles/scan_fleet_dashboard/files/app/auth.py`
- Test: `roles/scan_fleet_dashboard/tests/test_auth.py` (create)

**Interfaces:**
- Consumes: `config.AUTH_ENABLED`, `config.ADMIN_USERS`, `dbo.customer_login_map`.
- Produces: final `auth.allowed_customers(q, user, enabled=None, admins=None)` — `None` when disabled or admin; sorted customer list for mapped users; 401 when enabled and no `X-Auth-User`; 403 when unmapped. Every endpoint already routes through `main._allowed`, so no endpoint edits are needed.

- [ ] **Step 1: Write the failing tests**

`roles/scan_fleet_dashboard/tests/test_auth.py`:

```python
import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

import auth
from conftest import FakeQuery


def test_disabled_is_unrestricted():
    assert auth.allowed_customers(FakeQuery(), None, enabled=False) is None


def test_admin_is_unrestricted():
    assert auth.allowed_customers(
        FakeQuery(), "jonathan", enabled=True, admins={"jonathan"}) is None


def test_mapped_user_gets_customer_list():
    q = FakeQuery().add("customer_login_map", [{"customer": "ACME"}])
    assert auth.allowed_customers(q, "acme_user", enabled=True, admins=set()) == ["ACME"]


def test_missing_header_401():
    with pytest.raises(HTTPException) as e:
        auth.allowed_customers(FakeQuery(), None, enabled=True, admins=set())
    assert e.value.status_code == 401


def test_unmapped_user_403():
    q = FakeQuery().add("customer_login_map", [])
    with pytest.raises(HTTPException) as e:
        auth.allowed_customers(q, "stranger", enabled=True, admins=set())
    assert e.value.status_code == 403


def test_endpoint_scopes_customers(monkeypatch):
    import config
    import db
    import main
    monkeypatch.setattr(config, "AUTH_ENABLED", True)
    monkeypatch.setattr(db, "query", FakeQuery()
        .add("customer_login_map", [{"customer": "ACME"}])
        .add("DISTINCT customer FROM dbo.devices",
             [{"customer": "ACME"}, {"customer": "Globex"}]))
    client = TestClient(main.app)
    r = client.get("/api/customers", headers={"X-Auth-User": "acme_user"})
    assert r.status_code == 200
    assert r.json() == ["ACME"]
    assert client.get("/api/customers").status_code == 401
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest roles/scan_fleet_dashboard/tests/test_auth.py -v`
Expected: FAIL — `NotImplementedError` from the Task 2 stub.

- [ ] **Step 3: Implement `auth.py`**

```python
"""Row-level security: map the caller to the customers they may see (spec §9).

Identity comes from the X-Auth-User header — on this infrastructure the app is
loopback-bound and exposed only through the Cloudflare tunnel, which is where
the header must be injected (Cloudflare Access).
TODO: verify the Cloudflare Access JWT (Cf-Access-Jwt-Assertion) instead of
trusting the plain header before enabling auth in production.
"""
from fastapi import HTTPException

import config

_MAP_SQL = "SELECT customer FROM dbo.customer_login_map WHERE login = ?"


def resolve_user(request):
    return request.headers.get("X-Auth-User")


def allowed_customers(q, user, enabled=None, admins=None):
    """None = unrestricted (auth off, or admin). Else the permitted customers."""
    enabled = config.AUTH_ENABLED if enabled is None else enabled
    admins = config.ADMIN_USERS if admins is None else admins
    if not enabled:
        return None
    if not user:
        raise HTTPException(status_code=401, detail="missing X-Auth-User header")
    if user in admins:
        return None
    rows = q(_MAP_SQL, (user,))
    if not rows:
        raise HTTPException(status_code=403, detail="no customer mapping for user")
    return sorted({r["customer"] for r in rows})
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest roles/scan_fleet_dashboard/tests -v` — Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add roles/scan_fleet_dashboard
git commit -m "feat(scan_fleet_dashboard): row-level security via customer_login_map (off by default)"
```

---

### Task 13: Full staging acceptance + cutover runbook (runbook only — NOT executed)

**Files:**
- Create: `docs/runbooks/scan-fleet-cutover.md`

- [ ] **Step 1: Deploy latest and run the spec §11 acceptance checklist on staging**

```bash
git push
ssh s1@192.168.1.16 "cd ~/Systems-One-Server && git pull && ansible-playbook -i staging webservers.yml --tags scan_fleet_dashboard"
ssh s1@192.168.1.16 "curl -s http://localhost:8091/health; curl -s http://localhost:8090/health"
```

Expected: both `{"status":"ok"}` — new app up, marketing_display untouched. Then via `ssh -L 8091:localhost:8091 s1@192.168.1.16` walk every item and record pass/fail in the PR/commit message:

- [ ] Error % lines (0.5–6%) and good_read (~95%) both readable in one mini-chart (symlog).
- [ ] Series colours stay fixed per metric when filtering/paginating.
- [ ] Target line reflects `alert_thresholds` where a row exists (compare a machine that has a row against one that doesn't; check against `SELECT * FROM dbo.alert_thresholds` via the mssql container).
- [ ] A machine below target shows its % in red on the card.
- [ ] Daily buckets sum ~288 packets; hourly ~12; missed-packet count correct on a day with a known gap (pick one from `device_statistics`).
- [ ] Clicking any mini-chart lands on that device's drill-down.
- [ ] Date range picker drives every query on the page (network tab: `from`/`to` on all calls).
- [ ] Row-level security: temporarily set `scan_fleet_dashboard_auth_enabled: true` in `host_vars/sysone_staging.yml`, redeploy, confirm `curl http://localhost:8091/api/customers` → 401 and with `-H "X-Auth-User: <mapped login from customer_login_map>"` → only that customer; then revert the var and redeploy.

- [ ] **Step 2: Write the cutover runbook**

`docs/runbooks/scan-fleet-cutover.md`:

```markdown
# Scan Fleet Dashboard — cutover runbook

**Do not execute until side-by-side testing on :8091 is signed off.**
Marketing_display (:8090) keeps running unmodified until then.

## Option A — repoint the Cloudflare tunnel (recommended: zero container changes)
1. In Cloudflare Zero Trust → Tunnels → the s1 tunnel, edit the public hostname
   that maps to `http://localhost:8090` and point it at `http://localhost:8091`.
2. Verify the public URL now serves the scan fleet dashboard.
3. Rollback = point it back at 8090.
4. Once stable for an agreed period: remove `marketing_display` from
   `webservers.yml`, run the playbook, then on the server
   `sudo docker rm -f marketing_display` and archive
   `/opt/marketing-display`. Keep the role in git history.

## Option B — take over port 8090
1. Set `scan_fleet_dashboard_port: 8090` in `host_vars/sysone_staging.yml`
   and remove `marketing_display` from `webservers.yml`; commit.
2. On the server: `sudo docker rm -f marketing_display`, then
   `ansible-playbook -i staging webservers.yml --tags scan_fleet_dashboard`.
3. Verify `curl http://localhost:8090/health` serves the new app and the
   tunnel URL works.
4. Rollback: revert the commit, re-run the full `webservers.yml` play
   (rebuilds marketing_display), set the scan fleet port back to 8091.

## Post-cutover
- Decide whether to enable auth (`scan_fleet_dashboard_auth_enabled`) —
  requires Cloudflare Access injecting `X-Auth-User` (see auth.py TODO).
- Consider the nightly `device_statistics_daily` rollup table (spec §9) if
  30-day queries feel slow at fleet scale.
```

- [ ] **Step 3: Commit**

```bash
git add docs/runbooks/scan-fleet-cutover.md
git commit -m "docs: scan fleet dashboard acceptance results + cutover runbook"
git push
```

---

## Self-Review Notes (already applied)

- **Spec coverage:** §1–§2 (Task 2), §3–§4 (Task 7), §5 (Tasks 4+8), §6 (Task 3), §7 (Tasks 5+9), §8 (Tasks 6+10), §9 API+RLS+caching (Tasks 4–6, 12, KPI cache in Task 5), §10 build order mirrored by task order, §11 (Task 13). Device health tab (§3, lowest priority) = Task 11. The nightly rollup table from §9 is deliberately deferred (noted in the runbook) — raw queries with `utc_bounds` range scans are index-friendly on `IX_device_statistics_ts`.
- **Known deviations from the spec's suggested API:** shift summary rides inside `/api/throughput/kpis`; fleet health uses `/api/fleet-health`; `/api/device/:id/summary` folds in the info-strip data. All intentional (fewer round-trips), documented in task Interfaces blocks.
- **Type consistency spot-checks:** `customer_scope` returns `(str, list)` and every builder threads it identically; all builders take `q` first; `pct()` is the single divide-by-zero guard; `display_name` is always `location + " / " + machine_name`.
- **`db.query` coerces datetimes to ISO strings** — every test fixture uses strings for `day`/`ts` accordingly; frontend treats them as strings too.

