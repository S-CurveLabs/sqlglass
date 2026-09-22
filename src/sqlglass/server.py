"""sqlglass: the MCP tool surface.

Tools are plain functions (importable and unit-testable); the decorator registers
them without wrapping.
"""

from __future__ import annotations

from typing import Any

from mcp.types import ToolAnnotations

try:  # mcp 2.x
    from mcp.server.mcpserver import MCPServer
except ImportError:  # mcp 1.x, where the same class is called FastMCP
    from mcp.server.fastmcp import FastMCP as MCPServer

from . import __version__
from . import config as cfg
from . import ddl
from . import plan as showplan
from . import refactor as rf
from . import schema as sc
from . import snapshots
from .builder import build_select as _build_select
from .engines import Param, open_engine
from .errors import SqlGlassError, did_you_mean
from .library import Library, ParamDef, Query, parse_text, slug
from .lint import lint
from .preview import preview_write as _preview_write
from .tsql.analyze import ParseFailure, analyze, format_sql as _format_sql, translate
from .tsql.guard import check_read_only
from .tsql.lexer import declared_variables, quote_ident, variables

INSTRUCTIONS = """\
Tools for building, checking, running (read-only) and managing SQL queries for SQL Server / Azure SQL.

Building a query: search_schema / list_tables -> describe_table (real column names, types, keys, row counts) ->
build_select (joins inferred from foreign keys) or hand-written T-SQL -> lint_sql (pass the connection so unknown
tables/columns are caught) -> explain_query (estimated plan, nothing executes) -> run_query.
Never guess table or column names: look them up first. The schema is cached; refresh_schema re-reads it.

Writes: this server never modifies data. For an UPDATE / DELETE / INSERT use preview_write: it returns (and can run)
the equivalent read-only SELECT showing the affected rows with current and new values; the user applies the write themselves.

New objects: build_create_table / build_procedure / build_view return DDL script text (validated against the schema)
for the user to run; save it with save_query(kind='script') if it should be kept. They never execute anything.

Execution is strictly read-only: only SELECT / WITH (plus DECLARE / SET @var) is accepted, everything runs in a
rolled-back transaction, results are capped (max_rows) and time-limited. Aggregate in SQL instead of pulling rows.
Pass values as params ({"@Start": "2026-01-01"}), never by pasting them into the SQL text.

The query library is a folder of .sql files with a '-- name/description/connection/tags/param' header.
list_queries / get_query / find_usage to reuse what exists before writing something new; save_query to keep a
finished query. Every library write takes a snapshot first (restore_snapshot undoes it) and returns a diff;
pass dry_run=true to preview.
"""

try:  # mcp 2.x reports a server version in the handshake
    mcp = MCPServer("sqlglass", instructions=INSTRUCTIONS, version=__version__)
except TypeError:  # mcp 1.x FastMCP has no version argument
    mcp = MCPServer("sqlglass", instructions=INSTRUCTIONS)
READ_ONLY = ToolAnnotations(readOnlyHint=True)
DESTRUCTIVE = ToolAnnotations(destructiveHint=True)


# --------------------------------------------------------------------------- plumbing


def _library() -> Library:
    return Library(cfg.load().library)


def _schema(connection: str = "", refresh: bool = False) -> sc.Schema:
    conn = cfg.load().connection(connection)
    cached = None if refresh else sc.load(conn.name)
    if cached is not None:
        return cached
    engine = open_engine(conn)
    try:
        fresh = engine.introspect()
    finally:
        engine.close()
    sc.save(fresh)
    return fresh


def _cached_schema(connection: str) -> sc.Schema | None:
    """For lint: use the cache when there is one, never open a connection just to lint."""
    try:
        return sc.load(cfg.load().connection(connection).name)
    except SqlGlassError:
        return None


