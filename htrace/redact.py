"""Redaction for the public demo trace.

Two layers:
  1. value scrub — every value found in a Hermes ``.env`` (length >= 8) is
     replaced verbatim, so real keys never survive even if the pattern miss;
  2. pattern scrub — provider key shapes, bearer headers, private key blocks,
     bot tokens, home-directory usernames, e-mail addresses.

This is for the trace that ships inside the public Pages demo. Traces exported
by users are NOT redacted automatically: the viewer states that, and
``htrace build --redact`` is offered as an explicit choice.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any, Dict, Iterable, Tuple

MASK = "<redacted>"

_PATTERNS: Tuple[Tuple[str, re.Pattern], ...] = (
    ("private_key", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.S)),
    ("bearer", re.compile(r"(?i)\bBearer\s+[A-Za-z0-9\-\._~\+/=]{12,}")),
    ("openai_key", re.compile(r"\bsk-[A-Za-z0-9_\-]{16,}")),
    ("anthropic_key", re.compile(r"\bsk-ant-[A-Za-z0-9_\-]{16,}")),
    ("github_token", re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})")),
    ("aws_key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("telegram_token", re.compile(r"\b\d{8,10}:[A-Za-z0-9_-]{30,}\b")),
    ("secret_assign", re.compile(
        r"(?i)\b(api[_-]?key|token|secret|password|passwd|authorization|access[_-]?key)\b\s*[:=]\s*"
        r"[\"']?([A-Za-z0-9\-_\.\+/]{8,})[\"']?")),
    ("email", re.compile(r"\b[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}\b")),
)

_USER_PATTERNS = (
    # Windows paths, including the JSON-escaped form (C:\\Users\\name) that
    # nested tool arguments carry inside args_raw.
    re.compile(r"(?i)([A-Za-z]:\\{1,4}Users\\{1,4})([^\\\s\"']+)"),
    re.compile(r"(?i)([A-Za-z]:/{1,4}Users/)([^/\s\"']+)"),
    re.compile(r"(?i)(/c/Users/)([^/\s\"']+)"),
    re.compile(r"(?i)(/home/)([^/\s\"']+)"),
    re.compile(r"(?i)(/Users/)([^/\s\"']+)"),
)


def load_env_values(env_path: Path) -> list[str]:
    values: list[str] = []
    try:
        text = env_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return values
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        _, _, raw = line.partition("=")
        value = raw.strip().strip("'\"")
        if len(value) >= 8 and not value.lower().startswith(("http://", "https://")):
            values.append(value)
    return values


def _redact_string(text: str, stats: Dict[str, int], home_names: Iterable[str]) -> str:
    # Secret/pattern pass first: an e-mail scrubbed here stays an e-mail, while a
    # username-first pass would turn alice@example.com into <user>@example.com and
    # leave the domain behind.
    for label, pat in _PATTERNS:
        if label == "secret_assign":
            def _sub(m: re.Match, _label=label) -> str:
                return f"{m.group(1)}={MASK}"
            text, n = pat.subn(_sub, text)
        else:
            text, n = pat.subn(MASK, text)
        if n:
            stats[label] = stats.get(label, 0) + n

    for name in home_names:
        if not name or len(name) < 3:
            continue
        # The login name also shows up bare: `ls -la` ownership columns, process
        # listings, service names. For a public demo it must not.
        token = re.compile(rf"(?<!\w){re.escape(name)}(?!\w)")
        text, n = token.subn("<user>", text)
        if n:
            stats["username"] = stats.get("username", 0) + n
        for pat in _USER_PATTERNS:
            text, n = pat.subn(lambda m: m.group(1) + "<user>", text)
            if n:
                stats["home_path"] = stats.get("home_path", 0) + n
    return text


def redact_document(doc: Dict[str, Any], env_values: Iterable[str] = (), home_names: Iterable[str] = ()) -> Tuple[Dict[str, Any], Dict[str, int]]:
    stats: Dict[str, int] = {}
    secrets = [v for v in env_values if v]

    def walk(value: Any) -> Any:
        if isinstance(value, str):
            out = value
            for secret in secrets:
                if secret in out:
                    out = out.replace(secret, MASK)
                    stats["env_value"] = stats.get("env_value", 0) + 1
            return _redact_string(out, stats, home_names)
        if isinstance(value, list):
            return [walk(v) for v in value]
        if isinstance(value, dict):
            return {k: walk(v) for k, v in value.items()}
        return value

    return walk(doc), stats


def default_home_names() -> list[str]:
    names = {Path.home().name}
    local = os.environ.get("USERNAME") or os.environ.get("USER")
    if local:
        names.add(local)
    return [n for n in names if n]
