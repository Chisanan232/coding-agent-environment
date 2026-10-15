#!/usr/bin/env python3
"""Contract-level checks for the decision-delegation policy (SPE-104).

These prove the policy text, the scenario-to-level rule order it documents, and
that the installer delivers the managed blocks and skill byte-identically to both
hosts. They do not prove a live model obeys the policy; that needs a fresh-process
host probe, reported separately.
"""
import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SKILL = ROOT / ".claude/skills/decision-delegation/SKILL.md"
FIX = json.loads((Path(__file__).parent / "fixtures/decision-delegation.json").read_text())
CLAUDE_MD = ROOT / ".claude/CLAUDE.md"
AGENTS_MD = ROOT / "codex/AGENTS.md"


def base_level(flags: set) -> int:
    if ("verified_authorization_same_scope" in flags and "scope_changed" not in flags
            and "agent_authored_record_only" not in flags):
        return 3
    if "changes_protected" in flags or "changes_level1_invariant" in flags:
        return 2
    if "substantive_design" in flags:
        return 1
    return 0


def classify(flags: set) -> list:
    """Reference order from the skill; Level 4 is an overlay reported with the base level."""
    base = base_level(flags)
    return [4, base] if "host_refusal" in flags else [base]


def flat(text: str) -> str:
    return re.sub(r"\s+", " ", text)


def managed_block(path: Path, name: str) -> str:
    text = path.read_text()
    start = f"<!-- coding-agent-environment:{name}:start -->"
    end = f"<!-- coding-agent-environment:{name}:end -->"
    return text[text.index(start): text.index(end) + len(end)]


class PolicyText(unittest.TestCase):
    text = flat(SKILL.read_text())

    def test_fixture_matches_reference_order(self) -> None:
        """Checks fixture consistency with the reference order, not model behavior."""
        self.assertIn("Level 4 (is a host control refusing the action?), Level 3", self.text)
        for s in FIX["scenarios"]:
            with self.subTest(s["id"]):
                self.assertEqual(classify(set(s["flags"])), s["expected"])

    def test_routine_decisions_stay_autonomous(self) -> None:
        for phrase in ["Never ask the founder about", "algorithm and implementation details",
                       "CI/build environment debugging", "safe dependency updates",
                       "Jira bug/improvement tickets", "safe task-owned post-merge cleanup",
                       "Never ask the founder to choose between technically equivalent internal implementations"]:
            with self.subTest(phrase):
                self.assertIn(phrase, self.text)

    def test_consequential_decisions_escalate_with_one_packet(self) -> None:
        for prop in FIX["protected_properties"]:
            with self.subTest(prop):
                self.assertIn(prop, self.text)
        for gate in FIX["gated_classes"]:
            with self.subTest(gate):
                self.assertIn(gate, self.text.split("Test in order")[0])
        for phrase in ["Send ONE packet", "recommended option", "exact minimum founder action",
                       "Never write repetitive multi-page packets"]:
            self.assertIn(phrase, self.text)

    def test_existing_decisions_are_not_rerequested_or_fabricated(self) -> None:
        for phrase in ["Do not re-ask an approved decision within the same verified scope",
                       "Cite its durable Jira/ADR record",
                       "Reconfirm only when actual scope, risk, authority, resources or material conditions changed",
                       "never inferred or fabricated from any agent-produced record",
                       "The author field is not proof", "names the scope"]:
            self.assertIn(phrase, self.text)

    def test_host_denials_are_not_bypassed_or_mislabeled(self) -> None:
        for phrase in ["Do not bypass ASK, DENY, managed settings or an explicit classifier refusal",
                       "Branch protection yields only to the two `engineering-workflow` administrator-merge exceptions",
                       "`bypassPermissions`", "rewrite commands, add wrappers or use alternative tools",
                       "never a missing founder product decision", "Approving a host permission prompt is not founder product consent",
                       "evasion",
                       "No human approval is ever claimed that a human did not give",
                       "agent review is not a human PR approval"]:
            self.assertIn(phrase, self.text)

    def test_release_dag_continues_in_order(self) -> None:
        steps = ["Verify the result.", "Update Jira with actual evidence.",
                 "Verify current PR and base-branch status.", "Resolve every material review finding; dismissing one needs a recorded technical reason, and a human REQUEST_CHANGES is never dismissed.",
                 "As main coordinator only, perform the merge `engineering-workflow` permits.", "Identify newly unblocked tasks.", "Continue automatically."]
        raw = SKILL.read_text()
        flat_raw = flat(raw)
        positions = [flat_raw.index(f"{i}. {s}") for i, s in enumerate(steps, 1)]
        self.assertEqual(positions, sorted(positions))
        for phrase in ["A finished subagent task or PR is not campaign completion",
                       "Never claim `RELEASE_READY`", "As main coordinator only, perform the merge",
                       "needing its own Level 3 record or Level 2 packet", "implies no new publication or deployment authorization"]:
            self.assertIn(phrase, self.text)

    def test_active_state_is_preserved(self) -> None:
        for phrase in ["Preserve active or ambiguously owned worktrees, branches, background processes, caches and test data",
                       "Never delete another Claude Code/Codex session's state",
                       "age or apparent inactivity alone is not proof of abandonment",
                       "never suppress a failing security test"]:
            self.assertIn(phrase, self.text)

    def test_jira_reads_cannot_mutate_and_incident_guard_exists(self) -> None:
        for phrase in ["Never call an update/edit/transition/add operation merely to read data",
                       "FORNX-439, comment 27362", "literal `placeholder`",
                       "Never send placeholder, empty, template or dummy bodies",
                       "Independently read the result back",
                       "founder decision comments are immutable to agents"]:
            self.assertIn(phrase, self.text)

    def test_repo_claude_settings_not_bypass_mode_and_skill_defers(self) -> None:
        """Scope: repo Claude settings only; corporate profiles are covered by the resolver tests."""
        self.assertIn("Corporate/managed profiles keep their restrictions; this skill never loosens them", self.text)
        settings = json.loads((ROOT / ".claude/settings.json").read_text())
        self.assertNotEqual(settings.get("permissions", {}).get("defaultMode"), "bypassPermissions")


