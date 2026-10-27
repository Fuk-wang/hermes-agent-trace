#!/usr/bin/env python
"""Install / remove the htrace shell hook, and self-test it.

Design rules (why this script is conservative):

* It never edits a config.yaml unless you name the file explicitly with
  ``--write <path>``; the default is a dry run that prints the exact YAML.
* ``--write`` backs the file up first, inserts the block under ``hooks:`` if
  that key exists, and refuses to touch a file it cannot parse.
* ``--remove`` deletes only the htrace entries, leaving other hooks alone.

Usage
  python hooks/install.py                       # dry run + self test
  python hooks/install.py --snippet             # just the YAML
  python hooks/install.py --write <config.yaml> # apply (off by default)
  python hooks/install.py --remove <config.yaml>
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

HOOK = Path(__file__).resolve().parent / "htrace_hook.py"
EVENTS = ["pre_tool_call", "post_tool_call", "post_api_request", "on_session_end", "subagent_stop"]


def hook_command() -> str:
    """Command line Hermes will spawn. Absolute paths, quoted."""
    exe = sys.executable or "python"
    return f'"{exe}" "{HOOK}"'


def _yaml_str(value: str) -> str:
    """Single-quoted YAML scalar: keeps Windows backslashes literal."""
    return "'" + value.replace("'", "''") + "'"


def snippet(events=None, timeout=5) -> str:
    events = events or EVENTS
    lines = ["hooks:"]
    for ev in events:
        lines.append(f"  {ev}:")
        lines.append(f"    - command: {_yaml_str(hook_command())}")
        lines.append(f"      timeout: {timeout}")
    return "\n".join(lines) + "\n"


def self_test() -> int:
    payload = {
        "hook_event_name": "post_tool_call",
        "tool_name": "terminal",
        "args": {"command": "echo hello"},
        "result": '{"output": "hello"}',
        "duration_ms": 42,
        "session_id": "htrace-selftest",
        "tool_call_id": "selftest-call",
    }
    env = dict(os.environ)
    env["HTRACE_DIR"] = str(Path(os.environ.get("HTRACE_TEST_DIR") or Path.cwd() / ".htrace-selftest"))
    proc = subprocess.run([sys.executable, str(HOOK)], input=json.dumps(payload),
                          capture_output=True, text=True, env=env, timeout=20)
    out_dir = Path(env["HTRACE_DIR"])
    files = sorted(out_dir.glob("events-*.jsonl"))
    ok = proc.returncode == 0 and not proc.stdout.strip() and files
    print(f"self-test: exit={proc.returncode} stdout={proc.stdout.strip()!r} stderr={proc.stderr.strip()[:200]!r}")
    if files:
        last = files[-1].read_text(encoding="utf-8").strip().splitlines()[-1]
        row = json.loads(last)
        print(f"  wrote {files[-1]}  event={row.get('hook_event_name')} tool={row.get('tool_name')} "
              f"duration_ms={row.get('duration_ms')} ts={row.get('_htrace_ts')}")
    else:
        print(f"  no JSONL under {out_dir}")
    return 0 if ok else 1


def _load_yaml(path: Path):
    try:
        import yaml  # type: ignore
    except ImportError:
        return None, "PyYAML not installed; cannot safely edit YAML"
    try:
        return yaml.safe_load(path.read_text(encoding="utf-8")) or {}, None
    except Exception as exc:  # noqa: BLE001 - report, never guess
        return None, f"cannot parse {path}: {exc}"


def apply_config(path: Path, timeout: int) -> int:
    cfg, err = _load_yaml(path)
    if err:
        print("refused:", err)
        return 2
    if not isinstance(cfg, dict):
        print("refused: config root is not a mapping")
        return 2
    hooks = cfg.setdefault("hooks", {})
    if not isinstance(hooks, dict):
        print("refused: `hooks:` is not a mapping")
        return 2
    for ev in EVENTS:
        entries = hooks.setdefault(ev, [])
        if not isinstance(entries, list):
            print(f"refused: hooks.{ev} is not a list")
            return 2
        entries[:] = [e for e in entries if "htrace_hook.py" not in json.dumps(e)[:400]]
        entries.append({"command": hook_command(), "timeout": timeout})
    backup = path.with_suffix(path.suffix + ".htrace.bak")
    shutil.copy2(path, backup)
    try:
        import yaml  # type: ignore
        path.write_text(yaml.safe_dump(cfg, sort_keys=False, allow_unicode=True), encoding="utf-8")
    except Exception as exc:  # noqa: BLE001
        print(f"write failed ({exc}); backup at {backup}")
        return 2
    print(f"applied to {path} (backup: {backup})")
    print("next: restart Hermes, then approve the hook once (or run with --accept-hooks)")
    return 0


def remove_config(path: Path) -> int:
    cfg, err = _load_yaml(path)
    if err:
        print("refused:", err)
        return 2
    hooks = cfg.get("hooks") if isinstance(cfg, dict) else None
    if not isinstance(hooks, dict):
        print("nothing to remove")
        return 0
    removed = 0
    for ev in list(hooks.keys()):
        entries = hooks.get(ev)
        if isinstance(entries, list):
            keep = [e for e in entries if "htrace_hook.py" not in json.dumps(e)[:400]]
            removed += len(entries) - len(keep)
            if keep:
                hooks[ev] = keep
            else:
                hooks.pop(ev, None)
    if not hooks:
        cfg.pop("hooks", None)
    backup = path.with_suffix(path.suffix + ".htrace.bak")
    shutil.copy2(path, backup)
    import yaml  # type: ignore
    path.write_text(yaml.safe_dump(cfg, sort_keys=False, allow_unicode=True), encoding="utf-8")
    print(f"removed {removed} htrace hook entr{'y' if removed == 1 else 'ies'} from {path} (backup: {backup})")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="htrace shell-hook installer")
    ap.add_argument("--snippet", action="store_true", help="print the YAML block and exit")
    ap.add_argument("--write", metavar="CONFIG_YAML", help="apply to this config file (explicit opt-in)")
    ap.add_argument("--remove", metavar="CONFIG_YAML", help="remove htrace entries from this config file")
    ap.add_argument("--timeout", type=int, default=5)
    ap.add_argument("--no-test", action="store_true")
    args = ap.parse_args()

    if args.snippet:
        print(snippet(timeout=args.timeout), end="")
        return 0

    code = 0
    if not args.no_test:
        code |= self_test()

    if args.remove:
        return remove_config(Path(args.remove).expanduser())
    if args.write:
        return apply_config(Path(args.write).expanduser(), args.timeout)

    print("\nAdd this to a Hermes config.yaml (profile-scoped is fine), then restart:\n")
    print(snippet(timeout=args.timeout), end="")
    print("\nNotes")
    print("  · This script did NOT modify any config. Nothing is enabled until you pass --write.")
    print("  · First use needs approval: run Hermes with --accept-hooks once, or set hooks_auto_accept: true.")
    print("  · Events land in $HERMES_HOME/htrace/events-YYYYMMDD.jsonl (override with HTRACE_DIR).")
    print("  · Feed them into a trace with: python -m htrace build --hooks <dir>")
    return code


if __name__ == "__main__":
    raise SystemExit(main())
