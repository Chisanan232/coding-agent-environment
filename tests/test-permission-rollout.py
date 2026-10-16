#!/usr/bin/env python3
"""scripts/permission-rollout.py against synthetic fixtures in a temp $HOME.
Nothing here reads or writes a real ~/.claude or the repo's own settings."""
import copy
import importlib.util
import io
import json
import os
import contextlib
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("rollout", ROOT / "scripts/permission-rollout.py")
rollout = importlib.util.module_from_spec(spec)
spec.loader.exec_module(rollout)

BASE_ASK = [
    "Bash(terraform apply:*)", "Bash(rtk terraform apply:*)",
    "Bash(terraform destroy:*)", "Bash(rtk terraform destroy:*)",
    "Bash(terraform -* destroy*)", "Bash(rtk terraform -* destroy*)",
    "Bash(terraform -* apply*)", "Bash(rtk terraform -* apply*)",
    "Bash(rtk proxy *)",
    "Bash(gcloud secrets versions access *)", "Bash(rtk gcloud secrets versions access *)",
    "Bash(gcloud * secrets versions access*)", "Bash(rtk gcloud * secrets versions access*)",
    "mcp__neon__get_connection_string",
    "Bash(git worktree add -f*)", "Bash(git worktree add * -f*)",
    "Bash(git worktree remove* -f*)", "Bash(git * worktree remove*)",
]
BASELINE = {
    "permissions": {"allow": ["Bash(git status:*)", "Bash(git worktree list:*)"], "ask": BASE_ASK, "deny": [], "defaultMode": "auto"},
    "autoMode": {
        "environment": ["$defaults", "**Worktree and session ownership**: only task-owned worktrees."],
        "allow": ["$defaults", "**Authorized pull request merge**: user-authorized, merge commit, pinned SHA."],
    },
}
LIVE = {
    "model": "sonnet",
    "hooks": {"PreToolUse": [{"matcher": "Bash"}]},
    "permissions": {
        "allow": ["Bash(git worktree *)", "Bash(rtk git worktree *)", "Bash(sync-private:*)"],
        "ask": ["Bash(git worktree add*-f*)", "Bash(git * worktree remove*)", "Bash(gcloud*projects/*)", "Bash(pkill *)"],
        "deny": [
            "Bash(terraform destroy:*)", "Bash(terraform apply -destroy*)", "Bash(rtk terraform destroy *)", "Bash(terraform * destroy*)",
            "Bash(rtk proxy *)", "Bash(gcloud secrets versions access *)", "mcp__neon__get_connection_string",
            "Bash(private-only-deny *)",
        ],
        "defaultMode": "auto",
    },
    "autoMode": {
        "environment": [
            "### Org-wide", "**Organization**: None configured",
            "**Source control**: The trusted repo only — no remotes are configured for /x",
            "**Trusted repo**: /x (no remotes configured; local-only checkout)",
            "routine under /x/ prefix",
            "**Worktree cleanup**: git worktree remove --force is routine.",
            "**Process cleanup**: terminating this session's own process is routine.",
            "**Force-push**: every force-push (--force, --force-with-lease, -f) always asks.",
        ],
        "allow": ["$defaults", "**Evidence-gated pull request merge**: old prose.", "**Private**: keep me."],
    },
}