def _resolve(sql: str, query: str, connection: str, params: dict[str, Any] | None):
    """(sql text, connection, params) for an inline statement or a library query."""
    if bool(sql.strip()) == bool(query.strip()):
        raise SqlGlassError("Pass exactly one of 'sql' (inline statement) or 'query' (library query id or name).")
    given = {("@" + k.lstrip("@")).lower(): v for k, v in (params or {}).items()}
    if sql.strip():
        conn = cfg.load().connection(connection)
        written = {v.lower(): v for v in variables(sql)}  # bind under the casing the SQL uses (case-sensitive collations)
        unused = [k for k in given if k not in written]
        if unused:
            raise SqlGlassError(f"Parameter {', '.join(unused)} is not used in the SQL. It uses: {', '.join(written.values()) or '(none)'}")
        clash = [written[k] for k in given if k in {d.lower() for d in declared_variables(sql)}]
        if clash:
            raise SqlGlassError(f"{', '.join(clash)} is DECLAREd inside the SQL and also passed in params; the server would declare it "
                              f"twice. Remove the DECLARE and keep the param, or drop it from params.")
        return sql, conn, [Param(written[k], "", v) for k, v in given.items()]
    q = _library().get(query)
    if q.kind == "script":
        raise SqlGlassError(f"'{q.id}' is a DDL script kept for a person to review and run; this server never executes scripts.")
    conn = cfg.load().connection(connection or q.connection)
    known = {p.name.lower() for p in q.params}
    extra = [k for k in given if k not in known]
    if extra:
        raise SqlGlassError(f"Query '{q.id}' has no parameter {', '.join(extra)}. Its params: "
                          f"{', '.join(p.render() for p in q.params) or '(none)'}")
    bound, missing = [], []
    written = {v.lower(): v for v in variables(q.body)}
    for p in q.params:
        name = written.get(p.name.lower(), p.name)
        if p.name.lower() in given:
            bound.append(Param(name, p.type, given[p.name.lower()]))
        elif p.has_default:
            bound.append(Param(name, p.type, p.default_value))
        else:
            missing.append(p.render())
    if missing:
        raise SqlGlassError(f"Query '{q.id}' needs a value for: {'; '.join(missing)}. Pass them in 'params'.")
    return q.body, conn, bound


def _apply(lib: Library, label: str, changes: dict[str, str | None], dry_run: bool) -> dict:
    diffs, real = {}, {}
    for qid, after in changes.items():
        before = lib.text(qid)
        if before == after:
            continue
        real[qid] = after
        diffs[qid] = rf.unified_diff(before or "", after or "", qid + ".sql") or "(whitespace only)"
    out: dict[str, Any] = {"library": str(lib.root), "changed": sorted(real), "diff": diffs}
    if not real:
        out["status"] = "nothing to change"
    elif dry_run:
        out["status"] = "preview only (dry_run); nothing was written"
    else:
        out["snapshot"] = snapshots.take(lib, list(real), label)
        for qid, after in real.items():
            lib.write(qid, after)
        out["status"] = "applied"
    return out


def _findings(sql: str, connection: str, header_params: list[str] | None = None) -> list[dict]:
    return [f.to_dict() for f in lint(sql, _cached_schema(connection), header_params)]


# --------------------------------------------------------------------------- connections & schema


@mcp.tool(annotations=READ_ONLY)
def list_connections() -> dict:
    """List the configured database connections (from sqlglass.toml), which is the default, where the query
    library lives, and whether each connection has a cached schema."""
    c = cfg.load()
    items = []
    for conn in c.connections.values():
        cached = sc.load(conn.name)
        items.append({**conn.describe(), "default": conn.name == c.default_connection,
                      "schema_cached": f"{len(cached.tables)} tables, read {cached.fetched}" if cached else "no (first schema tool call reads it)"})
    out = {"config_file": str(c.path) if c.path else None, "connections": items, "library": str(c.library) if c.library else None}
    if not c.path:
        out["setup"] = (f"No sqlglass.toml found (looked in: {', '.join(str(p) for p in cfg.candidates())}). "
                        f"Copy sqlglass.example.toml to sqlglass.toml in the workspace root and edit it.")
    return out


@mcp.tool()
def refresh_schema(connection: str = "") -> dict:
    """Re-read tables, views, columns, keys, indexes and foreign keys from the database into the local cache.
    Run this when a table or column seems to be missing, or after the database changed."""
    s = _schema(connection, refresh=True)
    return {"connection": s.connection, "database": s.database, "tables": sum(t.kind == "table" for t in s.tables.values()),
            "views": sum(t.kind == "view" for t in s.tables.values()), "foreign_keys": len(s.foreign_keys),
            "cache": str(sc.cache_path(s.connection))}


