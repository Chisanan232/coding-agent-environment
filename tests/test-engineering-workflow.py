#!/usr/bin/env python3
"""Semantic contract checks for the shared engineering workflow."""

import re
import json
import os
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
WORKFLOW = ROOT / ".claude/skills/engineering-workflow/SKILL.md"
TITLE = re.compile(
    r"^\[(?:[A-Z][A-Z0-9]*-[0-9]+|[0-9]+)\] "
    r"(?P<emoji>✨|🐛|♻️|✅|📝|🔧|🔌|🪝|👨‍💻|🧭|⬆️|🗑️|🚨|🔒|🎨|🧪|👷|🛡️) "
    r"[a-z0-9][a-z0-9._/-]*: (?P<summary>[^\n]+)$"
)


def structurally_valid_title(title: str) -> bool:
    """Check machine-verifiable shape; imperative meaning requires human review."""
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
            "Resolve and fetch the actual tracking remote",
            "configured pre-commit hooks pass",
            "Never force-push a protected, main, or release branch",
            "Force-pushing a feature branch requires explicit engineer permission",
            "Create the worktree as a sibling of the main checkout",
            "Only the main coordinator may trigger the merge",
            "under 500 changed lines when practical",
            "Preserve any required repository pull-request template",
            "The 72-character limit applies to the imperative summary",
        ]
        for phrase in required:
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, text)
        self.assertIn("The 60-character limit applies to the imperative summary", text)
        self.assertIn("A product, test, security, or\nquality failure is never", text)
        for change_type in [
            "`feat`", "`fix`", "`refactor`", "`test`", "`docs`", "`config`",
            "`deps`", "`remove`", "`lint`", "`security`",
        ]:
            with self.subTest(change_type=change_type):
                self.assertIn(change_type, text)

    def test_admin_exceptions_preserve_failure_and_eligibility_boundaries(self) -> None:
        text = WORKFLOW.read_text()
        section = text.split("### Narrow administrator merge exceptions", 1)[1]
        required = [
            "Use normal eligible independent review whenever it exists",
            "independent evidence must prove an external",
            "prevented CI from genuinely running or completing",
            "no genuine product/test/security/quality failure",
            "full local\n   equivalent must be green",
            "mergeable and conflict-free",
            "external cause and local-equivalent evidence before admin merge",
            "slow or healthy running job",
            "author assertion alone\n   does not qualify",
            "real governance/ownership check",
            "sole genuinely eligible required reviewer",
            "no alternative eligible independent reviewer/team",
            "Freshly verify the owner/admin identity",
            "independent\n   adversarial agent review must both be clean",
            "Every genuine required check\n   must be green",
            "separately qualified CI_UNAVAILABLE_EXTERNAL",
            "no unresolved blocking findings or conflicts",
            "deadlock and justification durably before admin merge",
            "does\n   not replace an available eligible independent repository reviewer",
            "**Create a merge commit**, never squash or rebase-merge",
            "Fix a\ngenuine failed check rather than reclassifying or bypassing it",
        ]
        for phrase in required:
            with self.subTest(boundary=phrase):
                self.assertIn(phrase, section)
        self.assertIn("Exactly two narrow exceptions", text)
        self.assertIn("neither grants blanket administrator authority", text)
        self.assertNotIn("Never use administrator privileges to bypass them", text)
        for path in [ROOT / '.claude/CLAUDE.md', ROOT / 'codex/AGENTS.md']:
            self.assertIn('owner-only same-identity review deadlock', path.read_text())

    def test_admin_exception_behavior_scenarios(self) -> None:
        cases = json.loads((ROOT / "tests/fixtures/workflow-admin-exceptions.json").read_text())
        indexed = {case["id"]: case for case in cases}
        self.assertEqual(len(indexed), len(cases))
        self.assertEqual({case["id"] for case in cases if case["admin_merge_allowed"]},
                         {"external-qualified", "owner-qualified", "both-qualified"})
        for boundary in ["product", "test", "security", "quality", "conflict", "review"]:
            for kind in ["external", "owner"]:
                self.assertFalse(indexed[f"{kind}-{boundary}"]["admin_merge_allowed"])
        # Native behavioral validation supplies actual decisions; offline CI
        # validates policy anchors and preserves the independently authored cases.
        result_path = os.environ.get("WORKFLOW_ADMIN_PROBE_RESULTS")
        if result_path:
            results = json.loads(Path(result_path).read_text())
            actual = {case["id"]: case for case in results}
            self.assertEqual(len(results), len(actual))
            self.assertEqual(set(actual), set(indexed))
            for case_id, expected in indexed.items():
                with self.subTest(case=case_id):
                    self.assertIs(actual[case_id]["admin_merge_allowed"], expected["admin_merge_allowed"])
                    self.assertTrue(actual[case_id]["reason"].strip())

    def test_pull_request_title_examples_and_negative_controls(self) -> None:
        valid = [
            "[PROJ-123] ✨ restapi: Add user authentication",
            "[PROJ-123] ✨ restapi: Add new user authentication endpoint",
            "[42] 🐛 api: Fix retry leak",
            "[SPE-33] 🔧 workflow: Unify engineering lifecycle contract",
            "[SEC-7] 🔒 auth: Reject leaked credentials",
            "[OPS-2] 👷 ci: Stabilize release checks",
            "[SEC-8] 🛡️ privacy: Redact analytics metadata",
            "[HORO-1033] ✨ web: Preserve console state and reading continuity",
        ]
        invalid = [
            "SPE-33 🔧 workflow: Unify engineering lifecycle contract",
            "[SPE-33] workflow: Unify engineering lifecycle contract",
            "[SPE-33] config workflow: Unify engineering lifecycle contract",
            "[SPE-33] 🔧 Workflow Unify engineering lifecycle contract",
            "🛡️ Redact analytics route metadata and UTM payloads",
            "✨ (web): Preserve console state and reading continuity [HORO-1033]",
            "[HORO-1033] ✨ (web): Preserve console state and reading continuity",
            "[not a ticket] ✨ web: Preserve console state",
            "[SPE-33] 中文 workflow: Restore workflow parity",
            "[SPE-33] 🔧 workflow: " + "x" * 60,
        ]
        self.assertTrue(all(structurally_valid_title(title) for title in valid))
        self.assertFalse(any(structurally_valid_title(title) for title in invalid))

    def test_imperative_meaning_remains_a_review_judgment(self) -> None:
        nonimperative = "[SPE-33] 🔧 workflow: Was changed yesterday"
        self.assertTrue(structurally_valid_title(nonimperative))
        self.assertIn(
            "reviewers must assess\nwhether its summary actually uses imperative meaning",
            WORKFLOW.read_text(),
        )

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
