#!/usr/bin/env python3
"""Regression tests for ADR-0012's permission profile.

Fixtures are synthetic and disposable (tempdir $HOME, in-memory JSON) —
nothing here touches a real ~/.claude. Where real Claude Code runtime
behavior would be the only true verification (does a live session actually
prompt), that is explicitly out of reach here and not claimed: see the
RTKHookSelfDecision class docstring and ADR-0012's Consequences section.
"""
import json
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SETTINGS_PATH = ROOT / ".claude/settings.json"
SYNC_CHECK = ROOT / "scripts/sync-check.sh"

MANDATORY_ASK = {
    "Bash(git push --force*)",
    "Bash(git push * --force*)",
    "Bash(git push -f *)",
    "Bash(git push * -f *)",
    "Bash(rtk git push --force*)",
    "Bash(rtk git push * --force*)",
    "Bash(rtk git push -f *)",
    "Bash(rtk git push * -f *)",
    "Bash(git reset --hard *)",
    "Bash(git clean -f*)",
    "Bash(kill -9 *)",
    "Bash(pkill *)",
    "Bash(killall *)",
    "Bash(gh repo delete *)",
    "Bash(rtk gh repo delete *)",
    "Bash(gh api * -X DELETE*)",
    "Bash(rtk gh api * -X DELETE*)",
    "Bash(gh api * --method DELETE*)",
    "Bash(rtk gh api * --method DELETE*)",
    "Bash(terraform apply:*)",
    "mcp__gcloud__run_gcloud_command",
    "mcp__polar-production__execute_tool",
}

MANDATORY_DENY = {
    "Bash(terraform destroy:*)",
    "Bash(rtk proxy *)",
    "Bash(gcloud secrets versions access *)",
    "Bash(rtk gcloud secrets versions access *)",
    "mcp__neon__get_connection_string",
}

# Patterns that would reproduce SPE-83: any allow rule RTK's self-decision
# could match for a plain `git push`, at git-subcommand granularity,
# regardless of trailing wildcard shape.
FORBIDDEN_PUSH_ALLOW = re.compile(r"^Bash\((?:rtk )?git push(?: \*|:\*)?\)$")


def load_settings():
    return json.loads(SETTINGS_PATH.read_text())


class SettingsSchemaValidity(unittest.TestCase):
    def test_settings_json_is_valid_and_has_permissions_block(self):
        d = load_settings()
        self.assertIn("permissions", d)
        for key in ("allow", "ask", "deny"):
            self.assertIn(key, d["permissions"])
            self.assertIsInstance(d["permissions"][key], list)
        self.assertEqual(d["permissions"]["defaultMode"], "auto")

    def test_auto_mode_environment_is_generic_not_instance_specific(self):
        """Org names / trusted-repo paths belong in a private profile overlay
        (ADR-0012 Decision 6), never this public repo's settings.json."""
        d = load_settings()
        env_lines = d.get("autoMode", {}).get("environment", [])
        self.assertTrue(env_lines, "expected autoMode.environment to be populated")
        banned_substrings = ["/Users/", "horonom", "Horonomy", "AAASM", "HORO-"]
        for line in env_lines:
            for banned in banned_substrings:
                self.assertNotIn(banned, line, f"instance-specific content leaked into tracked autoMode.environment: {line!r}")


class NoOverlapOrDuplicates(unittest.TestCase):
    def test_no_duplicates_within_each_bucket(self):
        p = load_settings()["permissions"]
        for key in ("allow", "ask", "deny"):
            rules = p[key]
            self.assertEqual(len(rules), len(set(rules)), f"duplicate entries in permissions.{key}")

    def test_no_rule_in_two_buckets(self):
        """A rule present in both allow and ask/deny is not a conflict per
        Claude Code's documented precedence (deny > ask > allow always wins),
        but it is dead weight / a maintenance trap — flag it."""
        p = load_settings()["permissions"]
        allow, ask, deny = set(p["allow"]), set(p["ask"]), set(p["deny"])
        self.assertEqual(allow & ask, set())
        self.assertEqual(allow & deny, set())
        self.assertEqual(ask & deny, set())


class MandatorySafeguardsPresent(unittest.TestCase):
    """No accidental weakening of the operations ADR-0012 Decision 4/5 and
    docs/SECURITY.md's table declare mandatory. A future edit that silently
    drops one of these must fail CI, not just code review."""

    def test_mandatory_ask_rules_present(self):
        ask = set(load_settings()["permissions"]["ask"])
        missing = MANDATORY_ASK - ask
        self.assertEqual(missing, set(), f"mandatory ask rule(s) missing: {missing}")

    def test_mandatory_deny_rules_present(self):
        deny = set(load_settings()["permissions"]["deny"])
        missing = MANDATORY_DENY - deny
        self.assertEqual(missing, set(), f"mandatory deny rule(s) missing: {missing}")

    def test_rm_rf_and_broad_gh_api_methods_are_intentionally_not_blanket_ask(self):
        """ADR-0012's 60->47 narrowing is a deliberate decision, not drift —
        pin it so a well-meaning future PR can't silently re-add the
        overbroad form without updating the ADR."""
        ask = load_settings()["permissions"]["ask"]
        self.assertNotIn("Bash(rm -rf *)", ask)
        for method in ("PATCH", "PUT", "POST"):
            for flag in ("-X", "--method"):
                self.assertNotIn(f"Bash(gh api * {flag} {method}*)", ask)
                self.assertNotIn(f"Bash(rtk gh api * {flag} {method}*)", ask)