@mcp.tool(annotations=READ_ONLY)
def list_tables(connection: str = "", schema: str = "", pattern: str = "", limit: int = 200) -> dict:
    """List tables and views with row counts and descriptions. Filter by schema name and/or a name pattern
    ('*invoice*'). For a big database prefer search_schema."""
    s = _schema(connection)
    hits = [t for t in s.tables.values() if (not schema or t.schema.lower() == schema.lower())
            and (not pattern or t.full in s.search(pattern, 10_000)["tables"])]
    hits.sort(key=lambda t: t.full.lower())
    rows = [{k: v for k, v in {"name": t.full, "kind": t.kind, "rows": t.rows, "columns": len(t.columns),
                               "description": t.description}.items() if v not in ("", None)} for t in hits[:limit]]
    return {"connection": s.connection, "database": s.database, "count": len(hits), "tables": rows,
            **({"truncated": f"showing {limit} of {len(hits)}; narrow with schema= or pattern="} if len(hits) > limit else {})}


@mcp.tool(annotations=READ_ONLY)
def describe_table(table: str, connection: str = "") -> dict:
    """Columns (type, nullability, identity/computed), primary key, indexes, foreign keys in both directions and
    row count of one table or view. Always do this before writing SQL against a table."""
    s = _schema(connection)
    t = s.require(table)
    cols = []
    for c in t.columns:
        flags = [f for f, on in (("pk", c.name in t.primary_key), ("identity", c.identity), ("computed", c.computed)) if on]
        cols.append({k: v for k, v in {"name": c.name, "type": c.type + ("" if c.nullable else " NOT NULL"),
                                       "flags": flags, "description": c.description}.items() if v})
    return {"table": t.full, "kind": t.kind, "rows": t.rows, "description": t.description or None, "columns": cols,
            "primary_key": t.primary_key,
            "indexes": [{"name": i.name, "columns": i.columns, **({"included": i.included} if i.included else {}),
                         **({"unique": True} if i.unique else {})} for i in t.indexes if not i.primary_key],
            **s.relationships(t), "schema_read": s.fetched}


@mcp.tool(annotations=READ_ONLY)
def search_schema(pattern: str, connection: str = "") -> dict:
    """Find tables and columns whose name contains the text (or matches a * ? wildcard pattern), plus tables whose
    description mentions it. The way to locate data in an unfamiliar database."""
    s = _schema(connection)
    return {"connection": s.connection, **s.search(pattern)}


@mcp.tool(annotations=READ_ONLY)
def find_join_path(from_table: str, to_table: str, connection: str = "") -> dict:
    """How two tables relate: the shortest chain of declared foreign keys between them, as ready-to-use JOIN lines."""
    s = _schema(connection)
    built = _build_select(s, [from_table, to_table], columns=None)
    joins = [ln for ln in built.sql.splitlines() if ln.startswith(("FROM", "INNER JOIN", "LEFT JOIN"))]
    return {"from_and_joins": "\n".join(joins), "notes": [n for n in built.notes if "No columns were named" not in n]}


# --------------------------------------------------------------------------- build & check


@mcp.tool(annotations=READ_ONLY)
def build_select(tables: list[str], columns: list[str] | None = None, aggregates: list[dict] | None = None,
                 filters: list[str] | None = None, order_by: list[str] | None = None, top: int | None = None,
                 distinct: bool = False, join_type: str = "INNER", connection: str = "") -> dict:
    """Generate a SELECT from the cached schema: names validated, joins inferred from foreign keys (bridge tables
    added automatically), aliases assigned, GROUP BY derived. Returns SQL text only; nothing is executed.

    tables:     ["dbo.PoHeader", "dbo.Vendor"]  (first = FROM; the rest are joined)
    columns:    ["Vendor.Name", "PoHeader.OrderDate"]  (Table.Column, or a bare Column when unambiguous)
    aggregates: [{"fn": "SUM", "column": "PoLine.Amount", "alias": "Total"}]  fn: SUM COUNT COUNT_DISTINCT AVG MIN MAX
    filters:    ["PoHeader.OrderDate >= @Start", "Vendor.Country = 'US'"]  (ANDed; use @params for values)
    order_by:   ["Total DESC"]        top: 50        join_type: INNER | LEFT
    """
    s = _schema(connection)
    built = _build_select(s, tables, columns, aggregates, filters, order_by, top, distinct, join_type)
    return {"sql": built.sql, "notes": built.notes, "lint": [f.to_dict() for f in lint(built.sql, s) if f.severity != "info"]}


