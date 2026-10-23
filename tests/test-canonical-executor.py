#!/usr/bin/env python3
"""Canonical merge executor selection (SPE-118). Synthetic gh data, synthetic settings and a scratch HOME; nothing touches the real ~/.claude."""
import importlib.util
import io
import json
import os
import pathlib
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.dont_write_bytecode = True


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, ROOT / path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


pm = load("pm_ce", "tests/test-pr-merge-policy.py")
omg, HEAD, REPO = pm.omg, pm.HEAD, pm.REPO
wsp = load("wsp_ce", "scripts/workstation-policy.py")
ASKS = wsp.merge_scope_candidate()["permissions"]["ask"]
FACTS = {"repo": REPO, "pr": 7, "head": HEAD, "class": "A"}


def settings(home, rule=True, extra_allow=(), deny=()):
    allow = ["Bash(gh pr merge:*)", "Bash(rtk gh pr *)", "mcp__github__merge_pull_request", *extra_allow]
    if rule:
        allow.append(wsp.trusted_runner_rule(home))
    return {"permissions": {"allow": allow, "ask": ASKS, "deny": list(deny)}}


class Home:
    """A scratch HOME with an executable runner, an allowlist naming REPO and a settings file."""

    def __init__(self, test, runner=True, repos=(REPO,), rule=True, deny=()):
        self.d = tempfile.TemporaryDirectory()
        test.addCleanup(self.d.cleanup)
        self.home = os.path.realpath(self.d.name)
        os.makedirs(os.path.join(self.home, ".claude", "bin"))
        self.path = os.path.join(self.home, ".claude", "bin", "owner-merge-runner")
        if runner:
            with open(self.path, "w") as fh:
                fh.write("#!/bin/sh\n")
            os.chmod(self.path, 0o755)
        self.settings = os.path.join(self.home, "settings.json")
        for path, doc in ((os.path.join(self.home, ".claude", "owner-merge-runner-allowlist.json"), {"repos": {r: {} for r in repos}}),
                          (self.settings, settings(self.home, rule, deny=deny))):
            with open(path, "w") as fh:
                json.dump(doc, fh)

    def select(self, facts=FACTS):
        return omg.select_executor([self.settings], facts, self.home, *omg.runner_available(self.home))


class Selection(unittest.TestCase):
    def test_runner_is_the_executor_when_installed_allowlisted_and_host_allowed(self):
        sel = Home(self).select()
        self.assertEqual((sel["executor"], sel["host"]), ("owner-merge-runner", "allow"))
        self.assertIn(f"--pr 7 --expect-head {HEAD} --review-evidence", sel["command"])
        self.assertTrue(sel["evidence_path"].endswith(f"{REPO.replace('/', '__')}-7-{HEAD[:12]}.json"))
        self.assertEqual(omg.decide("PASS", [], FACTS, sel["host"], sel["command"])["outcome"], "ALLOW")

    def test_the_runner_command_is_exactly_what_the_single_allow_rule_matches(self):
        h = Home(self)
        sel = h.select()
        self.assertEqual(omg.host_permission([h.settings], sel["command"], rtk_twin=False), "allow")
        self.assertEqual(omg.host_permission([h.settings], "python3 " + sel["command"], rtk_twin=False), "classifier")  # a different spelling is not authorized

    def test_fallbacks_are_the_direct_command_with_a_host_ask(self):
        for label, h in (("no rule (ASK-first only)", Home(self, rule=False)), ("runner not installed", Home(self, runner=False)),
                         ("repo not allowlisted", Home(self, repos=("other/repo",))), ("no allowlist entries", Home(self, repos=()))):
            sel = h.select()
            self.assertEqual((sel["executor"], sel["host"]), ("direct-gh", "ask"), label)
            self.assertEqual(sel["command"], omg.merge_command(FACTS), label)
            self.assertEqual(omg.decide("PASS", [], FACTS, sel["host"], sel["command"])["outcome"], "ASK", label)

    def test_a_host_deny_on_the_runner_command_is_never_routed_around(self):
        h = Home(self, deny=["Bash(*owner-merge-runner*)"])
        sel = h.select()
        self.assertEqual(sel["executor"], "direct-gh")  # runner not offered; the direct command still asks (it is not made allow)
        self.assertNotEqual(sel["host"], "allow")

    def test_repo_match_is_case_insensitive_and_a_symlinked_or_non_executable_runner_is_unavailable(self):
        h = Home(self)
        self.assertEqual(h.select({**FACTS, "repo": REPO.upper()})["executor"], "owner-merge-runner")
        os.chmod(h.path, 0o644)
        self.assertEqual(omg.runner_available(h.home)[0], False)
        os.chmod(h.path, 0o755)
        os.rename(h.path, h.path + ".real")
        os.symlink(h.path + ".real", h.path)
        self.assertEqual(omg.runner_available(h.home)[0], False)

    def test_a_malformed_allowlist_means_no_runner(self):
        h = Home(self)
        with open(os.path.join(h.home, ".claude", "owner-merge-runner-allowlist.json"), "w") as fh:
            fh.write("{not json")
        self.assertEqual(omg.runner_available(h.home)[1], set())
        self.assertEqual(h.select()["executor"], "direct-gh")


class Cli(unittest.TestCase):
    def check(self, h, fake, *extra):
        buf = io.StringIO()
        with mock.patch.dict(os.environ, {"HOME": h.home}), redirect_stdout(buf):
            rc = omg.main(["check", "--repo", REPO, "--pr", "7", "--owners", "Chisanan232", "--settings", h.settings, *extra], run=fake)
        return rc, json.loads(buf.getvalue())

    def test_owner_pr_gets_the_runner_command_and_allow(self):
        rc, out = self.check(Home(self), pm.Fake())
        self.assertEqual((rc, out["verdict"], out["decision"]["outcome"], out["executor"]), (0, "PASS", "ALLOW", "owner-merge-runner"))
        self.assertTrue(out["merge_command"].startswith(out["merge_command"].split(" ")[0]) and "owner-merge-runner" in out["merge_command"])
        self.assertIn("independent review PASS", out["evidence_note"])
        self.assertIn("owner-merge-runner", out["decision"]["approval_scope"])

    def test_without_the_rule_the_direct_command_asks(self):
        rc, out = self.check(Home(self, rule=False), pm.Fake())
        self.assertEqual((rc, out["decision"]["outcome"], out["executor"]), (0, "ASK", "direct-gh"))
        self.assertEqual(out["merge_command"], omg.merge_command(FACTS))
        self.assertNotIn("evidence_path", out)

    def test_non_owner_pr_without_owner_approval_stays_ask_and_offers_no_command(self):
        rc, out = self.check(Home(self), pm.third_party())
        self.assertEqual((rc, out["verdict"], out["decision"]["outcome"]), (1, "REFUSE", "ASK"))
        self.assertNotIn("merge_command", out)

    def test_failing_ci_is_not_offered_any_executor(self):
        rc, out = self.check(Home(self), pm.Fake(statusCheckRollup=[{"name": "t", "status": "COMPLETED", "conclusion": "FAILURE"}]))
        self.assertNotEqual(out["verdict"], "PASS")
        self.assertNotIn("merge_command", out)
        self.assertNotEqual(out["decision"]["outcome"], "ALLOW")


if __name__ == "__main__":
    unittest.main()