class ManagedBlocks(unittest.TestCase):
    def test_both_hosts_carry_identical_delegation_text(self) -> None:
        paragraphs = []
        for path, name in [(CLAUDE_MD, "engineering-workflow"), (AGENTS_MD, "signal-first")]:
            block = flat(managed_block(path, name))
            paragraphs.append(block[block.index("Decision delegation: act as senior"):block.rindex("<!--")].strip())
        self.assertEqual(paragraphs[0], paragraphs[1])
        for gate in FIX["gated_classes"]:
            self.assertIn(gate, paragraphs[0])

    def test_both_hosts_carry_the_same_invariants(self) -> None:
        blocks = [managed_block(CLAUDE_MD, "engineering-workflow"), managed_block(AGENTS_MD, "signal-first")]
        for block in blocks:
            text = flat(block)
            for phrase in ["`decision-delegation`", "Level 2 founder decision at least for",
                           "never infer founder consent from any agent-produced record",
                           "never bypassed or relabeled a product decision", "no fake human approval",
                           "preserve active or ambiguously owned worktrees and sessions", "FORNX-439 comment 27362"]:
                with self.subTest(phrase=phrase):
                    self.assertIn(phrase, text)

    def test_skill_is_in_the_shared_manifest(self) -> None:
        self.assertIn("decision-delegation", json.loads((ROOT / "codex/shared-skills.json").read_text())["skills"])


class Installation(unittest.TestCase):
    def test_installer_delivers_blocks_and_skill_identically_and_preserves_outside_bytes(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            live = Path(temp) / "home"
            repo = Path(temp) / "repo"
            for rel in [".claude", "codex", "scripts"]:
                shutil.copytree(ROOT / rel, repo / rel)
            manifest = json.loads((repo / "codex/subtraction-skills.json").read_text())
            for name, spec in manifest["skills"].items():
                folder = live / ".agents/skills" / name
                folder.mkdir(parents=True)
                (folder / "SKILL.md").write_text("Disposable external skill fixture")
                spec["files"] = {"SKILL.md": hashlib.sha256((folder / "SKILL.md").read_bytes()).hexdigest()}
            (repo / "codex/subtraction-skills.json").write_text(json.dumps(manifest))
            env = dict(os.environ, CODING_AGENT_SYNC_HOME=str(live))
            env.pop("CODEX_HOME", None)
            installer = ["bash", str(repo / "scripts/profile-install.sh")]
            outside = b"Local unrelated instructions\r\n"
            targets = {"--global": (".codex", "codex/AGENTS.md", "signal-first"),
                       "--claude-skills": (".claude", ".claude/CLAUDE.md", "engineering-workflow")}
            for flag, (host, policy, marker) in targets.items():
                with self.subTest(host=host):
                    instr = live / host / ("AGENTS.md" if host == ".codex" else "CLAUDE.md")
                    instr.parent.mkdir(parents=True, exist_ok=True)
                    instr.write_bytes(outside)
                    run = lambda *a: subprocess.run(installer + [flag, *a], env=env, capture_output=True, text=True)
                    self.assertEqual(run("--check").returncode, 1)
                    done = run()
                    self.assertEqual(done.returncode, 0, done.stderr)
                    self.assertEqual(run("--check").returncode, 0)
                    self.assertTrue(instr.read_bytes().startswith(outside))
                    block = managed_block(ROOT / policy, marker).encode()
                    self.assertIn(block, instr.read_bytes())
                    self.assertEqual((live / host / "skills/decision-delegation/SKILL.md").read_bytes(), SKILL.read_bytes())


if __name__ == "__main__":
    unittest.main()
