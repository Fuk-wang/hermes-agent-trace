"""Static index of tool implementations in a Hermes source tree.

Hermes tools register themselves at import time:

    registry.register(name="read_file", toolset="file", schema=READ_FILE_SCHEMA,
                      handler=_handle_read_file, ...)

So a regex over ``tools/*.py`` plus a ``def`` lookup gives a
``tool name -> file:line -> handler`` map without importing anything. That map
is what lets the viewer jump from a recorded call to the code that ran it,
which is the anti-hallucination part of the trace.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Dict, List, Optional

_REGISTER = re.compile(r"registry\.register\s*\(")
_NAME_LITERAL = re.compile(r"""name\s*=\s*(?:"([^"]+)"|'([^']+)'|([A-Za-z_][\w.]*))""")
_TOOLSET = re.compile(r"""toolset\s*=\s*(?:"([^"]+)"|'([^']+)'|([A-Za-z_][\w.]*))""")
_HANDLER = re.compile(r"""handler\s*=\s*([A-Za-z_][\w.]*)""")
_SCHEMA_NAME = re.compile(r"""["']name["']\s*:\s*["']([a-z0-9_]+)["']""")


def _line_of(text: str, offset: int) -> int:
    return text.count("\n", 0, offset) + 1


def _def_line(text: str, func: str) -> Optional[int]:
    m = re.search(rf"^\s*(?:async\s+)?def\s+{re.escape(func)}\s*\(", text, re.MULTILINE)
    return _line_of(text, m.start()) if m else None


def _first(*groups: str) -> str:
    for g in groups:
        if g:
            return g
    return ""


def build_index(source_root: str | Path, default_hermes_home: Optional[str] = None) -> Dict[str, Any]:
    root = Path(source_root).expanduser()
    tools_dir = root / "tools"
    index: Dict[str, Any] = {}
    if not tools_dir.is_dir():
        return {"root": str(root), "tools": index, "note": "no tools/ directory found"}

    for py in sorted(tools_dir.glob("*.py")):
        try:
            text = py.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for m in _REGISTER.finditer(text):
            window = text[m.start(): m.start() + 2000]
            n = _NAME_LITERAL.search(window)
            if not n:
                continue
            name = _first(n.group(1) or "", n.group(2) or "", n.group(3) or "")
            if not name or name.startswith("_"):
                # Registration inside a loop over names (browser tools): fall back
                # to the schema literals defined in the same file.
                continue
            toolset = _first(*[g or "" for g in _TOOLSET.search(window).groups()]) if _TOOLSET.search(window) else ""
            hm = _HANDLER.search(window)
            handler = hm.group(1) if hm else ""
            handler_line = _def_line(text, handler) if handler else None
            index[name] = {
                "file": f"tools/{py.name}",
                "line": _line_of(text, m.start()),
                "handler": handler,
                "handler_line": handler_line,
                "toolset": toolset,
                "module": f"tools.{py.stem}",
            }

    # Schema-literal pass picks up names registered indirectly (loops over a dict).
    for py in sorted(tools_dir.glob("*.py")):
        try:
            text = py.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for m in _SCHEMA_NAME.finditer(text):
            name = m.group(1)
            if name in index:
                continue
            index[name] = {
                "file": f"tools/{py.name}",
                "line": _line_of(text, m.start()),
                "handler": "",
                "handler_line": None,
                "toolset": "",
                "module": f"tools.{py.stem}",
                "inferred": True,
            }

    return {
        "root": str(root),
        "tools": index,
        "default_hermes_home": default_hermes_home,
    }
