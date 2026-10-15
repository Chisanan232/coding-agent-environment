#!/usr/bin/env python3
"""Regression tests for ADR-0012's permission profile.

Fixtures are synthetic and disposable (tempdir $HOME, in-memory JSON) —
nothing here touches a real ~/.claude. Where real Claude Code runtime
behavior would be the only true verification (does a live session actually
prompt), that is explicitly out of reach here and not claimed: see the
RTKHookSelfDecision class docstring and ADR-0012's Consequences section.
"""
import fnmatch
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
    "Bash(terraform * destroy*)",
    "Bash(rtk terraform * destroy*)",
    "Bash(terraform * apply *-destroy*)",
    "Bash(terraform * apply *--destroy*)",
    "Bash(rtk proxy *)",
    "Bash(gcloud secrets versions access *)",
    "Bash(rtk gcloud secrets versions access *)",
    "Bash(gcloud * secrets versions access*)",
    "Bash(rtk gcloud * secrets versions access*)",
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


def bash_rule_matches(command, rule):
    """Approximates Claude Code's documented Bash rule matching: '*' is a
    greedy any-text wildcard: a ':*' suffix is equivalent to a trailing
    ' *', including matching the bare command with nothing after it. This
    is a model of the documented semantics for CI-time regression testing —
    not the real engine — see RTKHookSelfDecision's docstring for the
    equivalent caveat about the real rtk binary."""
    if not (rule.startswith("Bash(") and rule.endswith(")")):
        return False
    pattern = rule[5:-1]
    if pattern.endswith(":*"):
        stem = pattern[:-2]
        return command == stem or fnmatch.fnmatchcase(command, stem + " *")
    return fnmatch.fnmatchcase(command, pattern)


class GlobalFlagReorderingDenyRegression(unittest.TestCase):
    """Found via an automated security review of this ADR's own commit
    (2026-10-10): a global flag placed BEFORE the subcommand (terraform's
    `-chdir=`, gcloud's `--project=`, etc.) changes the command's word
    order enough that a deny rule anchored to "terraform destroy" or
    "gcloud secrets versions access" as a contiguous prefix no longer
    matches — the same bug class as SPE-83's gh-api-flag-ordering fix,
    just not yet applied to these two deny rules until now. Mirrors the
    verification methodology already used for SPE-83's allow-side fix."""

    def test_terraform_destroy_still_denied_with_a_preceding_global_flag(self):
        deny = load_settings()["permissions"]["deny"]
        for cmd in ("terraform -chdir=/tmp destroy", "terraform destroy",
                    "terraform -chdir=/tmp apply -destroy",
                    "terraform apply -auto-approve -destroy",
                    "terraform apply -destroy"):
            with self.subTest(cmd=cmd):
                self.assertTrue(any(bash_rule_matches(cmd, r) for r in deny), f"not denied: {cmd!r}")

    def test_terraform_plan_with_the_destroy_flag_is_not_falsely_denied(self):
        """`terraform plan -destroy` never mutates anything — it's exactly
        as read-only as `terraform plan`. The fix for the above must not
        turn this into a false block."""
        deny = load_settings()["permissions"]["deny"]
        for cmd in ("terraform plan -destroy", "terraform plan", "terraform init", "terraform apply"):
            with self.subTest(cmd=cmd):
                self.assertFalse(any(bash_rule_matches(cmd, r) for r in deny), f"incorrectly denied: {cmd!r}")

    def test_gcloud_secrets_access_still_denied_with_a_preceding_global_flag(self):
        deny = load_settings()["permissions"]["deny"]
        for cmd in ("gcloud --project=evil secrets versions access latest",
                    "gcloud secrets versions access latest",
                    "rtk gcloud --project=evil secrets versions access latest"):
            with self.subTest(cmd=cmd):
                self.assertTrue(any(bash_rule_matches(cmd, r) for r in deny), f"not denied: {cmd!r}")


