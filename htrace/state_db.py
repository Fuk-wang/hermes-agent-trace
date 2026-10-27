"""Read Hermes session storage into session/call records.

Source of truth: ``state.db`` (SQLite) in a Hermes home or profile home.
Tables used:
  sessions          one row per conversation, with token/cost aggregates
  messages          one row per message; assistant rows carry ``tool_calls``
                    (JSON array) and tool rows carry ``tool_call_id``
  async_delegations parent/child edges for delegated subagents

Everything here is read-only. The database is opened with ``mode=ro`` so a
live Hermes process that holds the WAL cannot be disturbed.
"""

from __future__ import annotations

import datetime as _dt
import json
import os
import sqlite3
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

MAX_ARG_CHARS = 4000
MAX_RESULT_CHARS = 4000
MAX_TEXT_CHARS = 2000


def default_hermes_home() -> Path:
    env = os.environ.get("HERMES_HOME")
    if env:
        return Path(env).expanduser()
    if os.name == "nt":
        local = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
        return Path(local) / "hermes"
    return Path.home() / ".hermes"


def iso(ts: Optional[float]) -> Optional[str]:
    if ts is None:
        return None
    try:
        return _dt.datetime.fromtimestamp(float(ts)).isoformat(timespec="seconds")
    except (ValueError, OSError, OverflowError):
        return None


def discover_homes(explicit: Optional[Iterable[str]] = None) -> List[Tuple[str, Path, Path]]:
    """[(label, home, state_db)] for every Hermes home that has a state.db."""
    found: List[Tuple[str, Path, Path]] = []
    if explicit:
        for raw in explicit:
            home = Path(raw).expanduser()
            db = home / "state.db"
            if db.is_file():
                found.append((home.name or "custom", home, db))
        return found

    home = default_hermes_home()
    db = home / "state.db"
    if db.is_file():
        found.append(("default", home, db))

    prof_root = home / "profiles"
    if prof_root.is_dir():
        for child in sorted(prof_root.iterdir()):
            if not child.is_dir() or child.name.startswith("."):
                continue
            pdb = child / "state.db"
            if pdb.is_file():
                found.append((child.name, child, pdb))
    return found


def _connect(db: Path) -> sqlite3.Connection:
    uri = db.resolve().as_uri() + "?mode=ro"
    conn = sqlite3.connect(uri, uri=True, timeout=5.0)
    conn.row_factory = sqlite3.Row
    return conn


def _clip(text: Optional[str], limit: int) -> Tuple[Optional[str], int, bool]:
    if text is None:
        return None, 0, False
    size = len(text)
    if size <= limit:
        return text, size, False
    return text[:limit], size, True


def _maybe_json(raw: str) -> Any:
    raw = raw.strip()
    if not raw:
        return raw
    if raw[0] in "[{":
        try:
            return json.loads(raw)
        except (ValueError, TypeError):
            return raw
    return raw


def _call_entries(tool_calls_raw: str) -> List[Dict[str, Any]]:
    """Normalise the three tool_call shapes Hermes has stored over time."""
    data = _maybe_json(tool_calls_raw)
    if isinstance(data, dict):
        data = [data]
    if not isinstance(data, list):
        return []
    out: List[Dict[str, Any]] = []
    for item in data:
        if not isinstance(item, dict):
            continue
        fn = item.get("function") if isinstance(item.get("function"), dict) else {}
        name = item.get("name") or fn.get("name") or item.get("tool_name") or "?"
        args = item.get("arguments", fn.get("arguments", item.get("args")))
        if isinstance(args, str):
            args = _maybe_json(args)
        out.append({
            "call_id": item.get("call_id") or item.get("id") or "",
            "name": str(name),
            "args": args,
        })
    return out


def _result_status(result: Optional[str]) -> str:
    """error only when the tool result itself declares failure."""
    if not result:
        return "pending"
    data = _maybe_json(result)
    if isinstance(data, dict):
        if data.get("error"):
            return "error"
        if data.get("success") is False:
            return "error"
        code = data.get("exit_code", data.get("returncode"))
        if isinstance(code, int) and code != 0:
            return "error"
        if data.get("ok") is False:
            return "error"
    return "ok"


