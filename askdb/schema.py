"""SQLite schema introspection and the merge with the glossary."""

import sqlite3
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from askdb.glossary import Glossary

DISTINCT_CAP = 50
_TEXT_TYPE_HINTS = ("CHAR", "TEXT", "CLOB")


@dataclass
class Column:
    name: str
    type: str
    not_null: bool
    primary_key: bool
    distinct_values: list[str] | None = None


@dataclass
class ForeignKey:
    column: str
    ref_table: str
    ref_column: str


@dataclass
class Index:
    name: str
    unique: bool
    columns: list[str]


@dataclass
class Table:
    name: str
    row_count: int
    columns: list[Column] = field(default_factory=list)
    foreign_keys: list[ForeignKey] = field(default_factory=list)
    indexes: list[Index] = field(default_factory=list)


@dataclass
class Schema:
    tables: list[Table]

    def table(self, name: str) -> Table | None:
        return next((t for t in self.tables if t.name == name), None)


def connect_readonly(db_path: Path) -> sqlite3.Connection:
    uri = f"{db_path.resolve().as_uri()}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    conn.execute("PRAGMA query_only=ON")
    return conn


def _quote(identifier: str) -> str:
    return '"' + identifier.replace('"', '""') + '"'


def _is_text(declared_type: str) -> bool:
    upper = declared_type.upper()
    return upper == "" or any(hint in upper for hint in _TEXT_TYPE_HINTS)


def _distincts(conn: sqlite3.Connection, table: str, column: str) -> list[str] | None:
    """Distinct non-NULL values, or None when there are more than DISTINCT_CAP."""
    rows = conn.execute(
        f"SELECT DISTINCT {_quote(column)} FROM {_quote(table)} "
        f"WHERE {_quote(column)} IS NOT NULL LIMIT ?",
        (DISTINCT_CAP + 1,),
    ).fetchall()
    if len(rows) > DISTINCT_CAP:
        return None
    return sorted(str(r[0]) for r in rows)


def introspect(
    db_path: Path,
    excluded: frozenset[str] = frozenset(),
    with_distincts: bool = True,
) -> Schema:
    """Read tables, columns, keys, indexes and row counts.

    `excluded` holds "table" or "table.column" entries; those are skipped entirely, so their
    values are never read. Distincts are collected for low-cardinality text columns only.
    """
    conn = connect_readonly(db_path)
    try:
        names = [
            r[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' "
                "ORDER BY name"
            )
        ]
        tables = []
        for name in names:
            if name in excluded:
                continue
            hidden = {e.split(".", 1)[1] for e in excluded if e.startswith(name + ".")}
            row_count = conn.execute(f"SELECT COUNT(*) FROM {_quote(name)}").fetchone()[0]
            table = Table(name=name, row_count=row_count)
            for _, col, ctype, notnull, _, pk in conn.execute(f"PRAGMA table_info({_quote(name)})"):
                if col in hidden:
                    continue
                column = Column(col, ctype, bool(notnull), pk > 0)
                if with_distincts and _is_text(ctype):
                    column.distinct_values = _distincts(conn, name, col)
                table.columns.append(column)
            kept = {c.name for c in table.columns}
            for fk in conn.execute(f"PRAGMA foreign_key_list({_quote(name)})"):
                _, _, ref_table, from_col, to_col, *_ = fk
                ref_hidden = ref_table in excluded or f"{ref_table}.{to_col}" in excluded
                if from_col in kept and not ref_hidden:
                    table.foreign_keys.append(ForeignKey(from_col, ref_table, to_col))
            for _, idx_name, unique, *_ in conn.execute(f"PRAGMA index_list({_quote(name)})"):
                cols = [r[2] for r in conn.execute(f"PRAGMA index_info({_quote(idx_name)})")]
                if all(c in kept for c in cols):
                    table.indexes.append(Index(idx_name, bool(unique), cols))
            tables.append(table)
        return Schema(tables)
    finally:
        conn.close()


def merge_glossary(schema: Schema, glossary: "Glossary") -> dict[str, Any]:
    """Schema plus glossary text, as the JSON served by /api/schema-info.

    `schema` is already free of excluded tables and columns, and only its tables are emitted,
    so glossary text for excluded entries never appears.
    """
    described = {t.name: t for t in glossary.tables}
    tables = []
    for table in schema.tables:
        out = asdict(table)
        gtable = described.get(table.name)
        out["description"] = gtable.description if gtable else None
        gcols = {c.name: c for c in gtable.columns} if gtable else {}
        for col in out["columns"]:
            gcol = gcols.get(col["name"])
            col["description"] = gcol.description if gcol else None
            col["synonyms"] = list(gcol.synonyms) if gcol else []
        tables.append(out)
    return {
        "name": glossary.name,
        "display_name": glossary.display_name,
        "description": glossary.description,
        "fiscal_year_start_month": glossary.fiscal_year_start_month,
        "as_of": glossary.as_of.model_dump() if glossary.as_of else None,
        "tables": tables,
        "metrics": {k: v.model_dump() for k, v in glossary.metrics.items()},
        "terms": glossary.terms,
        "joins": glossary.joins,
        "examples": [e.model_dump() for e in glossary.examples],
    }
