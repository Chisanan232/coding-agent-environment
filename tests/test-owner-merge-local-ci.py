#!/usr/bin/env python3
"""Tests for scripts/owner-merge-local-ci.py (SPE-135) against disposable LOCAL git repositories.

The recorder runs the owner's configured argv with no shell and prints one OWNER-MERGE-LOCAL-CI line. Nothing leaves the machine,
no GitHub resource is touched and no real owner config or evidence directory is read or written.
"""
import contextlib
import importlib.util
import io
import json
import os
import pathlib
import re
import stat
import subprocess
import sys
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]


def load(name, rel):
    spec = importlib.util.spec_from_file_location(name, ROOT / rel)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


omg = load("omg", "scripts/owner-merge-gate.py")
rec = load("omlc", "scripts/owner-merge-local-ci.py")
ENV = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@example.invalid", GIT_COMMITTER_NAME="t",
           GIT_COMMITTER_EMAIL="t@example.invalid", GIT_CONFIG_GLOBAL="/dev/null", GIT_CONFIG_SYSTEM="/dev/null")
REPO = "Chisanan232/r"


def git(cwd, *a):
    return subprocess.run(["git", *a], cwd=cwd, env=ENV, capture_output=True, text=True, check=True).stdout.strip()


def py(code):
    return [sys.executable, "-c", code]


