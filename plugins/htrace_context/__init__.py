"""htrace-context — capture what each API request actually puts in the prompt.

Two hooks, one JSONL line each, paired by ``(session_id, api_request_id)``:

``pre_api_request``  -> ``{"kind":"ctx_req", chars:{six categories}, counts, sections, ...}``
``post_api_request`` -> ``{"kind":"ctx_resp", usage:{input_tokens,...}, ...}``

Design rules:

* Never raise, never block. A broken capture must not cost the user a turn.
* Sizes only in the JSONL; raw text goes to a separate store capped at the last
  ``HTRACE_CTX_RAW_KEEP`` requests (default 50), older files are pruned.
* No config edits, no network, no state outside ``HTRACE_CTX_DIR``
  (default ``<HERMES_HOME>/htrace-context``).
"""

from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from . import segments

_LOCK = threading.Lock()
_MAX_MSG_CHARS = 20000      # per message, raw store only
_MAX_SYS_CHARS = 200000     # system prompt, raw store only
_RAW_KEEP = int(os.environ.get("HTRACE_CTX_RAW_KEEP", "50") or 50)


# --------------------------------------------------------------------------- paths
def _base_dir() -> Path:
    env = os.environ.get("HTRACE_CTX_DIR")
    if env:
        return Path(env)
    home = os.environ.get("HERMES_HOME")
    return (Path(home) if home else Path.home() / ".hermes") / "htrace-context"


def _jsonl_path() -> Path:
    return _base_dir() / f"context-{time.strftime('%Y%m%d')}.jsonl"


def _append_line(obj: Dict[str, Any]) -> None:
    path = _jsonl_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(obj, ensure_ascii=False, default=str)
    with _LOCK:
        with path.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")


