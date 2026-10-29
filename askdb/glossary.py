"""Glossary YAML: load, validate against the schema, and apply `excluded`."""

import re
import sqlite3
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from askdb.schema import Schema, _quote, connect_readonly

_JOIN_RE = re.compile(r"^\s*(\w+)\.(\w+)\s*=\s*(\w+)\.(\w+)\s*$")


class GlossaryError(Exception):
    """The glossary file is unreadable or does not parse into the expected shape."""

    def __init__(self, errors: list[str]):
        super().__init__("; ".join(errors))
        self.errors = errors


class _Model(BaseModel):
    # Unknown keys (e.g. `path`, `name` comments for other tools) are tolerated, not trusted.
    model_config = ConfigDict(extra="ignore")


class GlossaryColumn(_Model):
    name: str
    type: str | None = None
    description: str | None = None
    synonyms: list[str] = Field(default_factory=list)
    primary_key: bool = False
    references: str | None = None


class GlossaryTable(_Model):
    name: str
    description: str | None = None
    columns: list[GlossaryColumn] = Field(default_factory=list)


class Metric(_Model):
    sql: str
    description: str | None = None
    table: str | None = None  # table the expression is evaluated against, if any


class AsOf(_Model):
    column: str | None = None  # "table.column"
    mode: str = "data_max"  # data_max | today


class Example(_Model):
    q: str
    sql: str


class Calendar(_Model):
    fiscal_year_start_month: int = Field(1, ge=1, le=12)


class Glossary(_Model):
    name: str | None = None
    display_name: str | None = None
    description: str = ""
    calendar: Calendar = Field(default_factory=Calendar)
    as_of: AsOf | None = None
    tables: list[GlossaryTable] = Field(default_factory=list)
    metrics: dict[str, Metric] = Field(default_factory=dict)
    terms: dict[str, str] = Field(default_factory=dict)
    joins: list[str] = Field(default_factory=list)
    examples: list[Example] = Field(default_factory=list)
    excluded: list[str] = Field(default_factory=list)  # "table" or "table.column"

    @property
    def fiscal_year_start_month(self) -> int:
        return self.calendar.fiscal_year_start_month


def load_glossary(path: Path) -> Glossary:
    """Parse the YAML into a Glossary. Raises GlossaryError with readable messages."""
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise GlossaryError([f"Cannot read glossary file {path}: {exc.strerror or exc}"]) from exc
    except yaml.YAMLError as exc:
        raise GlossaryError([f"Glossary {path.name} is not valid YAML: {exc}"]) from exc
    if not isinstance(raw, dict):
        raise GlossaryError([f"Glossary {path.name} must be a YAML mapping at the top level."])
    if "fiscal_year_start_month" in raw and "calendar" not in raw:
        raw["calendar"] = {"fiscal_year_start_month": raw.pop("fiscal_year_start_month")}
    try:
        return Glossary.model_validate(raw)
    except ValidationError as exc:
        errors = [
            f"{'.'.join(str(p) for p in e['loc']) or 'glossary'}: {e['msg']}" for e in exc.errors()
        ]
        raise GlossaryError(errors) from exc


def _compiles(conn: sqlite3.Connection, sql: str) -> str | None:
    """None if SQLite can prepare `sql`, else the error message."""
    try:
        conn.execute("EXPLAIN " + sql)
    except sqlite3.Error as exc:
        return str(exc)
    return None


def _compiles_somewhere(conn: sqlite3.Connection, template: str, tables: list[str]) -> str | None:
    """Try `template.format(table=...)` against each table; None if any compiles."""
    last: str | None = None
    for table in tables:
        last = _compiles(conn, template.format(table=_quote(table)))
        if last is None:
            return None
    return last or "no tables to compile against"


