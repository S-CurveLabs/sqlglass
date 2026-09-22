"""DDL script generators: text only, validated against the schema, never executed."""

import pytest

from sqlglass import ddl
from sqlglass import server as srv
from sqlglass.errors import SqlGlassError

COLUMNS = [{"name": "VendorScoreId", "type": "int", "identity": True},
           {"name": "VendorId", "type": "int", "nullable": False},
           {"name": "Score", "type": "decimal(5,2)", "nullable": False, "default": "0", "description": "0-100, it's relative"},
           {"name": "ScoredOn", "type": "date", "default": "CAST(GETDATE() AS date)"},
           {"name": "Comment", "type": "nvarchar(400)"}]


def test_create_table_script(erp):
    s = ddl.create_table(erp, "dbo.VendorScore", COLUMNS, ["VendorScoreId"], [{"columns": ["VendorId"], "references": "Vendor"}],
                         [{"columns": ["ScoredOn"], "include": ["Score"]}], description="Quarterly vendor scores")
    assert s.sql == """IF OBJECT_ID(N'dbo.VendorScore', N'U') IS NULL
BEGIN
    CREATE TABLE [dbo].[VendorScore] (
        [VendorScoreId] int IDENTITY(1,1) NOT NULL,
        [VendorId] int NOT NULL,
        [Score] decimal(5,2) NOT NULL CONSTRAINT [DF_VendorScore_Score] DEFAULT (0),
        [ScoredOn] date NULL CONSTRAINT [DF_VendorScore_ScoredOn] DEFAULT (CAST(GETDATE() AS date)),
        [Comment] nvarchar(400) NULL,
        CONSTRAINT [PK_VendorScore] PRIMARY KEY CLUSTERED ([VendorScoreId]),
        CONSTRAINT [FK_VendorScore_Vendor] FOREIGN KEY ([VendorId]) REFERENCES [dbo].[Vendor] ([VendorId])
    );
    CREATE INDEX [IX_VendorScore_ScoredOn] ON [dbo].[VendorScore] ([ScoredOn]) INCLUDE ([Score]);
    CREATE INDEX [IX_VendorScore_VendorId] ON [dbo].[VendorScore] ([VendorId]);
    EXEC sys.sp_addextendedproperty @name = N'MS_Description', @value = N'Quarterly vendor scores', @level0type = N'SCHEMA', @level0name = N'dbo', @level1type = N'TABLE', @level1name = N'VendorScore';
    EXEC sys.sp_addextendedproperty @name = N'MS_Description', @value = N'0-100, it''s relative', @level0type = N'SCHEMA', @level0name = N'dbo', @level1type = N'TABLE', @level1name = N'VendorScore', @level2type = N'COLUMN', @level2name = N'Score';
END
"""
    assert s.undo_sql == "DROP TABLE IF EXISTS [dbo].[VendorScore];\n"
    assert any("Added an index on the foreign key" in n for n in s.notes) and any("never runs DDL" in n for n in s.notes)


def test_create_table_checks(erp):
    with pytest.raises(SqlGlassError, match="already exists"):
        ddl.create_table(erp, "dbo.Vendor", COLUMNS)
    with pytest.raises(SqlGlassError, match="not a valid T-SQL type"):
        ddl.create_table(erp, "dbo.X", [{"name": "a", "type": "int); DROP TABLE dbo.Vendor; --"}])
    with pytest.raises(SqlGlassError, match="single expression"):
        ddl.create_table(erp, "dbo.X", [{"name": "a", "type": "int", "default": "0); DROP TABLE dbo.Vendor; --"}])
    with pytest.raises(SqlGlassError, match="not a valid object name"):
        ddl.create_table(erp, "dbo.X]; DROP TABLE t", COLUMNS)
    with pytest.raises(SqlGlassError, match="Did you mean: VendorId"):
        ddl.create_table(erp, "dbo.X", COLUMNS, ["VendorID_"])
    with pytest.raises(SqlGlassError, match="Did you mean: dbo.Vendor"):
        ddl.create_table(erp, "dbo.X", COLUMNS, foreign_keys=[{"columns": ["VendorId"], "references": "dbo.Vendr"}])
    with pytest.raises(SqlGlassError, match="IDENTITY needs an integer"):
        ddl.create_table(erp, "dbo.X", [{"name": "a", "type": "date", "identity": True}])

    s = ddl.create_table(erp, "dbo.X", [{"name": "VendorId", "type": "bigint"}, {"name": "Amount", "type": "float"}, {"name": "Code", "type": "varchar"}],
                         foreign_keys=[{"columns": ["VendorId"], "references": "dbo.Vendor"}, ])
    text = " ".join(s.notes)
    assert "Type mismatch: VendorId is bigint" in text and "float is approximate" in text and "without a length" in text and "heap" in text
    s = ddl.create_table(erp, "dbo.Y", [{"name": "Country", "type": "char(2)"}], foreign_keys=[{"columns": ["Country"], "references": "dbo.Vendor", "ref_columns": ["Country"]}])
    assert any("not a primary key or unique index" in n for n in s.notes)
    assert any("No cached schema" in n for n in ddl.create_table(None, "T", [{"name": "a", "type": "int"}]).notes)


