#!/usr/bin/env python3
"""Tests for scripts/branch-protection-rollout.py (SPE-118) with a stateful fake gh. No network, no real repository."""
import importlib.util
import json
import os
import pathlib
import sys
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("bpr", ROOT / "scripts/branch-protection-rollout.py")
bpr = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(bpr)

REPO, BRANCH, CHECKS = "Chisanan232/demo-repo", "main", ["isolated-validation", "gitleaks"]


def get_shape(p):
    """What GET returns for a protection set by `p` (GitHub nests booleans under {"enabled": ...})."""
    rsc = p["required_status_checks"]
    return {"required_status_checks": {"strict": rsc["strict"], "contexts": rsc["contexts"],
                                       "checks": [{"context": c, "app_id": None} for c in rsc["contexts"]]},
            "enforce_admins": {"enabled": p["enforce_admins"]}, "allow_force_pushes": {"enabled": p["allow_force_pushes"]},
            "allow_deletions": {"enabled": p["allow_deletions"]}}


class FakeGh:
    def __init__(self, existing=None, rules=(), put_rc=0, drift=None):
        self.prot = existing
        self.rules, self.put_rc, self.drift = list(rules), put_rc, drift
        self.calls = []

    def __call__(self, argv, stdin=None):
        self.calls.append((list(argv), stdin))
        if argv[0] == "api" and argv[1].endswith("/protection") and len(argv) == 2:
            if self.prot is None:
                return 1, "", "gh: Branch not protected (HTTP 404)"
            return 0, json.dumps(self.prot), ""
        if argv[0] == "api" and "/rules/branches/" in argv[1]:
            return 0, json.dumps([{"type": t, "ruleset_id": 1, "parameters": {"required_status_checks": [{"context": c} for c in CHECKS]}}
                                  for t in self.rules]), ""
        if argv[:3] == ["api", "-X", "PUT"]:
            if self.put_rc:
                return 1, "", "HTTP 403"
            p = json.loads(stdin)
            self.prot = get_shape(p)
            if self.drift:
                self.prot = self.drift(self.prot)
            return 0, "{}", ""
        if argv[:3] == ["api", "-X", "DELETE"]:
            self.prot = None
            return 0, "", ""
        raise AssertionError(argv)

    def writes(self):
        return [c for c in self.calls if c[0][:2] == ["api", "-X"]]


