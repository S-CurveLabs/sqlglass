"""The tool surface end to end, against the SQLite fixture (schema cache, guard, params, row cap, library, snapshots)."""

import asyncio
import os
import sqlite3

import pytest

from sqlglass import server as srv
from sqlglass.errors import GuardError, SqlGlassError


def test_tools_register_with_descriptions():
    tools = asyncio.run(srv.mcp.list_tools())
    assert len(tools) == 29 and all(t.description and len(t.description) > 40 for t in tools)
    by_name = {t.name: t for t in tools}
    dump = lambda n: by_name[n].annotations.model_dump(by_alias=True) if by_name[n].annotations else {}  # attribute names differ between mcp 1.x and 2.x
    assert dump("run_query")["readOnlyHint"] is True and dump("delete_query")["destructiveHint"] is True
    assert dump("save_query").get("readOnlyHint") is not True


def test_no_config_is_explained(tmp_path, monkeypatch):
    monkeypatch.setenv("SQLGLASS_HOME", str(tmp_path / "home"))
    monkeypatch.delenv("SQLGLASS_CONFIG", raising=False)
    monkeypatch.delenv("SQLGLASS_WORKSPACE", raising=False)
    monkeypatch.chdir(tmp_path)
    assert "sqlglass.example.toml" in srv.list_connections()["setup"]
    with pytest.raises(SqlGlassError, match="No sqlglass.toml"):
        srv.list_tables()


def test_config_rejects_passwords(workspace):
    cfg = workspace / "sqlglass.toml"
    cfg.write_text(cfg.read_text() + 'password = "hunter2"\n')
    with pytest.raises(SqlGlassError, match="never store a password"):
        srv.list_connections()


def test_schema_tools(workspace):
    assert srv.list_connections()["connections"][0]["schema_cached"].startswith("no")
    tables = srv.list_tables()
    assert [t["name"] for t in tables["tables"]] == ["main.Blocked", "main.OpenPo", "main.PoHeader", "main.PoLine", "main.Vendor"]
    assert srv.list_connections()["connections"][0]["schema_cached"].startswith("5 tables")
    d = srv.describe_table("PoHeader")
    assert d["rows"] == 3 and d["primary_key"] == ["PoId"] and d["columns"][0] == {"name": "PoId", "type": "integer", "flags": ["pk"]}
    assert d["references"] == ["(VendorId) -> main.Vendor(VendorId)"] and d["referenced_by"] == ["main.PoLine(PoId) -> (PoId)"]
    assert d["indexes"] == [{"name": "IX_PoHeader_Vendor", "columns": ["VendorId"]}]
    assert srv.search_schema("vendorid")["columns"] == ["main.Blocked.VendorId (integer)", "main.OpenPo.VendorId (integer)",
                                                        "main.PoHeader.VendorId (integer)", "main.Vendor.VendorId (integer)"]
    with pytest.raises(SqlGlassError, match="Did you mean: main.Vendor"):
        srv.describe_table("Vendro")
    path = srv.find_join_path("Vendor", "PoLine")
    assert path["from_and_joins"].count("JOIN") == 2 and any("bridge" in n for n in path["notes"])


def test_schema_is_cached_until_refreshed(workspace):
    srv.list_tables()
    cn = sqlite3.connect(workspace / "erp.db")
    cn.execute("CREATE TABLE Extra (Id INTEGER PRIMARY KEY)")
    cn.commit()
    cn.close()
    assert srv.list_tables()["count"] == 5
    assert srv.refresh_schema()["tables"] == 5 and srv.list_tables()["count"] == 6


def test_run_query_params_cap_and_guard(workspace):
    out = srv.run_query(sql="SELECT Name FROM Vendor WHERE Country = @C ORDER BY Name", params={"C": "US"})
    assert out["rows"] == [["Acme"]] and out["columns"][0]["name"] == "Name" and "truncated" not in out
    out = srv.run_query(sql="SELECT PoLineId FROM PoLine ORDER BY 1")
    assert out["row_count"] == 3 and "first 3 rows" in out["truncated"]
    assert srv.run_query(sql="SELECT PoLineId FROM PoLine ORDER BY 1", max_rows=1)["row_count"] == 1
    assert srv.run_query(sql="SELECT PoLineId FROM PoLine", max_rows=999)["row_count"] == 3  # cannot exceed the connection cap
    hostile = "x' OR 1=1; DROP TABLE Vendor; --"
    assert srv.run_query(sql="SELECT Name FROM Vendor WHERE Name = @n", params={"@n": hostile})["rows"] == []
    for bad in ("DELETE FROM Vendor", "SELECT 1; DROP TABLE Vendor", "PRAGMA writable_schema = 1"):
        with pytest.raises(GuardError):
            srv.run_query(sql=bad)
    with pytest.raises(SqlGlassError, match="no such column"):
        srv.run_query(sql="SELECT Nope FROM Vendor")
    with pytest.raises(SqlGlassError, match="exactly one"):
        srv.run_query()
    assert srv.run_query(sql="SELECT COUNT(*) FROM Vendor")["rows"] == [[3]]


