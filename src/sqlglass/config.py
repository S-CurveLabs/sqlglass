"""Connection + library settings, read from sqlglass.toml. Passwords never live in the file."""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from .errors import SqlGlassError, did_you_mean
from .schema import home

FILE = "sqlglass.toml"
AUTH_MODES = ("windows", "sql", "entra-interactive", "entra-integrated", "entra-default")


@dataclass
class Connection:
    name: str
    engine: str = "mssql"
    server: str = ""
    database: str = ""
    auth: str = "windows"
    user: str = ""
    password_env: str = ""
    driver: str = ""
    encrypt: bool = True
    trust_server_certificate: bool = False
    path: str = ""  # sqlite
    max_rows: int = 200
    timeout_seconds: int = 30
    include_schemas: list[str] = field(default_factory=list)
    exclude_schemas: list[str] = field(default_factory=list)

    def describe(self) -> dict:
        where = self.path if self.engine == "sqlite" else f"{self.server}/{self.database}"
        return {"name": self.name, "engine": self.engine, "target": where, "auth": self.auth if self.engine == "mssql" else "n/a",
                "max_rows": self.max_rows, "timeout_seconds": self.timeout_seconds}


@dataclass
class Config:
    path: Path | None
    connections: dict[str, Connection]
    library: Path | None
    default_connection: str = ""

    def connection(self, name: str = "") -> Connection:
        name = name or self.default_connection
        if not name:
            if len(self.connections) == 1:
                return next(iter(self.connections.values()))
            raise SqlGlassError("No connection was named and there is no default. "
                              f"Available: {', '.join(self.connections) or '(none defined)'}. {_where(self)}")
        hit = next((c for k, c in self.connections.items() if k.lower() == name.lower()), None)
        if hit is None:
            raise SqlGlassError(f"Unknown connection '{name}'.{did_you_mean(name, self.connections)} "
                              f"Available: {', '.join(self.connections) or '(none defined)'}. {_where(self)}")
        return hit


def _where(cfg: Config) -> str:
    return f"Connections are defined in {cfg.path}." if cfg.path else \
        f"No {FILE} was found; copy sqlglass.example.toml to {FILE} in the workspace root and edit it."


def candidates() -> list[Path]:
    out = []
    if os.environ.get("SQLGLASS_CONFIG"):
        out.append(Path(os.environ["SQLGLASS_CONFIG"]))
    if os.environ.get("SQLGLASS_WORKSPACE"):
        out.append(Path(os.environ["SQLGLASS_WORKSPACE"]) / FILE)
    out += [Path.cwd() / FILE, home() / FILE]
    return out


def load() -> Config:
    path = next((p for p in candidates() if p.is_file()), None)
    if path is None:
        return Config(None, {}, None)
    try:
        raw = tomllib.loads(path.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as ex:
        raise SqlGlassError(f"{path} is not valid TOML: {ex}") from None
    connections: dict[str, Connection] = {}
    for name, body in (raw.get("connections") or {}).items():
        if "password" in body:
            raise SqlGlassError(f"{path}: never store a password in the file; use password_env = \"ENV_VAR_NAME\".")
        unknown = set(body) - set(Connection.__dataclass_fields__) | ({"name"} & set(body))
        if unknown:
            raise SqlGlassError(f"{path}: connection '{name}' has unknown setting(s): {', '.join(sorted(unknown))}")
        c = Connection(name=name, **body)
        if c.engine not in ("mssql", "sqlite"):
            raise SqlGlassError(f"{path}: connection '{name}': engine must be 'mssql' or 'sqlite'")
        if c.engine == "mssql" and c.auth not in AUTH_MODES:
            raise SqlGlassError(f"{path}: connection '{name}': auth must be one of {', '.join(AUTH_MODES)}")
        connections[name] = c
    lib = raw.get("library") or {}
    lib_path = Path(lib["path"]) if lib.get("path") else None
    if lib_path is not None and not lib_path.is_absolute():
        lib_path = path.parent / lib_path
    return Config(path, connections, lib_path, lib.get("default_connection", ""))
