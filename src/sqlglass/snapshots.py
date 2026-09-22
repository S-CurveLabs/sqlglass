"""Automatic before-images of every library write, so any edit can be rolled back
even when it was never committed to git. One JSON file per write."""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timedelta
from pathlib import Path

from .errors import SqlGlassError
from .library import Library
from .schema import home

KEEP = 100


def _bucket(lib: Library) -> Path:
    key = str(lib.root.resolve()).lower()
    return home() / "snapshots" / f"{re.sub(r'[^A-Za-z0-9]+', '-', lib.root.name)[:40]}-{hashlib.sha1(key.encode()).hexdigest()[:8]}"


_STAMP = "%Y%m%d-%H%M%S-%f"
_STAMP_RE = re.compile(r"^(\d{8}-\d{6}-)(\d{3,6})")


def _next_stamp(bucket: Path) -> str:
    """A timestamp later than every snapshot already in the bucket.

    Ids sort as strings and 'latest' is the last one, so two writes inside the same clock tick must
    still get increasing ids (a tie would let the label text decide which one is 'latest')."""
    now = datetime.now()
    newest = None
    for p in bucket.glob("*.json"):
        m = _STAMP_RE.match(p.stem)
        if m:  # ids written before microsecond stamps had 3 fractional digits
            t = datetime.strptime(m[1] + m[2].ljust(6, "0"), _STAMP)
            newest = t if newest is None or t > newest else newest
    if newest is not None and now <= newest:
        now = newest + timedelta(microseconds=1)
    return now.strftime(_STAMP)


def take(lib: Library, query_ids: list[str], label: str) -> str:
    """Record the current text (None = file does not exist yet) of every query about to change."""
    bucket = _bucket(lib)
    bucket.mkdir(parents=True, exist_ok=True)
    stamp = _next_stamp(bucket)
    snap_id = f"{stamp}-{re.sub(r'[^A-Za-z0-9]+', '-', label)[:40].strip('-')}"
    body = {"library": str(lib.root), "label": label, "files": {i: lib.text(i) for i in query_ids}}
    (bucket / f"{snap_id}.json").write_text(json.dumps(body, indent=1), encoding="utf-8")
    for old in sorted(bucket.glob("*.json"))[:-KEEP]:
        old.unlink()
    return snap_id


def listing(lib: Library) -> list[dict]:
    out = []
    for p in sorted(_bucket(lib).glob("*.json"), reverse=True):
        body = json.loads(p.read_text(encoding="utf-8"))
        s = p.stem
        out.append({"id": s, "taken": f"{s[0:4]}-{s[4:6]}-{s[6:8]} {s[9:11]}:{s[11:13]}:{s[13:15]}",
                    "before": body["label"], "queries": sorted(body["files"])})
    return out


def load(lib: Library, snapshot_id: str) -> dict[str, str | None]:
    items = listing(lib)
    if not items:
        raise SqlGlassError(f"No snapshots exist for {lib.root}")
    snap_id = items[0]["id"] if snapshot_id == "latest" else snapshot_id
    path = _bucket(lib) / f"{snap_id}.json"
    if not path.is_file():
        raise SqlGlassError(f"No snapshot '{snapshot_id}'. Use list_snapshots; 'latest' undoes the most recent write.")
    return json.loads(path.read_text(encoding="utf-8"))["files"]
