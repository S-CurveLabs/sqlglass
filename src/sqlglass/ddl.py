"""DDL script generators: CREATE TABLE, CREATE PROCEDURE, CREATE VIEW.

These produce *text*. Nothing here is ever executed by the server -- the guard would
refuse it anyway -- so the value is in getting the script right before a human runs
it: names validated against the cached schema, foreign keys pointed at real keys with
matching types, parameters typed, conventional constraint names, idempotent wrappers.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field

from .errors import GuardError, SqlGlassError, did_you_mean
from .schema import Schema
from .tsql.guard import check_read_only
from .tsql.lexer import COMMENT, NUMBER, PUNCT, STRING, WORD, declared_variables, quote_ident as q, significant, split_batches, tokenize, variables

_IDENT = re.compile(r"^[A-Za-z_#][A-Za-z0-9_ $#@-]{0,127}$")
_TYPE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(\s*\(\s*(\d+|max)\s*(,\s*\d+\s*)?\))?$", re.I)
_PARAM = re.compile(r"^@[A-Za-z_][A-Za-z0-9_]*$")


class DdlError(SqlGlassError):
    pass


@dataclass
class Script:
    sql: str
    undo_sql: str = ""
    notes: list[str] = field(default_factory=list)


def _split_name(name: str, default_schema: str = "dbo") -> tuple[str, str]:
    parts = [p.strip('[]"') for p in re.split(r"\.(?![^\[]*\])", name.strip())]
    if len(parts) > 2 or not all(_IDENT.match(p) for p in parts):
        raise DdlError(f"'{name}' is not a valid object name. Use schema.Name, e.g. dbo.VendorScore.")
    return (parts[0], parts[1]) if len(parts) == 2 else (default_schema, parts[0])


def _ident(name: str) -> str:
    """Keep generated names inside SQL Server's 128-character identifier limit."""
    if len(name) <= 128:
        return name
    return name[:119] + "_" + hashlib.sha1(name.encode()).hexdigest()[:8]


def _base(sql_type: str) -> str:
    return sql_type.split("(")[0].strip().lower()


def _default_expr(text: str, where: str) -> str:
    """A column default is a free expression, but it must stay one expression."""
    toks = tokenize(text)
    if any(t.kind == COMMENT or (t.kind == PUNCT and t.text == ";") for t in toks):
        raise DdlError(f"{where}: a default must be a single expression (no ';' or comments): {text}")
    depth = 0
    for t in significant(toks):
        depth += (t.text == "(") - (t.text == ")") if t.kind == PUNCT else 0
        if depth < 0:
            raise DdlError(f"{where}: unbalanced parentheses in default: {text}")
    if depth:
        raise DdlError(f"{where}: unbalanced parentheses in default: {text}")
    return text.strip()


def _literal(text: str, where: str) -> str:
    toks = significant(tokenize(text))
    if toks and toks[0].kind == PUNCT and toks[0].text == "-":
        toks = toks[1:]
    ok = len(toks) == 1 and (toks[0].kind in (STRING, NUMBER) or (toks[0].kind == WORD and toks[0].upper == "NULL"))
    if not ok:
        raise DdlError(f"{where}: a parameter default must be one literal ('text', 123 or NULL), got: {text}")
    return text.strip()


# ------------------------------------------------------------------ CREATE TABLE


