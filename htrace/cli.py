"""htrace CLI.

    python -m htrace build --out trace.json --source <hermes-agent source>
    python -m htrace build --redact --demo-js --out demo/trace.demo.js
    python -m htrace index --source <hermes-agent source> --out tools.index.json
    python -m htrace stats --trace trace.json
    python -m htrace serve --dir . --port 8765
"""

from __future__ import annotations

import argparse
import http.server
import json
import socketserver
import sys
from pathlib import Path

from . import __version__
from . import build as build_mod
from . import state_db


def _cmd_build(args: argparse.Namespace) -> int:
    doc = build_mod.build_document(
        homes=args.home or None,
        session_limit=args.limit,
        hook_paths=args.hooks or None,
        source_root=args.source,
        apply_redaction=args.redact,
        env_path=args.env,
    )
    out = Path(args.out).expanduser()
    build_mod.write_document(doc, out, as_demo_js=args.demo_js)
    totals = doc["totals"]
    size_kb = out.stat().st_size / 1024
    print(f"wrote {out}  ({size_kb:.1f} KiB)")
    print(f"  sessions={totals['sessions']}  tool_calls={totals['calls']}  errors={totals['errors']}"
          f"  pending={totals['pending']}  wall_ms={totals['total_ms']}")
    print(f"  tokens in={totals['input_tokens']} out={totals['output_tokens']}"
          f"  cache_read={totals['cache_read_tokens']}  cost=${totals['cost_usd']:.4f}")
    if doc["source"]["redacted"]:
        print(f"  redaction: {doc['source'].get('redaction_stats')}")
    if doc["source"].get("hook_calls_merged"):
        print(f"  hook calls merged: {doc['source']['hook_calls_merged']}")
    missing = [s["id"] for s in doc["sessions"] if not s["stats"]["calls"] and s["message_count"] > 2]
    if missing:
        print(f"  note: {len(missing)} session(s) had messages but no tool calls")
    return 0


def _cmd_index(args: argparse.Namespace) -> int:
    from . import source_index
    doc = source_index.build_index(args.source)
    out = Path(args.out).expanduser()
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(doc, ensure_ascii=False, indent=2), encoding="utf-8")
    resolved = sum(1 for v in doc["tools"].values() if v.get("handler_line"))
    print(f"wrote {out}  tools={len(doc['tools'])}  with_handler_line={resolved}")
    return 0


def _cmd_stats(args: argparse.Namespace) -> int:
    doc = json.loads(Path(args.trace).read_text(encoding="utf-8"))
    t = doc["totals"]
    print(f"trace {doc['schema']} generated {doc['generated_at']}")
    print(f"  sessions={t['sessions']} calls={t['calls']} errors={t['errors']} pending={t['pending']}")
    print(f"  tools={len(doc.get('by_tool', {}))}  source_index={len(doc.get('tools', {}))}")
    top = sorted(doc.get("by_tool", {}).items(), key=lambda kv: kv[1]["calls"], reverse=True)[:10]
    for name, st in top:
        print(f"    {name:<24} calls={st['calls']:<5} errors={st['errors']:<3} ms={st['ms']}")
    return 0


def _cmd_homes(args: argparse.Namespace) -> int:
    for label, home, db in state_db.discover_homes(args.home or None):
        size = db.stat().st_size / 1024 / 1024
        print(f"{label:<12} {db}  ({size:.1f} MiB)")
    return 0


def _cmd_serve(args: argparse.Namespace) -> int:
    root = Path(args.dir).expanduser().resolve()
    trace = Path(args.trace).expanduser().resolve() if args.trace else root / "trace.json"

    class Handler(http.server.SimpleHTTPRequestHandler):
        def __init__(self, *a, **kw):
            super().__init__(*a, directory=str(root), **kw)

        def do_GET(self):  # noqa: N802
            if self.path.split("?")[0] in ("/api/trace", "/api/v1/trace"):
                if not trace.is_file():
                    self.send_error(404, "no trace.json")
                    return
                body = trace.read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            super().do_GET()

        def log_message(self, fmt, *a):
            sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % a))

    with socketserver.TCPServer(("127.0.0.1", args.port), Handler) as httpd:
        print(f"htrace viewer: http://127.0.0.1:{args.port}/   (trace: {trace})")
        print("reserved backend endpoint: GET /api/trace")
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            print("\nstopped")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="htrace", description="Hermes Agent trace builder")
    parser.add_argument("--version", action="version", version=f"htrace {__version__}")
    sub = parser.add_subparsers(dest="command")

    p_build = sub.add_parser("build", help="build a trace document from state.db (+hooks)")
    p_build.add_argument("--out", default="trace.json")
    p_build.add_argument("--home", action="append", help="Hermes home (repeatable); default = autodetect")
    p_build.add_argument("--limit", type=int, default=None, help="most recent N sessions per home")
    p_build.add_argument("--hooks", action="append", help="hook JSONL file or directory (repeatable)")
    p_build.add_argument("--source", default=None, help="Hermes source tree for the tool->source index")
    p_build.add_argument("--redact", action="store_true", help="scrub secrets, home paths, e-mails")
    p_build.add_argument("--env", default=None, help="explicit .env to harvest secret values from")
    p_build.add_argument("--demo-js", action="store_true", help="emit `window.HTRACE_DEMO = ...`")
    p_build.set_defaults(func=_cmd_build)

    p_index = sub.add_parser("index", help="only build the tool->source index")
    p_index.add_argument("--source", required=True)
    p_index.add_argument("--out", default="tools.index.json")
    p_index.set_defaults(func=_cmd_index)

    p_stats = sub.add_parser("stats", help="summarise a trace document")
    p_stats.add_argument("--trace", default="trace.json")
    p_stats.set_defaults(func=_cmd_stats)

    p_homes = sub.add_parser("homes", help="list discoverable Hermes homes")
    p_homes.add_argument("--home", action="append")
    p_homes.set_defaults(func=_cmd_homes)

    p_serve = sub.add_parser("serve", help="serve the viewer locally (+ reserved /api/trace)")
    p_serve.add_argument("--dir", default=".")
    p_serve.add_argument("--trace", default=None)
    p_serve.add_argument("--port", type=int, default=8765)
    p_serve.set_defaults(func=_cmd_serve)

    args = parser.parse_args(argv)
    if not getattr(args, "func", None):
        parser.print_help()
        return 2
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
