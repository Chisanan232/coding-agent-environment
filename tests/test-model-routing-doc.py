#!/usr/bin/env python3
"""docs/MODEL-ROUTING.md stays honest: Sonnet default, Haiku opt-in, and the repo really has no Haiku route (SPE-126)."""
import json
import re
import sys
import unittest
from pathlib import Path

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[1]
DOC = (ROOT / "docs/MODEL-ROUTING.md").read_text()


class ModelRoutingDoc(unittest.TestCase):
    def test_baseline_is_stated_and_nothing_is_claimed_deployed(self):
        self.assertIn("Sonnet medium as main", DOC)
        self.assertIn("selective Opus high", DOC)
        self.assertIn("Nothing here is deployed", DOC)
        self.assertIn("opt-in only", DOC)

    def test_admission_table_has_a_never_haiku_row_and_a_fail_closed_row(self):
        rows = [l for l in DOC.splitlines() if l.startswith("|")]
        self.assertTrue(any("Never Haiku" in l and "permissions" in l and "merge or release authority" in l for l in rows))
        self.assertTrue(any("effective model id is not verified" in l and l.rstrip().endswith("Sonnet |") for l in rows))
        self.assertIn("no model call is made to choose a model", DOC)

    def test_cost_is_unknown_and_no_saving_is_claimed(self):
        self.assertIn("Dollar cost is UNKNOWN", DOC)
        self.assertIsNone(re.search(r"\d+\s*%\s*(cheaper|saving|savings|less cost|reduction in cost)", DOC, re.I))
        self.assertNotIn("saves", DOC.lower())
        self.assertIn("at least 30 paired tasks", DOC)

    def test_the_documented_state_matches_the_repository(self):
        for agent in (ROOT / ".claude/agents").glob("*.md"):
            self.assertNotRegex(agent.read_text(), r"(?im)^model:\s*.*haiku", agent.name)
        settings = json.loads((ROOT / ".claude/settings.json").read_text())
        self.assertNotIn("CLAUDE_CODE_SUBAGENT_MODEL", settings.get("env", {}))
        self.assertEqual(settings.get("model"), "sonnet")

    def test_context_rule_names_the_flags_and_the_exception(self):
        self.assertIn("--strict-mcp-config --disable-slash-commands", DOC)
        self.assertIn("Do not apply them to tests whose purpose is to observe instruction or skill loading", DOC)

    def test_owners_are_referenced_not_overridden_and_links_resolve(self):
        for owner in ("SPE-93", "SPE-108", "SPE-121"):
            self.assertIn(owner, DOC)
        for target in re.findall(r"\]\((?!https?://|#)([^)#]+)", DOC):
            self.assertTrue((ROOT / "docs" / target).resolve().exists(), target)

    def test_the_pilot_figures_cited_have_an_in_repo_source(self):
        ev = (ROOT / "docs/ROUTING-EVAL.md").read_text()
        self.assertIn("## Pilot result (2026-10-24)", ev)
        self.assertIn("INCONCLUSIVE_KEEP_OPT_IN", ev)
        self.assertIn("0 agent calls and used 0 Haiku tokens in all six tasks", ev)
        self.assertIn("2.1.287 to 2.1.292", DOC)
        self.assertIn("(SPE-122, SPE-126)", DOC.splitlines()[0])

    def test_worker_boundaries_are_stated(self):
        for phrase in ("never spawns agents", "never merges, publishes, closes tickets", "There is no retry loop"):
            self.assertIn(phrase, DOC)


if __name__ == "__main__":
    unittest.main()