@mcp.tool(annotations=READ_ONLY)
def lint_sql(sql: str, connection: str = "") -> dict:
    """Check a query for correctness traps (NOT IN + NULLs, LEFT JOIN turned INNER by WHERE, join without ON,
    TOP without ORDER BY), performance problems (functions on filtered columns, SELECT *, NOLOCK, leading-wildcard LIKE)
    and style. With a connection whose schema is cached, also verifies every table and column exists."""
    schema = _cached_schema(connection)
    findings = [f.to_dict() for f in lint(sql, schema)]
    return {"findings": findings, "checked_against_schema": schema.connection if schema else
            "no (no cached schema; call refresh_schema or describe_table once to enable name checks)",
            "summary": "clean" if not findings else f"{len(findings)} finding(s)"}


@mcp.tool(annotations=READ_ONLY)
def analyze_sql(sql: str) -> dict:
    """What a query touches: tables/views, columns per table, CTEs, parameters and its output columns."""
    a = analyze(sql)
    declared = {v.lower() for v in declared_variables(sql)}
    return {**a.summary(), "parameters": [v for v in variables(sql) if v.lower() not in declared],
            "declared_variables": declared_variables(sql)}


@mcp.tool(annotations=READ_ONLY)
def format_sql(sql: str) -> dict:
    """Pretty-print T-SQL in one canonical style. Normalises cosmetics (adds AS to aliases, rewrites
    '[x] = expr' aliases to 'expr AS [x]'); meaning is unchanged. Comments inside expressions may move."""
    return {"sql": _format_sql(sql)}


@mcp.tool(annotations=READ_ONLY)
def translate_sql(sql: str, to_dialect: str, from_dialect: str = "tsql") -> dict:
    """Translate a query between dialects (tsql, postgres, mysql, sqlite, snowflake, bigquery, databricks, oracle,
    duckdb, redshift): TOP<->LIMIT, ISNULL/COALESCE, GETDATE, DATEADD, string functions, quoting."""
    out, notes = translate(sql, to_dialect.lower(), from_dialect.lower())
    return {"sql": out, "notes": notes}


# --------------------------------------------------------------------------- DDL scripts (text only, never executed)


def _script(s: ddl.Script) -> dict:
    return {"sql": s.sql, "undo_sql": s.undo_sql, "notes": s.notes, "executed": False}


def _body_and_params(sql: str, query: str, params: list[dict] | None, description: str):
    if bool(sql.strip()) == bool(query.strip()):
        raise SqlGlassError("Pass exactly one of 'sql' (the SELECT text) or 'query' (a library query id or name).")
    if sql.strip():
        return sql, params or [], description
    q = _library().get(query)
    from_header = [{"name": p.name, "type": p.type, "default": p.default, "description": p.description} for p in q.params]
    return q.body, params if params is not None else from_header, description or q.description


@mcp.tool(annotations=READ_ONLY)
def build_create_table(name: str, columns: list[dict], primary_key: list[str] | None = None, foreign_keys: list[dict] | None = None,
                       indexes: list[dict] | None = None, description: str = "", connection: str = "") -> dict:
    """Generate a CREATE TABLE script (TEXT ONLY -- this server never runs DDL; the user reviews and runs it).
    Checked against the cached schema: the name must be free, foreign keys must point at a real primary/unique key
    with the same column types, FK columns get an index, constraints get conventional names, and the script is
    wrapped in IF OBJECT_ID(...) IS NULL so it can be re-run. Returns sql + undo_sql + notes.

    name:         "dbo.VendorScore"
    columns:      [{"name": "VendorScoreId", "type": "int", "identity": true},
                   {"name": "VendorId", "type": "int", "nullable": false},
                   {"name": "Score", "type": "decimal(5,2)", "nullable": false, "default": "0", "description": "0-100"}]
    primary_key:  ["VendorScoreId"]
    foreign_keys: [{"columns": ["VendorId"], "references": "dbo.Vendor"}]   (ref_columns default to the parent's primary key)
    indexes:      [{"columns": ["ScoredOn"], "include": ["Score"], "unique": false}]
    """
    return _script(ddl.create_table(_cached_schema(connection), name, columns, primary_key, foreign_keys, indexes, description))


