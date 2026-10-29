import shutil
import sqlite3
import threading
import time

import pytest

from askdb.executor import (
    MAX_CELL_CHARS,
    Executor,
    QueryError,
    QueryTimeout,
    authorizer,
)
from askdb.schema import connect_readonly
from askdb.settings import Settings
from askdb.sql_guard import GuardError

SERIES_20K = (
    "WITH RECURSIVE c(n) AS (SELECT 1 UNION ALL SELECT n + 1 FROM c WHERE n < 20000) "
    "SELECT n FROM c"
)
FOREVER = "WITH RECURSIVE c(n) AS (SELECT 1 UNION ALL SELECT n + 1 FROM c) SELECT COUNT(*) FROM c"


@pytest.fixture
def executor(sample_db):
    return Executor(sample_db, row_cap=10_000, timeout_s=5)


def test_runs_a_query(executor):
    result = executor.run("SELECT plan_name, monthly_price FROM plans ORDER BY monthly_price")
    assert result.columns == ["plan_name", "monthly_price"]
    assert result.rows == [
        ("Starter", 29.0),
        ("Professional", 79.0),
        ("Business", 149.0),
        ("Enterprise", 299.0),
    ]
    assert result.truncated is False


def test_glossary_style_queries_run(executor):
    result = executor.run(
        "SELECT strftime('%Y', invoice_date) y, SUM(amount) FILTER (WHERE status = 'paid') "
        "FROM invoices GROUP BY y ORDER BY y"
    )
    assert len(result.rows) >= 3
    assert executor.run("SELECT COUNT(*) FROM customers").rows == [(200,)]


def test_trailing_semicolon_and_comment(executor):
    assert executor.run("SELECT 1 AS one; -- trailing").rows == [(1,)]
    assert executor.run("SELECT 1 AS one -- trailing, no semicolon").rows == [(1,)]
    assert executor.run("SELECT /* inline */ 2 AS two").rows == [(2,)]


def test_row_cap_truncates_and_flags(sample_db):
    result = Executor(sample_db, row_cap=10_000, timeout_s=5).run(SERIES_20K)
    assert len(result.rows) == 10_000
    assert result.truncated is True
    assert result.rows[0] == (1,)
    assert result.rows[-1] == (10_000,)


def test_exactly_at_cap_is_not_truncated(sample_db):
    result = Executor(sample_db, row_cap=200, timeout_s=5).run("SELECT * FROM customers")
    assert len(result.rows) == 200
    assert result.truncated is False


def test_order_by_survives_the_cap_wrapper(sample_db):
    result = Executor(sample_db, row_cap=5, timeout_s=5).run(
        "SELECT customer_id FROM customers ORDER BY customer_id DESC"
    )
    assert [r[0] for r in result.rows] == [200, 199, 198, 197, 196]
    assert result.truncated is True


def test_slow_query_times_out(sample_db):
    started = time.monotonic()
    with pytest.raises(QueryTimeout):
        Executor(sample_db, row_cap=10_000, timeout_s=0.3).run(FOREVER)
    assert time.monotonic() - started < 3


def test_executor_usable_after_timeout(sample_db):
    ex = Executor(sample_db, row_cap=10_000, timeout_s=0.3)
    with pytest.raises(QueryTimeout):
        ex.run(FOREVER)
    assert ex.run("SELECT 1").rows == [(1,)]


def test_guard_rejection_never_touches_the_database(tmp_path):
    ex = Executor(tmp_path / "does_not_exist.db", row_cap=10, timeout_s=1)
    with pytest.raises(GuardError):
        ex.run("DROP TABLE customers")


def test_sql_errors_are_reported_with_sqlites_message(executor):
    with pytest.raises(QueryError, match="no such column"):
        executor.run("SELECT nope FROM customers")
    with pytest.raises(QueryError, match="no such table"):
        executor.run("SELECT * FROM nope")


