#!/usr/bin/env python3
"""Offline tests for the INSTALLED mode of scripts/qa-side.py (SPE-153, ADR 0016 PR 2).

Nothing here is privileged: the "root-owned" tree is a scratch directory owned by the current user (the root-uid constant is
substituted), the QA uid is the system `nobody` account whose uid is only compared, never assumed, and every process-control
primitive is replaced by a tripwire. The attended hop is covered by tests/test-qa-handoff.py and is not exercised here."""
import contextlib
import hashlib
import importlib.util
import io
import json
import os
import pwd
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
SIDE = SCRIPTS / "qa-side.py"
spec = importlib.util.spec_from_file_location("qa_side_under_test", SIDE)
side = importlib.util.module_from_spec(spec)
spec.loader.exec_module(side)
qis_spec = importlib.util.spec_from_file_location("qis_for_side_test", SCRIPTS / "qa-identity-setup.py")
qis = importlib.util.module_from_spec(qis_spec)
qis_spec.loader.exec_module(qis)

ME = os.getuid()
QA = pwd.getpwnam("nobody")
PRODUCER_VAR = "SU" + "DO_UID"
ATTEMPT = "fornax-FORNX-1-run-ab12cd34"
SENTINEL = "qa-sentinel-" + "ab" * 16
SHA = "c" * 64
POISON = {"PATH": "/tmp/evil:/opt/poison", "HOME": "/tmp/evil-home", "CODEX_HOME": "/tmp/evil-codex", "LANG": "xx_XX.POISON",
          "TERM": "poison-term", "SHELL": "/tmp/evil-shell", "TMPDIR": "/tmp/evil-tmp", "CARGO_HOME": "/tmp/evil-cargo",
          "XDG_CONFIG_HOME": "/tmp/evil-xdg"}


def forbidden(*_a, **_k):
    raise AssertionError("a process-control primitive was called")


class Tree:
    """A scratch install: <base>/cfg/qa-side.json, <base>/codex/codex and an exchange <base>/X with inbox/RUN.lock."""

    def __init__(self, request=None, verb="exec"):
        self.base = Path(os.path.realpath(tempfile.mkdtemp(prefix="qa-side-test-")))
        self.verb = verb
        os.chmod(self.base, 0o755)
        self.cfg_dir, self.codex_dir, self.x = self.base / "cfg", self.base / "codex", self.base / "X"
        for d in (self.cfg_dir, self.codex_dir):
            d.mkdir(mode=0o755)
            os.chmod(d, 0o755)
        self.codex = self.codex_dir / "codex"
        self.codex.write_bytes(b"#!/bin/sh\nexit 0\n")
        os.chmod(self.codex, 0o555)
        self.digest = hashlib.sha256(self.codex.read_bytes()).hexdigest()
        self.x.mkdir()
        os.chmod(self.x, 0o711)
        self.inbox = self.x / "inbox"
        self.inbox.mkdir()
        os.chmod(self.inbox, 0o700)
        (self.x / "outbox").mkdir()
        (self.inbox / "RUN.lock").write_text("")
        self.attempt = self.inbox / ATTEMPT
        self.attempt.mkdir()
        os.chmod(self.attempt, 0o700)
        self.config = {"qa_name": QA.pw_name, "qa_uid": QA.pw_uid, "producer_uid": ME, "exchange": str(self.x),
                       "codex_path": str(self.codex), "codex_sha256": self.digest}
        self.cfg = self.cfg_dir / "qa-side.json"
        self.write_config()
        self.source = self.attempt / "source"
        (self.source / "sub").mkdir(parents=True)
        for d in (self.source, self.source / "sub"):
            os.chmod(d, 0o755)               # not group-writable whatever the umask (a 0002 umask made these 0775 and tripped the check)
        (self.source / "a.txt").write_text("alpha")
        (self.source / "sub" / "b.sh").write_text("#!/bin/sh\n")
        os.chmod(self.source / "sub" / "b.sh", 0o755)
        os.symlink("a.txt", self.source / "link")
        os.symlink("/etc/hosts", self.source / "abs-link")
        (self.attempt / "prompt.txt").write_text("review this")
        os.chmod(self.attempt / "prompt.txt", 0o444)
        self.request = request if request is not None else self.good_request(verb)
        self.write_request()

    @staticmethod
    def good_request(verb, attempt=ATTEMPT):
        return {"probe": {"verb": "probe", "attempt": attempt, "pid": 4242, "sentinel": SENTINEL},
                "stage": {"verb": "stage", "attempt": attempt},
                "exec": {"verb": "exec", "attempt": attempt, "timeout": 1800, "profile_sha256": SHA}}[verb]

    def write_config(self):
        self.cfg.write_text(json.dumps(self.config))
        os.chmod(self.cfg, 0o444)

    def write_request(self, raw=None):
        path = self.attempt / "request.json"
        if os.path.lexists(path):
            os.unlink(path)
        path.write_text(raw if raw is not None else json.dumps(self.request))
        os.chmod(path, 0o444)

    def close(self):
        for root, dirs, files in os.walk(self.base):
            for name in dirs + files:
                p = os.path.join(root, name)
                if not os.path.islink(p):
                    os.chmod(p, 0o700 if os.path.isdir(p) else 0o600)
        shutil.rmtree(self.base, ignore_errors=True)


class InstalledCase(unittest.TestCase):
    def setUp(self):
        self.trees = []
        stack = contextlib.ExitStack()
        self.addCleanup(stack.close)
        stack.enter_context(mock.patch.object(side, "ROOT_UID", ME))
        # the installed process must be the QA uid; the test process is not, so the two getters answer for it
        stack.enter_context(mock.patch("os.getuid", return_value=QA.pw_uid))
        stack.enter_context(mock.patch("os.geteuid", return_value=QA.pw_uid))
        self.env = {PRODUCER_VAR: str(ME)}
        stack.enter_context(mock.patch.dict(os.environ, self.env, clear=False))
        for name in ("os.kill", "os.killpg", "os.setsid", "os.execve", "os.execv", "signal.alarm", "subprocess.Popen", "subprocess.run"):
            stack.enter_context(mock.patch(name, side_effect=forbidden))
        for name in ("qa_stage", "qa_exec", "detach"):
            stack.enter_context(mock.patch.object(side, name, side_effect=forbidden))
        self.addCleanup(lambda: [t.close() for t in self.trees])

    def tree(self, **kw):
        t = Tree(**kw)
        self.trees.append(t)
        return t

    def run_side(self, t, argv, **kw):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = side.run_installed(argv, config_path=str(t.cfg), chain_anchor=str(t.base), **kw)
        return rc, out.getvalue(), err.getvalue()

    def assertRefused(self, t, argv, msg=None):
        rc, out, err = self.run_side(t, argv)
        self.assertNotEqual(rc, 0, msg)
        self.assertEqual((out, err), ("", ""), msg)

    def dry(self, t, verb="exec", extra=()):
        rc, out, err = self.run_side(t, [verb, ATTEMPT, "--dry-run", *extra])
        self.assertEqual((rc, err), (0, ""), out)
        return json.loads(out)


