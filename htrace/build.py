"""Compose the trace document consumed by the viewer."""

from __future__ import annotations

import datetime as _dt
import json
from pathlib import Path
from typing import Any, Dict, List, Optional

from . import SCHEMA, __version__
from . import hooks_jsonl, redact, source_index, state_db


def _now_iso() -> str:
    return _dt.datetime.now().astimezone().isoformat(timespec="seconds")


def _merge_hook_calls(session: Dict[str, Any], hook_bucket: Dict[str, Any]) -> int:
    """Append hook-observed calls the DB did not pair, return number added."""
    known = {c.get("call_id") for c in session["calls"] if c.get("call_id")}
    added = 0
    for call in hook_bucket.get("calls", []):
        if call.get("call_id") and call["call_id"] in known:
            continue
        session["calls"].append({
            "call_id": call.get("call_id") or "",
            "name": call.get("name") or "?",
            "turn": None,
            "seq": len(session["calls"]) + 1,
            "started_at": call.get("started_at"),
            "started_ts": None,
            "ended_at": call.get("ended_at"),
            "ended_ts": None,
            "duration_ms": call.get("duration_ms"),
            "args_raw": json.dumps(call.get("args"), ensure_ascii=False)[: state_db.MAX_ARG_CHARS]
            if call.get("args") is not None else None,
            "args": call.get("args"),
            "args_bytes": len(json.dumps(call.get("args"), ensure_ascii=False)) if call.get("args") is not None else 0,
            "args_truncated": False,
            "status": "ok" if call.get("result") else "pending",
            "result": (call.get("result") or "")[: state_db.MAX_RESULT_CHARS] or None,
            "result_bytes": len(call.get("result") or ""),
            "result_truncated": len(call.get("result") or "") > state_db.MAX_RESULT_CHARS,
            "error": None,
            "source": "hook",
        })
        added += 1
    if hook_bucket.get("api"):
        session.setdefault("api_calls", []).extend(hook_bucket["api"])
    return added


def _synthetic_session(sid: str, bucket: Dict[str, Any], profile: str) -> Dict[str, Any]:
    meta = bucket.get("session", {})
    session = {
        "id": sid,
        "profile": profile,
        "title": "(hook-only session)",
        "source": meta.get("platform") or "hook",
        "model": meta.get("model") or "",
        "started_at": meta.get("started_at"),
        "ended_at": meta.get("ended_at"),
        "started_ts": None,
        "ended_ts": None,
        "cwd": "",
        "git_branch": "",
        "message_count": 0,
        "tool_call_count": len(bucket.get("calls", [])),
        "parent_session_id": "",
        "tokens": {"input": 0, "output": 0, "cache_read": 0, "cache_write": 0, "reasoning": 0},
        "cost_usd": None,
        "cost_status": "unknown",
        "tool_names": [],
        "calls": [],
        "timeline": [],
        "stats": {"calls": 0, "errors": 0, "pending": 0, "total_ms": 0, "p50_ms": 0, "max_ms": 0, "by_tool": {}},
        "api_calls": bucket.get("api", []),
    }
    _merge_hook_calls(session, bucket)
    _recompute_stats(session)
    return session


