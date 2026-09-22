"""The schema model: what an engine introspects, what gets cached, what lint/builder read.

The cache is what makes the offline tools schema-aware: once ``refresh_schema`` has
run, lint can flag unknown columns and the builder can infer joins without touching
the database again.
"""

from __future__ import annotations

import fnmatch
import json
import os
import re
import heapq
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path

from .errors import SqlGlassError, did_you_mean


_AUDIT = re.compile(r"(edited|created|modified|updated|entered|approved|deleted)by|lastedit|createdat|modifiedat", re.I)


def home() -> Path:
    base = os.environ.get("SQLGLASS_HOME") or os.path.join(os.environ.get("LOCALAPPDATA") or Path.home(), "sqlglass")
    return Path(base)


@dataclass
class Column:
    name: str
    type: str
    nullable: bool = True
    identity: bool = False
    computed: bool = False
    description: str = ""


@dataclass
class Index:
    name: str
    columns: list[str]
    included: list[str] = field(default_factory=list)
    unique: bool = False
    primary_key: bool = False


@dataclass
class Table:
    schema: str
    name: str
    kind: str = "table"  # table | view
    rows: int | None = None
    description: str = ""
    columns: list[Column] = field(default_factory=list)
    indexes: list[Index] = field(default_factory=list)

    @property
    def key(self) -> str:
        return f"{self.schema.lower()}.{self.name.lower()}"

    @property
    def full(self) -> str:
        return f"{self.schema}.{self.name}"

    @property
    def primary_key(self) -> list[str]:
        return next((i.columns for i in self.indexes if i.primary_key), [])

    def column(self, name: str) -> Column | None:
        return next((c for c in self.columns if c.name.lower() == name.lower()), None)


@dataclass
class ForeignKey:
    name: str
    from_table: str  # "schema.name"
    from_columns: list[str]
    to_table: str
    to_columns: list[str]


