"""Schema introspection and glossary merge."""

from __future__ import annotations

import sqlite3
from contextlib import closing
from pathlib import Path

from pydantic import BaseModel, Field

from askdb.glossary import Example, Glossary, Metric

DISTINCT_CAP = 50
MAX_DISTINCT_VALUE_LEN = 80


class SchemaError(Exception):
    """The database could not be opened or read."""


class ForeignKey(BaseModel):
    column: str
    ref_table: str
    ref_column: str


class IndexInfo(BaseModel):
    name: str
    unique: bool
    columns: list[str]


class ColumnInfo(BaseModel):
    name: str
    type: str
    not_null: bool
    primary_key: bool
    description: str | None = None
    synonyms: list[str] = Field(default_factory=list)
    distinct_values: list[str] | None = None  # low-cardinality text columns only


class TableInfo(BaseModel):
    name: str
    description: str | None = None
    row_count: int
    columns: list[ColumnInfo]
    foreign_keys: list[ForeignKey]
    indexes: list[IndexInfo]


class SchemaInfo(BaseModel):
    tables: list[TableInfo]
    # Glossary content, present after merge_glossary
    description: str | None = None
    fiscal_year_start_month: int = 1
    as_of: dict[str, str] | None = None
    metrics: dict[str, Metric] = Field(default_factory=dict)
    terms: dict[str, str] = Field(default_factory=dict)
    joins: list[str] = Field(default_factory=list)
    examples: list[Example] = Field(default_factory=list)


def connect_readonly(db_path: str | Path) -> sqlite3.Connection:
    """Open the database read-only. The app never opens it read-write."""
    path = Path(db_path).resolve()
    if not path.is_file():
        raise SchemaError(f"Database file not found: {path}")
    try:
        conn = sqlite3.connect(f"{path.as_uri()}?mode=ro&immutable=0", uri=True)
        conn.execute("PRAGMA query_only=ON")
    except sqlite3.Error as exc:
        raise SchemaError(f"Cannot open {path} read-only: {exc}") from exc
    return conn


def _q(identifier: str) -> str:
    return '"' + identifier.replace('"', '""') + '"'


def _is_text(declared_type: str) -> bool:
    t = declared_type.upper()  # SQLite TEXT affinity rule
    return "CHAR" in t or "CLOB" in t or "TEXT" in t


def introspect(
    db_path: str | Path,
    *,
    excluded: list[str] | tuple[str, ...] = (),
    distinct_cap: int = DISTINCT_CAP,
) -> SchemaInfo:
    """Read tables, columns, keys, indexes, row counts and low-cardinality text values.

    Excluded tables/columns ("table" or "table.column") are never read, counted or returned.
    `distinct_cap=0` skips value sampling.
    """
    hidden_tables = {e.lower() for e in excluded if "." not in e}
    hidden_cols = {tuple(e.lower().split(".", 1)) for e in excluded if "." in e}

    def hidden(table: str, column: str) -> bool:
        return table.lower() in hidden_tables or (table.lower(), column.lower()) in hidden_cols

    try:
        with closing(connect_readonly(db_path)) as conn:
            names = [
                r[0]
                for r in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' "
                    "AND name NOT LIKE 'sqlite_%' ORDER BY name"
                )
                if r[0].lower() not in hidden_tables
            ]
            tables = [_table_info(conn, n, hidden, hidden_tables, distinct_cap) for n in names]
    except sqlite3.Error as exc:
        raise SchemaError(f"Cannot read the database (is it a SQLite file?): {exc}") from exc
    return SchemaInfo(tables=tables)


def _table_info(conn, name, hidden, hidden_tables, distinct_cap) -> TableInfo:
    columns = []
    for _cid, col, ctype, notnull, _dflt, pk in conn.execute(f"PRAGMA table_info({_q(name)})"):
        if hidden(name, col):
            continue
        distinct = None
        if distinct_cap and _is_text(ctype) and not pk:
            distinct = _distinct_values(conn, name, col, distinct_cap)
        columns.append(
            ColumnInfo(
                name=col,
                type=ctype,
                not_null=bool(notnull),
                primary_key=bool(pk),
                distinct_values=distinct,
            )
        )

    fks = []
    for _id, _seq, ref_table, frm, to, *_ in conn.execute(f"PRAGMA foreign_key_list({_q(name)})"):
        if hidden(name, frm) or ref_table.lower() in hidden_tables:
            continue
        if to is None:  # implicit reference to the parent's primary key
            to = next(
                (r[1] for r in conn.execute(f"PRAGMA table_info({_q(ref_table)})") if r[5]), ""
            )
        fks.append(ForeignKey(column=frm, ref_table=ref_table, ref_column=to))

    indexes = []
    for _seq, idx_name, unique, *_ in conn.execute(f"PRAGMA index_list({_q(name)})"):
        idx_cols = [r[2] for r in conn.execute(f"PRAGMA index_info({_q(idx_name)})")]
        if any(c is None or hidden(name, c) for c in idx_cols):
            continue
        indexes.append(IndexInfo(name=idx_name, unique=bool(unique), columns=idx_cols))

    row_count = conn.execute(f"SELECT COUNT(*) FROM {_q(name)}").fetchone()[0]
    return TableInfo(
        name=name,
        row_count=row_count,
        columns=columns,
        foreign_keys=fks,
        indexes=indexes,
    )


def _distinct_values(conn, table: str, column: str, cap: int) -> list[str] | None:
    rows = conn.execute(
        f"SELECT DISTINCT {_q(column)} FROM {_q(table)} WHERE {_q(column)} IS NOT NULL LIMIT ?",
        (cap + 1,),
    ).fetchall()
    values = [r[0] for r in rows]
    if len(values) > cap or any(
        not isinstance(v, str) or len(v) > MAX_DISTINCT_VALUE_LEN for v in values
    ):
        return None
    return sorted(values)


def merge_glossary(schema: SchemaInfo, glossary: Glossary) -> SchemaInfo:
    """Attach glossary descriptions and business definitions to an introspected schema."""
    docs = {t.name.lower(): t for t in glossary.tables}
    for table in schema.tables:
        doc = docs.get(table.name.lower())
        if not doc:
            continue
        table.description = doc.description
        col_docs = {c.name.lower(): c for c in doc.columns}
        for col in table.columns:
            cd = col_docs.get(col.name.lower())
            if cd:
                col.description = cd.description
                col.synonyms = cd.synonyms
    schema.description = glossary.description
    schema.fiscal_year_start_month = glossary.calendar.fiscal_year_start_month
    schema.as_of = glossary.as_of.model_dump() if glossary.as_of else None
    schema.metrics = glossary.metrics
    schema.terms = glossary.terms
    schema.joins = glossary.joins
    schema.examples = glossary.examples
    return schema


def build_schema_info(db_path: str | Path, glossary: Glossary) -> SchemaInfo:
    """Introspect the database (honouring `excluded`) and merge the glossary into it."""
    return merge_glossary(introspect(db_path, excluded=glossary.excluded), glossary)
