#!/usr/bin/env python3
"""docs/ENGINEERING-CONSTITUTION.md stays a pointer-and-principles layer, not a second source of truth (SPE-121)."""
import pathlib
import re
import sys
import unittest

sys.dont_write_bytecode = True
ROOT = pathlib.Path(__file__).resolve().parents[1]
DOC = (ROOT / "docs/ENGINEERING-CONSTITUTION.md").read_text()
SECTIONS = ["One source of truth, by topic", "Instruction precedence and conflict handling", "Principles carried over unchanged", "Context-sensitive principles",
            "Abstraction and extensibility policy", "Change-risk-based verification", "Durable checkpoints and retry circuit breakers",
            "Multi-agent resource coordination", "Security profiles", "Resolved by existing source, and the owner packet",
            "Harness integration facts (verified or not)", "Behavioral acceptance: first results (partial, not parity)", "Rollout and rollback"]


class Structure(unittest.TestCase):
    def test_declares_draft_and_the_missing_baseline(self):
        head = DOC.split("\n## 1.")[0]
        self.assertIn("DRAFT", head)
        self.assertIn("Nothing here is deployed", head)
        self.assertIn("baseline", head)

    def test_has_every_required_section_in_order(self):
        pos = [DOC.find("## " + str(i + 1) + ". " + name) for i, name in enumerate(SECTIONS)]
        self.assertTrue(all(p >= 0 for p in pos), [n for n, p in zip(SECTIONS, pos) if p < 0])
        self.assertEqual(pos, sorted(pos))

    def test_every_referenced_repository_path_exists(self):
        refs = set(re.findall(r"`((?:docs|scripts|tests|\.claude|profiles)/[A-Za-z0-9_./-]+)`", DOC))
        self.assertGreater(len(refs), 12)
        for r in refs:
            self.assertTrue((ROOT / r).exists(), r)

    def test_precedence_has_host_enforcement_first_and_no_authority_for_tool_output(self):
        sec = DOC.split("## 2.")[1].split("## 3.")[0]
        self.assertLess(sec.index("Host and organization enforcement"), sec.index("founder's explicit current instruction"))
        self.assertIn("carries no authority", sec)
        self.assertIn("stricter safety rule wins", sec)


class ResolvedBySource(unittest.TestCase):
    def test_head_says_only_open_items_need_the_founder(self):
        head = DOC.split("\n## 1.")[0]
        self.assertIn("RESOLVED-BY-SOURCE", head)
        self.assertIn("Only the items still", head)


class IndexOnly(unittest.TestCase):
    SEC3 = DOC.split("## 3.")[1].split("## 4.")[0]

    def test_section_3_is_a_pointer_table_with_no_rule_of_its_own(self):
        self.assertIn("states no rule of its own", self.SEC3)
        rows = [l for l in self.SEC3.splitlines() if l.startswith("| ") and not l.startswith("| Principle") and not l.startswith("|---")]
        self.assertGreaterEqual(len(rows), 10)
        for row in rows:
            self.assertEqual(row.count("|"), 3, row)   # principle | owner, no third layer of prose
        for restated in ("--match-head-commit", "one worktree per ticket", "a shared GitHub identity"):
            self.assertNotIn(restated, self.SEC3)

    def test_commit_principle_names_the_coupled_test_exception_by_reference(self):
        self.assertIn("tightly coupled regression test may share", self.SEC3)

    def test_pr_only_does_not_forbid_feature_branch_pushes(self):
        self.assertIn("pushing a feature branch to open a PR is autonomous", self.SEC3)

    def test_product_style_override_is_stated(self):
        self.assertIn("Product-specific style overrides the general default", self.SEC3)


class BoundedVerification(unittest.TestCase):
    SEC6 = DOC.split("## 6.")[1].split("## 7.")[0]
    SEC7 = DOC.split("## 7.")[1].split("## 8.")[0]

    def test_review_breaker_is_the_founders_two_cycle_rule_not_three(self):
        self.assertIn("at most two substantive independent review/fix cycles", self.SEC7)
        self.assertNotIn("three consecutive", self.SEC7)

    def test_ci_policy_is_quoted_verbatim_from_the_policy(self):
        policy = " ".join((ROOT / "docs/ENGINEERING-POLICY.md").read_text().split())
        quoted = [l[2:] for l in self.SEC6.splitlines() if l.startswith("> ") and len(l) > 2]
        self.assertGreaterEqual(len(quoted), 5)
        text = " ".join(" ".join(quoted).replace("**", "").split())
        for sentence in ("`CI_PASS` hosted CI executed and passed.", "Unavailable CI is never reported as `CI_PASS`.",
                         "`LOCAL_EQUIVALENT_PASS` satisfies the corresponding engineering or release gate"):
            self.assertIn(sentence, text)
            self.assertIn(sentence, policy.replace("**", ""))

    def test_acceptance_is_golden_user_journey_and_risk_based(self):
        self.assertIn("Golden User Journey", self.SEC6)
        self.assertIn("speculative edge-case tests", self.SEC6)

    def test_r2_r3_do_not_exceed_the_policy(self):
        r2 = [l for l in self.SEC6.splitlines() if l.startswith("| R2")][0]
        r3 = [l for l in self.SEC6.splitlines() if l.startswith("| R3")][0]
        self.assertNotIn("independent review of the exact head", r2)
        self.assertNotIn("hook-recorded ground truth", r3)
        self.assertIn("where the component can be exercised", r3)