class T(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        bpr.BACKUPS = os.path.join(self.tmp.name, "backups")
        self.out = []

    def run_cmd(self, cmd, gh, sha=None, enforce=True):
        argv = [cmd, "--repo", REPO, "--branch", BRANCH, "--checks", ",".join(CHECKS)]
        if sha:
            argv += ["--expect-payload-sha", sha]
        if not enforce:
            argv.append("--no-enforce-admins")
        self.out = []
        return bpr.main(argv, gh=gh, out=self.out.append)

    def sha(self, enforce=True):
        return bpr.payload_sha(bpr.payload(CHECKS, enforce))

    def test_plan_is_read_only_and_reports_the_pin(self):
        gh = FakeGh()
        self.assertEqual(self.run_cmd("plan", gh), 0)
        self.assertEqual(gh.writes(), [])
        self.assertEqual(self.out[0]["payload_sha256"], self.sha())
        self.assertTrue(self.out[0]["can_apply"])
        self.assertIn("bind the owner", self.out[0]["owner_effect"])

    def test_plan_without_enforce_admins_warns_owner_can_bypass(self):
        self.run_cmd("plan", FakeGh(), enforce=False)
        self.assertIn("owner can bypass", self.out[0]["owner_effect"])

    def test_apply_needs_the_exact_pin(self):
        gh = FakeGh()
        self.assertEqual(self.run_cmd("apply", gh), 2)
        self.assertEqual(self.run_cmd("apply", gh, sha="0" * 64), 2)
        self.assertEqual(self.run_cmd("apply", gh, sha=self.sha(False)), 2)  # a different payload's pin
        self.assertEqual(gh.writes(), [])

    def test_apply_writes_reads_back_backs_up_and_prints_rollback(self):
        gh = FakeGh()
        self.assertEqual(self.run_cmd("apply", gh, sha=self.sha()), 0)
        self.assertTrue(self.out[0]["applied"] and self.out[0]["readback_matches"])
        self.assertEqual(len(gh.writes()), 1)
        put, stdin = gh.writes()[0]
        self.assertEqual(put, ["api", "-X", "PUT", f"repos/{REPO}/branches/{BRANCH}/protection", "--input", "-"])
        body = json.loads(stdin)
        self.assertIs(body["enforce_admins"], True)
        self.assertIs(body["allow_force_pushes"], False)
        self.assertEqual(body["required_status_checks"]["contexts"], sorted(CHECKS))
        self.assertIsNone(body["required_pull_request_reviews"])
        self.assertNotIn("required_linear_history", body)  # merge commits must stay possible
        path = self.out[0]["backup"]
        self.assertEqual(oct(os.stat(path).st_mode & 0o777), "0o600")
        with open(path) as f:
            self.assertIsNone(json.load(f)["prior"])
        self.assertIn("rollback", self.out[0]["rollback"])

    def test_apply_refuses_existing_protection_or_ruleset(self):
        for gh in (FakeGh(existing=get_shape(bpr.payload(["x"], True))), FakeGh(rules=["required_status_checks"])):
            self.assertEqual(self.run_cmd("apply", gh, sha=self.sha()), 2)
            self.assertEqual(gh.writes(), [])

    def test_apply_reports_a_put_failure_and_a_readback_that_differs(self):
        gh = FakeGh(put_rc=1)
        self.assertEqual(self.run_cmd("apply", gh, sha=self.sha()), 5)
        self.assertFalse(self.out[0]["applied"])
        gh = FakeGh(drift=lambda s: dict(s, enforce_admins={"enabled": False}))
        self.assertEqual(self.run_cmd("apply", gh, sha=self.sha()), 5)
        self.assertFalse(self.out[0]["readback_matches"])

    def test_verify_requires_match_and_runner_acceptance(self):
        gh = FakeGh()
        self.run_cmd("apply", gh, sha=self.sha())
        before = len(gh.writes())
        self.assertEqual(self.run_cmd("verify", gh), 0)
        self.assertEqual(len(gh.writes()), before)
        self.assertTrue(self.out[-1]["matches_payload"] and self.out[-1]["runner_server_side_check"])
        gh.prot["enforce_admins"] = {"enabled": False}  # owner could bypass: runner's own check must object too
        self.assertEqual(self.run_cmd("verify", gh), 5)
        self.assertEqual(self.run_cmd("verify", FakeGh()), 5)

    def test_rollback_only_deletes_what_apply_wrote(self):
        gh = FakeGh()
        self.run_cmd("apply", gh, sha=self.sha())
        edited = json.loads(json.dumps(gh.prot))
        edited["required_status_checks"]["contexts"].append("extra")
        edited["required_status_checks"]["checks"].append({"context": "extra", "app_id": None})
        gh.prot = edited
        self.assertEqual(self.run_cmd("rollback", gh, sha=self.sha()), 2)  # later edit is not silently lost
        self.assertIsNotNone(gh.prot)
        gh.prot = get_shape(bpr.payload(CHECKS, True))
        self.assertEqual(self.run_cmd("rollback", gh, sha="1" * 64), 2)
        self.assertEqual(self.run_cmd("rollback", gh, sha=self.sha()), 0)
        self.assertIsNone(gh.prot)
        self.assertTrue(self.out[0]["rolled_back"])
        self.assertEqual(self.run_cmd("rollback", gh, sha=self.sha()), 0)  # idempotent

    def test_bad_arguments_and_unreadable_state_fail_closed(self):
        self.assertEqual(bpr.main(["plan", "--repo", "bad repo", "--branch", "main", "--checks", "a"], gh=FakeGh(), out=print), 64)
        self.assertEqual(bpr.main(["plan", "--repo", REPO, "--branch", "a/../b", "--checks", "a"], gh=FakeGh(), out=print), 64)
        self.assertEqual(bpr.main(["plan", "--repo", REPO, "--branch", "main", "--checks", ","], gh=FakeGh(), out=print), 64)

        def boom(argv, stdin=None):
            return 1, "", "HTTP 500"
        self.assertEqual(self.run_cmd("plan", boom), 4)
        self.assertEqual(self.run_cmd("apply", boom, sha=self.sha()), 4)


if __name__ == "__main__":
    unittest.main()
