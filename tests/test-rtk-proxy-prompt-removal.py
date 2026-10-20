#!/usr/bin/env python3
"""SPE-119: retiring the broad `Bash(rtk proxy *)` ask without letting a protected command through. Scratch copies only.

The host matcher itself was measured separately (docs/ROLLOUT-RTK-PROXY-SCOPING.md); these tests are the fast regression net.
"""
import importlib.util
import json
import pathlib
import sys
import unittest

sys.dont_write_bytecode = True
ROOT = pathlib.Path(__file__).resolve().parents[1]


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, ROOT / path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


cr = load("cr", "scripts/candidate-rollout.py")
wsp, pr = cr.wsp, cr.pr
BASELINE = json.loads((ROOT / ".claude/settings.json").read_text())
LIVE = {"env": {"EXAMPLE": "DUMMY-NOT-A-REAL-SECRET"}, "model": "m",
        "permissions": {"allow": ["Bash(ls:*)", "Bash(cargo test *)"], "deny": [],
                        "ask": ["Bash(rtk proxy *)", "Bash(git push --force*)", "Bash(rtk git push --force*)", "Bash(git*--output*)", "Bash(rtk git*--output*)"]},
        "autoMode": {"environment": ["$defaults", "keep me"]}}
PARITY_GAPS = ["rtk proxy git diff HEAD --output=/tmp/x", "rtk proxy git log -p --output=f", "rtk proxy git show HEAD --ext-diff",
               "rtk proxy git diff --ext-diff HEAD~1", "rtk proxy git diff --no-index /a /b", "rtk proxy git fetch --upload-pack=cmd origin",
               "rtk proxy git diff --textconv HEAD~1 HEAD", "rtk proxy git -C /peer diff --output=/tmp/x", "rtk proxy git -c core.pager=x diff --no-index /a /b",
               "rtk proxy git diff --ext-dif HEAD", "rtk proxy git diff --outp=/tmp/x HEAD", "rtk proxy git log --tex -p"]
ROUTINE = ["rtk proxy cargo test -p fornax-cli", "rtk proxy cargo build --release", "rtk proxy cargo clippy --all-targets", "rtk proxy cargo fmt --check",
           "rtk proxy git diff origin/main...HEAD -- tests/ docs/", "rtk proxy git show origin/main:src/lib.rs", "rtk proxy git log --oneline -5",
           "rtk proxy git status --short", "rtk proxy grep -rn needle src"]
PROTECTED = list(pr.PROXY_PROTECTED_COMMANDS) + PARITY_GAPS


def merged_permissions(cand=None):
    cand = cand or wsp.proxy_scoped_candidate()
    merged, rep = cr.merge(LIVE, cand)
    return merged, rep, cr.compat(LIVE, merged, cand)


class Replacement(unittest.TestCase):
    def test_broad_ask_is_retired_and_nothing_else_is(self):
        merged, rep, _ = merged_permissions()
        self.assertNotIn("Bash(rtk proxy *)", merged["permissions"]["ask"])
        self.assertEqual(rep["asks_retired"], ["Bash(rtk proxy *)"])
        for r in LIVE["permissions"]["ask"]:
            if r != "Bash(rtk proxy *)":
                self.assertIn(r, merged["permissions"]["ask"])
        for key in ("env", "model", "autoMode"):
            self.assertEqual(merged[key], LIVE[key])
        self.assertEqual(merged["permissions"]["deny"], [])

    def test_only_scoped_wrapped_allows_are_added(self):
        merged, rep, _ = merged_permissions()
        self.assertEqual(rep["allow_added"], wsp.PROXY_ROUTINE_ALLOW)
        self.assertEqual(merged["permissions"]["allow"][:2], LIVE["permissions"]["allow"])
        for r in merged["permissions"]["allow"]:
            self.assertNotIn(r, ("Bash(rtk proxy *)", "Bash(*)", "Bash"))
            self.assertTrue(r.startswith("Bash(rtk proxy ") or r in LIVE["permissions"]["allow"], r)

    def test_every_protected_wrapped_command_still_asks_including_the_parity_gaps(self):
        merged, _, findings = merged_permissions()
        for cmd in PROTECTED:
            self.assertEqual(pr.static_decision(cmd, merged["permissions"]), "ask", cmd)
        self.assertFalse([f for f in findings if f.startswith("BLOCKING")], findings)

    def test_routine_wrapped_commands_no_longer_ask(self):
        merged, _, _ = merged_permissions()
        for cmd in ROUTINE:
            self.assertEqual(pr.static_decision(cmd, merged["permissions"]), "allow", cmd)

    def test_the_old_live_file_prompts_for_all_routine_commands(self):
        for cmd in ROUTINE:
            self.assertEqual(pr.static_decision(cmd, LIVE["permissions"]), "ask", cmd)


