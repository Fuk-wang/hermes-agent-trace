"""Smoke tests for the htrace parser. stdlib unittest only.

    python -m unittest discover -s tests -v
"""

from __future__ import annotations

import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from htrace import hooks_jsonl, redact, state_db  # noqa: E402


def make_db(path: Path) -> None:
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        create table sessions(
            id text primary key, source text, user_id text, session_key text, chat_id text,
            chat_type text, thread_id text, display_name text, origin_json text,
            expiry_finalized int, model text, model_config text, system_prompt text,
            system_prompt_hash text, parent_session_id text, started_at real, ended_at real,
            end_reason text, message_count int, tool_call_count int, input_tokens int,
            output_tokens int, cache_read_tokens int, cache_write_tokens int,
            reasoning_tokens int, cwd text, git_branch text, git_repo_root text,
            git_metadata_generation int, billing_provider text, billing_base_url text,
            billing_mode text, estimated_cost_usd real, actual_cost_usd real, cost_status text,
            cost_source text, pricing_version text, title text, title_source text,
            last_activity_at real, last_activity_description text, last_activity_provenance text,
            api_call_count int, handoff_state text, handoff_platform text, handoff_error text,
            compression_failure_cooldown_until real, compression_failure_error text,
            compression_fallback_streak int, compression_ineffective_count int,
            compression_recovery_deadline real, profile_name text, transport_profile text,
            rewind_count int, archived int, pinned int, hidden int, last_read_at real,
            tool_names text
        );
        create table messages(
            id integer primary key autoincrement, session_id text, role text, content text,
            tool_call_id text, tool_calls text, tool_name text, effect_disposition text,
            timestamp real, token_count int, finish_reason text, reasoning text,
            reasoning_content text, reasoning_details text, codex_reasoning_items text,
            codex_message_items text, platform_message_id text, observed int,
            _compressed_summary int default 0, active int default 1, compacted int default 0,
            api_content text, display_kind text, display_metadata text, display_identity blob,
            display_order int
        );
        create table async_delegations(
            delegation_id text primary key, origin_session text, origin_ui_session_id text,
            parent_session_id text, state text, dispatched_at real, completed_at real,
            updated_at real, event_json text, result_json text, delivery_state text,
            delivery_attempts int, delivered_at real, owner_pid int, owner_started_at int,
            task_json text, delivery_claim text, delivery_claimed_at real, origin_session_id text
        );
        """
    )
    conn.execute(
        "insert into sessions(id, source, model, started_at, message_count, tool_call_count,"
        " input_tokens, output_tokens, cache_read_tokens, estimated_cost_usd, cost_status,"
        " title, cwd, tool_names) values"
        " ('s1','cli','deepseek-flash',1000.0,4,2,100,50,900,0.0123,'estimated','demo','/tmp','[\"read_file\"]')"
    )
    conn.execute("insert into messages(session_id, role, content, timestamp) values ('s1','user','hello',1000.0)")
    conn.execute(
        "insert into messages(session_id, role, content, tool_calls, timestamp) values"
        " ('s1','assistant','', ?, 1001.0)",
        (json.dumps([{"id": "c1", "call_id": "c1", "type": "function",
                      "function": {"name": "read_file", "arguments": '{"path": "a.txt"}'}}]),),
    )
    conn.execute(
        "insert into messages(session_id, role, content, tool_call_id, tool_name, timestamp)"
        " values ('s1','tool','{\"content\":\"hi\"}','c1','read_file',1003.5)"
    )
    conn.execute(
        "insert into messages(session_id, role, content, tool_calls, timestamp) values ('s1','assistant','', ?, 1004.0)",
        (json.dumps([{"id": "c2", "function": {"name": "terminal", "arguments": '{"command":"false"}'}}]),),
    )
    conn.execute(
        "insert into messages(session_id, role, content, tool_call_id, tool_name, timestamp)"
        " values ('s1','tool','{\"exit_code\":1,\"output\":\"\"}','c2','terminal',1004.4)"
    )
    conn.execute(
        "insert into messages(session_id, role, content, tool_calls, timestamp) values ('s1','assistant','', ?, 1005.0)",
        (json.dumps([{"id": "c3", "function": {"name": "search_files", "arguments": "{}"}}]),),
    )
    conn.commit()
    conn.close()


class StateDbTest(unittest.TestCase):
    def test_pairing_duration_status(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "state.db"
            make_db(db)
            home = state_db.load_home("default", Path(tmp), db)
            session = home["sessions"][0]
            calls = {c["call_id"]: c for c in session["calls"]}
            self.assertEqual(len(calls), 3)
            self.assertEqual(calls["c1"]["duration_ms"], 2500)
            self.assertEqual(calls["c1"]["status"], "ok")
            self.assertEqual(calls["c2"]["status"], "error")
            self.assertEqual(calls["c3"]["status"], "pending")  # never paired, never dropped
            self.assertIsNone(calls["c3"]["ended_at"])
            self.assertEqual(session["stats"]["calls"], 3)
            self.assertEqual(session["stats"]["errors"], 1)
            self.assertEqual(session["stats"]["pending"], 1)
            self.assertEqual(session["tokens"]["input"], 100)

    def test_hook_only_home_is_empty_not_fatal(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(state_db.discover_homes([tmp]), [])


class HooksFoldTest(unittest.TestCase):
    def test_live_and_documented_shapes(self):
        events = [
            {"hook_event_name": "pre_tool_call", "tool_name": "terminal",
             "extra": {"tool_call_id": "x"}, "_htrace_ts": 10.0},
            {"hook_event_name": "post_tool_call", "tool_name": "terminal", "tool_input": {"command": "ls"},
             "extra": {"tool_call_id": "x", "result": "{}", "duration_ms": 42}, "_htrace_ts": 10.042},
            {"hook_event_name": "post_tool_call", "tool_name": "read_file", "args": {"path": "b"},
             "result": "{}", "duration_ms": 7, "session_id": "flat", "tool_call_id": "y",
             "_htrace_ts": 11.0},
        ]
        fold = hooks_jsonl.fold(events)
        nested = fold["unknown"]["calls"][0]
        self.assertEqual(nested["name"], "terminal")
        self.assertEqual(nested["duration_ms"], 42)
        self.assertEqual(nested["args"], {"command": "ls"})
        flat = fold["flat"]["calls"][0]
        self.assertEqual(flat["duration_ms"], 7)


class RedactTest(unittest.TestCase):
    def test_secrets_paths_and_escaped_paths(self):
        doc = {
            "a": "key sk-abcdefghijklmnopqrstuvwx and ghp_abcdefghijklmnopqrstuvwxyz0123",
            "b": r"C:\Users\alice\secret.txt",
            "c": r"C:\\\\Users\\\\alice\\\\nested.json",
            "d": "alice ran `ls -la` then mailed alice@example.com",
            "e": "Authorization: Bearer abcdefghijklmnopqrstuvwxyz",
        }
        out, stats = redact.redact_document(doc, env_values=["supersecretvalue"], home_names=["alice"])
        blob = json.dumps(out)
        self.assertNotIn("sk-abcdefghijklmnopqrstuvwx", blob)
        self.assertNotIn("ghp_", blob)
        self.assertNotIn("alice", blob)
        self.assertNotIn("example.com", blob)
        self.assertNotIn("abcdefghijklmnopqrstuvwxyz", blob)
        self.assertGreater(stats["username"], 0)
        self.assertGreater(stats["home_path"], 0)


if __name__ == "__main__":
    unittest.main()