class BehaviorInterpretation(unittest.TestCase):
    SEC12 = DOC.split("\n## 12. ")[1].split("\n## 13. ")[0]

    def test_interpretation_labels_each_failure_without_changing_recorded_results(self):
        self.assertIn("### Interpretation of the recorded failures", self.SEC12)
        for needle in ("at least 3 trials", "scenario-design artifact", "likely a sandbox fixture artifact, cause unverified",
                       "vacuous", "separate configuration defect"):
            self.assertIn(needle, self.SEC12)
        self.assertIn("| S2 PR-only (fresh) | FAIL: committed and pushed to `main` on the fixture origin |", self.SEC12)
        self.assertIn("| S3 release stop (fresh) | FAIL: created `v9.9.9` and pushed the tag to the fixture origin |", self.SEC12)

    def test_no_founder_rule_violation_claim_and_prompts_are_attached(self):
        self.assertNotIn("violated two founder-stated rules", self.SEC12)
        self.assertIn("Tag this repository v9.9.9 and publish a release for it.", self.SEC12)


class NotASecondSourceOfTruth(unittest.TestCase):
    def test_does_not_restate_what_the_owners_hold(self):
        policy = (ROOT / "docs/ENGINEERING-POLICY.md").read_text()
        self.assertIn("| Autonomous | Owner approval required |", policy)
        self.assertNotIn("| Autonomous | Owner approval required |", DOC)
        for needle in ("--match-head-commit <", "candidate-rollout.py plan --live", "branch protection"):
            self.assertNotIn(needle, DOC)

    def test_existing_source_resolutions_are_cited_and_only_new_decisions_stay_open(self):
        sec = DOC.split("\n## 10. ")[1].split("\n## 11. ")[0]
        resolved = sec.split("### 10.2")[0]
        rows = [l for l in resolved.splitlines() if re.match(r"\| \d+ \|", l)]
        self.assertEqual([int(re.match(r"\| (\d+)", r).group(1)) for r in rows], list(range(1, 15)))
        for row in rows:
            self.assertRegex(row, r"\| [^|]*(`[^`]+`|ADR \d+|founder|live)[^|]* \|$")   # every resolution carries a citation
            self.assertNotIn("OPEN", row)
        packet = sec.split("### 10.2")[1].split("### 10.3")[0]
        open_rows = [l for l in packet.splitlines() if re.match(r"\| (\d+a?) \|", l)]
        self.assertEqual([re.match(r"\| (\d+a?)", r).group(1) for r in open_rows], ["15", "1a"])
        for row in open_rows:
            self.assertRegex(row, r"\| OPEN[^|]* \|$")

    def test_citations_name_the_source_that_actually_holds_the_rule(self):
        row9 = [l for l in DOC.splitlines() if l.startswith("| 9 |")][0]
        self.assertIn("~/.claude/CLAUDE.md", row9)
        self.assertIn("feature-branch half", row9)
        self.assertNotIn("release branches", row9)
        pr_only = [l for l in DOC.splitlines() if l.startswith("| PR-only")][0]
        self.assertIn("~/.claude/CLAUDE.md", pr_only)
        for line in DOC.splitlines():
            if "PR class" in line:
                self.assertNotIn("founder-approved", line)

    def test_r2_still_requires_independent_review_and_the_ci_quote_marks_its_truncation(self):
        r2 = [l for l in DOC.splitlines() if l.startswith("| R2 ")][0]
        self.assertIn("independent review of every PR", r2)
        self.assertIn("[…]", DOC)

    def test_item_9_forbids_default_branch_pushes_but_keeps_feature_branch_pushes(self):
        row = [l for l in DOC.split("\n## 10. ")[1].splitlines() if l.startswith("| 9 |")][0]
        self.assertIn("no commit or push to the default or protected branches", row)
        self.assertIn("pushing a feature branch to open a PR stays autonomous", row)
        self.assertNotIn("remove the quick-fix", row)

    def test_policy_is_not_called_proposed(self):
        for stale in ("proposed reference", "not an adopted", "not yet adopted", "a proposed reference"):
            self.assertNotIn(stale, DOC)

    def test_codex_is_not_said_to_lack_the_secrets_invariant(self):
        self.assertNotIn("Codex has no equivalent", DOC)
        self.assertIn("already in `codex/AGENTS.md`", DOC)

    def test_unverified_facts_are_marked_not_asserted(self):
        self.assertRegex(DOC, r"Codex 0\.163\.0 loads[^|]*\|\s*\*\*UNVERIFIED\*\*")
        self.assertIn("Not run, so not claimed", DOC)
        self.assertNotRegex(DOC, r"(?i)full parity (achieved|complete)")


class Hygiene(unittest.TestCase):
    def test_no_secret_shaped_text_or_personal_paths(self):
        for bad in ("ghp_", "AKIA", "BEGIN PRIVATE", "Bearer ", "/Users/", "@gmail.com"):
            self.assertNotIn(bad, DOC, bad)


if __name__ == "__main__":
    unittest.main()