@dataclass
class Schema:
    connection: str
    database: str = ""
    fetched: str = ""
    tables: dict[str, Table] = field(default_factory=dict)
    foreign_keys: list[ForeignKey] = field(default_factory=list)
    default_schema: str = "dbo"
    dialect: str = "tsql"

    # ------------------------------------------------------------------ lookup

    def find(self, name: str) -> Table | None:
        """Resolve 'Vendor', 'dbo.Vendor', '[dbo].[Vendor]' or 'db.dbo.Vendor'; None when absent or ambiguous."""
        parts = [p.strip('[]"') for p in re.split(r"\.(?![^\[]*\])", name.strip())]
        if len(parts) >= 2:
            return self.tables.get(f"{parts[-2].lower() or self.default_schema}.{parts[-1].lower()}")
        hits = [t for t in self.tables.values() if t.name.lower() == parts[0].lower()]
        if len(hits) > 1:
            hits = [t for t in hits if t.schema.lower() == self.default_schema] or hits
        return hits[0] if len(hits) == 1 else None

    def require(self, name: str) -> Table:
        t = self.find(name)
        if t is None:
            bare = name.split(".")[-1].strip('[]"')
            same = [x.full for x in self.tables.values() if x.name.lower() == bare.lower()]
            if len(same) > 1:
                raise SqlGlassError(f"'{name}' is ambiguous: it exists as {', '.join(same)}. Use the schema-qualified name.")
            raise SqlGlassError(f"Table or view '{name}' is not in the cached schema for connection "
                              f"'{self.connection}'.{did_you_mean(bare, sorted({x.full for x in self.tables.values()}))}"
                              f" Use search_schema to look for it, or refresh_schema if it is new.")
        return t

    def search(self, pattern: str, limit: int = 50) -> dict:
        pat = pattern.lower() if any(ch in pattern for ch in "*?") else f"*{pattern.lower()}*"
        tables = [t.full for t in self.tables.values() if fnmatch.fnmatch(t.name.lower(), pat)]
        columns = [f"{t.full}.{c.name} ({c.type})" for t in self.tables.values() for c in t.columns
                   if fnmatch.fnmatch(c.name.lower(), pat)]
        described = [t.full for t in self.tables.values() if t.description and fnmatch.fnmatch(t.description.lower(), pat)
                     and t.full not in tables]
        return {"tables": sorted(tables)[:limit], "columns": sorted(columns)[:limit],
                "tables_by_description": sorted(described)[:limit],
                "truncated": len(tables) > limit or len(columns) > limit}

    # ------------------------------------------------------------------ joins

    def relationships(self, table: Table) -> dict:
        return {
            "references": [f"{quote_cols(fk.from_columns)} -> {self._name(fk.to_table)}{quote_cols(fk.to_columns)}"
                           for fk in self.foreign_keys if fk.from_table == table.key],
            "referenced_by": [f"{self._name(fk.from_table)}{quote_cols(fk.from_columns)} -> {quote_cols(fk.to_columns)}"
                              for fk in self.foreign_keys if fk.to_table == table.key],
        }

    def _name(self, key: str) -> str:
        t = self.tables.get(key)
        return t.full if t else key

    def join_path(self, start: str, goal: str) -> list[tuple[ForeignKey, bool]] | None:
        """Cheapest chain of foreign keys from start to goal. Each hop is (fk, forward?).
        Audit-trail keys (LastEditedBy, CreatedBy ...) cost far more than a plain hop, so
        Customers -> Orders -> OrderLines beats Customers -> People -> OrderLines."""
        if start == goal:
            return []
        edges: dict[str, list[tuple[str, ForeignKey, bool]]] = {}
        for fk in self.foreign_keys:
            edges.setdefault(fk.from_table, []).append((fk.to_table, fk, True))
            edges.setdefault(fk.to_table, []).append((fk.from_table, fk, False))
        best: dict[str, float] = {start: 0}
        prev: dict[str, tuple[str, ForeignKey, bool]] = {}
        heap = [(0.0, start)]
        while heap:
            cost, cur = heapq.heappop(heap)
            if cur == goal:
                path = []
                while cur != start:
                    cur, fk, forward = prev[cur]
                    path.append((fk, forward))
                return path[::-1]
            if cost > best.get(cur, float("inf")):
                continue
            for nxt, fk, forward in edges.get(cur, []):
                c = cost + (10 if _AUDIT.search(" ".join(fk.from_columns)) else 1) + (5 if fk.from_table == fk.to_table else 0)
                if c < best.get(nxt, float("inf")):
                    best[nxt], prev[nxt] = c, (cur, fk, forward)
                    heapq.heappush(heap, (c, nxt))
        return None

    def name_based_join(self, a: Table, b: Table) -> list[tuple[str, str]]:
        """Fallback when no FK is declared (common in reporting DBs): b's PK columns that also exist in a, or vice versa."""
        for parent, child in ((b, a), (a, b)):
            pk = parent.primary_key
            if pk and all(child.column(c) for c in pk):
                pairs = [(c, c) for c in pk]
                return pairs if parent is b else [(y, x) for x, y in pairs]
        return []

    # ------------------------------------------------------------------ cache

    def to_json(self) -> str:
        return json.dumps(asdict(self), indent=1)

    @classmethod
    def from_json(cls, text: str) -> "Schema":
        raw = json.loads(text)
        tables = {}
        for key, t in raw["tables"].items():
            t["columns"] = [Column(**c) for c in t["columns"]]
            t["indexes"] = [Index(**i) for i in t["indexes"]]
            tables[key] = Table(**t)
        return cls(raw["connection"], raw.get("database", ""), raw.get("fetched", ""), tables,
                   [ForeignKey(**f) for f in raw["foreign_keys"]], raw.get("default_schema", "dbo"), raw.get("dialect", "tsql"))


def quote_cols(cols: list[str]) -> str:
    return "(" + ", ".join(cols) + ")"


def cache_path(connection: str) -> Path:
    return home() / "schema" / (re.sub(r"[^A-Za-z0-9_.-]+", "-", connection) + ".json")


def save(schema: Schema) -> Path:
    schema.fetched = datetime.now().isoformat(timespec="seconds")
    path = cache_path(schema.connection)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(schema.to_json(), encoding="utf-8")
    return path


def load(connection: str) -> Schema | None:
    path = cache_path(connection)
    return Schema.from_json(path.read_text(encoding="utf-8")) if path.exists() else None
