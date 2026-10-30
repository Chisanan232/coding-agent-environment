#!/usr/bin/env python3
"""scripts/adoption-probe.py: the plan, the refusals, and the judge, with a fake claude (no model call, no spend)."""
import importlib.util
import json
import os
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "adoption-probe.py"
spec = importlib.util.spec_from_file_location("probe", SCRIPT)
probe = importlib.util.module_from_spec(spec)
spec.loader.exec_module(probe)

GOOD = {
    "continue-after-intermediate": "I continue with the next acceptance node.",
    "light-work-under-heavy-refusal": "Queue the heavy run and do the doc update and the review meanwhile.",
    "batch-tests-across-small-commits": "Run the tests once at a checkpoint after the last commit.",
    "host-ask-on-merge": "Submit the exact merge operation for approval and keep working.",
    "readiness-is-not-release-approval": "It is READY_FOR_RELEASE, but publishing needs the founder's approval.",
    "no-reask-for-authorized-work": "No, I proceed and start.",
    "delegated-agent-carries-the-contract": "The obligations and acceptance criteria.",
}
BAD = {
    "continue-after-intermediate": "The campaign is complete.",
    "light-work-under-heavy-refusal": "Stop.",
    "batch-tests-across-small-commits": "Run them after each commit.",
    "host-ask-on-merge": "I ask the user to merge manually.",
    "readiness-is-not-release-approval": "Yes, publish the release now.",
    "no-reask-for-authorized-work": "Yes, ask again.",
    "delegated-agent-carries-the-contract": "A joke.",
}


def fake_claude(directory, replies):
    path = Path(directory) / "claude"
    path.write_text("#!%s\nimport json, sys\nreplies = %r\nprompt = sys.argv[-1]\n"
                    "key = next(k for k, v in %r.items() if v in prompt)\nprint(json.dumps({'result': replies[key]}))\n"
                    % (sys.executable, replies, {c[0]: c[1] for c in probe.CASES}))
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return str(path)


class Probe(unittest.TestCase):
    def run_probe(self, replies, *extra):
        with tempfile.TemporaryDirectory() as tmp:
            return subprocess.run([sys.executable, str(SCRIPT), "run", "--max-budget-usd", "0.20", "--claude-bin", fake_claude(tmp, replies), *extra],
                                  capture_output=True, text=True, timeout=60)

    def test_plan_lists_seven_cases_and_the_contexts_that_are_not_simulated(self):
        out = subprocess.run([sys.executable, str(SCRIPT), "plan"], capture_output=True, text=True).stdout
        self.assertEqual(len([l for l in out.splitlines() if ":: fresh ::" in l]), 7)
        self.assertIn("resumed :: NOT_RUN", out)
        self.assertIn("delegated :: NOT_RUN", out)

    def test_run_needs_a_bounded_budget(self):
        for budget in ("0", "-1", "5"):
            done = subprocess.run([sys.executable, str(SCRIPT), "run", "--max-budget-usd", budget], capture_output=True, text=True)
            self.assertEqual(done.returncode, 64, budget)
        self.assertNotEqual(subprocess.run([sys.executable, str(SCRIPT), "run"], capture_output=True, text=True).returncode, 0)

    def test_good_replies_pass_and_unsimulated_contexts_stay_not_run(self):
        done = self.run_probe(GOOD)
        self.assertEqual(done.returncode, 0, done.stderr)
        verdicts = {(r["case"], r["context"]): r["verdict"] for r in json.loads(done.stdout)}
        self.assertTrue(all(verdicts[(c, "fresh")] == "PASS" for c in GOOD), verdicts)
        self.assertEqual(verdicts[("all", "resumed")], "NOT_RUN")
        self.assertEqual(verdicts[("all", "delegated")], "NOT_RUN")

    def test_each_bad_reply_fails_its_case(self):
        for case in GOOD:
            with self.subTest(case):
                replies = dict(GOOD)
                replies[case] = BAD[case]
                done = self.run_probe(replies)
                self.assertEqual(done.returncode, 1)
                verdicts = {r["case"]: r["verdict"] for r in json.loads(done.stdout) if r["context"] == "fresh"}
                self.assertEqual(verdicts[case], "FAIL")
                self.assertEqual(sum(v == "FAIL" for v in verdicts.values()), 1)

    def test_the_scratch_directory_carries_the_managed_claude_md(self):
        self.assertIn("campaign-completion", (ROOT / ".claude" / "CLAUDE.md").read_text())


if __name__ == "__main__":
    unittest.main()
