# TTY1 Dashboard Overhaul Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Rewrite the TTY1 console dashboard: performance totals (today/week/year), host metrics, compact service health, problems-only log pane; drop the dead OpenClaw panel.

**Architecture:** One stdlib-only Python file shipped as an Ansible template (`roles/s1_dashboard/templates/docker-dashboard.py.j2`). The template is *valid Python* — the only Jinja is inside two string constants (DB credentials) — so unit tests import it directly. All external data comes from `subprocess` calls to `docker`; performance totals come from `docker exec mssql sqlcmd` cached 60 s; the problems scan is cached 30 s.

**Tech Stack:** Python 3 stdlib, ANSI escapes, unittest, Ansible role.

## Global Constraints

- No third-party Python packages — stdlib only (script runs on host Python on Ubuntu).
- The `.j2` template must stay valid importable Python; Jinja expressions ONLY inside the `DB_USER` / `DB_PASS` string literals.
- Business-time boundaries computed in SAST (UTC+2), matching existing queries that use `DATEADD(HOUR, 2, ts_datetime)`.
- DB login: `{{ mssql_rm_admin_login }}` / `{{ mssql_rm_admin_password }}` (defined in `group_vars/dbservers.yml`), database `S1_Remote_Monitoring`. Never use the SA password.
- Good-read % colours: green ≥ 97, orange ≥ 90, red < 90.
- Refresh cadence: render 5 s; perf query TTL 60 s; problems scan TTL 30 s.
- Deviation from spec (intentional): per-container `docker stats` is dropped — no panel displays it and it was the slowest call on the render path.
- Every fetcher returns a sentinel (`None`/empty) on failure; render never raises.
- Local dev machine is Windows: run tests with `python`, they must not touch `/proc` or `docker`.

---

### Task 1: Pure-logic core of the new dashboard template + unit tests

**Files:**
- Create: `roles/s1_dashboard/templates/docker-dashboard.py.j2` (constants, ANSI helpers, pure functions — fetchers/render added in Tasks 2–3)
- Create: `roles/s1_dashboard/tests/test_dashboard.py`

**Interfaces:**
- Produces (used by Tasks 2–3): `period_starts(today: date) -> (str, str, str)`, `build_perf_query(today_s, week_s, year_s) -> str`, `parse_perf_output(text) -> dict|None` (keys `today/week/year`, each `{"items": int, "pct": float|None}`), `good_read_color(pct) -> str`, `fmt_int(n) -> str`, `is_problem_line(line) -> bool`, `TTLCache().get(key, ttl, fn, clock=...)`, plus ANSI constants and `clr/_strip_ansi/pad/truncate/bar`.

- [ ] **Step 1: Write the failing tests**

Create `roles/s1_dashboard/tests/test_dashboard.py`:

```python
"""Unit tests for the pure-logic parts of docker-dashboard.py.j2.

The template is valid Python (Jinja only inside two string constants),
so we load it directly as a module despite the .j2 extension.
"""
import importlib.machinery
import importlib.util
import os
import unittest
from datetime import date

TEMPLATE = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "..", "templates", "docker-dashboard.py.j2"
)


def load():
    loader = importlib.machinery.SourceFileLoader("dash", TEMPLATE)
    spec = importlib.util.spec_from_loader("dash", loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


dash = load()


class PeriodStarts(unittest.TestCase):
    def test_midweek(self):
        # Wed 2026-07-15 -> week starts Mon 2026-07-13
        self.assertEqual(
            dash.period_starts(date(2026, 7, 15)),
            ("2026-07-15", "2026-07-13", "2026-01-01"),
        )

    def test_on_monday_week_start_is_today(self):
        self.assertEqual(dash.period_starts(date(2026, 7, 13))[1], "2026-07-13")

    def test_january_first(self):
        self.assertEqual(
            dash.period_starts(date(2026, 1, 1)),
            ("2026-01-01", "2025-12-29", "2026-01-01"),
        )


class BuildPerfQuery(unittest.TestCase):
    def test_contains_boundaries_and_table(self):
        q = dash.build_perf_query("2026-07-20", "2026-07-13", "2026-01-01")
        self.assertIn("'2026-07-20'", q)
        self.assertIn("'2026-07-13'", q)
        self.assertIn("'2026-01-01'", q)
        self.assertIn("dbo.device_statistics", q)
        self.assertIn("SET NOCOUNT ON", q)


class ParsePerfOutput(unittest.TestCase):
    def test_valid_line(self):
        p = dash.parse_perf_output("123|100|500|450|9000|8900\n")
        self.assertEqual(p["today"]["items"], 123)
        self.assertEqual(p["week"]["pct"], 90.0)
        self.assertEqual(p["year"]["items"], 9000)

    def test_zero_items_gives_none_pct(self):
        p = dash.parse_perf_output("0|0|0|0|0|0")
        self.assertIsNone(p["today"]["pct"])

    def test_garbage_returns_none(self):
        self.assertIsNone(dash.parse_perf_output("Sqlcmd: Error: connection failed"))
        self.assertIsNone(dash.parse_perf_output(""))

    def test_skips_noise_lines_before_data(self):
        p = dash.parse_perf_output("some warning\n1|1|2|2|3|3\n")
        self.assertEqual(p["today"]["items"], 1)


class GoodReadColor(unittest.TestCase):
    def test_thresholds(self):
        self.assertEqual(dash.good_read_color(97.0), dash.FG_GREEN)
        self.assertEqual(dash.good_read_color(96.9), dash.FG_ORANGE)
        self.assertEqual(dash.good_read_color(90.0), dash.FG_ORANGE)
        self.assertEqual(dash.good_read_color(89.9), dash.FG_RED)


class FmtInt(unittest.TestCase):
    def test_thousands_spaces(self):
        self.assertEqual(dash.fmt_int(1234567), "1 234 567")
        self.assertEqual(dash.fmt_int(0), "0")


class IsProblemLine(unittest.TestCase):
    def test_matches(self):
        for line in ("ERROR: boom", "connection failed", "Traceback (most recent",
                     "WARN slow query", "unhandled exception"):
            self.assertTrue(dash.is_problem_line(line), line)

    def test_non_matches(self):
        for line in ("INFO: all good", "GET /health 200", ""):
            self.assertFalse(dash.is_problem_line(line), line)


class CacheTest(unittest.TestCase):
    def test_ttl(self):
        t = [0.0]
        calls = []

        def fn():
            calls.append(1)
            return len(calls)

        c = dash.TTLCache()
        self.assertEqual(c.get("k", 60, fn, clock=lambda: t[0]), 1)
        self.assertEqual(c.get("k", 60, fn, clock=lambda: t[0]), 1)
        t[0] = 61.0
        self.assertEqual(c.get("k", 60, fn, clock=lambda: t[0]), 2)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python roles/s1_dashboard/tests/test_dashboard.py -v`
