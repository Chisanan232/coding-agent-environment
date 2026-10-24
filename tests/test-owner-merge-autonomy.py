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
    """Permission-rule glob as measured on the real host: `*` is a wildcard, `\\*` is a literal star, `prefix:*` is a word-bounded prefix."""
    if pattern.endswith(":*"):
        head = pattern[:-2]
        return cmd == head or cmd.startswith(head + " ")
    out, i = [], 0
    while i < len(pattern):
        if pattern[i] == "\\" and pattern[i + 1:i + 2] == "*":
            out.append(re.escape("*"))
            i += 2
        elif pattern[i] == "*":
            out.append(".*")
            i += 1
        elif pattern[i] == "?":   # measured on the real host: ? is a single-character wildcard
            out.append(".")
            i += 1
        else:
            out.append(re.escape(pattern[i]))
            i += 1
    return re.fullmatch("".join(out), cmd, re.S) is not None


def decide(cmd):
    cmd = " ".join(cmd.split())   # measured on the real host: a tab or a doubled space matches a space in a rule
    cmd = re.sub(r"(^|\s)#.*$", "", cmd).rstrip()   # measured: a trailing shell comment is stripped before matching, so its text is invisible to every rule
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

    def test_quotes_and_backslashes_cannot_disguise_a_flag(self):
        for pre in PREFIXES:
            base = gate_cmd(pre)
            for disguised in ('--ad""min', "--ad''min", '--"admin"', '-"s"'):
                self.assertEqual(decide(base.replace(" --merge", f" {disguised} --merge", 1)), "ask", (pre, disguised))
                self.assertEqual(decide(f"{base} {disguised}"), "ask", (pre, disguised))
            self.assertEqual(decide(f"{pre} pr review 7 --repo a/b --approve=true --comment"), "ask", pre)

    # glob wildcards (cannot be expressed in a pattern) and characters that cannot change how the shell splits or expands the command
    UNCOVERED = set("?!#%+,@^]}~\\")   # ? and the backslash cannot be matched by a rule on the real host (measured); a literal * is asked with an escaped star

    def test_every_character_outside_the_standard_form_asks(self):
        import string
        standard = set(string.ascii_letters + string.digits + " ._/=:-")
        for pre in PREFIXES:
            base = gate_cmd(pre)
            for ch in map(chr, range(33, 127)):
                if ch in standard or ch in self.UNCOVERED:
                    continue
                for cmd in (f"{base}{ch}", base.replace(" --merge", f"{ch}--merge", 1)):
                    self.assertEqual(decide(cmd), "ask", (pre, repr(ch), cmd))

    def test_no_rule_contains_whitespace_other_than_a_plain_space(self):
        # measured on the real host: a tab or newline inside a rule is normalised to a space, so such an ask matches every command and makes the
        # standard pinned merge ask every time (the positive control failed with these rules present)
        for r in BLOCK["add_ask"] + BLOCK["add_allow"]:
            self.assertFalse(set(r) & set("\t\n\r\f\v"), repr(r))

    def test_a_tab_cannot_separate_a_flag_from_the_pin(self):
        for pre in PREFIXES:
            base = gate_cmd(pre)
            for tail in ("--admin", "-s", "--squash", "--delete-branch"):
                self.assertEqual(decide(f"{base}\t{tail}"), "ask", (pre, tail))
                self.assertEqual(decide(base.replace(" --merge", f"\t{tail} --merge", 1)), "ask", (pre, tail))
            self.assertEqual(decide(f"{pre} pr review 7 --repo a/b\t--approve --comment"), "ask", pre)

    def test_a_shell_comment_can_neither_hide_nor_supply_the_pin(self):
        # measured on the real host: comment text is stripped before matching, so a pin that appears only in a comment does not make the command
        # rule-allowed; it falls to the classifier like any unlisted merge. Ask rules cannot match comment text either, which is why none exist.
        for pre in PREFIXES:
            for cmd in (f"{pre} pr merge 7 --repo a/b --merge # --merge --match-head-commit {SHA}", f"{pre} pr merge 7 --repo a/b --merge #--match-head-commit {SHA}"):
                self.assertEqual(decide(cmd), "classifier", cmd)
            self.assertEqual(decide(gate_cmd(pre) + " # note"), "allow")   # a harmless comment after the full pinned form is still the pinned form

    def test_ordinary_review_and_comment_bodies_stay_routine(self):
        # quotes, issue references and short body flags are normal in comments; the review allow therefore carries no generic metacharacter asks
        for pre in PREFIXES:
            for cmd in (f"{pre} pr review 7 --repo a/b --comment --body ok", f"{pre} pr review 7 --repo a/b --comment -b ok",
                        f'{pre} pr review 7 --repo a/b --comment --body "Looks fine, see #12"', f'{pre} pr comment 7 --repo a/b --body "Fixes #12: ok"'):
                self.assertEqual(decide(cmd), "allow", cmd)

    def test_review_approval_in_a_disguised_spelling_is_a_documented_residual(self):
        # quoting or brace expansion can reassemble --approve after the text was matched; the review allow cannot catch that without asking on
        # every ordinary body. The built-in Self-Approval protection is the only other control, and it is not deterministic (see the doc residual).
        for pre in PREFIXES:
            for disguised in ('--ap""prove', "--{approve,comment=false}"):
                self.assertEqual(decide(f"{pre} pr review 7 --repo a/b {disguised} --comment=false --body ok"), "allow", (pre, disguised))

    def test_comment_reviews_are_allowed_and_plain_approvals_ask(self):
        for pre in PREFIXES:
            self.assertEqual(decide(f"{pre} pr review 7 --repo a/b --comment --body ok"), "allow", pre)
            for bad in ("--approve", "-a", "-ca", "-ac", "-r", "--comment --approve", "--request-changes"):
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


