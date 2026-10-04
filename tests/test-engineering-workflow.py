#!/usr/bin/env python3
"""Semantic contract checks for the shared engineering workflow."""

import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
WORKFLOW = ROOT / ".claude/skills/engineering-workflow/SKILL.md"
TITLE = re.compile(
    r"^\[[^\[\]\n]+\] (?:✨|🐛|♻️|✅|📝|🔧|🔌|🪝|👨‍💻|🧭|⬆️|🗑️|🚨|🔒) "
    r"[a-z0-9][a-z0-9._/-]*: (?P<summary>[^\n]+)$"
)


def valid_title(title: str) -> bool:
    match = TITLE.fullmatch(title)
    return bool(match and len(match.group("summary")) < 60)


class EngineeringWorkflowContract(unittest.TestCase):
    def test_contract_owns_required_lifecycle_boundaries(self) -> None:
        text = WORKFLOW.read_text()
        required = [
            "<release-or-phase>/<ticket>/<type>/<2-4_word_snake_summary>",
            "<emoji> <scope>: <imperative summary>",
            "one concern and its necessary tests",
            "CI_UNAVAILABLE_EXTERNAL",
            "meaningful independent review",
            "[<ticket-number>] <emoji> <scope>: <imperative summary under 60 chars>",
            "Create a merge commit",
            "Reconcile the authoritative Jira ticket or established issue tracker",
            "Remove the owned worktree",
        ]
        for phrase in required:
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, text)
        self.assertIn("The 60-character limit applies to the imperative summary", text)
        self.assertIn("A product, test, security, or\nquality failure is never", text)

    def test_pull_request_title_examples_and_negative_controls(self) -> None:
        valid = [
            "[PROJ-123] ✨ restapi: Add user authentication",
            "[PROJ-123] ✨ restapi: Add new user authentication endpoint",
            "[42] 🐛 api: Fix retry leak",
            "[SPE-33] 🔧 workflow: Unify engineering lifecycle contract",
            "[SEC-7] 🔒 auth: Reject leaked credentials",
        ]
        invalid = [
            "SPE-33 🔧 workflow: Unify engineering lifecycle contract",
            "[SPE-33] workflow: Unify engineering lifecycle contract",
            "[SPE-33] 🔧 Workflow Unify engineering lifecycle contract",
            "[SPE-33] 🔧 workflow: " + "x" * 60,
        ]
        self.assertTrue(all(valid_title(title) for title in valid))
        self.assertFalse(any(valid_title(title) for title in invalid))

    def test_codex_materialized_contract_is_the_exact_shared_source(self) -> None:
        installer = (ROOT / "scripts/profile-install.sh").read_text()
        self.assertIn("shared_skills = ['evidence-first-briefing', 'engineering-workflow']", installer)
        self.assertNotIn("root / 'codex/skills/engineering-workflow", installer)

    def test_codex_policy_routes_lifecycle_to_exact_shared_source(self) -> None:
        policy = (ROOT / "codex/AGENTS.md").read_text()
        self.assertIn("Use $engineering-workflow for the engineering lifecycle", policy)
        self.assertIn(
            "[<ticket-number>] <emoji> <scope>: <imperative summary under 60 chars>",
            policy,
        )

    def test_ci_tracks_both_policy_adapters_and_contract_tests(self) -> None:
        workflow = (ROOT / ".github/workflows/capability-validation.yml").read_text()
        self.assertEqual(workflow.count("- '.claude/CLAUDE.md'"), 2)
        self.assertEqual(workflow.count("- '.claude/skills/**'"), 2)
        self.assertIn("python3 tests/test-engineering-workflow.py", workflow)


if __name__ == "__main__":
    unittest.main()
