"""Shell-hook capture reader.

A Hermes shell hook receives one JSON object on stdin per event and can be
installed for ``pre_tool_call`` / ``post_tool_call`` / ``post_api_request`` /
``on_session_start`` / ``subagent_stop``. ``hooks/htrace_hook.py`` appends each
payload as one JSON line; this module folds those lines back into calls and API
events keyed by session id, so they can be merged with (or used instead of)
``state.db``.
"""

from __future__ import annotations

import datetime as _dt
import json
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple


def _iso(ts: Optional[float]) -> Optional[str]:
    if ts is None:
        return None
    try:
        return _dt.datetime.fromtimestamp(float(ts)).isoformat(timespec="seconds")
    except (ValueError, OSError, OverflowError):
        return None


def read_jsonl(paths: Iterable[str | Path]) -> List[Dict[str, Any]]:
    events: List[Dict[str, Any]] = []
    for raw in paths:
        p = Path(raw).expanduser()
        if p.is_dir():
            files = sorted(p.glob("*.jsonl"))
        else:
            files = [p]
        for f in files:
            try:
                with f.open("r", encoding="utf-8", errors="replace") as fh:
                    for line in fh:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            obj = json.loads(line)
                        except ValueError:
                            continue
                        if isinstance(obj, dict):
                            obj.setdefault("_file", str(f))
                            events.append(obj)
            except OSError:
                continue
    events.sort(key=lambda e: float(e.get("_htrace_ts") or 0))
    return events


def fold(events: Iterable[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    """session_id -> {calls: [...], api: [...], session: {...}}

    Two payload shapes exist in the wild: the flat shape documented for
    ``hermes hooks test`` (tool_name / args / result / duration_ms at top level)
    and the live shape where Hermes nests the same fields under ``extra``
    (``tool_input`` instead of ``args``). Both are accepted.
    """
    out: Dict[str, Dict[str, Any]] = {}
    started: Dict[Tuple[str, str], Dict[str, Any]] = {}

    for ev in events:
        event = ev.get("hook_event_name") or ev.get("event") or ""
        extra = ev.get("extra") if isinstance(ev.get("extra"), dict) else {}

        def pick(*names, default=None, _ev=ev, _extra=extra):
            for n in names:
                for src in (_ev, _extra):
                    if n in src and src[n] is not None:
                        return src[n]
            return default

        sid = pick("session_id", "task_id", default="unknown")
        bucket = out.setdefault(sid, {"calls": [], "api": [], "session": {}, "subagents": []})
        ts = ev.get("_htrace_ts")
        call_id = pick("tool_call_id", default="")
        tool_name = pick("tool_name", default="?")
        result = pick("result")
        duration = pick("duration_ms")

        if event == "pre_tool_call":
            key = (sid, call_id or f"{tool_name}@{ts}")
            started[key] = dict(ev, _args=pick("args", "tool_input"), _tool_call_id=call_id)
        elif event == "post_tool_call":
            key = (sid, call_id or f"{tool_name}@start")
            pre = started.pop(key, {})
            if duration is None and isinstance(pre, dict) and pre.get("_htrace_ts") and ts:
                duration = int(round((float(ts) - float(pre["_htrace_ts"])) * 1000))
            bucket["calls"].append({
                "call_id": call_id or pre.get("_tool_call_id", ""),
                "name": tool_name if tool_name != "?" else pre.get("tool_name", "?"),
                "args": pick("args", "tool_input", default=pre.get("_args")),
                "started_at": _iso(pre.get("_htrace_ts")) if isinstance(pre, dict) else None,
                "ended_at": _iso(ts),
                "duration_ms": duration,
                "result": result,
                "source": "hook",
            })
        elif event == "post_api_request":
            bucket["api"].append({
                "model": pick("response_model", "model"),
                "provider": pick("provider"),
                "duration_s": pick("api_duration"),
                "finish_reason": pick("finish_reason"),
                "message_count": pick("message_count"),
                "usage": pick("usage", default={}),
                "api_call_count": pick("api_call_count"),
                "at": _iso(pick("ended_at", default=ts)),
            })
        elif event == "on_session_start":
            bucket["session"].setdefault("started_at", _iso(ts))
            bucket["session"].setdefault("platform", pick("platform"))
        elif event == "on_session_end":
            bucket["session"]["ended_at"] = _iso(ts)
            bucket["session"]["completed"] = pick("completed")
            bucket["session"]["failed"] = pick("failed")
            bucket["session"]["model"] = pick("model")
        elif event == "subagent_stop":
            bucket["subagents"].append({
                "parent_session": pick("parent_session_id", default=sid),
                "child_role": pick("child_role"),
                "status": pick("child_status"),
                "summary": str(pick("child_summary", default=""))[:400],
                "duration_ms": pick("duration_ms"),
                "tool_calls": pick("tool_call_history", default=[]),
            })
    return out
