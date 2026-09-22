"""Regression tests for the findings accepted from the 2026-09-21 external code review (CODE_REVIEW_RESPONSE.md)."""

import re

import pytest

from sqlglass import ddl
from sqlglass import server as srv
from sqlglass.errors import SqlGlassError
from sqlglass.library import parse_text
from sqlglass.lint import lint
from sqlglass.plan import summarize
from tests.test_core import PLAN


def test_preview_run_binds_each_statement_its_own_params(workspace):
    """Finding #1: the COUNT has no SET clause, so a parameter used only in SET must not be bound to it."""
    srv.list_tables()
    out = srv.preview_write("UPDATE Vendor SET Name = @NewName WHERE VendorId = @Id", run=True, params={"NewName": "Zed", "Id": 2})
    assert out["affected_rows"] == 1 and out["rows"] == [[2, "Globex", "Zed"]]


def test_include_only_missing_index_has_no_broken_ddl():
    """Finding #3."""
    xml = PLAN.replace('<ColumnGroup Usage="EQUALITY"><Column Name="[Status]"/></ColumnGroup>', "")
    mi = summarize([xml])["statements"][0]["missing_indexes"][0]
    assert "suggested_ddl" not in mi and "covering index" in mi["note"]


def test_generated_identifiers_fit_128_chars():
    """Finding #4."""
    long = "T" * 100
    s = ddl.create_table(None, f"dbo.{long}", [{"name": "C" * 60, "type": "int", "default": "0"}, {"name": "D" * 60, "type": "int"}],
                         indexes=[{"columns": ["C" * 60, "D" * 60]}])
    names = re.findall(r"\[([^\]]+)\]", s.sql)
    assert names and max(len(n) for n in names) <= 128 and any(re.search(r"_[0-9a-f]{8}$", n) for n in names)


def test_lowercase_unicode_literal_default():
    """Finding #5."""
    assert parse_text("x", "-- param: @n nvarchar(10) = n'it''s'\nSELECT 1").params[0].default_value == "it's"


def test_flipped_comparison_is_still_non_sargable():
    """Improvement #2."""
    found = {f.rule for f in lint("SELECT PoId FROM dbo.PoHeader WHERE '2026-01-01' = CAST(OrderDate AS date)")}
    assert "non-sargable-predicate" in found


def test_param_also_declared_in_sql_is_refused_early(workspace):
    """Improvement #4."""
    with pytest.raises(SqlGlassError, match="DECLAREd inside the SQL"):
        srv.run_query(sql="DECLARE @c TEXT; SELECT Name FROM Vendor WHERE Country = @c", params={"c": "US"})
