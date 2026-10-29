"""Run guarded SQL against the read-only database, bounded by a row cap, timeout and concurrency."""

import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from askdb.schema import connect_readonly
from askdb.settings import Settings
from askdb.sql_guard import ALLOWED_FUNCTIONS, check_plan, check_sql

MAX_CELL_CHARS = 1_000
PROGRESS_INTERVAL_OPS = 1_000


class QueryTimeout(Exception):
    """The query ran longer than QUERY_TIMEOUT_S and was aborted."""


class QueryError(Exception):
    """SQLite rejected or failed the query (syntax, unknown column, ...). `message` is SQLite's."""

    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


@dataclass
class QueryResult:
    columns: list[str]
    rows: list[tuple[Any, ...]]
    truncated: bool  # more rows existed than the cap
    duration_s: float


def make_authorizer(denied_columns: frozenset[str] = frozenset()):
    """Build the authorizer callback: SELECT machinery, reads of non-internal tables, and
    allowlisted functions only. `denied_columns` holds "table.column" entries (the glossary's
    `excluded:` columns); reading one fails even through `SELECT *`, which SQLite expands into
    per-column reads before asking.
    """
    denied = frozenset(d.lower() for d in denied_columns)

    def authorizer(action: int, arg1: str | None, arg2: str | None, *_: Any) -> int:
        if action in (sqlite3.SQLITE_SELECT, sqlite3.SQLITE_RECURSIVE):
            return sqlite3.SQLITE_OK
        if action == sqlite3.SQLITE_READ:
            if arg1 and arg1.lower().startswith("sqlite_"):
                return sqlite3.SQLITE_DENY
            if arg1 and arg2 and f"{arg1}.{arg2}".lower() in denied:
                return sqlite3.SQLITE_DENY
            return sqlite3.SQLITE_OK
        if action == sqlite3.SQLITE_FUNCTION:
            allowed = (arg2 or "").lower() in ALLOWED_FUNCTIONS
            return sqlite3.SQLITE_OK if allowed else sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_DENY

    return authorizer


authorizer = make_authorizer()


def display_value(value: Any, truncate: bool = True) -> Any:
    if isinstance(value, bytes):
        return f"<blob {len(value)} bytes>"
    if truncate and isinstance(value, str) and len(value) > MAX_CELL_CHARS:
        return f"{value[:MAX_CELL_CHARS]}… ({len(value)} chars)"
    return value


class Executor:
    """One per database. `run` is blocking: call it from a worker thread."""

    def __init__(
        self,
        db_path: Path,
        row_cap: int,
        timeout_s: float,
        max_concurrent: int = 2,
        allowed_tables: frozenset[str] | None = None,
        denied_columns: frozenset[str] = frozenset(),
    ):
        self.db_path = db_path
        self._authorizer = make_authorizer(denied_columns)
        self.row_cap = row_cap
        self.timeout_s = timeout_s
        self.allowed_tables = allowed_tables
        self._slots = threading.BoundedSemaphore(max_concurrent)

    @classmethod
    def from_settings(
        cls,
        settings: Settings,
        allowed_tables: frozenset[str] | None = None,
        denied_columns: frozenset[str] = frozenset(),
    ) -> "Executor":
        return cls(
            settings.db_path,
            settings.row_cap,
            settings.query_timeout_s,
            settings.max_concurrent_queries,
            allowed_tables,
            denied_columns,
        )

    def run(self, sql: str, truncate_cells: bool = True) -> QueryResult:
        """Guard, then execute. Raises GuardError, QueryTimeout or QueryError."""
        guarded = check_sql(sql, self.allowed_tables)
        with self._slots:  # time spent queued does not count against the timeout
            conn = connect_readonly(self.db_path)  # a fresh connection per request
            try:
                conn.set_authorizer(self._authorizer)
                deadline = time.monotonic() + self.timeout_s
                timed_out = False

                def over_deadline() -> int:
                    nonlocal timed_out
                    timed_out = time.monotonic() > deadline
                    return int(timed_out)

                conn.set_progress_handler(over_deadline, PROGRESS_INTERVAL_OPS)
                started = time.monotonic()
                try:
                    check_plan(conn, guarded)
                    # Newlines keep a `--` comment inside the query from eating the closing paren.
                    cursor = conn.execute(
                        f"SELECT * FROM (\n{guarded.sql}\n) LIMIT {self.row_cap + 1}"
                    )
                    columns = [d[0] for d in cursor.description or []]
                    fetched = cursor.fetchall()
                except sqlite3.Error as exc:
                    if timed_out:
                        raise QueryTimeout(
                            f"The query took longer than {self.timeout_s:g} seconds."
                        ) from exc
                    if "not authorized" in str(exc) or "prohibited" in str(exc):
                        raise QueryError(
                            "The query used a table, column or function that is not available."
                        ) from exc
                    raise QueryError(str(exc)) from exc
                truncated = len(fetched) > self.row_cap
                rows = [
                    tuple(display_value(v, truncate_cells) for v in row)
                    for row in fetched[: self.row_cap]
                ]
                return QueryResult(columns, rows, truncated, time.monotonic() - started)
            finally:
                conn.close()
