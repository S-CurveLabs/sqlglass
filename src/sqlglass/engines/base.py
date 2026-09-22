"""What every engine does, plus the parameter and result plumbing they share."""

from __future__ import annotations

import datetime as dt
import decimal
import re
import uuid
from dataclasses import dataclass
from typing import Any, Protocol

from ..errors import SqlGlassError
from ..schema import Schema

MAX_CELL_CHARS = 400
_NAME = re.compile(r"^@[A-Za-z_][A-Za-z0-9_]*$")
_TYPE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(\s*\(\s*(\d+|max)\s*(,\s*\d+\s*)?\))?$", re.I)


class EngineError(SqlGlassError):
    pass


@dataclass(frozen=True)
class Param:
    name: str  # with the leading @
    type: str  # T-SQL type, '' = infer from the value
    value: Any

    def __post_init__(self):
        if not _NAME.match(self.name):
            raise SqlGlassError(f"'{self.name}' is not a valid parameter name (expected @Name).")
        if self.type and not _TYPE.match(self.type):
            raise SqlGlassError(f"'{self.type}' is not a valid T-SQL type for parameter {self.name}.")

    @property
    def sql_type(self) -> str:
        if self.type:
            return self.type
        v = self.value
        if isinstance(v, bool):
            return "bit"
        if isinstance(v, int):
            return "bigint"
        if isinstance(v, float):
            return "float"
        return "nvarchar(4000)"


def literal(value: Any) -> str:
    """Render a Python value as a T-SQL literal. Strings cannot break out: ' is the only escape and it is doubled."""
    if value is None:
        return "NULL"
    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, (int, float, decimal.Decimal)):
        return repr(value) if not isinstance(value, decimal.Decimal) else str(value)
    return "N'" + str(value).replace("'", "''") + "'"


def declare_bound(params: list[Param]) -> tuple[str, list[Any]]:
    """DECLAREs that take their value from ODBC '?' markers (pyodbc has no named parameters)."""
    return "".join(f"DECLARE {p.name} {p.sql_type} = ?;\n" for p in params), [p.value for p in params]


def declare_literal(params: list[Param]) -> str:
    return "".join(f"DECLARE {p.name} {p.sql_type} = {literal(p.value)};\n" for p in params)


@dataclass
class Result:
    columns: list[dict]
    rows: list[list]
    truncated: bool
    elapsed_ms: int

    def to_dict(self, max_rows: int) -> dict:
        out = {"columns": self.columns, "rows": self.rows, "row_count": len(self.rows), "elapsed_ms": self.elapsed_ms}
        if self.truncated:
            out["truncated"] = (f"Only the first {max_rows} rows are shown; more exist. "
                                f"Aggregate or filter in SQL rather than raising max_rows.")
        return out


def jsonable(v: Any) -> Any:
    if v is None or isinstance(v, (bool, int, float)):
        return v
    if isinstance(v, decimal.Decimal):
        return int(v) if v == v.to_integral_value() else float(v)
    if isinstance(v, (dt.datetime, dt.date, dt.time)):
        return v.isoformat()
    if isinstance(v, (bytes, bytearray, memoryview)):
        b = bytes(v)
        return "0x" + b[:16].hex() + ("..." if len(b) > 16 else "")
    if isinstance(v, uuid.UUID):
        return str(v)
    s = str(v)
    return s if len(s) <= MAX_CELL_CHARS else s[:MAX_CELL_CHARS] + f"... [{len(s)} chars]"


class Engine(Protocol):
    dialect: str

    def introspect(self) -> Schema: ...
    def run(self, sql: str, params: list[Param], max_rows: int) -> Result: ...
    def plan(self, sql: str, params: list[Param]) -> list[str]: ...
    def close(self) -> None: ...