def _recompute_stats(session: Dict[str, Any]) -> None:
    calls = session.get("calls", [])
    by_tool: Dict[str, Dict[str, Any]] = {}
    durations: List[int] = []
    for call in calls:
        bucket = by_tool.setdefault(call["name"], {"calls": 0, "errors": 0, "ms": 0, "bytes": 0})
        bucket["calls"] += 1
        bucket["bytes"] += (call.get("args_bytes") or 0) + (call.get("result_bytes") or 0)
        if call.get("duration_ms") is not None:
            bucket["ms"] += call["duration_ms"]
            durations.append(call["duration_ms"])
        if call.get("status") == "error":
            bucket["errors"] += 1
    durations.sort()
    session["stats"] = {
        "calls": len(calls),
        "errors": sum(1 for c in calls if c.get("status") == "error"),
        "pending": sum(1 for c in calls if c.get("status") == "pending"),
        "total_ms": sum(durations),
        "p50_ms": durations[len(durations) // 2] if durations else 0,
        "max_ms": durations[-1] if durations else 0,
        "by_tool": by_tool,
    }
    session["tool_call_count"] = session["tool_call_count"] or len(calls)


def _subagent_edges(homes: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    edges: List[Dict[str, Any]] = []
    seen = set()
    for home in homes:
        for d in home.get("delegations", []):
            key = (d["id"], d["parent_session"])
            if key in seen:
                continue
            seen.add(key)
            edges.append({
                "kind": "delegation",
                "id": d["id"],
                "parent_session": d["parent_session"],
                "origin_session": d.get("origin_session"),
                "state": d["state"],
                "goal": d.get("goal") or "",
                "dispatched_at": d.get("dispatched_at"),
                "completed_at": d.get("completed_at"),
            })
        for s in home["sessions"]:
            if s.get("parent_session_id"):
                edges.append({
                    "kind": "child_session",
                    "id": s["id"],
                    "parent_session": s["parent_session_id"],
                    "origin_session": s["parent_session_id"],
                    "state": "completed" if s.get("ended_at") else "running",
                    "goal": s.get("title") or "",
                    "dispatched_at": s.get("started_at"),
                    "completed_at": s.get("ended_at"),
                })
    return edges


def build_document(
    homes: Optional[List[str]] = None,
    session_limit: Optional[int] = None,
    hook_paths: Optional[List[str]] = None,
    source_root: Optional[str] = None,
    apply_redaction: bool = False,
    env_path: Optional[str] = None,
) -> Dict[str, Any]:
    discovered = state_db.discover_homes(homes)
    if homes and not discovered:
        import sys as _sys
        print(f"warning: no state.db found under {homes!r} — building from hook events only",
              file=_sys.stderr)
    loaded: List[Dict[str, Any]] = []
    if discovered:
        for label, home, db in discovered:
            loaded.append(state_db.load_home(label, home, db, session_limit=session_limit))
    elif hook_paths:
        # Hook-only trace: no session database yet (fresh profile, CI, demo).
        loaded.append({"label": "hooks", "home": "", "state_db": "", "sessions": [], "delegations": []})
    else:
        raise SystemExit(
            "No Hermes state.db found. Pass --home <hermes home> or set HERMES_HOME."
        )

    hook_fold: Dict[str, Dict[str, Any]] = {}
    hook_files: List[str] = []
    if hook_paths:
        events = hooks_jsonl.read_jsonl(hook_paths)
        hook_fold = hooks_jsonl.fold(events)
        hook_files = [str(p) for p in hook_paths]
        merged = 0
        for home in loaded:
            for session in home["sessions"]:
                bucket = hook_fold.get(session["id"])
                if bucket:
                    merged += _merge_hook_calls(session, bucket)
                    _recompute_stats(session)
        known_ids = {s["id"] for home in loaded for s in home["sessions"]}
        extra = [sid for sid in hook_fold if sid not in known_ids]
        if extra:
            profile = loaded[0]["label"]
            for sid in extra:
                loaded[0]["sessions"].append(_synthetic_session(sid, hook_fold[sid], profile))
    else:
        merged = 0

    sessions = [s for home in loaded for s in home["sessions"]]
    sessions.sort(key=lambda s: s.get("started_ts") or 0, reverse=True)

    index_doc: Dict[str, Any] = {"root": "", "tools": {}}
    if source_root:
        index_doc = source_index.build_index(source_root)

    by_tool: Dict[str, Dict[str, Any]] = {}
    totals = {
        "sessions": len(sessions),
        "calls": 0,
        "errors": 0,
        "pending": 0,
        "total_ms": 0,
        "input_tokens": 0,
        "output_tokens": 0,
        "cache_read_tokens": 0,
        "cost_usd": 0.0,
    }
    for session in sessions:
        totals["calls"] += session["stats"]["calls"]
        totals["errors"] += session["stats"]["errors"]
        totals["pending"] += session["stats"]["pending"]
        totals["total_ms"] += session["stats"]["total_ms"]
        tokens = session.get("tokens", {})
        totals["input_tokens"] += tokens.get("input", 0)
        totals["output_tokens"] += tokens.get("output", 0)
        totals["cache_read_tokens"] += tokens.get("cache_read", 0)
        if session.get("cost_usd"):
            totals["cost_usd"] += float(session["cost_usd"])
        for name, st in session["stats"]["by_tool"].items():
            bucket = by_tool.setdefault(name, {"calls": 0, "errors": 0, "ms": 0, "bytes": 0, "sessions": set()})
            bucket["calls"] += st["calls"]
            bucket["errors"] += st["errors"]
            bucket["ms"] += st["ms"]
            bucket["bytes"] += st["bytes"]
            bucket["sessions"].add(session["id"])
    for name, bucket in by_tool.items():
        bucket["sessions"] = len(bucket["sessions"])

    doc: Dict[str, Any] = {
        "schema": SCHEMA,
        "generated_at": _now_iso(),
        "generator": {"name": "htrace", "version": __version__},
        "source": {
            "mode": "merged" if hook_fold else "state_db",
            "homes": [{"profile": h["label"], "home": h["home"], "state_db": h["state_db"]} for h in loaded],
            "hook_files": hook_files,
            "hook_calls_merged": merged if hook_fold else 0,
            "redacted": apply_redaction,
            "source_index_root": index_doc.get("root", ""),
        },
        "totals": {k: (round(v, 4) if isinstance(v, float) else v) for k, v in totals.items()},
        "by_tool": by_tool,
        "sessions": sessions,
        "tools": index_doc.get("tools", {}),
        "subagents": _subagent_edges(loaded),
    }

    if apply_redaction:
        env_values: List[str] = []
        for home in loaded:
            env_values.extend(redact.load_env_values(Path(home["home"]) / ".env"))
        doc, stats = redact.redact_document(doc, env_values, redact.default_home_names())
        doc["source"]["redaction_stats"] = stats
    return doc


def write_document(doc: Dict[str, Any], out_path: Path, as_demo_js: bool = False) -> Path:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(doc, ensure_ascii=False, separators=(",", ":"))
    if as_demo_js:
        out_path.write_text("window.HTRACE_DEMO = " + payload + ";\n", encoding="utf-8")
    else:
        out_path.write_text(payload, encoding="utf-8")
    return out_path
