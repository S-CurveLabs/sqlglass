import sqlite3

import pytest

from sqlglass.schema import Column, ForeignKey, Index, Schema, Table

DDL = """
CREATE TABLE Vendor (VendorId INTEGER PRIMARY KEY, Name TEXT NOT NULL, Country TEXT);
CREATE TABLE PoHeader (PoId INTEGER PRIMARY KEY, VendorId INTEGER NOT NULL REFERENCES Vendor(VendorId), OrderDate TEXT NOT NULL, Status TEXT);
CREATE TABLE PoLine (PoLineId INTEGER PRIMARY KEY, PoId INTEGER NOT NULL REFERENCES PoHeader(PoId), Amount REAL, Note TEXT);
CREATE TABLE Blocked (VendorId INTEGER PRIMARY KEY, Reason TEXT);
CREATE INDEX IX_PoHeader_Vendor ON PoHeader (VendorId);
CREATE VIEW OpenPo AS SELECT * FROM PoHeader WHERE Status = 'OPEN';
INSERT INTO Vendor VALUES (1,'Acme','US'),(2,'Globex','DE'),(3,'Initech',NULL);
INSERT INTO PoHeader VALUES (10,1,'2026-01-05','OPEN'),(11,1,'2026-02-01','CLOSED'),(12,2,'2026-03-10','OPEN');
INSERT INTO PoLine VALUES (100,10,50.0,'a'),(101,10,25.5,NULL),(102,11,10.0,'b'),(103,12,99.0,'it''s');
INSERT INTO Blocked VALUES (3,'audit');
"""


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    """A SQLite database, a config pointing at it, an empty library, and a private SQLGLASS_HOME."""
    db = tmp_path / "erp.db"
    cn = sqlite3.connect(db)
    cn.executescript(DDL)
    cn.close()
    (tmp_path / "sqlglass.toml").write_text(
        f'[library]\npath = "queries"\ndefault_connection = "erp"\n\n'
        f'[connections.erp]\nengine = "sqlite"\npath = "{db.as_posix()}"\nmax_rows = 3\n', encoding="utf-8")
    monkeypatch.setenv("SQLGLASS_CONFIG", str(tmp_path / "sqlglass.toml"))
    monkeypatch.setenv("SQLGLASS_HOME", str(tmp_path / "home"))
    monkeypatch.delenv("SQLGLASS_WORKSPACE", raising=False)
    return tmp_path


@pytest.fixture
def erp() -> Schema:
    """The same model as a SQL Server schema (dbo, T-SQL quoting), built by hand."""
    def table(name, cols, pk):
        return Table("dbo", name, "table", 1000, "", [Column(c, t, c not in pk) for c, t in cols],
                     [Index(f"PK_{name}", pk, [], True, True)])
    tables = [
        table("Vendor", [("VendorId", "int"), ("Name", "nvarchar(100)"), ("Country", "char(2)")], ["VendorId"]),
        table("PoHeader", [("PoId", "int"), ("VendorId", "int"), ("OrderDate", "date"), ("Status", "varchar(10)")], ["PoId"]),
        table("PoLine", [("PoLineId", "int"), ("PoId", "int"), ("Amount", "decimal(18,2)"), ("Note", "nvarchar(max)")], ["PoLineId"]),
        table("Blocked", [("VendorId", "int"), ("Reason", "nvarchar(200)")], ["VendorId"]),
        table("Calendar", [("DateKey", "int"), ("FiscalPeriod", "char(6)")], ["DateKey"]),
    ]
    fks = [ForeignKey("FK_PoHeader_Vendor", "dbo.poheader", ["VendorId"], "dbo.vendor", ["VendorId"]),
           ForeignKey("FK_PoLine_PoHeader", "dbo.poline", ["PoId"], "dbo.poheader", ["PoId"])]
    return Schema("erp", "ERP", "", {t.key: t for t in tables}, fks)