class RTKPrefixTwinCoverage(unittest.TestCase):
    """Found via an automated security review (2026-10-11): every `ask`-
    gated destructive-command rule must have an `rtk <same rule>` twin,
    since RTK's hook rewrites a Bash invocation by prefixing `rtk ` onto
    it (RTK.md: "git status -> rtk git status", documented as applying
    to "all other commands", not just git). A bare rule with no rtk twin
    never matches the RTK-rewritten form Claude Code's native engine
    actually sees, so the ask prompt silently never fires for it — the
    same bug shape SPE-83 and the gh-api/terraform/gcloud fixes already
    address, just previously missed for git reset --hard, git clean -f,
    kill -9, pkill, and killall."""

    # Bare ask rules whose rtk-prefixed Bash twin is mandatory. Each must
    # be matched by "rtk " + the bare rule's own pattern.
    _REQUIRES_RTK_TWIN = (
        "Bash(git reset --hard *)",
        "Bash(git clean -f*)",
        "Bash(kill -9 *)",
        "Bash(pkill *)",
        "Bash(killall *)",
    )

    def test_every_dangerous_bash_ask_rule_has_an_rtk_prefixed_twin(self):
        ask = load_settings()["permissions"]["ask"]
        for rule in self._REQUIRES_RTK_TWIN:
            pattern = rule[len("Bash("):-1]
            twin = f"Bash(rtk {pattern})"
            with self.subTest(rule=rule):
                self.assertIn(twin, ask, f"missing rtk-prefixed twin of {rule!r}: expected {twin!r}")

    def test_rtk_rewritten_dangerous_commands_are_still_caught(self):
        ask = load_settings()["permissions"]["ask"]
        for cmd in (
            "rtk git reset --hard HEAD~1",
            "rtk git clean -fd",
            "rtk kill -9 1234",
            "rtk pkill node",
            "rtk killall node",
        ):
            with self.subTest(cmd=cmd):
                self.assertTrue(any(bash_rule_matches(cmd, r) for r in ask), f"not caught: {cmd!r}")

    def test_no_bash_ask_or_deny_rule_is_missing_its_rtk_twin(self):
        """General sweep, not just the curated list above: for every bare
        (non-rtk-prefixed) Bash ask/deny rule, its exact `rtk `-prefixed
        twin must also be present. Catches this bug class even if a
        future rule is added without its twin."""
        settings = load_settings()
        for kind in ("ask", "deny"):
            rules = settings["permissions"][kind]
            for rule in rules:
                if not rule.startswith("Bash(") or rule.startswith("Bash(rtk "):
                    continue
                twin = "Bash(rtk " + rule[len("Bash("):]
                with self.subTest(kind=kind, rule=rule):
                    self.assertIn(twin, rules, f"{kind} rule {rule!r} has no rtk twin {twin!r}")

    def test_rtk_terraform_apply_bare_is_ask_gated(self):
        ask = load_settings()["permissions"]["ask"]
        for cmd in ("terraform apply -auto-approve", "rtk terraform apply -auto-approve"):
            with self.subTest(cmd=cmd):
                self.assertTrue(any(bash_rule_matches(cmd, r) for r in ask), f"not caught: {cmd!r}")

    def test_rtk_terraform_destroy_bare_is_denied(self):
        deny = load_settings()["permissions"]["deny"]
        for cmd in ("terraform destroy", "rtk terraform destroy"):
            with self.subTest(cmd=cmd):
                self.assertTrue(any(bash_rule_matches(cmd, r) for r in deny), f"not denied: {cmd!r}")

    def test_rtk_terraform_apply_destroy_adjacent_is_denied(self):
        deny = load_settings()["permissions"]["deny"]
        for cmd in (
            "terraform apply -destroy",
            "rtk terraform apply -destroy",
            "terraform apply --destroy",
            "rtk terraform apply --destroy",
        ):
            with self.subTest(cmd=cmd):
                self.assertTrue(any(bash_rule_matches(cmd, r) for r in deny), f"not denied: {cmd!r}")


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

    def test_live_missing_the_merge_automode_allow_entry_is_reported(self):
        def mutate(d):
            d["autoMode"].pop("allow", None)

        home = self._fixture_home(mutate)
        try:
            report = self._run_sync_check(home)
            self.assertIn("missing autoMode.allow", report)
        finally:
            shutil.rmtree(home, ignore_errors=True)


OVERLAY_TEMPLATE = ROOT / "docs/examples/secret-metadata-overlay.example.json"


def _rule_regex(rule):
    """Emulate Claude Code Bash rule matching: `X:*` = X or X + space + anything;
    otherwise `*` spans any text. Static layer only: the classifier, quoting and
    `$()` are not modeled, and compound commands (`a && b`) are NOT split the way
    Claude Code splits them, so this is looser than the real engine for compounds."""
    body = rule[len("Bash("):-1]
    if body.endswith(":*"):
        pre = re.escape(body[:-2])
        return re.compile(rf"^{pre}(?: .*)?$", re.S)
    return re.compile("^" + ".*".join(re.escape(x) for x in body.split("*")) + "$", re.S)


