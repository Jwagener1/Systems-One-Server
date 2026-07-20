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
