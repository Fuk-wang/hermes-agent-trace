"""Tests for the context view: segmentation, calibration, raw sidecar, plugin install.

    python -m unittest discover -s tests -v
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from htrace import context as ctx_mod  # noqa: E402
from htrace import plugin_install, segments  # noqa: E402


class SegmentsTest(unittest.TestCase):
    def test_plugin_copy_is_byte_identical(self):
        """The plugin measures sizes with its own copy; a drift silently breaks calibration."""
        repo = ROOT / "htrace" / "segments.py"
        plugin = ROOT / "plugins" / "htrace_context" / "segments.py"
        self.assertTrue(plugin.exists(), "plugin copy missing")
        self.assertEqual(repo.read_bytes(), plugin.read_bytes(),
                         "htrace/segments.py and plugins/htrace_context/segments.py drifted")

    def test_system_prompt_splits_on_headings(self):
        prompt = ("preamble text\n\n## Tool policy\nx\n\n## Skills\n- one\n- two\n\n"
                  "# Hermes runtime environment\nhost: win\n")
        res = segments.classify_system(prompt)
        self.assertEqual(res["chars"]["sys"] + res["chars"]["injected"], len(prompt))
        self.assertGreater(res["chars"]["injected"], 0)
        kinds = {s["heading"]: s["k"] for s in res["sections"]}
        self.assertEqual(kinds["Skills"], "injected")
        self.assertEqual(kinds["Tool policy"], "sys")

    def test_system_message_is_not_double_counted(self):
        """OpenAI-style requests repeat the system prompt in messages[0]."""
        system = "# Skills\n" + "x" * 400
        messages = [{"role": "system", "content": system},
                    {"role": "user", "content": "hello"}]
        seg = segments.segment_request(system_prompt=system, messages=messages, tools=None)
        self.assertEqual(sum(seg["chars"].values()), len(system) + len("hello"))

    def test_cjk_is_counted_apart(self):
        self.assertEqual(segments.cjk_chars("abc 中文测试 xyz"), 4)
        seg = segments.segment_request(system_prompt="", messages=[{"role": "user", "content": "中文"}], tools=[])
        self.assertEqual(seg["cjk"]["user"], 2)
        self.assertEqual(seg["chars"]["user"], 2)


def _events(n_requests: int = 14) -> list:
    """Synthetic capture: two profiles' worth of composition, with real usage = chars/cpt."""
    cpt = {"sys": 4.0, "injected": 3.2, "tools": 4.4, "user": 2.4, "assistant": 3.9, "tool": 1.6}
    cjk_cpt = 1.65
    out = []
    for i in range(n_requests):
        base = {"sys": 12000 if i % 2 else 22000, "injected": 2000 if i % 2 else 11000,
                "tools": 6400 if i % 3 else 37000, "user": 30 + 120 * i,
                "assistant": 140 * i, "tool": 400 * i}
        cjk = {"sys": 190, "injected": 0, "tools": 0, "user": 10 + i, "assistant": 0, "tool": 0}
        truth = round(sum((base[c] - cjk[c]) / cpt[c] for c in ctx_mod.CATEGORIES)
                      + sum(cjk.values()) / cjk_cpt)
        sid = f"s{i // 7}"
        out.append({"kind": "ctx_req", "session_id": sid, "api_request_id": f"r{i}", "api_call_count": i + 1,
                    "ts": 1000 + i, "chars": base, "cjk": cjk, "tools_present": True,
                    "sections": [{"heading": "Skills", "k": "injected", "chars": base["injected"]}],
                    "counts": {"user": 1, "assistant": 1, "tool": 1, "sys": 1},
                    "raw_ref": None, "_file": str(Path(tempfile.gettempdir()) / "x" / "context-1.jsonl")})
        out.append({"kind": "ctx_resp", "session_id": sid, "api_request_id": f"r{i}", "api_call_count": i + 1,
                    "usage": {"prompt_tokens": truth}, "_file": "same"})
    return out


class CalibrationTest(unittest.TestCase):
    def test_fit_recovers_coefficients_and_reports_deviation(self):
        rows = ctx_mod.pair(_events())
        cal = ctx_mod.fit(rows)
        self.assertGreaterEqual(cal["n"], 10)
        self.assertTrue(cal["ok"], cal.get("note"))
        self.assertLess(cal["deviation"]["max"], 0.05)
        self.assertIn("cv", cal)
        self.assertLess(cal["cv"]["max"], 0.05)
        self.assertAlmostEqual(cal["categories"]["sys"]["chars_per_token"], 4.0, delta=0.35)
        self.assertTrue(all(cal["categories"][c]["identifiable"] for c in ctx_mod.CATEGORIES))

    def test_uncalibratable_data_is_labelled_estimated(self):
        """One request cannot identify six coefficients: the UI must say estimated."""
        rows = ctx_mod.pair(_events(3))
        cal = ctx_mod.fit(rows)
        self.assertFalse(cal["ok"])
        self.assertIn("note", cal)


