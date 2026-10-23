#!/usr/bin/env python3
"""ALLOW / ASK / DENY decision matrix (SPE-118). Synthetic gh data and synthetic settings files only; nothing touches a real host."""
import importlib.util
import io
import json
import pathlib
import sys
import tempfile
import unittest
from contextlib import redirect_stdout

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.dont_write_bytecode = True


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, ROOT / path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


pm = load("pm_tests", "tests/test-pr-merge-policy.py")
omg, HEAD, REPO, OWNERS = pm.omg, pm.HEAD, pm.REPO, pm.OWNERS
cr = load("cr_dec", "scripts/candidate-rollout.py")

BROAD_LIVE = {"permissions": {"allow": ["Bash(gh pr merge:*)", "Bash(gh pr *)", "Bash(rtk gh pr *)", "mcp__github__merge_pull_request", "Bash(ls:*)"],
                              "ask": ["Bash(gh pr merge * --admin*)"], "deny": []}}
RUNNER_ALLOW = {"permissions": {"allow": ["Bash(gh pr merge * --merge --match-head-commit *)", "Bash(rtk gh pr merge * --merge --match-head-commit *)"],
                                "ask": [], "deny": []}}


class Matrix(unittest.TestCase):
    def setUp(self):
        self.d = pathlib.Path(tempfile.mkdtemp(prefix="dec-"))
        self.addCleanup(__import__("shutil").rmtree, self.d, True)

    def settings(self, cfg):
        p = self.d / "settings.json"
        p.write_text(json.dumps(cfg))
        return str(p)

    def decide(self, fake, cfg=None):
        verdict, reasons, facts = omg.evaluate(fake, REPO, 7, OWNERS)
        cmd = omg.merge_command(facts)
        host = omg.host_permission(self.settings(cfg), cmd) if cfg is not None and verdict == "PASS" else "not_evaluated"
        return omg.decide(verdict, reasons, facts, host), facts

    # --- ALLOW -----------------------------------------------------------------------------------------------------------
    def test_owner_pr_green_with_a_host_allow_is_allow(self):
        d, _ = self.decide(pm.Fake(), RUNNER_ALLOW)
        self.assertEqual(d["outcome"], "ALLOW")
        self.assertEqual(d["dimensions"]["PR_OWNER_CLASS"], "A")
        self.assertEqual(d["dimensions"]["GITHUB_APPROVAL_REQUIREMENT"], "NOT_REQUIRED")
        self.assertEqual((d["asks"], d["denies"]), ([], []))

    def test_non_owner_with_owner_approval_and_a_host_allow_is_allow(self):
        d, _ = self.decide(pm.third_party(reviews=[pm.approval()]), RUNNER_ALLOW)
        self.assertEqual((d["outcome"], d["dimensions"]["PR_OWNER_CLASS"]), ("ALLOW", "C"))

    def test_bot_with_genuine_owner_approval_and_a_host_allow_is_allow(self):
        d, _ = self.decide(pm.bot(reviews=[pm.approval()]), RUNNER_ALLOW)
        self.assertEqual((d["outcome"], d["dimensions"]["PR_OWNER_CLASS"]), ("ALLOW", "B"))

    # --- ASK -------------------------------------------------------------------------------------------------------------
    def test_owner_pr_host_confirmation_needed_is_ask_not_a_failure(self):
        d, _ = self.decide(pm.Fake(), {"permissions": {"allow": [], "ask": ["Bash(gh pr merge:*)", "Bash(rtk gh pr merge:*)"], "deny": []}})
        self.assertEqual(d["outcome"], "ASK")
        self.assertEqual(d["dimensions"]["ENGINEERING_GATE"], "PASS")  # a PASS in one dimension...
        self.assertEqual(d["dimensions"]["HOST_TOOL_PERMISSION"], "ask")  # ...is not a PASS in all
        self.assertEqual(d["asks"][0]["resolver"], "host_prompt")

    def test_no_settings_or_no_rule_never_yields_allow(self):
        d, _ = self.decide(pm.Fake())
        self.assertEqual((d["outcome"], d["dimensions"]["HOST_TOOL_PERMISSION"]), ("ASK", "not_evaluated"))
        d, _ = self.decide(pm.Fake(), {"permissions": {"allow": ["Bash(ls:*)"], "ask": [], "deny": []}})
        self.assertEqual((d["outcome"], d["dimensions"]["HOST_TOOL_PERMISSION"]), ("ASK", "classifier"))

    def test_non_owner_without_approval_asks_for_a_genuine_owner_github_review(self):
        d, _ = self.decide(pm.third_party(), RUNNER_ALLOW)
        self.assertEqual(d["outcome"], "ASK")
        self.assertEqual(d["dimensions"]["GITHUB_APPROVAL_REQUIREMENT"], "OWNER_APPROVAL_MISSING")
        self.assertIn("owner_github_approval", [a["resolver"] for a in d["asks"]])
        self.assertNotIn("approval_scope", d)  # nothing to approve at the host until the review exists

    def test_bot_without_approval_and_a_stranger_approval_both_ask_for_the_owner(self):
        for fake in (pm.bot(), pm.bot(reviews=[pm.approval("stranger")]), pm.third_party(reviews=[pm.approval("some-maintainer")])):
            d, _ = self.decide(fake, RUNNER_ALLOW)
            self.assertEqual(d["outcome"], "ASK")
            self.assertIn("owner_github_approval", [a["resolver"] for a in d["asks"]])

    def test_stale_head_asks_to_revalidate_and_never_allows(self):
        d, _ = self.decide(pm.Fake(headRefOid="b" * 40), RUNNER_ALLOW)  # review was recorded for another head
        self.assertEqual(d["outcome"], "ASK")
        self.assertIn("agent_runs_independent_review", [a["resolver"] for a in d["asks"]])
        d, _ = self.decide(pm.Fake(behind=1), RUNNER_ALLOW)
        self.assertEqual(d["outcome"], "ASK")
        self.assertIn("agent_revalidates", [a["resolver"] for a in d["asks"]])

    def test_missing_evidence_and_open_threads_are_asks_with_a_named_resolver(self):
        f = pm.Fake()
        f.pr["comments"] = []
        d, _ = self.decide(f, RUNNER_ALLOW)
        self.assertEqual((d["outcome"], d["dimensions"]["ENGINEERING_GATE"]), ("ASK", "PENDING"))
        d, _ = self.decide(pm.Fake(threads=[{"isResolved": False, "isOutdated": False}]), RUNNER_ALLOW)
        self.assertEqual(d["outcome"], "ASK")

    def test_unknown_reason_code_is_never_allow(self):
        d = omg.decide("REFUSE", ["BRAND_NEW_REASON: something"], {"class": "A"}, "allow")
        self.assertEqual(d["outcome"], "ASK")

    def test_ask_approval_scope_is_exactly_the_pinned_operation(self):
        d, facts = self.decide(pm.Fake(), {"permissions": {"allow": [], "ask": ["Bash(gh pr merge:*)"], "deny": []}})
        self.assertEqual(d["approval_scope"], "exactly the pinned operation: " + omg.merge_command(facts))
        self.assertIn(f"--match-head-commit {HEAD}", d["approval_scope"])

    # --- DENY ------------------------------------------------------------------------------------------------------------
    def test_red_ci_is_deny_with_a_remediation_resolver_even_when_the_host_allows(self):
        d, _ = self.decide(pm.Fake(statusCheckRollup=[{"name": "ci", "status": "COMPLETED", "conclusion": "FAILURE"}]), RUNNER_ALLOW)
        self.assertEqual((d["outcome"], d["dimensions"]["ENGINEERING_GATE"]), ("DENY", "FAIL"))
        self.assertEqual(d["denies"][0]["resolver"], "author_fixes_ci")

    def test_material_violations_are_deny(self):
        for fake in (pm.Fake(isFork=True), pm.Fake(isCrossRepository=True), pm.Fake(state="CLOSED")):
            d, _ = self.decide(fake, RUNNER_ALLOW)
            self.assertEqual(d["outcome"], "DENY", fake.pr.get("state"))
            self.assertEqual(d["dimensions"]["GITHUB_MERGE_ELIGIBILITY"], "BLOCKED")

    def test_a_host_deny_rule_is_deny_and_beats_everything(self):
        d, _ = self.decide(pm.Fake(), {"permissions": {"allow": ["Bash(gh pr merge:*)"], "ask": [], "deny": ["Bash(gh pr merge:*)"]}})
        self.assertEqual((d["outcome"], d["dimensions"]["HOST_TOOL_PERMISSION"]), ("DENY", "deny"))

    def test_gate_error_fails_closed_to_deny(self):
        self.assertEqual(omg.decide("ERROR", ["x"], {}, "allow")["outcome"], "DENY")

    def test_deny_outranks_ask_when_both_apply(self):
        d = omg.decide("REFUSE", ["THIRD_PARTY_NEEDS_OWNER_APPROVAL: x", "CI_FAILED: ['ci']"], {"class": "C"}, "allow")
        self.assertEqual(d["outcome"], "DENY")
        self.assertEqual(len(d["asks"]), 1)

    # --- host rules: the merge-scope candidate, MCP, reads ---------------------------------------------------------------
    def merged_scope(self):
        return cr.merge(json.loads(json.dumps(BROAD_LIVE)), cr.wsp.merge_scope_candidate())[0]

    def test_broad_live_allows_alone_are_a_host_allow_the_candidate_turns_them_into_ask(self):
        cmd = omg.merge_command({"pr": 7, "repo": REPO, "head": HEAD})
        self.assertEqual(omg.host_permission(self.settings(BROAD_LIVE), cmd), "allow")
        self.assertEqual(omg.host_permission(self.settings(self.merged_scope()), cmd), "ask")
        d, _ = self.decide(pm.Fake(), self.merged_scope())
        self.assertEqual(d["outcome"], "ASK")  # transitional ASK-first: every pinned owner merge prompts

    def test_mcp_merge_route_cannot_silently_evade_the_authorization(self):
        perms = self.merged_scope()["permissions"]
        self.assertIn("mcp__github__merge_pull_request", perms["ask"])  # ask beats the live allow of the same tool
        self.assertIn("mcp__github__merge_pull_request", BROAD_LIVE["permissions"]["allow"])

    def test_non_merge_and_read_only_commands_are_unaffected(self):
        perms = self.merged_scope()["permissions"]
        path = self.settings({"permissions": perms})
        for cmd in ("gh pr view 7 --json mergeable", "gh pr list --json mergeable", "gh pr checks 7", "ls -la"):
            self.assertNotEqual(omg.host_permission(path, cmd), "ask", cmd)
        for kept in BROAD_LIVE["permissions"]["allow"] + BROAD_LIVE["permissions"]["ask"]:
            self.assertIn(kept, perms["allow"] + perms["ask"])

    def test_malformed_rules_in_settings_do_not_crash_the_decision(self):
        path = self.settings({"permissions": {"allow": [{"x": 1}, "Bash(gh pr merge:*)", "Bash(rtk gh pr merge:*)"], "ask": [None], "deny": []}})
        self.assertEqual(omg.host_permission(path, "gh pr merge 7"), "allow")

    def test_an_allow_for_only_one_spelling_is_not_an_allow(self):
        path = self.settings({"permissions": {"allow": ["Bash(gh pr merge:*)"], "ask": [], "deny": []}})  # the hook rewrites to `rtk gh ...`
        self.assertEqual(omg.host_permission(path, "gh pr merge 7"), "classifier")