class ArgvTests(InstalledCase):
    def test_a_good_call_resolves(self):
        t = self.tree()
        self.assertEqual(self.dry(t)["verb"], "exec")

    def test_bad_attempt_names_are_refused(self):
        t = self.tree()
        for bad in ("..", "a..b", "../x", "x/../y", "/etc/passwd", "a/b", "-x", "--x", ".hidden", "_x", "", "a b", "a\nb",
                    "a" * 152, "é1"):
            self.assertRefused(t, ["exec", bad], repr(bad))

    def test_longest_allowed_attempt_name_is_accepted_by_the_parser(self):
        self.assertEqual(side.parse_installed_argv(["stage", "a" * 151]), ("stage", "a" * 151, False))

    def test_verb_allowlist_and_shapes(self):
        t = self.tree()
        for bad in ([], ["run"], ["qa-stage"], ["version", ATTEMPT], ["exec"], ["exec", ATTEMPT, "extra"], ["--dry-run"],
                    ["exec", ATTEMPT, "--config", "/tmp/x"], ["exec", ATTEMPT, "--codex-bin"], ["EXEC", ATTEMPT],
                    ["exec", ATTEMPT, "--dry-run", "--dry-run"]):
            self.assertRefused(t, bad, repr(bad))

    def test_allowed_argv_table_is_the_only_source_of_shapes(self):
        self.assertEqual(set(side.ALLOWED_ARGV), {"version", "probe", "stage", "exec"})
        self.assertEqual(side.ALLOWED_ARGV["exec"], ("exec", "<attempt>"))

    def test_producer_codex_bin_is_ignored(self):
        t = self.tree()
        plan = self.dry(t, extra=("--codex-bin", "/tmp/evil/codex"))
        self.assertEqual(plan["paths"]["codex"], str(t.codex))
        self.assertEqual(plan["argv"][0], str(t.codex))
        self.assertNotIn("evil", json.dumps(plan))

    def test_no_environment_or_argv_moves_the_config(self):
        with mock.patch.dict(os.environ, {"CAE_QA_CONFIG": "/tmp/x", "QA_SIDE_CONFIG": "/tmp/x"}):
            out, err = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                rc = side.main(["exec", ATTEMPT, "--dry-run"])
            self.assertNotEqual(rc, 0)
            self.assertEqual((out.getvalue(), err.getvalue()), ("", ""))
        self.assertEqual(side.CONFIG_PATH, "/Library/cae-qa/qa-side.json")


class IdentityTests(InstalledCase):
    def test_wrong_real_or_effective_uid(self):
        t = self.tree()
        for name in ("os.getuid", "os.geteuid"):
            with mock.patch(name, return_value=ME + 12345):
                self.assertRefused(t, ["exec", ATTEMPT, "--dry-run"], name)

    def test_euid_equal_to_the_producer_uid_is_refused(self):
        t = self.tree()
        with mock.patch("os.getuid", return_value=ME), mock.patch("os.geteuid", return_value=ME):
            self.assertRefused(t, ["exec", ATTEMPT, "--dry-run"])

    def test_producer_uid_variable_must_match(self):
        t = self.tree()
        for bad in (str(ME + 1), "", "abc", "-1", " %d" % ME, "%d\n" % ME):
            with mock.patch.dict(os.environ, {PRODUCER_VAR: bad}):
                self.assertRefused(t, ["exec", ATTEMPT, "--dry-run"], repr(bad))
        with mock.patch.dict(os.environ):
            os.environ.pop(PRODUCER_VAR)
            self.assertRefused(t, ["exec", ATTEMPT, "--dry-run"])

    def test_config_name_must_match_the_uid(self):
        t = self.tree()
        t.config["qa_name"] = "somebodyelse"
        os.chmod(t.cfg, 0o644)
        t.write_config()
        self.assertRefused(t, ["exec", ATTEMPT, "--dry-run"])


class ConfigChainTests(InstalledCase):
    def test_config_not_root_owned(self):
        t = self.tree()
        with mock.patch.object(side, "ROOT_UID", ME + 1):
            self.assertRefused(t, ["version"])

    def test_config_or_a_directory_above_it_writable_by_group_or_world(self):
        for bit in (0o020, 0o002):
            for target in ("cfg", "dir", "base"):
                t = self.tree()
                p = {"cfg": t.cfg, "dir": t.cfg_dir, "base": t.base}[target]
                os.chmod(p, stat.S_IMODE(os.stat(p).st_mode) | bit)
                self.assertRefused(t, ["version"], "%s %o" % (target, bit))

    def test_config_that_is_a_link_or_has_a_link_above_it(self):
        t = self.tree()
        real = t.cfg_dir / "real.json"
        os.rename(t.cfg, real)
        os.symlink(real, t.cfg)
        self.assertRefused(t, ["version"])
        t2 = self.tree()
        moved = t2.base / "cfg-real"
        os.rename(t2.cfg_dir, moved)
        os.symlink(moved, t2.cfg_dir)
        self.assertRefused(t2, ["version"])

    def test_config_shape(self):
        for mutate in (lambda c: c.pop("qa_name"), lambda c: c.update(extra=1), lambda c: c.update(qa_uid="5"),
                       lambda c: c.update(qa_uid=True), lambda c: c.update(producer_uid=c["qa_uid"]), lambda c: c.update(codex_sha256="zz"),
                       lambda c: c.update(exchange="relative/x"), lambda c: c.update(exchange="/a/../b"),
                       lambda c: c.update(codex_path="codex")):
            t = self.tree()
            mutate(t.config)
            os.chmod(t.cfg, 0o644)
            t.write_config()
            self.assertRefused(t, ["version"])
        t = self.tree()
        os.chmod(t.cfg, 0o644)
        t.cfg.write_text("{not json")
        self.assertRefused(t, ["version"])

    def test_codex_digest_drift_and_codex_chain(self):
        t = self.tree()
        os.chmod(t.codex, 0o755)
        t.codex.write_bytes(b"#!/bin/sh\necho tampered\n")
        os.chmod(t.codex, 0o555)
        self.assertRefused(t, ["version"])
        t = self.tree()
        os.chmod(t.codex, 0o575)
        self.assertRefused(t, ["version"], "world writable codex")
        t = self.tree()
        os.chmod(t.codex_dir, 0o775)
        self.assertRefused(t, ["version"], "group writable codex dir")
        t = self.tree()
        real = t.codex_dir / "real"
        os.rename(t.codex, real)
        os.symlink(real, t.codex)
        self.assertRefused(t, ["version"], "codex is a link")
        t = self.tree()
        os.unlink(t.codex)
        t.codex.mkdir()
        self.assertRefused(t, ["version"], "codex is a directory")