def test_blob_and_long_text_display(tmp_path):
    db = tmp_path / "b.db"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE t (b BLOB, s TEXT)")
    conn.execute("INSERT INTO t VALUES (?, ?)", (b"\x00" * 17, "x" * 5000))
    conn.commit()
    conn.close()
    ex = Executor(db, row_cap=10, timeout_s=1)
    blob, text = ex.run("SELECT b, s FROM t").rows[0]
    assert blob == "<blob 17 bytes>"
    assert text.startswith("x" * MAX_CELL_CHARS + "…") and "5000 chars" in text
    assert ex.run("SELECT s FROM t", truncate_cells=False).rows[0][0] == "x" * 5000


def test_duplicate_column_names_survive_the_wrapper(executor):
    result = executor.run(
        "SELECT c.customer_id, s.customer_id FROM customers c "
        "JOIN subscriptions s ON s.customer_id = c.customer_id LIMIT 3"
    )
    assert len(result.columns) == 2 and len(result.rows) == 3


def test_allowed_tables_enforced(sample_db):
    ex = Executor(sample_db, row_cap=10, timeout_s=1, allowed_tables=frozenset({"plans"}))
    assert len(ex.run("SELECT * FROM plans").rows) == 4
    with pytest.raises(GuardError):
        ex.run("SELECT * FROM customers")


# --- cartesian joins -------------------------------------------------------


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT * FROM customers, invoices",
        "SELECT * FROM customers c CROSS JOIN invoices i",
        "SELECT COUNT(*) FROM customers a, subscriptions b, invoices c",
        "SELECT * FROM customers c JOIN invoices i",
        "SELECT * FROM customers c JOIN invoices i ON 1 = 1",
    ],
)
def test_cartesian_join_rejected(executor, sql):
    with pytest.raises(GuardError, match="join condition"):
        executor.run(sql)


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT COUNT(*) FROM customers c, subscriptions s WHERE c.customer_id = s.customer_id",
        "SELECT COUNT(*) FROM customers c JOIN subscriptions s ON c.customer_id = s.customer_id",
        "SELECT COUNT(*) FROM customers c JOIN support_tickets t ON t.created_at > c.signup_date "
        "AND t.customer_id = c.customer_id",
        "SELECT COUNT(*) FROM customers c, (SELECT MAX(signup_date) m FROM customers) x "
        "WHERE c.signup_date = x.m",
        "SELECT COUNT(*) FROM customers, (SELECT MAX(signup_date) m FROM customers) x",
        "WITH x AS (SELECT 1 n) SELECT COUNT(*) FROM customers, x",
        "SELECT industry FROM customers UNION ALL SELECT plan_name FROM plans",
    ],
)
def test_legitimate_joins_allowed(executor, sql):
    assert executor.run(sql).rows


# --- concurrency -----------------------------------------------------------