def test_create_procedure(erp):
    body = "SELECT v.Name, h.PoId\nFROM dbo.PoHeader h\nJOIN dbo.Vendor v ON v.VendorId = h.VendorId\nWHERE h.OrderDate >= @Start AND v.Country = @Country;"
    s = ddl.create_procedure("dbo.usp_PosSince", body, [{"name": "Start", "type": "date", "default": "'2026-01-01'", "description": "first day"},
                                                        {"name": "@Country", "type": "char(2)"}], description="POs since a date")
    assert s.sql == """-- POs since a date
CREATE OR ALTER PROCEDURE [dbo].[usp_PosSince]
    @Start date = '2026-01-01',  -- first day
    @Country char(2)
AS
BEGIN
    SET NOCOUNT ON;

    SELECT v.Name, h.PoId
    FROM dbo.PoHeader h
    JOIN dbo.Vendor v ON v.VendorId = h.VendorId
    WHERE h.OrderDate >= @Start AND v.Country = @Country;
END
GO
"""
    assert any("EXEC [dbo].[usp_PosSince] @Start = '2026-01-01', @Country = <char(2)>;" in n for n in s.notes)
    assert not any("NOT a pure read" in n for n in s.notes)
    for kwargs, msg in [({"params": [{"name": "Start", "type": "date"}]}, "uses @Country but no such parameter"),
                        ({"params": [{"name": "Start"}, {"name": "Country", "type": "char(2)"}]}, "needs a T-SQL type"),
                        ({"params": [{"name": "Start", "type": "date", "default": "GETDATE(); DROP TABLE t"}, {"name": "Country", "type": "char(2)"}]}, "one literal")]:
        with pytest.raises(SqlGlassError, match=msg):
            ddl.create_procedure("dbo.usp_X", body, **kwargs)
    with pytest.raises(SqlGlassError, match="sp_"):
        ddl.create_procedure("dbo.sp_Thing", "SELECT 1")
    with pytest.raises(SqlGlassError, match="single batch"):
        ddl.create_procedure("dbo.usp_X", "SELECT 1\nGO\nSELECT 2")
    writes = ddl.create_procedure("dbo.usp_Close", "UPDATE dbo.PoHeader SET Status = 'CLOSED' WHERE PoId = @PoId", [{"name": "PoId", "type": "int"}])
    assert any("NOT a pure read" in n for n in writes.notes)


def test_create_view():
    s = ddl.create_view("rpt.OpenPo", "SELECT h.PoId, h.VendorId FROM dbo.PoHeader h WHERE h.Status = 'OPEN';", "Open POs")
    assert s.sql == "-- Open POs\nCREATE OR ALTER VIEW [rpt].[OpenPo]\nAS\nSELECT h.PoId, h.VendorId FROM dbo.PoHeader h WHERE h.Status = 'OPEN';\nGO\n"
    assert any("frozen at creation" in n for n in ddl.create_view("dbo.V", "SELECT h.*, COUNT(*) OVER () AS n FROM dbo.PoHeader h").notes)
    assert not any("frozen" in n for n in ddl.create_view("dbo.V", "SELECT COUNT(*) AS n FROM dbo.PoHeader").notes)
    ddl.create_view("dbo.V", "SELECT TOP 5 PoId FROM dbo.PoHeader ORDER BY PoId")
    for sql, msg in [("SELECT PoId FROM dbo.PoHeader ORDER BY PoId", "cannot have ORDER BY"),
                     ("SELECT PoId FROM dbo.PoHeader WHERE VendorId = @v", "cannot take parameters"),
                     ("DELETE FROM dbo.PoHeader", "must be a single SELECT")]:
        with pytest.raises(SqlGlassError, match=msg):
            ddl.create_view("dbo.V", sql)


def test_tools_generate_text_and_scripts_are_never_run(workspace):
    srv.list_tables()
    body = "SELECT v.Name FROM Vendor v WHERE v.Country = @Country"
    srv.save_query("by-country", body, description="Vendors in a country", params=[{"name": "Country", "type": "char(2)", "default": "'US'"}])
    proc = srv.build_procedure("dbo.usp_VendorsByCountry", query="by-country")
    assert proc["executed"] is False and "@Country char(2) = 'US'" in proc["sql"] and proc["sql"].startswith("-- Vendors in a country\n")
    table = srv.build_create_table("main.Score", [{"name": "VendorId", "type": "integer"}], foreign_keys=[{"columns": ["VendorId"], "references": "Vendor"}])
    assert "REFERENCES [main].[Vendor] ([VendorId])" in table["sql"]
    with pytest.raises(SqlGlassError, match="exactly one"):
        srv.build_view("dbo.V")

    saved = srv.save_query("ddl/usp_vendors_by_country", proc["sql"], kind="script", tags=["ddl"])
    assert saved["status"] == "applied" and "lint" not in saved
    assert (workspace / "queries/ddl/usp_vendors_by_country.sql").read_text().splitlines()[1] == "-- kind: script"
    assert srv.get_query("usp_vendors_by_country")["kind"] == "script"
    assert srv.lint_library(min_severity="info")["findings"].keys() <= {"by-country"}
    with pytest.raises(SqlGlassError, match="never executes scripts"):
        srv.run_query(query="usp_vendors_by_country")
    assert srv.list_tables()["count"] == 5 and srv.refresh_schema()["tables"] == 4  # nothing was created
