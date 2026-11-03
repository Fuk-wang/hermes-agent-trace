"""Install / remove the htrace-context plugin in ONE Hermes home.

Same rules as ``hooks/install.py``: nothing is written unless you name the home,
an existing copy is backed up before it is replaced, and ``--remove`` deletes
only what this tool installed.

    python -m htrace plugin --status
    python -m htrace plugin --install            # default home = this machine's default profile
    python -m htrace plugin --install --home <profiles/monitor>
    python -m htrace plugin --remove  --home <profiles/monitor>

The plugin is copied (not symlinked) to ``<home>/plugins/htrace-context/`` so a
profile keeps working when this repository moves. ``segments.py`` is copied from
``htrace/segments.py`` at install time — the two must stay byte-identical.
"""

from __future__ import annotations

import datetime as _dt
import shutil
import sys
from pathlib import Path
from typing import List, Optional

PLUGIN_NAME = "htrace-context"
REPO_PLUGIN_DIR = Path(__file__).resolve().parent.parent / "plugins" / "htrace_context"
SEGMENTS = Path(__file__).resolve().parent / "segments.py"


def _default_home() -> Path:
    import os

    env = os.environ.get("HERMES_HOME")
    if env:
        return Path(env)
    return Path.home() / ".hermes"


def _stamp() -> str:
    return _dt.datetime.now().strftime("%Y%m%d-%H%M%S")


def status(home: Path) -> dict:
    target = home / "plugins" / PLUGIN_NAME
    data_dir = home / "htrace-context"
    jsonl = sorted(data_dir.glob("context-*.jsonl")) if data_dir.exists() else []
    raw = sorted((data_dir / "raw").glob("ctx-*.json")) if (data_dir / "raw").exists() else []
    return {
        "home": str(home),
        "installed": target.exists(),
        "target": str(target),
        "files": sorted(p.name for p in target.glob("*")) if target.exists() else [],
        "data_dir": str(data_dir),
        "jsonl_files": len(jsonl),
        "jsonl_lines": sum(1 for p in jsonl for _ in p.open(encoding="utf-8")),
        "raw_files": len(raw),
        "segments_synced": (target / "segments.py").exists()
        and (target / "segments.py").read_bytes() == SEGMENTS.read_bytes(),
    }


def _hermes_exe() -> Optional[str]:
    import shutil

    return shutil.which("hermes")


def _set_enabled(home: Path, enabled: bool) -> Optional[str]:
    """Enable/disable the plugin through the CLI, after backing config.yaml up ourselves.

    The plugin loads only when it is named in ``plugins.enabled``; discovery alone
    is not enough. Backing the file up first keeps ``--remove`` a real undo.
    """
    import os
    import subprocess

    exe = _hermes_exe()
    if not exe:
        return "hermes executable not found on PATH; run `hermes plugins enable %s` yourself" % PLUGIN_NAME
    cfg = home / "config.yaml"
    if cfg.exists():
        bak = home / "backups" / f"config.yaml.{_stamp()}.bak"
        bak.parent.mkdir(parents=True, exist_ok=True)
        bak.write_bytes(cfg.read_bytes())
        print(f"backup  {bak}")
    env = dict(os.environ)
    env["HERMES_HOME"] = str(home)
    verb = "enable" if enabled else "disable"
    proc = subprocess.run([exe, "plugins", verb, PLUGIN_NAME], env=env, capture_output=True,
                          text=True, timeout=180)
    out = (proc.stdout or "").strip().splitlines()
    if out:
        print(f"{verb}  {out[-1][:160]}")
    if proc.returncode != 0:
        return f"`hermes plugins {verb}` exited {proc.returncode}: {(proc.stderr or '').strip()[:200]}"
    return None


def install(home: Path, *, dry_run: bool = False) -> int:
    home = Path(home)
    target = home / "plugins" / PLUGIN_NAME
    if not REPO_PLUGIN_DIR.exists():
        print(f"error: plugin source missing: {REPO_PLUGIN_DIR}", file=sys.stderr)
        return 2
    print(f"home    {home}")
    print(f"source  {REPO_PLUGIN_DIR}")
    print(f"target  {target}")
    backup: Optional[Path] = None
    if target.exists():
        backup = home / "backups" / f"{PLUGIN_NAME}-{_stamp()}"
        print(f"backup  {backup}  (existing copy moved aside)")
    if dry_run:
        print("dry run: nothing written")
        return 0
    if backup is not None:
        backup.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(target), str(backup))
    target.mkdir(parents=True, exist_ok=True)
    copied: List[str] = []
    for src in sorted(REPO_PLUGIN_DIR.glob("*")):
        if src.name == "__pycache__":
            continue
        shutil.copy2(src, target / src.name)
        copied.append(src.name)
    # segments.py must be this build's copy, not whatever sat in the repo module.
    shutil.copy2(SEGMENTS, target / "segments.py")
    if "segments.py" not in copied:
        copied.append("segments.py")
    (home / "htrace-context").mkdir(parents=True, exist_ok=True)
    print(f"wrote   {', '.join(sorted(copied))}")
    why = _set_enabled(home, True)
    if why:
        print(f"WARN    {why}")
        print("        the plugin will not load until it is enabled")
    print(f"data    {home / 'htrace-context'}  (ctx JSONL + last-50 raw store)")
    print("remove  python -m htrace plugin --remove --home " + str(home))
    return 0


def remove(home: Path, *, purge_data: bool = False, dry_run: bool = False) -> int:
    home = Path(home)
    target = home / "plugins" / PLUGIN_NAME
    data = home / "htrace-context"
    would = []
    if target.exists():
        would.append(str(target))
    if purge_data and data.exists():
        would.append(str(data))
    if not would:
        print(f"nothing to remove in {home}")
        return 0
    if dry_run:
        for path in would:
            print("would remove " + path)
        return 0
    why = _set_enabled(home, False)
    if why:
        print(f"WARN    {why}")
    for path in would:
        print("removed " + path)
        shutil.rmtree(path, ignore_errors=True)
    if not purge_data:
        print(f"kept    {data}  (captured trace data; add --purge-data to delete)")
    return 0
