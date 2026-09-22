"""Token-aware edits: they never touch strings or comments, and leave all other formatting alone."""

from __future__ import annotations

import difflib
import re

from .errors import SqlGlassError
from .tsql.analyze import ParseFailure, analyze
from .tsql.lexer import NUMBER, PUNCT, STRING, Token, quote_ident, significant, tokenize


class RefactorError(SqlGlassError):
    pass


def unified_diff(before: str, after: str, name: str) -> str:
    return "".join(difflib.unified_diff(before.splitlines(True), after.splitlines(True), f"a/{name}", f"b/{name}", n=2))


def _splice(sql: str, edits: list[tuple[int, int, str]]) -> str:
    for start, end, text in sorted(edits, reverse=True):
        sql = sql[:start] + text + sql[end:]
    return sql


def _parts(name: str) -> list[str]:
    return [p.strip('[]"') for p in re.split(r"\.(?![^\[]*\])", name.strip())]


def _fmt(name: str, like: Token) -> str:
    plain = re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name) is not None
    return name if plain and like.kind == "word" else quote_ident(name)


def _is(tok: Token | None, text: str) -> bool:
    return tok is not None and tok.kind == PUNCT and tok.text == text


def rename_table(sql: str, old: str, new: str) -> tuple[str, int]:
    """Rename references to a table/view. 'old' is schema.name; bare references to name are renamed too when the
    query really uses it as a table (so a column or alias that happens to share the name is left alone)."""
    old_parts, new_parts = _parts(old), _parts(new)
    if len(old_parts) != 2 or len(new_parts) != 2:
        raise RefactorError("Give both names as schema.table, e.g. old='dbo.Vendor', new='dbo.Supplier'.")
    try:
        bare_is_table = any(t.name.lower() == old_parts[1].lower() and not t.schema for t in analyze(sql).tables)
    except ParseFailure:
        bare_is_table = False
    toks = significant(tokenize(sql))
    edits: list[tuple[int, int, str]] = []
    i = 0
    while i < len(toks):
        t = toks[i]
        prev = toks[i - 1] if i else None
        nxt = toks[i + 1] if i + 1 < len(toks) else None
        if t.ident is not None and not _is(prev, "."):
            if t.ident.lower() == old_parts[0].lower() and _is(nxt, ".") and i + 2 < len(toks) \
                    and (toks[i + 2].ident or "").lower() == old_parts[1].lower():
                name = toks[i + 2]
                edits.append((t.start, name.end, f"{_fmt(new_parts[0], t)}.{_fmt(new_parts[1], name)}"))
                i += 3
                continue
            if bare_is_table and t.ident.lower() == old_parts[1].lower() and not _is(nxt, ".") and not _is(nxt, "(") \
                    and prev is not None and prev.upper in ("FROM", "JOIN", "APPLY", ","):
                edits.append((t.start, t.end, _fmt(new_parts[1], t)))
        i += 1
    return _splice(sql, edits), len(edits)


def rename_column(sql: str, table: str, old: str, new: str) -> tuple[str, int]:
    """Rename a column of one table: alias-qualified references always, bare references only when
    that table is the only one in the query."""
    table_parts = _parts(table)
    try:
        a = analyze(sql)
    except ParseFailure as ex:
        raise RefactorError(f"Cannot rename safely in a query that does not parse: {ex}") from None
    refs = [t for t in a.tables if t.name.lower() == table_parts[-1].lower()
            and (len(table_parts) < 2 or not t.schema or t.schema.lower() == table_parts[-2].lower())]
    if not refs:
        return sql, 0
    qualifiers = {(r.alias or r.name).lower() for r in refs} | {r.name.lower() for r in refs if not r.alias}
    only_table = len(a.tables) == 1 and not a.ctes
    toks = significant(tokenize(sql))
    edits = []
    for i, t in enumerate(toks):
        if t.ident is None or t.ident.lower() != old.lower():
            continue
        prev = toks[i - 1] if i else None
        nxt = toks[i + 1] if i + 1 < len(toks) else None
        if _is(nxt, "(") or _is(nxt, "."):
            continue
        if _is(prev, "."):
            if i >= 2 and (toks[i - 2].ident or "").lower() in qualifiers:
                edits.append((t.start, t.end, _fmt(new, t)))
        elif only_table and not (prev is not None and prev.upper == "AS"):
            edits.append((t.start, t.end, _fmt(new, t)))
    return _splice(sql, edits), len(edits)


def extract_parameter(sql: str, literal_text: str, param: str) -> tuple[str, int, str, str]:
    """Replace every occurrence of one literal with @param. Returns (sql, count, inferred type, default as written)."""
    if not re.fullmatch(r"@[A-Za-z_][A-Za-z0-9_]*", param):
        raise RefactorError(f"'{param}' is not a valid parameter name (expected @Name).")
    wanted = literal_text.strip()
    bare = wanted[wanted.index("'") + 1:-1] if wanted.endswith("'") and "'" in wanted[:2] else wanted
    hits = []
    for t in tokenize(sql):
        if t.kind == STRING and t.text[t.text.index("'") + 1:-1].replace("''", "'") == bare:
            hits.append(t)
        elif t.kind == NUMBER and t.text == wanted:
            hits.append(t)
    if not hits:
        raise RefactorError(f"The literal {literal_text} does not occur in the query (strings and numbers only; "
                            f"text inside comments is ignored).")
    if any(v.lower() == param.lower() for v in (t.text for t in tokenize(sql) if t.kind == "variable")):
        raise RefactorError(f"{param} is already used in this query; pick another name.")
    first = hits[0]
    if first.kind == NUMBER:
        sql_type = "decimal(18,4)" if "." in first.text else "int"
    elif re.fullmatch(r"\d{4}-\d{2}-\d{2}", bare) or re.fullmatch(r"\d{8}", bare):
        sql_type = "date"
    elif re.fullmatch(r"\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}(:\d{2}(\.\d+)?)?", bare):
        sql_type = "datetime2"
    else:
        sql_type = f"nvarchar({max(50, len(bare))})" if first.text[:1] in "Nn" else f"varchar({max(50, len(bare))})"
    return _splice(sql, [(t.start, t.end, param) for t in hits]), len(hits), sql_type, first.text
