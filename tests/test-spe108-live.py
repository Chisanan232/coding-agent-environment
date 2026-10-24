#!/usr/bin/env python3
"""End-to-end test of the SPE-108 operator scripts in a synthetic HOME (never the real one).

Order under test: install (no overlay) -> build overlay -> install -> retire legacy sections -> drift.
"""
import hashlib
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
LIVE = ROOT / "scripts/spe108-live"
sys.path.insert(0, str(LIVE))
import ledger as L  # noqa: E402

RETIRE_TITLES = ("Commit Policy", "Pull Request Policy", "Git Worktree Workflow", "Delegation Model")

LEGACY = (
    "# title\n\n## Commit Policy\n\n1. old rule\n\n---\n\n## Pull Request Policy\nx\n\n## Git Worktree Workflow\nx\n\n"
    "## Delegation Model\nx\n\n## Zed Private Rule\n\n12. only in the live file\nplain bullet only here\n\n---\n\n"
    "## CodeGraph\nlive line extra\n"
    "<!-- coding-agent-environment:engineering-workflow:start -->\n## Merge Policy\nOLD merge text\n\n"
    "## Decision Delegation\nold\n<!-- coding-agent-environment:engineering-workflow:end -->\n"
)


INHERITED_LOCATION_OVERRIDES = ("CODING_AGENT_LOCAL_DIR", "CODING_AGENT_LOCAL_BASELINE_DIR", "CODING_AGENT_SYNC_HOME",
                                "CODEX_HOME", "INSTALL_BIN_DIR", "CODING_AGENT_PROFILE_DIR")


