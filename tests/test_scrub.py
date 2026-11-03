"""MEMORY / USER PROFILE text must not reach a published sidecar.

    python -m unittest discover -s tests -v
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from htrace import context  # noqa: E402

RULE = "\u2550" * 46


def _sample() -> str:
    return "\n".join([
        "# Hermes runtime environment",
        "env line stays",
        "",
        RULE,
        "MEMORY (your personal notes) [96% \u2014 2,129/2,200 chars]",
        RULE,
        "Foreign HTTPS is mostly SNI-blocked here.",
        "\u00a7",
        "Another private note.",
        "",
        RULE,
        "USER PROFILE (who the user is)",
        RULE,
        "name: Zhang, works at a bank",
        "",
        RULE,
        "## Skills",
        RULE,
        "- skill list stays public",
        "",
    ])


class PersonalNotesScrubTest(unittest.TestCase):
    def test_bodies_replaced_headings_kept(self):
        out, dropped = context.scrub_personal(_sample())
        self.assertGreater(dropped, 0)
        self.assertNotIn("SNI", out)
        self.assertNotIn("works at a bank", out)
        self.assertIn("MEMORY (your personal notes)", out)
        self.assertIn("USER PROFILE (who the user is)", out)
        self.assertIn("2,129/2,200 chars", out)          # the size annotation survives
        self.assertIn("已脱敏", out)
        self.assertIn("env line stays", out)             # unrelated sections untouched
        self.assertIn("- skill list stays public", out)

    def test_disabled_is_identity(self):
        sample = _sample()
        out, dropped = context.scrub_personal(sample, enabled=False)
        self.assertEqual(out, sample)
        self.assertEqual(dropped, 0)

    def test_truncated_block_redacts_to_end(self):
        text = "\n".join(["intro", "MEMORY (your personal notes) [96% \u2014 2,129/2,200 chars]",
                          "note one only, cut off mid-sen"])
        out, dropped = context.scrub_personal(text)
        self.assertNotIn("cut off", out)
        self.assertIn("MEMORY (your personal notes)", out)
        self.assertGreater(dropped, 0)


class RawSidecarScrubTest(unittest.TestCase):
    def test_sidecar_carries_no_personal_text(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = Path(tmp) / "htrace-context"
            (store / "raw").mkdir(parents=True)
            prompt = "\n".join([
                "# Hermes runtime environment",
                "env",
                "",
                RULE,
                "MEMORY (your personal notes) [96% \u2014 64/2,200 chars]",
                RULE,
                "SNI-blocked secret env note",
                "",
            ])
            raw = {
                "system_prompt": {"text": prompt},
                "tools": [{"text": "{}"}],
                "messages": [{"role": "user", "text": "hi", "tool_name": None}],
            }
            (store / "raw" / "ctx-a.json").write_text(json.dumps(raw), encoding="utf-8")
            req = {"kind": "ctx_req", "session_id": "s1", "api_request_id": "r1",
                   "api_call_count": 1, "ts": 1770000000, "model": "m",
                   "counts": {"user": 1},
                   "chars": {"sys": 12000, "injected": 900, "tools": 400,
                             "user": 2, "assistant": 0, "tool": 0},
                   "cjk": {"sys": 0, "injected": 0, "tools": 0,
                           "user": 0, "assistant": 0, "tool": 0},
                   "sections": [], "total_chars": 13302,
                   "raw_ref": "raw/ctx-a.json", "truncated": False, "tools_present": True}
            res = {"kind": "ctx_resp", "session_id": "s1", "api_request_id": "r1",
                   "api_call_count": 1, "usage": {"input_tokens": 3300, "output_tokens": 10},
                   "model": "m"}
            (store / "context-20260101.jsonl").write_text(
                json.dumps(req) + "\n" + json.dumps(res) + "\n", encoding="utf-8")

            doc, raw_doc = context.build([str(store)], raw_keep=50)
            self.assertEqual(doc["personal_notes"], "replaced-by-size")
            blob = json.dumps(raw_doc, ensure_ascii=False)
            self.assertNotIn("SNI", blob)
            self.assertIn("MEMORY (your personal notes)", blob)
            entry = list(raw_doc["requests"].values())[0]
            self.assertGreater(entry["personal_redacted"], 0)

            doc2, raw2 = context.build([str(store)], raw_keep=50, scrub_personal_on=False)
            self.assertEqual(doc2["personal_notes"], "published")
            self.assertIn("SNI", json.dumps(raw2, ensure_ascii=False))


if __name__ == "__main__":
    unittest.main()
