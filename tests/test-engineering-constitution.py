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
            "Multi-agent resource coordination", "Security profiles", "Open decisions (founder), with recommended defaults",
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


class NotASecondSourceOfTruth(unittest.TestCase):
    def test_does_not_restate_what_the_owners_hold(self):
        policy = (ROOT / "docs/ENGINEERING-POLICY.md").read_text()
        self.assertIn("| Autonomous | Owner approval required |", policy)
        self.assertNotIn("| Autonomous | Owner approval required |", DOC)
        for needle in ("--match-head-commit <", "candidate-rollout.py plan --live", "branch protection"):
            self.assertNotIn(needle, DOC)

    def test_the_open_decision_table_is_complete_and_states_each_item(self):
        rows = [l for l in DOC.split("## 10.")[1].split("## 11.")[0].splitlines() if re.match(r"\| \d+ \|", l)]
        self.assertEqual(len(rows), 15)
        for row in rows:
            self.assertRegex(row, r"\| (OPEN|RESOLVED[^|]*) \| [^|]+ \|$")   # state, then the interim behavior
        self.assertEqual(sum("RESOLVED" in r for r in rows), 3)

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
