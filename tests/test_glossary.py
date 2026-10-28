import pytest

from askdb.glossary import GlossaryError, load_glossary, validate_glossary
from askdb.schema import introspect


def write(tmp_path, text):
    p = tmp_path / "g.yaml"
    p.write_text(text, encoding="utf-8")
    return p


def check(tmp_path, text, db):
    glossary = load_glossary(write(tmp_path, text))
    return validate_glossary(glossary, introspect(db, distinct_cap=0), db)


def test_shipped_glossary_loads_and_validates(sample_glossary, sample_db):
    glossary = load_glossary(sample_glossary)
    assert glossary.calendar.fiscal_year_start_month == 1
    assert {t.name for t in glossary.tables} == {
        "customers",
        "plans",
        "subscriptions",
        "invoices",
        "support_tickets",
    }
    assert validate_glossary(glossary, introspect(sample_db, distinct_cap=0), sample_db) == []


def test_full_featured_glossary_validates(tmp_path, sample_db):
    text = """
as_of: { column: invoices.invoice_date, mode: data_max }
calendar: { fiscal_year_start_month: 4 }
tables:
  - name: invoices
    description: Billing
    columns:
      - { name: status, description: "paid etc", synonyms: [state] }
metrics:
  revenue: { sql: "SUM(amount) FILTER (WHERE status = 'paid')", description: Net revenue }
  active_mrr: { sql: "SELECT SUM(mrr) FROM subscriptions WHERE status = 'active'" }
terms:
  paid invoice: "status = 'paid'"
joins:
  - invoices.customer_id = customers.customer_id
examples:
  - q: How many invoices?
    sql: SELECT COUNT(*) FROM invoices
excluded: [support_tickets.subject]
"""
    assert check(tmp_path, text, sample_db) == []


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("tables:\n  - name: nope\n", "unknown table 'nope'"),
        ("tables:\n  - name: plans\n    columns:\n      - name: nope\n", "unknown column 'nope'"),
        (
            "tables:\n  - name: plans\n    columns:\n      - {name: plan_id, references: ghost.id}",
            "unknown table 'ghost'",
        ),
        (
            "tables:\n  - name: plans\n    columns:\n      - {name: plan_id, references: plans.zz}",
            "unknown column 'plans.zz'",
        ),
        ("tables:\n  - name: plans\n  - name: plans\n", "documented twice"),
        ("as_of: { column: invoices.nope }\n", "unknown column 'invoices.nope'"),
        ("as_of: { column: nodot }\n", "must be 'table.column'"),
        ("joins:\n  - a is b\n", "must look like"),
        ("joins:\n  - invoices.zz = customers.customer_id\n", "unknown column 'invoices.zz'"),
        ("excluded: [ghost]\n", "excluded: unknown table 'ghost'"),
        ("excluded: [plans.ghost]\n", "unknown column 'plans.ghost'"),
        ("metrics:\n  bad: { sql: 'SUM(no_such_col)' }\n", "metrics.bad: SQL does not compile"),
        ("metrics:\n  bad: { sql: 'SELECT FROM WHERE' }\n", "metrics.bad: SQL does not compile"),
        ("metrics:\n  bad: { sql: 'SUM(amount); DROP TABLE plans' }\n", "metrics.bad"),
        (
            "examples:\n  - q: x\n    sql: SELECT nope FROM plans\n",
            "examples[1]: SQL does not compile",
        ),
    ],
)
def test_invalid_references_are_reported(tmp_path, sample_db, text, expected):
    errors = check(tmp_path, text, sample_db)
    assert any(expected in e for e in errors), errors


def test_all_errors_are_collected_not_just_the_first(tmp_path, sample_db):
    text = "tables:\n  - name: nope\nexcluded: [ghost]\nmetrics:\n  m: { sql: 'SUM(zz)' }\n"
    assert len(check(tmp_path, text, sample_db)) == 3


def test_metric_validation_never_executes_or_writes(tmp_path, sample_db):
    check(tmp_path, "metrics:\n  m: { sql: 'SELECT 1; DROP TABLE plans' }\n", sample_db)
    assert introspect(sample_db, distinct_cap=0).tables  # still intact (and ro anyway)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("calendar: { fiscal_year_start_month: 13 }\n", "fiscal_year_start_month"),
        ("calendar: { fiscal_year_start_month: 0 }\n", "fiscal_year_start_month"),
        ("as_of: { column: a.b, mode: yesterday }\n", "as_of.mode"),
        ("mystery_key: 1\n", "mystery_key"),
        ("tables:\n  - name: plans\n    colums: []\n", "colums"),
        ("tables:\n  - description: no name\n", "name"),
        ("metrics:\n  m: { description: no sql }\n", "metrics.m.sql"),
        ("- just\n- a list\n", "YAML mapping"),
        ("", "YAML mapping"),
        ("key: [unclosed\n", "not valid YAML"),
    ],
)
def test_structural_errors_raise_glossary_error(tmp_path, text, expected):
    with pytest.raises(GlossaryError) as exc:
        load_glossary(write(tmp_path, text))
    assert any(expected in e for e in exc.value.errors), exc.value.errors


def test_missing_file_raises_glossary_error(tmp_path):
    with pytest.raises(GlossaryError, match="Cannot read glossary file"):
        load_glossary(tmp_path / "missing.yaml")
