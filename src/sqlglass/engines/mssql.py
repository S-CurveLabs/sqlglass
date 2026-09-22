"""SQL Server / Azure SQL over ODBC.

Read-only is enforced here a second time: every query runs with autocommit off and
the transaction is rolled back no matter what happened. Point the connection at a
db_datareader login and there is a third layer the server itself enforces.
"""

from __future__ import annotations

import os
import time

from ..config import Connection
from ..schema import Column, ForeignKey, Index, Schema, Table
from .base import EngineError, Param, Result, declare_bound, declare_literal, jsonable

PREFERRED_DRIVERS = ("ODBC Driver 18 for SQL Server", "ODBC Driver 17 for SQL Server",
                     "ODBC Driver 13.1 for SQL Server", "ODBC Driver 13 for SQL Server",
                     "SQL Server Native Client 11.0", "SQL Server")
LOGIN_TIMEOUT = 15


def _pyodbc():
    try:
        import pyodbc
    except ImportError:
        raise EngineError("pyodbc is not installed in this environment (pip install pyodbc).") from None
    return pyodbc


def pick_driver(configured: str = "") -> str:
    installed = _pyodbc().drivers()
    if configured:
        if configured not in installed:
            raise EngineError(f"ODBC driver '{configured}' is not installed. Installed: {', '.join(installed)}")
        return configured
    for d in PREFERRED_DRIVERS:
        if d in installed:
            return d
    raise EngineError("No SQL Server ODBC driver is installed. Install 'ODBC Driver 18 for SQL Server' from Microsoft.")


def connection_string(c: Connection, driver: str) -> str:
    """Values are brace-quoted so a ';' in a name or password cannot add keywords."""
    def q(v: str) -> str:
        return "{" + v.replace("}", "}}") + "}"

    modern = driver.startswith("ODBC Driver")
    parts = [f"DRIVER={q(driver)}", f"SERVER={q(c.server)}"]
    if c.database:
        parts.append(f"DATABASE={q(c.database)}")
    parts.append("APP=sqlglass")
    if modern:
        parts += [f"Encrypt={'yes' if c.encrypt else 'no'}",
                  f"TrustServerCertificate={'yes' if c.trust_server_certificate else 'no'}",
                  "ApplicationIntent=ReadOnly"]
    if c.auth == "windows":
        parts.append("Trusted_Connection=yes")
    elif c.auth == "sql":
        pwd = os.environ.get(c.password_env or "")
        if not c.user or not pwd:
            raise EngineError(f"Connection '{c.name}' uses SQL authentication: set 'user' in the config and put the password "
                              f"in the environment variable named by password_env ('{c.password_env or 'not set'}').")
        parts += [f"UID={q(c.user)}", f"PWD={q(pwd)}"]
    else:
        if not modern:
            raise EngineError(f"Entra ID sign-in needs 'ODBC Driver 17/18 for SQL Server'; only '{driver}' is installed.")
        mode = {"entra-interactive": "ActiveDirectoryInteractive", "entra-integrated": "ActiveDirectoryIntegrated",
                "entra-default": "ActiveDirectoryDefault"}[c.auth]
        parts.append(f"Authentication={mode}")
        if c.user:
            parts.append(f"UID={q(c.user)}")
    return ";".join(parts)