@mcp.tool(annotations=READ_ONLY)
def build_procedure(name: str, sql: str = "", query: str = "", params: list[dict] | None = None, description: str = "") -> dict:
    """Generate a CREATE OR ALTER PROCEDURE script (TEXT ONLY -- never executed here) around a query.
    Give inline 'sql', or a library 'query' -- then its header params (name, type, default) become the procedure's
    parameters automatically. Every @variable in the body must be a typed parameter.
    params: [{"name": "@StartDate", "type": "date", "default": "'2026-01-01'", "description": "first order date"}]
    Returns sql + undo_sql + notes (including an EXEC example)."""
    body, plist, desc = _body_and_params(sql, query, params, description)
    return _script(ddl.create_procedure(name, body, plist, desc))


@mcp.tool(annotations=READ_ONLY)
def build_view(name: str, sql: str = "", query: str = "", description: str = "") -> dict:
    """Generate a CREATE OR ALTER VIEW script (TEXT ONLY -- never executed here) from a SELECT or a library query.
    Refuses what a view cannot contain: parameters, DECLARE, ORDER BY without TOP."""
    body, _, desc = _body_and_params(sql, query, None, description)
    return _script(ddl.create_view(name, body, desc))


# --------------------------------------------------------------------------- execute (read-only)


@mcp.tool(annotations=READ_ONLY)
def run_query(sql: str = "", query: str = "", params: dict[str, Any] | None = None, connection: str = "",
              max_rows: int | None = None) -> dict:
    """Run a read-only query and return the first rows. Give either inline 'sql' or a library 'query' (id or name).
    params: {"@Start": "2026-01-01"}; library queries fall back to their header defaults.
    Anything but SELECT/WITH is refused before reaching the database. Rows are capped at the connection's
    max_rows (lower it with max_rows=); to look at big data, aggregate in SQL."""
    text, conn, bound = _resolve(sql, query, connection, params)
    check_read_only(text)
    cap = max(1, min(max_rows or conn.max_rows, conn.max_rows))
    engine = open_engine(conn)
    try:
        result = engine.run(text, bound, cap)
    finally:
        engine.close()
    return {"connection": conn.name, **result.to_dict(cap)}


@mcp.tool(annotations=READ_ONLY)
def explain_query(sql: str = "", query: str = "", params: dict[str, Any] | None = None, connection: str = "") -> dict:
    """Estimated execution plan, summarised: the expensive operators, scans on big tables, key lookups, sorts,
    implicit conversions, optimizer warnings and missing-index suggestions. The query is compiled, NOT executed,
    so this is safe on heavy queries. (SQL Server login needs the SHOWPLAN permission.)"""
    text, conn, bound = _resolve(sql, query, connection, params)
    check_read_only(text)
    engine = open_engine(conn)
    try:
        raw = engine.plan(text, bound)
    finally:
        engine.close()
    body = showplan.summarize(raw) if engine.dialect == "tsql" else {"plan": raw}
    return {"connection": conn.name, **body, "lint": _findings(text, conn.name)}


