"""Guided SELECT builder: the caller names tables and columns, the builder supplies what is
easy to get wrong by hand -- validated names, join conditions from the declared foreign keys,
consistent aliases, GROUP BY that matches the select list."""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from .errors import SqlGlassError, did_you_mean
from .schema import Schema, Table
from .tsql.lexer import PUNCT, WORD, significant, tokenize

_SIMPLE_REF = re.compile(r"(\[[^\]]+\]|\w+)(\.(\[[^\]]+\]|\w+)){0,2}")
AGGREGATES = ("SUM", "COUNT", "COUNT_DISTINCT", "AVG", "MIN", "MAX")


class BuildError(SqlGlassError):
    pass


@dataclass
class Built:
    sql: str
    notes: list[str] = field(default_factory=list)


class _Scope:
    def __init__(self, schema: Schema):
        self.schema = schema
        self.tsql = schema.dialect == "tsql"
        self.tables: list[Table] = []
        self.alias: dict[str, str] = {}  # table key -> alias

    def q(self, name: str) -> str:
        return "[" + name.replace("]", "]]") + "]" if self.tsql else '"' + name.replace('"', '""') + '"'

    def table_sql(self, t: Table) -> str:
        name = f"{self.q(t.schema)}.{self.q(t.name)}" if self.tsql else self.q(t.name)
        return f"{name} AS {self.alias[t.key]}"

    def add(self, t: Table) -> None:
        if t.key in self.alias:
            return
        base = "".join(ch for ch in t.name if ch.isupper()).lower() or t.name[:1].lower()
        base = re.sub(r"[^a-z]", "", base)[:4] or "t"
        alias, n = base, 1
        while alias in self.alias.values() or alias.upper() in _RESERVED:
            n += 1
            alias = f"{base}{n}"
        self.alias[t.key] = alias
        self.tables.append(t)

    def column(self, ref: str) -> str:
        """'Table.Column' or 'Column' -> alias.[Column], validated against the schema."""
        parts = [p.strip('[]"') for p in ref.strip().split(".")]
        col_name = parts[-1]
        if len(parts) >= 2:
            qual = ".".join(parts[:-1]).lower()  # a table already in the query wins over the schema-wide lookup
            t = next((x for x in self.tables if x.name.lower() == qual or x.full.lower() == qual), None)                 or self.schema.require(".".join(parts[:-1]))
            if t.key not in self.alias:
                raise BuildError(f"'{ref}': table {t.full} is not part of this query; add it to 'tables'.")
            owners = [t]
        else:
            owners = [t for t in self.tables if t.column(col_name)]
        if not owners:
            every = [c.name for t in self.tables for c in t.columns]
            raise BuildError(f"No column '{col_name}' in {', '.join(t.full for t in self.tables)}.{did_you_mean(col_name, every)}")
        if len(owners) > 1:
            raise BuildError(f"Column '{col_name}' exists in {', '.join(t.full for t in owners)}; write it as Table.{col_name}.")
        col = owners[0].column(col_name)
        if col is None:
            raise BuildError(f"{owners[0].full} has no column '{col_name}'.{did_you_mean(col_name, [c.name for c in owners[0].columns])}")
        return f"{self.alias[owners[0].key]}.{self.q(col.name)}"

    def expression(self, text: str) -> str:
        """Rewrite Table.Column references inside a free-form predicate/expression to alias.[Column]."""
        toks = significant(tokenize(text))
        names = {t.name.lower(): t for t in self.tables}
        edits = []
        i = 0
        while i < len(toks):
            t = toks[i]
            if t.ident is not None and t.ident.lower() in names and i + 2 < len(toks) and toks[i + 1].text == "." \
                    and toks[i + 2].ident is not None and not (i and toks[i - 1].text == "."):
                edits.append((t.start, toks[i + 2].end, self.column(f"{t.ident}.{toks[i + 2].ident}")))
                i += 3
                continue
            nxt = toks[i + 1] if i + 1 < len(toks) else None
            if t.kind == WORD and not (i and toks[i - 1].text == ".") and not (nxt and nxt.kind == PUNCT and nxt.text in "(.") \
                    and sum(1 for tb in self.tables if tb.column(t.text)) == 1 and t.upper not in _RESERVED:
                edits.append((t.start, t.end, self.column(t.text)))
            i += 1
        for start, end, new in sorted(edits, reverse=True):
            text = text[:start] + new + text[end:]
        return text.strip()


_RESERVED = {"AS", "ON", "IN", "IS", "OR", "BY", "TO", "AND", "NOT", "NULL", "LIKE", "BETWEEN", "CASE", "WHEN", "THEN", "ELSE", "END",
             "ASC", "DESC", "EXISTS", "SELECT", "FROM", "WHERE", "ALL", "ANY", "TOP", "SET", "KEY", "GO", "IF"}


