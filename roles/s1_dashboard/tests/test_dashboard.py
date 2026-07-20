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
        self.assertIn("DATEADD(HOUR,2,", q)


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


class TemplateJinjaSafety(unittest.TestCase):
    def test_no_stray_jinja_outside_credentials(self):
        with open(TEMPLATE, encoding="utf-8") as f:
            for n, line in enumerate(f, 1):
                if "mssql_rm_admin" in line:
                    continue
                self.assertNotIn("{" + "{", line, f"literal Jinja braces on line {n}")


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

    def test_width_at_80_cols(self):
        out = dash.render(80, 22, dash.demo_snapshot())
        lines = out.split("\n")
        self.assertEqual(len(lines), 22)
        for line in lines:
            self.assertEqual(len(dash._strip_ansi(line)), 80, repr(line[:40]))

    def test_narrow_and_tiny_terminals_keep_frame(self):
        for cols, rows in ((60, 15), (100, 2)):
            out = dash.render(cols, rows, dash.demo_snapshot())
            for line in out.split("\n"):
                self.assertEqual(len(dash._strip_ansi(line)), cols)


if __name__ == "__main__":
    unittest.main()
