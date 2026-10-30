#!/usr/bin/env python3
"""Baseline generator: single source feeds both host spans, drift and size budget are caught."""
import importlib.util
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("build_baseline", ROOT / "scripts" / "build-baseline.py")
bb = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bb)

BASE = "<!-- block:one -->\nalpha\nbeta\n<!-- /block:one -->\n\n<!-- block:two -->\ngamma\n<!-- /block:two -->\n"
HOST = ("head\n<!-- coding-agent-environment:baseline:one:start -->\nOLD\n<!-- coding-agent-environment:baseline:one:end -->\n"
        "mid\n<!-- coding-agent-environment:baseline:two:start -->\nOLD2\n<!-- coding-agent-environment:baseline:two:end -->\ntail\n")


class Generator(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        d = Path(self.tmp.name)
        self.base, self.host = d / "base.md", d / "host.md"
        self.base.write_text(BASE)
        self.host.write_text(HOST)
        self.targets = {self.host: ("one", "two")}

    def tearDown(self):
        self.tmp.cleanup()

    def test_check_flags_drift_and_write_fixes_it_idempotently(self):
        self.assertTrue(bb.run("check", self.base, self.targets))
        self.assertEqual(bb.run("write", self.base, self.targets), [])
        self.assertEqual(self.host.read_text().count("alpha\nbeta"), 1)
        self.assertIn("head\n", self.host.read_text())
        self.assertIn("tail\n", self.host.read_text())
        self.assertEqual(bb.run("check", self.base, self.targets), [])
        before = self.host.read_text()
        bb.run("write", self.base, self.targets)
        self.assertEqual(self.host.read_text(), before)

    def test_hand_edit_between_markers_is_detected(self):
        bb.run("write", self.base, self.targets)
        self.host.write_text(self.host.read_text().replace("gamma", "gamma EDITED"))
        self.assertTrue(any("drift" in p for p in bb.run("check", self.base, self.targets)))

    def test_missing_or_duplicate_markers_fail_closed(self):
        self.host.write_text(HOST.replace("<!-- coding-agent-environment:baseline:two:end -->", ""))
        self.assertTrue(any("markers" in p for p in bb.run("check", self.base, self.targets)))
        self.host.write_text(HOST + HOST)
        self.assertTrue(any("markers" in p for p in bb.run("check", self.base, self.targets)))

    def test_undefined_block_and_duplicate_definition(self):
        self.assertTrue(any("not defined" in p for p in bb.run("check", self.base, {self.host: ("one", "nope")})))
        self.base.write_text(BASE + BASE)
        with self.assertRaises(ValueError):
            bb.run("check", self.base, self.targets)

    def test_size_budget(self):
        self.base.write_text("<!-- block:one -->\n" + "x" * (bb.SIZE_BUDGET + 1) + "\n<!-- /block:one -->\n")
        self.assertTrue(any("budget" in p for p in bb.run("check", self.base, {self.host: ("one",)})))


class RepoInSync(unittest.TestCase):
    def test_host_files_match_the_baseline(self):
        self.assertEqual(bb.run("check"), [])

    def test_decision_delegation_and_merge_sequence_are_defined_once(self):
        for host in bb.TARGETS:
            text = host.read_text()
            self.assertEqual(text.count("Decision delegation: act as senior engineer"), 1, host)
            self.assertEqual(text.count("Merge an eligible PR only with"), 1, host)
            self.assertEqual(text.count("Standing contract (universal"), 1, host)


if __name__ == "__main__":
    unittest.main()