class RequestTests(InstalledCase):
    def test_all_three_verbs_resolve_with_a_good_request(self):
        for verb in ("probe", "stage", "exec"):
            t = self.tree(verb=verb)
            plan = self.dry(t, verb)
            self.assertEqual(plan["attempt"], ATTEMPT)
            self.assertEqual(plan["paths"]["inbox"], str(t.x / "inbox" / ATTEMPT))
            self.assertEqual(plan["paths"]["outbox"], str(t.x / "outbox" / ATTEMPT))
            self.assertTrue(plan["paths"]["codex_home"].endswith("/qa/%s/codexhome" % ATTEMPT))

    def test_run_lock_must_exist_and_be_a_regular_file(self):
        t = self.tree()
        os.unlink(t.inbox / "RUN.lock")
        self.assertRefused(t, ["exec", ATTEMPT])
        os.symlink(t.cfg, t.inbox / "RUN.lock")
        self.assertRefused(t, ["exec", ATTEMPT])

    @unittest.skipIf(os.geteuid() == 0, "root can read a mode 0o311 directory, so the old read-mode open would not fail")
    def test_search_only_exchange_root_is_accepted(self):
        """The real exchange root is root:wheel 0711, so the QA user can search but not read it. 0o311 reproduces that for the
        owner: a read-mode open of it fails with EACCES, the search open must not."""
        t = self.tree()
        os.chmod(t.x, 0o311)
        self.addCleanup(os.chmod, t.x, 0o700)
        with self.assertRaises(PermissionError):
            os.close(os.open(t.x, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW))
        self.assertEqual(self.dry(t)["verb"], "exec")
        os.chmod(t.x, 0o000)
        self.assertRefused(t, ["exec", ATTEMPT])

    def test_directory_opens_use_search_flags_only_where_the_directory_is_just_traversed(self):
        """Behaviour, not source text: the exchange, inbox and attempt directories are opened for search only; the clone
        directory and its children, which are listed, are opened for reading. Recorded through an os.open spy."""
        t = self.tree(verb="stage")
        opened = []
        real = os.open

        def spy(path, flags, *a, **k):
            opened.append((str(path), flags))
            return real(path, flags, *a, **k)
        search_bit = side.search_flags() & ~os.O_DIRECTORY & ~os.O_NOFOLLOW
        self.assertTrue(search_bit)
        fds = []
        with mock.patch("os.open", side_effect=spy):
            plan = side.validate_installed(["stage", ATTEMPT], str(t.cfg), str(t.base), fds)
            sfd = side.open_source_dir(plan["afd"], ME)
            side.copy_tree_at(sfd, t.base / "audit-copy", ME)
        self.addCleanup(lambda: [os.close(f) for f in fds + [sfd]])
        dirs = [(p, f) for p, f in opened if f & os.O_DIRECTORY]
        traversed = [(p, f) for p, f in dirs if os.path.basename(p) in (str(t.x), "inbox", ATTEMPT) or p == str(t.x)]
        self.assertEqual(len(traversed), 3, dirs)
        for path, flags in traversed:
            self.assertEqual(flags & side.search_flags(), side.search_flags(), path)
            self.assertTrue(flags & os.O_NOFOLLOW, path)
        listed = [(p, f) for p, f in dirs if (p, f) not in traversed]
        self.assertTrue(listed)                                   # source plus the child directories of the copy
        for path, flags in listed:
            self.assertFalse(flags & search_bit, path)
            self.assertTrue(flags & os.O_NOFOLLOW, path)

    def test_symlinked_inbox_attempt_or_request(self):
        t = self.tree()
        os.rename(t.inbox, t.x / "inbox-real")
        os.symlink(t.x / "inbox-real", t.inbox)
        self.assertRefused(t, ["exec", ATTEMPT], "inbox")
        t = self.tree()
        os.rename(t.attempt, t.inbox / "real-attempt")
        os.symlink(t.inbox / "real-attempt", t.attempt)
        self.assertRefused(t, ["exec", ATTEMPT], "attempt dir")
        t = self.tree()
        os.rename(t.attempt / "request.json", t.attempt / "real.json")
        os.symlink(t.attempt / "real.json", t.attempt / "request.json")
        self.assertRefused(t, ["exec", ATTEMPT], "request")
        t = self.tree()
        moved = t.base / "X-real"
        os.rename(t.x, moved)
        os.symlink(moved, t.x)
        self.assertRefused(t, ["exec", ATTEMPT], "exchange")

    def test_request_missing_fifo_or_directory(self):
        t = self.tree()
        os.unlink(t.attempt / "request.json")
        self.assertRefused(t, ["exec", ATTEMPT], "missing")
        os.mkfifo(t.attempt / "request.json")
        self.assertRefused(t, ["exec", ATTEMPT], "fifo")
        os.unlink(t.attempt / "request.json")
        (t.attempt / "request.json").mkdir()
        self.assertRefused(t, ["exec", ATTEMPT], "directory")

    def _foreign(self, t, target_path):
        ino = os.stat(target_path).st_ino
        real = os.fstat

        def fake(fd):
            st = real(fd)
            if st.st_ino == ino:
                return os.stat_result((st.st_mode, st.st_ino, st.st_dev, st.st_nlink, st.st_uid + 1, st.st_gid, st.st_size,
                                       st.st_atime, st.st_mtime, st.st_ctime))
            return st
        return mock.patch("os.fstat", side_effect=fake)

    def test_foreign_owned_inbox_attempt_request_and_exchange(self):
        for label, rel in (("inbox", "inbox"), ("attempt", "inbox/" + ATTEMPT), ("request", "inbox/%s/request.json" % ATTEMPT),
                           ("exchange", "")):
            t = self.tree()
            with self._foreign(t, t.x / rel if rel else t.x):
                self.assertRefused(t, ["exec", ATTEMPT], label)

    def test_producer_uid_that_owns_nothing(self):
        t = self.tree()
        t.config["producer_uid"] = ME + 7
        os.chmod(t.cfg, 0o644)
        t.write_config()
        with mock.patch.dict(os.environ, {PRODUCER_VAR: str(ME + 7)}):
            self.assertRefused(t, ["exec", ATTEMPT])

    def test_hard_linked_or_oversized_request(self):
        t = self.tree()
        os.link(t.attempt / "request.json", t.base / "second-name")
        self.assertRefused(t, ["exec", ATTEMPT], "two links")
        t = self.tree()
        t.write_request(json.dumps(dict(t.request, pad="x" * 4096)))
        self.assertRefused(t, ["exec", ATTEMPT], "oversize")
        t = self.tree()
        pad = 4096 - len(json.dumps(t.request)) - 1
        t.write_request(json.dumps(t.request) + " " * pad)
        self.assertEqual(os.stat(t.attempt / "request.json").st_size, 4095)
        self.assertEqual(self.dry(t)["verb"], "exec", "just under the cap is fine")

    def test_request_writable_by_group_or_world(self):
        t = self.tree()
        os.chmod(t.attempt / "request.json", 0o666)
        self.assertRefused(t, ["exec", ATTEMPT])

    def test_unknown_missing_and_mismatched_keys(self):
        good = Tree.good_request("exec")
        cases = {
            "unknown key": dict(good, codex_bin="/tmp/evil"),
            "missing key": {k: v for k, v in good.items() if k != "timeout"},
            "other verb": Tree.good_request("stage"),
            "wrong verb": dict(good, verb="stage"),
            "wrong attempt": dict(good, attempt="other-attempt"),
            "not a dict": [good],
        }
        for label, request in cases.items():
            t = self.tree(request=request)
            self.assertRefused(t, ["exec", ATTEMPT], label)
        t = self.tree()
        t.write_request("not json")
        self.assertRefused(t, ["exec", ATTEMPT], "garbage")
        t.write_request("")
        self.assertRefused(t, ["exec", ATTEMPT], "empty")

    def test_typed_ranges_exec(self):
        base = Tree.good_request("exec")
        bad = [dict(base, timeout=59), dict(base, timeout=14401), dict(base, timeout="1800"), dict(base, timeout=True),
               dict(base, timeout=1800.0), dict(base, timeout=None), dict(base, profile_sha256="C" * 64),
               dict(base, profile_sha256="c" * 63), dict(base, profile_sha256="c" * 64 + "\n"), dict(base, profile_sha256=5)]
        for request in bad:
            t = self.tree(request=request)
            self.assertRefused(t, ["exec", ATTEMPT], json.dumps(request))
        for edge in (60, 14400):
            t = self.tree(request=dict(base, timeout=edge))
            self.assertEqual(self.dry(t)["verb"], "exec")

    def test_typed_ranges_probe(self):
        base = Tree.good_request("probe")
        bad = [dict(base, pid=1), dict(base, pid=0), dict(base, pid=-5), dict(base, pid="12"), dict(base, pid=True),
               dict(base, sentinel="qa-sentinel-" + "AB" * 16), dict(base, sentinel="qa-sentinel-" + "ab" * 15),
               dict(base, sentinel=SENTINEL + "\n"), dict(base, sentinel="x" + SENTINEL), dict(base, sentinel=7)]
        for request in bad:
            t = self.tree(request=request, verb="probe")
            self.assertRefused(t, ["probe", ATTEMPT], json.dumps(request))
        t = self.tree(request=dict(base, pid=2), verb="probe")
        self.assertEqual(self.dry(t, "probe")["verb"], "probe")

    def test_version_reads_no_request(self):
        t = self.tree()
        os.unlink(t.attempt / "request.json")
        os.unlink(t.inbox / "RUN.lock")
        rc, out, err = self.run_side(t, ["version"])
        self.assertEqual((rc, err), (0, ""))
        info = json.loads(out)
        self.assertEqual(set(info), {"protocol", "helper_sha256", "codex_sha256"})
        self.assertEqual(info["protocol"], side.PROTOCOL)
        self.assertEqual(info["codex_sha256"], t.digest)
        self.assertEqual(info["helper_sha256"], hashlib.sha256(SIDE.read_bytes()).hexdigest())


