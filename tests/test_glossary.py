import pytest

from askdb.glossary import (
    GlossaryError,
    apply_excluded,
    excluded_set,
    load_glossary,
    validate_glossary,
)
from askdb.schema import introspect, merge_glossary


def write(tmp_path, text):
    path = tmp_path / "g.yaml"
    path.write_text(text, encoding="utf-8")
    return path


def validate(tmp_path, text, db):
    glossary = load_glossary(write(tmp_path, text))
    return validate_glossary(glossary, introspect(db, with_distincts=False), db)


def test_sample_glossary_loads_and_validates(sample_db, sample_glossary):
    glossary = load_glossary(sample_glossary)
    assert glossary.name == "cloudmetrics"
    assert glossary.fiscal_year_start_month == 1
    assert [t.name for t in glossary.tables][0] == "customers"
    assert validate_glossary(glossary, introspect(sample_db, with_distincts=False), sample_db) == []


def test_missing_file(tmp_path):
    with pytest.raises(GlossaryError, match="Cannot read glossary"):
        load_glossary(tmp_path / "nope.yaml")


def test_bad_yaml(tmp_path):
    with pytest.raises(GlossaryError, match="not valid YAML"):
        load_glossary(write(tmp_path, "tables: [unclosed"))


def test_top_level_must_be_mapping(tmp_path):
    with pytest.raises(GlossaryError, match="mapping"):
        load_glossary(write(tmp_path, "- a\n- b\n"))


def test_wrong_shape_reports_location(tmp_path):
    with pytest.raises(GlossaryError) as info:
        load_glossary(write(tmp_path, "tables:\n  - description: no name\n"))
    assert any(e.startswith("tables.0.name") for e in info.value.errors)


def test_fiscal_year_month_out_of_range(tmp_path):
    with pytest.raises(GlossaryError):
        load_glossary(write(tmp_path, "calendar: {fiscal_year_start_month: 13}\n"))


def test_top_level_fiscal_year_key_is_accepted(tmp_path):
    glossary = load_glossary(write(tmp_path, "fiscal_year_start_month: 4\n"))
    assert glossary.fiscal_year_start_month == 4


def test_bad_table(tmp_path, tiny_db):
    errors = validate(tmp_path, "tables:\n  - name: dragons\n", tiny_db)
    assert errors == ["tables: unknown table 'dragons'"]


def test_bad_column(tmp_path, tiny_db):
    text = "tables:\n  - name: pets\n    columns:\n      - {name: wings}\n"
    assert validate(tmp_path, text, tiny_db) == ["tables.pets: unknown column 'wings'"]


def test_bad_reference(tmp_path, tiny_db):
    text = (
        "tables:\n  - name: pets\n    columns:\n      - {name: owner_id, references: owners.nope}\n"
    )
    errors = validate(tmp_path, text, tiny_db)
    assert errors == ["tables.pets.owner_id: references unknown 'owners.nope'"]


def test_bad_join(tmp_path, tiny_db):
    errors = validate(
        tmp_path, "joins:\n  - pets.owner_id = owners.missing\n  - nonsense\n", tiny_db
    )
    assert (
        "joins: 'pets.owner_id = owners.missing' refers to unknown column 'owners.missing'"
        in errors
    )
    assert any("is not of the form" in e for e in errors)


def test_bad_metric_sql(tmp_path, tiny_db):
    text = "metrics:\n  pets_total:\n    sql: 'SUM(no_such_col)'\n"
    errors = validate(tmp_path, text, tiny_db)
    assert len(errors) == 1
    assert errors[0].startswith("metrics.pets_total: SQL does not compile")


def test_metric_unknown_table(tmp_path, tiny_db):
    text = "metrics:\n  m:\n    sql: 'COUNT(*)'\n    table: ghosts\n"
    assert validate(tmp_path, text, tiny_db) == ["metrics.m: unknown table 'ghosts'"]