def test_concurrency_limit(sample_db):
    ex = Executor(sample_db, row_cap=10, timeout_s=5, max_concurrent=2)
    active = peak = 0
    lock = threading.Lock()
    original = ex._slots

    class Counting:
        def __enter__(self):
            nonlocal active, peak
            original.acquire()
            with lock:
                active += 1
                peak = max(peak, active)
            time.sleep(0.15)

        def __exit__(self, *exc):
            nonlocal active
            with lock:
                active -= 1
            original.release()

    ex._slots = Counting()
    threads = [threading.Thread(target=ex.run, args=("SELECT 1",)) for _ in range(6)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert peak == 2


def test_queued_time_does_not_count_against_timeout(sample_db):
    ex = Executor(sample_db, row_cap=10, timeout_s=0.5, max_concurrent=1)
    results = []
    ex._slots.acquire()
    t = threading.Thread(target=lambda: results.append(ex.run("SELECT 1").rows))
    t.start()
    time.sleep(0.8)
    ex._slots.release()
    t.join(timeout=5)
    assert results == [[(1,)]]


def test_executor_from_settings(sample_db):
    settings = Settings(
        _env_file=None, db_path=sample_db, row_cap=7, query_timeout_s=2, max_concurrent_queries=3
    )
    ex = Executor.from_settings(settings)
    assert (ex.row_cap, ex.timeout_s) == (7, 2)
    assert ex._slots._value == 3


# --- defense in depth: SQL that gets PAST the guard still cannot write ------

WRITES = [
    "INSERT INTO plans VALUES (9, 'x', 1, 1, 'y')",
    "UPDATE plans SET monthly_price = 0",
    "DELETE FROM plans",
    "DROP TABLE plans",
    "CREATE TABLE evil (a)",
    "CREATE TEMP TABLE evil (a)",
    "ALTER TABLE plans ADD COLUMN x",
]


@pytest.fixture
def db_copy(sample_db, tmp_path):
    path = tmp_path / "copy.db"
    shutil.copy(sample_db, path)
    return path


def plan_count(path):
    conn = sqlite3.connect(path)
    try:
        return conn.execute("SELECT COUNT(*), SUM(monthly_price) FROM plans").fetchone()
    finally:
        conn.close()


@pytest.mark.parametrize("sql", [w for w in WRITES if "TEMP" not in w])
def test_read_only_uri_alone_blocks_writes(db_copy, sql):
    # TEMP tables live in a separate scratch database, so mode=ro does not stop them; the
    # authorizer test below does.
    conn = sqlite3.connect(f"{db_copy.as_uri()}?mode=ro", uri=True)
    with pytest.raises(sqlite3.Error):
        conn.execute(sql)
    conn.close()
    assert plan_count(db_copy) == (4, 556.0)


@pytest.mark.parametrize("sql", WRITES)
def test_query_only_alone_blocks_writes(db_copy, sql):
    conn = sqlite3.connect(db_copy)  # read-write handle, only PRAGMA query_only protects it
    conn.execute("PRAGMA query_only=ON")
    with pytest.raises(sqlite3.Error):
        conn.execute(sql)
    conn.close()
    assert plan_count(db_copy) == (4, 556.0)


@pytest.mark.parametrize(
    "sql", WRITES + ["ATTACH DATABASE ':memory:' AS x", "PRAGMA user_version=5"]
)
def test_authorizer_alone_blocks_writes(db_copy, sql):
    conn = sqlite3.connect(db_copy)  # read-write handle, only the authorizer protects it
    conn.set_authorizer(authorizer)
    with pytest.raises(sqlite3.Error, match="not authorized"):
        conn.execute(sql)
    conn.close()
    assert plan_count(db_copy) == (4, 556.0)


def test_authorizer_blocks_functions_and_internal_tables(db_copy):
    conn = sqlite3.connect(db_copy)
    conn.set_authorizer(authorizer)
    for sql in [
        "SELECT randomblob(8)",
        "SELECT load_extension('x')",
        "SELECT sql FROM sqlite_master",
        "SELECT * FROM pragma_table_info('plans')",
    ]:
        with pytest.raises(sqlite3.Error):
            conn.execute(sql).fetchall()
    assert conn.execute("SELECT COUNT(*), upper('a') FROM plans").fetchone() == (4, "A")
    conn.close()


def test_full_stack_connection_blocks_writes_past_the_guard(db_copy):
    """Same connection setup as Executor.run, with the guard skipped."""
    conn = connect_readonly(db_copy)
    conn.set_authorizer(authorizer)
    for sql in WRITES:
        with pytest.raises(sqlite3.Error):
            conn.execute(sql)
    conn.close()
    assert plan_count(db_copy) == (4, 556.0)


def test_multiple_statements_fail_in_sqlite_itself(db_copy):
    conn = connect_readonly(db_copy)
    with pytest.raises(sqlite3.Error):
        conn.execute("SELECT 1; DELETE FROM plans")
    conn.close()
