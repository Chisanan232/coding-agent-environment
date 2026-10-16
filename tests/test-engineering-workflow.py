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
    r"\([a-z0-9][a-z0-9._/-]*\): (?P<summary>[^\n]+)$"
)

BRANCH = re.compile(
    r"^[A-Za-z0-9][A-Za-z0-9._-]*/(?:[A-Z][A-Z0-9]*-[0-9]+|[0-9]+)/[a-z0-9]+(?:_[a-z0-9]+){1,3}$"
)


def valid_branch(name: str) -> bool:
    """Phase/ticket/summary only; a type segment would add a fourth part."""
    return bool(BRANCH.fullmatch(name)) and len(name.rsplit("/", 1)[1]) <= 30


COMMIT = re.compile(
    r"^(?P<emoji>✨|🐛|♻️|✅|📝|🔧|🔌|🪝|👨‍💻|🧭|⬆️|🗑️|🚨|🔒|🎨|🧪|👷|🛡️) "
    r"\([a-z0-9][a-z0-9._/-]*\): (?P<summary>[^\n]+)$"
)


def valid_commit_subject(subject: str) -> bool:
    match = COMMIT.fullmatch(subject)
    return bool(match and len(match.group("summary")) < 72)


def structurally_valid_title(title: str) -> bool:
    """Check machine-verifiable shape; imperative meaning requires human review."""
    match = TITLE.fullmatch(title)
    return bool(match and len(match.group("summary")) < 60)


class EngineeringWorkflowContract(unittest.TestCase):
    def test_contract_owns_required_lifecycle_boundaries(self) -> None:
        text = WORKFLOW.read_text()
        required = [
            "<version-or-phase>/<ticket-id>/<short_summary>",
            "<GitEmoji> (<scope>): <imperative summary>",
            "one concern and its necessary tests",
            "CI_UNAVAILABLE_EXTERNAL",
            "meaningful independent review",
            "[<TICKET>] <GitEmoji> (<scope>): <imperative summary under 60 chars>",
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
        for gitmoji in ["✨", "🐛", "♻️", "✅", "📝", "🔧", "⬆️", "🗑️", "🚨", "🔒"]:
            with self.subTest(gitmoji=gitmoji):
                self.assertIn(f"| {gitmoji} |", text)

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
            "[PROJ-123] ✨ (restapi): Add user authentication",
            "[PROJ-123] ✨ (restapi): Add new user authentication endpoint",
            "[42] 🐛 (api): Fix retry leak",
            "[SPE-33] 🔧 (workflow): Unify engineering lifecycle contract",
            "[SEC-7] 🔒 (auth): Reject leaked credentials",
            "[OPS-2] 👷 (ci): Stabilize release checks",
            "[SEC-8] 🛡️ (privacy): Redact analytics metadata",
            "[HORO-1033] ✨ (web): Preserve console state and reading continuity",
        ]
        invalid = [
            "SPE-33 🔧 workflow: Unify engineering lifecycle contract",
            "[SPE-33] workflow: Unify engineering lifecycle contract",
            "[SPE-33] config (workflow): Unify engineering lifecycle contract",
            "[SPE-33] 🔧 Workflow Unify engineering lifecycle contract",
            "🛡️ Redact analytics route metadata and UTM payloads",
            "✨ (web): Preserve console state and reading continuity [HORO-1033]",
            "[HORO-1033] ✨ web: Preserve console state and reading continuity",
            "[SPE-33] 🔧 (workflow) Unify engineering lifecycle contract",
            "[not a ticket] ✨ (web): Preserve console state",
            "[SPE-33] 中文 (workflow): Restore workflow parity",
            "[SPE-33] 🔧 (workflow): " + "x" * 60,
        ]
        self.assertTrue(all(structurally_valid_title(title) for title in valid))
        self.assertFalse(any(structurally_valid_title(title) for title in invalid))

    def test_imperative_meaning_remains_a_review_judgment(self) -> None:
        nonimperative = "[SPE-33] 🔧 (workflow): Was changed yesterday"
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

    def test_branch_name_has_no_type_segment_and_derives_the_worktree(self) -> None:
        valid = ["full/SPE-108/restore_dev_contract", "v0.1.0/TEST-1/add_endpoint", "sprint3/42/extract_payment_service"]
        invalid = [
            "full/SPE-108/feat/restore_dev_contract",
            "v0.1.0/TEST-1/fix/auth_token_refresh",
            "full/SPE-108",
            "full/spe-108/restore_dev_contract",
            "full/SPE-108/single",
            "full/SPE-108/" + "_".join(["word"] * 7),
        ]
        self.assertTrue(all(valid_branch(name) for name in valid))
        self.assertFalse(any(valid_branch(name) for name in invalid))
        text = WORKFLOW.read_text()
        self.assertNotIn("<type>", text)
        self.assertIn("<repo>-<phase>-<ticket>-<summary>", text)

    def test_commit_subject_requires_parenthesized_scope(self) -> None:
        valid = [
            "✨ (model): Add property to TestModel",
            "🐛 (service-payment): Fix double charge on retry",
            "✅ (order): Add repository lookup test",
        ]
        invalid = [
            "✨ web: Add property to TestModel",
            "✨ model(order): Add property to TestModel",
            "✨ (web) Add property to TestModel",
            "(model): Add property to TestModel",
            "✨ (): Add property to TestModel",
            "✨ (model): " + "x" * 72,
        ]
        self.assertTrue(all(valid_commit_subject(subject) for subject in valid))
        self.assertFalse(any(valid_commit_subject(subject) for subject in invalid))
        self.assertIn("Parentheses around the scope are required", WORKFLOW.read_text())

    def test_pull_request_template_exists_with_evidence_sections(self) -> None:
        template = ROOT / ".github/PULL_REQUEST_TEMPLATE.md"
        self.assertTrue(template.is_file())
        text = template.read_text()
        for heading in ["## Outcome", "## Validation", "## Risks and unknowns", "## References"]:
            with self.subTest(heading=heading):
                self.assertIn(heading, text)


if __name__ == "__main__":
    unittest.main()