class NegativeControls(unittest.TestCase):
    def test_ripgrep_pre_helper_is_never_allowed_by_a_scoped_rule(self):
        merged, _, _ = merged_permissions()
        for cmd in ("rtk proxy rg --pre sh needle f", "rtk proxy rg needle", "rtk proxy rg --search-zip needle"):
            self.assertNotEqual(pr.static_decision(cmd, merged["permissions"]), "allow", cmd)
            self.assertNotEqual(pr.static_decision(cmd, BASELINE["permissions"]), "allow", cmd)

    def test_wrapping_never_makes_a_protected_command_allowed(self):
        merged, _, _ = merged_permissions()
        for cmd in ("rtk proxy git push --force origin x", "rtk proxy git -C /p push --force origin x", "rtk proxy git reset --hard HEAD",
                    "rtk proxy gcloud secrets versions access latest --secret=s", "rtk proxy terraform apply", "rtk proxy sh -c 'git push -f'"):
            self.assertNotEqual(pr.static_decision(cmd, merged["permissions"]), "allow", cmd)

    def test_a_candidate_that_drops_the_scoped_asks_is_blocked(self):
        cand = wsp.proxy_scoped_candidate()
        cand["permissions"]["ask"] = [r for r in cand["permissions"]["ask"] if "worktree" not in r]
        _, _, findings = merged_permissions(cand)
        self.assertTrue(any(f.startswith("BLOCKING protected wrapped command would not ask") for f in findings), findings)

    def test_a_candidate_without_the_parity_asks_is_blocked_on_the_flag_forms(self):
        cand = wsp.proxy_scoped_candidate()
        cand["permissions"]["ask"] = [r for r in cand["permissions"]["ask"] if r not in wsp.PROXY_PARITY_ASKS]
        _, _, findings = merged_permissions(cand)
        blocked = " ".join(f for f in findings if f.startswith("BLOCKING protected wrapped command would not ask"))
        for flag in ("--output", "--ext-diff", "--no-index", "--upload-pack", "--textconv"):
            self.assertIn(flag, blocked, flag)

    def test_an_exact_allow_outside_the_scoped_set_is_blocked(self):
        cand = wsp.proxy_scoped_candidate()
        cand["permissions"]["allow"] = cand["permissions"]["allow"] + ["Bash(rtk proxy python3 -c 'print(1)')"]
        _, _, findings = merged_permissions(cand)
        self.assertTrue(any(f.startswith("BLOCKING candidate allow is not one of the scoped") for f in findings), findings)

    def test_nextest_is_allowed_only_for_run(self):
        merged, _, _ = merged_permissions()
        self.assertEqual(pr.static_decision("rtk proxy cargo nextest run -p x", merged["permissions"]), "allow")
        self.assertNotEqual(pr.static_decision("rtk proxy cargo nextest self update --yes", merged["permissions"]), "allow")

    def test_the_tool_refuses_to_retire_any_other_ask(self):
        cand = wsp.proxy_scoped_candidate()
        cand["permissions"]["retire_ask"] = ["Bash(rtk proxy *)", "Bash(git push --force*)"]
        merged, rep, findings = merged_permissions(cand)
        self.assertIn("Bash(git push --force*)", merged["permissions"]["ask"])
        self.assertEqual(rep["retire_refused"], ["Bash(git push --force*)"])
        self.assertTrue(any(f.startswith("BLOCKING candidate asks to retire") for f in findings), findings)

    def test_a_broad_wrapper_allow_or_blanket_bash_allow_fails_the_lint(self):
        for bad in ("Bash(rtk proxy *)", "Bash(rtk proxy:*)", "Bash(*)", "Bash(rtk proxy git push:*)"):
            self.assertTrue(wsp.lint_settings({"permissions": {"allow": [bad]}}), bad)