# --------------------------------------------------------------------------- raw store
def _prune_raw(raw_dir: Path) -> None:
    try:
        files = sorted(raw_dir.glob("ctx-*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    except OSError:
        return
    for old in files[max(_RAW_KEEP, 0):]:
        try:
            old.unlink()
        except OSError:
            pass


def _write_raw(key: str, payload: Dict[str, Any]) -> Optional[str]:
    if _RAW_KEEP <= 0:
        return None
    raw_dir = _base_dir() / "raw"
    try:
        raw_dir.mkdir(parents=True, exist_ok=True)
        name = "ctx-" + "".join(c if c.isalnum() or c in "-_" else "_" for c in key)[-120:] + ".json"
        (raw_dir / name).write_text(json.dumps(payload, ensure_ascii=False, default=str), encoding="utf-8")
        _prune_raw(raw_dir)
        return f"raw/{name}"
    except Exception:
        return None


def _clip(text: str, cap: int) -> Dict[str, Any]:
    text = text or ""
    return {"text": text[:cap], "chars": len(text), "truncated": len(text) > cap}


# --------------------------------------------------------------------------- hooks
def _system_text(value: Any) -> Optional[str]:
    """Accept a plain string, or Anthropic-style content blocks, as one text blob."""
    if value is None:
        return None
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        parts = []
        for block in value:
            if isinstance(block, dict):
                if isinstance(block.get("text"), str):
                    parts.append(block["text"])
            elif isinstance(block, str):
                parts.append(block)
        return "\n".join(parts) if parts else None
    return str(value)


def on_pre_api_request(**kw: Any) -> None:
    """Measure one request. Sizes only here; raw text goes to the capped store."""
    try:
        sid = str(kw.get("session_id") or "")
        rid = str(kw.get("api_request_id") or "")
        turn = str(kw.get("turn_id") or "")
        count = kw.get("api_call_count")
        ts = kw.get("started_at") or time.time()
        key = f"{sid}-{rid or turn}-{count or 0}-{int(float(ts))}"

        messages = kw.get("request_messages") or kw.get("conversation_history") or []
        # OpenAI-style requests carry the system prompt as messages[0]; the hook's
        # ``system_prompt`` is None there, so recover it or the whole prompt lands in
        # one bucket and the six-way split becomes a five-way one.
        system_prompt = _system_text(kw.get("system_prompt"))
        if not system_prompt:
            for msg in messages:
                if isinstance(msg, dict) and msg.get("role") == "system":
                    system_prompt = _system_text(msg.get("content"))
                    break
        request = kw.get("request") or {}
        body = request.get("body") if isinstance(request, dict) else None
        tools = body.get("tools") if isinstance(body, dict) else None
        tools_truncated = bool(request.get("_truncated")) if isinstance(request, dict) else False

        seg = segments.segment_request(system_prompt=system_prompt, messages=messages, tools=tools)

        record = {
            "kind": "ctx_req",
            "v": 1,
            "ts": float(ts),
            "session_id": sid,
            "task_id": str(kw.get("task_id") or ""),
            "turn_id": turn,
            "api_request_id": rid,
            "api_call_count": count,
            "model": str(kw.get("model") or ""),
            "provider": str(kw.get("provider") or ""),
            "message_count": kw.get("message_count"),
            "tool_count": kw.get("tool_count"),
            "request_char_count": kw.get("request_char_count"),
            "approx_input_tokens": kw.get("approx_input_tokens"),
            "chars": seg["chars"],
            "cjk": seg.get("cjk") or {},
            "total_chars": seg["total_chars"],
            "counts": seg["counts"],
            "sections": [{"heading": s["heading"], "k": s["k"], "chars": s["chars"]} for s in seg["sections"]],
            "tools_present": seg["tools_present"],
            "payload_truncated": tools_truncated,
        }
        record["raw_ref"] = _write_raw(key, {
            "key": key,
            "session_id": sid,
            "api_request_id": rid,
            "turn_id": turn,
            "api_call_count": count,
            "ts": float(ts),
            "system_prompt": _clip(str(system_prompt or ""), _MAX_SYS_CHARS),
            "messages": [
                {
                    "role": (m.get("role") if isinstance(m, dict) else "?"),
                    "tool_name": (m.get("tool_name") if isinstance(m, dict) else "") or "",
                    **_clip(segments._message_text(m), _MAX_MSG_CHARS),
                }
                for m in list(messages or [])[:400]
            ],
            "tools": [
                {"name": (t.get("function", {}).get("name") if isinstance(t, dict) else str(t)) or "",
                 **_clip(json.dumps(t, ensure_ascii=False), _MAX_MSG_CHARS)}
                for t in (tools or [])[:200]
            ] if isinstance(tools, list) else [],
        })
        _append_line(record)
    except Exception:
        return


def on_post_api_request(**kw: Any) -> None:
    """Pair the provider's real token usage with the request just measured."""
    try:
        usage = kw.get("usage") or {}
        _append_line({
            "kind": "ctx_resp",
            "v": 1,
            "ts": time.time(),
            "session_id": str(kw.get("session_id") or ""),
            "task_id": str(kw.get("task_id") or ""),
            "turn_id": str(kw.get("turn_id") or ""),
            "api_request_id": str(kw.get("api_request_id") or ""),
            "api_call_count": kw.get("api_call_count"),
            "model": str(kw.get("model") or ""),
            "provider": str(kw.get("provider") or ""),
            "finish_reason": kw.get("finish_reason"),
            "api_duration": kw.get("api_duration"),
            "first_chunk_at": kw.get("first_chunk_at"),
            "message_count": kw.get("message_count"),
            "usage": {k: usage.get(k) for k in
                      ("input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens",
                       "reasoning_tokens", "prompt_tokens", "completion_tokens")
                      if isinstance(usage, dict) and usage.get(k) is not None} if isinstance(usage, dict) else {},
        })
    except Exception:
        return


def register(ctx: Any) -> None:
    ctx.register_hook("pre_api_request", on_pre_api_request)
    ctx.register_hook("post_api_request", on_post_api_request)
