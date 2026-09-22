"""Turn a write statement into the SELECT that shows what it would do.

    UPDATE -> the rows it would touch, each SET column as a current / new pair
    DELETE -> the rows it would remove
    INSERT -> the rows it would add, under the target's column names

The write itself is never sent anywhere. What comes back is ordinary read-only SQL,
and it must pass the same guard as everything else before it is returned.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from sqlglot import exp

from .errors import SqlGlassError
from .schema import Schema, Table
from .tsql.analyze import parse, table_name
from .tsql.guard import check_read_only
from .tsql.lexer import quote_ident as q

CAVEATS = ("This shows the statement's own effect on the data as it is right now. Triggers, cascading foreign keys, "
           "constraint violations, defaults and identity values are not reflected.")


class PreviewError(SqlGlassError):
    pass


@dataclass
class Preview:
    kind: str
    target: str
    sql: str
    count_sql: str
    notes: list[str] = field(default_factory=list)


def preview_write(sql: str, schema: Schema | None = None, changed_only: bool = False) -> Preview:
    statements = [s for s in parse(sql) if not isinstance(s, exp.Declare)]
    if len(statements) != 1:
        raise PreviewError(f"Give exactly one UPDATE, DELETE or INSERT statement (found {len(statements)}).")
    stmt = statements[0]
    if isinstance(stmt, exp.Merge):
        raise PreviewError("MERGE cannot be previewed as one SELECT. Preview its branches separately: the WHEN MATCHED part as an "
                           "UPDATE ... FROM target JOIN source, the WHEN NOT MATCHED part as an INSERT ... SELECT ... WHERE NOT EXISTS.")
    if isinstance(stmt, exp.Query):
        raise PreviewError("This is already a SELECT; run it with run_query.")
    build = {exp.Update: _update, exp.Delete: _delete, exp.Insert: _insert}.get(type(stmt))
    if build is None:
        raise PreviewError(f"Only UPDATE, DELETE and INSERT can be previewed (got {type(stmt).__name__.upper()}).")
    out = build(stmt, schema, changed_only) if build is _update else build(stmt, schema)
    if stmt.args.get("limit") is not None:
        out.notes.append("The statement uses TOP; the preview shows every qualifying row, of which the write would pick an arbitrary subset.")
    if stmt.args.get("returning") is not None or stmt.find(exp.Into) is not None:
        out.notes.append("An OUTPUT clause was ignored.")
    out.notes.append(CAVEATS)
    for text in (out.sql, out.count_sql):
        check_read_only(text)
    return out


# ------------------------------------------------------------------ helpers


def _with(stmt: exp.Expression) -> str:
    w = stmt.args.get("with_") or stmt.args.get("with")
    return w.sql(dialect="tsql") + "\n" if w is not None else ""


def _s(node: exp.Expression) -> str:
    return node.sql(dialect="tsql")


def _bare(t: exp.Table) -> str:
    c = t.copy()
    c.set("alias", None)
    c.set("joins", None)
    return _s(c)


def _where(stmt: exp.Expression, extra: str = "") -> str:
    w = stmt.args.get("where")
    conds = ([f"({_s(w.this)})"] if w is not None and extra else [_s(w.this)] if w is not None else []) + ([extra] if extra else [])
    return "\nWHERE " + "\n  AND ".join(conds) if conds else ""


def _lookup(schema: Schema | None, t: exp.Table) -> Table | None:
    if schema is None:
        return None
    return schema.find(".".join(p for p in (t.db, table_name(t)) if p))


def _keys(table: Table | None, qualifier: str, notes: list[str]) -> list[str]:
    if table is None:
        notes.append("No cached schema for the target, so its key columns are not included; call describe_table once and retry "
                     "to get the primary key in the preview.")
        return []
    if not table.primary_key:
        notes.append(f"{table.full} has no primary key; rows in the preview cannot be identified uniquely.")
    return [f"{qualifier}.{q(c)}" for c in table.primary_key]


def _target(stmt: exp.Expression, this: exp.Table, alias_name: str | None) -> tuple[str, str, exp.Table]:
    """(FROM clause, qualifier for the target's columns, the real target table)."""
    source = stmt.args.get("from_") or stmt.args.get("from")
    if source is not None:  # UPDATE h SET ... FROM dbo.PoHeader AS h JOIN ...
        wanted = (alias_name or this.alias_or_name).lower()
        real = next((t for t in source.find_all(exp.Table) if t.alias_or_name.lower() == wanted), None)
        joins = "".join("\n" + _s(j) for j in stmt.args.get("joins") or [])
        return _s(source) + joins, q(alias_name or this.alias_or_name), real if real is not None else this
    return "FROM " + _s(this), q(alias_name or this.alias_or_name), this


# ------------------------------------------------------------------ UPDATE


def _update(stmt: exp.Update, schema: Schema | None, changed_only: bool) -> Preview:
    notes: list[str] = []
    from_sql, qual, real = _target(stmt, stmt.this, None)
    table = _lookup(schema, real)
    pairs = []
    for a in stmt.expressions:
        if not isinstance(a, exp.EQ) or not isinstance(a.this, exp.Column):
            raise PreviewError(f"Cannot preview the assignment '{_s(a)}'. Only 'column = expression' is supported "
                               f"(rewrite 'col += x' as 'col = col + x'; variable assignments are not supported).")
        name = a.this.name
        if table is not None and table.column(name) is None:
            raise PreviewError(f"{table.full} has no column '{name}'.")
        pairs.append((name, f"{qual}.{q(name)}", _s(a.expression)))
    set_names = {n.lower() for n, _, _ in pairs}
    select = [k for k in _keys(table, qual, notes) if k.split(".")[-1].strip("[]").lower() not in set_names]
    for name, old, new in pairs:
        select += [f"{old} AS {q(name + ' (current)')}", f"{new} AS {q(name + ' (new)')}"]
    extra = ""
    if changed_only:  # EXCEPT compares NULLs as equal, which plain <> does not
        extra = (f"EXISTS (SELECT {', '.join(o for _, o, _ in pairs)} EXCEPT SELECT {', '.join(n for _, _, n in pairs)})")
        notes.append("changed_only: rows whose new values equal their current values are left out (the UPDATE would still touch them).")
    head = _with(stmt)
    body = f"{from_sql}{_where(stmt, extra)}"
    if stmt.args.get("where") is None:
        notes.append("WARNING: the UPDATE has no WHERE clause; it would change every row of the target.")
    return Preview("update", _bare(real), f"{head}SELECT\n    " + ",\n    ".join(select) + f"\n{body}\n",
                   f"{head}SELECT COUNT(*) AS affected_rows\n{body}\n", notes)


# ------------------------------------------------------------------ DELETE


def _delete(stmt: exp.Delete, schema: Schema | None) -> Preview:
    notes: list[str] = []
    aliases = stmt.args.get("tables") or []
    this = stmt.this
    if aliases:  # DELETE l FROM dbo.PoLine AS l JOIN ...
        wanted = aliases[0].name.lower()
        real = next((t for t in this.find_all(exp.Table) if t.alias_or_name.lower() == wanted), this)
        from_sql, qual = "FROM " + _s(this), q(aliases[0].name)
    else:
        from_sql, qual, real = _target(stmt, this, None)
    table = _lookup(schema, real)
    columns = [f"{qual}.{q(c.name)}" for c in table.columns] if table is not None else [f"{qual}.*"]
    head = _with(stmt)
    body = f"{from_sql}{_where(stmt)}"
    if stmt.args.get("where") is None:
        notes.append("WARNING: the DELETE has no WHERE clause; it would remove every row of the target.")
    if table is not None and schema is not None:
        children = sorted({schema.tables[fk.from_table].full for fk in schema.foreign_keys
                           if fk.to_table == table.key and fk.from_table in schema.tables})
        if children:
            notes.append(f"Rows in {', '.join(children)} reference {table.full}; the DELETE fails (or cascades) where such rows exist.")
    return Preview("delete", _bare(real), f"{head}SELECT\n    " + ",\n    ".join(columns) + f"\n{body}\n",
                   f"{head}SELECT COUNT(*) AS affected_rows\n{body}\n", notes)


# ------------------------------------------------------------------ INSERT


def _insert(stmt: exp.Insert, schema: Schema | None) -> Preview:
    notes: list[str] = []
    target = stmt.this.this if isinstance(stmt.this, exp.Schema) else stmt.this
    names = [c.name for c in stmt.this.expressions] if isinstance(stmt.this, exp.Schema) else []
    table = _lookup(schema, target)
    if not names:
        if table is None:
            raise PreviewError("The INSERT has no column list and the target is not in the cached schema, so the new rows' columns "
                               "cannot be named. Add a column list: INSERT INTO t (a, b) ...")
        names = [c.name for c in table.columns if not c.identity and not c.computed]
        notes.append("The INSERT has no column list; the table's insertable columns were assumed, in table order.")
    if table is not None:
        unknown = [n for n in names if table.column(n) is None]
        if unknown:
            raise PreviewError(f"{table.full} has no column {', '.join(unknown)}.")
        missing = [c.name for c in table.columns if not c.nullable and not c.identity and not c.computed
                   and c.name.lower() not in {n.lower() for n in names}]
        if missing:
            notes.append(f"NOT NULL column(s) {', '.join(missing)} are not supplied; the INSERT fails unless they have defaults.")
    source = stmt.expression
    if source is None:
        raise PreviewError("INSERT ... DEFAULT VALUES / EXEC cannot be previewed.")
    source = source.copy()
    inner_with = source.args.get("with_") or source.args.get("with")
    head = _with(stmt)
    if inner_with is not None:  # a CTE cannot live inside a derived table in T-SQL: hoist it
        head += _s(inner_with) + "\n"
        source.set("with_", None)
        source.set("with", None)
    if isinstance(source, exp.Query) and source.args.get("order") is not None and source.args.get("limit") is None:
        source.set("order", None)
        notes.append("ORDER BY was dropped from the source SELECT (not allowed in a derived table, and it does not affect which rows are inserted).")
    inner = _s(source)
    derived = f"FROM (\n{inner}\n) AS new_rows ({', '.join(q(n) for n in names)})"
    return Preview("insert", _bare(target), f"{head}SELECT new_rows.*\n{derived}\n", f"{head}SELECT COUNT(*) AS affected_rows\n{derived}\n", notes)