class WrappedTwins(unittest.TestCase):
    @staticmethod
    def sample(rule):
        return rule[len("Bash("):-1].replace(":*", " x").replace("**", "x").replace("*", "x")

    def test_every_bare_protected_ask_also_asks_when_wrapped(self):
        merged, _, _ = merged_permissions()
        for rule in wsp.ASK_BASH_RULES:
            wrapped = "rtk proxy " + self.sample(rule)
            self.assertEqual(pr.static_decision(wrapped, merged["permissions"]), "ask", wrapped)
            self.assertEqual(pr.static_decision(wrapped, BASELINE["permissions"]), "ask", wrapped)

    def test_release_publish_and_wrapped_merge_ask(self):
        merged, _, _ = merged_permissions()
        for cmd in ("rtk proxy gh release create v1.0", "rtk proxy cargo publish", "rtk proxy npm publish", "rtk proxy twine upload dist/x",
                    "rtk proxy gh pr merge 7 --repo a/b --merge --match-head-commit " + "0" * 40):
            self.assertEqual(pr.static_decision(cmd, merged["permissions"]), "ask", cmd)

    def test_abbreviated_long_git_options_still_ask(self):
        merged, _, _ = merged_permissions()
        for cmd in ("rtk proxy git diff --ext-dif HEAD", "rtk proxy git diff --ext HEAD", "rtk proxy git diff --ou=/tmp/x HEAD",
                    "rtk proxy git diff --outp=/tmp/x HEAD", "rtk proxy git log --tex -p", "rtk proxy git show --text HEAD"):
            self.assertEqual(pr.static_decision(cmd, merged["permissions"]), "ask", cmd)

    def test_allow_only_candidate_still_runs_the_protection_check(self):
        live = {"permissions": {"ask": [], "allow": []}}
        cand = {"permissions": {"ask": [], "allow": ["Bash(rtk proxy git diff:*)"], "deny": []}}
        merged, _ = cr.merge(live, cand)
        findings = cr.compat(live, merged, cand)
        self.assertTrue(any(f.startswith("BLOCKING protected wrapped command would not ask") and "--output" in f for f in findings), findings)


class RepositoryState(unittest.TestCase):
    def test_baseline_carries_the_scoped_asks_and_allows_but_no_broad_rule(self):
        perm = BASELINE["permissions"]
        self.assertNotIn("Bash(rtk proxy *)", perm["ask"] + perm["allow"])
        self.assertTrue(set(wsp.PROXY_PARITY_ASKS) <= set(perm["ask"]))
        self.assertTrue(set(wsp.PROXY_ROUTINE_ALLOW) <= set(perm["allow"]))
        for cmd in PROTECTED:
            self.assertEqual(pr.static_decision(cmd, perm), "ask", cmd)

    def test_example_candidate_matches_the_generator_and_lints_clean(self):
        example = json.loads((ROOT / "docs/examples/candidate-settings-rtk-proxy-scoped.example.json").read_text())
        self.assertEqual(example, wsp.proxy_scoped_candidate())
        self.assertEqual(wsp.lint_settings(example), [])


if __name__ == "__main__":
    unittest.main()