def validate_glossary(glossary: Glossary, schema: Schema, db_path: Path) -> list[str]:
    """Check every reference against the real schema. Returns exact error messages."""
    errors: list[str] = []

    def column_exists(table: str, column: str) -> bool:
        t = schema.table(table)
        return t is not None and any(c.name == column for c in t.columns)

    for gtable in glossary.tables:
        if schema.table(gtable.name) is None:
            errors.append(f"tables: unknown table '{gtable.name}'")
            continue
        for gcol in gtable.columns:
            if not column_exists(gtable.name, gcol.name):
                errors.append(f"tables.{gtable.name}: unknown column '{gcol.name}'")
            if gcol.references:
                ref_table, _, ref_col = gcol.references.partition(".")
                if not column_exists(ref_table, ref_col):
                    errors.append(
                        f"tables.{gtable.name}.{gcol.name}: references unknown '{gcol.references}'"
                    )

    for join in glossary.joins:
        m = _JOIN_RE.match(join)
        if not m:
            errors.append(f"joins: '{join}' is not of the form table.column = table.column")
            continue
        for table, column in (m.group(1, 2), m.group(3, 4)):
            if not column_exists(table, column):
                errors.append(f"joins: '{join}' refers to unknown column '{table}.{column}'")

    if glossary.as_of and glossary.as_of.mode not in ("data_max", "today"):
        errors.append(f"as_of.mode: '{glossary.as_of.mode}' must be data_max or today")
    if glossary.as_of and glossary.as_of.mode == "data_max":
        table, _, column = (glossary.as_of.column or "").partition(".")
        if not column_exists(table, column):
            errors.append(f"as_of.column: unknown column '{glossary.as_of.column}'")

    for entry in glossary.excluded:
        table, _, column = entry.partition(".")
        if not (column_exists(table, column) if column else schema.table(table)):
            errors.append(f"excluded: unknown table or column '{entry}'")

    all_tables = [t.name for t in schema.tables]
    conn = connect_readonly(db_path)
    try:
        for name, metric in glossary.metrics.items():
            if metric.table and schema.table(metric.table) is None:
                errors.append(f"metrics.{name}: unknown table '{metric.table}'")
                continue
            tables = [metric.table] if metric.table else all_tables
            template = "SELECT " + metric.sql + " FROM {table}"
            if (msg := _compiles_somewhere(conn, template, tables)) is not None:
                errors.append(f"metrics.{name}: SQL does not compile: {msg}")
        for name, predicate in glossary.terms.items():
            template = "SELECT 1 FROM {table} WHERE " + predicate
            if (msg := _compiles_somewhere(conn, template, all_tables)) is not None:
                errors.append(f"terms.{name}: SQL does not compile: {msg}")
        for i, example in enumerate(glossary.examples):
            if (msg := _compiles(conn, example.sql)) is not None:
                errors.append(f"examples[{i}]: SQL does not compile: {msg}")
    finally:
        conn.close()
    return errors


def excluded_set(glossary: Glossary) -> frozenset[str]:
    return frozenset(glossary.excluded)


def apply_excluded(glossary: Glossary) -> Glossary:
    """Return a copy with excluded tables/columns removed.

    Metrics, terms and examples whose SQL mentions an excluded name as a whole word are dropped
    too, so nothing about an excluded field reaches the model through them. Free-text
    `description` fields are the admin's responsibility.
    """
    hidden_names: set[str] = set()
    for entry in glossary.excluded:
        # A column entry hides the column name; the table name alone stays usable.
        hidden_names.add(entry.rpartition(".")[2])

    def mentions_hidden(sql: str) -> bool:
        words = set(re.findall(r"\w+", sql))
        return bool(words & hidden_names)

    data: dict[str, Any] = glossary.model_dump()
    tables = []
    for gtable in data["tables"]:
        if gtable["name"] in glossary.excluded:
            continue
        gtable["columns"] = [
            c for c in gtable["columns"] if f"{gtable['name']}.{c['name']}" not in glossary.excluded
        ]
        tables.append(gtable)
    data["tables"] = tables
    data["metrics"] = {k: v for k, v in data["metrics"].items() if not mentions_hidden(v["sql"])}
    data["terms"] = {k: v for k, v in data["terms"].items() if not mentions_hidden(v)}
    data["examples"] = [e for e in data["examples"] if not mentions_hidden(e["sql"])]
    data["joins"] = [j for j in data["joins"] if not mentions_hidden(j)]
    return Glossary.model_validate(data)
