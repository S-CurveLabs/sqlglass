"""Pure logic: lexer, guard, analysis, lint, refactor, builder, plan summary. No database."""

import pytest

from sqlglass import refactor as rf
from sqlglass.builder import BuildError, build_select
from sqlglass.engines.base import Param, declare_bound, declare_literal, literal
from sqlglass.errors import GuardError, SqlGlassError
from sqlglass.library import parse_text
from sqlglass.lint import lint
from sqlglass.plan import summarize
from sqlglass.tsql.analyze import analyze, format_sql, translate
from sqlglass.tsql.guard import check_read_only
from sqlglass.tsql.lexer import declared_variables, split_batches, tokenize, variables


def rules(sql, schema=None, header=None):
    return {f.rule for f in lint(sql, schema, header)}


# ------------------------------------------------------------------ lexer


def test_lexer_roundtrip_and_kinds():
    sql = "SELECT [a]]b], N'it''s', \"q\", @p, @@ROWCOUNT /* x /* nested */ y */ FROM #t -- tail"
    toks = tokenize(sql)
    assert "".join(t.text for t in toks) == sql
    kinds = {t.text: t.kind for t in toks}
    assert kinds["[a]]b]"] == "bracket" and kinds["N'it''s'"] == "string" and kinds["@p"] == "variable"
    assert kinds["/* x /* nested */ y */"] == "comment" and kinds["#t"] == "word"
    assert next(t for t in toks if t.kind == "bracket").ident == "a]b"


def test_batches_split_only_on_standalone_go():
    sql = "SELECT 1 AS GO\nGO\nSELECT 'x\nGO\ny'\ngo 2 -- again\nSELECT go FROM t"
    assert split_batches(sql) == ["SELECT 1 AS GO", "SELECT 'x\nGO\ny'", "SELECT go FROM t"]


def test_variables():
    sql = "DECLARE @a int = 1, @b date; SELECT @a, @B, @c, @@VERSION, '@notme'"
    assert variables(sql) == ["@a", "@b", "@c"]
    assert declared_variables(sql) == ["@a", "@b"]


# ------------------------------------------------------------------ guard

ALLOWED = [
    "SELECT 1",
    "WITH c AS (SELECT 1 AS x) SELECT * FROM c",
    "DECLARE @d date = '2026-01-01'; SET @d = DATEADD(day, 1, @d); SELECT @d",
    "SELECT [update], [into] FROM dbo.t WHERE note = 'DROP TABLE x' -- delete me",
    "(SELECT 1) UNION ALL (SELECT 2)",
    "SELECT * FROM t ORDER BY a OFFSET 10 ROWS FETCH NEXT 10 ROWS ONLY",
    "SELECT REPLACE(name, 'a', 'b') FROM t",
]
REFUSED = [
    "", "-- nothing", "DROP TABLE t", "SELECT 1 DROP TABLE t", "SELECT 1; DELETE FROM t",
    "SELECT * INTO #x FROM t", "WITH c AS (SELECT 1 x) DELETE FROM t", "EXEC sp_who", "sp_who",
    "SELECT 1\nGO\nUPDATE t SET a = 1", "SET NOCOUNT ON; SELECT 1", "SELECT * FROM OPENROWSET('x','y','z')",
    "SELECT NEXT VALUE FOR dbo.seq", "WAITFOR DELAY '00:10'; SELECT 1", "DECLARE @x int = 1", "PRAGMA table_info(t)",
    "SELECT 1; BEGIN TRAN", "select 1 /* */ ; tRuNcAtE table t", "MERGE t USING s ON 1=1 WHEN MATCHED THEN DELETE;",
]


@pytest.mark.parametrize("sql", ALLOWED)
def test_guard_allows(sql):
    check_read_only(sql)


@pytest.mark.parametrize("sql", REFUSED)
def test_guard_refuses(sql):
    with pytest.raises(GuardError):
        check_read_only(sql)


def test_params_cannot_inject():
    p = Param("@v", "", "x'; DROP TABLE t; --")
    assert literal(p.value) == "N'x''; DROP TABLE t; --'"
    assert declare_literal([p]) == "DECLARE @v nvarchar(4000) = N'x''; DROP TABLE t; --';\n"
    assert declare_bound([Param("@n", "int", 5)]) == ("DECLARE @n int = ?;\n", [5])
    for bad in (("@v; DROP", "int"), ("@v", "int; DROP TABLE t"), ("v", "")):
        with pytest.raises(SqlGlassError):
            Param(bad[0], bad[1], 1)