class RawSidecarTest(unittest.TestCase):
    def _capture(self, home: Path, n: int, raw: bool = True, prefix: str = "") -> Path:
        store = home / "htrace-context"
        raw_dir = store / "raw"
        raw_dir.mkdir(parents=True)
        lines = []
        for i in range(n):
            ref = None
            if raw:
                ref = f"raw/ctx-{i}.json"
                (raw_dir / f"ctx-{i}.json").write_text(json.dumps({
                    "system_prompt": {"text": "# Skills\nskill index\n## ENV\nhost"},
                    "messages": [{"role": "user", "text": f"question {i}", "chars": 12},
                                 {"role": "tool", "tool_name": "terminal", "text": "out", "chars": 3}],
                    "tools": [{"name": "terminal", "text": '{"name":"terminal"}'}],
                }), encoding="utf-8")
            lines.append(json.dumps({"kind": "ctx_req", "session_id": f"{prefix}s{i % 3}", "api_request_id": f"{prefix}r{i}",
                                     "api_call_count": i, "ts": 100 + i, "tools_present": True,
                                     "chars": {"sys": 100, "injected": 1000, "tools": 100, "user": 10,
                                               "assistant": 5, "tool": 3},
                                     "cjk": {}, "raw_ref": ref, "sections": []}))
            lines.append(json.dumps({"kind": "ctx_resp", "session_id": f"{prefix}s{i % 3}", "api_request_id": f"{prefix}r{i}",
                                     "api_call_count": i, "usage": {"prompt_tokens": 400 + i}}))
        (store / "context-20260101.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")
        return store

    def test_only_the_newest_requests_keep_raw_text(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = self._capture(Path(tmp), 60)
            doc, raw = ctx_mod.build([str(store)], raw_keep=50)
            self.assertEqual(len(doc["requests"]), 60)
            self.assertEqual(len(raw["requests"]), 50)
            kept = [r for r in doc["requests"] if r["has_raw"]]
            self.assertEqual(len(kept), 50)
            newest = max(doc["requests"], key=lambda r: r["ts"])
            self.assertTrue(newest["has_raw"])
            self.assertFalse(min(doc["requests"], key=lambda r: r["ts"])["has_raw"])
            # every category the drawer offers has text for a kept request
            segs = raw["requests"][newest["k"]]["segments"]
            for cat in ("sys", "injected", "tools", "user", "tool"):
                self.assertTrue(segs[cat]["text"], cat)

    def test_raw_scope_limits_which_store_publishes_text(self):
        with tempfile.TemporaryDirectory() as a, tempfile.TemporaryDirectory() as b:
            store_a = self._capture(Path(a), 4, prefix="a-")
            store_b = self._capture(Path(b), 4, prefix="b-")
            doc, raw = ctx_mod.build([str(store_a), str(store_b)], raw_paths=[str(store_a)])
            self.assertEqual(doc["totals"]["requests"], 8)
            self.assertEqual(len(raw["requests"]), 4)
            self.assertEqual(doc["totals"]["raw_kept"], 4)


class PluginInstallTest(unittest.TestCase):
    def setUp(self):
        self._orig_enable = plugin_install._set_enabled
        plugin_install._set_enabled = lambda home, enabled: None   # never touch a real config here

    def tearDown(self):
        plugin_install._set_enabled = self._orig_enable

    def test_install_backup_and_remove(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "profile"
            home.mkdir()
            (home / "config.yaml").write_text("model:\n  default: x\n", encoding="utf-8")
            self.assertEqual(plugin_install.install(home), 0)
            target = home / "plugins" / "htrace-context"
            self.assertTrue((target / "__init__.py").exists())
            self.assertTrue((target / "plugin.yaml").exists())
            self.assertEqual((target / "segments.py").read_bytes(),
                             (ROOT / "htrace" / "segments.py").read_bytes())
            self.assertTrue((home / "htrace-context").is_dir())
            # reinstalling keeps a copy of the previous tree
            self.assertEqual(plugin_install.install(home), 0)
            self.assertTrue(list((home / "backups").glob("htrace-context-*")))
            self.assertEqual(plugin_install.remove(home), 0)
            self.assertFalse(target.exists())
            self.assertTrue((home / "htrace-context").is_dir(), "captured data must survive --remove")
            self.assertEqual(plugin_install.remove(home, purge_data=True), 0)
            self.assertFalse((home / "htrace-context").exists())


if __name__ == "__main__":
    unittest.main()