class MssqlEngine:
    dialect = "tsql"

    def __init__(self, conn: Connection):
        self.conn = conn
        self.driver = pick_driver(conn.driver)
        self._cn = None

    def _connect(self, autocommit: bool = False):
        pyodbc = _pyodbc()
        if self._cn is None:
            try:
                self._cn = pyodbc.connect(connection_string(self.conn, self.driver), autocommit=autocommit,
                                          timeout=LOGIN_TIMEOUT, readonly=True)
            except pyodbc.Error as ex:
                raise EngineError(f"Could not connect to '{self.conn.name}' ({self.conn.server}/{self.conn.database}) "
                                  f"with {self.driver}: {_message(ex)}") from None
            self._cn.timeout = self.conn.timeout_seconds
        self._cn.autocommit = autocommit
        return self._cn

    def close(self) -> None:
        if self._cn is not None:
            try:
                self._cn.close()
            finally:
                self._cn = None

    # ------------------------------------------------------------------ run

    def run(self, sql: str, params: list[Param], max_rows: int) -> Result:
        pyodbc = _pyodbc()
        prefix, values = declare_bound(params)
        cn = self._connect(autocommit=False)
        cur = cn.cursor()
        started = time.perf_counter()
        try:
            cur.execute("SET NOCOUNT ON;\n" + prefix + sql, *values)
            while cur.description is None:
                if not cur.nextset():
                    return Result([], [], False, int((time.perf_counter() - started) * 1000))
            columns = [{"name": d[0], "type": getattr(d[1], "__name__", str(d[1]))} for d in cur.description]
            fetched = cur.fetchmany(max_rows + 1)
            rows = [[jsonable(v) for v in row] for row in fetched[:max_rows]]
            return Result(columns, rows, len(fetched) > max_rows, int((time.perf_counter() - started) * 1000))
        except pyodbc.Error as ex:
            raise EngineError(_explain(ex, self.conn.timeout_seconds)) from None
        finally:
            try:
                cur.close()
                cn.rollback()
            except pyodbc.Error:
                self.close()

    # ------------------------------------------------------------------ plan

    def plan(self, sql: str, params: list[Param]) -> list[str]:
        """Estimated plan. Under SHOWPLAN_XML the server compiles the batch and executes nothing."""
        pyodbc = _pyodbc()
        cn = self._connect(autocommit=True)
        cur = cn.cursor()
        try:
            cur.execute("SET SHOWPLAN_XML ON")
            try:
                cur.execute(declare_literal(params) + sql)
                chunks = []
                while True:
                    if cur.description is not None:
                        chunks += [row[0] for row in cur.fetchall() if row and isinstance(row[0], str)]
                    if not cur.nextset():
                        break
            finally:
                cur.execute("SET SHOWPLAN_XML OFF")
            if not chunks:
                raise EngineError("The server returned no plan.")
            return chunks
        except pyodbc.Error as ex:
            self.close()  # SHOWPLAN_XML OFF may not have run; do not reuse this connection
            msg = _message(ex)
            if "SHOWPLAN" in msg.upper():
                msg += " (the login needs: GRANT SHOWPLAN TO [user];)"
            raise EngineError(msg) from None
        finally:
            cur.close()

    # ------------------------------------------------------------------ introspection

    def introspect(self) -> Schema:
        pyodbc = _pyodbc()
        cn = self._connect(autocommit=False)
        cur = cn.cursor()
        try:
            database = cur.execute("SELECT DB_NAME()").fetchone()[0]
            schema = Schema(self.conn.name, database)
            by_id: dict[int, Table] = {}
            for oid, sname, oname, kind, rows, desc in cur.execute(_OBJECTS).fetchall():
                if not self._wanted(sname):
                    continue
                t = Table(sname, oname, "view" if kind.strip() == "V" else "table", rows, desc or "")
                by_id[oid] = schema.tables[t.key] = t
            for oid, name, tname, max_len, prec, scale, nullable, identity, computed, desc in cur.execute(_COLUMNS).fetchall():
                if oid in by_id:
                    by_id[oid].columns.append(Column(name, _type(tname, max_len, prec, scale), bool(nullable),
                                                     bool(identity), bool(computed), desc or ""))
            indexes: dict[tuple[int, int], Index] = {}
            for oid, iid, iname, unique, pk, col, included in cur.execute(_INDEXES).fetchall():
                if oid not in by_id:
                    continue
                ix = indexes.get((oid, iid))
                if ix is None:
                    ix = indexes[(oid, iid)] = Index(iname, [], [], bool(unique), bool(pk))
                    by_id[oid].indexes.append(ix)
                (ix.included if included else ix.columns).append(col)
            fks: dict[str, ForeignKey] = {}
            for fname, from_id, from_col, to_id, to_col in cur.execute(_FOREIGN_KEYS).fetchall():
                if from_id in by_id and to_id in by_id:
                    fk = fks.setdefault(f"{from_id}:{fname}", ForeignKey(fname, by_id[from_id].key, [], by_id[to_id].key, []))
                    fk.from_columns.append(from_col)
                    fk.to_columns.append(to_col)
            schema.foreign_keys = list(fks.values())
            return schema
        except pyodbc.Error as ex:
            raise EngineError(f"Schema introspection failed: {_message(ex)}") from None
        finally:
            cur.close()
            cn.rollback()

    def _wanted(self, schema_name: str) -> bool:
        s = schema_name.lower()
        inc = [x.lower() for x in self.conn.include_schemas]
        exc = [x.lower() for x in self.conn.exclude_schemas] + ["sys", "information_schema"]
        return (not inc or s in inc) and s not in exc


