"""Static checks on LLM-generated SQL: one SELECT, allowed functions, no cartesian join.

This is one layer of the defense in depth in SPEC §6. `executor` adds the read-only connection,
the authorizer, the timeout and the row cap; none of those layers trusts the others.
"""

import re
import sqlite3
from dataclasses import dataclass, field

import sqlglot
from sqlglot import exp
from sqlglot.errors import SqlglotError
from sqlglot.tokens import Tokenizer, TokenType

MAX_SQL_CHARS = 20_000

# SQLite function names (lower case). Shared with the executor's authorizer, which sees the
# names SQLite itself uses. Deliberately absent: load_extension, readfile, writefile, edit,
# fts3_tokenizer, randomblob, zeroblob, random, printf/format, sqlite_* introspection, pragma_*.
ALLOWED_FUNCTIONS = frozenset(
    {
        # aggregates
        "count", "sum", "avg", "total", "min", "max", "group_concat", "string_agg",
        # window
        "row_number", "rank", "dense_rank", "percent_rank", "cume_dist", "ntile",
        "lag", "lead", "first_value", "last_value", "nth_value",
        # scalar
        "abs", "coalesce", "ifnull", "iif", "nullif", "instr", "length", "octet_length",
        "lower", "upper", "ltrim", "rtrim", "trim", "replace", "round", "sign",
        "substr", "substring", "typeof", "hex", "char", "unicode", "concat", "concat_ws",
        "like", "glob",
        # math
        "ceil", "ceiling", "floor", "trunc", "sqrt", "pow", "power", "ln", "log", "log10",
        "log2", "exp", "mod", "pi",
        # dates
        "date", "time", "datetime", "julianday", "strftime", "unixepoch",
    }
)  # fmt: skip

_PARSER_ONLY_FUNCTIONS = frozenset({"cast"})  # syntax in SQLite, a Func node in sqlglot

_FORBIDDEN_NODES = (
    exp.Insert,
    exp.Update,
    exp.Delete,
    exp.Merge,
    exp.Create,
    exp.Drop,
    exp.Alter,
    exp.Command,
    exp.Pragma,
    exp.Attach,
    exp.Detach,
    exp.Transaction,
    exp.Commit,
    exp.Rollback,
    exp.Set,
    exp.Use,
)
_CALL_RE = re.compile(r"\s*([A-Za-z_]\w*)\s*\(")


class GuardError(Exception):
    """The SQL was rejected. `reason` is safe to show the user."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


@dataclass
class GuardResult:
    sql: str  # text to execute: the original, minus trailing semicolons and comments
    unconditioned_join: bool = False  # a join with no ON/USING (comma or CROSS join)
    base_table_aliases: frozenset[str] = field(default_factory=frozenset)


def _execution_text(sql: str) -> str:
    """The original SQL cut after its last non-semicolon token.

    Drops trailing `;` and trailing comments so the text can sit inside a wrapping subquery.
    """
    tokens = Tokenizer().tokenize(sql)
    kept = [t for t in tokens if t.token_type != TokenType.SEMICOLON]
    if not kept:
        raise GuardError("The query is empty.")
    return sql[: kept[-1].end + 1]


def _function_name(node: exp.Func) -> str | None:
    if isinstance(node, exp.Anonymous):
        return node.name.lower()
    match = _CALL_RE.match(node.sql(dialect="sqlite"))
    return match.group(1).lower() if match else None


def _has_condition(join: exp.Join) -> bool:
    """True if the join has USING, or an ON that mentions a column.

    sqlglot fills a bare `JOIN t` with `ON TRUE`, and `ON 1 = 1` is no better, so an ON clause
    without any column reference counts as no condition.
    """
    if join.args.get("using"):
        return True
    on = join.args.get("on")
    return on is not None and on.find(exp.Column) is not None


def check_sql(sql: str, allowed_tables: frozenset[str] | None = None) -> GuardResult:
    """Reject anything but a single read-only SELECT / WITH ... SELECT.

    `allowed_tables`, when given, limits which tables the query may read (CTE names are exempt).
    Raises GuardError with a plain-language reason.
    """
    if not sql or not sql.strip():
        raise GuardError("The query is empty.")
    if len(sql) > MAX_SQL_CHARS:
        raise GuardError(f"The query is longer than {MAX_SQL_CHARS} characters.")
    try:
        parsed = sqlglot.parse(sql, read="sqlite")
        # A trailing comment after `;` parses as an empty Semicolon node, not a statement.
        statements = [s for s in parsed if s is not None and not isinstance(s, exp.Semicolon)]
        text = _execution_text(sql)
    except SqlglotError as exc:
        raise GuardError(f"The query could not be parsed: {str(exc).splitlines()[0]}") from exc
    except RecursionError as exc:
        raise GuardError("The query is too deeply nested.") from exc

    if len(statements) != 1:
        raise GuardError("Only a single statement is allowed.")
    tree = statements[0]
    if not isinstance(tree, (exp.Select, exp.SetOperation)):
        raise GuardError("Only SELECT queries are allowed (the database is read-only).")

    for node in tree.walk():
        if isinstance(node, _FORBIDDEN_NODES):
            raise GuardError(f"{type(node).__name__.upper()} is not allowed (read-only).")

    for func in tree.find_all(exp.Func):
        name = _function_name(func)
        if name is not None and name not in ALLOWED_FUNCTIONS | _PARSER_ONLY_FUNCTIONS:
            raise GuardError(f"The function {name}() is not allowed.")

    cte_names = {cte.alias_or_name.lower() for cte in tree.find_all(exp.CTE)}
    aliases: set[str] = set()
    for table in tree.find_all(exp.Table):
        name = table.name.lower()
        if not name:  # table-valued function; the function allowlist above already ruled on it
            continue
        if name in cte_names and not table.args.get("db"):
            continue
        if name.startswith("sqlite_"):
            raise GuardError("Reading SQLite internal tables is not allowed.")
        if allowed_tables is not None and name not in {t.lower() for t in allowed_tables}:
            raise GuardError(f"The table '{table.name}' is not available.")
        aliases.add(table.alias_or_name.lower())

    unconditioned = any(not _has_condition(join) for join in tree.find_all(exp.Join))
    return GuardResult(text, unconditioned, frozenset(aliases))


def check_plan(conn: sqlite3.Connection, guarded: GuardResult) -> None:
    """Reject joins that SQLite plans as nested full scans of unrelated tables.

    A join with no condition shows up in EXPLAIN QUERY PLAN as two sibling `SCAN` steps over base
    tables. A join with an equality condition gets an index (or an automatic one) and shows
    `SEARCH`. The plan alone would also flag inequality joins, so a query is only rejected when
    it also contains a join without ON/USING.
    """
    if not guarded.unconditioned_join:
        return
    try:
        plan = conn.execute("EXPLAIN QUERY PLAN\n" + guarded.sql).fetchall()
    except sqlite3.Error:
        return  # a bad query is reported by the real execution, with SQLite's message
    scans_by_parent: dict[int, int] = {}
    for _id, parent, _unused, detail in plan:
        match = re.match(r"SCAN (?:TABLE )?(\S+)", detail)
        if match and match.group(1).lower() in guarded.base_table_aliases:
            scans_by_parent[parent] = scans_by_parent.get(parent, 0) + 1
    if any(count > 1 for count in scans_by_parent.values()):
        raise GuardError(
            "The query joins tables without a join condition, which would multiply every row "
            "by every row. Add an ON clause (or a WHERE that links the tables)."
        )
