"""Black-box context CAS checks through installed host profile helpers."""
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
SHARED_SKILLS = (
    "engineering-runtime,post-merge-close,ticket-pickup-check,"
    "dev-impl-loop,workflow-resume"
)


class InstalledContextTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="context-install-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.home = self.root / "home"
        self.home.mkdir(mode=0o700)
        self.repo = self.root / "install-source"
        self.repo.mkdir()
        for name in (".claude", "codex", "scripts"):
            shutil.copytree(ROOT / name, self.repo / name)
        self.env = dict(os.environ, HOME=str(self.home), CODING_AGENT_SYNC_HOME=str(self.home))
        self.env.pop("CODEX_HOME", None)
        self.git_env = {key: value for key, value in os.environ.items()
                        if not key.startswith("GIT_")}
        self.git_env.update(GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM="1")

    def git(self, *args):
        return subprocess.run(["git", "-c", "core.hooksPath=/dev/null", *map(str, args)],
                              env=self.git_env, capture_output=True, text=True, check=True)

    def install(self, host):
        mode = "--global" if host == "codex" else "--claude-skills"
        result = subprocess.run(
            ["bash", str(self.repo / "scripts/profile-install.sh"), mode,
             "--shared-skills=" + SHARED_SKILLS],
            env=self.env, capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def make_worktree(self, host):
        seed = self.root / (host + "-seed")
        seed.mkdir()
        self.git("init", "-q", seed)
        self.git("-C", seed, "config", "user.name", "Context Install Test")
        self.git("-C", seed, "config", "user.email", "context-install@example.invalid")
        (seed / "README").write_text("isolated worktree fixture\n")
        self.git("-C", seed, "add", "README")
        self.git("-C", seed, "commit", "-qm", "fixture")
        worktree = self.root / (host + "-worktree")
        self.git("-C", seed, "worktree", "add", "-q", "-b", "fixture-" + host,
                 worktree, "HEAD")
        self.assertFalse((worktree / ("." + host)).exists())
        return worktree

    def run_bootstrap(self, host, worktree, **extra_env):
        runtime = self.home / ("." + host) / "skills/engineering-runtime/scripts"
        bootstrap = runtime / "bootstrap.py"
        env = dict(self.env, **extra_env)
        result = subprocess.run([sys.executable, str(bootstrap)], cwd=worktree, env=env,
                                capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)
        values = {}
        for line in result.stdout.splitlines():
            key, value = shlex.split(line, posix=True)[1].split("=", 1)
            values[key] = value
        self.assertEqual(Path(values["ENGINEERING_RUNTIME"]).resolve(), runtime.resolve())
        return values

    def run_context(self, helper, root, action, *args):
        return subprocess.run([sys.executable, str(helper), "context", str(root), action,
                               *map(str, args)], cwd=root.parent, env=self.env,
                              capture_output=True, text=True, timeout=15)

    def test_installed_both_host_context_bootstraps_enforce_cas_and_tombstones(self):
        for host in ("claude", "codex"):
            with self.subTest(host=host):
                self.install(host)
                installed_skill_root = self.home / ("." + host) / "skills"
                for skill in SHARED_SKILLS.split(","):
                    self.assertTrue((installed_skill_root / skill / "SKILL.md").is_file(), skill)
                runtime = installed_skill_root / "engineering-runtime/scripts"
                bootstrap = runtime / "bootstrap.py"
                helper = runtime / "ticket-state.py"
                self.assertTrue(helper.is_file())
                self.assertFalse((self.home / ("." + host) / "hooks/ticket-state.py").exists())
                worktree = self.make_worktree(host)
                context = worktree / ("." + host)
                self.assertFalse((context / "hooks/ticket-state.py").exists())

                initialized = self.run_context(
                    helper, context, "initialize", "--host", host, "--worktree", worktree,
                    "--ticket", "SPE-17", "--fresh-owned-scope")
                self.assertEqual(initialized.returncode, 0, initialized.stderr)
                active = json.loads(initialized.stdout)
                self.assertEqual(active["status"], "managed")
                self.assertEqual(active["state"], "active")

                active_env = self.run_bootstrap(
                    host, worktree,
                    ENGINEERING_CURRENT_TICKET="STALE-1",
                    ENGINEERING_CURRENT_WORKTREE="/stale/worktree",
                    ENGINEERING_CURRENT_RELEASE="stale-release")
                self.assertEqual(active_env["ENGINEERING_CONTEXT_STATUS"], "managed")
                self.assertEqual(active_env["ENGINEERING_CURRENT_TICKET"], "SPE-17")
                self.assertEqual(active_env["ENGINEERING_CURRENT_WORKTREE"], str(worktree.resolve()))
                self.assertEqual(active_env["ENGINEERING_CONTEXT_CAS_TOKEN"], active["token"])

                published = self.run_context(
                    helper, context, "publish", "--worktree", worktree, "--ticket", "SPE-17",
                    "--release", "phase-two",
                    "--expected-generation", active["generation"], "--expected-token", active["token"])
                self.assertEqual(published.returncode, 0, published.stderr)
                current = json.loads(published.stdout)
                self.assertEqual(current["ticket"], "SPE-17")
                self.assertEqual(current["release"], "phase-two")
                before_handover = (context / ".current-context.json").read_bytes()
                handover = self.run_context(
                    helper, context, "publish", "--worktree", worktree, "--ticket", "SPE-18",
                    "--expected-generation", current["generation"], "--expected-token", current["token"])
                self.assertNotEqual(handover.returncode, 0)
                self.assertEqual((context / ".current-context.json").read_bytes(), before_handover)
                before_stale_cas = (context / ".current-context.json").read_bytes()
                stale = self.run_context(
                    helper, context, "publish", "--worktree", worktree, "--ticket", "SPE-19",
                    "--expected-generation", active["generation"], "--expected-token", active["token"])
                self.assertNotEqual(stale.returncode, 0)
                self.assertEqual((context / ".current-context.json").read_bytes(), before_stale_cas)

                cleared = self.run_context(
                    helper, context, "clear", "--expected-generation", current["generation"],
                    "--expected-token", current["token"])
                self.assertEqual(cleared.returncode, 0, cleared.stderr)
                tombstone = json.loads(cleared.stdout)
                self.assertEqual(tombstone["state"], "cleared")
                markers = {
                    ".current-ticket": b"RAW-STALE-2\n",
                    ".current-worktree": b"/raw/stale/worktree\n",
                    ".current-release": b"raw-stale-release\n",
                }
                for name, content in markers.items():
                    (context / name).write_bytes(content)
                cleared_env = self.run_bootstrap(
                    host, worktree,
                    ENGINEERING_CURRENT_TICKET="ENV-STALE-3",
                    ENGINEERING_CURRENT_WORKTREE="/env/stale/worktree",
                    ENGINEERING_CURRENT_RELEASE="env-stale-release",
                    CLAUDE_CURRENT_TICKET="CLAUDE-STALE-4")
                self.assertEqual(cleared_env["ENGINEERING_CONTEXT_STATUS"], "managed")
                self.assertEqual(cleared_env["ENGINEERING_CONTEXT_GENERATION"], tombstone["generation"])
                for key in ("ENGINEERING_CURRENT_TICKET", "ENGINEERING_CURRENT_WORKTREE",
                            "ENGINEERING_CURRENT_RELEASE"):
                    self.assertEqual(cleared_env[key], "")
                for name, content in markers.items():
                    self.assertEqual((context / name).read_bytes(), content)
                self.assertFalse((context / "hooks/ticket-state.py").exists())


if __name__ == "__main__":
    unittest.main()
