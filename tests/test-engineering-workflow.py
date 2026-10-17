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
            "**Create a merge commit**, never squash or rebase-merge",
            "Fix a\ngenuine failed check rather than reclassifying or bypassing it",
        ]
        for phrase in required:
            with self.subTest(boundary=phrase):
                self.assertIn(phrase, section)
        flat = " ".join(section.split())
        for phrase in ['owner-admin merge', 'effective admin-bypass rights', 'freshly verified', 'A separate human GitHub Approve is not required', 'zero GitHub reviews is not by itself a blocker', 'independent risk-based review', 'Every genuine required check and security gate must', 'separately qualified CI_UNAVAILABLE_EXTERNAL', 'no unresolved blocking findings, `REQUEST_CHANGES`, or conflicts', '--match-head-commit <full sha>', 'HARNESS_PERMISSION_BLOCK', 'never route around it']:
            with self.subTest(owner_admin=phrase):
                self.assertIn(phrase, flat)
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
                         {"external-qualified", "owner-qualified", "both-qualified",
                          "owner-alternative", "owner-external-ci-qualified"})
        for refused in ["owner-non-owner", "owner-no-admin-permission", "owner-red-ci",
                        "owner-requested-changes", "owner-host-refusal", "owner-new-head"]:
            self.assertFalse(indexed[refused]["admin_merge_allowed"], refused)
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

    def test_campaign_autonomy_contract_defines_completion_blockers_and_resumption(self) -> None:
        text = WORKFLOW.read_text()
        section = text.split("## Campaign autonomy", 1)[1].split("\n## ", 1)[0]
        flat = " ".join(section.split())
        required = [
            "`TASK_DONE` is not `CAMPAIGN_DONE`",
            "`PR_MERGED` is not `QA_SIGNED_OFF`",
            "`QA_SIGNED_OFF` is not `RELEASE_PUBLISHED`",
            "One blocked task never freezes the campaign",
            "`WAITING_RESOURCE`", "`WAITING_DEPENDENCY`", "`WAITING_OPERATOR`",
            "snapshot older than its freshness window (120 seconds) is stale",
            "A cached checkpoint or historical ticket status is not proof",
            "revalidate the exact merged SHA",
            "no second heavy build while the lane belongs to another owner",
            "release task-owned reservations",
            "Preserve active or resumable peer resources",
            "A passive checkpoint is not automatic resumption",
            "never claim a closed session can wake itself",
            "Never poll an unchanged state with model calls",
            "`READY_FOR_RELEASE` requires every required",
            "`RELEASE_BLOCKED_BY_OPERATOR`",
            "Never tag, publish, deploy to production or spend unapproved funds on the basis of campaign state",
            "never narrows, gates defined elsewhere",
            "non-model watcher that wakes the agent on change",
        ]
        for phrase in required:
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, flat)

    def test_campaign_verdict_scenarios(self) -> None:
        cases = json.loads((ROOT / "tests/fixtures/campaign-verdicts.json").read_text())
        ids = [case["id"] for case in cases]
        self.assertEqual(len(ids), len(set(ids)))
        gates = ("engineering", "integration", "qa", "security", "dogfood", "cleanup")

        def verdict(case: dict) -> str:
            open_gates = {gate for gate in gates if not case["gates"][gate]}
            if not open_gates:
                return "READY_FOR_RELEASE"
            if open_gates <= set(case.get("founder_gates", [])):
                return "RELEASE_BLOCKED_BY_OPERATOR"
            return "IN_PROGRESS"

        for case in cases:
            with self.subTest(case=case["id"]):
                self.assertEqual(verdict(case), case["verdict"])
        self.assertTrue(any(c["verdict"] == "READY_FOR_RELEASE" for c in cases))
        mixed = {"gates": dict.fromkeys(gates, True), "founder_gates": ["dogfood"]}
        mixed["gates"].update(qa=False, dogfood=False)
        self.assertEqual(verdict(mixed), "IN_PROGRESS", "a non-founder open gate must not hide behind a founder gate")
        self.assertTrue(any(c["verdict"] == "IN_PROGRESS" and c["gates"]["engineering"] for c in cases),
                        "a merged PR alone must not be READY_FOR_RELEASE")

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
        self.assertIn("alone defines the commit, branch, worktree and pull-request formats", " ".join(policy.split()))
        self.assertNotIn("<emoji> <scope>:", policy)
        self.assertNotIn("<ticket-number>", policy)

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

    def test_commit_granularity_rules(self) -> None:
        text = " ".join(WORKFLOW.read_text().split())
        for phrase in [
            "one logically small change per commit",
            "one new test per commit",
            "utilities and mocks land in earlier commits than the tests that use them",
        ]:
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, text)
        self.assertNotIn("one concern and its necessary tests", text)

    def test_all_repository_changes_go_through_pull_requests(self) -> None:
        text = " ".join(WORKFLOW.read_text().split())
        self.assertIn("All repository code changes go through pull requests", text)
        self.assertIn("never push directly to the default or protected branch (main/master); merge only via PR", text)
        claude = (ROOT / ".claude/CLAUDE.md").read_text()
        for stale in ["Quick-Fix Path", "Push directly", "quick-fix path"]:
            with self.subTest(stale=stale):
                self.assertNotIn(stale, claude)

    def test_ci_requirement_differs_for_public_and_private_repositories(self) -> None:
        text = " ".join(WORKFLOW.read_text().split())
        self.assertIn("Public repositories require green CI", text)
        self.assertIn("qualified `CI_UNAVAILABLE_EXTERNAL`", text)
        self.assertIn("Private repositories require the full local suite", text)

    def test_jira_lifecycle_rules_are_owned_by_the_workflow(self) -> None:
        text = " ".join(WORKFLOW.read_text().split())
        for phrase in [
            "move the ticket to In Progress when work really starts",
            "post a real start comment",
            "set Start Date to the actual date",
            "set an estimated Due Date",
            "never fabricate dates",
            "Epic or Story cascades In Progress only for items that truly started",
            "close a ticket only when every acceptance criterion passes",
        ]:
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, text)

    def test_ticket_pickup_uses_the_typeless_branch_and_worktree_format(self) -> None:
        text = " ".join((ROOT / ".claude/skills/ticket-pickup-check/SKILL.md").read_text().split())
        self.assertIn("<version-or-phase>/<ticket-id>/<short_summary>", text)
        self.assertNotIn("<type>", text)
        self.assertNotIn("[type]", text)
        config = (ROOT / ".claude/hooks/config.env").read_text()
        self.assertIn("<version-or-phase>/<ticket-id>/<short_summary>", config)
        self.assertNotIn("<type>", config)

    def test_skill_commit_examples_use_the_current_subject_format(self) -> None:
        example = re.compile(r"`(?:✨|🐛|♻️|✅|📝|🔧|🔌|🪝|🗑️|🚨|🔒|⬆️) [^`\n]*`")
        checked = 0
        for path in sorted((ROOT / ".claude/skills").rglob("*.md")):
            for match in example.finditer(path.read_text()):
                subject = match.group(0).strip("`")
                with self.subTest(path=str(path.relative_to(ROOT)), example=subject):
                    checked += 1
                    self.assertTrue(valid_commit_subject(subject))
        self.assertGreater(checked, 5)

    def test_subagent_maximum_is_seven_and_owned_only_by_the_workflow(self) -> None:
        text = " ".join(WORKFLOW.read_text().split())
        self.assertIn("at most seven concurrent subagents (dynamic 0-7)", text)
        self.assertIn("one owner per scope", text)
        for adapter in [ROOT / "codex/AGENTS.md", ROOT / ".claude/CLAUDE.md"]:
            adapter_text = " ".join(adapter.read_text().split())
            with self.subTest(adapter=str(adapter.relative_to(ROOT))):
                self.assertIn("engineering-workflow` Delegation section", adapter_text)
                for stale in ["Eight", "1..8", "0..7", "dynamic 0-7"]:
                    self.assertNotIn(stale, adapter_text)

    def test_frontend_qa_requires_real_playwright_evidence(self) -> None:
        text = " ".join((ROOT / ".claude/skills/acceptance-validation/SKILL.md").read_text().split())
        for phrase in [
            "read the project design specs",
            "run the app",
            "verify through Playwright screenshots",
            "personal Mac only",
            "sanitized",
            "never invent screenshots",
        ]:
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, text)
        self.assertIn("frontend QA evidence defined in `acceptance-validation`", " ".join(WORKFLOW.read_text().split()))

    def test_post_merge_close_requires_disk_check_and_cleanup_status(self) -> None:
        text = " ".join((ROOT / ".claude/skills/post-merge-close/SKILL.md").read_text().split())
        self.assertIn("Check free disk space", text)
        self.assertIn("report the cleanup status", text)
        self.assertIn("SPE-109", text)


if __name__ == "__main__":
    unittest.main()