class Cli(unittest.TestCase):
    def run_check(self, fake, *extra):
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = omg.main(["check", "--repo", REPO, "--pr", "7", "--owners", "Chisanan232", *extra], run=fake)
        return rc, json.loads(buf.getvalue())

    def test_check_prints_the_decision_and_keeps_the_gate_verdict_and_exit_code(self):
        rc, out = self.run_check(pm.Fake())
        self.assertEqual((rc, out["verdict"], out["decision"]["outcome"]), (0, "PASS", "ASK"))  # no settings: host not evaluated
        rc, out = self.run_check(pm.third_party())
        self.assertEqual((rc, out["verdict"], out["decision"]["outcome"]), (1, "REFUSE", "ASK"))
        self.assertNotIn("merge_command", out)  # an ASK that still needs a human review offers no command to run

    def test_check_with_settings_never_prints_rule_text(self):
        with tempfile.TemporaryDirectory() as d:
            p = pathlib.Path(d) / "s.json"
            p.write_text(json.dumps({"env": {"TOKEN": "DUMMY-NOT-A-SECRET"}, **RUNNER_ALLOW}))
            buf = io.StringIO()
            with redirect_stdout(buf):
                omg.main(["check", "--repo", REPO, "--pr", "7", "--owners", "Chisanan232", "--settings", str(p)], run=pm.Fake())
            self.assertNotIn("DUMMY-NOT-A-SECRET", buf.getvalue())
            self.assertEqual(json.loads(buf.getvalue())["decision"]["outcome"], "ALLOW")


if __name__ == "__main__":
    unittest.main()