def create_table(schema: Schema | None, name: str, columns: list[dict], primary_key: list[str] | None = None,
                 foreign_keys: list[dict] | None = None, indexes: list[dict] | None = None, description: str = "") -> Script:
    default_schema = schema.default_schema if schema is not None else "dbo"
    sch, tbl = _split_name(name, default_schema)
    notes: list[str] = []
    if schema is not None and schema.find(f"{sch}.{tbl}") is not None:
        raise DdlError(f"{sch}.{tbl} already exists in the cached schema of '{schema.connection}'. Pick another name, or "
                       f"describe_table it to see what is there.")
    if schema is None:
        notes.append("No cached schema: foreign-key targets and name collisions could not be checked.")
    if not columns:
        raise DdlError("Give at least one column: [{\"name\": \"VendorId\", \"type\": \"int\", \"nullable\": false}]")

    cols: dict[str, dict] = {}
    for c in columns:
        cname, ctype = str(c.get("name", "")).strip('[]"'), str(c.get("type", "")).strip()
        if not _IDENT.match(cname):
            raise DdlError(f"'{cname}' is not a valid column name.")
        if not _TYPE.match(ctype):
            raise DdlError(f"Column {cname}: '{ctype}' is not a valid T-SQL type (e.g. int, nvarchar(100), decimal(18,2), date).")
        if cname.lower() in cols:
            raise DdlError(f"Column {cname} is listed twice.")
        if _base(ctype) in ("text", "ntext", "image"):
            notes.append(f"Column {cname}: {ctype} is deprecated; use {'nvarchar(max)' if _base(ctype) == 'ntext' else 'varchar(max)' if _base(ctype) == 'text' else 'varbinary(max)'}.")
        if _base(ctype) in ("varchar", "nvarchar", "char", "nchar", "varbinary") and "(" not in ctype:
            notes.append(f"Column {cname}: {ctype} without a length means length 1 in a column definition; give it a length.")
        if _base(ctype) == "float" and re.search(r"amount|price|cost|value|total|rate", cname, re.I):
            notes.append(f"Column {cname}: float is approximate; money-like values belong in decimal(p,s).")
        cols[cname.lower()] = {**c, "name": cname, "type": ctype}

    def need(col: str, where: str) -> dict:
        hit = cols.get(col.strip('[]"').lower())
        if hit is None:
            raise DdlError(f"{where}: no column '{col}' in this table.{did_you_mean(col, [c['name'] for c in cols.values()])}")
        return hit

    pk = [need(c, "primary_key") for c in primary_key or []]
    for c in pk:
        if c.get("nullable", True) and "nullable" in c:
            notes.append(f"Primary-key column {c['name']} was made NOT NULL.")
        c["nullable"] = False
    if not pk:
        notes.append("No primary key: the table will be a heap. Most tables should have one.")

    lines = []
    for c in cols.values():
        line = f"{q(c['name'])} {c['type']}"
        if c.get("identity"):
            if _base(c["type"]) not in ("int", "bigint", "smallint", "tinyint", "decimal", "numeric"):
                raise DdlError(f"Column {c['name']}: IDENTITY needs an integer type, not {c['type']}.")
            line += " IDENTITY(1,1)"
            c["nullable"] = False
        line += " NULL" if c.get("nullable", True) else " NOT NULL"
        if c.get("default") not in (None, ""):
            df_name, df_expr = q(_ident("DF_" + tbl + "_" + c["name"])), _default_expr(str(c["default"]), "Column " + c["name"])
            line += f" CONSTRAINT {df_name} DEFAULT ({df_expr})"
        lines.append(line)
    if pk:
        lines.append(f"CONSTRAINT {q('PK_' + tbl)} PRIMARY KEY CLUSTERED ({', '.join(q(c['name']) for c in pk)})")

    indexed = [[c["name"].lower() for c in pk]] if pk else []
    index_sql = []
    for ix in indexes or []:
        ix_cols = [need(c, "indexes")["name"] for c in ix.get("columns") or []]
        if not ix_cols:
            raise DdlError("Each index needs 'columns'.")
        include = [need(c, "indexes.include")["name"] for c in ix.get("include") or []]
        ix_name = _ident(ix.get("name") or f"{'UX' if ix.get('unique') else 'IX'}_{tbl}_{'_'.join(ix_cols)}")
        index_sql.append(f"CREATE {'UNIQUE ' if ix.get('unique') else ''}INDEX {q(ix_name)} ON {q(sch)}.{q(tbl)} "
                         f"({', '.join(q(c) for c in ix_cols)})" + (f" INCLUDE ({', '.join(q(c) for c in include)})" if include else "") + ";")
        indexed.append([c.lower() for c in ix_cols])

    for fk in foreign_keys or []:
        fk_cols = [need(c, "foreign_keys") for c in fk.get("columns") or []]
        if not fk_cols or not fk.get("references"):
            raise DdlError("Each foreign key needs 'columns' and 'references' (the parent table).")
        rsch, rtbl = _split_name(str(fk["references"]), default_schema)
        ref_cols = [str(c).strip('[]"') for c in fk.get("ref_columns") or []]
        if schema is not None:
            parent = schema.require(str(fk["references"]))
            rsch, rtbl = parent.schema, parent.name
            ref_cols = ref_cols or parent.primary_key
            if not ref_cols:
                raise DdlError(f"{parent.full} has no primary key; name the referenced columns in 'ref_columns'.")
            for mine, theirs in zip(fk_cols, ref_cols):
                pc = parent.column(theirs)
                if pc is None:
                    raise DdlError(f"{parent.full} has no column '{theirs}'.{did_you_mean(theirs, [c.name for c in parent.columns])}")
                if pc.type.lower().replace(" ", "") != mine["type"].lower().replace(" ", ""):
                    notes.append(f"Type mismatch: {mine['name']} is {mine['type']} but {parent.full}.{pc.name} is {pc.type}. "
                                 f"SQL Server requires the same type for a foreign key; change {mine['name']} to {pc.type}.")
            unique_sets = [sorted(c.lower() for c in i.columns) for i in parent.indexes if i.unique or i.primary_key]
            if sorted(c.lower() for c in ref_cols) not in unique_sets:
                notes.append(f"{parent.full}({', '.join(ref_cols)}) is not a primary key or unique index there; the foreign key will be rejected.")
        elif not ref_cols:
            raise DdlError("Without a cached schema the parent's key is unknown; give 'ref_columns'.")
        if len(ref_cols) != len(fk_cols):
            raise DdlError(f"Foreign key to {rsch}.{rtbl}: {len(fk_cols)} column(s) here but {len(ref_cols)} referenced.")
        lines.append(f"CONSTRAINT {q(_ident(f'FK_{tbl}_{rtbl}'))} FOREIGN KEY ({', '.join(q(c['name']) for c in fk_cols)}) "
                     f"REFERENCES {q(rsch)}.{q(rtbl)} ({', '.join(q(c) for c in ref_cols)})")
        mine = [c["name"].lower() for c in fk_cols]
        if not any(ix[:len(mine)] == mine for ix in indexed):
            index_sql.append(f"CREATE INDEX {q(_ident(f'IX_{tbl}_' + '_'.join(c['name'] for c in fk_cols)))} ON {q(sch)}.{q(tbl)} "
                             f"({', '.join(q(c['name']) for c in fk_cols)});")
            indexed.append(mine)
            notes.append(f"Added an index on the foreign key ({', '.join(c['name'] for c in fk_cols)}): SQL Server does not create one, "
                         f"and joins/deletes on the parent scan without it.")

    full = f"{q(sch)}.{q(tbl)}"
    body = [f"CREATE TABLE {full} (", *("    " + ln + ("," if i < len(lines) - 1 else "") for i, ln in enumerate(lines)), ");", *index_sql]
    props = []
    for label, col, text in [(None, None, description)] + [(c["name"], c["name"], c.get("description", "")) for c in cols.values()]:
        if text:
            level2 = f", @level2type = N'COLUMN', @level2name = N'{col.replace(chr(39), chr(39) * 2)}'" if col else ""
            props.append(f"EXEC sys.sp_addextendedproperty @name = N'MS_Description', @value = N'{str(text).replace(chr(39), chr(39) * 2)}', "
                         f"@level0type = N'SCHEMA', @level0name = N'{sch}', @level1type = N'TABLE', @level1name = N'{tbl}'{level2};")
    sql = (f"IF OBJECT_ID(N'{sch}.{tbl}', N'U') IS NULL\nBEGIN\n" + "\n".join("    " + ln for ln in body + props) + "\nEND\n")
    notes.append("Script only: this server never runs DDL. Review it and run it yourself (SSMS / Azure Data Studio), then call refresh_schema.")
    return Script(sql, f"DROP TABLE IF EXISTS {full};\n", notes)