def build_select(schema: Schema, tables: list[str], columns: list[str] | None = None, aggregates: list[dict] | None = None,
                 filters: list[str] | None = None, order_by: list[str] | None = None, top: int | None = None,
                 distinct: bool = False, join_type: str = "INNER") -> Built:
    if not tables:
        raise BuildError("Name at least one table.")
    join_type = join_type.upper()
    if join_type not in ("INNER", "LEFT"):
        raise BuildError("join_type must be INNER or LEFT.")
    scope, notes = _Scope(schema), []
    wanted = [schema.require(t) for t in tables]
    scope.add(wanted[0])
    joins: list[str] = []
    for target in wanted[1:]:
        if target.key in scope.alias:
            continue
        path = min(filter(lambda p: p is not None, (schema.join_path(t.key, target.key) for t in scope.tables)), key=len, default=None)
        if path is None:
            anchor = next((t for t in scope.tables if schema.name_based_join(t, target)), None)
            if anchor is None:
                raise BuildError(f"No foreign-key path links {target.full} to {', '.join(t.full for t in scope.tables)}, and no "
                                 f"shared key columns were found. Use describe_table on both to find the join columns, then write "
                                 f"the join by hand.")
            scope.add(target)
            pairs = schema.name_based_join(anchor, target)
            cond = " AND ".join(f"{scope.alias[target.key]}.{scope.q(b)} = {scope.alias[anchor.key]}.{scope.q(a)}" for a, b in pairs)
            joins.append(f"{join_type} JOIN {scope.table_sql(target)} ON {cond}")
            notes.append(f"No foreign key is declared between {anchor.full} and {target.full}; joined on matching key column "
                         f"name(s) {', '.join(a for a, _ in pairs)}. VERIFY this is the right relationship.")
            continue
        for fk, forward in path:
            here, there = (fk.from_table, fk.to_table) if forward else (fk.to_table, fk.from_table)
            new = schema.tables[there]
            if new.key in scope.alias:
                continue
            scope.add(new)
            if new.key != target.key:
                notes.append(f"{new.full} was added as a bridge to reach {target.full}.")
            here_cols, there_cols = (fk.from_columns, fk.to_columns) if forward else (fk.to_columns, fk.from_columns)
            cond = " AND ".join(f"{scope.alias[there]}.{scope.q(tc)} = {scope.alias[here]}.{scope.q(hc)}"
                                for hc, tc in zip(here_cols, there_cols))
            joins.append(f"{join_type} JOIN {scope.table_sql(new)} ON {cond}")
            if not forward:
                notes.append(f"{new.full} is on the many side of {schema.tables[here].full}: rows of {schema.tables[here].full} "
                             f"repeat once per match, so SUM/COUNT over its columns will be inflated unless aggregated first.")

    plain = [scope.column(c) if _SIMPLE_REF.fullmatch(c.strip()) else scope.expression(c) for c in columns or []]
    agg_sql = []
    for a in aggregates or []:
        fn = str(a.get("fn", "")).upper()
        if fn not in AGGREGATES:
            raise BuildError(f"Aggregate fn must be one of {', '.join(AGGREGATES)} (got '{a.get('fn')}').")
        target = "*" if a.get("column") in (None, "", "*") else scope.column(a["column"])
        if target == "*" and fn != "COUNT":
            raise BuildError(f"{fn} needs a column.")
        call = f"COUNT(DISTINCT {target})" if fn == "COUNT_DISTINCT" else f"{fn}({target})"
        alias = a.get("alias") or (f"{fn.title().replace('_', '')}{'' if target == '*' else re.sub(r'[^A-Za-z0-9]', '', a['column'].split('.')[-1])}")
        agg_sql.append(f"{call} AS {scope.q(alias)}")
    select_list = plain + agg_sql
    if not select_list:
        select_list = [f"{scope.alias[t.key]}.{scope.q(c.name)}" for t in scope.tables for c in t.columns]
        notes.append("No columns were named, so every column is listed; trim the list to what is needed.")

    head = "SELECT" + (" DISTINCT" if distinct else "") + (f" TOP ({int(top)})" if top and scope.tsql else "")
    lines = [head, "    " + ",\n    ".join(select_list), f"FROM {scope.table_sql(scope.tables[0])}", *joins]
    if filters:
        lines.append("WHERE " + "\n  AND ".join(scope.expression(f) for f in filters))
    if agg_sql and plain:
        lines.append("GROUP BY " + ", ".join(plain))
    if order_by:
        parts = []
        for o in order_by:
            m = re.fullmatch(r"(.*?)(\s+(?:ASC|DESC))?", o.strip(), re.I)
            ref = m.group(1).strip()
            alias_hit = next((a.get("alias") for a in aggregates or [] if a.get("alias", "").lower() == ref.strip('[]"').lower()), None)
            parts.append((scope.q(alias_hit) if alias_hit else scope.expression(ref)) + (m.group(2) or "").upper())
        lines.append("ORDER BY " + ", ".join(parts))
    elif top:
        notes.append("TOP/LIMIT without order_by returns arbitrary rows; add order_by.")
    if top and not scope.tsql:
        lines.append(f"LIMIT {int(top)}")
    return Built("\n".join(lines) + "\n", list(dict.fromkeys(notes)))
