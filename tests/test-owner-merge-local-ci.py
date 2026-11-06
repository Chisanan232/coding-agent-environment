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
from unittest import mock

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
        self.pr_info = {"headRefName": "feat/x", "headRefOid": self.head, "title": "feat: x"}
        self.gh_calls = []
        patcher = mock.patch.object(rec.gate, "gh", self.fake_gh)
        patcher.start()
        self.addCleanup(patcher.stop)

    def fake_gh(self, *args):
        self.gh_calls.append(args)
        if isinstance(self.pr_info, Exception):
            raise self.pr_info
        return json.dumps(self.pr_info)

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

    def record_from_cli(self, host_args=()):
        args = ["record", "--repo", REPO, "--pr", "7", "--base", "main", "--worktree", self.wt, *host_args]
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.dict(os.environ, {"HOME": self.tmp.name}), \
                mock.patch.object(rec, "load_entry", return_value=self.entry(py(f"open({self.marker!r}, 'w').write('ran')"))), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = rec.main(args)
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

    def test_evidence_host_defaults_to_existing_claude_location(self):
        code, out, _ = self.record_from_cli()
        self.assertEqual(code, 0)
        self.assertTrue(os.path.exists(self.marker))
        self.assertTrue(os.path.isdir(os.path.join(self.tmp.name, ".claude", "owner-merge-evidence")))
        self.assertFalse(os.path.exists(os.path.join(self.tmp.name, ".codex", "owner-merge-evidence")))
        self.assertTrue(out.startswith("OWNER-MERGE-LOCAL-CI {"))

    def test_explicit_codex_host_uses_only_codex_evidence_location(self):
        os.mkdir(os.path.join(self.tmp.name, ".codex"), 0o755)
        code, out, _ = self.record_from_cli(("--evidence-host", "codex"))
        self.assertEqual(code, 0)
        self.assertTrue(os.path.exists(self.marker))
        evidence = os.path.join(self.tmp.name, ".codex", "owner-merge-evidence")
        self.assertTrue(os.path.isdir(evidence))
        self.assertEqual(stat.S_IMODE(os.stat(evidence).st_mode), 0o700)
        self.assertFalse(os.path.exists(os.path.join(self.tmp.name, ".claude")))
        self.assertTrue(out.startswith("OWNER-MERGE-LOCAL-CI {"))

    def test_evidence_host_does_not_accept_an_arbitrary_path(self):
        out, err = io.StringIO(), io.StringIO()
        args = ["record", "--repo", REPO, "--pr", "7", "--base", "main", "--worktree", self.wt,
                "--evidence-host", self.logs]
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            with self.assertRaises(SystemExit) as raised:
                rec.main(args)
        self.assertEqual(raised.exception.code, 2)
        self.assertEqual(out.getvalue(), "")
        self.assertFalse(os.path.exists(self.marker))

    def test_codex_symlinked_host_root_refuses_without_fallback_or_execution(self):
        outside = os.path.join(self.tmp.name, "outside")
        os.mkdir(outside, 0o700)
        os.symlink(outside, os.path.join(self.tmp.name, ".codex"))
        os.makedirs(os.path.join(self.tmp.name, ".claude", "owner-merge-evidence"), mode=0o700)
        code, out, _ = self.record_from_cli(("--evidence-host", "codex"))
        self.assertEqual((code, out), (2, ""))
        self.assertFalse(os.path.exists(self.marker))
        self.assertEqual(os.listdir(os.path.join(self.tmp.name, ".claude", "owner-merge-evidence")), [])

    def test_codex_wrong_host_root_mode_refuses_without_execution(self):
        root = os.path.join(self.tmp.name, ".codex")
        os.mkdir(root, 0o777)
        os.chmod(root, 0o777)
        code, out, _ = self.record_from_cli(("--evidence-host", "codex"))
        self.assertEqual((code, out), (2, ""))
        self.assertFalse(os.path.exists(self.marker))

    def test_codex_wrong_host_root_owner_refuses_without_execution(self):
        os.mkdir(os.path.join(self.tmp.name, ".codex"), 0o755)
        with mock.patch.object(rec.os, "getuid", return_value=os.getuid() + 1):
            code, out, _ = self.record_from_cli(("--evidence-host", "codex"))
        self.assertEqual((code, out), (2, ""))
        self.assertFalse(os.path.exists(self.marker))

    def test_codex_symlinked_evidence_dir_refuses_without_execution(self):
        root = os.path.join(self.tmp.name, ".codex")
        os.mkdir(root, 0o755)
        outside = os.path.join(self.tmp.name, "outside")
        os.mkdir(outside, 0o700)
        os.symlink(outside, os.path.join(root, "owner-merge-evidence"))
        code, out, _ = self.record_from_cli(("--evidence-host", "codex"))
        self.assertEqual((code, out), (2, ""))
        self.assertFalse(os.path.exists(self.marker))
        self.assertEqual(os.listdir(outside), [])

    def test_codex_wrong_evidence_mode_refuses_without_execution(self):
        root = os.path.join(self.tmp.name, ".codex")
        os.mkdir(root, 0o755)
        evidence = os.path.join(root, "owner-merge-evidence")
        os.mkdir(evidence, 0o755)
        code, out, _ = self.record_from_cli(("--evidence-host", "codex"))
        self.assertEqual((code, out), (2, ""))
        self.assertFalse(os.path.exists(self.marker))

    def test_codex_existing_log_symlink_refuses_before_any_check(self):
        root = os.path.join(self.tmp.name, ".codex")
        os.mkdir(root, 0o755)
        evidence = os.path.join(root, "owner-merge-evidence")
        os.mkdir(evidence, 0o700)
        log = rec.log_path(evidence, "c0", self.head, f"{REPO.replace('/', '__')}-7")
        os.symlink(self.marker, log)
        code, out, _ = self.record_from_cli(("--evidence-host", "codex"))
        self.assertEqual((code, out), (2, ""))
        self.assertFalse(os.path.exists(self.marker))

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
                   "LD_PRELOAD": "/x", "DYLD_INSERT_LIBRARIES": "/x", "SOME_TOKEN": "t", "CARGO_HOME": "/x", "RUSTFLAGS": "-C link-arg=x",
                   "RUST_LOG": "trace"}
        with mock.patch.dict(os.environ, hostile):
            self.record(dump, env={"OWNER_FIXED": "1"})
        log = pathlib.Path(self.logs, os.listdir(self.logs)[0]).read_text().split(",")
        for key in hostile:
            self.assertNotIn(key, log)
        for key in ("PATH", "HOME", "OWNER_FIXED"):
            self.assertIn(key, log)

    def test_absent_resource_caps_keep_standalone_behavior(self):
        inherited = {key: value for key, value in os.environ.items()
                     if key not in ("CARGO_BUILD_JOBS", "RUST_TEST_THREADS", "NEXTEST_TEST_THREADS")}
        code = "import os; print(*(os.environ.get(k, '<unset>') for k in ('CARGO_BUILD_JOBS','RUST_TEST_THREADS','NEXTEST_TEST_THREADS')), sep='\\n')"
        with mock.patch.dict(os.environ, inherited, clear=True):
            result, _, err = self.record(py(code))
        self.assertEqual(result, 0)
        self.assertEqual(self.log_text().splitlines(), ["<unset>", "<unset>", "<unset>"])
        self.assertIn("none (standalone behavior)", err)

    def test_inherited_resource_caps_are_forwarded_without_broad_cargo_or_rust_environment(self):
        code = "import os; print(*(os.environ.get(k, '<unset>') for k in ('CARGO_BUILD_JOBS','RUST_TEST_THREADS','NEXTEST_TEST_THREADS','CARGO_HOME','RUSTFLAGS','RUST_LOG')), sep='\\n')"
        caps = {"CARGO_BUILD_JOBS": "3", "RUST_TEST_THREADS": "3", "NEXTEST_TEST_THREADS": "3"}
        with mock.patch.dict(os.environ, {**caps, "CARGO_HOME": "/private", "RUSTFLAGS": "-C link-arg=x", "RUST_LOG": "trace"}):
            result, out, err = self.record(py(code))
        self.assertEqual(result, 0)
        self.assertEqual(self.log_text().splitlines(), ["3", "3", "3", "<unset>", "<unset>", "<unset>"])
        self.assertIn("CARGO_BUILD_JOBS=3", err)
        self.assertIn("RUST_TEST_THREADS=3", err)
        self.assertIn("NEXTEST_TEST_THREADS=3", err)

    def test_configured_resource_limits_are_minimized_with_inherited_caps(self):
        code = "import os; print(*(os.environ.get(k, '<unset>') for k in ('CARGO_BUILD_JOBS','RUST_TEST_THREADS','NEXTEST_TEST_THREADS')), sep='\\n')"
        caps = {"CARGO_BUILD_JOBS": "8", "RUST_TEST_THREADS": "6", "NEXTEST_TEST_THREADS": "4"}
        configured = {"CARGO_BUILD_JOBS": "3", "RUST_TEST_THREADS": "9", "NEXTEST_TEST_THREADS": "2"}
        with mock.patch.dict(os.environ, caps):
            result, out, err = self.record(py(code), env=configured)
        self.assertEqual(result, 0)
        self.assertEqual(self.log_text().splitlines(), ["3", "6", "2"])
        self.assertEqual(json.loads(out.split(" ", 1)[1])["env"], configured)
        self.assertIn("CARGO_BUILD_JOBS=3", err)
        self.assertIn("RUST_TEST_THREADS=6", err)
        self.assertIn("NEXTEST_TEST_THREADS=2", err)

    def test_inherited_resource_caps_are_snapshotted_once_across_checks(self):
        code = "import os; print(os.environ.get('CARGO_BUILD_JOBS','<unset>'))"
        original = rec.run_check
        calls = []
        def change_after_first(*args, **kwargs):
            result = original(*args, **kwargs)
            calls.append(True)
            if len(calls) == 1:
                os.environ["CARGO_BUILD_JOBS"] = "1"
            return result
        with mock.patch.dict(os.environ, {"CARGO_BUILD_JOBS": "3"}), mock.patch.object(rec, "run_check", change_after_first):
            result, _, _ = self.record(py(code), py(code))
        self.assertEqual(result, 0)
        logs = [pathlib.Path(self.logs, name).read_text().strip() for name in sorted(os.listdir(self.logs))]
        self.assertEqual(logs, ["3", "3"])

    def test_invalid_inherited_caps_refuse_before_execution_and_log_truncation(self):
        bad_values = ("", "0", "-1", "+2", "1.5", "2x", " 2", "２", "1025", "9" * 100)
        for key in ("CARGO_BUILD_JOBS", "RUST_TEST_THREADS", "NEXTEST_TEST_THREADS"):
            for value in bad_values:
                with self.subTest(key=key, value=value):
                    os.makedirs(self.logs, mode=0o700, exist_ok=True)
                    path = rec.log_path(self.logs, "c0", self.head, f"{REPO.replace('/', '__')}-7")
                    pathlib.Path(path).write_bytes(b"preserve existing log")
                    os.chmod(path, 0o600)
                    with mock.patch.dict(os.environ, {key: value}):
                        code, out, _ = self.record(py(f"open({self.marker!r}, 'w')"))
                    self.assertEqual((code, out), (2, ""))
                    self.assertFalse(os.path.exists(self.marker))
                    self.assertEqual(pathlib.Path(path).read_bytes(), b"preserve existing log")

    def test_invalid_configured_resource_limits_refuse_before_execution_and_log_truncation(self):
        for key in ("CARGO_BUILD_JOBS", "RUST_TEST_THREADS", "NEXTEST_TEST_THREADS"):
            for value in ("", "0", "-2", "+3", " 3", "3x", "３", "1025", "9" * 100):
                with self.subTest(key=key, value=value):
                    os.makedirs(self.logs, mode=0o700, exist_ok=True)
                    path = rec.log_path(self.logs, "c0", self.head, f"{REPO.replace('/', '__')}-7")
                    pathlib.Path(path).write_bytes(b"preserve existing log")
                    os.chmod(path, 0o600)
                    code, out, _ = self.record(py(f"open({self.marker!r}, 'w')"), env={key: value})
                    self.assertEqual((code, out), (2, ""))
                    self.assertFalse(os.path.exists(self.marker))
                    self.assertEqual(pathlib.Path(path).read_bytes(), b"preserve existing log")

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
        self.pr_info["headRefOid"] = git(self.wt, "rev-parse", "HEAD")
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

    def log_text(self):
        return pathlib.Path(self.logs, os.listdir(self.logs)[0]).read_text()

    def test_checks_get_the_hosted_context_and_the_record_carries_it(self):
        dump = py("import os; print(*(os.environ.get(k, '<unset>') for k in ('PULL_BRANCH', 'PR_TITLE', 'GIT_BASE', 'CARGO_TARGET_DIR')), sep='\\n')")
        code, out, err = self.record(dump)
        self.assertEqual(code, 0)
        self.assertEqual(self.log_text().split("\n")[:4], ["feat/x", "feat: x", self.base_sha, os.path.join(self.wt, "target")])
        parsed = json.loads(out.split(" ", 1)[1])
        self.assertEqual(parsed["context"], {"PULL_BRANCH": "feat/x", "PR_TITLE": "feat: x", "GIT_BASE": self.base_sha})
        omg.parse_local_record(parsed)
        self.assertEqual(self.gh_calls, [("pr", "view", "7", "--repo", REPO, "--json", "headRefName,headRefOid,title")])
        self.assertIn("CARGO_TARGET_DIR", err)
        self.assertNotIn("feat: x", err)   # names only

    def test_ambient_context_variables_are_not_forwarded(self):
        dump = py("import os; print(*(os.environ.get(k, '<unset>') for k in ('PULL_BRANCH', 'PR_TITLE', 'GIT_BASE', 'CARGO_TARGET_DIR')), sep='\\n')")
        ambient = {"PULL_BRANCH": "evil", "PR_TITLE": "evil", "GIT_BASE": "evil", "CARGO_TARGET_DIR": "/evil"}
        with mock.patch.dict(os.environ, ambient):
            self.assertEqual(self.record(dump)[0], 0)
        text = self.log_text()
        self.assertNotIn("evil", text)
        self.assertIn("feat/x", text)

    def test_a_pr_head_other_than_the_worktree_head_refuses(self):
        self.pr_info = dict(self.pr_info, headRefOid="c" * 40)
        code, out, err = self.record(py(f"open({self.marker!r}, 'w')"))
        self.assertEqual((code, out), (2, ""))
        self.assertIn("not the worktree HEAD", err)
        self.assertFalse(os.path.exists(self.marker))

    def test_a_gh_failure_refuses_before_any_check(self):
        self.pr_info = RuntimeError("gh pr view failed: boom")
        code, out, err = self.record(py(f"open({self.marker!r}, 'w')"))
        self.assertEqual((code, out), (2, ""))
        self.assertIn("cannot read the PR", err)
        self.assertFalse(os.path.exists(self.marker))

    def test_malformed_pr_fields_refuse(self):
        good = dict(self.pr_info)
        for label, bad in (("ref space", {"headRefName": "a b"}), ("ref empty", {"headRefName": ""}), ("ref long", {"headRefName": "a" * 201}),
                           ("ref type", {"headRefName": 5}), ("title empty", {"title": ""}), ("title long", {"title": "t" * 501}),
                           ("title newline", {"title": "a\nb"}), ("title control", {"title": "a\x1bb"}), ("title type", {"title": None}),
                           ("missing ref", {"headRefName": None})):
            with self.subTest(label):
                self.pr_info = dict(good, **bad)
                code, out, _ = self.record(py(f"open({self.marker!r}, 'w')"))
                self.assertEqual((code, out), (2, ""))
                self.assertFalse(os.path.exists(self.marker))
        self.pr_info = ["not", "a", "dict"]
        self.assertEqual(self.record(py("pass"))[0], 2)


if __name__ == "__main__":
    unittest.main()