@mcp.tool(annotations=READ_ONLY)
def preview_write(sql: str, changed_only: bool = False, run: bool = False, params: dict[str, Any] | None = None,
                  connection: str = "") -> dict:
    """Dry-run a write WITHOUT writing: converts one UPDATE / DELETE / INSERT into the read-only SELECT that shows
    what it would do, plus a COUNT(*) of affected rows. The write statement itself is never sent to the database.
      UPDATE -> key columns + each SET column as '[col (current)]' / '[col (new)]'   (changed_only=true hides no-op rows)
      DELETE -> the rows that would be removed (and which child tables reference them)
      INSERT -> the rows that would be added, under the target's column names
    run=true also executes the preview (read-only, row-capped) and returns the affected-row count and first rows.
    This server cannot apply the write; hand the reviewed statement to the user to run themselves."""
    p = _preview_write(sql, _cached_schema(connection), changed_only)
    out: dict[str, Any] = {"kind": p.kind, "target": p.target, "preview_sql": p.sql, "count_sql": p.count_sql, "notes": p.notes}
    if run:
        def bound_for(text: str) -> dict:  # the COUNT has no SET clause, so it uses fewer params than the row preview
            used = {v.lower() for v in variables(text)}
            return {k: v for k, v in (params or {}).items() if ("@" + k.lstrip("@")).lower() in used}
        out["affected_rows"] = run_query(sql=p.count_sql, params=bound_for(p.count_sql), connection=connection)["rows"][0][0]
        rows = run_query(sql=p.sql, params=bound_for(p.sql), connection=connection)
        out.update({k: rows[k] for k in ("columns", "rows", "row_count", "truncated") if k in rows})
    return out


@mcp.tool(annotations=READ_ONLY)
def sample_table(table: str, rows: int = 10, columns: list[str] | None = None, connection: str = "") -> dict:
    """A few rows of a table or view, to see what the values actually look like."""
    s = _schema(connection)
    t = s.require(table)
    built = _build_select(s, [t.full], columns=[f"{t.name}.{c}" for c in columns] if columns else None, top=max(1, min(rows, 100)))
    out = run_query(sql=built.sql, connection=s.connection)
    return {"table": t.full, **out}


@mcp.tool(annotations=READ_ONLY)
def profile_table(table: str, columns: list[str] | None = None, connection: str = "") -> dict:
    """Per-column row count, NULLs, distinct values, min and max, in one aggregate query. Use it to learn a
    column's grain and range before filtering or joining on it. Up to 15 columns per call."""
    s = _schema(connection)
    t = s.require(table)
    unknown = [c for c in columns or [] if t.column(c) is None]
    if unknown:
        raise SqlGlassError(f"{t.full} has no column {', '.join(unknown)}.{did_you_mean(unknown[0], [c.name for c in t.columns])}")
    chosen = ([t.column(c) for c in columns] if columns else t.columns)[:15]
    tsql = s.dialect == "tsql"
    q = quote_ident if tsql else (lambda n: '"' + n.replace('"', '""') + '"')
    no_minmax = () if not tsql else ("bit", "xml", "text", "ntext", "image", "geography", "geometry", "hierarchyid", "varbinary", "binary", "timestamp", "rowversion", "uniqueidentifier")
    no_distinct = () if not tsql else ("xml", "text", "ntext", "image", "geography", "geometry")
    parts = ["COUNT(*) AS [rows]" if tsql else 'COUNT(*) AS "rows"']
    for i, c in enumerate(chosen):
        col = q(c.name)
        parts.append(f"COUNT({col}) AS n{i}" if not c.type.startswith(no_distinct) else f"SUM(CASE WHEN {col} IS NULL THEN 0 ELSE 1 END) AS n{i}")
        parts.append(f"COUNT(DISTINCT {col}) AS d{i}" if not c.type.startswith(no_distinct) else f"NULL AS d{i}")
        if c.type.startswith(no_minmax):
            parts += [f"NULL AS lo{i}", f"NULL AS hi{i}"]
        else:
            parts += [f"MIN({col}) AS lo{i}", f"MAX({col}) AS hi{i}"]
    source = f"{q(t.schema)}.{q(t.name)}" if tsql else q(t.name)
    res = run_query(sql="SELECT " + ",\n       ".join(parts) + f"\nFROM {source}", connection=s.connection)
    row = res["rows"][0]
    total = row[0]
    profile = []
    for i, c in enumerate(chosen):
        n, d, lo, hi = row[1 + i * 4: 5 + i * 4]
        profile.append({"column": c.name, "type": c.type, "nulls": total - (n or 0), "distinct": d, "min": lo, "max": hi,
                        **({"unique": True} if d is not None and d == total and total else {})})
    return {"table": t.full, "rows": total, "columns": profile, "elapsed_ms": res["elapsed_ms"]}