# ------------------------------------------------------------------ analysis

QUERY = """
WITH po AS (SELECT h.PoId, h.VendorId FROM dbo.PoHeader h WHERE h.OrderDate >= @Start)
SELECT v.Name, SUM(l.Amount) AS Total
FROM po JOIN ERP.dbo.Vendor v ON v.VendorId = po.VendorId
LEFT JOIN dbo.PoLine l ON l.PoId = po.PoId
GROUP BY v.Name
"""


def test_analyze_tables_columns_ctes():
    a = analyze(QUERY)
    assert sorted(t.full for t in a.tables) == ["ERP.dbo.Vendor", "dbo.PoHeader", "dbo.PoLine"]
    assert a.ctes == ["po"]
    assert a.columns == {"dbo.poheader": ["OrderDate", "PoId", "VendorId"], "dbo.poline": ["Amount", "PoId"],
                         "dbo.vendor": ["Name", "VendorId"]}
    assert a.output_columns == ["Name", "Total"]


def test_format_and_translate():
    assert format_sql("select a,b from dbo.t where a=1").splitlines()[0] == "SELECT"
    out, notes = translate("SELECT TOP 5 ISNULL(a, 0) FROM t", "postgres")
    assert "LIMIT 5" in out and "COALESCE" in out and notes
    with pytest.raises(SqlGlassError):
        translate("SELECT 1", "klingon")


# ------------------------------------------------------------------ lint


def test_lint_clean_query(erp):
    assert lint(QUERY, erp, ["@Start"]) == []


@pytest.mark.parametrize("sql,rule", [
    ("SELECT * FROM dbo.Vendor", "select-star"),
    ("SELECT Name FROM dbo.Vendor WITH (NOLOCK)", "nolock"),
    ("SELECT PoId FROM dbo.PoHeader WHERE YEAR(OrderDate) = 2026", "non-sargable-predicate"),
    ("SELECT PoId FROM dbo.PoHeader WHERE CAST(OrderDate AS date) = '2026-01-01'", "non-sargable-predicate"),
    ("SELECT Name FROM dbo.Vendor WHERE Name LIKE '%acme'", "leading-wildcard-like"),
    ("SELECT Name FROM dbo.Vendor WHERE VendorId NOT IN (SELECT VendorId FROM dbo.Blocked)", "not-in-subquery"),
    ("SELECT v.Name FROM dbo.Vendor v, dbo.PoHeader h WHERE v.VendorId = h.VendorId", "comma-join"),
    ("SELECT TOP 5 Name FROM dbo.Vendor", "top-without-order-by"),
    ("SELECT v.Name FROM dbo.Vendor v LEFT JOIN dbo.PoHeader h ON h.VendorId = v.VendorId WHERE h.Status = 'OPEN'",
     "left-join-filtered-in-where"),
    ("SELECT PoId FROM dbo.PoHeader WHERE OrderDate BETWEEN '2026-01-01' AND '2026-01-31'", "between-date-end"),
    ("SELECT Name FROM dbo.Vendor ORDER BY 1", "order-by-ordinal"),
    ("SELECT Name FROM Vendor", "missing-schema-prefix"),
    ("WITH a AS (SELECT 1 AS x), b AS (SELECT 2 AS y) SELECT x FROM a", "unused-cte"),
    ("SELECT Name FROM dbo.Vendor UNION SELECT Reason FROM dbo.Blocked", "union-distinct"),
    ("SELEC oops FROM", "parse-error"),
])
def test_lint_rules_fire(sql, rule):
    assert rule in rules(sql)


def test_lint_negatives():
    ok = ("SELECT v.Name FROM dbo.Vendor v LEFT JOIN dbo.PoHeader h ON h.VendorId = v.VendorId AND h.Status = 'OPEN' "
          "WHERE h.PoId IS NULL AND v.VendorId >= 5 AND EXISTS (SELECT * FROM dbo.Blocked b WHERE b.VendorId = v.VendorId)")
    assert rules(ok) == set()
    assert "non-sargable-predicate" not in rules("SELECT Name FROM dbo.Vendor WHERE Name = UPPER(@n)")
    assert "join-without-on" not in rules("SELECT v.Name FROM dbo.Vendor v CROSS JOIN dbo.Blocked b")


