#!/usr/bin/env python3
"""Invariant manifest: every always-loaded safety/contract clause stays reachable.

Each clause names the file that owns it today and a whitespace-normalized phrase.
A clause that lives outside `.claude/CLAUDE.md` must be named by `reach` text that
appears in `.claude/CLAUDE.md`, so the always-loaded file still routes to it.
Moving a clause means updating its `file` (and `reach`) in the same commit.
"""
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CLAUDE = ".claude/CLAUDE.md"
WORKFLOW = ".claude/skills/engineering-workflow/SKILL.md"

# (id, file, phrase, reach) -- reach is required when file != CLAUDE.
MANIFEST = [
    # Safe implementation: validation sequence and safety items 16-20.
    ("validation-impacted-first", CLAUDE, "After each logical unit of change, run impacted tests first.", None),
    ("validation-full-suite", CLAUDE, "Before declaring work complete, run the full validation suite.", None),
    ("validation-not-red", CLAUDE, "Do not mark a task done while any test or lint check is red.", None),
    ("validation-unrunnable-check", CLAUDE, "If a check cannot be run locally, say so explicitly before committing.", None),
    ("no-overwrite-uncommitted", CLAUDE, "Never overwrite uncommitted changes without explicit user confirmation.", None),
    ("delete-in-scope", CLAUDE, "Delete only within authorized scope after checking dependents and risk.", None),
    ("no-force-push-item", CLAUDE, "Never force-push without explicit user confirmation.", None),
    ("no-verify-item", CLAUDE, "Never skip pre-commit hooks (`--no-verify`) without explicit user confirmation.", None),
    ("unknown-state", CLAUDE, "Do not delete or overwrite unknown state.", None),
    # Never-do list.
    ("never-force-push", CLAUDE, "- Force-push to any branch", None),
    ("never-reset-hard", CLAUDE, "- Run `git reset --hard`", None),
    ("never-clean-fd", CLAUDE, "- Run `git clean -fd`", None),
    ("never-drop-tables", CLAUDE, "- Drop or truncate database tables", None),
    ("never-curl-shell", CLAUDE, "- Pipe remote content directly to a shell (`curl | bash`)", None),
    ("never-publish", CLAUDE, "- Publish packages to registries", None),
    ("never-edit-ci-defs", CLAUDE, "- Modify CI/CD pipeline definitions without review", None),
    ("never-commit-credentials", CLAUDE, "- Commit `.env` or any file containing credentials", None),
    ("never-unauthorised-delete", CLAUDE, "Delete outside the authorized scope or remove protective controls without owner approval", None),
    # Delivery: PR-only, formats, worktree, Jira, QA, cleanup.
    ("pr-only-claude", CLAUDE, "Never push directly to the default or protected branch.", None),
    ("pr-only-workflow", WORKFLOW, "never push directly to the default or protected branch (main/master); merge only via PR", "engineering-workflow"),
    ("formats-owned-by-workflow", CLAUDE, "which owns the branch, commit and pull-request rules", None),
    ("commit-format", WORKFLOW, "<GitEmoji> (<scope>): <imperative summary>", "engineering-workflow"),
    ("branch-format", WORKFLOW, "<version-or-phase>/<ticket-id>/<short_summary>", "engineering-workflow"),
    ("pr-title-format", WORKFLOW, "[<TICKET>] <GitEmoji> (<scope>): <imperative summary under 60 chars>", "engineering-workflow"),
    ("worktree-derivation", WORKFLOW, "<repo>-<phase>-<ticket>-<summary>", "engineering-workflow"),
    ("small-commits", WORKFLOW, "one logically small change per commit", "engineering-workflow"),
    ("jira-lifecycle", WORKFLOW, "Jira lifecycle", "engineering-workflow"),
    ("jira-dates", WORKFLOW, "never fabricate dates", "engineering-workflow"),
    ("frontend-qa", ".claude/skills/acceptance-validation/SKILL.md", "never invent screenshots", "acceptance-validation"),
    ("cleanup-disk-check", ".claude/skills/post-merge-close/SKILL.md", "Check free disk space", "post-merge-close"),
    ("worktree-per-ticket-resolution", CLAUDE, "Claude workflow skills resolve the active worktree from", None),
    # Delegation.
    ("subagent-maximum", WORKFLOW, "at most seven concurrent subagents (dynamic 0-7)", "engineering-workflow"),
    ("one-owner-per-scope", WORKFLOW, "one owner per scope", "engineering-workflow"),
    ("decision-levels", CLAUDE, "Test Level 4 overlay, then 3, 2, 1, else 0.", None),
    ("host-refusal-not-bypassed", CLAUDE, "never bypassed or relabeled a product decision", None),
    ("ready-for-release", ".claude/skills/decision-delegation/SKILL.md", "READY_FOR_RELEASE", "decision-delegation"),
    ("no-deploy-via-merge", CLAUDE, "a merge that deploys or publishes needs its own authorization", None),
    # Merge / CI.
    ("merge-commit", CLAUDE, "Use **Create a merge commit**", None),
    ("ci-unavailable-external", CLAUDE, "qualified `CI_UNAVAILABLE_EXTERNAL`", None),
    ("owner-only-admin-merge", CLAUDE, "owner-only same-identity review deadlock", None),
    ("never-bypass-failing-gate", CLAUDE, "never bypass a genuine failing gate", None),
    ("ci-no-merge-on-red", CLAUDE, "Do not merge on red CI.", None),
    ("ci-no-suppression", CLAUDE, "Do not add suppression annotations to silence failures.", None),
    ("ci-no-delete-failing-tests", CLAUDE, "Do not delete failing tests.", None),
    ("ci-no-no-verify", CLAUDE, "Do not bypass pre-commit with `--no-verify`.", None),
    ("ci-reproduce-locally", CLAUDE, "Reproduce locally", None),
    ("ci-flaky-not-skipped", CLAUDE, "Do not mark them as skip.", None),
    ("push-gate-no-no-verify", CLAUDE, "`full-test-gate.sh` and `precommit-gate.sh`", None),
    # Release operations prohibitions.
    ("release-no-manual-tags", CLAUDE, "Do not manually create git tags", None),
    ("release-no-main-push", CLAUDE, "Do not push directly to `main` / `master` during a release window.", None),
    ("release-no-ci-edits", CLAUDE, "Do not modify CI/CD pipeline definitions during active release.", None),
    ("release-no-publish", CLAUDE, "Do not publish packages to registries", None),
    # Routing and layering.
    ("model-main", CLAUDE, "Main model: Sonnet, effort medium, permission mode Auto.", None),
    ("model-opus-architect", CLAUDE, "delegate first to the `opus-architect` subagent", None),
    ("mcp-prefer-capability", CLAUDE, "prefer it over manual", None),
    ("mcp-active-servers", CLAUDE, "`fetch` and `github` are active by default", None),
    ("layering-project-wins", CLAUDE, "Project values take precedence when both define the same concept.", None),
    ("layering-global-applies", CLAUDE, "These global behavioral rules apply unless the project file explicitly overrides them.", None),
]