# --------------------------------------------------------------------------- library


@mcp.tool(annotations=READ_ONLY)
def list_queries(search: str = "", tag: str = "") -> dict:
    """Browse the saved-query library. 'search' matches id, name, description and the SQL text; 'tag' filters by tag."""
    lib = _library()
    needle = search.lower()
    hits = [q for q in lib.all() if (not tag or tag.lower() in (t.lower() for t in q.tags))
            and (not needle or needle in f"{q.id}\n{q.name}\n{q.description}\n{q.body}".lower())]
    return {"library": str(lib.root), "count": len(hits), "queries": [q.card() for q in hits]}


@mcp.tool(annotations=READ_ONLY)
def get_query(query: str) -> dict:
    """One saved query: header fields, parameters, the SQL, what tables it touches, and lint findings."""
    q = _library().get(query)
    out = {**q.card(), "sql": q.body}
    try:
        out["tables"] = [t.full for t in analyze(q.body).tables]
    except ParseFailure:
        pass
    if q.kind != "script":
        out["lint"] = _findings(q.body, q.connection, [p.name for p in q.params])
    return out


@mcp.tool()
def save_query(id: str, sql: str, name: str = "", description: str = "", connection: str = "", tags: list[str] | None = None,
               params: list[dict] | None = None, kind: str = "", overwrite: bool = False, dry_run: bool = False) -> dict:
    """Save a query to the library as <id>.sql (id may contain folders: 'purchasing/open-pos-by-vendor').
    sql is the body only -- do not DECLARE the parameters in it; describe them in params:
      [{"name": "@Start", "type": "date", "default": "'2026-01-01'", "description": "first order date"}]
    Updating an existing query needs overwrite=true; header fields left empty keep their current value.
    kind='script' stores DDL text from build_create_table / build_procedure / build_view (e.g. id 'ddl/usp_open_pos'):
    kept and versioned with the queries, but never linted as a query and never executed."""
    if kind not in ("", "query", "script"):
        raise SqlGlassError("kind must be 'query' (default) or 'script'.")
    lib = _library()
    qid = id.replace("\\", "/").removesuffix(".sql")
    existing = lib.text(qid)
    if existing is not None and not overwrite:
        raise SqlGlassError(f"Query '{qid}' already exists. Pass overwrite=true to replace it (a snapshot is taken), or pick another id.")
    old = parse_text(qid, existing) if existing is not None else Query(qid)
    body = parse_text(qid, sql).body if sql.lstrip().startswith("-- name:") else sql
    q = Query(qid, name or old.name or qid.rsplit("/", 1)[-1], description or old.description, connection or old.connection,
              tags if tags is not None else old.tags, old.owner, ("" if kind == "query" else kind) or old.kind,
              [ParamDef(p["name"] if p["name"].startswith("@") else "@" + p["name"], p.get("type", ""), str(p.get("default", "")),
                        p.get("description", "")) for p in params] if params is not None else old.params, body)
    out = _apply(lib, f"save {qid}", {qid: q.render()}, dry_run)
    if q.kind != "script":
        out["lint"] = _findings(q.body, q.connection, [p.name for p in q.params])
    return out


@mcp.tool(annotations=DESTRUCTIVE)
def delete_query(query: str, dry_run: bool = False) -> dict:
    """Remove a query from the library. Its text is kept in a snapshot, so restore_snapshot brings it back."""
    lib = _library()
    q = lib.get(query)
    return _apply(lib, f"delete {q.id}", {q.id: None}, dry_run)


