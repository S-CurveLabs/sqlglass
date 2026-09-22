"""The parts of the SQL Server engine that can be checked without a server."""

import pytest

from sqlglass.config import Connection
from sqlglass.engines.base import EngineError
from sqlglass.engines import mssql
from sqlglass.engines.mssql import MssqlEngine, _type, connection_string, pick_driver

D18 = "ODBC Driver 18 for SQL Server"


def test_connection_strings(monkeypatch):
    win = connection_string(Connection("erp", server="srv\inst", database="ERP"), D18)
    assert win == ("DRIVER={ODBC Driver 18 for SQL Server};SERVER={srv\inst};DATABASE={ERP};APP=sqlglass;Encrypt=yes;"
                   "TrustServerCertificate=no;ApplicationIntent=ReadOnly;Trusted_Connection=yes")
    legacy = connection_string(Connection("erp", server="srv", database="ERP"), "SQL Server")
    assert "Encrypt" not in legacy and legacy.endswith("Trusted_Connection=yes")
    entra = connection_string(Connection("az", server="x.database.windows.net", database="d", auth="entra-interactive", user="me@x.com"), D18)
    assert "Authentication=ActiveDirectoryInteractive;UID={me@x.com}" in entra and "PWD" not in entra
    with pytest.raises(EngineError, match="ODBC Driver 17/18"):
        connection_string(Connection("az", server="x", auth="entra-default"), "SQL Server")

    sql = Connection("r", server="s", database="d", auth="sql", user="reader", password_env="SQLGLASS_TEST_PWD")
    with pytest.raises(EngineError, match="SQLGLASS_TEST_PWD"):
        connection_string(sql, D18)
    monkeypatch.setenv("SQLGLASS_TEST_PWD", "p;w}d;Trusted_Connection=yes")
    assert connection_string(sql, D18).endswith("UID={reader};PWD={p;w}}d;Trusted_Connection=yes}")  # braces keep ';' inert


def test_type_rendering():
    assert [_type(*a) for a in [("nvarchar", 200, 0, 0), ("nvarchar", -1, 0, 0), ("varchar", 50, 0, 0), ("decimal", 9, 18, 4),
                                ("datetime2", 8, 27, 7), ("datetime2", 7, 23, 3), ("int", 4, 10, 0)]] == \
        ["nvarchar(100)", "nvarchar(max)", "varchar(50)", "decimal(18,4)", "datetime2", "datetime2(3)", "int"]


def test_driver_selection_and_connect_failure(monkeypatch):
    monkeypatch.setattr(mssql, "LOGIN_TIMEOUT", 1)
    with pytest.raises(EngineError, match="is not installed. Installed:"):
        pick_driver("No Such Driver")
    try:
        engine = MssqlEngine(Connection("nowhere", server="127.0.0.1,1", database="x"))
    except EngineError:
        pytest.skip("no SQL Server ODBC driver on this machine")
    with pytest.raises(EngineError, match="Could not connect to 'nowhere'"):
        engine.run("SELECT 1", [], 1)