def test_lint_schema_and_params(erp):
    found = {f.rule: f.message for f in lint("SELECT v.Nmae FROM dbo.Vendr x JOIN dbo.Vendor v ON v.VendorId = x.Id", erp)}
    assert "Vendor" in found["unknown-table"] and "Did you mean: Name" in found["unknown-column"]
    assert rules("SELECT Name FROM dbo.Vendor WHERE Country = @C", None, ["@Start"]) >= {"undeclared-parameter", "unused-parameter"}
    assert "parameter-declared-twice" in rules("DECLARE @C char(2) = 'US'; SELECT Name FROM dbo.Vendor WHERE Country = @C", None, ["@C"])
    assert "unknown-table" not in rules("SELECT a FROM #tmp", erp) and "unknown-table" not in rules("SELECT a FROM Other.dbo.T", erp)


# ------------------------------------------------------------------ refactor


def test_rename_table_is_token_aware():
    sql = "SELECT Vendor.Name, 'dbo.Vendor' FROM dbo.Vendor -- dbo.Vendor\nJOIN [dbo].[Vendor] v2 ON 1=1 JOIN Vendor v3 ON 1=1"
    out, n = rf.rename_table(sql, "dbo.Vendor", "dbo.Supplier")
    assert n == 3
    assert out == "SELECT Vendor.Name, 'dbo.Vendor' FROM dbo.Supplier -- dbo.Vendor\nJOIN [dbo].[Supplier] v2 ON 1=1 JOIN Supplier v3 ON 1=1"


def test_rename_column_respects_aliases():
    sql = "SELECT v.Name, b.Name, 'Name' FROM dbo.Vendor v JOIN dbo.Blocked b ON b.VendorId = v.VendorId WHERE v.[Name] LIKE 'a%'"
    out, n = rf.rename_column(sql, "dbo.Vendor", "Name", "VendorName")
    assert n == 2 and "v.VendorName, b.Name, 'Name'" in out and "v.[VendorName] LIKE" in out
    out, n = rf.rename_column("SELECT Name, Name AS Name2 FROM dbo.Vendor ORDER BY Name", "dbo.Vendor", "Name", "Full Name")
    assert n == 3 and out == "SELECT [Full Name], [Full Name] AS Name2 FROM dbo.Vendor ORDER BY [Full Name]"
    assert rf.rename_column("SELECT Name FROM dbo.Blocked", "dbo.Vendor", "Name", "X") == ("SELECT Name FROM dbo.Blocked", 0)


def test_extract_parameter():
    sql = "SELECT 1 FROM t WHERE d >= '2026-01-01' AND e < '2026-01-01' -- '2026-01-01'"
    out, n, typ, default = rf.extract_parameter(sql, "'2026-01-01'", "@Start")
    assert (n, typ, default) == (2, "date", "'2026-01-01'")
    assert out == "SELECT 1 FROM t WHERE d >= @Start AND e < @Start -- '2026-01-01'"
    assert rf.extract_parameter("SELECT 1 FROM t WHERE a > 100", "100", "@Min")[2] == "int"
    with pytest.raises(SqlGlassError):
        rf.extract_parameter(sql, "'nope'", "@x")


# ------------------------------------------------------------------ builder


def test_build_select_infers_bridge_join(erp):
    built = build_select(erp, ["dbo.Vendor", "PoLine"], columns=["Vendor.Name"],
                         aggregates=[{"fn": "sum", "column": "PoLine.Amount", "alias": "Total"}],
                         filters=["PoHeader.OrderDate >= @Start", "Country = 'US'"], order_by=["Total desc"], top=10)
    assert built.sql == (
        "SELECT TOP (10)\n    v.[Name],\n    SUM(pl.[Amount]) AS [Total]\n"
        "FROM [dbo].[Vendor] AS v\n"
        "INNER JOIN [dbo].[PoHeader] AS ph ON ph.[VendorId] = v.[VendorId]\n"
        "INNER JOIN [dbo].[PoLine] AS pl ON pl.[PoId] = ph.[PoId]\n"
        "WHERE ph.[OrderDate] >= @Start\n  AND v.[Country] = 'US'\n"
        "GROUP BY v.[Name]\nORDER BY [Total] DESC\n")
    assert any("bridge" in n for n in built.notes) and any("many side" in n for n in built.notes)
    assert lint(built.sql, erp, ["@Start"]) == []