@mcp.tool(annotations=READ_ONLY)
def find_usage(table: str, column: str = "") -> dict:
    """Impact analysis: which saved queries read a given table/view (and optionally a given column of it).
    Ask this before a table or column is changed, renamed or retired."""
    lib = _library()
    name = table.split(".")[-1].strip('[]"').lower()
    schema_part = table.split(".")[-2].strip('[]"').lower() if "." in table else ""
    hits, unparsed = [], []
    for q in lib.all():
        try:
            a = analyze(q.body)
        except ParseFailure:
            unparsed.append(q.id)
            continue
        refs = [t for t in a.tables if t.name.lower() == name and (not schema_part or not t.schema or t.schema.lower() == schema_part)]
        if not refs:
            continue
        cols = sorted({c for r in refs for c in a.columns.get(r.key, [])})
        if column and column.lower() not in (c.lower() for c in cols):
            if not (column.lower() in (u.lower().split(".")[-1].strip("[]") for u in a.unresolved_columns)):
                continue
        hits.append({"id": q.id, "name": q.name, "columns_used": cols,
                     **({"maybe": f"'{column}' appears unqualified; could belong to another table"} if column and column.lower() not in (c.lower() for c in cols) else {})})
    return {"table": table, "column": column or None, "used_by": hits, "count": len(hits),
            **({"not_analyzed": unparsed} if unparsed else {})}


@mcp.tool(annotations=READ_ONLY)
def lint_library(min_severity: str = "warning") -> dict:
    """Lint every saved query (against each query's cached connection schema when available). Catches queries
    broken by a schema change: run it after refresh_schema."""
    rank = {"error": 0, "warning": 1, "info": 2}
    if min_severity not in rank:
        raise SqlGlassError("min_severity must be error, warning or info.")
    report = {}
    for q in _library().all():
        if q.kind == "script":
            continue
        found = [f for f in _findings(q.body, q.connection, [p.name for p in q.params]) if rank[f["severity"]] <= rank[min_severity]]
        if found:
            report[q.id] = found
    return {"queries_with_findings": len(report), "findings": report}


@mcp.tool()
def rename_in_library(kind: str, old: str, new: str, table: str = "", dry_run: bool = True) -> dict:
    """Follow a database rename through every saved query, token-aware (strings and comments untouched).
    kind='table':  old='dbo.Vendor', new='dbo.Supplier'
    kind='column': table='dbo.Vendor', old='Name', new='VendorName' (alias-qualified references, and bare ones
                   in single-table queries; ambiguous bare references are reported, not changed).
    dry_run defaults to TRUE: review the diff, then call again with dry_run=false."""
    if kind not in ("table", "column") or (kind == "column" and not table):
        raise SqlGlassError("kind must be 'table', or 'column' together with table='schema.name'.")
    lib = _library()
    changes, counts = {}, {}
    for q in lib.all():
        body, n = rf.rename_table(q.body, old, new) if kind == "table" else rf.rename_column(q.body, table, old, new)
        if n:
            q.body = body
            changes[q.id], counts[q.id] = q.render(), n
    out = _apply(lib, f"rename {kind} {old} to {new}", changes, dry_run)
    out["replacements"] = counts
    return out


@mcp.tool()
def extract_parameter(query: str, literal: str, param: str, description: str = "", dry_run: bool = False) -> dict:
    """Turn a hard-coded value in a saved query into a parameter: every occurrence of the literal ('2026-01-01', 100)
    becomes @param, and a '-- param:' header line is added with the old value as its default."""
    lib = _library()
    q = lib.get(query)
    q.body, n, sql_type, default = rf.extract_parameter(q.body, literal, param)
    q.params.append(ParamDef(param, sql_type, default, description))
    out = _apply(lib, f"extract {param} in {q.id}", {q.id: q.render()}, dry_run)
    out["replaced"] = n
    out["param"] = q.params[-1].render()
    return out


@mcp.tool(annotations=READ_ONLY)
def list_snapshots() -> dict:
    """Before-images taken automatically ahead of every library write, newest first."""
    lib = _library()
    return {"library": str(lib.root), "snapshots": snapshots.listing(lib)[:30]}


@mcp.tool(annotations=DESTRUCTIVE)
def restore_snapshot(snapshot_id: str = "latest", dry_run: bool = False) -> dict:
    """Put the queries in a snapshot back to how they were before that write ('latest' = undo the last write).
    The restore is itself snapshotted, so it can be undone too."""
    lib = _library()
    return _apply(lib, f"restore {snapshot_id}", snapshots.load(lib, snapshot_id), dry_run)


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
