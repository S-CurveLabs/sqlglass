"""preview_write: a write statement becomes the read-only SELECT that shows what it would do."""

import pytest

from sqlglass import server as srv
from sqlglass.errors import GuardError, SqlGlassError
from sqlglass.preview import preview_write
from sqlglass.tsql.guard import check_read_only


def test_update_single_table(erp):
    p = preview_write("UPDATE dbo.PoHeader SET Status = 'CLOSED', OrderDate = DATEADD(day, 1, OrderDate) "
                      "WHERE Status = 'OPEN' AND VendorId = @v", erp)
    assert (p.kind, p.target) == ("update", "dbo.PoHeader")
    assert p.sql == ("SELECT\n    [PoHeader].[PoId],\n    [PoHeader].[Status] AS [Status (current)],\n    'CLOSED' AS [Status (new)],\n"
                     "    [PoHeader].[OrderDate] AS [OrderDate (current)],\n    DATEADD(DAY, 1, OrderDate) AS [OrderDate (new)]\n"
                     "FROM dbo.PoHeader\nWHERE Status = 'OPEN' AND VendorId = @v\n")
    assert p.count_sql == "SELECT COUNT(*) AS affected_rows\nFROM dbo.PoHeader\nWHERE Status = 'OPEN' AND VendorId = @v\n"


def test_update_from_join_changed_only(erp):
    p = preview_write("UPDATE h SET h.Status = v.Country FROM dbo.PoHeader AS h JOIN dbo.Vendor v ON v.VendorId = h.VendorId "
                      "WHERE v.Country = 'US' OR v.Country IS NULL", erp, changed_only=True)
    assert p.target == "dbo.PoHeader" and "[h].[PoId]" in p.sql and "v.Country AS [Status (new)]" in p.sql
    assert "FROM dbo.PoHeader AS h JOIN dbo.Vendor AS v ON v.VendorId = h.VendorId" in p.sql
    assert "WHERE (v.Country = 'US' OR v.Country IS NULL)\n  AND EXISTS (SELECT [h].[Status] EXCEPT SELECT v.Country)" in p.sql


def test_delete_and_insert(erp):
    p = preview_write("DELETE l FROM dbo.PoLine l JOIN dbo.PoHeader h ON h.PoId = l.PoId WHERE h.Status = 'CLOSED'", erp)
    assert p.target == "dbo.PoLine" and p.sql.startswith("SELECT\n    [l].[PoLineId],\n    [l].[PoId],") and "WHERE h.Status = 'CLOSED'" in p.sql
    p = preview_write("DELETE FROM dbo.Vendor", erp)
    assert any("no WHERE" in n for n in p.notes) and any("dbo.PoHeader reference" in n for n in p.notes)
    p = preview_write("WITH c AS (SELECT PoId FROM dbo.PoHeader WHERE Status = 'X') DELETE FROM dbo.PoLine WHERE PoId IN (SELECT PoId FROM c)")
    assert p.sql.startswith("WITH c AS (") and "[PoLine].*" in p.sql  # no schema: falls back to *

    p = preview_write("INSERT INTO dbo.Blocked (VendorId, Reason) SELECT v.VendorId, 'none' FROM dbo.Vendor v ORDER BY v.Name", erp)
    assert p.sql == "SELECT new_rows.*\nFROM (\nSELECT v.VendorId, 'none' FROM dbo.Vendor AS v\n) AS new_rows ([VendorId], [Reason])\n"
    p = preview_write("INSERT INTO dbo.Vendor VALUES (9, N'it''s', 'US')", erp)
    assert "VALUES (9, N'it''s', 'US')" in p.sql and p.sql.endswith("AS new_rows ([VendorId], [Name], [Country])\n")
    for col in ("VendorId", "OrderDate"):
        erp.tables["dbo.poheader"].column(col).nullable = False
    p = preview_write("INSERT INTO dbo.PoHeader (PoId, Status) VALUES (1, 'OPEN')", erp)
    assert any("VendorId, OrderDate are not supplied" in n for n in p.notes)


@pytest.mark.parametrize("sql,msg", [
    ("MERGE dbo.t AS t USING dbo.s AS s ON t.id = s.id WHEN MATCHED THEN UPDATE SET t.a = s.a;", "MERGE cannot"),
    ("SELECT 1", "already a SELECT"),
    ("DROP TABLE dbo.Vendor", "Only UPDATE, DELETE and INSERT"),
    ("UPDATE dbo.Vendor SET Name = 'x'; DELETE FROM dbo.Vendor", "exactly one"),
    ("UPDATE dbo.Vendor SET Nmae = 'x'", "no column 'Nmae'"),
    ("INSERT INTO dbo.Nope VALUES (1)", "no column list"),
])
def test_refusals(erp, sql, msg):
    with pytest.raises(SqlGlassError, match=msg):
        preview_write(sql, erp)


def test_output_is_always_a_pure_read(erp):
    for sql in ("UPDATE dbo.Vendor SET Name = (SELECT TOP 1 Reason FROM dbo.Blocked ORDER BY Reason) WHERE VendorId = 1",
                "DELETE FROM dbo.Blocked WHERE Reason = 'DROP TABLE x'",
                "INSERT INTO dbo.Blocked (VendorId, Reason) SELECT VendorId, Name FROM dbo.Vendor"):
        p = preview_write(sql, erp)
        check_read_only(p.sql)
        check_read_only(p.count_sql)


def test_tool_runs_the_preview_and_changes_nothing(workspace):
    srv.list_tables()  # cache the schema so the preview can include the key
    out = srv.preview_write("UPDATE PoHeader SET Status = 'CLOSED' WHERE VendorId = @v", run=True, params={"v": 1})
    assert out["affected_rows"] == 2 and [c["name"] for c in out["columns"]] == ["PoId", "Status (current)", "Status (new)"]
    assert out["rows"] == [[10, "OPEN", "CLOSED"], [11, "CLOSED", "CLOSED"]]
    only = srv.preview_write("UPDATE PoHeader SET Status = 'CLOSED' WHERE VendorId = @v", changed_only=True, run=True, params={"v": 1})
    assert only["affected_rows"] == 1 and only["rows"] == [[10, "OPEN", "CLOSED"]]
    gone = srv.preview_write("DELETE FROM PoLine WHERE Amount < 30", run=True)
    assert gone["affected_rows"] == 2 and [r[0] for r in gone["rows"]] == [101, 102]
    text_only = srv.preview_write("INSERT INTO Blocked (VendorId, Reason) SELECT VendorId, 'x' FROM Vendor")
    assert "rows" not in text_only and "new_rows" in text_only["preview_sql"]

    assert srv.run_query(sql="SELECT COUNT(*) FROM PoLine")["rows"] == [[4]]
    assert srv.run_query(sql="SELECT COUNT(*) FROM PoHeader WHERE Status = 'OPEN'")["rows"] == [[2]]
    with pytest.raises(GuardError, match="preview_write"):
        srv.run_query(sql="UPDATE PoHeader SET Status = 'CLOSED'")