def static_decision(command, permissions):
    """deny > ask > allow, first bucket with a match wins; else 'classifier'."""
    for bucket in ("deny", "ask", "allow"):
        for rule in permissions[bucket]:
            if rule.startswith("Bash(") and _rule_regex(rule).match(command):
                return bucket
    return "classifier"


class SPE105EffectiveBehavior(unittest.TestCase):
    """Effective static allow/ask/deny outcomes, not just JSON validity. The
    Auto Mode classifier layer is NOT modeled and is not claimed here."""

    def setUp(self):
        self.perm = load_settings()["permissions"]

    def assertDecision(self, expected, commands, perm=None):
        for cmd in commands:
            for prefix in ("", "rtk "):
                with self.subTest(cmd=prefix + cmd):
                    self.assertEqual(expected, static_decision(prefix + cmd, perm or self.perm))

    def test_read_only_git_is_statically_allowed(self):
        self.assertDecision("allow", [
            "git status", "git status --short", "git log --oneline -5", "git diff HEAD~1",
            "git show abc123", "git rev-parse HEAD", "git rev-list --count main..HEAD",
            "git ls-files", "git remote -v", "git worktree list --porcelain",
        ])

    def test_narrow_git_allows_do_not_cover_broader_commands(self):
        self.assertDecision("classifier", [
            "git -c core.pager=evil status", "git remote add x http://x", "git remote set-url origin x",
            "git reflog expire --all", "git statusx", "git worktree remove /some/tree",
        ])

    def test_output_writing_git_forms_ask(self):
        self.assertDecision("ask", ["git diff --output=/tmp/x", "git log -p --output=/tmp/y", "git show HEAD --output=f", "git diff --ext-diff HEAD~1", "git diff --no-index /dev/null /tmp/x"])

    def test_dangerous_worktree_operations_ask_in_every_flag_position(self):
        self.assertDecision("ask", [
            "git worktree remove --force /t", "git worktree remove -f /t", "git worktree remove /t -f",
            "git worktree remove /t --force", "git worktree remove -f -f /t",
            "git worktree prune", "git worktree prune --dry-run", "git worktree move a b",
        ])

    def test_force_abbreviations_and_global_options_cannot_skip_the_worktree_asks(self):
        """git accepts any unambiguous long-option prefix (--for, --fo, --f)."""
        self.assertDecision("ask", [
            "git worktree remove --forc /t", "git worktree remove --for /t", "git worktree remove --fo /t",
            "git worktree remove /t --f", "git worktree remove --for --for /t",
            "git -C /repo worktree remove /t", "git -c k=v worktree remove --force /t",
            "git -C /repo worktree prune", "git -C /repo worktree move a b",
            "git worktree unlock /t", "git worktree add -B existing ../w HEAD~5",
            "git worktree add ../w -B existing", "git worktree add --force ../w", "git worktree add ../w --for",
            "git fetch --upload-pack=cmd .", "git fetch origin --upload-pack=cmd", "git fetch --upl=cmd .",
        ])

    def test_worktree_repair_is_bare_only(self):
        self.assertDecision("allow", ["git worktree repair"])
        self.assertDecision("classifier", ["git worktree repair /some/path"])

    def test_routine_worktree_operations_remain_allowed(self):
        self.assertDecision("allow", ["git worktree add ../w -b feature", "git worktree list", "git worktree repair"])

    def test_no_allow_rule_reaches_git_push_or_force_removal(self):
        self.assertDecision("classifier", ["git push origin main"])
        self.assertDecision("ask", ["git push --force origin x", "git reset --hard HEAD~1", "git clean -fd"])

    def test_deny_boundaries_unchanged_and_beat_everything(self):
        self.assertDecision("deny", [
            "terraform destroy", "terraform -chdir=x destroy", "terraform apply -destroy",
            "gcloud secrets versions access latest --secret=s",
            "gcloud --project=p secrets versions access latest --secret=s",
        ])
        self.assertIn("mcp__neon__get_connection_string", self.perm["deny"])
        self.assertEqual("deny", static_decision("rtk proxy git push", self.perm))

    def test_public_baseline_carries_no_project_specific_secret_rules(self):
        """ADR-0012 Decision 2/6: private project names live in an overlay."""
        blob = json.dumps(self.perm)
        self.assertNotIn("horonom", blob)
        self.assertNotIn("sync-neon-secret", blob)
        self.assertNotRegex(blob, r"gcloud secrets (?:versions )?(?:add|create)")

    def test_merge_authorization_is_evidence_gated_prose_not_a_blanket(self):
        d = load_settings()
        entries = d["autoMode"]["allow"]
        self.assertEqual("$defaults", entries[0])
        merge = [e for e in entries if "merge" in e.lower()]
        self.assertEqual(1, len(merge))
        for needle in ("merge commit", "--match-head-commit", "squash", "green", "conflicts", "owns or administers"):
            self.assertIn(needle, merge[0])
        self.assertNotIn("soft_deny", d["autoMode"])
        self.assertNotIn("hard_deny", d["autoMode"])

    def test_bypass_permissions_never_enabled(self):
        text = SETTINGS_PATH.read_text()
        self.assertNotIn("bypassPermissions", text)
        self.assertEqual("auto", load_settings()["permissions"]["defaultMode"])


