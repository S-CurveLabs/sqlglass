"""Tree-level questions about a query, answered with sqlglot (T-SQL dialect)."""

from __future__ import annotations

from dataclasses import dataclass, field

import sqlglot
from sqlglot import exp
from sqlglot.errors import SqlglotError

from ..errors import SqlGlassError
from .lexer import split_batches

DIALECTS = ("tsql", "postgres", "mysql", "sqlite", "snowflake", "bigquery", "databricks", "oracle", "duckdb", "redshift")


class ParseFailure(SqlGlassError):
    pass


@dataclass(frozen=True)
class TableRef:
    catalog: str
    schema: str
    name: str
    alias: str = ""

    @property
    def full(self) -> str:
        return ".".join(p for p in (self.catalog, self.schema, self.name) if p)

    @property
    def key(self) -> str:
        """Lower-case name as written, without the database part: 'dbo.vendor' or just 'vendor'."""
        return ".".join(p for p in (self.schema, self.name) if p).lower()

    @property
    def is_temp(self) -> bool:
        return self.name.startswith("#") or self.name.startswith("@")


@dataclass
class Analysis:
    statements: list[exp.Expression]
    tables: list[TableRef] = field(default_factory=list)
    ctes: list[str] = field(default_factory=list)
    columns: dict[str, list[str]] = field(default_factory=dict)  # table key -> column names
    unresolved_columns: list[str] = field(default_factory=list)  # no/unknown qualifier
    output_columns: list[str] = field(default_factory=list)

    def summary(self) -> dict:
        return {
            "tables": [t.full for t in self.tables],
            "ctes": self.ctes,
            "columns_by_table": self.columns,
            "unqualified_columns": self.unresolved_columns,
            "output_columns": self.output_columns,
        }


def table_name(t: exp.Table) -> str:
    """The name as written: sqlglot strips the # / ## / @ that marks temp tables and table variables."""
    ident = t.this
    if isinstance(ident, exp.Identifier):
        return ("##" if ident.args.get("global_") else "#" if ident.args.get("temporary") else "") + t.name
    return ("@" if isinstance(ident, (exp.Parameter, exp.Var)) or "@" in ident.sql(dialect="tsql")[:1] else "") + t.name


def parse(sql: str, dialect: str = "tsql") -> list[exp.Expression]:
    out: list[exp.Expression] = []
    for batch in split_batches(sql) if dialect == "tsql" else [sql]:
        try:
            out.extend(e for e in sqlglot.parse(batch, read=dialect) if e is not None)
        except SqlglotError as ex:
            first = str(ex).splitlines()[0]
            raise ParseFailure(f"Could not parse the SQL as {dialect}: {first}") from None
    return out


def analyze(sql: str) -> Analysis:
    a = Analysis(parse(sql))
    seen_tables: dict[str, TableRef] = {}
    columns: dict[str, dict[str, str]] = {}
    unresolved: dict[str, str] = {}
    for stmt in a.statements:
        cte_names = {c.alias.lower() for c in stmt.find_all(exp.CTE)}
        a.ctes.extend(c.alias for c in stmt.find_all(exp.CTE) if c.alias not in a.ctes)
        alias_map: dict[str, TableRef] = {}
        real: list[TableRef] = []
        for t in stmt.find_all(exp.Table):
            if not t.name or (not t.db and t.name.lower() in cte_names):
                continue
            ref = TableRef(t.catalog, t.db, table_name(t), t.alias)
            real.append(ref)
            seen_tables.setdefault(ref.full.lower(), ref)
            alias_map[(t.alias or t.name).lower()] = ref
        for c in stmt.find_all(exp.Column):
            if isinstance(c.this, exp.Star):
                continue
            qualifier = c.table.lower()
            ref = alias_map.get(qualifier) if qualifier else (real[0] if len(real) == 1 and not cte_names else None)
            if ref is not None:
                columns.setdefault(ref.key, {}).setdefault(c.name.lower(), c.name)
            elif not qualifier or qualifier not in cte_names:
                unresolved.setdefault(c.sql(dialect="tsql").lower(), c.sql(dialect="tsql"))
        if isinstance(stmt, exp.Query):
            a.output_columns = [s.alias_or_name or s.sql(dialect="tsql") for s in stmt.selects]
    a.tables = list(seen_tables.values())
    a.columns = {k: sorted(v.values(), key=str.lower) for k, v in sorted(columns.items())}
    a.unresolved_columns = sorted(unresolved.values(), key=str.lower)
    return a


def format_sql(sql: str) -> str:
    """Canonical pretty-print. Normalises style (adds AS, rewrites [x] = expr aliases); semantics are unchanged."""
    batches = []
    for batch in split_batches(sql):
        try:
            batches.append(";\n\n".join(sqlglot.transpile(batch, read="tsql", write="tsql", pretty=True)))
        except SqlglotError as ex:
            raise ParseFailure(f"Could not parse the SQL, so it was not formatted: {str(ex).splitlines()[0]}") from None
    return "\nGO\n".join(batches) + "\n"


def translate(sql: str, to: str, source: str = "tsql") -> tuple[str, list[str]]:
    for d in (to, source):
        if d not in DIALECTS:
            raise SqlGlassError(f"Unknown dialect '{d}'. Supported: {', '.join(DIALECTS)}")
    notes: list[str] = []
    try:
        parts = sqlglot.transpile(sql, read=source, write=to, pretty=True, unsupported_level=sqlglot.ErrorLevel.IGNORE)
    except SqlglotError as ex:
        raise ParseFailure(f"Could not parse the SQL as {source}: {str(ex).splitlines()[0]}") from None
    if any(isinstance(e, exp.Command) for e in parse(sql, source)):
        notes.append("Part of the input was passed through untranslated (procedural or vendor-specific statement).")
    notes.append("Review date/time, string-collation and NULL-ordering semantics; translation is syntactic.")
    return ";\n\n".join(parts) + "\n", notes