def test_engine_is_read_only_even_without_the_guard(workspace):
    from sqlglass import config
    from sqlglass.engines import EngineError, open_engine
    engine = open_engine(config.load().connection("erp"))
    with pytest.raises(EngineError, match="readonly"):
        engine.run("DELETE FROM Vendor", [], 10)
    engine.close()


def test_sample_profile_explain(workspace):
    s = srv.sample_table("Vendor", rows=2, columns=["Name"])
    assert s["table"] == "main.Vendor" and len(s["rows"]) == 2 and [c["name"] for c in s["columns"]] == ["Name"]
    p = srv.profile_table("Vendor")
    assert p["rows"] == 3
    by_col = {c["column"]: c for c in p["columns"]}
    assert by_col["VendorId"]["unique"] and by_col["Country"]["nulls"] == 1 and by_col["Country"]["distinct"] == 2
    assert (by_col["Name"]["min"], by_col["Name"]["max"]) == ("Acme", "Initech")
    with pytest.raises(SqlGlassError, match="Did you mean: Country"):
        srv.profile_table("Vendor", columns=["County"])
    plan = srv.explain_query(sql="SELECT Name FROM Vendor WHERE VendorId = @id", params={"id": 1})
    assert any("SEARCH" in line for line in plan["plan"])


def test_build_select_runs_on_sqlite(workspace):
    built = srv.build_select(["Vendor", "PoLine"], columns=["Vendor.Name"], aggregates=[{"fn": "SUM", "column": "PoLine.Amount", "alias": "Total"}],
                             filters=["PoHeader.Status = @s"], order_by=["Total DESC"], top=2)
    assert 'LIMIT 2' in built["sql"] and "TOP" not in built["sql"]
    assert srv.run_query(sql=built["sql"], params={"s": "OPEN"})["rows"] == [["Globex", 99.0], ["Acme", 75.5]]


def test_library_lifecycle(workspace):
    body = "SELECT v.Name, h.PoId\nFROM Vendor v\nJOIN PoHeader h ON h.VendorId = v.VendorId\nWHERE h.OrderDate >= '2026-02-01' AND v.Country = @Country"
    preview = srv.save_query("purchasing/pos-since", body, description="POs since a date", tags=["po"], dry_run=True,
                             params=[{"name": "Country", "type": "char(2)"}])
    assert preview["status"].startswith("preview") and not (workspace / "queries").exists()
    saved = srv.save_query("purchasing/pos-since", body, description="POs since a date", tags=["po"],
                           params=[{"name": "Country", "type": "char(2)"}])
    assert saved["status"] == "applied" and (workspace / "queries/purchasing/pos-since.sql").read_text().startswith("-- name: pos-since\n")
    with pytest.raises(SqlGlassError, match="overwrite=true"):
        srv.save_query("purchasing/pos-since", "SELECT 1")

    ex = srv.extract_parameter("pos-since", "'2026-02-01'", "@Since", description="first order date")
    assert ex["replaced"] == 1 and ex["param"] == "@Since date = '2026-02-01' | first order date"
    got = srv.get_query("pos-since")
    assert got["params"] == ["@Country char(2)", "@Since date = '2026-02-01' | first order date"] and "h.OrderDate >= @Since" in got["sql"]
    assert [f for f in got["lint"] if f["severity"] == "error"] == []

    with pytest.raises(SqlGlassError, match="needs a value for: @Country"):
        srv.run_query(query="pos-since")
    with pytest.raises(SqlGlassError, match="has no parameter @bogus"):
        srv.run_query(query="pos-since", params={"bogus": 1, "Country": "US"})
    assert srv.run_query(query="purchasing/pos-since", params={"Country": "US"})["rows"] == [["Acme", 11]]
    assert srv.run_query(query="pos-since", params={"Country": "US", "Since": "2026-01-01"})["row_count"] == 2

    srv.save_query("vendors", "SELECT Name FROM Vendor WHERE Name <> 'Vendor'")
    assert [q["id"] for q in srv.list_queries()["queries"]] == ["purchasing/pos-since", "vendors"]
    assert [q["id"] for q in srv.list_queries(tag="po")["queries"]] == ["purchasing/pos-since"]
    assert [q["id"] for q in srv.list_queries(search="orderdate")["queries"]] == ["purchasing/pos-since"]
    assert [u["id"] for u in srv.find_usage("Vendor", "Name")["used_by"]] == ["purchasing/pos-since", "vendors"]
    assert [u["id"] for u in srv.find_usage("PoHeader")["used_by"]] == ["purchasing/pos-since"]
    assert srv.find_usage("Vendor", "Country")["used_by"][0]["columns_used"] == ["Country", "Name", "VendorId"]