class HostCorpus(unittest.TestCase):
    CORPUS = json.loads((ROOT / "docs/permission-corpus/owner-merge-corpus.json").read_text())["commands"]

    def test_example_candidate_equals_the_documented_block(self):
        ex = json.loads((ROOT / "docs/examples/owner-merge-pinned-allow.example.json").read_text())["permissions"]
        self.assertEqual((ex["allow"], ex["ask"], ex["deny"]), (BLOCK["add_allow"], BLOCK["add_ask"], []))

    def test_corpus_is_nonexistent_targets_only_and_matches_the_emulation(self):
        ids = [c["id"] for c in self.CORPUS]
        self.assertEqual(len(ids), len(set(ids)))
        for c in self.CORPUS:
            if "--repo" in c["cmd"]:
                self.assertIn("zz-nonexistent-probe-repo", c["cmd"], c["id"])
            if c["class"] == "UNKNOWN_REQUIRES_REVIEW":
                self.assertNotEqual(decide(c["cmd"]), "ask", (c["id"], "documented residual: no ask rule can catch it"))
                continue
            self.assertEqual(decide(c["cmd"]), c["expect"], (c["id"], c["cmd"]))
        self.assertGreaterEqual(len(self.CORPUS), 30)


class BodyFromFile(unittest.TestCase):
    def test_no_allowed_pr_verb_can_post_a_local_file(self):
        for pre in PREFIXES:
            for verb in ("comment 7", "create --title t", "edit 7", "review 7 --comment"):
                for flag in ("--body-file ./f", "--body-file=./f", "-F ./f", "-F./f", "--template ./f", "-T ./f"):
                    self.assertEqual(decide(f"{pre} pr {verb} --repo a/b {flag}"), "ask", (pre, verb, flag))
                self.assertEqual(decide(f"{pre} pr {verb} --repo a/b --body ok"), "allow", (pre, verb))


class StoredResults(unittest.TestCase):
    def test_stored_results_cover_exactly_the_corpus_and_record_the_tested_commands(self):
        res = json.loads((ROOT / "docs/permission-corpus/owner-merge-results-2026-10-22.json").read_text())
        corpus = {c["id"]: c for c in HostCorpus.CORPUS}
        self.assertEqual({e["id"] for e in res["cases"]}, set(corpus))
        self.assertTrue(res["score"]["valid"])
        for e in res["cases"]:
            recorded = " ".join(e["recorded"])
            if corpus[e["id"]]["class"] == "UNKNOWN_REQUIRES_REVIEW" and not e["recorded"]:
                continue   # a measured residual that produced no hook event (the command never ran)
            self.assertTrue(e["recorded"], e["id"])
            for tok in corpus[e["id"]]["cmd"].split():
                if any(ch in tok for ch in "${`*#"):   # expansions and globs legitimately change what the shell passes on
                    continue
                strip = lambda x: x.replace('"', "").replace("'", "").replace("\\", "")
                self.assertIn(strip(tok), strip(recorded), (e["id"], tok))
            corpus_flags = {t for t in corpus[e["id"]]["cmd"].split() if t.startswith("--")}
            for tok in e["recorded"][0].split() if e["recorded"] else []:
                if tok.startswith("--"):   # a recorded flag the corpus command never had means a different operation ran
                    self.assertIn(tok, corpus_flags, (e["id"], tok))
            c = corpus[e["id"]]
            if c["class"] == "AUTO_SAFE":
                self.assertEqual(e["outcome"], "ran-no-prompt", e["id"])
            elif c["class"] == "OWNER_ASK":
                self.assertEqual(e["outcome"], "held", e["id"])
        self.assertEqual(res["score"]["protected_leaked"], [])


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
