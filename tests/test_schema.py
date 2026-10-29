import sqlite3

import pytest

from askdb.schema import DISTINCT_CAP, connect_readonly, introspect


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


def test_sample_db_keys_and_columns(sample_db):
    schema = introspect(sample_db)
    subs = schema.table("subscriptions")
    assert [c.name for c in subs.columns if c.primary_key] == ["subscription_id"]
    assert {(fk.column, fk.ref_table, fk.ref_column) for fk in subs.foreign_keys} == {
        ("customer_id", "customers", "customer_id"),
        ("plan_id", "plans", "plan_id"),
    }
    end_date = next(c for c in subs.columns if c.name == "end_date")
    assert end_date.not_null is False


def test_low_cardinality_text_gets_distincts(sample_db):
    schema = introspect(sample_db)
    status = next(c for c in schema.table("subscriptions").columns if c.name == "status")
    assert status.distinct_values == ["active", "churned", "paused", "trial"]


def test_high_cardinality_and_non_text_have_no_distincts(sample_db):
    schema = introspect(sample_db)
    company = next(c for c in schema.table("customers").columns if c.name == "company_name")
    mrr = next(c for c in schema.table("subscriptions").columns if c.name == "mrr")
    assert company.distinct_values is None
    assert mrr.distinct_values is None


def test_distinct_cap_boundary(tiny_db):
    schema = introspect(tiny_db)
    pets = schema.table("pets")
    kind = next(c for c in pets.columns if c.name == "kind")
    nickname = next(c for c in pets.columns if c.name == "nickname")
    assert kind.distinct_values == ["cat", "dog"]
    assert nickname.distinct_values is None  # 60 distinct > DISTINCT_CAP
    assert DISTINCT_CAP == 50


def test_indexes_and_fks(tiny_db):
    pets = introspect(tiny_db).table("pets")
    assert [(i.name, i.columns) for i in pets.indexes] == [("idx_pets_owner", ["owner_id"])]
    assert [(f.column, f.ref_table) for f in pets.foreign_keys] == [("owner_id", "owners")]


def test_excluded_column_is_not_read(tiny_db):
    schema = introspect(tiny_db, excluded=frozenset({"pets.kind"}))
    pets = schema.table("pets")
    assert "kind" not in [c.name for c in pets.columns]


def test_excluded_table_removed_with_its_fks(tiny_db):
    schema = introspect(tiny_db, excluded=frozenset({"owners"}))
    assert schema.table("owners") is None
    assert schema.table("pets").foreign_keys == []


def test_excluded_column_drops_covering_index(tiny_db):
    schema = introspect(tiny_db, excluded=frozenset({"pets.owner_id"}))
    assert schema.table("pets").indexes == []


def test_connection_is_read_only(tiny_db):
    conn = connect_readonly(tiny_db)
    with pytest.raises(sqlite3.OperationalError):
        conn.execute("INSERT INTO owners VALUES (9, 'Zed')")
    conn.close()


def test_empty_database_has_no_tables(tmp_path):
    path = tmp_path / "empty.db"
    sqlite3.connect(path).close()
    assert introspect(path).tables == []