class SecretMetadataOverlayTemplate(unittest.TestCase):
    """The private-overlay template grants metadata + write-only version add,
    never plaintext reads, and deny still wins when merged with the baseline."""

    def setUp(self):
        t = json.loads(OVERLAY_TEMPLATE.read_text())
        rules = [r.replace("<private-gcp-project>", "proj-x") for r in t["permissions"]["allow"]]
        base = load_settings()["permissions"]
        asks = t["permissions"]["ask"]
        self.merged = {"deny": base["deny"], "ask": base["ask"] + asks, "allow": base["allow"] + rules}

    def test_metadata_and_write_only_commands_are_allowed_for_the_scoped_project(self):
        for cmd in (
            "gcloud secrets list --project=proj-x", "gcloud secrets describe s1 --project=proj-x",
            "gcloud secrets create s1 --project=proj-x", "gcloud secrets versions list s1 --project=proj-x",
            "gcloud secrets versions describe 3 --secret=s1 --project=proj-x",
            "gcloud secrets versions add s1 --project=proj-x --data-file=-",
        ):
            with self.subTest(cmd=cmd):
                self.assertEqual("allow", static_decision(cmd, self.merged))

    def test_other_projects_and_unscoped_shapes_are_not_allowed(self):
        for cmd in (
            "gcloud secrets versions list s1 --project=other", "gcloud secrets create s1",
            "gcloud secrets versions add s1 --project=proj-x --data-file=/etc/passwd",
            "gcloud secrets versions add s1 --project=proj-x --data-file=- --extra",
            "gcloud secrets delete s1 --project=proj-x", "gcloud --project=proj-x secrets create s1 --data-file /k", "gcloud secrets versions destroy 1 --secret=s --project=proj-x",
        ):
            with self.subTest(cmd=cmd):
                self.assertNotEqual("allow", static_decision(cmd, self.merged))

    def test_qualified_names_impersonation_and_file_uploads_ask(self):
        for cmd in (
            "gcloud secrets describe projects/other/secrets/s --project=proj-x",
            "gcloud secrets versions add projects/other/secrets/s --project=proj-x --data-file=-",
            "gcloud secrets list --impersonate-service-account=a@b.iam --project=proj-x",
            "gcloud secrets create s1 --data-file=/home/u/key.pem --project=proj-x",
            "gcloud secrets create s1 --data-file=./k --project=proj-x",
            "gcloud secrets create --data-file=/home/u/key s1 --project=proj-x",
            "gcloud --impersonate-service-account=a@b.iam secrets list --project=proj-x",
        ):
            with self.subTest(cmd=cmd):
                self.assertEqual("ask", static_decision(cmd, self.merged))

    def test_secret_value_reads_stay_denied_even_with_the_overlay(self):
        for cmd in (
            "gcloud secrets versions access latest --secret=s1 --project=proj-x",
            "gcloud secrets versions access 1 --secret=s --project=proj-x --data-file=-",
            "gcloud --project=proj-x secrets versions access latest --secret=s1",
        ):
            with self.subTest(cmd=cmd):
                self.assertEqual("deny", static_decision(cmd, self.merged))

    def test_template_has_no_real_project_name(self):
        self.assertNotIn("horonom", OVERLAY_TEMPLATE.read_text())


if __name__ == "__main__":
    unittest.main()
