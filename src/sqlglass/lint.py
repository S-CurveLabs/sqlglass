"""Lint rules for T-SQL SELECT queries: correctness traps first, then performance, then style.

With a cached schema the linter also checks that tables and columns exist, which
catches a hallucinated column before the query ever reaches the server.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlglot import exp

from .errors import did_you_mean
from .schema import Schema
from .tsql.analyze import Analysis, ParseFailure, analyze, table_name
from .tsql.lexer import declared_variables, variables

ERROR, WARNING, INFO = "error", "warning", "info"
_ORDER = {ERROR: 0, WARNING: 1, INFO: 2}


@dataclass(frozen=True)
class Finding:
    rule: str
    severity: str
    message: str

    def to_dict(self) -> dict:
        return {"rule": self.rule, "severity": self.severity, "message": self.message}


def lint(sql: str, schema: Schema | None = None, header_params: list[str] | None = None) -> list[Finding]:
    try:
        a = analyze(sql)
    except ParseFailure as ex:
        return [Finding("parse-error", ERROR, str(ex))]
    out: list[Finding] = []
    for stmt in a.statements:
        if isinstance(stmt, exp.Command):
            out.append(Finding("not-analyzed", INFO, f"Statement not analyzed (procedural T-SQL): {stmt.sql(dialect='tsql')[:60]}"))
            continue
        for rule in _RULES:
            out.extend(rule(stmt))
    out.extend(_parameters(sql, header_params))
    if schema is not None:
        out.extend(_against_schema(a, schema))
    unique = list(dict.fromkeys(out))
    return sorted(unique, key=lambda f: _ORDER[f.severity])


# ------------------------------------------------------------------ correctness


def _not_in_subquery(stmt):
    for n in stmt.find_all(exp.Not):
        inner = n.this
        if isinstance(inner, exp.In) and inner.args.get("query") is not None:
            yield Finding("not-in-subquery", WARNING,
                          f"NOT IN (subquery) returns no rows at all if the subquery yields a NULL: {_snip(n)}. Use NOT EXISTS.")


def _join_without_on(stmt):
    for j in stmt.find_all(exp.Join):
        if j.args.get("on") is not None or j.args.get("using") or (j.kind or "").upper() == "CROSS"                 or isinstance(j.this, (exp.Lateral, exp.Unnest)):
            continue
        table = j.this.alias_or_name
        if j.sql(dialect="tsql").lstrip().startswith(","):
            yield Finding("comma-join", WARNING, f"Old-style comma join to '{table}': if its WHERE condition is ever lost this "
                                                 f"becomes a cross join. Use explicit JOIN ... ON.")
        else:
            yield Finding("join-without-on", ERROR, f"JOIN to '{table}' has no ON condition.")


def _top_without_order(stmt):
    for s in stmt.find_all(exp.Select):
        if s.args.get("limit") is not None and s.args.get("order") is None:
            yield Finding("top-without-order-by", WARNING, "TOP without ORDER BY returns an arbitrary set of rows.")


def _left_join_filtered(stmt):
    for s in stmt.find_all(exp.Select):
        where = s.args.get("where")
        if where is None:
            continue
        for j in s.args.get("joins") or []:
            if (j.side or "").upper() != "LEFT":
                continue
            alias = j.this.alias_or_name.lower()
            for col in where.find_all(exp.Column):
                if col.table.lower() != alias:
                    continue
                parent = col.parent
                if isinstance(parent, exp.Is) or col.find_ancestor(exp.Coalesce, exp.Or) is not None \
                        or "ISNULL" in (col.parent.sql(dialect="tsql").upper() if col.parent else ""):
                    continue
                yield Finding("left-join-filtered-in-where", WARNING,
                              f"WHERE filters on '{col.sql(dialect='tsql')}' from LEFT JOINed '{alias}', which silently turns it "
                              f"into an INNER JOIN. Move the condition into the ON clause if unmatched rows should survive.")
                break


def _between_dates(stmt):
    for b in stmt.find_all(exp.Between):
        high = b.args.get("high")
        if isinstance(high, exp.Literal) and high.is_string and len(high.this) == 10 and high.this[4:5] == "-":
            yield Finding("between-date-end", INFO,
                          f"BETWEEN ... '{high.this}' stops at midnight, so a datetime column loses that whole last day. "
                          f"Prefer col >= start AND col < day-after-end.")


# ------------------------------------------------------------------ performance

def _non_sargable(stmt):
    for s in stmt.find_all(exp.Select):
        conditions = [s.args.get("where")] + [j.args.get("on") for j in s.args.get("joins") or []]
        for cond in filter(None, conditions):
            for cmp in cond.find_all(exp.EQ, exp.GT, exp.GTE, exp.LT, exp.LTE, exp.NEQ, exp.Between, exp.In):
                sides = [cmp.this] + ([cmp.expression] if isinstance(cmp, (exp.EQ, exp.GT, exp.GTE, exp.LT, exp.LTE, exp.NEQ)) else [])
                wrapped = [x for x in sides if isinstance(x, (exp.Func, exp.Cast, exp.TryCast)) and not isinstance(x, exp.AggFunc)
                           and x.find(exp.Column) is not None]
                if wrapped and cmp.find_ancestor(exp.Select) is s:
                    yield Finding("non-sargable-predicate", WARNING,
                                  f"'{_snip(cmp)}' wraps a column in a function, so no index on it can be seeked (full scan). "
                                  f"Rewrite as a range on the bare column, e.g. col >= '2026-01-01' AND col < '2027-01-01'.")
            for like in cond.find_all(exp.Like):
                pat = like.expression
                if isinstance(pat, exp.Literal) and pat.is_string and pat.this.startswith(("%", "_")):
                    yield Finding("leading-wildcard-like", INFO, f"LIKE '{pat.this}' starts with a wildcard and cannot use an index seek.")


def _select_star(stmt):
    for s in stmt.find_all(exp.Select):
        if any(isinstance(e, exp.Star) or (isinstance(e, exp.Column) and isinstance(e.this, exp.Star)) for e in s.expressions):
            if isinstance(s.parent, exp.Exists) or s.find_ancestor(exp.Exists) is not None and s.parent_select is None:
                continue
            yield Finding("select-star", WARNING, "SELECT * reads every column (defeats covering indexes) and breaks consumers "
                                                  "when the table changes. List the columns needed.")
            return


def _nolock(stmt):
    for h in stmt.find_all(exp.WithTableHint):
        if any(e.name.upper() in ("NOLOCK", "READUNCOMMITTED") for e in h.expressions):
            yield Finding("nolock", WARNING, "WITH (NOLOCK) can return rows twice, skip rows, or read uncommitted data. "
                                             "Remove it unless approximate numbers are acceptable.")
            return


def _distinct_with_join(stmt):
    for s in stmt.find_all(exp.Select):
        if s.args.get("distinct") is not None and s.args.get("joins"):
            yield Finding("distinct-over-join", INFO, "DISTINCT over a join often hides row duplication from a one-to-many join; "
                                                      "check the join grain, or use EXISTS instead of joining.")
            return


def _union(stmt):
    for u in stmt.find_all(exp.Union):
        if u.args.get("distinct"):
            yield Finding("union-distinct", INFO, "UNION sorts and de-duplicates; use UNION ALL when the branches cannot overlap.")
            return


# ------------------------------------------------------------------ style


def _order_by_ordinal(stmt):
    for o in stmt.find_all(exp.Ordered):
        if isinstance(o.this, exp.Literal) and o.this.is_int:
            yield Finding("order-by-ordinal", INFO, f"ORDER BY {o.this.this} breaks silently when the select list changes; name the column.")
            return


def _unqualified_table(stmt):
    ctes = {c.alias.lower() for c in stmt.find_all(exp.CTE)}
    names = sorted({t.name for t in stmt.find_all(exp.Table)
                    if t.name and not t.db and t.name.lower() not in ctes and not table_name(t).startswith(("#", "@"))
                    and not isinstance(t.this, exp.Func)})
    if names:
        yield Finding("missing-schema-prefix", INFO, f"No schema prefix on: {', '.join(names)}. Write dbo.{names[0]} (name resolution "
                                                     f"is per-user otherwise, and plans are not shared).")


def _unused_cte(stmt):
    for cte in stmt.find_all(exp.CTE):
        name = cte.alias.lower()
        used = any(t.name.lower() == name and not t.db for t in stmt.find_all(exp.Table))
        if not used:
            yield Finding("unused-cte", WARNING, f"CTE '{cte.alias}' is defined but never referenced.")


def _ambiguous_columns(stmt):
    for s in stmt.find_all(exp.Select):
        if len(s.args.get("joins") or []) >= 1:
            aliases = {e.alias.lower() for e in s.expressions if e.alias}  # ORDER BY may name a select alias
            bare = sorted({c.name for c in s.find_all(exp.Column) if not c.table and c.name and c.name.lower() not in aliases
                           and c.find_ancestor(exp.Select) is s})
            if bare:
                yield Finding("unqualified-column", INFO, f"Columns without a table alias in a multi-table query: {', '.join(bare[:8])}. "
                                                          f"Qualify them so the query survives a column being added to another table.")


_RULES = [_join_without_on, _not_in_subquery, _left_join_filtered, _top_without_order, _non_sargable, _select_star, _nolock,
          _unused_cte, _distinct_with_join, _union, _between_dates, _order_by_ordinal, _unqualified_table, _ambiguous_columns]


# ------------------------------------------------------------------ whole-text rules


def _parameters(sql: str, header_params: list[str] | None):
    used = {v.lower(): v for v in variables(sql)}
    declared = {v.lower() for v in declared_variables(sql)}
    if header_params is None:
        return
    header = {p.lower(): p for p in header_params}
    for low, name in used.items():
        if low not in declared and low not in header:
            yield Finding("undeclared-parameter", ERROR, f"{name} is used but is neither DECLAREd nor listed as '-- param:' in the header.")
    for low, name in header.items():
        if low not in used:
            yield Finding("unused-parameter", WARNING, f"Header param {name} is never used in the query.")
        if low in declared:
            yield Finding("parameter-declared-twice", ERROR, f"{name} is a header param and is also DECLAREd in the body; running it "
                                                             f"would fail. Remove the DECLARE.")


def _against_schema(a: Analysis, schema: Schema):
    resolved = {}
    for ref in a.tables:
        if ref.is_temp or (ref.catalog and schema.database and ref.catalog.lower() != schema.database.lower()):
            continue
        table = schema.find(ref.key)
        if table is None:
            yield Finding("unknown-table", ERROR, f"'{ref.full}' is not in the cached schema of '{schema.connection}'."
                                                  f"{did_you_mean(ref.name, [t.name for t in schema.tables.values()])}")
        else:
            resolved[ref.key] = table
    for key, cols in a.columns.items():
        table = resolved.get(key)
        if table is None:
            continue
        for col in cols:
            if table.column(col) is None:
                yield Finding("unknown-column", ERROR, f"{table.full} has no column '{col}'."
                                                       f"{did_you_mean(col, [c.name for c in table.columns])}")


def _snip(node: exp.Expression, width: int = 70) -> str:
    text = " ".join(node.sql(dialect="tsql").split())
    return text if len(text) <= width else text[:width] + "..."