def _type(name: str, max_len: int, prec: int, scale: int) -> str:
    n = name.lower()
    if n in ("varchar", "char", "varbinary", "binary"):
        return f"{n}({'max' if max_len == -1 else max_len})"
    if n in ("nvarchar", "nchar"):
        return f"{n}({'max' if max_len == -1 else max_len // 2})"
    if n in ("decimal", "numeric"):
        return f"{n}({prec},{scale})"
    if n in ("datetime2", "time", "datetimeoffset") and scale != 7:
        return f"{n}({scale})"
    return n


def _message(ex: Exception) -> str:
    text = str(ex.args[1]) if len(getattr(ex, "args", ())) > 1 else str(ex)
    for noise in ("[Microsoft]", "[SQL Server]", "[ODBC SQL Server Driver]"):
        text = text.replace(noise, "")
    for d in PREFERRED_DRIVERS:
        text = text.replace(f"[{d}]", "")
    return " ".join(text.split())


def _explain(ex: Exception, timeout: int) -> str:
    state = ex.args[0] if getattr(ex, "args", None) else ""
    if state == "HYT00":
        return (f"The query was cancelled after the {timeout}s timeout. Use explain_query to see why it is slow, "
                f"narrow the filter, or raise timeout_seconds for this connection.")
    return f"SQL Server rejected the query: {_message(ex)}"


_OBJECTS = """
SELECT o.object_id, s.name, o.name, o.type,
       (SELECT SUM(p.rows) FROM sys.partitions p WHERE p.object_id = o.object_id AND p.index_id IN (0, 1)),
       CAST(ep.value AS nvarchar(4000))
FROM sys.objects o
JOIN sys.schemas s ON s.schema_id = o.schema_id
LEFT JOIN sys.extended_properties ep
       ON ep.class = 1 AND ep.major_id = o.object_id AND ep.minor_id = 0 AND ep.name = 'MS_Description'
WHERE o.type IN ('U', 'V') AND o.is_ms_shipped = 0
ORDER BY s.name, o.name"""

_COLUMNS = """
SELECT c.object_id, c.name, t.name, c.max_length, c.precision, c.scale, c.is_nullable, c.is_identity, c.is_computed,
       CAST(ep.value AS nvarchar(4000))
FROM sys.columns c
JOIN sys.objects o ON o.object_id = c.object_id AND o.type IN ('U', 'V') AND o.is_ms_shipped = 0
JOIN sys.types t ON t.user_type_id = c.user_type_id
LEFT JOIN sys.extended_properties ep
       ON ep.class = 1 AND ep.major_id = c.object_id AND ep.minor_id = c.column_id AND ep.name = 'MS_Description'
ORDER BY c.object_id, c.column_id"""

_INDEXES = """
SELECT i.object_id, i.index_id, i.name, i.is_unique, i.is_primary_key, c.name, ic.is_included_column
FROM sys.indexes i
JOIN sys.objects o ON o.object_id = i.object_id AND o.type IN ('U', 'V') AND o.is_ms_shipped = 0
JOIN sys.index_columns ic ON ic.object_id = i.object_id AND ic.index_id = i.index_id
JOIN sys.columns c ON c.object_id = ic.object_id AND c.column_id = ic.column_id
WHERE i.index_id > 0 AND i.is_hypothetical = 0 AND i.name IS NOT NULL
ORDER BY i.object_id, i.index_id, ic.is_included_column, ic.key_ordinal, ic.index_column_id"""

_FOREIGN_KEYS = """
SELECT fk.name, fk.parent_object_id, pc.name, fk.referenced_object_id, rc.name
FROM sys.foreign_keys fk
JOIN sys.foreign_key_columns fkc ON fkc.constraint_object_id = fk.object_id
JOIN sys.columns pc ON pc.object_id = fkc.parent_object_id AND pc.column_id = fkc.parent_column_id
JOIN sys.columns rc ON rc.object_id = fkc.referenced_object_id AND rc.column_id = fkc.referenced_column_id
ORDER BY fk.object_id, fkc.constraint_column_id"""