Expected: FAIL at import (`FileNotFoundError` — template doesn't exist yet).

- [ ] **Step 3: Write the template's logic core**

Create `roles/s1_dashboard/templates/docker-dashboard.py.j2`:

```python
#!/usr/bin/env python3
"""S1 Server Dashboard - full-screen console display on TTY1.

Rendered by Ansible from templates/docker-dashboard.py.j2.
The only Jinja substitutions are the two DB_* constants below;
the file is valid Python as-is, which keeps it unit-testable.
"""

import argparse
import os
import re
import subprocess
import time
from datetime import date, datetime, timedelta, timezone

# -- DB credentials (Ansible-injected) ----------------------------------------
DB_USER = "{{ mssql_rm_admin_login }}"
DB_PASS = "{{ mssql_rm_admin_password }}"
DB_NAME = "S1_Remote_Monitoring"

SAST = timezone(timedelta(hours=2))
PERF_TTL = 60.0
PROBLEMS_TTL = 30.0
PROBLEM_RE = re.compile(r"error|warn|exception|traceback|fail", re.IGNORECASE)

# -- ANSI ---------------------------------------------------------------------
RESET     = "\033[0m"
BOLD      = "\033[1m"
DIM       = "\033[2m"
BG_BAR    = "\033[48;5;236m"

FG_CYAN   = "\033[38;5;87m"
FG_GREEN  = "\033[38;5;84m"
FG_YELLOW = "\033[38;5;220m"
FG_RED    = "\033[38;5;203m"
FG_ORANGE = "\033[38;5;214m"
FG_WHITE  = "\033[38;5;252m"
FG_DIM    = "\033[38;5;240m"
FG_ACCENT = "\033[38;5;213m"
FG_PURPLE = "\033[38;5;177m"

ANSI_RE = re.compile(r"\033\[[0-9;]*m")


def clr(text, *codes):
    return "".join(codes) + text + RESET


def _strip_ansi(s):
    return ANSI_RE.sub("", s)


def pad(text, width):
    visible = len(_strip_ansi(text))
    if visible < width:
        return text + " " * (width - visible)
    return text


def truncate(text, max_len):
    if len(text) > max_len:
        return text[: max_len - 1] + "…"
    return text


def bar(pct, width=24):
    filled = min(int(width * pct / 100), width)
    empty = width - filled
    fc = FG_RED if pct > 85 else (FG_ORANGE if pct > 60 else FG_GREEN)
    return BG_BAR + fc + "█" * filled + FG_DIM + "░" * empty + RESET


# -- Pure logic ---------------------------------------------------------------

def fmt_int(n):
    return f"{n:,}".replace(",", " ")


def good_read_color(pct):
    if pct >= 97.0:
        return FG_GREEN
    if pct >= 90.0:
        return FG_ORANGE
    return FG_RED


def period_starts(today):
    """(today, monday-of-week, jan-1) as ISO date strings."""
    monday = today - timedelta(days=today.weekday())
    jan1 = date(today.year, 1, 1)
    return today.isoformat(), monday.isoformat(), jan1.isoformat()


def build_perf_query(today_s, week_s, year_s):
    return (
        "SET NOCOUNT ON; "
        "SELECT "
        f"COALESCE(SUM(CASE WHEN d >= '{today_s}' THEN total_items END),0), "
        f"COALESCE(SUM(CASE WHEN d >= '{today_s}' THEN good_read END),0), "
        f"COALESCE(SUM(CASE WHEN d >= '{week_s}' THEN total_items END),0), "
        f"COALESCE(SUM(CASE WHEN d >= '{week_s}' THEN good_read END),0), "
        "COALESCE(SUM(total_items),0), "
        "COALESCE(SUM(good_read),0) "
        "FROM (SELECT CAST(DATEADD(HOUR,2,ts_datetime) AS date) AS d, "
        "total_items, good_read FROM dbo.device_statistics "
        f"WHERE DATEADD(HOUR,2,ts_datetime) >= '{year_s}') x;"
    )


def parse_perf_output(text):
    """Parse `sqlcmd -h -1 -W -s "|"` output: one 6-int row. None if absent."""
    for line in (l.strip() for l in text.splitlines()):
        parts = [p.strip() for p in line.split("|")]
        if len(parts) == 6 and all(p.isdigit() for p in parts):
            v = list(map(int, parts))

            def entry(items, good):
                pct = round(100.0 * good / items, 1) if items > 0 else None
                return {"items": items, "pct": pct}

            return {
                "today": entry(v[0], v[1]),
                "week": entry(v[2], v[3]),
                "year": entry(v[4], v[5]),
            }
    return None


def is_problem_line(line):
    return bool(PROBLEM_RE.search(line))


class TTLCache:
    def __init__(self):
        self._data = {}

    def get(self, key, ttl, fn, clock=time.monotonic):
        now = clock()
        hit = self._data.get(key)
        if hit is not None and now - hit[0] < ttl:
            return hit[1]
        val = fn()
        self._data[key] = (now, val)
        return val
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python roles/s1_dashboard/tests/test_dashboard.py -v`
Expected: all tests PASS.

- [ ] **Step 5: Commit**

```bash
git add roles/s1_dashboard/templates/docker-dashboard.py.j2 roles/s1_dashboard/tests/test_dashboard.py
git commit -m "s1_dashboard: logic core of new dashboard template with unit tests"
```

---

### Task 2: Data fetchers (system, docker, perf query, problems scan)

**Files:**
- Modify: `roles/s1_dashboard/templates/docker-dashboard.py.j2` (append after `TTLCache`)
- Modify: `roles/s1_dashboard/tests/test_dashboard.py` (append one test class)

**Interfaces:**
- Consumes: Task 1 functions/constants.
- Produces (used by Task 3): `get_disk_stats() -> dict`, `get_system_stats() -> dict|None`, `get_containers() -> list[(name,status,image)]`, `get_health_detail(name) -> str|None`, `fetch_perf() -> dict|None`, `scan_problems(names) -> list[(ts,service,msg)]`, `classify_services(ctrs) -> (ok: list[(name,color)], detail: list[(name,status,color)])`, module-level `CACHE = TTLCache()`.

- [ ] **Step 1: Write the failing test for `classify_services`**

Append to `roles/s1_dashboard/tests/test_dashboard.py` (before the `__main__` block):

```python
class ClassifyServices(unittest.TestCase):
    CTRS = [
        ("grafana", "Up 2 days (healthy)", "img"),
        ("wetty", "Up 2 days", "img"),
        ("mosquitto", "Up 2 days (unhealthy)", "img"),
        ("nodered", "Up 10 seconds (health: starting)", "img"),
        ("oldjob", "Exited (1) 3 days ago", "img"),
    ]

    def test_split(self):
        ok, detail = dash.classify_services(self.CTRS)
        self.assertEqual([n for n, _ in ok], ["grafana", "wetty"])
        self.assertEqual(ok[0][1], dash.FG_GREEN)   # healthy
        self.assertEqual(ok[1][1], dash.FG_CYAN)    # up, no healthcheck
        names = [d[0] for d in detail]
        self.assertEqual(names, ["mosquitto", "nodered", "oldjob"])
        self.assertEqual(detail[0][2], dash.FG_ORANGE)  # unhealthy
        self.assertEqual(detail[1][2], dash.FG_ORANGE)  # starting
        self.assertEqual(detail[2][2], dash.FG_RED)     # exited
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python roles/s1_dashboard/tests/test_dashboard.py -v`
Expected: `ClassifyServices` FAILS with `AttributeError: module 'dash' has no attribute 'classify_services'`; all others pass.

- [ ] **Step 3: Append the fetchers to the template**

Append to `roles/s1_dashboard/templates/docker-dashboard.py.j2`:

```python
# -- Data fetchers ------------------------------------------------------------

CACHE = TTLCache()

# docker --format templates need literal {{...}} in the deployed file, but a
# literal "{{" in this source would be parsed by Jinja at deploy time. Build
# the braces at runtime so the file contains none.
_LB = chr(123) * 2
_RB = chr(125) * 2
DOCKER_PS_FMT = f"{_LB}.Names{_RB}\t{_LB}.Status{_RB}\t{_LB}.Image{_RB}"
DOCKER_HEALTH_FMT = f"{_LB}.State.Health.Status{_RB}\t{_LB}json .State.Health.Log{_RB}"


def get_disk_stats(mount="/"):
    st = os.statvfs(mount)
    total = st.f_blocks * st.f_frsize / (1024 ** 3)
    free = st.f_bavail * st.f_frsize / (1024 ** 3)
    used = total - free
    pct = used / total * 100 if total > 0 else 0
    return {"total": total, "used": used, "free": free, "pct": pct, "mount": mount}


def get_system_stats():
    try:
        with open("/proc/loadavg") as f:
            parts = f.read().split()
        load1, load5, load15 = parts[0], parts[1], parts[2]

        with open("/proc/uptime") as f:
            secs = float(f.read().split()[0])
        days = int(secs // 86400)
        hours = int((secs % 86400) // 3600)
        mins = int((secs % 3600) // 60)
        uptime = f"{days}d {hours}h {mins}m"

        mem = {}
        with open("/proc/meminfo") as f:
            for line in f:
                k, v = line.split(":", 1)
                mem[k.strip()] = int(v.strip().split()[0])
        mem_total = mem["MemTotal"] / 1024
        mem_used = (mem["MemTotal"] - mem["MemFree"] - mem["Buffers"] - mem["Cached"]) / 1024
        mem_pct = mem_used / mem_total * 100
        swap_total = mem["SwapTotal"] / 1024
        swap_used = (mem["SwapTotal"] - mem["SwapFree"]) / 1024
        swap_pct = swap_used / swap_total * 100 if swap_total > 0 else 0

        def read_cpu():
            with open("/proc/stat") as f:
                line = f.readline()
            v = list(map(int, line.split()[1:]))
            return v[3], sum(v)

        i1, t1 = read_cpu()
        time.sleep(0.3)
        i2, t2 = read_cpu()
        cpu_pct = 100.0 * (1 - (i2 - i1) / max(t2 - t1, 1))

        return {
            "cpu_pct": cpu_pct, "load": f"{load1}  {load5}  {load15}",
            "mem_used": mem_used, "mem_total": mem_total, "mem_pct": mem_pct,
            "swap_used": swap_used, "swap_total": swap_total, "swap_pct": swap_pct,
            "uptime": uptime,
        }
    except Exception:
        return None


def get_containers():
    try:
        r = subprocess.run(
            ["docker", "ps", "-a", "--format", DOCKER_PS_FMT],
            capture_output=True, text=True, timeout=10,
        )
        out = []
        for line in r.stdout.strip().splitlines():
            p = line.split("\t")
            if len(p) >= 2:
                out.append((p[0], p[1], p[2] if len(p) > 2 else ""))
        return out
    except Exception:
        return []


def get_health_detail(name):
    try:
        r = subprocess.run(
            ["docker", "inspect", "--format", DOCKER_HEALTH_FMT, name],
            capture_output=True, text=True, timeout=5,
        )
        out = r.stdout.strip()
        if not out or "\t" not in out:
            return None
        status, log_json = out.split("\t", 1)
        if status == "healthy":
            return None
        import json as _json
        logs = _json.loads(log_json)
        if logs:
            last = logs[-1]
            output = last.get("Output", "").strip().replace("\n", " ")
            exit_code = last.get("ExitCode", "?")
            return f"exit={exit_code}  {output}"
    except Exception:
        pass
    return None


def fetch_perf():
    today = datetime.now(SAST).date()
    query = build_perf_query(*period_starts(today))
    try:
        r = subprocess.run(
            ["docker", "exec", "mssql", "/opt/mssql-tools18/bin/sqlcmd",
             "-S", "localhost", "-U", DB_USER, "-P", DB_PASS, "-d", DB_NAME,
             "-C", "-h", "-1", "-W", "-s", "|", "-Q", query],
            capture_output=True, text=True, timeout=20,
        )
        if r.returncode != 0:
            return None
        return parse_perf_output(r.stdout)
    except Exception:
        return None


def scan_problems(names):
    """Error/warn lines from all containers' last 30 min, oldest first."""
    problems = []
    for name in names:
        try:
            r = subprocess.run(
                ["docker", "logs", "--since", "30m", "--timestamps", "--tail", "200", name],
                capture_output=True, text=True, timeout=10,
            )
            for line in (r.stdout + r.stderr).splitlines():
                ts, _, msg = line.partition(" ")
                if msg and is_problem_line(msg):
                    problems.append((ts, name, msg.strip()))
        except Exception:
            problems.append(("", name, "(error reading logs)"))
    problems.sort(key=lambda p: p[0])
    return problems


def classify_services(ctrs):
    """Split containers into compact-grid entries vs full detail rows."""
    ok, detail = [], []
    for name, status, _image in ctrs:
        low = status.lower()
        if "(healthy)" in low:
            ok.append((name, FG_GREEN))
        elif low.startswith("up") and "(" not in low:
            ok.append((name, FG_CYAN))
        elif low.startswith("up"):
            detail.append((name, status, FG_ORANGE))
        else:
            detail.append((name, status, FG_RED))
    return ok, detail
```

Do NOT "simplify" `DOCKER_PS_FMT`/`DOCKER_HEALTH_FMT` into literal `{{.Names}}`
strings — Ansible's template step would try to evaluate them as Jinja and fail.

- [ ] **Step 4: Run tests to verify they pass**

Run: `python roles/s1_dashboard/tests/test_dashboard.py -v`
Expected: all tests PASS (including `ClassifyServices`).

- [ ] **Step 5: Commit**

```bash
git add roles/s1_dashboard/templates/docker-dashboard.py.j2 roles/s1_dashboard/tests/test_dashboard.py
git commit -m "s1_dashboard: data fetchers (perf query, problems scan, docker/system stats)"
```

---

### Task 3: Renderer, demo mode, main loop

**Files:**
- Modify: `roles/s1_dashboard/templates/docker-dashboard.py.j2` (append)
- Modify: `roles/s1_dashboard/tests/test_dashboard.py` (append two test classes)

**Interfaces:**
- Consumes: everything from Tasks 1–2.
- Produces: `pack_entries(entries, inner, gap=3) -> list[str]`, `render(cols, rows, snap) -> str`, `gather() -> dict`, `demo_snapshot() -> dict`, `main()`. Snapshot dict keys: `now` (str), `sys` (dict|None), `disk` (dict), `ctrs` (list), `health` (dict name->str), `perf` (dict|None), `problems` (list).

- [ ] **Step 1: Write the failing tests**

Append to `roles/s1_dashboard/tests/test_dashboard.py` (before `__main__`):

```python
class PackEntries(unittest.TestCase):
    def test_packs_within_width(self):
        entries = [(f"svc{i}", dash.FG_GREEN) for i in range(8)]
        rows = dash.pack_entries(entries, inner=40)
        self.assertTrue(len(rows) >= 2)
        for row in rows:
            self.assertLessEqual(len(dash._strip_ansi(row)), 40)

    def test_single_row_when_fits(self):
        rows = dash.pack_entries([("a", dash.FG_GREEN), ("b", dash.FG_CYAN)], inner=120)
        self.assertEqual(len(rows), 1)


class RenderSmoke(unittest.TestCase):
    def test_demo_render_shape(self):
        out = dash.render(100, 40, dash.demo_snapshot())
        lines = out.split("\n")
        self.assertEqual(len(lines), 40)
        for line in lines:
            self.assertEqual(len(dash._strip_ansi(line)), 100, repr(line[:40]))
        text = dash._strip_ansi(out)
        self.assertIn("PERFORMANCE", text)
        self.assertIn("THIS WEEK", text)
        self.assertIn("SERVICES", text)
        self.assertIn("PROBLEMS", text)
        self.assertNotIn("OPENCLAW", text)

    def test_render_with_db_down(self):
        snap = dash.demo_snapshot()
        snap["perf"] = None
        text = dash._strip_ansi(dash.render(100, 40, snap))
        self.assertIn("DB unavailable", text)

    def test_render_no_problems(self):
        snap = dash.demo_snapshot()
        snap["problems"] = []
        text = dash._strip_ansi(dash.render(100, 40, snap))
        self.assertIn("all services healthy", text)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python roles/s1_dashboard/tests/test_dashboard.py -v`
Expected: `PackEntries` and `RenderSmoke` FAIL with `AttributeError` (no `pack_entries`/`render`/`demo_snapshot`); all others pass.

- [ ] **Step 3: Append renderer, demo snapshot and main loop**

Append to `roles/s1_dashboard/templates/docker-dashboard.py.j2`:

```python
# -- Render -------------------------------------------------------------------

def _frow(content, inner):
    return clr("║", FG_DIM) + pad(content, inner) + clr("║", FG_DIM)


def _fline(left, ch, right, inner):
    return clr(left + ch * inner + right, FG_DIM)


def pack_entries(entries, inner, gap=3):
    """Pack '<dot> name' cells into rows that fit the inner width."""
    rows, cur, cur_w = [], "", 0
    for name, color in entries:
        cell = clr("● ", color) + clr(name, FG_WHITE)
        w = 2 + len(name)
        if cur and cur_w + gap + w > inner:
            rows.append(cur)
            cur, cur_w = "", 0
        if cur:
            cur += " " * gap + cell
            cur_w += gap + w
        else:
            cur = "  " + cell
            cur_w = 2 + w
    if cur:
        rows.append(cur)
    return rows


def render(cols, rows, snap):
    inner = cols - 2
    lines = []
    push = lines.append

    # header
    title = clr("  ⚙  S1  SERVER", BOLD, FG_ACCENT)
    ts = clr(snap["now"] + "  ", FG_DIM)
    gap = inner - len(_strip_ansi(title)) - len(_strip_ansi(ts))
    push(_fline("╔", "═", "╗", inner))
    push(clr("║", FG_DIM) + title + " " * max(0, gap) + ts + clr("║", FG_DIM))
    push(_fline("╠", "═", "╣", inner))

    # performance
    push(_frow(clr("  \U0001F4CA  PERFORMANCE", BOLD, FG_ACCENT), inner))
    push(_fline("╟", "─", "╢", inner))
    push(_frow(clr(f"  {'':<12}{'ITEMS':>14}{'GOOD READ':>14}", BOLD, FG_DIM), inner))
    perf = snap["perf"]
    for key, label in (("today", "TODAY"), ("week", "THIS WEEK"), ("year", "THIS YEAR")):
        if perf:
            row = perf[key]
            items_s = fmt_int(row["items"])
            pct = row["pct"]
            pct_s = f"{pct:.1f}%" if pct is not None else "—"
            pc = good_read_color(pct) if pct is not None else FG_DIM
            content = (clr(f"  {label:<12}", BOLD, FG_WHITE)
                       + clr(f"{items_s:>14}", BOLD, FG_CYAN)
                       + clr(f"{pct_s:>14}", BOLD, pc))
        else:
            content = (clr(f"  {label:<12}", BOLD, FG_WHITE)
                       + clr(f"{'—':>14}{'—':>14}", FG_DIM))
        push(_frow(content, inner))
    if not perf:
        push(_frow(clr("  DB unavailable", DIM, FG_ORANGE), inner))

    # system
    push(_fline("╠", "═", "╣", inner))
    s = snap["sys"]
    disk = snap["disk"]

    def stat_row(label, b, val, extra=""):
        return _frow(clr(f" {label:<5}", BOLD, FG_WHITE) + " " + b + " "
                     + clr(val, BOLD, FG_CYAN) + clr(f"  {extra}", FG_DIM), inner)

    if s:
        push(stat_row("CPU", bar(s["cpu_pct"]), f"{s['cpu_pct']:5.1f}%",
                      f"load {s['load']}   uptime {s['uptime']}"))
        push(stat_row("MEM", bar(s["mem_pct"]),
                      f"{s['mem_used']:5.0f} / {s['mem_total']:.0f} MB", f"{s['mem_pct']:.1f}%"))
        push(stat_row("SWAP", bar(s["swap_pct"]),
                      f"{s['swap_used']:5.0f} / {s['swap_total']:.0f} MB", f"{s['swap_pct']:.1f}%"))
    else:
        push(_frow(clr("  system stats unavailable", DIM, FG_ORANGE), inner))
    push(stat_row("DISK", bar(disk["pct"]),
                  f"{disk['used']:.1f} / {disk['total']:.1f} GB",
                  f"{disk['pct']:.1f}%  {disk['mount']}"))

    # services
    push(_fline("╠", "═", "╣", inner))
    ctrs = snap["ctrs"]
    ok, detail = classify_services(ctrs)
    push(_frow(clr("  \U0001F433  SERVICES  ", BOLD, FG_GREEN)
               + clr(f"{len(ok)}/{len(ctrs)} ok", FG_DIM), inner))
    push(_fline("╟", "─", "╢", inner))
    for row in pack_entries(ok, inner):
        push(_frow(row, inner))
    for name, status, color in detail:
        push(_frow(clr("  ● ", color) + clr(f"{truncate(name, 24):<24}", BOLD, FG_WHITE)
                   + clr(truncate(status, max(1, inner - 30)), color), inner))
        d = snap["health"].get(name)
        if d:
            push(_frow(clr(f"     ⚠  {truncate(d, inner - 8)}", FG_ORANGE), inner))

    # problems
    push(_fline("╠", "═", "╣", inner))
    push(_frow(clr("  ⚠  PROBLEMS  (last 30 min)", BOLD, FG_YELLOW), inner))
    push(_fline("╟", "─", "╢", inner))
    FOOTER = 3
    avail = max(1, rows - len(lines) - FOOTER)
    problems = snap["problems"]
    if problems:
        for ts, svc, msg in problems[-avail:]:
            tss = ts[11:19] if len(ts) >= 19 and "T" in ts else "        "
            head = f"  {tss} [{svc}] "
            content = (clr(f"  {tss} ", FG_DIM) + clr(f"[{svc}] ", FG_PURPLE)
                       + clr(truncate(msg, max(1, inner - len(head))), FG_WHITE))
            push(_frow(content, inner))
    else:
        push(_frow(clr("  ✓  all services healthy — no errors in last 30 min", FG_GREEN), inner))

    while len(lines) < rows - FOOTER:
        push(_frow("", inner))
    lines[:] = lines[: rows - FOOTER]

    # footer
    push(_fline("╠", "═", "╣", inner))
    push(_frow(clr("  Ctrl+C to exit   render 5s   stats 60s   problems 30s", FG_DIM), inner))
    push(_fline("╚", "═", "╝", inner))
    return "\n".join(lines)


# -- Snapshot -----------------------------------------------------------------

def gather():
    ctrs = get_containers()
    names = [c[0] for c in ctrs]
    health = {}
    for name, status, _image in ctrs:
        low = status.lower()
        if low.startswith("up") and "(" in low and "(healthy)" not in low:
            d = get_health_detail(name)
            if d:
                health[name] = d
    problems = list(CACHE.get("problems", PROBLEMS_TTL, lambda: scan_problems(names)))
    for name, d in health.items():
        problems.append(("", name, f"healthcheck: {d}"))
    return {
        "now": datetime.now().strftime("%a %d %b %Y  %H:%M:%S"),
        "sys": get_system_stats(),
        "disk": get_disk_stats(),
        "ctrs": ctrs,
        "health": health,
        "perf": CACHE.get("perf", PERF_TTL, fetch_perf),
        "problems": problems,
    }


def demo_snapshot():
    return {
        "now": "Sun 20 Jul 2026  09:45:12",
        "sys": {"cpu_pct": 4.2, "load": "0.17  0.22  0.25",
                "mem_used": 4800, "mem_total": 7800, "mem_pct": 61.5,
                "swap_used": 120, "swap_total": 2048, "swap_pct": 5.9,
                "uptime": "2d 8h 4m"},
        "disk": {"total": 97.9, "used": 40.2, "free": 57.7, "pct": 41.1, "mount": "/"},
        "ctrs": [
            ("grafana", "Up 2 days (healthy)", ""),
            ("mssql", "Up 2 days (healthy)", ""),
            ("nodered", "Up 2 days (healthy)", ""),
            ("s1_reporter", "Up 2 days (healthy)", ""),
            ("marketing_display", "Up 2 days (healthy)", ""),
            ("wetty", "Up 2 days", ""),
            ("mosquitto", "Up 2 days (unhealthy)", ""),
        ],
        "health": {"mosquitto": "exit=1  pgrep not found"},
        "perf": {
            "today": {"items": 12345, "pct": 98.2},
            "week": {"items": 81002, "pct": 97.9},
            "year": {"items": 4102388, "pct": 91.1},
        },
        "problems": [
            ("2026-07-20T07:15:01.000000000Z", "systems_one_ingest", "WARN reconnecting to broker"),
            ("2026-07-20T07:16:44.000000000Z", "mosquitto", "Error: healthcheck failed"),
        ],
    }


# -- Main ---------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(description="S1 TTY1 dashboard")
    ap.add_argument("--demo", action="store_true",
                    help="render one frame with canned data and exit")
    args = ap.parse_args()

    if args.demo:
        try:
            sz = os.get_terminal_size()
            cols, rows = sz.columns, sz.lines
        except Exception:
            cols, rows = 120, 40
        print(render(cols, rows - 2, demo_snapshot()))
        return

    print("\033[?25l\033[H\033[J", end="", flush=True)
    try:
        while True:
            try:
                sz = os.get_terminal_size()
                cols, rows = sz.columns, sz.lines
            except Exception:
                cols, rows = 120, 40
            output = render(cols, rows - 2, gather())
            print("\033[H\033[J", end="")
            print("\n\n" + output, flush=True)
            time.sleep(5)
    except KeyboardInterrupt:
        print("\033[?25h\033[H\033[J")
        print("Dashboard closed.")


if __name__ == "__main__":
    main()
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python roles/s1_dashboard/tests/test_dashboard.py -v`
Expected: all tests PASS. If `RenderSmoke` width assertions fail, the culprit is
almost always an emoji (📊/🐳) counting as 1 char in Python but rendering 2 cells —
that's fine on the console; the test measures `len()` so widths must be computed
from `_strip_ansi` length only, which the render code already does.

- [ ] **Step 5: Eyeball the demo render locally**

Run: `python roles/s1_dashboard/templates/docker-dashboard.py.j2 --demo`
Expected: full-frame dashboard with fake data; PERFORMANCE rows show 12 345 / 81 002 / 4 102 388, mosquitto appears as an orange detail row, problems pane lists two tagged lines.

- [ ] **Step 6: Commit**

```bash
git add roles/s1_dashboard/templates/docker-dashboard.py.j2 roles/s1_dashboard/tests/test_dashboard.py
git commit -m "s1_dashboard: renderer, demo mode and main loop for new dashboard"
```

---

### Task 4: Rewire the Ansible role (copy → template, drop old file)

**Files:**
- Modify: `roles/s1_dashboard/tasks/main.yml` (the "Install docker-dashboard.py" task)
- Delete: `roles/s1_dashboard/files/docker-dashboard.py`

**Interfaces:**
- Consumes: `templates/docker-dashboard.py.j2` from Tasks 1–3; `mssql_rm_admin_login` / `mssql_rm_admin_password` from `group_vars/dbservers.yml` (the host is in both `webservers` and `dbservers`, so the vars resolve in the web play).
- Produces: deployed file `/opt/s1-dashboard/docker-dashboard.py`, mode `0750`, owner root, group `{{ s1_dashboard_user }}` (script embeds a DB password — not world-readable).

- [ ] **Step 1: Replace the copy task with a template task**

In `roles/s1_dashboard/tasks/main.yml` replace:

```yaml
- name: Install docker-dashboard.py
  copy:
    src: docker-dashboard.py
    dest: "{{ s1_dashboard_dir }}/docker-dashboard.py"
    owner: root
    group: root
    mode: "0755"
```

with:

```yaml
- name: Install docker-dashboard.py (templated - embeds RM DB login)
  template:
    src: docker-dashboard.py.j2
    dest: "{{ s1_dashboard_dir }}/docker-dashboard.py"
    owner: root
    group: "{{ s1_dashboard_user }}"
    mode: "0750"
```

- [ ] **Step 2: Delete the old static file**

```bash
git rm roles/s1_dashboard/files/docker-dashboard.py
```

- [ ] **Step 3: Validate YAML and run full test suite**

Run: `python -c "import yaml; yaml.safe_load(open('roles/s1_dashboard/tasks/main.yml'))" && python roles/s1_dashboard/tests/test_dashboard.py`
Expected: no YAML error; all tests pass.

- [ ] **Step 4: Commit**

```bash
git add roles/s1_dashboard/tasks/main.yml
git commit -m "s1_dashboard: deploy dashboard as template with RM DB login, drop old script"
```

---

### Task 5: Deploy to s1_server and verify live

**Files:** none in repo (server rollout). Use Windows OpenSSH (`/c/Windows/System32/OpenSSH/ssh.exe` from Git Bash, or `ssh` in PowerShell) — Git Bash's bundled ssh cannot read the key.

**Interfaces:**
- Consumes: pushed master; server repo clone at `/home/s1/Systems-One-Server`; wrapper loop `/opt/s1-dashboard/start-dashboard.sh` (restarts the python process automatically when it exits).

- [ ] **Step 1: Push and pull the server clone**

```bash
git push origin master
/c/Windows/System32/OpenSSH/ssh.exe s1_server 'cd /home/s1/Systems-One-Server && git pull --ff-only && git log --oneline -1'
```
Expected: server clone at the new HEAD.

- [ ] **Step 2: Verify the perf query returns sane numbers**

```bash
/c/Windows/System32/OpenSSH/ssh.exe s1_server 'docker exec mssql /opt/mssql-tools18/bin/sqlcmd -S localhost -U admin -P "SysOne012!" -d S1_Remote_Monitoring -C -h -1 -W -s "|" -Q "SET NOCOUNT ON; SELECT COUNT(*) FROM dbo.device_statistics;"'
```
Expected: a row count > 0. Then cross-check today/year items against the marketing app: `curl -s http://127.0.0.1:8090/api/stats` on the server — `items_today` / `items_year` should be the same order of magnitude as the dashboard query results (small drift from cache timing is fine).

- [ ] **Step 3: Render the deployed template on the server**

Render the template into place (Ansible isn't installed on the server; the only
Jinja in the file is the two credential constants). Pipe a script via ssh stdin
to avoid quoting issues:

```bash
/c/Windows/System32/OpenSSH/ssh.exe s1_server sudo python3 - <<'PYEOF'
src = open("/home/s1/Systems-One-Server/roles/s1_dashboard/templates/docker-dashboard.py.j2").read()
src = src.replace("{{ mssql_rm_admin_login }}", "admin")
src = src.replace("{{ mssql_rm_admin_password }}", "SysOne012!")
open("/opt/s1-dashboard/docker-dashboard.py", "w").write(src)
print("rendered", len(src), "bytes")
PYEOF
/c/Windows/System32/OpenSSH/ssh.exe s1_server 'sudo chown root:s1 /opt/s1-dashboard/docker-dashboard.py && sudo chmod 0750 /opt/s1-dashboard/docker-dashboard.py && ls -la /opt/s1-dashboard/'
```

(If the permission classifier blocks writing to `/opt`, hand the equivalent command to the user to run with the `!` prefix.)

- [ ] **Step 4: Restart the dashboard and verify**

```bash
/c/Windows/System32/OpenSSH/ssh.exe s1_server 'sudo pkill -f docker-dashboard.py; sleep 6; pgrep -af docker-dashboard.py'
```
Expected: a fresh `python3 /opt/s1-dashboard/docker-dashboard.py` process (the wrapper loop restarts it). Then run one live frame over SSH to inspect content:

```bash
/c/Windows/System32/OpenSSH/ssh.exe -t s1_server 'cd /opt/s1-dashboard && timeout 12 python3 docker-dashboard.py; true'
```
Expected: real data — performance rows populated, services grid shows ~11 containers, mosquitto (currently unhealthy) appears as a detail row and in problems.

- [ ] **Step 5: Commit nothing — confirm clean tree and report**

Run: `git status -sb`
Expected: clean. Report the live numbers seen in Step 4 to the user.
```
