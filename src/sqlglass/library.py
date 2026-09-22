"""The query library: a folder of .sql files, each opening with a small header block.

    -- name: Open POs by vendor
    -- description: Open purchase-order value per vendor since a start date.
    -- kind: script          (only for DDL scripts kept for a human to run; omit for queries)
    -- connection: erp
    -- tags: purchasing, monthly
    -- param: @StartDate date = '2026-01-01' | first order date to include
    SELECT ...

The files stay plain SQL that runs unchanged in SSMS / Azure Data Studio (declare the
params there); git is the history. A query's id is its path without '.sql'.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .errors import SqlGlassError, did_you_mean

KEYS = ("name", "description", "kind", "connection", "tags", "owner", "param")
_HEADER = re.compile(rf"^--\s*({'|'.join(KEYS)})\s*:\s?(.*)$", re.I)
_CONTINUATION = re.compile(r"^--\s{3,}(\S.*)$")
_PARAM = re.compile(r"^(@[A-Za-z_][A-Za-z0-9_]*)\s*([^=|]*?)\s*(?:=\s*([^|]*?))?\s*(?:\|\s*(.*))?$")
_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 _.-]*(/[A-Za-z0-9][A-Za-z0-9 _.-]*)*$")


class LibraryError(SqlGlassError):
    pass


@dataclass
class ParamDef:
    name: str
    type: str = ""
    default: str = ""  # as written, e.g. '2026-01-01' (with quotes) or 100 or NULL
    description: str = ""

    @property
    def has_default(self) -> bool:
        return self.default != ""

    @property
    def default_value(self) -> Any:
        d = self.default.strip()
        if d.upper() == "NULL":
            return None
        if len(d) >= 2 and d[0] in "'Nn" and d.endswith("'"):
            return d[d.index("'") + 1:-1].replace("''", "'")
        for cast in (int, float):
            try:
                return cast(d)
            except ValueError:
                pass
        return d

    def render(self) -> str:
        out = self.name + (f" {self.type}" if self.type else "")
        if self.has_default:
            out += f" = {self.default}"
        return out + (f" | {self.description}" if self.description else "")


@dataclass
class Query:
    id: str
    name: str = ""
    description: str = ""
    connection: str = ""
    tags: list[str] = field(default_factory=list)
    owner: str = ""
    kind: str = ""  # '' = a runnable query; 'script' = DDL kept for a human to run (never linted as a query, never executed)
    params: list[ParamDef] = field(default_factory=list)
    body: str = ""

    def render(self) -> str:
        lines = [f"-- name: {self.name or self.id.rsplit('/', 1)[-1]}"]
        if self.description:
            first, *rest = self.description.splitlines()
            lines.append(f"-- description: {first}")
            lines += [f"--    {r.strip()}" for r in rest if r.strip()]
        if self.kind:
            lines.append(f"-- kind: {self.kind}")
        if self.connection:
            lines.append(f"-- connection: {self.connection}")
        if self.tags:
            lines.append(f"-- tags: {', '.join(self.tags)}")
        if self.owner:
            lines.append(f"-- owner: {self.owner}")
        lines += [f"-- param: {p.render()}" for p in self.params]
        return "\n".join(lines) + "\n\n" + self.body.strip() + "\n"

    def card(self) -> dict:
        out = {"id": self.id, "name": self.name, "kind": self.kind, "description": self.description, "connection": self.connection,
               "tags": self.tags, "params": [p.render() for p in self.params]}
        return {k: v for k, v in out.items() if v}


def parse_text(query_id: str, text: str) -> Query:
    q = Query(query_id)
    lines = text.lstrip("﻿").splitlines()
    i, last = 0, ""
    description: list[str] = []
    while i < len(lines):
        line = lines[i].rstrip()
        m = _HEADER.match(line)
        cont = _CONTINUATION.match(line) if last == "description" else None
        if m:
            last, value = m.group(1).lower(), m.group(2).strip()
            if last == "description":
                description.append(value)
            elif last == "tags":
                q.tags = [t.strip() for t in value.split(",") if t.strip()]
            elif last == "param":
                pm = _PARAM.match(value)
                if not pm:
                    raise LibraryError(f"{query_id}: cannot read header line '{line}'. "
                                       f"Expected: -- param: @Name type = default | description")
                q.params.append(ParamDef(pm.group(1), pm.group(2).strip(), (pm.group(3) or "").strip(), (pm.group(4) or "").strip()))
            else:
                setattr(q, last, value)
        elif cont:
            description.append(cont.group(1).strip())
        else:
            break
        i += 1
    q.description = "\n".join(description)
    q.body = "\n".join(lines[i:]).strip()
    q.name = q.name or query_id.rsplit("/", 1)[-1]
    return q


def slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-") or "query"


class Library:
    def __init__(self, root: Path | None):
        if root is None:
            raise LibraryError("No query library is configured. Add to sqlglass.toml:  [library]  path = \"queries\"")
        self.root = root

    def path_of(self, query_id: str) -> Path:
        query_id = query_id.replace("\\", "/").removesuffix(".sql")
        if not _ID.match(query_id) or ".." in query_id:
            raise LibraryError(f"'{query_id}' is not a valid query id. Use letters, digits, '-', '_' and '/' for folders, "
                               f"e.g. purchasing/open-pos-by-vendor")
        return self.root / (query_id + ".sql")

    def ids(self) -> list[str]:
        if not self.root.is_dir():
            return []
        return sorted(p.relative_to(self.root).as_posix()[:-4] for p in self.root.rglob("*.sql"))

    def all(self) -> list[Query]:
        return [self.read(i) for i in self.ids()]

    def read(self, query_id: str) -> Query:
        return parse_text(query_id, self.path_of(query_id).read_text(encoding="utf-8"))

    def get(self, id_or_name: str) -> Query:
        wanted = id_or_name.replace("\\", "/").removesuffix(".sql").lower()
        ids = self.ids()
        hit = next((i for i in ids if i.lower() == wanted), None) or \
            next((i for i in ids if i.lower().rsplit("/", 1)[-1] == wanted), None)
        if hit:
            return self.read(hit)
        queries = self.all()
        by_name = [q for q in queries if q.name.lower() == wanted]
        if len(by_name) == 1:
            return by_name[0]
        raise LibraryError(f"No query '{id_or_name}' in {self.root}."
                           f"{did_you_mean(id_or_name, ids + [q.name for q in queries])} Use list_queries to browse.")

    def text(self, query_id: str) -> str | None:
        p = self.path_of(query_id)
        return p.read_text(encoding="utf-8") if p.is_file() else None

    def write(self, query_id: str, text: str | None) -> None:
        p = self.path_of(query_id)
        if text is None:
            p.unlink(missing_ok=True)
            return
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8", newline="\n")