def test_good_metric_term_and_example(tmp_path, tiny_db):
    text = (
        "metrics:\n  n_pets:\n    sql: 'COUNT(*)'\n    table: pets\n"
        "terms:\n  dog: \"kind = 'dog'\"\n"
        "examples:\n  - {q: how many, sql: 'SELECT COUNT(*) FROM pets'}\n"
    )
    assert validate(tmp_path, text, tiny_db) == []


def test_bad_term_and_example_sql(tmp_path, tiny_db):
    text = "terms:\n  x: \"nope = 1\"\nexamples:\n  - {q: q, sql: 'SELEC 1'}\n"
    errors = validate(tmp_path, text, tiny_db)
    assert any(e.startswith("terms.x:") for e in errors)
    assert any(e.startswith("examples[0]:") for e in errors)


def test_metric_sql_cannot_write(tmp_path, tiny_db):
    """Validation compiles with EXPLAIN on a read-only connection; it never executes."""
    text = "metrics:\n  m:\n    sql: '1; DROP TABLE pets'\n    table: pets\n"
    validate(tmp_path, text, tiny_db)
    assert introspect(tiny_db, with_distincts=False).table("pets") is not None


def test_bad_excluded_and_as_of(tmp_path, tiny_db):
    text = "excluded: [pets.ghost, nowhere]\nas_of: {column: pets.nope, mode: data_max}\n"
    errors = validate(tmp_path, text, tiny_db)
    assert "excluded: unknown table or column 'pets.ghost'" in errors
    assert "excluded: unknown table or column 'nowhere'" in errors
    assert "as_of.column: unknown column 'pets.nope'" in errors


def test_bad_as_of_mode(tmp_path, tiny_db):
    errors = validate(tmp_path, "as_of: {mode: tomorrow}\n", tiny_db)
    assert errors[0] == "as_of.mode: 'tomorrow' must be data_max or today"


def test_excluded_is_applied_to_glossary_and_merge(tmp_path, tiny_db):
    text = (
        "tables:\n"
        "  - name: pets\n"
        "    columns:\n"
        "      - {name: kind, description: secret kind}\n"
        "      - {name: nickname, description: visible nick}\n"
        "metrics:\n"
        "  by_kind: {sql: 'COUNT(DISTINCT kind)', table: pets}\n"
        "  n: {sql: 'COUNT(*)', table: pets}\n"
        "excluded: [pets.kind]\n"
    )
    glossary = load_glossary(write(tmp_path, text))
    cleaned = apply_excluded(glossary)
    assert set(cleaned.metrics) == {"n"}
    schema = introspect(tiny_db, excluded=excluded_set(glossary))
    merged = merge_glossary(schema, cleaned)
    pets = next(t for t in merged["tables"] if t["name"] == "pets")
    assert [c["name"] for c in pets["columns"]] == ["id", "owner_id", "nickname", "born"]
    assert "secret kind" not in str(merged)
    nick = next(c for c in pets["columns"] if c["name"] == "nickname")
    assert nick["description"] == "visible nick"


def test_excluded_table_is_dropped_from_merge(tmp_path, tiny_db):
    glossary = load_glossary(
        write(tmp_path, "tables:\n  - {name: owners, description: hush}\nexcluded: [owners]\n")
    )
    merged = merge_glossary(
        introspect(tiny_db, excluded=excluded_set(glossary)), apply_excluded(glossary)
    )
    assert [t["name"] for t in merged["tables"]] == ["pets"]
    assert "hush" not in str(merged)


def test_merge_carries_descriptions_for_sample(sample_db, sample_glossary):
    glossary = load_glossary(sample_glossary)
    merged = merge_glossary(introspect(sample_db), apply_excluded(glossary))
    subs = next(t for t in merged["tables"] if t["name"] == "subscriptions")
    mrr = next(c for c in subs["columns"] if c["name"] == "mrr")
    assert "Already monthly" in mrr["description"]
    assert merged["description"].startswith("CloudMetrics is a B2B")
