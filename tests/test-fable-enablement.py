#!/usr/bin/env python3
"""Fable 5.2 enablement stays prepared-not-applied: template exists, nothing loadable names a Fable model (SPE-172)."""
import re
import sys
import unittest
from pathlib import Path

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[1]
TEMPLATE = ROOT / "docs/fable/fable-reasoner.agent.md.template"
RUNBOOK = ROOT / "docs/fable/enablement-runbook.md"
FABLE_MODEL = re.compile(r"(?im)^\s*model:\s*\S*fable")


def loadable_agent_files():
    """Files under any directory named `agents` in this repo. Not ~/.claude/agents or installed plugins: those are outside the repo."""
    return [p for d in ROOT.rglob("agents") if d.is_dir() and ".git" not in d.parts
            for p in d.rglob("*") if p.is_file()]


class FableEnablement(unittest.TestCase):
    def test_template_exists_and_is_not_loadable(self):
        text = TEMPLATE.read_text()
        self.assertRegex(text, FABLE_MODEL)
        self.assertNotEqual(TEMPLATE.suffix, ".md")
        self.assertNotIn("agents", TEMPLATE.relative_to(ROOT).parts)
        self.assertIn("tools: Read, Grep, Glob", text)
        self.assertIn("maxTurns: 12", text)

    def test_no_loadable_agent_file_references_a_fable_model(self):
        for p in loadable_agent_files():
            self.assertNotRegex(p.read_text(errors="ignore"), FABLE_MODEL, str(p))

    def test_runbook_covers_required_topics(self):
        text = RUNBOOK.read_text().lower()
        for topic in ("retention", "storage location", "expir", "enforce", "rollback", "fallback",
                      "unrecognized_model", "--max-budget-usd", "cleanupperioddays"):
            self.assertIn(topic, text, topic)
        self.assertIn("not applied", text)

    def test_unverifiable_claims_carry_a_label(self):
        text = RUNBOOK.read_text()
        for claim in ("Claude Code 2.1.306 logs", "Covered Model", "`cleanupPeriodDays` is unset", "claude -p --max-budget-usd <cap>",
                      "`maxTurns: 12` in the agent file"):
            at = text.index(claim)
            self.assertIn("[UNVERIFIED", text[max(0, at - 220):at + 260], claim)


if __name__ == "__main__":
    unittest.main()