class Fixture(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
        (self.home / ".claude").mkdir()
        self._old_home = os.environ.get("HOME")
        os.environ["HOME"] = str(self.home)
        self.base_p = self.home / "baseline.json"
        self.live_p = self.home / ".claude/settings.json"
        self.base_p.write_text(json.dumps(BASELINE, indent=4))
        self.live_p.write_text(json.dumps(LIVE, indent=4) + "\n")

    def tearDown(self):
        if self._old_home is None:
            os.environ.pop("HOME", None)
        else:
            os.environ["HOME"] = self._old_home
        self.tmp.cleanup()

    def run_tool(self, *argv):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = rollout.main(list(argv))
        return rc, out.getvalue()

    def merged(self, owners=("OwnerA", "OwnerB")):
        return rollout.merge(copy.deepcopy(LIVE), copy.deepcopy(BASELINE), list(owners))


class MergeSemantics(Fixture):
    def test_deny_is_emptied_and_every_former_deny_command_asks(self):
        new = self.merged()
        self.assertEqual([], new["permissions"]["deny"])
        for cmd in rollout.FORMER_DENY_COMMANDS:
            for pre in ("", "rtk "):
                if pre == "rtk " and cmd.startswith("rtk "):
                    continue
                with self.subTest(cmd=pre + cmd):
                    self.assertEqual("ask", rollout.static_decision(pre + cmd, new["permissions"]))

    def test_live_only_deny_is_moved_to_ask_not_dropped_or_allowed(self):
        new = self.merged()
        self.assertIn("Bash(private-only-deny *)", new["permissions"]["ask"])
        self.assertNotIn("Bash(private-only-deny *)", new["permissions"]["allow"])
        self.assertEqual("ask", rollout.static_decision("private-only-deny x", new["permissions"]))

    def test_old_broad_terraform_destroy_glob_does_not_survive_as_a_false_positive(self):
        new = self.merged()
        self.assertNotIn("Bash(terraform * destroy*)", new["permissions"]["ask"])
        for cmd in ("terraform plan -out destroy.tfplan", "terraform workspace select destroy-x"):
            with self.subTest(cmd=cmd):
                self.assertNotEqual("ask", rollout.static_decision(cmd, new["permissions"]))
        for cmd in ("terraform destroy", "terraform -chdir=x destroy", "rtk terraform -chdir=x destroy"):
            with self.subTest(cmd=cmd):
                self.assertEqual("ask", rollout.static_decision(cmd, new["permissions"]))

    def test_subsumed_deny_variants_are_not_duplicated_into_ask(self):
        ask = self.merged()["permissions"]["ask"]
        self.assertNotIn("Bash(terraform apply -destroy*)", ask)
        self.assertNotIn("Bash(rtk terraform destroy *)", ask)

    def test_live_only_entries_and_unrelated_keys_survive(self):
        new = self.merged()
        self.assertIn("Bash(sync-private:*)", new["permissions"]["allow"])
        self.assertIn("Bash(pkill *)", new["permissions"]["ask"])
        self.assertIn("**Private**: keep me.", new["autoMode"]["allow"])
        self.assertIn("**Process cleanup**: terminating this session's own process is routine.", new["autoMode"]["environment"])
        self.assertEqual(LIVE["model"], new["model"])
        self.assertEqual(LIVE["hooks"], new["hooks"])

    def test_superseded_broad_rules_are_retired(self):
        new = self.merged()
        self.assertNotIn("Bash(git worktree *)", new["permissions"]["allow"])
        self.assertNotIn("Bash(git worktree add*-f*)", new["permissions"]["ask"])
        self.assertIn("Bash(git * worktree remove*)", new["permissions"]["ask"], "ownership gate on global-option removal is kept")
        self.assertNotIn("Bash(gcloud*projects/*)", new["permissions"]["ask"])
        self.assertIn("Bash(gcloud secrets*projects/*)", new["permissions"]["ask"], "narrowed replacement kept in live")

    def test_stale_environment_snapshot_is_replaced_by_defaults_and_owner_lines(self):
        env = self.merged()["autoMode"]["environment"]
        self.assertEqual("$defaults", env[0])
        joined = "\n".join(env)
        for stale in ("no remotes", "local-only checkout", "None configured", "### ", "routine under", "Worktree cleanup"):
            self.assertNotIn(stale, joined)
        self.assertIn("`OwnerA`, `OwnerB`", joined)
        self.assertEqual(2, sum("OwnerA" in e for e in env))

    def test_older_live_copies_of_baseline_lines_are_not_duplicated(self):
        env = self.merged()["autoMode"]["environment"]
        self.assertFalse(any(e.startswith("**Force-push**: every force-push (--force,") for e in env))

    def test_old_merge_prose_is_replaced_by_the_baseline_prose(self):
        allow = self.merged()["autoMode"]["allow"]
        self.assertEqual("$defaults", allow[0])
        self.assertFalse(any(e.startswith("**Evidence-gated") for e in allow))
        self.assertIn(BASELINE["autoMode"]["allow"][1], allow)

    def test_a_baseline_deny_rule_becomes_ask_not_dropped(self):
        base = copy.deepcopy(BASELINE)
        base["permissions"]["deny"] = ["Bash(newly-denied *)"]
        new = rollout.merge(copy.deepcopy(LIVE), base, [])
        self.assertEqual([], new["permissions"]["deny"])
        self.assertIn("Bash(newly-denied *)", new["permissions"]["ask"])

    def test_merge_is_idempotent(self):
        once = self.merged()
        twice = rollout.merge(copy.deepcopy(once), copy.deepcopy(BASELINE), ["OwnerA", "OwnerB"])
        self.assertEqual(once, twice)

    def test_no_owners_means_no_trust_lines(self):
        self.assertNotIn("OwnerA", json.dumps(self.merged(owners=())))

    def test_merged_result_passes_its_own_policy_check(self):
        self.assertEqual([], rollout.problems(self.merged()))

    def test_bypass_permissions_and_git_push_allow_are_flagged(self):
        bad = self.merged()
        bad["permissions"]["defaultMode"] = "bypassPermissions"
        bad["permissions"]["allow"].append("Bash(git push:*)")
        found = "\n".join(rollout.problems(bad))
        self.assertIn("bypassPermissions", found)
        self.assertIn("git push", found)


class PersonalDevTrust(Fixture):
    def real(self, name):
        return os.path.realpath(self.home / name)

    def merged_dev(self, live=None, dev=("~/dev-root",)):
        return rollout.merge(copy.deepcopy(live or LIVE), copy.deepcopy(BASELINE), ["OwnerA"], list(dev))

    def test_dev_dirs_add_scoped_file_access_prose_allow_exception_and_env_read_gate(self):
        new = self.merged_dev()
        self.assertEqual([self.real("dev-root")], new["permissions"]["additionalDirectories"])
        env = "\n".join(new["autoMode"]["environment"])
        self.assertIn("Personal development machine", env)
        self.assertIn(f"`{self.real('dev-root')}`", env)
        for guard in ("credential or key files", ".claude", ".git/hooks", "shell startup"):
            self.assertIn(guard, env)
        allow = "\n".join(new["autoMode"]["allow"])
        self.assertIn("Cross-repository personal development", allow)
        for guard in ("credential or key files", ".claude", ".git/hooks", "shell startup", ".env", "irreversible deletion"):
            self.assertIn(guard, allow)
        for rule in ("Read(**/.env)", "Read(**/.env.local)"):
            self.assertIn(rule, new["permissions"]["ask"])
        self.assertEqual([], rollout.problems(new))

    def test_without_dev_dirs_nothing_about_directories_is_added(self):
        new = rollout.merge(copy.deepcopy(LIVE), copy.deepcopy(BASELINE), ["OwnerA"])
        self.assertNotIn("additionalDirectories", new["permissions"])
        self.assertNotIn("Personal development machine", json.dumps(new))
        self.assertNotIn("Read(**/.env)", new["permissions"]["ask"])

    def test_narrower_dev_dirs_narrow_the_grant_and_the_prose_together(self):
        first = self.merged_dev(dev=("~/dev", "~/work"))
        self.assertEqual([self.real("dev"), self.real("work")], first["permissions"]["additionalDirectories"])
        second = self.merged_dev(live=first, dev=("~/work",))
        self.assertEqual([self.real("work")], second["permissions"]["additionalDirectories"])
        env = [e for e in second["autoMode"]["environment"] if e.startswith("**Personal development machine")]
        self.assertEqual(1, len(env))
        self.assertNotIn(self.real("dev") + "`", env[0])
        self.assertEqual(1, sum(e.startswith("**Cross-repository") for e in second["autoMode"]["allow"]))
        self.assertEqual(first, self.merged_dev(live=first, dev=("~/dev", "~/work")))

    def test_trailing_slash_and_duplicates_collapse(self):
        new = self.merged_dev(dev=("~/x", "~/x/"))
        self.assertEqual([self.real("x")], new["permissions"]["additionalDirectories"])

    def test_every_bypass_the_reviewer_found_is_rejected(self):
        home = str(Path.home())
        bad = ["~/.", "~/./", home + "/.", "~/dev/../..", "~/..", "/..", "/Volumes/x/../..", "/Users/other", "/users/someone",
               "/USERS", "~/Library", home + "/Library", home + "/library/Keychains", "~/.config", home + "/.SSH", "~/.kube", "~/.docker",
               "~/.netrc", "~/.npmrc", "/private/tmp", "/tmp", "/", "~", home, "/Volumes", "/Volumes/OnlyName", "~someone/dev",
               "relative/path", "", "~/a`b", "~/a\nb", "~/.ssh", "~/.claude/x", "/private/etc"]
        for d in bad:
            with self.subTest(dir=d):
                self.assertIsNotNone(rollout.check_dev_dir(d), d)
        self.assertEqual(self.real("dev-root"), rollout.normalize_dev_dir("~/dev-root")[0])
        self.assertIsNone(rollout.check_dev_dir("/Volumes/Personal/dev"))

    def test_a_symlink_into_a_credential_directory_is_rejected(self):
        (self.home / ".ssh").mkdir()
        (self.home / "innocent").symlink_to(self.home / ".ssh")
        self.assertIsNotNone(rollout.check_dev_dir("~/innocent"))
        (self.home / "outside").symlink_to("/private/tmp")
        self.assertIsNotNone(rollout.check_dev_dir("~/outside"))

    def test_cli_refuses_bad_dev_dirs_and_bad_owners_and_writes_nothing(self):
        before = self.live_p.read_bytes()
        for extra in (["--dev-dirs", str(Path.home())], ["--dev-dirs", "~/dev/../.."], ["--owners=bad owner`x"], ["--owners=-leading"], ["--owners=a/b"]):
            with self.subTest(extra=extra):
                rc, out = self.run_tool("apply", "--baseline", str(self.base_p), "--live", str(self.live_p), *extra,
                                        "--expect-live-sha", rollout.sha(self.live_p), "--expect-baseline-sha", rollout.sha(self.base_p))
                self.assertEqual(6, rc, out)
                self.assertEqual(before, self.live_p.read_bytes())

    def test_verify_flags_a_broad_additional_directory(self):
        bad = self.merged_dev()
        bad["permissions"]["additionalDirectories"].append(str(Path.home()))
        self.assertIn("home directory", "\n".join(rollout.problems(bad)))

    def test_existing_additional_directories_are_preserved(self):
        live = copy.deepcopy(LIVE)
        live["permissions"]["additionalDirectories"] = ["~/already-there"]
        new = self.merged_dev(live=live)
        self.assertEqual(["~/already-there", self.real("dev-root")], new["permissions"]["additionalDirectories"])


class CuratedEnvironmentLines(Fixture):
    def env(self, lines):
        live = copy.deepcopy(LIVE)
        live["autoMode"]["environment"] = lines
        return rollout.merge(live, copy.deepcopy(BASELINE), [])["autoMode"]["environment"]

    def test_curated_lines_with_a_default_key_survive_and_default_copies_go(self):
        curated = ["**Sensitive data locations**: ~/vault, ~/clients", "**Protected IaC scopes**: modules/prod only",
                   "**Sensitive remote targets**: host db-prod-1", "**Repository visibility**: all my repos are private"]
        copies = ["**Sensitive data locations & audiences**: any file or store holding personal data, confidential business data, credentials",
                  "**Primary use of Claude Code**: software development",
                  "**Repository visibility**: assume private unless the remote host and repo name indicate otherwise"]
        env = self.env(curated + copies)
        for line in curated:
            self.assertIn(line, env)
        for line in copies:
            self.assertNotIn(line, env)

    def test_secrets_line_keeps_its_live_only_guidance_and_loses_only_the_false_sentence(self):
        line = ("**Secrets**: never materialize a credential in plaintext. Use `/x/bin/sync-neon-secret` for Neon to GCP transfers "
                "(stays inside an OS pipe). `gcloud secrets versions access` and `mcp__neon__get_connection_string` are denied by rule, not merely discouraged.")
        env = self.env([line])
        kept = [e for e in env if e.startswith("**Secrets**")]
        self.assertEqual(1, len(kept))
        self.assertIn("sync-neon-secret", kept[0])
        self.assertIn("OS pipe", kept[0])
        self.assertNotIn("denied by rule", kept[0])
        self.assertEqual(env, self.env(env), "rewriting is idempotent")

    def test_plan_lists_every_environment_line_it_removes(self):
        rc, out = self.run_tool("plan", "--baseline", str(self.base_p), "--live", str(self.live_p))
        self.assertEqual(0, rc, out)
        self.assertIn("environment lines removed (", out)
        self.assertIn("Worktree cleanup", out)


class FileModes(Fixture):
    def test_extra_files_keep_the_live_files_mode(self):
        os.chmod(self.live_p, 0o600)
        rc, out = self.run_tool("apply", "--baseline", str(self.base_p), "--live", str(self.live_p), "--unmerged-ok",
                                "--expect-live-sha", rollout.sha(self.live_p), "--expect-baseline-sha", rollout.sha(self.base_p))
        self.assertEqual(0, rc, out)
        backup = next((self.home / ".claude/backups").glob("settings.json.pre-SPE-106.*"))
        self.assertEqual(0o600, backup.stat().st_mode & 0o777)
        rc, out = self.run_tool("rollback", "--live", str(self.live_p), "--backup", str(backup))
        self.assertEqual(0, rc, out)
        saved = next((self.home / ".claude").glob("settings.json.pre-rollback.*"))
        self.assertEqual(0o600, saved.stat().st_mode & 0o777)

    def test_rollback_into_a_missing_file_uses_the_backup_mode(self):
        backup = self.home / "b.json"
        backup.write_text("{}")
        os.chmod(backup, 0o600)
        self.live_p.unlink()
        rc, _ = self.run_tool("rollback", "--live", str(self.live_p), "--backup", str(backup))
        self.assertEqual(0, rc)
        self.assertEqual(0o600, self.live_p.stat().st_mode & 0o777)


class CommandModes(Fixture):
    def args(self, *extra):
        return ["--baseline", str(self.base_p), "--live", str(self.live_p), "--owners", "OwnerA", "--unmerged-ok", *extra]

    def test_plan_writes_nothing(self):
        before = self.live_p.read_bytes()
        rc, out = self.run_tool("plan", *self.args())
        self.assertEqual(0, rc)
        self.assertEqual(before, self.live_p.read_bytes())
        self.assertIn("permissions.deny: 8 -> 0", out)
        self.assertFalse((self.home / ".claude/backups").exists())

    def test_apply_refuses_without_both_hashes(self):
        before = self.live_p.read_bytes()
        rc, _ = self.run_tool("apply", *self.args())
        self.assertEqual(3, rc)
        rc, _ = self.run_tool("apply", *self.args("--expect-live-sha", rollout.sha(self.live_p), "--expect-baseline-sha", "0" * 64))
        self.assertEqual(3, rc)
        self.assertEqual(before, self.live_p.read_bytes())

    def test_apply_backs_up_writes_and_rollback_restores(self):
        original = self.live_p.read_bytes()
        rc, out = self.run_tool("apply", *self.args("--expect-live-sha", rollout.sha(self.live_p), "--expect-baseline-sha", rollout.sha(self.base_p)))
        self.assertEqual(0, rc, out)
        applied = json.loads(self.live_p.read_text())
        self.assertEqual([], applied["permissions"]["deny"])
        backups = list((self.home / ".claude/backups").glob("settings.json.pre-SPE-106.*"))
        self.assertEqual(1, len(backups))
        self.assertEqual(original, backups[0].read_bytes())
        rc, _ = self.run_tool("verify", "--live", str(self.live_p))
        self.assertEqual(0, rc)
        rc, _ = self.run_tool("rollback", "--live", str(self.live_p), "--backup", str(backups[0]))
        self.assertEqual(0, rc)
        self.assertEqual(original, self.live_p.read_bytes())

    def test_back_to_back_applies_never_overwrite_the_original_backup(self):
        original = self.live_p.read_bytes()
        for _ in range(2):
            rc, out = self.run_tool("apply", *self.args("--expect-live-sha", rollout.sha(self.live_p), "--expect-baseline-sha", rollout.sha(self.base_p)))
            self.assertEqual(0, rc, out)
        backups = sorted((self.home / ".claude/backups").glob("settings.json.pre-SPE-106.*"))
        self.assertEqual(2, len(backups))
        self.assertIn(original, [b.read_bytes() for b in backups])

    def test_apply_preserves_file_mode(self):
        os.chmod(self.live_p, 0o600)
        rc, out = self.run_tool("apply", *self.args("--expect-live-sha", rollout.sha(self.live_p), "--expect-baseline-sha", rollout.sha(self.base_p)))
        self.assertEqual(0, rc, out)
        self.assertEqual(0o600, self.live_p.stat().st_mode & 0o777)

    def test_apply_refuses_when_live_changed_since_review(self):
        reviewed = rollout.sha(self.live_p)
        base_sha = rollout.sha(self.base_p)
        live = json.loads(self.live_p.read_text())
        live["permissions"]["allow"].append("Bash(echo changed:*)")
        self.live_p.write_text(json.dumps(live, indent=4) + "\n")
        rc, _ = self.run_tool("apply", *self.args("--expect-live-sha", reviewed, "--expect-baseline-sha", base_sha))
        self.assertEqual(3, rc)

    def test_plan_refuses_a_baseline_that_does_not_cover_former_deny(self):
        weak = copy.deepcopy(BASELINE)
        weak["permissions"]["ask"] = [r for r in weak["permissions"]["ask"] if "versions access" not in r]
        live = json.loads(self.live_p.read_text())
        live["permissions"]["deny"] = []
        self.live_p.write_text(json.dumps(live))
        self.base_p.write_text(json.dumps(weak))
        rc, out = self.run_tool("plan", *self.args())
        self.assertEqual(2, rc)
        self.assertIn("former-deny command does not ask", out)

    def test_apply_refuses_a_symlinked_live_file(self):
        real = self.home / "real-settings.json"
        real.write_bytes(self.live_p.read_bytes())
        self.live_p.unlink()
        self.live_p.symlink_to(real)
        rc, out = self.run_tool("apply", *self.args("--expect-live-sha", rollout.sha(real), "--expect-baseline-sha", rollout.sha(self.base_p)))
        self.assertEqual(4, rc, out)
        self.assertTrue(self.live_p.is_symlink())

    def test_apply_leaves_no_temp_file_and_a_fixed_temp_name_is_not_used(self):
        (self.home / ".claude/settings.json.spe106.tmp").symlink_to(self.home / "victim.txt")
        rc, out = self.run_tool("apply", *self.args("--expect-live-sha", rollout.sha(self.live_p), "--expect-baseline-sha", rollout.sha(self.base_p)))
        self.assertEqual(0, rc, out)
        self.assertFalse((self.home / "victim.txt").exists(), "a planted symlink at a predictable temp name was followed")
        leftovers = [p.name for p in (self.home / ".claude").glob("settings.json.spe106.*") if p.name != "settings.json.spe106.tmp"]
        self.assertEqual([], leftovers)

    def test_rollback_saves_current_state_and_rejects_invalid_backup(self):
        bad = self.home / "bad-backup"
        bad.write_text("{not json")
        before = self.live_p.read_bytes()
        with self.assertRaises(ValueError):
            self.run_tool("rollback", "--live", str(self.live_p), "--backup", str(bad))
        self.assertEqual(before, self.live_p.read_bytes())
        good = self.home / "good-backup"
        good.write_text(json.dumps({"permissions": {"allow": [], "ask": [], "deny": []}}))
        rc, out = self.run_tool("rollback", "--live", str(self.live_p), "--backup", str(good))
        self.assertEqual(0, rc, out)
        saved = list((self.home / ".claude").glob("settings.json.pre-rollback.*"))
        self.assertEqual(1, len(saved))
        self.assertEqual(before, saved[0].read_bytes())

    def test_verify_fails_on_the_unmigrated_live_file(self):
        rc, out = self.run_tool("verify", "--live", str(self.live_p))
        self.assertEqual(1, rc)
        self.assertIn("permissions.deny is not empty", out)
        self.assertIn('lacks "$defaults"', out)


class MergedBaselineGate(Fixture):
    """The real live file may only receive a baseline that is committed and contained in origin/main."""

    def git(self, *a, cwd=None):
        import subprocess
        return subprocess.run(["git", *a], cwd=cwd or self.repo, capture_output=True, text=True, check=True)

    def setUp(self):
        super().setUp()
        self.repo = self.home / "repo"
        self.remote = self.home / "remote.git"
        self.remote.mkdir()
        self.git("init", "-q", "--bare", "-b", "main", cwd=self.remote)
        (self.repo / ".claude").mkdir(parents=True)
        self.git("init", "-q", "-b", "main")
        self.git("config", "user.email", "t@t")
        self.git("config", "user.name", "t")
        (self.repo / ".claude/settings.json").write_text(json.dumps(BASELINE))
        self.git("add", "-A")
        self.git("commit", "-qm", "base")
        self.git("remote", "add", "origin", str(self.remote))
        self._root = rollout.ROOT
        rollout.ROOT = self.repo.resolve()
        self.tracked = self.repo / ".claude/settings.json"

    def tearDown(self):
        rollout.ROOT = self._root
        super().tearDown()

    def apply(self, *extra):
        return self.run_tool("apply", "--baseline", str(self.tracked), "--live", str(self.live_p),
                             "--expect-live-sha", rollout.sha(self.live_p), "--expect-baseline-sha", rollout.sha(self.tracked), *extra)

    def test_a_baseline_outside_the_repo_is_refused(self):
        rc, out = self.run_tool("apply", "--baseline", str(self.base_p), "--live", str(self.live_p),
                                "--expect-live-sha", rollout.sha(self.live_p), "--expect-baseline-sha", rollout.sha(self.base_p))
        self.assertEqual(7, rc, out)
        self.assertIn("outside the repository", out)

    def test_committed_but_unmerged_is_refused_then_merged_is_accepted(self):
        before = self.live_p.read_bytes()
        self.git("push", "-q", "origin", "main")
        self.git("fetch", "-q", "origin", "main")
        (self.repo / "other.txt").write_text("x")
        self.git("add", "-A")
        self.git("commit", "-qm", "unmerged work")
        rc, out = self.apply()
        self.assertEqual(7, rc, out)
        self.assertIn("not contained in origin/main", out)
        self.assertEqual(before, self.live_p.read_bytes())
        self.git("push", "-q", "origin", "main")
        self.git("fetch", "-q", "origin", "main")
        rc, out = self.apply()
        self.assertEqual(0, rc, out)

    def test_uncommitted_baseline_edits_are_refused(self):
        self.git("push", "-q", "origin", "main")
        self.git("fetch", "-q", "origin", "main")
        edited = json.loads(self.tracked.read_text())
        edited["permissions"]["allow"].append("Bash(echo extra:*)")
        self.tracked.write_text(json.dumps(edited))
        rc, out = self.apply()
        self.assertEqual(7, rc, out)
        self.assertIn("uncommitted", out)

    def test_unmerged_ok_is_the_only_override(self):
        rc, out = self.apply("--unmerged-ok")
        self.assertEqual(0, rc, out)


class StaticGate(Fixture):
    def test_verify_with_a_baseline_flags_missing_ask_rules(self):
        merged = rollout.merge(copy.deepcopy(LIVE), copy.deepcopy(BASELINE), [])
        merged["permissions"]["ask"].remove("Bash(rtk proxy *)")
        self.live_p.write_text(json.dumps(merged))
        rc, out = self.run_tool("verify", "--live", str(self.live_p), "--baseline", str(self.base_p))
        self.assertEqual(1, rc)
        self.assertIn("baseline ask rules missing", out)

    def test_catch_all_allow_rules_are_flagged(self):
        merged = rollout.merge(copy.deepcopy(LIVE), copy.deepcopy(BASELINE), [])
        merged["permissions"]["allow"].append("Bash(*)")
        self.assertIn("catch-all allow", "\n".join(rollout.problems(merged)))


if __name__ == "__main__":
    unittest.main()
