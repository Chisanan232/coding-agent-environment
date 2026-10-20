#!/usr/bin/env python3
"""The gate-emitted merge command must be the one shape the narrow allow rule covers (SPE-118).

Permission globs are emulated (`*` = any characters). Ask rules are evaluated before allow rules,
so each risky flag must match an ask rule even though the allow glob also matches it.
"""
import importlib.util
import pathlib
import re
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("omg", ROOT / "scripts/owner-merge-gate.py")
omg = importlib.util.module_from_spec(spec)
spec.loader.exec_module(omg)

ALLOW = "gh pr merge * --merge --match-head-commit *"
ASKS = ["gh pr merge * --admin*", "gh pr merge --admin*", "gh pr merge * --squash*", "gh pr merge * --rebase*", "gh pr merge * --auto*"]
SHA = "0123456789abcdef0123456789abcdef01234567"
SIMPLE_ONLY = re.compile(r"^[A-Za-z0-9 ._/=:-]+$")   # no ; & | $ ( ) ` < > quotes or newlines


def glob(pattern, cmd):
    return re.fullmatch(".*".join(re.escape(p) for p in pattern.split("*")), cmd) is not None


def decide(cmd):
    if any(glob(a, cmd) for a in ASKS):
        return "ask"
    return "allow" if glob(ALLOW, cmd) else "classifier"


class PinnedForm(unittest.TestCase):
    def cmd(self):
        return omg.merge_command({"pr": 7, "repo": "Chisanan232/x", "head": SHA})

    def test_gate_command_is_allowed_and_simple(self):
        c = self.cmd()
        self.assertEqual(decide(c), "allow")
        self.assertRegex(c, SIMPLE_ONLY)
        self.assertIn(f"--match-head-commit {SHA}", c)

    def test_risky_flags_ask_even_though_allow_glob_matches(self):
        base = self.cmd()
        for flag in ("--admin", "--squash", "--rebase", "--auto"):
            self.assertEqual(decide(f"{base} {flag}"), "ask", flag)
            self.assertEqual(decide(base.replace("--merge", flag)), "ask", flag)

    def test_unpinned_or_compound_shapes_do_not_get_the_fast_path(self):
        self.assertEqual(decide("gh pr merge 7 --repo a/b --merge"), "classifier")
        self.assertEqual(decide("gh pr merge 7 --repo a/b --squash"), "ask")
        for bad in (f"cd /x && {self.cmd()}", f"X=1; {self.cmd()}"):
            self.assertEqual(decide(bad), "classifier", bad)

    def test_broad_allow_is_not_in_the_documented_diff_additions(self):
        doc = (ROOT / "docs/OWNER-MERGE-AUTONOMY.md").read_text()
        added = [l for l in doc.splitlines() if l.startswith("+") and "gh pr merge:*" in l]
        self.assertEqual(added, [])


if __name__ == "__main__":
    unittest.main()
