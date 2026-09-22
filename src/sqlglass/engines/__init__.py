from ..config import Connection
from .base import Engine, EngineError, Param, Result


def open_engine(conn: Connection) -> Engine:
    if conn.engine == "sqlite":
        from .sqlite import SqliteEngine
        return SqliteEngine(conn)
    from .mssql import MssqlEngine
    return MssqlEngine(conn)


__all__ = ["Engine", "EngineError", "Param", "Result", "open_engine"]