def test_build_select_errors_and_fallback(erp):
    with pytest.raises(BuildError, match="Did you mean: Name"):
        build_select(erp, ["Vendor"], columns=["Nam"])
    with pytest.raises(BuildError, match="Table.VendorId"):
        build_select(erp, ["Vendor", "PoHeader"], columns=["VendorId"])
    with pytest.raises(BuildError, match="No foreign-key path"):
        build_select(erp, ["Vendor", "Calendar"])
    with pytest.raises(SqlGlassError, match="Did you mean: dbo.Vendor"):
        build_select(erp, ["Vendr"])
    built = build_select(erp, ["Vendor", "Blocked"], columns=["Vendor.Name", "Blocked.Reason"], join_type="left")
    assert "LEFT JOIN [dbo].[Blocked] AS b ON b.[VendorId] = v.[VendorId]" in built.sql
    assert any("VERIFY" in n for n in built.notes)


# ------------------------------------------------------------------ library header


def test_header_roundtrip():
    text = ("-- name: Open POs\n-- description: First line.\n--    Second line.\n-- connection: erp\n-- tags: a, b\n"
            "-- param: @Start date = '2026-01-01' | first day\n-- param: @Min int\n\n-- a normal comment\nSELECT 1\n")
    q = parse_text("x/open-pos", text)
    assert (q.name, q.connection, q.tags, q.description) == ("Open POs", "erp", ["a", "b"], "First line.\nSecond line.")
    assert [(p.name, p.type, p.default_value, p.description) for p in q.params] == [("@Start", "date", "2026-01-01", "first day"),
                                                                                   ("@Min", "int", "", "")]
    assert not q.params[1].has_default and q.body == "-- a normal comment\nSELECT 1"
    assert q.render() == text


# ------------------------------------------------------------------ showplan

PLAN = """<ShowPlanXML xmlns="http://schemas.microsoft.com/sqlserver/2004/07/showplan" Version="1.6"><BatchSequence><Batch><Statements>
<StmtSimple StatementText="DECLARE @s date" StatementType="ASSIGN"/>
<StmtSimple StatementText="SELECT v.Name FROM dbo.PoHeader h JOIN dbo.Vendor v ON ..." StatementSubTreeCost="10" StatementEstRows="500">
 <QueryPlan>
  <MissingIndexes><MissingIndexGroup Impact="87.5"><MissingIndex Database="[ERP]" Schema="[dbo]" Table="[PoHeader]">
    <ColumnGroup Usage="EQUALITY"><Column Name="[Status]"/></ColumnGroup>
    <ColumnGroup Usage="INCLUDE"><Column Name="[VendorId]"/></ColumnGroup></MissingIndex></MissingIndexGroup></MissingIndexes>
  <Warnings><PlanAffectingConvert ConvertIssue="Seek Plan" Expression="CONVERT_IMPLICIT(int,[h].[Status],0)"/></Warnings>
  <RelOp PhysicalOp="Hash Match" LogicalOp="Inner Join" EstimateRows="500" EstimatedTotalSubtreeCost="10" EstimateIO="0" EstimateCPU="1.5"><Hash>
    <RelOp PhysicalOp="Clustered Index Scan" LogicalOp="Clustered Index Scan" EstimateRows="900000" EstimatedTotalSubtreeCost="8" EstimateIO="7" EstimateCPU="1" TableCardinality="900000">
      <IndexScan><Object Schema="[dbo]" Table="[PoHeader]" Index="[PK_PoHeader]"/></IndexScan></RelOp>
    <RelOp PhysicalOp="Clustered Index Seek" LogicalOp="Clustered Index Seek" EstimateRows="1" EstimatedTotalSubtreeCost="0.5" EstimateIO="0.4" EstimateCPU="0.1">
      <IndexScan><Object Schema="[dbo]" Table="[Vendor]" Index="[PK_Vendor]"/></IndexScan></RelOp>
  </Hash></RelOp>
 </QueryPlan></StmtSimple></Statements></Batch></BatchSequence></ShowPlanXML>"""


def test_plan_summary():
    out = summarize([PLAN])
    assert out["total_estimated_cost"] == 10 and len(out["statements"]) == 1
    s = out["statements"][0]
    top = s["operators_by_cost"][0]
    assert (top["operator"], top["object"], top["cost_pct"]) == ("Clustered Index Scan", "[dbo].[PoHeader] ([PK_PoHeader])", 80.0)
    assert [o["cost_pct"] for o in s["operators_by_cost"]] == [80.0, 15.0, 5.0]
    assert any("Clustered Index Scan on [dbo].[PoHeader]" in f and "900,000" in f for f in s["findings"])
    assert any("Implicit conversion" in f for f in s["findings"])
    assert s["missing_indexes"][0]["suggested_ddl"] == \
        "CREATE INDEX IX_PoHeader_Status ON [dbo].[PoHeader] ([Status]) INCLUDE ([VendorId])"