class EnvironmentTests(InstalledCase):
    def expected(self, t, verb="exec"):
        home = QA.pw_dir
        outbox = str(t.x / "outbox" / ATTEMPT)
        return {"PATH": "/usr/bin:/bin:/usr/sbin:/sbin", "HOME": home, "USER": QA.pw_name, "LOGNAME": QA.pw_name,
                "TMPDIR": outbox + "/tmp", "XDG_CONFIG_HOME": outbox + "/home/.config", "XDG_DATA_HOME": outbox + "/home/.local/share",
                "XDG_CACHE_HOME": outbox + "/home/.cache", "CARGO_TARGET_DIR": outbox + "/cargo-target",
                "CARGO_NET_OFFLINE": "true", "CODEX_HOME": home + "/.local/state/coding-agent-environment/qa/%s/codexhome" % ATTEMPT}

    def poisoned(self):
        stack = contextlib.ExitStack()
        stack.enter_context(mock.patch.dict(os.environ, dict(POISON, **self.env, **{"LC_ALL": "poison", "USER": "attacker"}), clear=True))
        stack.enter_context(mock.patch.object(side, "HOME", side.HOME))
        stack.enter_context(mock.patch.object(side, "CANARY_ROOT", side.CANARY_ROOT))
        return stack

    def test_builder_equals_the_allowlist_whatever_the_caller_set(self):
        t = self.tree()
        with self.poisoned():
            built = side.installed_env(QA.pw_name, QA.pw_dir, str(t.x / "outbox" / ATTEMPT),
                                       QA.pw_dir + "/.local/state/coding-agent-environment/qa/%s/codexhome" % ATTEMPT)
        self.assertEqual(built, self.expected(t))
        self.assertTrue(all(v == v.strip() and "poison" not in v and "evil" not in v for v in built.values()))

    def test_dry_run_env_is_the_allowlist_under_a_poisoned_environment(self):
        t = self.tree()
        with self.poisoned():
            plan = self.dry(t, "exec")
        self.assertEqual(plan["env"], self.expected(t))
        self.assertNotIn("poison", json.dumps(plan))
        self.assertNotIn("evil", json.dumps(plan))

    def run_action(self, t, verb):
        """Run the real action path with every launcher replaced by a recorder; returns what each would have received."""
        seen = {}
        with self.poisoned(), \
                mock.patch.object(side, "detach", side_effect=lambda n, keep=(): seen.setdefault("alarm", n)), \
                mock.patch.object(side, "qa_stage", side_effect=lambda ns, build, copy, payload=None: seen.update(ns=ns, env=build(), copy=copy, environ=dict(os.environ)) or 0), \
                mock.patch.object(side, "qa_exec", side_effect=lambda ns, build, prompt: seen.update(ns=ns, env=build(), prompt=prompt, environ=dict(os.environ)) or 0), \
                mock.patch("os.execve", side_effect=lambda *a: seen.update(execve=a) or (_ for _ in ()).throw(SystemExit(0))), \
                mock.patch("os.dup2"):
            try:
                rc = side.run_installed([verb, ATTEMPT], config_path=str(t.cfg), chain_anchor=str(t.base))
            except SystemExit:
                rc = 0
            seen["rc"] = rc
            seen["env_after"] = dict(os.environ)
        return seen

    def test_exec_and_stage_pass_the_allowlist_and_a_scrubbed_process_environment(self):
        for verb in ("exec", "stage"):
            t = self.tree(verb=verb)
            seen = self.run_action(t, verb)
            self.assertEqual(seen["rc"], 0, verb)
            self.assertEqual(seen["env"], self.expected(t), verb)
            self.assertEqual(seen["environ"], {"PATH": "/usr/bin:/bin:/usr/sbin:/sbin", "HOME": QA.pw_dir}, verb)
            self.assertEqual(seen["ns"].codex_bin, str(t.codex))
            self.assertEqual(seen["ns"].inbox, str(t.x / "inbox" / ATTEMPT))
            self.assertEqual(seen["ns"].outbox, str(t.x / "outbox" / ATTEMPT))

    def test_exec_arms_an_alarm_and_takes_timeout_and_profile_from_the_request(self):
        t = self.tree()
        seen = self.run_action(t, "exec")
        self.assertEqual(seen["ns"].timeout, 1800)
        self.assertEqual(seen["ns"].profile_sha256, SHA)
        self.assertEqual(seen["alarm"], 1800 + side.ALARM_SLACK)

    def test_probe_execs_the_system_python_with_a_two_variable_environment(self):
        t = self.tree(verb="probe")
        seen = self.run_action(t, "probe")
        path, argv, env = seen["execve"]
        self.assertEqual(path, "/usr/bin/python3")
        self.assertEqual(argv[:3], ["/usr/bin/python3", "-I", "-c"])
        self.assertEqual(argv[3], side.PROBE)
        self.assertEqual(argv[4:], [str(t.x / "inbox"), str(t.x / "outbox"), str(t.x), pwd.getpwuid(ME).pw_dir, "4242", SENTINEL])
        self.assertEqual(env, {"PATH": "/usr/bin:/bin:/usr/sbin:/sbin", "HOME": QA.pw_dir})

    def test_the_attended_builder_is_untouched_and_does_copy_the_caller(self):
        with self.poisoned():
            attended = side.isolated_env(Path("/run/x"))
        self.assertIn("LANG", attended)
        self.assertEqual(attended["LANG"], "xx_XX.POISON")
        self.assertEqual(attended["TERM"], "poison-term")

    def test_pin_process_scrubs_the_environment_and_retargets_home(self):
        with self.poisoned():
            side.pin_process("/Users/qa")
            self.assertEqual(dict(os.environ), {"PATH": "/usr/bin:/bin:/usr/sbin:/sbin", "HOME": "/Users/qa"})
            self.assertEqual(side.HOME, Path("/Users/qa"))
            self.assertEqual(side.real_codex_home(), Path("/Users/qa/.codex"))
            self.assertEqual(side.CANARY_ROOT, Path("/Users/qa/.local/state/coding-agent-environment/qa-canary"))


class NoExecutionTests(InstalledCase):
    def test_dry_run_starts_nothing(self):
        for verb in ("probe", "stage", "exec"):
            t = self.tree(verb=verb)
            plan = self.dry(t, verb)
            self.assertEqual(set(plan), {"verb", "attempt", "argv", "env", "paths"})
        # the tripwires installed by setUp (Popen, run, execve, setsid, kill, killpg, alarm, qa_stage, qa_exec, detach) never fired

    def test_dry_run_does_not_touch_the_environment_or_create_files(self):
        t = self.tree()
        before = dict(os.environ)
        listing = sorted(str(p) for p in t.base.rglob("*"))
        self.dry(t)
        self.assertEqual(dict(os.environ), before)
        self.assertEqual(sorted(str(p) for p in t.base.rglob("*")), listing)

    def test_failure_is_silent_and_closes_its_descriptors(self):
        t = self.tree()
        os.unlink(t.inbox / "RUN.lock")
        opened = []
        real_open, real_close = os.open, os.close

        def spy_open(*a, **k):
            fd = real_open(*a, **k)
            opened.append(fd)
            return fd
        closed = []
        with mock.patch("os.open", side_effect=spy_open), mock.patch("os.close", side_effect=lambda fd: (closed.append(fd), real_close(fd))[1]):
            self.assertRefused(t, ["exec", ATTEMPT])
        for fd in opened:
            self.assertIn(fd, closed)


