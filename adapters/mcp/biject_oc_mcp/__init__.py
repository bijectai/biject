"""biject-oc-mcp — stdio MCP shim for biject's verification-proxy tool surface.

Exposes exactly three tools, every one a thin HTTPS call to the Rust
verification proxy (this process decides nothing):

* ``list_open_queries``     -> ``GET  /queries/open``
* ``get_item_context``      -> ``GET  /items/context``
* ``write_item_correction`` -> read-then-sign-then-``POST /items/write``

Any MCP client works (Claude Code, Codex, OpenCode, ...). See ``README.md``
for configuration, and note that exposing only these tools is ergonomics,
not enforcement — the enforcement bound is at the network layer.
"""

from .server import (
    get_item_context,
    list_open_queries,
    main,
    mcp,
    write_item_correction,
)

__all__ = [
    "get_item_context",
    "list_open_queries",
    "main",
    "mcp",
    "write_item_correction",
]
