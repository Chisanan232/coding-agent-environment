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
    "readiness-is-not-release-approval": "It is READY_FOR_RELEASE, but publishing needs the founder's separate explicit approval.",
    "no-reask-for-authorized-work": "No, I proceed and start.",
    "delegated-agent-carries-the-contract": "The obligations and the acceptance criteria.",
}
# Replies that read like the right answer but state the wrong stance; a judge that only looks for keywords passes them.
BAD = {
    "continue-after-intermediate": ["The campaign is complete.", "I will not continue; I report completion.", "I stop here and report."],
    "light-work-under-heavy-refusal": ["Stop.", "I will wait for the other build and not touch docs or review.", "Idle until the build finishes, then review."],
    "batch-tests-across-small-commits": ["Run them after each commit.", "Run the tests once per commit.", "Every commit gets its own full run."],
    "host-ask-on-merge": ["I ask the user to merge manually.", "I ask the founder to approve and perform the merge.", "I hand the merge back to the owner."],
    "readiness-is-not-release-approval": ["Yes, publish the release now.", "Yes, I publish now; it is ready for release; no approval needed.", "Sure, publishing now since READY_FOR_RELEASE."],
    "no-reask-for-authorized-work": ["Yes, ask again.", "I should ask for permission again before I start.", "I would ask again before starting."],
    "delegated-agent-carries-the-contract": ["Nothing, there is no need to include anything in the prompt.", "The subagent already knows the rules; assume it has loaded them."],
}


def fake_claude(directory, replies, cost=0.001, log=None):
    path = Path(directory) / "claude"
    path.write_text("#!%s\nimport json, os, sys\nreplies = %r\nprompt = sys.argv[-1]\n"
                    "open(%r, 'a').write(json.dumps({'argv': sys.argv[1:-1], 'cwd': os.getcwd(), 'has_md': os.path.exists('CLAUDE.md')}) + '\\n')\n"
                    "key = next(k for k, v in %r.items() if v in prompt)\n"
                    "reply = replies[key]\nreply = reply[0] if isinstance(reply, tuple) else reply\n"
                    "print(json.dumps({'result': reply, 'total_cost_usd': %r}))\n"
                    % (sys.executable, replies, str(log or Path(directory) / "calls.jsonl"), {c[0]: c[1] for c in probe.CASES}, cost))
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return str(path)