class DescriptorRelativeTests(InstalledCase):
    """The QA side must use only the descriptors it validated: a path swapped after validation is never followed."""

    def attempt_fd(self, t):
        return os.open(t.attempt, os.O_RDONLY | os.O_DIRECTORY)

    def test_copy_is_exact_and_keeps_links_as_links(self):
        t = self.tree()
        afd = self.attempt_fd(t)
        self.addCleanup(os.close, afd)
        sfd = side.open_source_dir(afd, ME)
        self.addCleanup(os.close, sfd)
        dst = t.base / "copy"
        side.copy_tree_at(sfd, dst, ME)
        self.assertEqual((dst / "a.txt").read_text(), "alpha")
        self.assertEqual(os.readlink(dst / "link"), "a.txt")
        self.assertEqual(os.readlink(dst / "abs-link"), "/etc/hosts")
        self.assertTrue(os.access(dst / "sub" / "b.sh", os.X_OK))
        self.assertFalse(os.path.islink(dst / "sub"))

    def test_symlinked_source_or_prompt_is_refused(self):
        t = self.tree()
        elsewhere = t.base / "elsewhere"
        elsewhere.mkdir()
        (elsewhere / "secret.txt").write_text("do not copy")
        os.rename(t.source, t.attempt / "real-source")
        os.symlink(elsewhere, t.source)
        afd = self.attempt_fd(t)
        self.addCleanup(os.close, afd)
        with self.assertRaises(OSError):
            side.open_source_dir(afd, ME)
        os.unlink(t.attempt / "prompt.txt")
        os.symlink(elsewhere / "secret.txt", t.attempt / "prompt.txt")
        with self.assertRaises(OSError):
            side.open_prompt_at(afd, ME)

    def test_foreign_owned_source_and_prompt_and_oversize_prompt(self):
        t = self.tree()
        afd = self.attempt_fd(t)
        self.addCleanup(os.close, afd)
        with self.assertRaises(side.Refuse):
            side.open_source_dir(afd, ME + 1)
        with self.assertRaises(side.Refuse):
            side.open_prompt_at(afd, ME + 1)
        os.chmod(t.attempt / "prompt.txt", 0o644)
        (t.attempt / "prompt.txt").write_bytes(b"x" * ((1 << 20) + 1))
        with self.assertRaises(side.Refuse):
            side.open_prompt_at(afd, ME)

    def test_a_link_planted_inside_the_tree_is_copied_as_a_link_not_followed(self):
        t = self.tree()
        os.symlink(t.base / "cfg", t.source / "to-dir")
        afd = self.attempt_fd(t)
        self.addCleanup(os.close, afd)
        sfd = side.open_source_dir(afd, ME)
        self.addCleanup(os.close, sfd)
        side.copy_tree_at(sfd, t.base / "copy2", ME)
        self.assertTrue(os.path.islink(t.base / "copy2" / "to-dir"))
        self.assertFalse((t.base / "copy2" / "to-dir" / "qa-side.json").exists() and not os.path.islink(t.base / "copy2" / "to-dir"))

    def copy_fails(self, t, producer=ME):
        afd = self.attempt_fd(t)
        self.addCleanup(os.close, afd)
        sfd = os.open(t.source, os.O_RDONLY | os.O_DIRECTORY)
        self.addCleanup(os.close, sfd)
        dst = t.base / ("out-%d" % len(os.listdir(t.base)))
        with self.assertRaises(side.Refuse):
            side.copy_tree_at(sfd, dst, producer)

    def test_hard_linked_file_is_refused_at_any_depth(self):
        t = self.tree()
        os.link(t.source / "a.txt", t.base / "other-name")
        self.copy_fails(t)
        t = self.tree()
        deep = t.source / "d1" / "d2" / "d3"
        deep.mkdir(parents=True)
        (deep / "f").write_text("x")
        os.link(deep / "f", t.base / "elsewhere")
        self.copy_fails(t)

    def test_foreign_owned_file_or_directory_is_refused_at_any_depth(self):
        # the producer uid is what is compared, so a different value stands in for "someone else owns it"
        t = self.tree()
        self.copy_fails(t, producer=ME + 1)
        t = self.tree()
        deep = t.source / "d1" / "d2"
        deep.mkdir(parents=True)
        real = os.fstat
        ino = os.stat(deep).st_ino

        def fake(fd):
            st = real(fd)
            if st.st_ino == ino:
                return os.stat_result((st.st_mode, st.st_ino, st.st_dev, st.st_nlink, st.st_uid + 1, st.st_gid, st.st_size,
                                       st.st_atime, st.st_mtime, st.st_ctime))
            return st
        with mock.patch("os.fstat", side_effect=fake):
            self.copy_fails(t)

    def test_nested_tree_of_owned_single_link_files_copies(self):
        t = self.tree()
        deep = t.source / "d1" / "d2" / "d3"
        deep.mkdir(parents=True)
        (deep / "f").write_text("deep")
        afd = self.attempt_fd(t)
        self.addCleanup(os.close, afd)
        sfd = side.open_source_dir(afd, ME)
        self.addCleanup(os.close, sfd)
        side.copy_tree_at(sfd, t.base / "nested", ME)
        self.assertEqual((t.base / "nested" / "d1" / "d2" / "d3" / "f").read_text(), "deep")

    def test_prompt_with_a_second_link_is_refused(self):
        t = self.tree()
        os.link(t.attempt / "prompt.txt", t.base / "prompt-twin")
        afd = self.attempt_fd(t)
        self.addCleanup(os.close, afd)
        with self.assertRaises(side.Refuse):
            side.open_prompt_at(afd, ME)

    def test_set_id_and_sticky_bits_are_not_carried_into_the_copy(self):
        t = self.tree()
        os.chmod(t.source / "a.txt", 0o4755)
        os.mkdir(t.source / "stick")
        os.chmod(t.source / "stick", 0o1777)
        afd = self.attempt_fd(t)
        self.addCleanup(os.close, afd)
        sfd = os.open(t.source, os.O_RDONLY | os.O_DIRECTORY)
        self.addCleanup(os.close, sfd)
        side.copy_tree_at(sfd, t.base / "masked", ME)
        self.assertEqual(stat.S_IMODE(os.stat(t.base / "masked" / "a.txt").st_mode), 0o755)
        self.assertEqual(stat.S_IMODE(os.stat(t.base / "masked" / "stick").st_mode), 0o777)

    def test_clone_limits_entries_bytes_and_depth(self):
        t = self.tree()
        sfd = os.open(t.source, os.O_RDONLY | os.O_DIRECTORY)
        self.addCleanup(os.close, sfd)
        count = 0
        entries = sum(len(files) + len(dirs) for _, dirs, files in os.walk(t.source))
        for label, patch, ok in (("entries", {"MAX_COPY_ENTRIES": entries - 1}, False), ("entries ok", {"MAX_COPY_ENTRIES": entries}, True),
                                 ("bytes", {"MAX_COPY_BYTES": 8}, False), ("bytes ok", {"MAX_COPY_BYTES": 5 + 10}, True),
                                 ("depth", {"MAX_COPY_DEPTH": 0}, False), ("depth ok", {"MAX_COPY_DEPTH": 1}, True)):
            count += 1
            dst = t.base / ("limit-%d" % count)
            with mock.patch.multiple(side, **patch):
                if ok:
                    side.copy_tree_at(sfd, dst, ME)
                else:
                    with self.assertRaises(side.Refuse, msg=label):
                        side.copy_tree_at(sfd, dst, ME)

    def test_a_file_that_grows_past_the_byte_cap_during_the_copy_is_refused(self):
        t = self.tree()
        (t.source / "big").write_bytes(b"x" * (3 << 20))
        sfd = os.open(t.source, os.O_RDONLY | os.O_DIRECTORY)
        self.addCleanup(os.close, sfd)
        with mock.patch.object(side, "MAX_COPY_BYTES", 2 << 20), self.assertRaises(side.Refuse):
            side.copy_tree_at(sfd, t.base / "toobig", ME)

    def swap_attempt_dir(self, t):
        """Replace inbox/<attempt> by a symlink to a hostile tree, as an attacker would between validation and use."""
        evil = t.base / "evil"
        (evil / "source").mkdir(parents=True)
        (evil / "source" / "stolen.txt").write_text("hostile")
        (evil / "prompt.txt").write_text("hostile prompt")
        os.rename(t.attempt, t.inbox / "moved-away")
        os.symlink(evil, t.attempt)

    def test_stage_copy_after_the_attempt_directory_is_swapped_uses_the_validated_one(self):
        t = self.tree(verb="stage")
        fds = []
        with mock.patch.object(side, "ROOT_UID", ME):
            plan = side.validate_installed(["stage", ATTEMPT], str(t.cfg), str(t.base), fds)
        self.addCleanup(lambda: [os.close(f) for f in fds if self._open(f)])
        sfd = side.open_source_dir(plan["afd"], ME)
        self.addCleanup(os.close, sfd)
        self.swap_attempt_dir(t)
        side.copy_tree_at(sfd, t.base / "after-swap", ME)
        self.assertTrue((t.base / "after-swap" / "a.txt").exists())
        self.assertFalse((t.base / "after-swap" / "stolen.txt").exists())

    def test_prompt_after_the_attempt_directory_is_swapped_is_the_validated_one(self):
        t = self.tree()
        fds = []
        plan = side.validate_installed(["exec", ATTEMPT], str(t.cfg), str(t.base), fds)
        self.addCleanup(lambda: [os.close(f) for f in fds if self._open(f)])
        self.swap_attempt_dir(t)
        with side.open_prompt_at(plan["afd"], ME) as handle:
            self.assertEqual(handle.read(), b"review this")

    @staticmethod
    def _open(fd):
        try:
            os.fstat(fd)
            return True
        except OSError:
            return False

    def test_the_action_path_hands_the_installed_callables_not_paths(self):
        t = self.tree(verb="stage")
        seen = self.run_action_with_live_fds(t, "stage")
        self.assertEqual(seen["copy_result"], "alpha")
        t = self.tree()
        seen = self.run_action_with_live_fds(t, "exec")
        self.assertEqual(seen["prompt_text"], b"review this")

    def run_action_with_live_fds(self, t, verb):
        """Like EnvironmentTests.run_action, but the recorders use the callables before the descriptors are closed."""
        seen = {}

        def stage(ns, build, copy, payload=None):
            copy(Path(t.base) / "staged")
            seen["copy_result"] = (t.base / "staged" / "a.txt").read_text()
            return 0

        def execute(ns, build, prompt):
            with prompt() as handle:
                seen["prompt_text"] = handle.read()
            return 0
        with mock.patch.dict(os.environ, self.env, clear=True), mock.patch.object(side, "HOME", side.HOME), \
                mock.patch.object(side, "CANARY_ROOT", side.CANARY_ROOT), mock.patch.object(side, "detach"), \
                mock.patch.object(side, "qa_stage", side_effect=stage), mock.patch.object(side, "qa_exec", side_effect=execute):
            self.assertEqual(side.run_installed([verb, ATTEMPT], config_path=str(t.cfg), chain_anchor=str(t.base)), 0)
        return seen

    def test_missing_source_or_prompt_fails_the_action_silently(self):
        t = self.tree(verb="stage")
        shutil.rmtree(t.source)
        with mock.patch.dict(os.environ, self.env, clear=True), mock.patch.object(side, "HOME", side.HOME), \
                mock.patch.object(side, "CANARY_ROOT", side.CANARY_ROOT):
            rc, out, err = self.run_side(t, ["stage", ATTEMPT])
        self.assertEqual((out, err), ("", ""))
        self.assertNotEqual(rc, 0)