def test_rename_and_restore(workspace):
    srv.save_query("a", "SELECT v.Name FROM main.Vendor v JOIN Blocked b ON b.VendorId = v.VendorId WHERE v.Name <> 'Name'")
    srv.save_query("b", "SELECT Name FROM Vendor ORDER BY Name")
    before = (workspace / "queries/a.sql").read_text()
    preview = srv.rename_in_library("column", "Name", "VendorName", table="Vendor")
    assert preview["status"].startswith("preview") and preview["replacements"] == {"a": 2, "b": 2}
    assert "+SELECT v.VendorName FROM" in preview["diff"]["a"] and (workspace / "queries/a.sql").read_text() == before
    done = srv.rename_in_library("table", "main.Vendor", "main.Supplier", dry_run=False)
    assert done["replacements"] == {"a": 1, "b": 1} and "FROM Supplier ORDER BY" in (workspace / "queries/b.sql").read_text()

    srv.refresh_schema()
    broken = srv.lint_library(min_severity="error")
    assert set(broken["findings"]) == {"a", "b"} and broken["findings"]["b"][0]["rule"] == "unknown-table"

    snaps = srv.list_snapshots()["snapshots"]
    assert snaps[0]["before"].startswith("rename table") and snaps[0]["queries"] == ["a", "b"]
    assert srv.restore_snapshot()["changed"] == ["a", "b"] and (workspace / "queries/a.sql").read_text() == before

    srv.delete_query("b")
    assert not (workspace / "queries/b.sql").exists()
    assert srv.restore_snapshot()["changed"] == ["b"] and (workspace / "queries/b.sql").exists()
    with pytest.raises(SqlGlassError, match="not a valid query id"):
        srv.save_query("../escape", "SELECT 1")


@pytest.mark.mssql
def test_snapshot_ids_increase_within_one_clock_tick(workspace, monkeypatch):
    """Fast machines make several library writes per millisecond; 'latest' must still be the last one."""
    from datetime import datetime

    from sqlglass import snapshots

    frozen = datetime(2026, 9, 21, 12, 0, 0, 500000)

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return frozen

    monkeypatch.setattr(snapshots, "datetime", Clock)
    srv.save_query("a", "SELECT Name FROM Vendor")
    srv.save_query("b", "SELECT Name FROM Vendor ORDER BY Name")
    srv.delete_query("b")
    ids = [s["id"] for s in srv.list_snapshots()["snapshots"]]
    assert len(set(ids)) == 3 and ids == sorted(ids, reverse=True)
    assert srv.restore_snapshot()["changed"] == ["b"] and (workspace / "queries/b.sql").exists()


def test_snapshot_ids_stay_after_old_millisecond_ids(workspace):
    from datetime import datetime

    from sqlglass import snapshots
    from sqlglass.library import Library

    lib = Library(workspace / "queries")
    bucket = snapshots._bucket(lib)
    bucket.mkdir(parents=True, exist_ok=True)
    future = datetime.now().replace(year=datetime.now().year + 1).strftime("%Y%m%d-%H%M%S-%f")[:-3]
    (bucket / f"{future}-save-zzz.json").write_text('{"label": "old", "files": {}}', encoding="utf-8")
    assert snapshots._next_stamp(bucket) > future


@pytest.mark.skipif(not os.environ.get("SQLGLASS_TEST_CONNECTION"), reason="set SQLGLASS_TEST_CONNECTION to a configured mssql connection")
def test_live_sql_server():
    name = os.environ["SQLGLASS_TEST_CONNECTION"]
    assert srv.refresh_schema(name)["tables"] >= 0
    out = srv.run_query(sql="SELECT @n + 1 AS n, DB_NAME() AS db", params={"n": 41}, connection=name)
    assert out["rows"][0][0] == 42
    plan = srv.explain_query(sql="SELECT name FROM sys.objects WHERE object_id = @id", params={"id": 1}, connection=name)
    assert plan["statements"][0]["operator_count"] >= 1
    with pytest.raises(GuardError):
        srv.run_query(sql="CREATE TABLE #t (a int); SELECT 1", connection=name)