class Probe(unittest.TestCase):
    def run_probe(self, replies, *extra, budget="0.20"):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        done = subprocess.run([sys.executable, str(SCRIPT), "run", "--max-budget-usd", budget, "--claude-bin", fake_claude(tmp.name, replies), *extra],
                              capture_output=True, text=True, timeout=60)
        return done

    def verdicts(self, done):
        return {(r["case"], r.get("variant", r.get("context"))): r["verdict"] for r in json.loads(done.stdout)["results"]}

    def test_plan_lists_seven_cases_and_the_contexts_that_are_not_simulated(self):
        out = subprocess.run([sys.executable, str(SCRIPT), "plan"], capture_output=True, text=True).stdout
        self.assertEqual(len([l for l in out.splitlines() if ":: fresh ::" in l]), 7)
        self.assertIn("resumed :: NOT_RUN", out)
        self.assertIn("delegated :: NOT_RUN", out)

    def test_run_needs_a_bounded_budget(self):
        for budget in ("0", "-1", "5", "nan", "inf", "0.01"):
            done = subprocess.run([sys.executable, str(SCRIPT), "run", "--max-budget-usd", budget], capture_output=True, text=True)
            self.assertEqual(done.returncode, 64, budget)
        self.assertNotEqual(subprocess.run([sys.executable, str(SCRIPT), "run"], capture_output=True, text=True).returncode, 0)

    def test_the_per_session_budget_is_rounded_down_so_the_total_is_never_exceeded(self):
        done = self.run_probe(GOOD, budget="0.20")                      # 0.20 / 7 = 0.0285... -> 0.02 each, never 0.03
        calls = [json.loads(l) for l in (self.dir / "calls.jsonl").read_text().splitlines()]
        budgets = {c["argv"][c["argv"].index("--max-budget-usd") + 1] for c in calls}
        self.assertEqual(budgets, {"0.02"})
        self.assertLessEqual(len(calls) * 0.02, 0.20)
        self.assertLessEqual(json.loads(done.stdout)["spent_usd_at_most"], 0.20)

    def test_the_session_is_isolated_and_tool_free(self):
        self.run_probe(GOOD)
        call = json.loads((self.dir / "calls.jsonl").read_text().splitlines()[0])
        argv = call["argv"]
        for flag in ("--no-session-persistence", "--strict-mcp-config", "--disable-slash-commands"):
            self.assertIn(flag, argv)
        self.assertEqual(argv[argv.index("--tools") + 1], "")
        self.assertEqual(argv[argv.index("--setting-sources") + 1], "project")
        self.assertTrue(call["has_md"])                                  # the repository CLAUDE.md is in the scratch directory

    def test_good_replies_pass_and_unsimulated_contexts_stay_not_run(self):
        done = self.run_probe(GOOD)
        self.assertEqual(done.returncode, 0, done.stderr)
        verdicts = self.verdicts(done)
        self.assertTrue(all(verdicts[(c, "baseline")] == "PASS" for c in GOOD), verdicts)
        self.assertEqual(verdicts[("all", "resumed")], "NOT_RUN")
        self.assertEqual(verdicts[("all", "delegated")], "NOT_RUN")

    def test_each_wrong_stance_fails_its_case_even_when_it_uses_the_right_words(self):
        for case, wrongs in BAD.items():
            for wrong in wrongs:
                with self.subTest(case=case, reply=wrong):
                    replies = dict(GOOD)
                    replies[case] = wrong
                    done = self.run_probe(replies)
                    self.assertEqual(done.returncode, 1)
                    verdicts = {r["case"]: r["verdict"] for r in json.loads(done.stdout)["results"] if r.get("variant") == "baseline"}
                    self.assertEqual(verdicts[case], "FAIL")
                    self.assertEqual(sum(v == "FAIL" for v in verdicts.values()), 1)

    def test_the_control_run_reports_which_cases_the_repository_file_changes(self):
        done = self.run_probe(GOOD, "--with-control", budget="0.28")
        results = json.loads(done.stdout)["results"]
        self.assertEqual(len([r for r in results if r.get("variant") == "control"]), 7)
        self.assertEqual({r["verdict"] for r in results if r.get("variant") == "delta"}, {"NOT_ATTRIBUTABLE"})   # the same fake answers both
        control_calls = [json.loads(l) for l in (self.dir / "calls.jsonl").read_text().splitlines() if not json.loads(l)["has_md"]]
        self.assertEqual(len(control_calls), 7)                          # the control directory has no CLAUDE.md

    def test_an_error_reply_is_an_error_not_a_fail_and_a_crash_loses_nothing(self):
        script = Path(tempfile.mkdtemp()) / "claude"
        script.write_text("#!%s\nimport json\nprint(json.dumps({'is_error': True, 'result': 'Not logged in'}))\n" % sys.executable)
        script.chmod(0o755)
        done = subprocess.run([sys.executable, str(SCRIPT), "run", "--max-budget-usd", "0.2", "--claude-bin", str(script)], capture_output=True, text=True)
        verdicts = {r["verdict"] for r in json.loads(done.stdout)["results"] if r.get("variant") == "baseline"}
        self.assertEqual(verdicts, {"ERROR"})
        missing = subprocess.run([sys.executable, str(SCRIPT), "run", "--max-budget-usd", "0.2", "--claude-bin", "/nonexistent/claude"], capture_output=True, text=True)
        self.assertEqual({r["verdict"] for r in json.loads(missing.stdout)["results"] if r.get("variant") == "baseline"}, {"ERROR"})


if __name__ == "__main__":
    unittest.main()