class LiveScripts(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        base = Path(self.temp.name).resolve()  # macOS /var is a symlink and the script refuses symlinked parents
        self.home = base / "home"
        (self.home / ".claude").mkdir(parents=True)
        self.live = self.home / ".claude/CLAUDE.md"
        self.live.write_text(LEGACY)
        self.repo = base / "repo"
        subprocess.run(["git", "clone", "-q", "--local", str(ROOT), str(self.repo)], check=True)
        # The clone's origin/main must equal HEAD (the script's freshness rule); mirror the working tree in.
        subprocess.run(["rsync", "-a", "--exclude", ".git", f"{ROOT}/", f"{self.repo}/"], check=True)
        env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t",
               "GIT_COMMITTER_EMAIL": "t@t"}
        subprocess.run(["git", "-C", str(self.repo), "add", "-A"], check=True, env=env)
        subprocess.run(["git", "-C", str(self.repo), "commit", "-q", "-m", "snapshot", "--allow-empty"], check=True, env=env)
        subprocess.run(["git", "-C", str(self.repo), "update-ref", "refs/remotes/origin/main", "HEAD"], check=True)
        # An inherited install or overlay location would make the scripts write the operator's real files.
        self.env = {k: v for k, v in os.environ.items() if k not in INHERITED_LOCATION_OVERRIDES}
        self.env["HOME"] = str(self.home)

    def run_script(self, *args, ok=True):
        done = subprocess.run(list(args), capture_output=True, text=True, env=self.env, cwd=self.repo)
        if ok:
            self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        return done

    def install(self):
        return self.run_script("bash", "scripts/profile-install.sh", "--claude-skills")

    def legacy_lines(self, text):
        """Content lines inside the four legacy sections of a synthetic live file."""
        out, inside = [], False
        for line in text.splitlines():
            if line.startswith("## "):
                inside = line[3:].strip() in RETIRE_TITLES
            if inside and not L.is_structural(line):
                out.append(line)
        return out

    def write_ledger(self, text, overrides=None, drop=()):
        """Synthetic ledger: every legacy line SUPERSEDED unless overridden; `drop` leaves a line unledgered."""
        overrides = overrides or {}
        path = Path(self.temp.name) / "ledger.tsv"
        rows = {}
        for line in self.legacy_lines(text):
            if line in drop:
                continue
            rows[L.line_key(line)] = (overrides.get(line, L.SUPERSEDED), "README.md:1", None)
        path.write_text("".join("\t".join((k, d, c)) + "\n" for k, (d, c, _) in sorted(rows.items())))
        path.chmod(0o600)
        return path

    def test_install_build_install_retire_leaves_one_copy_of_each_policy(self):
        retire = str(self.repo / "scripts/spe108-live/retire-sections.py")
        build = str(self.repo / "scripts/spe108-live/build-overlay.py")
        refused = self.run_script(sys.executable, retire, ok=False)
        self.assertNotEqual(refused.returncode, 0)  # no managed span with the replacements yet
        self.install()
        self.run_script(sys.executable, build, "--repo", str(self.repo))
        overlay = self.home / ".config/coding-agent-environment/local/baseline/claude/CLAUDE.private.md"
        self.assertEqual(overlay.stat().st_mode & 0o777, 0o600)
        same = self.run_script(sys.executable, build, "--repo", str(self.repo))
        self.assertIn("unchanged", same.stdout)  # idempotent: identical output is not an error
        original = overlay.read_bytes()
        overlay.write_bytes(original + b"operator edit\n")  # a differing overlay is never overwritten
        again = self.run_script(sys.executable, build, "--repo", str(self.repo), ok=False)
        self.assertIn("already exists", again.stderr + again.stdout)
        overlay.write_bytes(original)
        self.install()
        text = self.live.read_text()
        self.assertEqual(text.count("\n## Merge Policy\n"), 1)
        self.assertNotIn("OLD merge text", text)
        digest = hashlib.sha256(self.live.read_bytes()).hexdigest()
        mode_before = self.live.stat().st_mode & 0o777
        dry = self.run_script(sys.executable, retire, "--repo", str(self.repo))
        self.assertIn("legacy sections 4", dry.stdout)
        self.assertEqual(self.live.read_text(), text)  # dry run changes nothing
        unledgered = self.run_script(sys.executable, retire, "--apply", "--expect-sha256", digest, ok=False)
        self.assertIn("--ledger and --repo", unledgered.stdout + unledgered.stderr)
        ledger = self.write_ledger(text)
        args = ("--ledger", str(ledger), "--repo", str(self.repo))
        wrong = self.run_script(sys.executable, retire, "--apply", "--expect-sha256", "0" * 64, *args, ok=False)
        self.assertNotEqual(wrong.returncode, 0)
        self.run_script(sys.executable, retire, "--apply", "--expect-sha256", digest, *args)
        after = self.live.read_text()
        for gone in ("## Commit Policy", "## Pull Request Policy", "## Git Worktree Workflow"):
            self.assertNotIn(gone, after)
        self.assertEqual(after.count("\n## Delegation Model\n"), 1)  # only the managed span's copy remains
        self.assertIn("12. only in the live file", after)  # live-only rule survived outside the legacy blocks
        self.assertIn("live line extra", overlay.read_text())
        self.assertEqual(self.live.stat().st_mode & 0o777, mode_before)
        backups = list((self.home / ".claude/backups").glob("spe-108-retire-*/CLAUDE.md"))
        self.assertEqual(len(backups), 1)
        self.assertEqual(hashlib.sha256(backups[0].read_bytes()).hexdigest(), digest)
        self.assertEqual(backups[0].stat().st_mode & 0o777, 0o600)
        second = self.run_script(sys.executable, retire, ok=False)
        self.assertNotEqual(second.returncode, 0)  # nothing left to retire
        again = self.run_script(sys.executable, retire, *args)  # idempotent once a ledger vouches for it
        self.assertIn("already retired", again.stdout)
        self.assertEqual(self.live.read_text(), after)

    def test_script_env_never_inherits_an_overlay_location(self):
        with mock.patch.dict(os.environ, {"CODING_AGENT_LOCAL_DIR": "/nonexistent/real-overlay",
                                          "CODING_AGENT_LOCAL_BASELINE_DIR": "/nonexistent/real-baseline"}):
            self.setUp()
        self.assertNotIn("CODING_AGENT_LOCAL_DIR", self.env)
        self.assertNotIn("CODING_AGENT_LOCAL_BASELINE_DIR", self.env)

    def test_script_env_never_inherits_an_install_target_override(self):
        names = ("CODING_AGENT_SYNC_HOME", "CODEX_HOME", "INSTALL_BIN_DIR", "CODING_AGENT_PROFILE_DIR")
        with mock.patch.dict(os.environ, {name: "/nonexistent/real-" + name for name in names}):
            self.setUp()
        for name in names:
            self.assertNotIn(name, self.env)

    def test_retire_refuses_a_marker_inside_a_legacy_section(self):
        self.install()
        with self.live.open("a") as handle:
            handle.write("\n## Commit Policy\n<!-- coding-agent-environment:private-overlay:start -->\nx\n")
        done = self.run_script(sys.executable, str(self.repo / "scripts/spe108-live/retire-sections.py"), ok=False)
        self.assertNotEqual(done.returncode, 0)


    def prepare_installed(self):
        self.install()
        self.run_script(sys.executable, str(LIVE / "build-overlay.py"), "--repo", str(self.repo))
        self.install()
        return self.live.read_text()

    def retire(self, *extra, ok=True):
        return self.run_script(sys.executable, str(LIVE / "retire-sections.py"), "--repo", str(self.repo), *extra, ok=ok)

    def test_retire_refuses_unledgered_lines_and_open_conflicts_without_changing_the_file(self):
        text = self.prepare_installed()
        digest = hashlib.sha256(self.live.read_bytes()).hexdigest()
        missing = self.write_ledger(text, drop=("1. old rule",))
        done = self.retire("--ledger", str(missing), "--apply", "--expect-sha256", digest, ok=False)
        self.assertIn("unledgered", done.stdout)
        self.assertEqual(self.live.read_text(), text)
        conflict = self.write_ledger(text, overrides={"1. old rule": L.CONFLICT})
        done = self.retire("--ledger", str(conflict), "--apply", "--expect-sha256", digest, ok=False)
        self.assertIn("open conflict", done.stdout)
        self.assertEqual(self.live.read_text(), text)
        self.assertFalse(list((self.home / ".claude/backups").glob("spe-108-retire-*")))

    def test_retire_requires_a_preserved_overlay_line_to_exist_elsewhere(self):
        text = self.prepare_installed()
        digest = hashlib.sha256(self.live.read_bytes()).hexdigest()
        unique = self.write_ledger(text, overrides={"1. old rule": L.OVERLAY})
        done = self.retire("--ledger", str(unique), "--apply", "--expect-sha256", digest, ok=False)
        self.assertIn("overlay copy missing", done.stdout)
        self.assertEqual(self.live.read_text(), text)

    def test_ledger_with_retained_lines_builds_a_lossless_overlay_and_retire_passes(self):
        self.install()
        text = self.live.read_text()
        ledger = self.write_ledger(text, overrides={"1. old rule": L.OVERLAY})
        build = str(LIVE / "build-overlay.py")
        self.run_script(sys.executable, build, "--repo", str(self.repo), "--ledger", str(ledger))
        overlay = self.home / ".config/coding-agent-environment/local/baseline/claude/CLAUDE.private.md"
        self.assertIn("### Retained from: Commit Policy", overlay.read_text())
        self.assertIn("1. old rule", overlay.read_text())
        same = self.run_script(sys.executable, build, "--repo", str(self.repo), "--ledger", str(ledger))
        self.assertIn("unchanged", same.stdout)  # identical output is a no-op, not a refusal
        self.install()
        digest = hashlib.sha256(self.live.read_bytes()).hexdigest()
        dry = self.retire("--ledger", str(ledger))
        self.assertIn("lossless gate: PASS", dry.stdout)
        self.assertEqual(dry.stdout, self.retire("--ledger", str(ledger)).stdout)  # deterministic dry run
        self.retire("--ledger", str(ledger), "--apply", "--expect-sha256", digest)
        self.assertIn("1. old rule", self.live.read_text())  # still present, now only via the overlay

    def test_ledger_must_be_private_hash_only_and_well_formed(self):
        path = Path(self.temp.name) / "bad.tsv"
        path.write_text("L:0123456789ab\tSUPERSEDED_BY_APPROVED_RULE\tREADME.md:1\n")
        path.chmod(0o644)
        with self.assertRaises(L.LedgerError):
            L.load(path)
        path.chmod(0o600)
        self.assertEqual(len(L.load(path)), 1)
        for row in ("not-a-key\tSUPERSEDED_BY_APPROVED_RULE\tREADME.md:1\n",
                    "L:0123456789ab\tMADE_UP\tREADME.md:1\n",
                    "L:0123456789ab\tSUPERSEDED_BY_APPROVED_RULE\tfree text citation\n",
                    "L:0123456789ab\tSEMANTICALLY_PRESERVED\tREADME.md:1\treplacement\n"):
            path.write_text(row)
            with self.assertRaises(L.LedgerError):
                L.load(path)

    def test_rewrite_lines_is_pinned_exact_idempotent_and_rolls_back_byte_exact(self):
        target = self.home / "doc.md"
        target.write_text("keep one\n  old format line\nkeep two\n")
        ledger = Path(self.temp.name) / "rewrite.tsv"
        ledger.write_text("%s\tSUPERSEDED_BY_APPROVED_RULE\tREADME.md:1\tnew canonical line\n" % L.line_key("old format line"))
        ledger.chmod(0o600)
        script = str(LIVE / "rewrite-lines.py")
        before = target.read_bytes()
        digest = hashlib.sha256(before).hexdigest()
        base = (sys.executable, script, "--file", str(target), "--ledger", str(ledger))
        dry = self.run_script(*base, "--expect-sha256", digest)
        self.assertIn("lines to rewrite: 1", dry.stdout)
        self.assertEqual(dry.stdout, self.run_script(*base, "--expect-sha256", digest).stdout)
        self.assertEqual(target.read_bytes(), before)
        refused = self.run_script(*base, "--expect-sha256", "0" * 64, ok=False)
        self.assertNotEqual(refused.returncode, 0)
        self.run_script(*base, "--expect-sha256", digest, "--apply")
        self.assertEqual(target.read_text(), "keep one\n  new canonical line\nkeep two\n")  # indent kept, others untouched
        backup = next(self.home.glob("backups/spe-108-rewrite-*/doc.md"))
        self.assertEqual(backup.read_bytes(), before)
        self.assertEqual(backup.stat().st_mode & 0o777, 0o600)
        now = hashlib.sha256(target.read_bytes()).hexdigest()
        second = self.run_script(*base, "--expect-sha256", now, "--apply")
        self.assertIn("lines to rewrite: 0", second.stdout)
        self.assertEqual(hashlib.sha256(target.read_bytes()).hexdigest(), now)
        self.assertEqual(len(list(self.home.glob("backups/spe-108-rewrite-*"))), 1)  # no-op took no backup

    def test_verify_effective_inspects_the_final_file_not_counts(self):
        before = self.home / "before.md"
        before.write_text("# t\nkeep rule\nold <emoji> <scope>: format\ncited rule\n")
        after = self.home / "after.md"
        skills = self.home / "skills/engineering-workflow"
        skills.mkdir(parents=True)
        (skills / "SKILL.md").write_text("\n".join((
            "<version-or-phase>/<ticket-id>/<type>/<short_summary>", "<repo>-<phase>-<ticket>-<type>-<summary>",
            "<GitEmoji> (<scope>): <imperative summary>", "[<TICKET>] <GitEmoji> (<scope>): <imperative summary under 60 chars>",
            "independent review", "at most seven concurrent subagents", "never push directly")) + "\n")
        ledger = Path(self.temp.name) / "v.tsv"
        rows = [(L.line_key("old <emoji> <scope>: format"), L.SUPERSEDED), (L.line_key("cited rule"), L.SEMANTIC)]
        ledger.write_text("".join("%s\t%s\tREADME.md:1\n" % row for row in rows))
        ledger.chmod(0o600)
        main = ("# t\nkeep rule\nsee skills/engineering-workflow/SKILL.md\n## Resource Admission\nrelease owner\n"
                "@rules/secret-handling.md\n")
        after.write_text(main)
        args = (sys.executable, str(LIVE / "verify-effective.py"), "--host", "claude", "--before", str(before), "--after", str(after),
                "--skills-dir", str(self.home / "skills"), "--ledger", str(ledger), "--repo", str(self.repo))
        ok = self.run_script(*args)
        self.assertNotIn("FAIL", ok.stdout)
        self.assertEqual(ok.stdout, self.run_script(*args).stdout)  # deterministic
        after.write_text(main + "still the old <emoji> <scope>: form\n")  # a superseded form left active
        bad = self.run_script(*args, ok=False)
        self.assertIn("FAIL forbidden-legacy-commit-or-pr-format main:7", bad.stdout)
        after.write_text(main.replace("keep rule\n", ""))  # a valid founder line silently dropped
        lost = self.run_script(*args, ok=False)
        self.assertIn("FAIL lossless-unaccounted-line before:2", lost.stdout)
        (skills / "SKILL.md").write_text("nothing canonical\n")
        missing = self.run_script(*args, ok=False)
        self.assertIn("FAIL required-commit-format skill:-", missing.stdout)

    def test_clause_drift_counts_dispositioned_clauses_and_fails_on_open_conflict(self):
        drift = str(ROOT / "scripts/claude-clause-drift.py")
        self.live.write_text("# t\n\n## Zed Rule\n\n12. only in the live file\n")
        self.env["CODING_AGENT_SYNC_HOME"] = str(self.home)
        base = (sys.executable, drift, "--home", str(self.home))
        bare = self.run_script(*base, ok=False)
        self.assertIn("UNTRACKED", bare.stdout)
        ledger = Path(self.temp.name) / "d.tsv"
        rows = [(L.line_key("# t"), L.SEMANTIC), (L.line_key("## Zed Rule"), L.SUPERSEDED),
                (L.line_key("12. only in the live file"), L.SEMANTIC)]
        ledger.write_text("".join("%s\t%s\tREADME.md:1\n" % row for row in rows))
        ledger.chmod(0o600)
        covered = self.run_script(*base, "--ledger", str(ledger))
        self.assertIn("Claude clause drift: 0", covered.stdout)
        ledger.write_text("%s\t%s\tREADME.md:1\n" % (L.line_key("12. only in the live file"), L.CONFLICT))
        open_conflict = self.run_script(*base, "--ledger", str(ledger), ok=False)
        self.assertIn("OPEN CONFLICT live line 5", open_conflict.stdout)


    def test_ledger_rejects_replacement_on_conflict_escaping_citation_and_text_fence(self):
        path = Path(self.temp.name) / "l.tsv"
        path.write_text("L:0123456789ab\tREAL_CONFLICT_REQUIRING_OWNER_DECISION\tREADME.md:1\tnew\n")
        path.chmod(0o600)
        with self.assertRaises(L.LedgerError):  # a tool must never resolve an owner decision by rewriting
            L.load(path)
        for citation in ("/etc/hosts:1", "../outside.md:1", "README.md:999999", "README.md:0"):
            path.write_text("L:0123456789ab\tSEMANTICALLY_PRESERVED\t%s\n" % citation)
            self.assertEqual(L.check_citations(L.load(path), ROOT), ["L:0123456789ab"], citation)
        self.assertTrue(L.is_structural("```"))
        self.assertTrue(L.is_structural("```bash"))
        self.assertFalse(L.is_structural("```bash Never push to master"))  # trailing words are a rule, not a fence
        self.assertEqual(L.split_lines("a\x0cb\nc\n"), ["a\x0cb", "c"])  # newline-only, like an editor

    def test_rewrite_lines_skips_marker_regions_open_conflicts_and_odd_line_breaks(self):
        target = self.home / "doc.md"
        old = "old format line"
        target.write_text("%s\n<!-- coding-agent-environment:private-overlay:start -->\n%s\n"
                          "<!-- coding-agent-environment:private-overlay:end -->\nA\x0c%s\n" % (old, old, old))
        ledger = Path(self.temp.name) / "r.tsv"
        ledger.write_text("%s\tSUPERSEDED_BY_APPROVED_RULE\tREADME.md:1\tnew canonical\n" % L.line_key(old))
        ledger.chmod(0o600)
        digest = hashlib.sha256(target.read_bytes()).hexdigest()
        self.run_script(sys.executable, str(LIVE / "rewrite-lines.py"), "--file", str(target), "--ledger", str(ledger),
                        "--expect-sha256", digest, "--apply")
        # Only the plain line changed: not the overlay copy, and not a text that merely contains a form feed.
        self.assertEqual(target.read_text().splitlines(keepends=False)[0], "new canonical")
        self.assertEqual(target.read_text().count(old), 2)

    def verify_args(self, before, after, skills, ledger, *extra):
        return (sys.executable, str(LIVE / "verify-effective.py"), "--host", "claude", "--before", str(before), "--after",
                str(after), "--skills-dir", str(skills), "--ledger", str(ledger), "--repo", str(self.repo), *extra)

    def test_verify_effective_flags_a_superseded_line_left_active_and_an_overlay_loss(self):
        skills = self.home / "skills"
        (skills / "engineering-workflow").mkdir(parents=True)
        (skills / "engineering-workflow/SKILL.md").write_text("x\n")
        before, after = self.home / "b.md", self.home / "a.md"
        before.write_text("keep\nold commit form\n<!-- coding-agent-environment:private-overlay:start -->\nonly in overlay\n"
                          "<!-- coding-agent-environment:private-overlay:end -->\n")
        after.write_text("keep\nold commit form\n")  # the overlay block vanished and the old form stayed
        ledger = Path(self.temp.name) / "s.tsv"
        ledger.write_text("%s\tSUPERSEDED_BY_APPROVED_RULE\tREADME.md:1\tnew commit form\n" % L.line_key("old commit form"))
        ledger.chmod(0o600)
        done = self.run_script(*self.verify_args(before, after, skills, ledger), ok=False)
        self.assertIn("FAIL superseded-line-still-active after:2", done.stdout)
        self.assertRegex(done.stdout, r"FAIL lossless-unaccounted-line before:[\d,]*4")  # overlay text is founder text, not installer-owned
        plain = Path(self.temp.name) / "p.tsv"
        plain.write_text("%s\tSUPERSEDED_BY_APPROVED_RULE\tREADME.md:1\n" % L.line_key("old commit form"))
        plain.chmod(0o600)
        after.write_text("keep\nold commit form\nonly in overlay\n")
        relaxed = self.run_script(*self.verify_args(before, after, skills, plain), ok=False)
        self.assertNotIn("FAIL superseded", relaxed.stdout)  # tolerated while the legacy section is kept
        strict = self.run_script(*self.verify_args(before, after, skills, plain, "--strict"), ok=False)
        self.assertIn("FAIL superseded-line-still-active", strict.stdout)

    def test_rehearse_refuses_a_scratch_dir_inside_the_real_config_or_checkout(self):
        ledger = Path(self.temp.name) / "e.tsv"
        ledger.write_text("")
        ledger.chmod(0o600)
        script = str(LIVE / "rehearse.sh")
        for inside in (self.home / ".claude/scratch", self.repo / "scratch"):
            done = self.run_script("bash", script, "--repo", str(self.repo), "--ledger", str(ledger), "--keep-dir", str(inside), ok=False)
            self.assertEqual(done.returncode, 2, done.stdout + done.stderr)
            self.assertIn("must not be inside", done.stderr)
            self.assertFalse(inside.exists() and any(inside.iterdir()))


if __name__ == "__main__":
    unittest.main()