# ------------------------------------------------------------------ CREATE PROCEDURE / VIEW


def _body(sql: str, what: str) -> str:
    if len(split_batches(sql)) != 1:
        raise DdlError(f"The {what} body must be a single batch (no GO separators, not empty).")
    return sql.strip().rstrip(";").rstrip()


def create_procedure(name: str, body_sql: str, params: list[dict] | None = None, description: str = "") -> Script:
    sch, proc = _split_name(name)
    body = _body(body_sql, "procedure")
    notes: list[str] = []
    seen, decls = set(), []
    for p in params or []:
        pname = str(p.get("name", ""))
        pname = pname if pname.startswith("@") else "@" + pname
        ptype = str(p.get("type", "")).strip()
        if not _PARAM.match(pname):
            raise DdlError(f"'{pname}' is not a valid parameter name.")
        if not _TYPE.match(ptype):
            raise DdlError(f"Parameter {pname} needs a T-SQL type (e.g. date, int, nvarchar(50)); got '{ptype}'. "
                           f"A procedure cannot infer it.")
        if pname.lower() in seen:
            raise DdlError(f"Parameter {pname} is listed twice.")
        seen.add(pname.lower())
        default = p.get("default")
        line = f"    {pname} {ptype}" + (f" = {_literal(str(default), 'Parameter ' + pname)}" if default not in (None, "") else "")
        decls.append((line, str(p.get("description", ""))))
    declared = {v.lower() for v in declared_variables(body)}
    clash = sorted(seen & declared)
    if clash:
        raise DdlError(f"{', '.join(clash)} is a parameter and is also DECLAREd in the body; remove the DECLARE.")
    missing = [v for v in variables(body) if v.lower() not in seen and v.lower() not in declared]
    if missing:
        raise DdlError(f"The body uses {', '.join(missing)} but no such parameter is defined. Add it to params with a type.")
    unused = [p for p in seen if p not in {v.lower() for v in variables(body)}]
    if unused:
        notes.append(f"Parameter(s) {', '.join(sorted(unused))} are never used in the body.")
    try:
        check_read_only(body)
    except GuardError as ex:
        notes.append(f"The body is NOT a pure read, so this procedure would modify data or schema when executed ({str(ex).split('.')[0]}).")
    if not proc.lower().startswith(("usp_", "rpt_", "p_")):
        notes.append(f"Naming: consider a prefix such as usp_{proc}; never sp_ (SQL Server looks those up in master first).")
    if proc.lower().startswith("sp_"):
        raise DdlError("Do not name a procedure sp_*: SQL Server resolves that prefix against master first. Use usp_.")

    full = f"{q(sch)}.{q(proc)}"
    head = [f"-- {ln}" for ln in description.splitlines() if ln.strip()]
    param_lines = [ln + ("," if i < len(decls) - 1 else "") + (f"  -- {d}" if d else "") for i, (ln, d) in enumerate(decls)]
    sql = "\n".join([*head, f"CREATE OR ALTER PROCEDURE {full}", *param_lines, "AS", "BEGIN", "    SET NOCOUNT ON;", "",
                     *("    " + ln if ln.strip() else "" for ln in body.splitlines())]) + ";\nEND\nGO\n"
    example = ", ".join(f"{ln.split()[0]} = {ln.split(' = ', 1)[1] if ' = ' in ln else '<' + ln.split()[1] + '>'}" for ln, _ in decls)
    notes += [f"Try it with:  EXEC {full}{' ' + example if example else ''};",
              "CREATE OR ALTER needs SQL Server 2016 SP1+ / Azure SQL, and REPLACES an existing procedure of this name.",
              "Script only: this server never runs DDL. Review it and run it yourself."]
    return Script(sql, f"DROP PROCEDURE IF EXISTS {full};\n", notes)


