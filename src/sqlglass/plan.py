"""Boil a SHOWPLAN_XML document down to what matters when tuning a query.

A raw plan is tens of KB of XML; the model needs the expensive operators, the
scans, the optimizer's own warnings and its missing-index suggestions.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET

from .errors import SqlGlassError

NS = "{http://schemas.microsoft.com/sqlserver/2004/07/showplan}"
SCANS = {"Table Scan", "Clustered Index Scan", "Index Scan", "Columnstore Index Scan"}
TOP_OPERATORS = 8


def summarize(xml_docs: list[str]) -> dict:
    statements: list[dict] = []
    for doc in xml_docs:
        try:
            root = ET.fromstring(doc)
        except ET.ParseError as ex:
            raise SqlGlassError(f"The server returned a plan that is not valid XML: {ex}") from None
        for stmt in root.iter(f"{NS}StmtSimple"):
            if stmt.find(f"{NS}QueryPlan") is not None:
                statements.append(_statement(stmt))
    if not statements:
        raise SqlGlassError("The plan contains no query statement (only DECLARE/SET?).")
    return {"statements": statements, "total_estimated_cost": round(sum(s["estimated_cost"] for s in statements), 4)}


def _statement(stmt: ET.Element) -> dict:
    cost = _f(stmt.get("StatementSubTreeCost"))
    plan = stmt.find(f"{NS}QueryPlan")
    ops = []
    for op in plan.iter(f"{NS}RelOp"):
        # Per-operator IO+CPU is the honest own-cost. "Subtree minus children" is NOT: on the inner side of a
        # Nested Loops (e.g. an RLS predicate's sub-plan) SQL Server repeats the parent's accumulated cost downward.
        if op.get("EstimateIO") is not None or op.get("EstimateCPU") is not None:
            own = _f(op.get("EstimateIO")) + _f(op.get("EstimateCPU"))
        else:
            own = max(0.0, _f(op.get("EstimatedTotalSubtreeCost")) - sum(_f(c.get("EstimatedTotalSubtreeCost")) for c in _child_ops(op)))
        ops.append({"operator": op.get("PhysicalOp"), "logical": op.get("LogicalOp"), "object": _object(op),
                    "estimated_rows": round(_f(op.get("EstimateRows")), 1), "own_cost": own,
                    "table_rows": _f(op.get("TableCardinality")) or None})
    findings: list[str] = []
    for o in ops:
        pct = min(100.0, 100 * o["own_cost"] / cost) if cost else 0
        o["cost_pct"] = round(pct, 1)
        if o["operator"] in SCANS and pct >= 10:
            rows = f" (~{int(o['table_rows']):,} rows in the table)" if o["table_rows"] else ""
            findings.append(f"{o['operator']} on {o['object']}{rows} is {pct:.0f}% of the cost: no usable index for the "
                            f"filter/join, or the predicate is not sargable.")
        if o["operator"] == "Key Lookup" or (o["operator"] == "RID Lookup"):
            if pct >= 5:
                findings.append(f"{o['operator']} on {o['object']} ({pct:.0f}%): the index used does not cover the selected "
                                f"columns; select fewer columns or add INCLUDE columns.")
        if o["operator"] == "Sort" and pct >= 15:
            findings.append(f"Sort is {pct:.0f}% of the cost: an index in the ORDER BY / GROUP BY / join order would avoid it.")
    for w in plan.iter(f"{NS}Warnings"):
        for conv in w.iter(f"{NS}PlanAffectingConvert"):
            findings.append(f"Implicit conversion hurts {conv.get('ConvertIssue', 'the plan')}: {conv.get('Expression')}. "
                            f"Compare the column with a value of its own type.")
        if w.get("NoJoinPredicate") in ("1", "true"):
            findings.append("A join has NO join predicate (accidental cross join).")
        if w.find(f"{NS}ColumnsWithNoStatistics") is not None:
            findings.append("Some columns have no statistics; row estimates may be badly off.")
        if w.find(f"{NS}SpillToTempDb") is not None:
            findings.append("An operator spills to tempdb (memory grant too small for the actual rows).")
    top = sorted(ops, key=lambda o: o["own_cost"], reverse=True)[:TOP_OPERATORS]
    out = {
        "statement": " ".join((stmt.get("StatementText") or "").split())[:300],
        "estimated_cost": round(cost, 4),
        "estimated_rows": round(_f(stmt.get("StatementEstRows")), 1),
        "operators_by_cost": [{k: v for k, v in o.items() if k in ("operator", "logical", "object", "estimated_rows", "cost_pct") and v}
                              for o in top if o["cost_pct"] > 0 or len(ops) <= TOP_OPERATORS],
        "operator_count": len(ops),
        "findings": list(dict.fromkeys(findings)),
        "missing_indexes": _missing(plan),
    }
    if stmt.get("StatementOptmEarlyAbortReason") == "TimeOut":
        out["findings"].append("The optimizer timed out before finding its best plan; the query is complex enough to simplify.")
    return out


def _child_ops(op: ET.Element) -> list[ET.Element]:
    """RelOps one level down (they are nested inside an operator-specific element, not directly)."""
    out, stack = [], [c for c in op if c.tag != f"{NS}RelOp"]
    while stack:
        el = stack.pop()
        for c in el:
            (out if c.tag == f"{NS}RelOp" else stack).append(c)
    return out


def _object(op: ET.Element) -> str:
    stack = [c for c in op]
    while stack:
        el = stack.pop(0)
        if el.tag == f"{NS}RelOp":
            continue
        if el.tag == f"{NS}Object":
            name = ".".join(p for p in (el.get("Schema"), el.get("Table")) if p)
            return name + (f" ({el.get('Index')})" if el.get("Index") else "")
        stack.extend(el)
    return ""


def _missing(plan: ET.Element) -> list[dict]:
    out = []
    for group in plan.iter(f"{NS}MissingIndexGroup"):
        for mi in group.iter(f"{NS}MissingIndex"):
            cols = {g.get("Usage"): [c.get("Name") for c in g.iter(f"{NS}Column")] for g in mi.iter(f"{NS}ColumnGroup")}
            keys = cols.get("EQUALITY", []) + cols.get("INEQUALITY", [])
            table = f"{mi.get('Schema')}.{mi.get('Table')}"
            if not keys:  # an INCLUDE-only suggestion has nothing to key on; the DDL would be invalid
                out.append({"table": table, "estimated_improvement_pct": round(_f(group.get("Impact")), 1),
                            "note": f"The optimizer wants a covering index on {table} for columns {', '.join(cols.get('INCLUDE', []))} "
                                    f"but proposes no key columns; a DBA has to choose the key."})
                continue
            ddl = f"CREATE INDEX IX_{mi.get('Table', '').strip('[]')}_{'_'.join(k.strip('[]') for k in keys)} ON {table} ({', '.join(keys)})"
            if cols.get("INCLUDE"):
                ddl += f" INCLUDE ({', '.join(cols['INCLUDE'])})"
            out.append({"table": table, "estimated_improvement_pct": round(_f(group.get("Impact")), 1), "suggested_ddl": ddl,
                        "note": "Optimizer suggestion only; for a DBA to evaluate. This server cannot create indexes."})
    return out


def _f(v: str | None) -> float:
    try:
        return float(v) if v else 0.0
    except ValueError:
        return 0.0
