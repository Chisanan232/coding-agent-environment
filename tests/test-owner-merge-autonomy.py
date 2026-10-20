#!/usr/bin/env python3
"""The documented owner-merge rules (docs/OWNER-MERGE-AUTONOMY.md) against the gate's command shape (SPE-118).

The rule block is parsed out of the document, so the test checks what the owner will actually apply. Permission
globs are emulated (`*` = any characters, `prefix:*` = the command or that prefix followed by a space). Ask rules
win over allow rules, as documented. The emulation is a fast regression net; it does not replace the measurements
against the real host matcher recorded in the document.
"""
import importlib.util
import json
import pathlib
import re
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("omg", ROOT / "scripts/owner-merge-gate.py")
omg = importlib.util.module_from_spec(spec)
spec.loader.exec_module(omg)

DOC = (ROOT / "docs/OWNER-MERGE-AUTONOMY.md").read_text()
BLOCK = json.loads(re.search(r"```json\n(\{.*?\})\n```", DOC, re.S).group(1))
ALLOW = [r[5:-1] for r in BLOCK["add_allow"]]
ASK = [r[5:-1] for r in BLOCK["add_ask"]]
SHA = "0123456789abcdef0123456789abcdef01234567"
SIMPLE_ONLY = re.compile(r"^[A-Za-z0-9 ._/=:-]+$")   # no ; & | $ ( ) ` < > quotes or newlines
PREFIXES = ("gh", "rtk gh")


def glob(pattern, cmd):
    if pattern.endswith(":*"):
        head = pattern[:-2]
        return cmd == head or cmd.startswith(head + " ")
    return re.fullmatch(".*".join(re.escape(p) for p in pattern.split("*")), cmd) is not None


def decide(cmd):
    if any(glob(a, cmd) for a in ASK):
        return "ask"
    return "allow" if any(glob(a, cmd) for a in ALLOW) else "classifier"


def gate_cmd(prefix):
    c = omg.merge_command({"pr": 7, "repo": "Chisanan232/x", "head": SHA})
    return c.replace("gh ", prefix + " ", 1) if prefix != "gh" else c


class PinnedForm(unittest.TestCase):
    def test_gate_command_is_allowed_and_simple_in_both_spellings(self):
        for pre in PREFIXES:
            c = gate_cmd(pre)
            self.assertEqual(decide(c), "allow", c)
            self.assertRegex(c, SIMPLE_ONLY)
            self.assertIn(f"--match-head-commit {SHA}", c)

    def test_risky_flags_ask_in_both_spellings(self):
        for pre in PREFIXES:
            base = gate_cmd(pre)
            for flag in ("--admin", "--squash", "--rebase", "--auto"):
                self.assertEqual(decide(f"{base} {flag}"), "ask", f"{pre} {flag}")
                self.assertEqual(decide(base.replace("--merge", flag)), "ask", f"{pre} {flag}")

    def test_a_second_or_empty_pin_cannot_blank_the_head_pin(self):
        for pre in PREFIXES:
            base = gate_cmd(pre)
            self.assertEqual(decide(f"{base} --match-head-commit="), "ask", pre)
            self.assertEqual(decide(f"{base} --match-head-commit ''"), "ask", pre)
            self.assertEqual(decide(f"{base} --match-head-commit {SHA}"), "ask", pre)
            self.assertEqual(decide(base.replace(f"--match-head-commit {SHA}", "--match-head-commit=")), "ask", pre)

    def test_unpinned_merge_never_gets_the_fast_path(self):
        for pre in PREFIXES:
            self.assertEqual(decide(f"{pre} pr merge 7 --repo a/b --merge"), "classifier", pre)

    def test_approval_is_asked_not_allowed(self):
        for pre in PREFIXES:
            self.assertEqual(decide(f"{pre} pr review 7 --repo a/b --approve"), "ask", pre)


class DocumentedBlock(unittest.TestCase):
    def test_no_broad_pr_allow_is_added_and_each_removal_is_the_broad_one(self):
        for r in BLOCK["add_allow"]:
            self.assertNotRegex(r, r"gh pr (merge)?:?\*\)$", r)
            self.assertFalse(r.endswith("gh pr *)"), r)
        self.assertEqual(set(BLOCK["remove_allow"]), {"Bash(gh pr merge:*)", "Bash(gh pr *)", "Bash(rtk gh pr *)"})

    def test_every_allowed_merge_prefix_has_an_ask_twin_for_each_risky_flag(self):
        for pre in PREFIXES:
            for flag in ("--admin", "--squash", "--rebase", "--auto", "--match-head-commit="):
                self.assertTrue(any(pre + " pr merge" in r and flag in r for r in ASK), (pre, flag))

    def test_no_allow_overlaps_a_non_merge_ask_verb(self):
        for a in ALLOW:
            self.assertFalse(glob(a, "gh pr review 7 --approve") or glob(a, "rtk gh pr review 7 --approve"), a)


if __name__ == "__main__":
    unittest.main()