def normalized(path: str) -> str:
    return " ".join((ROOT / path).read_text().split())


class ClaudeMdInvariants(unittest.TestCase):
    def test_manifest_has_unique_ids(self) -> None:
        ids = [clause[0] for clause in MANIFEST]
        self.assertEqual(len(ids), len(set(ids)))

    def test_every_clause_exists_where_the_manifest_says(self) -> None:
        for clause_id, path, phrase, _ in MANIFEST:
            with self.subTest(clause=clause_id):
                self.assertIn(" ".join(phrase.split()), normalized(path))

    def test_claude_md_routes_to_every_moved_clause(self) -> None:
        claude = normalized(CLAUDE)
        for clause_id, path, _, reach in MANIFEST:
            if path == CLAUDE:
                continue
            with self.subTest(clause=clause_id):
                self.assertTrue(reach, "clause outside CLAUDE.md needs a reach pointer")
                self.assertIn(reach, claude)

    def test_every_relative_link_in_claude_md_resolves(self) -> None:
        text = (ROOT / CLAUDE).read_text()
        links = re.findall(r"\]\((?!https?:|#)([^)#]+)", text)
        self.assertGreater(len(links), 5)
        for link in links:
            with self.subTest(link=link):
                self.assertTrue(((ROOT / ".claude") / link).is_file())


if __name__ == "__main__":
    unittest.main()
