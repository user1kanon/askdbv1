import pytest

from askdb.sql_guard import GuardError, check_sql

REJECTED = {
    "drop": "DROP TABLE customers",
    "insert": "INSERT INTO plans VALUES (9, 'x', 1, 1, 'y')",
    "update": "UPDATE plans SET monthly_price = 0",
    "delete": "DELETE FROM customers",
    "replace": "REPLACE INTO plans VALUES (1, 'x', 1, 1, 'y')",
    "create": "CREATE TABLE t (a)",
    "create_as_select": "CREATE TABLE t AS SELECT * FROM customers",
    "alter": "ALTER TABLE plans ADD COLUMN x",
    "vacuum": "VACUUM",
    "pragma": "PRAGMA writable_schema = 1",
    "pragma_read": "PRAGMA table_info(customers)",
    "attach": "ATTACH DATABASE '/tmp/x.db' AS x",
    "detach": "DETACH DATABASE x",
    "explain": "EXPLAIN SELECT 1",
    "begin": "BEGIN",
    "multi": "SELECT 1; DROP TABLE customers",
    "multi_select": "SELECT 1; SELECT 2",
    "multi_with_comment": "SELECT 1; -- harmless\nDELETE FROM customers",
    "cte_delete": "WITH x AS (SELECT 1) DELETE FROM customers",
    "cte_insert": "WITH x AS (SELECT 1) INSERT INTO plans SELECT * FROM plans",
    "cte_update": "WITH x AS (SELECT 1) UPDATE plans SET max_users = 0",
    "load_extension": "SELECT load_extension('evil')",
    "load_extension_upper_nested": "SELECT COALESCE(LOAD_EXTENSION('evil'), 1)",
    "load_extension_in_where": "SELECT 1 FROM plans WHERE load_extension('x') IS NULL",
    "readfile": "SELECT readfile('/etc/passwd')",
    "writefile": "SELECT writefile('/tmp/x', 'data')",
    "randomblob": "SELECT randomblob(1000000000)",
    "zeroblob": "SELECT zeroblob(1000000000)",
    "fts3_tokenizer": "SELECT fts3_tokenizer('x')",
    "pragma_function": "SELECT * FROM pragma_table_info('customers')",
    "pragma_function_join": "SELECT * FROM plans, pragma_database_list()",
    "sqlite_master": "SELECT sql FROM sqlite_master",
    "sqlite_master_cte_alias": "WITH sqlite_master AS (SELECT 1) SELECT * FROM main.sqlite_master",
    "sqlite_schema": "SELECT * FROM sqlite_schema",
    "sqlite_stat": "SELECT * FROM sqlite_stat1",
    "empty": "",
    "whitespace": "   \n ",
    "comment_only": "-- nothing here",
    "semicolon_only": ";",
    "garbage": "this is not sql at all",
    "unterminated_comment": "SELECT 1 /* never closed",
    "unterminated_string": "SELECT 'abc",
    "too_long": "SELECT 1 " + "+ 1 " * 10_000,
}


@pytest.mark.parametrize("sql", REJECTED.values(), ids=REJECTED.keys())
def test_rejected(sql):
    with pytest.raises(GuardError) as info:
        check_sql(sql)
    assert info.value.reason


def test_comment_trick_cannot_hide_second_statement():
    with pytest.raises(GuardError):
        check_sql("SELECT 1 /* ; */ ; /* */ DROP TABLE customers")


def test_comment_after_semicolon_is_not_a_second_statement():
    assert check_sql("SELECT 1; -- done").sql == "SELECT 1"


def test_string_containing_keywords_is_fine():
    result = check_sql("SELECT 'DROP TABLE x; load_extension(1)' AS s")
    assert "DROP TABLE" in result.sql


def test_function_name_inside_string_or_column_is_fine():
    check_sql("SELECT subject FROM support_tickets WHERE subject LIKE '%load_extension%'")


ACCEPTED = {
    "plain": "SELECT * FROM customers",
    "trailing_semicolon": "SELECT COUNT(*) FROM customers;",
    "union": "SELECT industry FROM customers UNION SELECT country FROM customers",
    "cte": "WITH c AS (SELECT * FROM customers) SELECT COUNT(*) FROM c",
    "recursive_cte": (
        "WITH RECURSIVE c(n) AS (SELECT 1 UNION ALL SELECT n + 1 FROM c WHERE n < 5) "
        "SELECT n FROM c"
    ),
    "monthly_revenue": (
        "SELECT strftime('%Y-%m', invoice_date) AS m, SUM(amount) FROM invoices "
        "WHERE status = 'paid' GROUP BY m ORDER BY m"
    ),
    "filter_clause": "SELECT SUM(amount) FILTER (WHERE status = 'paid') FROM invoices",
    "resolution_time": (
        "SELECT AVG((julianday(resolved_at) - julianday(created_at)) * 24) "
        "FROM support_tickets WHERE resolved_at IS NOT NULL"
    ),
    "relative_date": "SELECT date('now', '-12 months'), datetime('now')",
    "quarter": (
        "SELECT strftime('%Y', invoice_date) || '-Q' || "
        "((CAST(strftime('%m', invoice_date) AS INTEGER) + 2) / 3) FROM invoices"
    ),
    "window": (
        "SELECT customer_id, RANK() OVER (ORDER BY SUM(amount) DESC) FROM invoices "
        "GROUP BY customer_id"
    ),
    "case_coalesce": (
        "SELECT CASE WHEN mrr > 100 THEN 'big' ELSE 'small' END, COALESCE(end_date, 'open'), "
        "IFNULL(mrr, 0), NULLIF(mrr, 0), ROUND(mrr, 1), SUBSTR(status, 1, 3) FROM subscriptions"
    ),
    "join_on": (
        "SELECT c.industry, SUM(s.mrr) FROM subscriptions s "
        "JOIN customers c ON c.customer_id = s.customer_id GROUP BY c.industry"
    ),
    "string_functions": (
        "SELECT UPPER(company_name), LENGTH(company_name), TRIM(country) FROM customers"
    ),
    "group_concat": "SELECT GROUP_CONCAT(DISTINCT industry) FROM customers",
    "blob_cast": "SELECT CAST(customer_id AS TEXT) FROM customers",
}


@pytest.mark.parametrize("sql", ACCEPTED.values(), ids=ACCEPTED.keys())
def test_accepted(sql):
    result = check_sql(sql)
    assert result.sql.strip()


def test_allowed_tables_limit():
    allowed = frozenset({"customers"})
    check_sql("SELECT * FROM customers", allowed)
    with pytest.raises(GuardError, match="invoices"):
        check_sql("SELECT * FROM invoices", allowed)
    with pytest.raises(GuardError, match="invoices"):
        check_sql(
            "SELECT * FROM customers WHERE customer_id IN (SELECT customer_id FROM invoices)",
            allowed,
        )


def test_allowed_tables_exempts_cte_names():
    check_sql("WITH x AS (SELECT * FROM customers) SELECT * FROM x", frozenset({"customers"}))


def test_cte_name_does_not_shield_a_schema_qualified_table():
    with pytest.raises(GuardError):
        check_sql(
            "WITH invoices AS (SELECT 1) SELECT * FROM main.invoices", frozenset({"customers"})
        )


def test_unconditioned_join_is_flagged_for_the_plan_check():
    assert check_sql("SELECT * FROM customers, plans").unconditioned_join
    assert check_sql("SELECT * FROM customers CROSS JOIN plans").unconditioned_join
    assert not check_sql(
        "SELECT * FROM customers c JOIN subscriptions s ON c.customer_id = s.customer_id"
    ).unconditioned_join