class DetachTests(unittest.TestCase):
    """detach() forks, so it works for a process-group leader (where a bare setsid raises EPERM). These tests really fork
    short-lived children of the test process; nothing is signalled except those children."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        self.pids = []
        self.addCleanup(self.reap)

    def reap(self):
        for pid in self.pids:
            try:
                os.kill(pid, signal.SIGKILL)
                os.waitpid(pid, 0)
            except OSError:
                pass

    def fork_leader(self, body):
        """Fork a child that leads its own process group, as the privilege tool may start the helper, and run body there.
        Returns its pid; the child exits 99 if body raises."""
        pid = os.fork()
        if pid == 0:
            try:
                os.setpgid(0, 0)
                body()
            except BaseException:
                os._exit(99)
            os._exit(0)
        self.pids.append(pid)
        return pid

    def finish(self, pid):
        _, status = os.waitpid(pid, 0)
        self.pids.remove(pid)
        return os.waitstatus_to_exitcode(status)

    def test_a_bare_setsid_fails_for_a_group_leader_which_is_why_detach_forks(self):
        pid = self.fork_leader(lambda: os._exit(0 if self._setsid_refused() else 5))
        self.assertEqual(self.finish(pid), 0)

    @staticmethod
    def _is_open(fd):
        try:
            os.fstat(fd)
            return True
        except OSError:
            return False

    @staticmethod
    def _setsid_refused():
        try:
            os.setsid()
        except PermissionError:
            return True
        return False

    def test_detach_succeeds_for_a_group_leader_and_the_child_is_a_clean_session_leader(self):
        marker = self.dir / "marker.json"
        extra = os.open(os.devnull, os.O_RDONLY)          # an inherited descriptor that must not survive
        held = os.open(self.dir, os.O_RDONLY)             # the held prompt stand-in that must survive

        def body():
            side.detach(300, keep=(held,))
            alarm = signal.alarm(0)
            marker.write_text(json.dumps({"pid": os.getpid(), "sid": os.getsid(0), "pgid": os.getpgid(0), "cwd": os.getcwd(),
                                          "stdin": os.readlink("/dev/fd/0") if os.path.islink("/dev/fd/0") else os.fstat(0).st_rdev,
                                          "alarm": alarm, "extra_open": self._is_open(extra), "held_open": self._is_open(held),
                                          "stdin_is_null": os.fstat(0).st_rdev == os.stat(os.devnull).st_rdev}))
            os._exit(7)
        pid = self.fork_leader(body)
        self.assertEqual(self.finish(pid), 7)            # the grandchild's status is the parent's status
        info = json.loads(marker.read_text())
        self.assertNotEqual(info["pid"], pid)
        self.assertEqual((info["sid"], info["pgid"]), (info["pid"], info["pid"]))
        self.assertEqual(info["cwd"], "/")
        self.assertTrue(info["stdin_is_null"])
        self.assertGreater(info["alarm"], 0)
        self.assertFalse(info["extra_open"])
        self.assertTrue(info["held_open"])
        os.close(extra)
        os.close(held)

    def test_a_signal_sent_to_the_parent_is_relayed_to_the_detached_child(self):
        ready, got = self.dir / "ready", self.dir / "got"

        def body():
            side.detach(300)
            signal.signal(signal.SIGTERM, lambda n, f: (got.write_text(str(n)), os._exit(9)))
            ready.write_text("1")
            time.sleep(30)
            os._exit(1)
        pid = self.fork_leader(body)
        for _ in range(200):
            if ready.exists():
                break
            time.sleep(0.05)
        self.assertTrue(ready.exists())
        os.kill(pid, signal.SIGTERM)
        self.assertEqual(self.finish(pid), 9)
        self.assertEqual(got.read_text(), str(int(signal.SIGTERM)))

    def test_a_child_killed_by_a_signal_gives_128_plus_the_signal(self):
        pid = self.fork_leader(lambda: (side.detach(300), os.kill(os.getpid(), signal.SIGKILL)))
        self.assertEqual(self.finish(pid), 128 + int(signal.SIGKILL))

    def test_the_parent_holds_no_descriptor_above_two(self):
        report = self.dir / "parent-fds"
        held = os.open(self.dir, os.O_RDONLY)

        def body():
            real_waitpid = os.waitpid

            def spy(child, flags):
                report.write_text(json.dumps([n for n in range(3, 64) if self._is_open(n)]))
                return real_waitpid(child, flags)
            os.waitpid = spy
            side.detach(300, keep=(held,))
            os._exit(0)
        pid = self.fork_leader(body)
        self.assertEqual(self.finish(pid), 0)
        self.assertNotIn(held, json.loads(report.read_text()))
        os.close(held)


class SharedPartsTests(unittest.TestCase):
    def test_probe_source_is_the_attended_probe(self):
        self.assertEqual(side.PROBE, qis.PROBE)

    def test_stdlib_cli_failure_prints_nothing_and_exits_non_zero(self):
        env = {"PATH": "/usr/bin:/bin", "HOME": "/nonexistent"}
        for argv in (["version"], ["exec", ATTEMPT], ["exec", ".."], ["bogus"]):
            if argv == ["bogus"]:
                continue                                  # not an installed verb: the attended parser answers (usage error)
            done = subprocess.run([sys.executable, "-I", str(SIDE)] + argv, capture_output=True, text=True, timeout=60, env=env)
            self.assertNotEqual(done.returncode, 0, argv)
            self.assertEqual((done.stdout, done.stderr), ("", ""), argv)

    def test_attended_subcommands_are_still_there(self):
        parser = side.build_parser()
        for name in ("qa-stage", "qa-exec"):
            with self.assertRaises(SystemExit):
                parser.parse_args([name])
        args = parser.parse_args(["qa-exec", "--inbox", "a", "--outbox", "b", "--codex-home", "c", "--profile-sha256", "d"])
        self.assertEqual((args.codex_bin, args.timeout), ("codex", 1800))


class EndpointProfileTests(unittest.TestCase):
    """Pure generators for the daemon-endpoint mode (SPE-145 PR-b1). Nothing starts a process; the real behaviour of these profiles is
    measured by the model-free demo as the QA user, and a uid-501 spike recorded in the PR."""
    HOME = "/Users/qa"
    EX = "/Users/Shared/qa-qa"
    PAY = "/Users/Shared/qa-qa/outbox/att-1/payload"

    def endpoint(self, **over):
        args = dict(qa_home=self.HOME, exchange=self.EX, attempt="att-1", port=23456, contract="fornax-daemon/1",
                    payload_dir=self.PAY, daemon_binary=self.PAY + "/bin/daemon")
        args.update(over)
        return side.Endpoint(**args)

    def test_none_mode_is_unchanged_and_has_no_endpoint_text(self):
        text = side.profile_toml(Path("/Users/Shared/qa-qa/outbox/att-1"), [], home=self.HOME, codex_home=self.HOME + "/.codex")
        self.assertIn("[permissions.qa_confined.network]\nenabled = false\n", text)
        for banned in ("unix_sockets", "[features]", "network_proxy", "allow_local_binding", "mode ="):
            self.assertNotIn(banned, text)
        self.assertIn('"/Users/qa/.codex" = "deny"', text)
        self.assertIn('"/Users/qa/.ssh" = "deny"', text)
        self.assertEqual(text, side.profile_toml(Path("/Users/Shared/qa-qa/outbox/att-1"), [], home=self.HOME, codex_home=self.HOME + "/.codex",
                                                 endpoint=None))

    def test_endpoint_mode_allows_exactly_two_sockets_and_no_tcp(self):
        e = self.endpoint()
        text = side.profile_toml(Path("/Users/Shared/qa-qa/outbox/att-1"), [], home=self.HOME, codex_home=self.HOME + "/.codex", endpoint=e)
        self.assertIn("[features]\nnetwork_proxy = true\n", text)
        self.assertIn('enabled = true\nmode = "limited"\nallow_local_binding = false\nenable_socks5 = false\n', text)
        table = text.split("[permissions.qa_confined.network.unix_sockets]\n")[1].strip().splitlines()
        self.assertEqual(table, ['"%s" = "allow"' % e.daemon_sock, '"%s" = "allow"' % e.relay_sock])
        for banned in ("domains", "allow_upstream_proxy", "dangerously", "allow_local_binding = true", "proxy_url", "mitm"):
            self.assertNotIn(banned, text)
        self.assertIn('"%s" = "read"' % e.state, text)                 # state readable to tool commands, never writable
        self.assertNotIn('"%s" = "write"' % e.state, text)
        self.assertNotIn(str(e.rl) + '" = "', text.split("[permissions.qa_confined.network]")[0].replace(str(e.state), ""))
        self.assertNotIn(str(e.port), text)                           # the TCP port never appears in what tool commands can read

    def test_endpoint_paths_are_short_derived_and_inside_the_qa_home(self):
        e = self.endpoint()
        self.assertEqual(e.dir.parent, Path(self.HOME) / "e")
        self.assertRegex(e.dir.name, r"^[0-9a-f]{12}$")
        self.assertEqual(self.endpoint().dir, e.dir)                    # both hops derive the same path
        self.assertNotEqual(self.endpoint(attempt="att-2").dir, e.dir)
        for sock in (e.daemon_sock, e.relay_sock):
            self.assertLess(len(str(sock).encode()), side.SUN_PATH_LIMIT)
        self.assertEqual(e.daemon_sock.name, "fornax.sock")

    def test_bad_parameters_are_refused(self):
        for over in ({"port": 0}, {"port": 19999}, {"port": 32768}, {"port": True}, {"port": "23456"}, {"port": None},
                     {"contract": "other/1"}, {"contract": None},
                     {"qa_home": "/Users/qa dir"}, {"qa_home": '/Users/q"a'}, {"qa_home": "/Users/qa\\x"}, {"qa_home": "/Users/qa/../x"},
                     {"qa_home": "relative/home"}, {"qa_home": "/"}, {"exchange": "/Users/Shared//qa-qa"}, {"exchange": "/Users/qa)(allow default"},
                     {"payload_dir": self.PAY + "\n(allow default)"}, {"daemon_binary": "/usr/bin/true"},
                     {"qa_home": "//Users/qa"}, {"exchange": "//Users/Shared/qa-qa"},
                     {"payload_dir": "/Users", "daemon_binary": "/Users/x"},                        # would cancel the read-deny
                     {"payload_dir": self.EX, "daemon_binary": self.EX + "/bin/daemon"},           # not strictly inside the exchange
                     {"payload_dir": self.HOME + "/p", "daemon_binary": self.HOME + "/p/d"},       # outside the exchange
                     {"exchange": self.HOME}, {"exchange": self.HOME + "/ex", "payload_dir": self.HOME + "/ex/p",
                                               "daemon_binary": self.HOME + "/ex/p/d"},             # exchange inside the home
                     {"qa_home": self.EX + "/home"},                                                # home inside the exchange
                     {"daemon_binary": self.PAY + "/../../x"}, {"qa_home": "/Users/" + "q" * 80}):
            with self.assertRaises(side.EndpointError, msg=str(over)):
                self.endpoint(**over)

    def test_daemon_profile_denies_before_it_allows_and_writes_only_its_state_and_tmp(self):
        e = self.endpoint()
        sb = side.daemon_sb(e)
        lines = sb.splitlines()
        self.assertEqual(lines[:3], ["(version 1)", "(allow default)", "(deny network*)"])
        for name in side.MACH_DENY:
            self.assertIn('(global-name "%s")' % name, sb)
        self.assertIn('(allow network-bind (local ip "localhost:23456"))', lines)
        self.assertIn('(allow network-inbound (local ip "localhost:23456"))', lines)
        self.assertIn('(allow network-bind (local unix-socket (subpath "%s")))' % e.state, lines)
        deny = next(i for i, l in enumerate(lines) if l.startswith("(deny file-read*"))
        allow = next(i for i, l in enumerate(lines) if l.startswith("(allow file-read* "))
        meta_at = next(i for i, l in enumerate(lines) if l.startswith("(allow file-read-metadata"))
        self.assertLess(deny, meta_at)                                  # SBPL: the later rule wins, so every re-allow must come after the deny
        self.assertLess(deny, allow)
        for root in side.READ_DENY + (self.HOME, self.EX):
            self.assertIn('(subpath "%s")' % root, lines[deny])
        self.assertEqual(lines[allow], '(allow file-read* (subpath "%s") (subpath "%s") (subpath "%s"))' % (e.state, e.tmp, self.PAY))
        self.assertNotIn('(subpath "%s")' % e.rl, lines[allow])         # the daemon has no business in the relay directory
        meta = lines[meta_at]
        for anc in ("/Users", self.HOME, self.HOME + "/e", str(e.dir), "/Users/Shared", self.EX, self.EX + "/outbox", self.EX + "/outbox/att-1"):
            self.assertIn('(literal "%s")' % anc, meta)
        self.assertLess(lines.index("(allow default)"), next(i for i, l in enumerate(lines) if l.startswith("(deny mach-lookup")))
        self.assertNotIn("subpath", meta)                               # metadata only on the literal ancestors: no listing, no content
        self.assertIn("(deny file-write*)", lines)
        writes = next(l for l in lines if l.startswith("(allow file-write*"))
        self.assertEqual(writes, '(allow file-write* (subpath "%s") (subpath "%s") (literal "/dev/null") (literal "/dev/dtracehelper"))' % (e.state, e.tmp))
        self.assertLess(lines.index("(deny file-write*)"), lines.index(writes))
        self.assertNotIn("network-outbound", sb)

    def test_relay_profile_reaches_one_port_and_writes_only_its_socket_directory(self):
        e = self.endpoint()
        lines = side.relay_sb(e).splitlines()
        self.assertEqual(lines[:3], ["(version 1)", "(allow default)", "(deny network*)"])
        self.assertIn('(allow network-outbound (remote ip "localhost:23456"))', lines)
        self.assertEqual([l for l in lines if "network-outbound" in l], ['(allow network-outbound (remote ip "localhost:23456"))'])
        self.assertIn('(allow network-bind (local unix-socket (subpath "%s")))' % e.rl, lines)
        self.assertEqual(next(l for l in lines if l.startswith("(allow file-write*")), '(allow file-write* (subpath "%s") (literal "/dev/null"))' % e.rl)
        self.assertEqual(next(l for l in lines if l.startswith("(allow file-read* ")), '(allow file-read* (subpath "%s"))' % e.rl)
        self.assertNotIn(self.PAY, "\n".join(lines))                    # the relay never needs the payload
        deny = next(i for i, l in enumerate(lines) if l.startswith("(deny file-read*"))
        self.assertLess(deny, next(i for i, l in enumerate(lines) if l.startswith("(allow file-read-metadata")))
        self.assertLess(deny, next(i for i, l in enumerate(lines) if l.startswith("(allow file-read* ")))
        for root in side.READ_DENY + (self.HOME, self.EX):
            self.assertIn('(subpath "%s")' % root, lines[deny])
        self.assertLess(lines.index("(allow default)"), next(i for i, l in enumerate(lines) if l.startswith("(deny mach-lookup")))
        self.assertLess(lines.index("(deny file-write*)"), next(i for i, l in enumerate(lines) if l.startswith("(allow file-write*")))

    def test_relay_source_is_fixed_and_forwards_to_loopback_only(self):
        self.assertIn("open_connection('127.0.0.1', PORT)", side.RELAY_SRC)
        self.assertEqual(side.RELAY_SRC.count("open_connection"), 1)
        self.assertNotIn("sys.argv[3]", side.RELAY_SRC)
        compile(side.RELAY_SRC, "relay", "exec")

    def test_digests_follow_the_text_and_the_environment_is_exact(self):
        e, other = self.endpoint(), self.endpoint(port=23457)
        d = side.endpoint_digests(e)
        self.assertEqual(set(d), {"daemon_profile_sha256", "relay_profile_sha256", "relay_source_sha256"})
        self.assertEqual(d["daemon_profile_sha256"], hashlib.sha256(side.daemon_sb(e).encode()).hexdigest())
        self.assertNotEqual(side.endpoint_digests(other)["daemon_profile_sha256"], d["daemon_profile_sha256"])
        self.assertEqual(e.daemon_env(), {"PATH": "/usr/bin:/bin", "FORNAX_HOME": str(e.state), "FORNAX_HTTP_PORT": "23456", "TMPDIR": str(e.tmp)})
        self.assertEqual(set(e.tool_env()), {"QA_ENDPOINT_STATE", "QA_ENDPOINT_SOCK", "QA_ENDPOINT_HTTP_SOCK"})
        self.assertNotIn("23456", "".join(e.tool_env().values()))

    def test_ancestors_are_outermost_first_and_unique(self):
        e = self.endpoint()
        got = e.ancestors(e.state, self.PAY)
        self.assertEqual(got[0], "/Users")
        self.assertEqual(len(got), len(set(got)))
        self.assertNotIn("/", got)
        self.assertIn(str(e.dir), got)
        self.assertNotIn(str(e.state), got)


class NetworkControlTests(unittest.TestCase):
    NET = ("neg_loopback_bind", "neg_uds_bind", "neg_testnet_connect", "neg_testnet6_connect", "neg_udp_testnet", "neg_resolver_socket")

    def test_all_six_network_controls_are_in_the_base_set_and_expect_eperm(self):
        got = {name: expect for name, _, expect in side.CONTROLS}
        for name in self.NET:
            self.assertEqual(got.get(name), "DENIED_EPERM", name)

    def test_a_socket_denial_counts_only_as_eperm(self):
        for observed in ("DENIED:13", "DENIED:61", "DENIED:65", "DENIED:51", "DENIED:None", "ALLOWED", "DENIED:1 ", ""):
            self.assertFalse(side.control_ok("DENIED_EPERM", observed), observed)
        self.assertTrue(side.control_ok("DENIED_EPERM", "DENIED:1"))

    def test_an_unknown_expectation_never_passes(self):
        for expect in ("DENIED_EPREM", "", None, "denied"):
            for observed in ("DENIED:1", "DENIED:13", "ALLOWED"):
                self.assertFalse(side.control_ok(expect, observed), (expect, observed))

    def test_filesystem_denials_still_accept_eperm_and_eacces(self):
        self.assertTrue(side.control_ok("DENIED", "DENIED:1"))
        self.assertTrue(side.control_ok("DENIED", "DENIED:13"))
        self.assertFalse(side.control_ok("DENIED", "DENIED:2"))
        self.assertFalse(side.control_ok("DENIED", "ALLOWED"))
        self.assertTrue(side.control_ok("ALLOWED", "ALLOWED"))
        self.assertFalse(side.control_ok("ALLOWED", "DENIED:1"))

    def test_every_network_snippet_prints_only_the_two_tokens(self):
        for name, snippet, _ in side.CONTROLS:
            if name in self.NET:
                self.assertIn("print('ALLOWED')", snippet, name)
                self.assertIn("print('DENIED:%s' % e.errno)", snippet, name)
                compile(snippet.format(out="/tmp"), name, "exec")


if __name__ == "__main__":
    unittest.main()
