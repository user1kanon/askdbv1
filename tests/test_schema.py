import sqlite3

import pytest

from askdb.glossary import load_glossary
from askdb.schema import (
    SchemaError,
    build_schema_info,
    connect_readonly,
    introspect,
)


def table(schema, name):
    return next(t for t in schema.tables if t.name == name)


def column(tbl, name):
    return next(c for c in tbl.columns if c.name == name)


def test_sample_db_tables_and_row_counts(sample_db):
    schema = introspect(sample_db)
    counts = {t.name: t.row_count for t in schema.tables}
    assert counts == {
        "customers": 200,
        "invoices": 1831,
        "plans": 4,
        "subscriptions": 282,
        "support_tickets": 709,
    }


def test_sample_db_columns_pk_and_fk(sample_db):
    schema = introspect(sample_db)
    subs = table(schema, "subscriptions")
    assert column(subs, "subscription_id").primary_key
    assert column(subs, "end_date").not_null is False
    assert column(subs, "status").not_null is True
    fks = {(f.column, f.ref_table, f.ref_column) for f in subs.foreign_keys}
    assert fks == {
        ("customer_id", "customers", "customer_id"),
        ("plan_id", "plans", "plan_id"),
    }


def test_low_cardinality_text_columns_list_distinct_values(sample_db):
    schema = introspect(sample_db)
    industry = column(table(schema, "customers"), "industry")
    assert industry.distinct_values == [
        "Education",
        "Finance",
        "Healthcare",
        "Manufacturing",
        "Retail",
        "Technology",
    ]
    assert column(table(schema, "invoices"), "status").distinct_values is not None


def test_high_cardinality_and_non_text_columns_have_no_distinct_values(sample_db):
    schema = introspect(sample_db)
    assert column(table(schema, "customers"), "company_name").distinct_values is None  # 200 values
    assert column(table(schema, "invoices"), "amount").distinct_values is None  # REAL
    assert column(table(schema, "customers"), "customer_id").distinct_values is None  # PK


def test_distinct_cap_zero_skips_sampling(sample_db):
    schema = introspect(sample_db, distinct_cap=0)
    assert all(c.distinct_values is None for t in schema.tables for c in t.columns)


def test_indexes_and_implicit_fk_target(tmp_db):
    schema = introspect(tmp_db)
    parent = table(schema, "parent")
    by_name = {i.name: i for i in parent.indexes}
    assert by_name["idx_parent_name"].unique is True
    assert by_name["idx_parent_name"].columns == ["name"]
    assert by_name["idx_parent_ssn"].unique is False
    # `parent_id INTEGER REFERENCES parent` has no explicit target column
    fk = table(schema, "child").foreign_keys[0]
    assert (fk.ref_table, fk.ref_column) == ("parent", "id")


def test_excluded_tables_and_columns_are_hidden_entirely(tmp_db):
    schema = introspect(tmp_db, excluded=["secrets", "parent.ssn"])
    assert {t.name for t in schema.tables} == {"parent", "child"}
    parent = table(schema, "parent")
    assert "ssn" not in {c.name for c in parent.columns}
    assert "idx_parent_ssn" not in {i.name for i in parent.indexes}
    assert "ssn" not in schema.model_dump_json()
    assert "s3cret" not in schema.model_dump_json()


def test_exclusion_is_case_insensitive(tmp_db):
    schema = introspect(tmp_db, excluded=["SECRETS", "Parent.SSN"])
    assert {t.name for t in schema.tables} == {"parent", "child"}


def test_excluded_table_drops_foreign_keys_pointing_at_it(tmp_db):
    schema = introspect(tmp_db, excluded=["parent"])
    assert table(schema, "child").foreign_keys == []


def test_connection_is_read_only(sample_db):
    conn = connect_readonly(sample_db)
    with pytest.raises(sqlite3.OperationalError):
        conn.execute(
            "INSERT INTO plans(plan_id, plan_name, monthly_price, max_users, features) "
            "VALUES (99, 'x', 1, 1, 'x')"
        )
    conn.close()


def test_missing_file_raises_schema_error(tmp_path):
    with pytest.raises(SchemaError, match="not found"):
        introspect(tmp_path / "nope.db")


def test_non_sqlite_file_raises_schema_error(tmp_path):
    bad = tmp_path / "bad.db"
    bad.write_text("this is not a database" * 50)
    with pytest.raises(SchemaError):
        introspect(bad)


def test_build_schema_info_merges_glossary(sample_db, sample_glossary):
    schema = build_schema_info(sample_db, load_glossary(sample_glossary))
    customers = table(schema, "customers")
    assert customers.description.startswith("All registered customer companies")
    assert "Industry vertical" in column(customers, "industry").description
    assert schema.description.startswith("CloudMetrics is a B2B")
    assert schema.fiscal_year_start_month == 1


def test_build_schema_info_applies_exclusions(tmp_db, tmp_path):
    g = tmp_path / "g.yaml"
    g.write_text("excluded: [secrets, parent.ssn]\n")
    schema = build_schema_info(tmp_db, load_glossary(g))
    assert {t.name for t in schema.tables} == {"parent", "child"}
    assert "ssn" not in {c.name for c in table(schema, "parent").columns}