def create_view(name: str, body_sql: str, description: str = "") -> Script:
    sch, view = _split_name(name)
    body = _body(body_sql, "view")
    try:
        check_read_only(body)
    except GuardError as ex:
        raise DdlError(f"A view must be a single SELECT. {ex}") from None
    if variables(body):
        raise DdlError(f"A view cannot take parameters ({', '.join(variables(body))}). Use create_procedure, or an inline "
                       f"table-valued function, for a parameterised query.")
    toks = significant(tokenize(body))
    if toks[0].upper == "DECLARE":
        raise DdlError("A view cannot DECLARE variables.")
    notes = []
    depth, top_level = 0, []
    for t in toks:
        if t.kind == PUNCT and t.text in "()":
            depth += 1 if t.text == "(" else -1
        elif depth == 0 and t.kind == WORD:
            top_level.append(t.upper)
    if "ORDER" in top_level and "TOP" not in top_level and "OFFSET" not in top_level:
        raise DdlError("A view cannot have ORDER BY (without TOP/OFFSET). Remove it; order in the query that reads the view.")
    if any(t.kind == PUNCT and t.text == "*" and toks[i - 1].upper in ("SELECT", "DISTINCT", ",", ".") for i, t in enumerate(toks) if i):
        notes.append("SELECT * in a view is frozen at creation time: new base-table columns do not appear until sp_refreshview. List the columns.")
    full = f"{q(sch)}.{q(view)}"
    head = [f"-- {ln}" for ln in description.splitlines() if ln.strip()]
    sql = "\n".join([*head, f"CREATE OR ALTER VIEW {full}", "AS", body]) + ";\nGO\n"
    notes += ["CREATE OR ALTER needs SQL Server 2016 SP1+ / Azure SQL, and REPLACES an existing view of this name.",
              "Script only: this server never runs DDL. Review it and run it yourself, then call refresh_schema."]
    return Script(sql, f"DROP VIEW IF EXISTS {full};\n", notes)
