"""Glossary YAML: load, parse and validate against the database schema."""

from __future__ import annotations

import re
import sqlite3
from contextlib import closing
from pathlib import Path
from typing import TYPE_CHECKING, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError

if TYPE_CHECKING:
    from askdb.schema import SchemaInfo

_JOIN_RE = re.compile(r"^\s*(\w+)\.(\w+)\s*=\s*(\w+)\.(\w+)\s*$")
_FULL_SELECT_RE = re.compile(r"^\s*(select|with)\b", re.IGNORECASE)


class GlossaryError(Exception):
    """The glossary could not be loaded. `errors` lists every problem found."""

    def __init__(self, errors: list[str]):
        self.errors = errors
        super().__init__("; ".join(errors))


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ColumnDoc(_Model):
    name: str
    type: str | None = None
    primary_key: bool = False
    references: str | None = None
    description: str | None = None
    synonyms: list[str] = Field(default_factory=list)


class TableDoc(_Model):
    name: str
    description: str | None = None
    columns: list[ColumnDoc] = Field(default_factory=list)


class Calendar(_Model):
    fiscal_year_start_month: int = Field(default=1, ge=1, le=12)


class AsOf(_Model):
    column: str
    mode: Literal["data_max", "today"] = "data_max"


class Metric(_Model):
    sql: str
    description: str | None = None


class Example(_Model):
    q: str
    sql: str


class Glossary(_Model):
    name: str | None = None
    display_name: str | None = None
    path: str | None = None  # informational; the DB path comes from settings
    calendar: Calendar = Field(default_factory=Calendar)
    description: str | None = None
    as_of: AsOf | None = None
    tables: list[TableDoc] = Field(default_factory=list)
    metrics: dict[str, Metric] = Field(default_factory=dict)
    terms: dict[str, str] = Field(default_factory=dict)
    joins: list[str] = Field(default_factory=list)
    examples: list[Example] = Field(default_factory=list)
    excluded: list[str] = Field(default_factory=list)  # "table" or "table.column"


def load_glossary(path: str | Path) -> Glossary:
    """Parse the YAML file. Raises GlossaryError listing every structural problem."""
    path = Path(path)
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise GlossaryError([f"Cannot read glossary file {path}: {exc.strerror or exc}"]) from exc
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise GlossaryError([f"Glossary is not valid YAML: {exc}"]) from exc
    if not isinstance(data, dict):
        raise GlossaryError(["Glossary must be a YAML mapping at the top level."])
    try:
        return Glossary.model_validate(data)
    except ValidationError as exc:
        raise GlossaryError(
            [f"{'.'.join(str(p) for p in e['loc'])}: {e['msg']}" for e in exc.errors()]
        ) from exc


def validate_glossary(glossary: Glossary, schema: SchemaInfo, db_path: str | Path) -> list[str]:
    """Check every reference against the real schema. Returns error strings (empty = valid).

    `schema` must be the unfiltered introspection (nothing excluded); `db_path` is opened
    read-only to compile metric and example SQL with EXPLAIN (nothing is executed).
    """
    from askdb.schema import connect_readonly

    cols = {t.name.lower(): {c.name.lower() for c in t.columns} for t in schema.tables}
    errors: list[str] = []

    def check_ref(ref: str, where: str) -> None:
        table, _, column = ref.partition(".")
        if table.lower() not in cols:
            errors.append(f"{where}: unknown table '{table}'")
        elif column and column.lower() not in cols[table.lower()]:
            errors.append(f"{where}: unknown column '{table}.{column}'")

    seen_tables: set[str] = set()
    for t in glossary.tables:
        if t.name.lower() in seen_tables:
            errors.append(f"tables: '{t.name}' is documented twice")
        seen_tables.add(t.name.lower())
        if t.name.lower() not in cols:
            errors.append(f"tables: unknown table '{t.name}'")
            continue
        seen_cols: set[str] = set()
        for c in t.columns:
            if c.name.lower() in seen_cols:
                errors.append(f"tables.{t.name}: column '{c.name}' is documented twice")
            seen_cols.add(c.name.lower())
            if c.name.lower() not in cols[t.name.lower()]:
                errors.append(f"tables.{t.name}: unknown column '{c.name}'")
            if c.references:
                if "." not in c.references:
                    errors.append(f"tables.{t.name}.{c.name}: references must be 'table.column'")
                else:
                    check_ref(c.references, f"tables.{t.name}.{c.name}.references")

    if glossary.as_of:
        if "." not in glossary.as_of.column:
            errors.append("as_of.column must be 'table.column'")
        else:
            check_ref(glossary.as_of.column, "as_of.column")

    for j in glossary.joins:
        m = _JOIN_RE.match(j)
        if not m:
            errors.append(f"joins: '{j}' must look like 'a.x = b.y'")
            continue
        check_ref(f"{m[1]}.{m[2]}", f"joins '{j}'")
        check_ref(f"{m[3]}.{m[4]}", f"joins '{j}'")

    for e in glossary.excluded:
        check_ref(e, "excluded")

    with closing(connect_readonly(db_path)) as conn:
        for name, metric in glossary.metrics.items():
            err = _metric_error(conn, metric.sql, sorted(cols))
            if err:
                errors.append(f"metrics.{name}: SQL does not compile: {err}")
        for i, ex in enumerate(glossary.examples, 1):
            err = _compile_error(conn, ex.sql)
            if err:
                errors.append(f"examples[{i}]: SQL does not compile: {err}")
    return errors


def _compile_error(conn: sqlite3.Connection, sql: str) -> str | None:
    try:
        conn.execute(f"EXPLAIN {sql.strip().rstrip(';')}")
    except sqlite3.Error as exc:
        return str(exc)
    return None


def _metric_error(conn: sqlite3.Connection, sql: str, tables: list[str]) -> str | None:
    """A metric is a full SELECT, or an expression that must compile against some one table."""
    if _FULL_SELECT_RE.match(sql):
        return _compile_error(conn, sql)
    first_error = None
    for table in tables:
        err = _compile_error(conn, f'SELECT {sql.strip().rstrip(";")} FROM "{table}"')
        if err is None:
            return None
        first_error = first_error or err
    return first_error or "no tables to compile against"