class SPE83PushAllowRegression(unittest.TestCase):
    """Structural mitigation for the open RTK defect (SPE-83): no allow rule
    may match `git push` at subcommand granularity in any form, because
    RTK's own PreToolUse self-decision bypasses a configured ask rule when
    one does. See ADR-0012 Decision 3."""

    def test_no_allow_rule_matches_plain_git_push(self):
        allow = load_settings()["permissions"]["allow"]
        offenders = [r for r in allow if FORBIDDEN_PUSH_ALLOW.match(r)]
        self.assertEqual(offenders, [], f"re-introduces SPE-83's bypass: {offenders}")


@unittest.skipUnless(shutil.which("rtk"), "rtk not installed — SPE-83 regression is skipped, not passed")
class RTKHookSelfDecision(unittest.TestCase):
    """Exercises the REAL installed rtk binary's PreToolUse hook against a
    disposable $HOME containing exactly this repo's tracked settings.json —
    not a live Claude Code session. This proves what rtk's hook *decides*
    given this file; it does not prove what a live Claude Code session does
    end-to-end (dialog suppressed or not) — that requires real runtime
    verification this suite cannot perform. Labeled synthetic per ADR-0012's
    Consequences section.
    """

    @classmethod
    def setUpClass(cls):
        cls.tmp_home = Path(tempfile.mkdtemp())
        (cls.tmp_home / ".claude").mkdir()
        shutil.copy(SETTINGS_PATH, cls.tmp_home / ".claude/settings.json")

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp_home, ignore_errors=True)

    def _hook(self, command):
        proc = subprocess.run(
            ["rtk", "hook", "claude"],
            input=json.dumps({"tool_name": "Bash", "tool_input": {"command": command}}),
            capture_output=True, text=True, env={"HOME": str(self.tmp_home), "PATH": "/usr/bin:/bin:/opt/homebrew/bin"},
        )
        return proc.stdout

    def test_force_push_is_not_self_approved(self):
        for cmd in ("git push --force-with-lease", "git push -f origin main", "git push origin main --force"):
            with self.subTest(cmd=cmd):
                out = self._hook(cmd)
                self.assertNotIn('"permissionDecision":"allow"', out.replace(" ", ""))

    def test_gh_api_delete_both_flag_orderings_not_self_approved(self):
        for cmd in ("gh api repos/foo/bar -X DELETE", "gh api -X DELETE repos/foo/bar", "gh api repos/foo/bar --method DELETE"):
            with self.subTest(cmd=cmd):
                out = self._hook(cmd)
                self.assertNotIn('"permissionDecision":"allow"', out.replace(" ", ""))

    def test_rtk_proxy_itself_is_unrewritten_and_relies_on_the_deny_rule(self):
        """rtk does not rewrite its own `proxy` subcommand — the deny rule
        must match the literal bare text, not an `rtk `-prefixed form."""
        out = self._hook("rtk proxy terraform destroy")
        self.assertNotIn("updatedInput", out)

    def test_routine_allowed_commands_are_not_denied_by_the_hook(self):
        """`cargo nextest run` is one of several commands RTK deliberately
        does not rewrite at all (empty hook output = defer to Claude Code's
        native engine, not a failure) — so this only asserts the hook never
        emits a deny/ask decision for these, not that it emits anything."""
        for cmd in ("git add -A", "cargo nextest run --lib", "gh pr create --title x"):
            with self.subTest(cmd=cmd):
                out = self._hook(cmd)
                self.assertNotIn('"permissionDecision":"deny"', out.replace(" ", ""))
                self.assertNotIn('"permissionDecision":"ask"', out.replace(" ", ""))


class SyncCheckSemanticDiff(unittest.TestCase):
    """scripts/sync-check.sh must treat a live machine's extra (private/
    machine-specific) permission rules as NOT drift, while still catching a
    genuinely missing mandatory rule. Both directions tested against
    disposable $HOME fixtures — never the real machine."""

    def _run_sync_check(self, home):
        proc = subprocess.run(
            ["bash", str(SYNC_CHECK)],
            capture_output=True, text=True,
            env={"CODING_AGENT_SYNC_HOME": str(home), "PATH": "/usr/bin:/bin:/opt/homebrew/bin"},
            cwd=ROOT,
        )
        return proc.stdout

    def _fixture_home(self, mutate):
        home = Path(tempfile.mkdtemp())
        (home / ".claude").mkdir()
        d = load_settings()
        mutate(d)
        (home / ".claude/settings.json").write_text(json.dumps(d))
        return home

    def test_live_superset_is_not_reported_as_differs(self):
        def mutate(d):
            d["permissions"]["allow"].append("Bash(/private/machine-specific-tool:*)")
            d["autoMode"]["environment"].append("**Private**: this machine's own org context")

        home = self._fixture_home(mutate)
        try:
            report = self._run_sync_check(home)
            settings_block = [l for l in report.splitlines() if "settings.json (owned settings)" in l]
            self.assertEqual(settings_block, [], f"superset incorrectly flagged as drift:\n{report}")
        finally:
            shutil.rmtree(home, ignore_errors=True)

    def test_live_missing_a_mandatory_rule_is_reported(self):
        def mutate(d):
            d["permissions"]["ask"] = [r for r in d["permissions"]["ask"] if "force" not in r]

        home = self._fixture_home(mutate)
        try:
            report = self._run_sync_check(home)
            self.assertIn("settings.json (owned settings)", report)
            self.assertIn("missing permissions.ask", report)
        finally:
            shutil.rmtree(home, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
