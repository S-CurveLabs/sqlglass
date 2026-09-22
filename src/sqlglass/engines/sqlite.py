"""SQLite, opened read-only. Mostly here so the whole run path is testable without a SQL Server."""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path

from ..config import Connection
from ..schema import Column, ForeignKey, Index, Schema, Table
from .base import EngineError, Param, Result, jsonable


class SqliteEngine:
    dialect = "sqlite"

    def __init__(self, conn: Connection):
        self.conn = conn
        path = Path(conn.path)
        if not path.is_file():
            raise EngineError(f"Connection '{conn.name}': SQLite file not found: {path}")
        self._cn = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True, timeout=conn.timeout_seconds)

    def close(self) -> None:
        self._cn.close()

    def run(self, sql: str, params: list[Param], max_rows: int) -> Result:
        started = time.perf_counter()
        deadline = started + self.conn.timeout_seconds
        self._cn.set_progress_handler(lambda: 1 if time.perf_counter() > deadline else 0, 10_000)
        try:
            cur = self._cn.execute(sql.strip().rstrip(";"), {p.name[1:]: p.value for p in params})
            columns = [{"name": d[0], "type": ""} for d in cur.description or []]
            fetched = cur.fetchmany(max_rows + 1)
        except sqlite3.OperationalError as ex:
            if "interrupted" in str(ex):
                raise EngineError(f"The query was cancelled after the {self.conn.timeout_seconds}s timeout.") from None
            raise EngineError(f"SQLite rejected the query: {ex}") from None
        except sqlite3.Error as ex:
            raise EngineError(f"SQLite rejected the query: {ex}") from None
        finally:
            self._cn.set_progress_handler(None, 0)
            self._cn.rollback()
        rows = [[jsonable(v) for v in row] for row in fetched[:max_rows]]
        return Result(columns, rows, len(fetched) > max_rows, int((time.perf_counter() - started) * 1000))

    def plan(self, sql: str, params: list[Param]) -> list[str]:
        try:
            cur = self._cn.execute("EXPLAIN QUERY PLAN " + sql.strip().rstrip(";"), {p.name[1:]: p.value for p in params})
            return [row[3] for row in cur.fetchall()]
        except sqlite3.Error as ex:
            raise EngineError(f"SQLite rejected the query: {ex}") from None

    def introspect(self) -> Schema:
        schema = Schema(self.conn.name, Path(self.conn.path).name, default_schema="main", dialect="sqlite")
        objects = self._cn.execute("SELECT name, type FROM sqlite_master WHERE type IN ('table','view') "
                                   "AND name NOT LIKE 'sqlite_%' ORDER BY name").fetchall()
        for name, kind in objects:
            t = Table("main", name, kind)
            quoted = '"' + name.replace('"', '""') + '"'
            info = self._cn.execute(f"PRAGMA table_info({quoted})").fetchall()
            t.columns = [Column(c[1], (c[2] or "").lower(), not c[3]) for c in info]
            pk = [c[1] for c in sorted((c for c in info if c[5]), key=lambda c: c[5])]
            if pk:
                t.indexes.append(Index("PRIMARY", pk, [], True, True))
            for _, iname, unique, origin, *_ in self._cn.execute(f"PRAGMA index_list({quoted})").fetchall():
                if origin == "pk":
                    continue
                iq = '"' + iname.replace('"', '""') + '"'
                cols = [r[2] for r in self._cn.execute(f"PRAGMA index_info({iq})").fetchall() if r[2]]
                t.indexes.append(Index(iname, cols, [], bool(unique)))
            if kind == "table":
                t.rows = self._cn.execute(f"SELECT COUNT(*) FROM {quoted}").fetchone()[0]
                fks: dict[int, ForeignKey] = {}
                for fid, _, target, col, to_col, *_ in self._cn.execute(f"PRAGMA foreign_key_list({quoted})").fetchall():
                    fk = fks.setdefault(fid, ForeignKey(f"fk_{name}_{fid}", t.key, [], f"main.{target.lower()}", []))
                    fk.from_columns.append(col)
                    fk.to_columns.append(to_col or col)
                schema.foreign_keys += fks.values()
            schema.tables[t.key] = t
        return schema
