#!/usr/bin/env python3
"""The ship/verify behavior map in docs/ENGINEERING-BASELINE.md stays true (SPE-147).

Contract-level: it proves each mapped behavior phrase is in its owner file and, for baseline rows, in both host entry points, and that
the named skills are shipped to both hosts. It does not prove a live model follows the behavior; that is scripts/adoption-probe.py.
"""
import json
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BASELINE = ROOT / "docs" / "ENGINEERING-BASELINE.md"
HOSTS = (ROOT / ".claude" / "CLAUDE.md", ROOT / "codex" / "AGENTS.md")


def flat(text):
    return re.sub(r"\s+", " ", text)


def rows():
    body = BASELINE.read_text().split("<!-- map -->")[1].split("<!-- /map -->")[0]
    out = []
    for line in body.splitlines():
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(cells) == 3 and cells[0] not in ("Behavior", "---") and not set(cells[0]) <= {"-"}:
            out.append(tuple(cells))
    return out


def owner_text(owner):
    kind, _, name = owner.partition(":")
    if owner == "baseline":
        return flat(BASELINE.read_text())
    if kind == "skill":
        return flat((ROOT / ".claude" / "skills" / name / "SKILL.md").read_text())
    if kind == "doc":
        return flat((ROOT / name).read_text())
    raise AssertionError("unknown owner " + owner)


class BehaviorMap(unittest.TestCase):
    def test_the_map_is_not_empty_and_covers_ship_and_verify(self):
        behaviors = [r[0] for r in rows()]
        self.assertGreaterEqual(len(behaviors), 15)
        self.assertTrue(any(b.startswith("ship:") for b in behaviors))
        self.assertTrue(any(b.startswith("verify:") for b in behaviors))

    def test_every_phrase_is_in_its_owner(self):
        for behavior, owner, phrase in rows():
            with self.subTest(behavior):
                self.assertIn(phrase, owner_text(owner))

    def test_baseline_phrases_reach_both_host_entry_points(self):
        for behavior, owner, phrase in rows():
            if owner != "baseline":
                continue
            for host in HOSTS:
                with self.subTest(behavior=behavior, host=host.name):
                    self.assertIn(phrase, flat(host.read_text()))

    def test_named_skills_ship_to_both_hosts(self):
        shipped = json.loads((ROOT / "codex" / "shared-skills.json").read_text())["skills"]
        for _, owner, _ in rows():
            if owner.startswith("skill:"):
                with self.subTest(owner):
                    self.assertTrue((ROOT / ".claude" / "skills" / owner[6:] / "SKILL.md").is_file())
                    self.assertIn(owner[6:], shipped)

    def test_a_missing_phrase_is_detected(self):
        self.assertNotIn("a phrase nobody wrote", owner_text("baseline"))


if __name__ == "__main__":
    unittest.main()
