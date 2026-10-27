#!/usr/bin/env python
"""htrace hook — Hermes shell hook sink.

Hermes runs a shell hook per event with ONE JSON object on stdin:

    {"hook_event_name": "post_tool_call", "tool_name": "terminal",
     "args": {...}, "result": "...", "duration_ms": 42,
     "session_id": "...", "tool_call_id": "..."}

This script appends that payload (plus a receive timestamp) as one JSON line,
so htrace can merge real-time events with state.db history. It never prints to
stdout: on a blocking event stdout is a directive channel, so silence is the
only safe answer.

Configure the output directory with HTRACE_DIR (default: <hermes home>/htrace).
"""

from __future__ import annotations

import datetime as _dt
import json
import os
import sys
from pathlib import Path

MAX_RESULT_CHARS = 8000
MAX_ARG_CHARS = 4000


def htrace_dir() -> Path:
    explicit = os.environ.get("HTRACE_DIR")
    if explicit:
        return Path(explicit).expanduser()
    home = os.environ.get("HERMES_HOME")
    if home:
        return Path(home).expanduser() / "htrace"
    if os.name == "nt":
        local = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
        return Path(local) / "hermes" / "htrace"
    return Path.home() / ".hermes" / "htrace"


def clip(value, limit):
    if not isinstance(value, str):
        return value
    return value if len(value) <= limit else value[:limit] + f"…[truncated {len(value)} chars]"


def main() -> int:
    raw = sys.stdin.read()
    if not raw.strip():
        return 0
    try:
        payload = json.loads(raw)
    except ValueError:
        return 0
    if not isinstance(payload, dict):
        return 0

    now = _dt.datetime.now()
    payload["_htrace_ts"] = now.timestamp()
    payload["_htrace_received"] = now.isoformat(timespec="milliseconds")
    payload["_htrace_pid"] = os.getpid()

    if isinstance(payload.get("result"), str):
        payload["result"] = clip(payload["result"], MAX_RESULT_CHARS)
    if isinstance(payload.get("args"), str):
        payload["args"] = clip(payload["args"], MAX_ARG_CHARS)

    try:
        target = htrace_dir()
        target.mkdir(parents=True, exist_ok=True)
        path = target / f"events-{now.strftime('%Y%m%d')}.jsonl"
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(payload, ensure_ascii=False, default=str) + "\n")
    except OSError:
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