def _json_bytes(value: Any) -> int:
    try:
        return len(json.dumps(value, ensure_ascii=False))
    except (TypeError, ValueError):
        return len(str(value))


def load_session(conn: sqlite3.Connection, row: sqlite3.Row, profile: str) -> Dict[str, Any]:
    session_id = row["id"]
    msgs = list(conn.execute(
        """select id, role, content, tool_call_id, tool_calls, tool_name, timestamp,
                  token_count, finish_reason, reasoning, active, compacted
             from messages where session_id = ? order by id""",
        (session_id,),
    ))

    calls: List[Dict[str, Any]] = []
    pending: Dict[str, Dict[str, Any]] = {}
    timeline: List[Dict[str, Any]] = []
    turn_index = -1
    seq = 0

    for msg in msgs:
        role = (msg["role"] or "").lower()
        ts = msg["timestamp"]
        if role == "user":
            turn_index += 1
            text, size, clipped = _clip(msg["content"], MAX_TEXT_CHARS)
            timeline.append({
                "kind": "user",
                "turn": turn_index,
                "at": iso(ts),
                "ts": ts,
                "text": text,
                "chars": size,
                "truncated": clipped,
            })
            continue
        if role == "assistant":
            text = (msg["content"] or "").strip()
            if text:
                body, size, clipped = _clip(text, MAX_TEXT_CHARS)
                timeline.append({
                    "kind": "assistant",
                    "turn": turn_index,
                    "at": iso(ts),
                    "ts": ts,
                    "text": body,
                    "chars": size,
                    "truncated": clipped,
                })
            raw_calls = msg["tool_calls"]
            if raw_calls:
                for entry in _call_entries(raw_calls):
                    seq += 1
                    args_obj = entry["args"]
                    args_text, args_size, args_clipped = _clip(
                        args_obj if isinstance(args_obj, str) else json.dumps(args_obj, ensure_ascii=False),
                        MAX_ARG_CHARS,
                    )
                    call = {
                        "call_id": entry["call_id"],
                        "name": entry["name"],
                        "turn": turn_index,
                        "seq": seq,
                        "started_at": iso(ts),
                        "started_ts": ts,
                        "ended_at": None,
                        "ended_ts": None,
                        "duration_ms": None,
                        "args_raw": args_text,
                        "args": args_obj if not args_clipped else None,
                        "args_bytes": args_size,
                        "args_truncated": args_clipped,
                        "status": "pending",
                        "source": "db",
                        "result": None,
                        "result_bytes": 0,
                        "result_truncated": False,
                        "error": None,
                    }
                    calls.append(call)
                    if entry["call_id"]:
                        pending[entry["call_id"]] = call
            continue
        if role == "tool":
            call_id = msg["tool_call_id"] or ""
            call = pending.pop(call_id, None)
            content = msg["content"]
            body, size, clipped = _clip(content, MAX_RESULT_CHARS)
            status = _result_status(content)
            if call is None:
                continue
            call["ended_at"] = iso(ts)
            call["ended_ts"] = ts
            if ts is not None and call["started_ts"] is not None:
                call["duration_ms"] = max(0, int(round((float(ts) - float(call["started_ts"])) * 1000)))
            call["result"] = body
            call["result_bytes"] = size
            call["result_truncated"] = clipped
            call["status"] = status
            if status == "error":
                call["error"] = "tool reported an error"
            timeline.append({
                "kind": "tool",
                "turn": turn_index,
                "at": iso(ts),
                "ts": ts,
                "name": call["name"],
                "call_id": call_id,
                "duration_ms": call["duration_ms"],
                "status": status,
            })

    for call in calls:
        if call["status"] == "pending" and call["started_ts"] is not None:
            call["status"] = "pending"

    by_tool: Dict[str, Dict[str, Any]] = {}
    for call in calls:
        bucket = by_tool.setdefault(call["name"], {"calls": 0, "errors": 0, "ms": 0, "bytes": 0})
        bucket["calls"] += 1
        bucket["bytes"] += call["args_bytes"] + call["result_bytes"]
        if call["duration_ms"] is not None:
            bucket["ms"] += call["duration_ms"]
        if call["status"] == "error":
            bucket["errors"] += 1

    durations = sorted(c["duration_ms"] for c in calls if c["duration_ms"] is not None)

    tool_names = []
    try:
        tool_names = json.loads(row["tool_names"]) if row["tool_names"] else []
    except (ValueError, TypeError):
        tool_names = []

    tokens = {
        "input": row["input_tokens"] or 0,
        "output": row["output_tokens"] or 0,
        "cache_read": row["cache_read_tokens"] or 0,
        "cache_write": row["cache_write_tokens"] or 0,
        "reasoning": row["reasoning_tokens"] or 0,
    }

    return {
        "id": session_id,
        "profile": profile or row["profile_name"] or "default",
        "title": row["title"] or "",
        "source": row["source"] or "",
        "model": row["model"] or "",
        "started_at": iso(row["started_at"]),
        "ended_at": iso(row["ended_at"]),
        "started_ts": row["started_at"],
        "ended_ts": row["ended_at"],
        "cwd": row["cwd"] or "",
        "git_branch": row["git_branch"] or "",
        "message_count": row["message_count"] or len(msgs),
        "tool_call_count": row["tool_call_count"] or len(calls),
        "parent_session_id": row["parent_session_id"] or "",
        "tokens": tokens,
        "cost_usd": row["estimated_cost_usd"] if row["estimated_cost_usd"] is not None else row["actual_cost_usd"],
        "cost_status": row["cost_status"] or "",
        "tool_names": tool_names if isinstance(tool_names, list) else [],
        "calls": calls,
        "timeline": timeline,
        "stats": {
            "calls": len(calls),
            "errors": sum(1 for c in calls if c["status"] == "error"),
            "pending": sum(1 for c in calls if c["status"] == "pending"),
            "total_ms": sum(durations),
            "p50_ms": durations[len(durations) // 2] if durations else 0,
            "max_ms": durations[-1] if durations else 0,
            "by_tool": by_tool,
        },
    }


def load_home(label: str, home: Path, db: Path, session_limit: Optional[int] = None) -> Dict[str, Any]:
    conn = _connect(db)
    try:
        rows = list(conn.execute(
            """select id, title, source, model, started_at, ended_at, cwd, git_branch,
                      message_count, tool_call_count, input_tokens, output_tokens,
                      cache_read_tokens, cache_write_tokens, reasoning_tokens,
                      estimated_cost_usd, actual_cost_usd, cost_status, tool_names,
                      parent_session_id, profile_name, archived
                 from sessions order by started_at desc"""
        ))
        if session_limit:
            rows = rows[:session_limit]
        sessions = [load_session(conn, row, label) for row in rows]

        delegations: List[Dict[str, Any]] = []
        try:
            for d in conn.execute(
                """select delegation_id, origin_session, parent_session_id, state,
                          dispatched_at, completed_at, task_json
                     from async_delegations order by dispatched_at"""
            ):
                task_title = ""
                try:
                    task = json.loads(d["task_json"] or "{}")
                    task_title = str(task.get("goal") or task.get("title") or "")[:160]
                except (ValueError, TypeError):
                    pass
                delegations.append({
                    "id": d["delegation_id"],
                    "parent_session": d["parent_session_id"] or d["origin_session"] or "",
                    "origin_session": d["origin_session"] or "",
                    "state": d["state"] or "",
                    "dispatched_at": iso(d["dispatched_at"]),
                    "completed_at": iso(d["completed_at"]),
                    "goal": task_title,
                })
        except sqlite3.Error:
            delegations = []
    finally:
        conn.close()
    return {"label": label, "home": str(home), "state_db": str(db), "sessions": sessions, "delegations": delegations}
