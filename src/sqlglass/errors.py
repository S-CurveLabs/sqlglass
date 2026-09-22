"""One base class for every failure the assistant is meant to read and act on.

MCP servers only forward the text of a deliberate ``ToolError``; anything else is
reported as an anonymous crash. Our errors carry the guidance ("Did you mean ...",
"run refresh_schema first"), so they must be ToolErrors. The import is optional so
the SQL core stays usable as a plain library without the mcp package.
"""

import difflib

try:  # mcp 2.x
    from mcp.server.mcpserver.exceptions import ToolError as _Base
except ImportError:
    try:  # mcp 1.x
        from mcp.server.fastmcp.exceptions import ToolError as _Base
    except ImportError:  # no mcp at all
        _Base = Exception


class SqlGlassError(_Base):
    pass


class GuardError(SqlGlassError):
    """The statement is not a pure read and will not be sent to the server."""


def did_you_mean(name: str, candidates) -> str:
    close = difflib.get_close_matches(name.lower(), [c.lower() for c in candidates], n=3, cutoff=0.5)
    by_lower = {c.lower(): c for c in candidates}
    return f" Did you mean: {', '.join(by_lower[c] for c in close)}?" if close else ""
