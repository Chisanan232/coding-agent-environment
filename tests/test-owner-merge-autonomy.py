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


def gh_merge_flags():
    """Every flag of the installed `gh pr merge`, read from its own help (skipped when gh is absent)."""
    import shutil
    import subprocess
    if not shutil.which("gh"):
        return None
    res = subprocess.run(["gh", "pr", "merge", "--help"], capture_output=True, text=True, timeout=20)
    text = res.stdout + res.stderr
    flags = set()
    for line in text.split("\nFLAGS", 1)[-1].split("\nINHERITED", 1)[0].splitlines():
        m = re.match(r"\s+(?:(-[A-Za-z]), )?(--[a-z][a-z-]*)", line)
        if m:
            flags.update(f for f in m.groups() if f)
    return flags


class EveryMergeFlag(unittest.TestCase):
    PERMITTED = {"--repo", "-R", "--merge", "--match-head-commit"}

    def test_every_other_gh_pr_merge_flag_asks_in_every_position(self):
        flags = gh_merge_flags()
        if flags is None:
            self.skipTest("gh not installed")
        flags = {f for f in flags if f not in self.PERMITTED | {"-h", "--help"}}
        self.assertTrue({"--admin", "--squash", "-s", "--delete-branch", "--disable-auto", "--subject"} <= flags, flags)
        for pre in PREFIXES:
            base = gate_cmd(pre)
            for f in sorted(flags):
                val = f if f.startswith("--") and f in ("--admin", "--auto", "--disable-auto", "--squash", "--rebase", "--delete-branch") or f in ("-s", "-r", "-d", "-m") else f"{f} x"
                for cmd in (f"{base} {val}", base.replace(" --repo", f" {val} --repo", 1), base.replace(" --merge", f" {val} --merge", 1)):
                    self.assertEqual(decide(cmd), "ask", (pre, f, cmd))

    def test_short_flag_clusters_ask(self):
        flags = gh_merge_flags()
        if flags is None:
            self.skipTest("gh not installed")
        shorts = sorted(f[1] for f in flags if re.fullmatch(r"-[A-Za-z]", f) and f not in ("-R", "-m", "-h")) + ["m"]
        for pre in PREFIXES:
            base = gate_cmd(pre)
            for a in shorts:
                for b in shorts:
                    for tail in (f"-{a}{b}", f"-{a}"):
                        self.assertEqual(decide(f"{base} {tail}"), "ask", (pre, tail))
                        self.assertEqual(decide(base.replace(" --repo", f" {tail} --repo", 1)), "ask", (pre, tail))
                        self.assertEqual(decide(base.replace(" --merge", f" {tail}tx --merge", 1)), "ask", (pre, tail))

    def test_shell_expansion_in_the_pin_asks(self):
        for pre in PREFIXES:
            base = gate_cmd(pre)
            for pin in ("$''", '$""', "$(printf '')", "`printf ''`", "${EMPTY}", "{a,}", "$'\\x00'"):
                self.assertEqual(decide(base.replace(f"--match-head-commit {SHA}", f"--match-head-commit {pin}")), "ask", (pre, pin))

    def test_comment_reviews_are_allowed_and_any_approval_asks(self):
        for pre in PREFIXES:
            self.assertEqual(decide(f"{pre} pr review 7 --repo a/b --comment --body ok"), "allow", pre)
            for bad in ("--approve", "-a", "-ca", "-ac", "-b x -a", "--comment --approve", "--request-changes"):
                self.assertEqual(decide(f"{pre} pr review 7 --repo a/b {bad}"), "ask", (pre, bad))

    def test_sole_empty_pin_and_merge_false_ask(self):
        for pre in PREFIXES:
            base = gate_cmd(pre)
            for cmd in (base.replace(f"--match-head-commit {SHA}", "--match-head-commit ''"), base.replace(f"--match-head-commit {SHA}", '--match-head-commit ""'),
                        f"{base} --merge=false -s", base.replace("--merge", "--subject --merge", 1), f"{base} -d", f"{base} --disable-auto"):
                self.assertEqual(decide(cmd), "ask", cmd)

    def test_routine_pr_verbs_keep_a_scoped_allow(self):
        for pre in PREFIXES:
            for verb in ("checkout 7 --repo a/b", "update-branch 7 --repo a/b", "view 7", "checks 7", "diff 7"):
                self.assertEqual(decide(f"{pre} pr {verb}"), "allow", (pre, verb))


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
