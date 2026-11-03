"""Split a provider request into the six context categories the viewer draws.

Pure functions, no Hermes imports: the same file is copied next to the plugin
(`plugins/htrace_context/segments.py`) so both sides measure sizes identically.
`tests/test_smoke.py` asserts the two copies are byte-identical.

Categories
    sys        system prompt minus the injected blocks below
    injected   memory / SOUL / skills index / environment / context files inside the system prompt
    tools      tool JSON schemas sent with the request
    user       user messages
    assistant  assistant messages (content + tool_calls JSON)
    tool       tool results

Sizes are CHARACTERS. Tokens are estimated by the builder from a fitted
chars-per-token coefficient per category, never guessed here.
"""

from __future__ import annotations

import json
import re
from typing import Any, Dict, Iterable, List, Optional

CATEGORIES = ["sys", "injected", "tools", "user", "assistant", "tool"]

LABELS = {
    "sys": "system prompt",
    "injected": "injected (memory/skills/env)",
    "tools": "tool schemas",
    "user": "user messages",
    "assistant": "assistant messages",
    "tool": "tool results",
}

# A system prompt section counts as "injected" when its heading names one of the
# blocks Hermes splices in. Everything else stays in `sys`, so an unknown heading
# can only ever under-attribute, never invent a category.
_INJECT_RE = re.compile(
    r"memor|skill|environment|soul|persona|context file|agents\.md|claude\.md|"
    r"instructions|workspace|project|sandbox|user profile",
    re.I,
)

_HEADING_RE = re.compile(r"^(#{1,4})\s+(\S.*)$", re.M)

# CJK runs one token per 1-2 characters, ASCII prose/code one token per ~4. One
# chars-per-token per category cannot cover both, so the fit counts them apart.
_CJK_RE = re.compile(r"[㐀-䶿一-鿿豈-﫿぀-ヿ가-힯]")


def cjk_chars(text: str) -> int:
    return len(_CJK_RE.findall(text or ""))


def _message_text(msg: Any) -> str:
    """The bytes a message contributes to the prompt, as JSON where it has structure."""
    if not isinstance(msg, dict):
        return str(msg)
    parts: List[str] = []
    content = msg.get("content")
    if content is not None:
        parts.append(content if isinstance(content, str) else json.dumps(content, ensure_ascii=False))
    tool_calls = msg.get("tool_calls")
    if tool_calls:
        parts.append(json.dumps(tool_calls, ensure_ascii=False))
    return "\n".join(parts)


def message_size(msg: Any) -> int:
    return len(_message_text(msg))


def system_sections(text: str) -> List[Dict[str, Any]]:
    """Split the system prompt on markdown headings and classify each block."""
    text = text or ""
    if not text:
        return []
    marks = [(m.start(), m.group(2).strip()) for m in _HEADING_RE.finditer(text)]
    if not marks:
        return [{"heading": "(no heading)", "k": "sys", "chars": len(text), "start": 0, "end": len(text)}]
    out: List[Dict[str, Any]] = []
    if marks[0][0] > 0:
        out.append({"heading": "(preamble)", "k": "sys", "chars": marks[0][0], "start": 0, "end": marks[0][0]})
    for i, (pos, heading) in enumerate(marks):
        end = marks[i + 1][0] if i + 1 < len(marks) else len(text)
        out.append({
            "heading": heading[:80],
            "k": "injected" if _INJECT_RE.search(heading) else "sys",
            "chars": end - pos,
            "start": pos,
            "end": end,
        })
    return out


def classify_system(text: str) -> Dict[str, Any]:
    """{chars: {sys, injected}, total, sections} for one system prompt."""
    text = text or ""
    secs = system_sections(text)
    chars = {"sys": 0, "injected": 0}
    cjk = {"sys": 0, "injected": 0}
    for sec in secs:
        chars[sec["k"]] += sec["chars"]
        cjk[sec["k"]] += cjk_chars(text[sec["start"]:sec["end"]])
    return {"chars": chars, "cjk": cjk, "total": len(text), "sections": secs}


def classify_messages(messages: Iterable[Any], *, skip_system: bool = False) -> Dict[str, Any]:
    """Per-role character totals plus per-message shapes for the request list.

    ``skip_system`` is set when the caller already measured the system prompt via
    ``system_prompt``: the same text also rides in ``messages[0]`` for OpenAI-style
    requests and must not be counted twice.
    """
    chars = {"user": 0, "assistant": 0, "tool": 0, "sys": 0}
    cjk = {"user": 0, "assistant": 0, "tool": 0, "sys": 0}
    counts = {"user": 0, "assistant": 0, "tool": 0, "sys": 0}
    shapes: List[Dict[str, Any]] = []
    for idx, msg in enumerate(messages or []):
        role = (msg.get("role") if isinstance(msg, dict) else "") or ""
        size = message_size(msg)
        shapes.append({
            "i": idx,
            "role": role or "?",
            "chars": size,
            "tool": (msg.get("tool_name") if isinstance(msg, dict) else "") or "",
        })
        if role == "system" and skip_system:
            continue
        bucket = role if role in ("user", "assistant", "tool") else "sys"
        chars[bucket] += size
        cjk[bucket] += cjk_chars(_message_text(msg))
        counts[bucket] += 1
    return {"chars": chars, "cjk": cjk, "counts": counts, "shapes": shapes}


def tools_chars(tools: Any) -> Optional[int]:
    """Character size of the tool schema array as sent, or None if absent."""
    if not isinstance(tools, list):
        return None
    return len(json.dumps(tools, ensure_ascii=False))


def tools_cjk(tools: Any) -> int:
    if not isinstance(tools, list):
        return 0
    return cjk_chars(json.dumps(tools, ensure_ascii=False))


def segment_request(*, system_prompt: Optional[str], messages: Any, tools: Any) -> Dict[str, Any]:
    """Everything the builder needs about one request, in category sizes."""
    sys_part = classify_system(system_prompt or "")
    msg_part = classify_messages(messages or [], skip_system=bool(system_prompt))
    chars = {
        "sys": sys_part["chars"]["sys"] + msg_part["chars"]["sys"],
        "injected": sys_part["chars"]["injected"],
        "tools": tools_chars(tools) or 0,
        "user": msg_part["chars"]["user"],
        "assistant": msg_part["chars"]["assistant"],
        "tool": msg_part["chars"]["tool"],
    }
    cjk = {
        "sys": sys_part["cjk"]["sys"] + msg_part["cjk"]["sys"],
        "injected": sys_part["cjk"]["injected"],
        "tools": tools_cjk(tools),
        "user": msg_part["cjk"]["user"],
        "assistant": msg_part["cjk"]["assistant"],
        "tool": msg_part["cjk"]["tool"],
    }
    return {
        "chars": chars,
        "cjk": cjk,
        "total_chars": sum(chars.values()),
        "counts": msg_part["counts"],
        "sections": sys_part.get("sections") or [],
        "tools_present": tools_chars(tools) is not None,
    }
