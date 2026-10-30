#!/usr/bin/env python3
"""Contract-level checks for the founder's autonomous-completion directive (SPE-144).

They prove the directive's text is in the always-loaded baseline of both hosts and in the decision-delegation skill, and that
the documented decision order yields the expected next action for each behavior the founder named. They do not prove a live
model obeys it: that needs a fresh-process host probe, reported separately (docs/ADOPTION-AND-RELOAD.md).
"""
import json
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FIX = json.loads((Path(__file__).parent / "fixtures/campaign-completion.json").read_text())
SKILL = ROOT / ".claude/skills/decision-delegation/SKILL.md"
HOSTS = {"claude": ROOT / ".claude/CLAUDE.md", "codex": ROOT / "codex/AGENTS.md"}


def flat(text):
    return re.sub(r"\s+", " ", text)


def next_action(state):
    """Reference decision order from the baseline block and the skill's campaign completion loop."""
    event = state["event"]
    if event == "merge_host_ask":
        return "SUBMIT_EXACT_MERGE_OPERATION"
    if event == "merge_host_denied":
        return "REPORT_DENIAL_ONCE_AND_CONTINUE"
    if event == "small_commits_made":
        return "BATCH_TESTS_AT_CHECKPOINT"
    if event == "heavy_admission_refused":
        if state.get("light_tasks", 0) > 0:
            return "QUEUE_HEAVY_AND_DO_LIGHT_WORK"
        return "REPORT_BLOCKED_WITH_WATCHER" if state.get("watcher") else "ARM_WATCHER_THEN_CONTINUE"
    if event == "ci_wait" and not state.get("watcher"):
        return "ARM_WATCHER_THEN_CONTINUE"
    if state.get("dag_nodes_remaining", 1) > 0:
        return "RUN_NEXT_TASK"
    if state.get("release_approved"):
        return "RELEASE_WITHIN_APPROVED_SCOPE"
    return "REPORT_READY_FOR_RELEASE_AND_REQUEST_RELEASE_APPROVAL"


class Scenarios(unittest.TestCase):
    def test_reference_order_matches_the_fixture(self):
        for s in FIX["scenarios"]:
            with self.subTest(s["id"]):
                self.assertEqual(next_action(s["state"]), s["expected"])

    def test_no_intermediate_result_ends_the_campaign(self):
        for event in ("pr_merged", "tests_passed", "handoff_sent"):
            self.assertEqual(next_action({"event": event, "dag_nodes_remaining": 1}), "RUN_NEXT_TASK")


class Text(unittest.TestCase):
    skill = flat(SKILL.read_text())

    def test_both_hosts_carry_the_same_block_with_the_five_behaviors(self):
        blocks = {}
        for host, path in HOSTS.items():
            text = path.read_text()
            start = "<!-- coding-agent-environment:baseline:campaign-completion:start -->"
            end = "<!-- coding-agent-environment:baseline:campaign-completion:end -->"
            self.assertEqual(text.count(start), 1, host)
            blocks[host] = flat(text[text.index(start): text.index(end)])
        self.assertEqual(blocks["claude"], blocks["codex"])
        block = blocks["claude"]
        for phrase in ["is an intermediate result, never completion", "select and run the next executable task",
                       "evidence-backed technical `READY_FOR_RELEASE`", "needs the founder's separate explicit approval",
                       "A refused heavy admission does not forbid light work", "queue the heavy task through the admission mechanism",
                       "record its identifier, trigger, expiry and next action", "claim automatic resumption only if it exists",
                       "Do not re-ask for already-authorized work"]:
            with self.subTest(phrase):
                self.assertIn(phrase, block)

    def test_batching_and_merge_ask_are_in_the_always_loaded_contract(self):
        for host, path in HOSTS.items():
            text = flat(path.read_text())
            for phrase in ["Commit granularity is separate from validation frequency",
                           "not after every commit",
                           "If the host asks, submit that exact operation and continue"]:
                with self.subTest(host=host, phrase=phrase):
                    self.assertIn(phrase, text)

    def test_skill_holds_the_detailed_loop(self):
        for phrase in ["### Campaign completion loop", "a merged PR, a passing test batch, a review, a checkpoint, a handoff, a status report",
                       "what was started or completed, the remaining core release blockers, any real watcher and its next action",
                       "A watcher that does not exist is not claimed",
                       "defers heavy builds and full suites only",
                       "does not require a build or full suite per commit",
                       "Neither returns the merge to the founder to do by hand",
                       "never includes a release, deployment or publication"]:
            with self.subTest(phrase):
                self.assertIn(phrase, self.skill)


if __name__ == "__main__":
    unittest.main()