class Recorder(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = os.path.realpath(self.tmp.name)
        self.wt, self.logs = os.path.join(base, "wt"), os.path.join(base, "logs")
        os.mkdir(self.wt)
        git(self.wt, "init", "-q", "-b", "main")
        pathlib.Path(self.wt, "a.txt").write_text("a")
        git(self.wt, "add", "a.txt")
        git(self.wt, "commit", "-q", "-m", "base")
        git(self.wt, "update-ref", "refs/remotes/origin/main", "HEAD")
        self.base_sha = git(self.wt, "rev-parse", "HEAD")
        git(self.wt, "checkout", "-q", "-b", "feat")
        pathlib.Path(self.wt, "b.txt").write_text("b")
        git(self.wt, "add", "b.txt")
        git(self.wt, "commit", "-q", "-m", "change")
        self.head = git(self.wt, "rev-parse", "HEAD")
        self.marker = os.path.join(base, "marker")

    def tearDown(self):
        self.tmp.cleanup()

    def entry(self, *commands, env=None):
        return {"checks": [(f"c{i}", tuple(argv)) for i, argv in enumerate(commands)], "max_age_hours": 24, "pinned_paths": (), "not_covered": [],
                "env": env or {}}

    def record(self, *commands, expect_head=None, env=None):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = rec.record(REPO, 7, "main", self.wt, self.entry(*commands, env=env), self.logs, expect_head)
        return code, out.getvalue(), err.getvalue()

    def test_clean_tree_prints_one_unfenced_line_the_gate_accepts(self):
        code, out, _ = self.record(py("print('ok')"), py("import sys; sys.exit(0)"))
        self.assertEqual(code, 0)
        lines = out.splitlines()
        self.assertEqual(len(lines), 1)
        self.assertTrue(lines[0].startswith("OWNER-MERGE-LOCAL-CI {"))
        parsed = json.loads(lines[0].split(" ", 1)[1])
        omg.parse_local_record(parsed)
        self.assertEqual((parsed["repo"], parsed["pr"], parsed["base"], parsed["head"], parsed["base_sha"]),
                         (REPO, 7, "main", self.head, self.base_sha))
        self.assertEqual([c["exit_code"] for c in parsed["commands"]], [0, 0])
        self.assertEqual([c["name"] for c in parsed["commands"]], ["c0", "c1"])

    def test_logs_are_private_and_their_digest_is_recorded(self):
        _, out, _ = self.record(py("print('hello')"))
        cmd = json.loads(out.split(" ", 1)[1])["commands"][0]
        logs = [os.path.join(self.logs, n) for n in os.listdir(self.logs)]
        self.assertEqual(len(logs), 1)
        self.assertEqual(stat.S_IMODE(os.stat(logs[0]).st_mode), 0o600)
        import hashlib
        self.assertEqual(cmd["log_sha256"], hashlib.sha256(pathlib.Path(logs[0]).read_bytes()).hexdigest())
        self.assertIn(b"hello", pathlib.Path(logs[0]).read_bytes())

    def test_a_dirty_tree_refuses_before_running_anything(self):
        pathlib.Path(self.wt, "untracked.txt").write_text("x")
        code, out, err = self.record(py(f"open({self.marker!r}, 'w')"))
        self.assertNotEqual(code, 0)
        self.assertEqual(out, "")
        self.assertFalse(os.path.exists(self.marker))
        self.assertIn("not clean", err)

    def test_a_command_that_dirties_the_tree_gets_no_record(self):
        code, out, _ = self.record(py(f"open({os.path.join(self.wt, 'made.txt')!r}, 'w')"))
        self.assertNotEqual(code, 0)
        self.assertEqual(out, "")

    def test_a_head_other_than_the_expected_one_refuses(self):
        code, out, _ = self.record(py("pass"), expect_head="c" * 40)
        self.assertNotEqual(code, 0)
        self.assertEqual(out, "")
        self.assertEqual(self.record(py("pass"), expect_head=self.head)[0], 0)

    def test_failures_are_still_printed_and_later_commands_still_run(self):
        code, out, _ = self.record(py("import sys; sys.exit(3)"), py(f"open({self.marker!r}, 'w')"))
        self.assertEqual(code, 1)
        self.assertEqual([c["exit_code"] for c in json.loads(out.split(" ", 1)[1])["commands"]], [3, 0])
        self.assertTrue(os.path.exists(self.marker))

    def test_argv_is_never_given_to_a_shell(self):
        _, out, _ = self.record(py(f"import sys; print(sys.argv)") + [f"; touch {self.marker}", f"$(touch {self.marker})", "`id`"])
        self.assertFalse(os.path.exists(self.marker))
        self.assertEqual(json.loads(out.split(" ", 1)[1])["commands"][0]["exit_code"], 0)

    def test_secret_shaped_argv_refuses_and_nothing_runs(self):
        for bad in (["tool", "--token=abcdef"], ["tool", "password: hunter2"], ["tool", "ghp_" + "A" * 30], ["tool", "AKIA" + "A" * 16],
                    ["tool", "-----BEGIN PRIVATE KEY-----"]):
            with self.subTest(bad=bad[1][:10]):
                code, out, _ = self.record(py(f"open({self.marker!r}, 'w')"), bad)
                self.assertNotEqual(code, 0)
                self.assertEqual(out, "")
                self.assertFalse(os.path.exists(self.marker))

    def test_checks_run_with_a_scrubbed_environment_plus_the_owners_env(self):
        from unittest import mock
        dump = py("import os, sys; sys.stdout.write(','.join(sorted(os.environ)))")
        hostile = {"PYTHONPATH": "/x", "PYTHONHOME": "/x", "NODE_PATH": "/x", "VIRTUAL_ENV": "/x", "PYTHONSTARTUP": "/x", "PIP_INDEX_URL": "/x",
                   "LD_PRELOAD": "/x", "DYLD_INSERT_LIBRARIES": "/x", "SOME_TOKEN": "t"}
        with mock.patch.dict(os.environ, hostile):
            self.record(dump, env={"OWNER_FIXED": "1"})
        log = pathlib.Path(self.logs, os.listdir(self.logs)[0]).read_text().split(",")
        for key in hostile:
            self.assertNotIn(key, log)
        for key in ("PATH", "HOME", "OWNER_FIXED"):
            self.assertIn(key, log)

    def test_a_failure_is_still_printed_when_the_checks_also_dirtied_the_tree(self):
        made = os.path.join(self.wt, "made.txt")
        code, out, _ = self.record(py(f"open({made!r}, 'w'); raise SystemExit(4)"))
        self.assertEqual(code, 1)
        self.assertEqual(json.loads(out.split(" ", 1)[1])["commands"][0]["exit_code"], 4)

    def test_the_evidence_directory_is_made_private_and_log_files_are_checked(self):
        os.mkdir(self.logs, 0o755)
        self.assertEqual(self.record(py("pass"))[0], 0)
        self.assertEqual(stat.S_IMODE(os.stat(self.logs).st_mode), 0o700)
        name = os.listdir(self.logs)[0]
        os.chmod(os.path.join(self.logs, name), 0o644)
        code, out, _ = self.record(py(f"open({self.marker!r}, 'w')"))   # same head and check name: the existing log has another mode
        self.assertEqual((code, out), (2, ""))
        self.assertFalse(os.path.exists(self.marker))
        os.remove(os.path.join(self.logs, name))
        os.symlink(self.marker, os.path.join(self.logs, name))
        self.assertEqual(self.record(py("pass"))[0], 2)

    def test_the_directory_must_be_owned_by_the_caller_and_not_a_symlink(self):
        real = os.path.join(self.tmp.name, "elsewhere")
        os.mkdir(real, 0o700)
        os.symlink(real, self.logs)
        self.assertEqual(self.record(py("pass"))[0], 2)

    def test_ambient_git_variables_cannot_redirect_the_recorders_git_calls(self):
        from unittest import mock
        other = os.path.join(os.path.realpath(self.tmp.name), "b")
        os.mkdir(other)
        git(other, "init", "-q", "-b", "main")
        pathlib.Path(other, "dirty.txt").write_text("x")
        env = {"GIT_DIR": os.path.join(self.wt, ".git"), "GIT_WORK_TREE": self.wt, "GIT_INDEX_FILE": os.path.join(self.wt, ".git", "index")}
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.dict(os.environ, env), contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = rec.record(REPO, 7, "main", other, self.entry(py(f"open({self.marker!r}, 'w')")), self.logs)
        self.assertEqual((code, out.getvalue()), (2, ""))
        self.assertFalse(os.path.exists(self.marker))

    def test_the_worktree_must_be_the_top_of_its_repository(self):
        sub = os.path.join(self.wt, "sub")
        os.mkdir(sub)
        pathlib.Path(sub, "f.txt").write_text("x")
        git(self.wt, "add", "sub/f.txt")
        git(self.wt, "commit", "-q", "-m", "sub")
        code, out, err = self.record(py("pass"))
        self.assertEqual(code, 0)
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            self.assertEqual(rec.record(REPO, 7, "main", sub, self.entry(py("pass")), self.logs), 2)
        self.assertEqual(out.getvalue(), "")

    def test_the_owners_env_is_written_into_the_record(self):
        _, out, _ = self.record(py("pass"), env={"CI": "1"})
        parsed = json.loads(out.split(" ", 1)[1])
        self.assertEqual(parsed["env"], {"CI": "1"})
        omg.parse_local_record(parsed)

    def test_two_checks_sharing_a_log_name_are_refused(self):
        entry = {"checks": [("a b", ("true",)), ("a_b", ("true",))], "max_age_hours": 24, "pinned_paths": (), "not_covered": [], "env": {}}
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            self.assertEqual(rec.record(REPO, 7, "main", self.wt, entry, self.logs), 2)
        self.assertEqual(out.getvalue(), "")

    def test_a_config_outside_the_owner_config_root_is_refused(self):
        cfg = os.path.join(os.path.realpath(self.tmp.name), "local-first-ci.json")
        pathlib.Path(cfg).write_text(json.dumps({"schema": 1, "repos": {REPO: {"checks": [{"name": "c0", "argv": ["true"]}]}}}))
        os.chmod(cfg, 0o600)
        err = io.StringIO()
        with contextlib.redirect_stderr(err), contextlib.redirect_stdout(io.StringIO()) as out:
            code = rec.main(["record", "--repo", REPO, "--pr", "7", "--base", "main", "--worktree", self.wt, "--config", cfg])
        self.assertEqual((code, out.getvalue()), (2, ""))

    def test_a_repo_missing_from_the_trust_file_is_refused(self):
        cfg = os.path.join(os.path.realpath(self.tmp.name), "local")
        os.mkdir(cfg, 0o700)
        path = os.path.join(cfg, "local-first-ci.json")
        pathlib.Path(path).write_text(json.dumps({"schema": 1, "repos": {"Chisanan232/other": {"checks": [{"name": "c0", "argv": ["true"]}]}}}))
        os.chmod(path, 0o600)
        with self.assertRaises(ValueError):
            rec.load_entry(REPO, path, cfg)


if __name__ == "__main__":
    unittest.main()
