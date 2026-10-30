#!/usr/bin/env python3
"""Offline tests for scripts/qa-identity-setup.py: pure checks and command construction. No account is ever created."""
import importlib.util
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "qa-identity-setup.py"
spec = importlib.util.spec_from_file_location("qa_identity_setup", SCRIPT)
qis = importlib.util.module_from_spec(spec)
spec.loader.exec_module(qis)

GOOD = {"uid": 777, "gid": 777, "groups": [777, 12, 61], "ctty": "ENXIO", "list_inbox": "ok", "write_inbox": "EACCES",
        "write_outbox": "ok", "write_exchange": "EACCES", "list_producer_home": "EACCES", "stat_producer_auth": "EACCES",
        "open_producer_ssh": "EPERM", "own_env_read": "ok", "foreign_env_leaked": False}


class ArgsTest(unittest.TestCase):
    def test_valid_defaults_exchange_under_shared(self):
        home, x = qis.check_args("fornaxqa", 777, None)
        self.assertEqual(str(home), "/Users/fornaxqa")
        self.assertEqual(str(x), "/Users/Shared/fornaxqa-qa")

    def test_rejects_bad_names_and_uids(self):
        for bad in ("Root", "root", "_www", "a", "x" * 40, "1abc", "has-dash", "admin"):
            with self.assertRaises(qis.Stop, msg=bad):
                qis.check_args(bad, 777, None)
        for bad in (0, 499, 1000, 501):
            if bad == 501:
                continue
            with self.assertRaises(qis.Stop, msg=str(bad)):
                qis.check_args("fornaxqa", bad, None)

    def test_exchange_must_be_direct_child_of_shared(self):
        for bad in ("/Users/Shared", "/tmp/x", "/Users/Shared/a/b", "/Users/Shared/../etc", "relative"):
            with self.assertRaises(qis.Stop, msg=bad):
                qis.check_args("fornaxqa", 777, bad)


class PureChecksTest(unittest.TestCase):
    def test_acl_entries_parse(self):
        out = "drwx------+ 2 u g 64 Oct 30 10:00 /x\n 0: user:fornaxqa allow list,search\n"
        self.assertEqual(qis.acl_entries(out), ["user:fornaxqa allow list,search"])
        self.assertEqual(qis.acl_entries("drwx------ 2 u g 64 Oct 30 10:00 /x\n"), [])

    def test_groups_allowlist(self):
        self.assertEqual(qis.groups_ok({"fornaxqa", "everyone", "localaccounts", "com.apple.sharepoint.group.1"}, "fornaxqa"), [])
        self.assertEqual(qis.groups_ok({"fornaxqa", "admin", "staff"}, "fornaxqa"), ["admin", "staff"])

    def test_good_probe_passes(self):
        self.assertEqual(qis.evaluate_probe(GOOD, 777), [])

    def test_probe_failures_are_each_reported(self):
        cases = {
            "uid": dict(GOOD, uid=501),
            "ctty": dict(GOOD, ctty="ok"),
            "priv group": dict(GOOD, groups=[777, 20]),
            "inbox write allowed": dict(GOOD, write_inbox="ok"),
            "enoent is not a denial": dict(GOOD, list_producer_home="ENOENT"),
            "crash is not a denial": dict(GOOD, open_producer_ssh="EIO"),
            "detector invalid": dict(GOOD, own_env_read="unreadable"),
            "env leaked": dict(GOOD, foreign_env_leaked=True),
            "env unknown": dict(GOOD, foreign_env_leaked=None),
            "outbox not writable": dict(GOOD, write_outbox="EACCES"),
        }
        for label, probe in cases.items():
            self.assertTrue(qis.evaluate_probe(probe, 777), label)

    def test_detector_invalid_blocks_even_when_no_leak(self):
        fails = qis.evaluate_probe(dict(GOOD, own_env_read="unreadable", foreign_env_leaked=False), 777)
        self.assertTrue(any("detector invalid" in f for f in fails))


class CommandShapeTest(unittest.TestCase):
    def test_qa_argv_clears_env_and_detaches(self):
        a = qis.qa_argv("fornaxqa", Path("/Users/fornaxqa"), ["/usr/bin/id"])
        self.assertEqual(a[:5], [qis.SUDO, "-n", "-u", "fornaxqa", "-H"])
        self.assertEqual(a[5:7], ["/usr/bin/env", "-i"])
        self.assertIn("HOME=/Users/fornaxqa", a)
        self.assertIn("-I", a)
        self.assertIn("os.setsid()", a[a.index("-c") + 1])
        self.assertEqual(a[-1], "/usr/bin/id")
        self.assertNotIn("SSH_AUTH_SOCK", " ".join(a))

    def test_login_command_is_a_printed_string_only(self):
        s = qis.Setup("fornaxqa", 777, Path("/Users/fornaxqa"), Path("/Users/Shared/fornaxqa-qa"))
        c = s.login_command()
        self.assertIn("login --device-auth", c)
        self.assertIn("env -i", c)

    def test_login_command_drops_owner_terminal(self):
        # sudo'd login must not inherit the owner's tty (TIOCSTI keystroke injection into the owner's shell)
        s = qis.Setup("fornaxqa", 777, Path("/Users/fornaxqa"), Path("/Users/Shared/fornaxqa-qa"))
        c = s.login_command()
        self.assertIn("os.setsid()", c)
        self.assertIn("-I", c)
        self.assertTrue(c.startswith(qis.SUDO))


class RollbackGuardTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name) / "qauser"
        self.home.mkdir()
        self.s = qis.Setup("qauser", 777, self.home, Path("/Users/Shared/qauser-qa"))

    def tearDown(self):
        self.tmp.cleanup()

    def _run_rollback(self, records):
        calls = []

        def fake_dscl(path, key):
            return records.get((path, key))

        def fake_run(argv, **kw):
            calls.append(list(argv))
            return subprocess.CompletedProcess(argv, 0, "", "")

        with mock.patch.object(qis, "dscl", fake_dscl), mock.patch.object(qis, "run", fake_run), \
                mock.patch.object(qis, "sudo_refresh", lambda: None), mock.patch("builtins.input", return_value="qauser"):
            return calls, self.s.rollback()

    def test_refuses_delete_when_uid_differs(self):
        with self.assertRaises(qis.Stop):
            self._run_rollback({("/Users/qauser", "UniqueID"): "501"})

    def test_refuses_delete_when_home_record_points_elsewhere(self):
        rec = {("/Users/qauser", "UniqueID"): "777", ("/Users/qauser", "PrimaryGroupID"): "777",
               ("/Users/qauser", "NFSHomeDirectory"): "/Users/bryant"}
        with self.assertRaises(qis.Stop):
            self._run_rollback(rec)

    def test_wrong_confirmation_changes_nothing(self):
        calls = []
        with mock.patch.object(qis, "run", lambda argv, **kw: calls.append(argv)), mock.patch("builtins.input", return_value="nope"):
            with self.assertRaises(qis.Stop):
                self.s.rollback()
        self.assertEqual(calls, [])

    def test_only_created_skips_untouched_account(self):
        rec = {("/Users/qauser", "UniqueID"): "777", ("/Users/qauser", "PrimaryGroupID"): "777",
               ("/Users/qauser", "NFSHomeDirectory"): str(self.home), ("/Groups/qauser", "PrimaryGroupID"): "777"}
        calls = []

        def fake_run(argv, **kw):
            calls.append(list(argv))
            return subprocess.CompletedProcess(argv, 0, "", "")

        with mock.patch.object(qis, "dscl", lambda p, k: rec.get((p, k))), mock.patch.object(qis, "run", fake_run), \
                mock.patch.object(qis, "sudo_refresh", lambda: None):
            self.s.rollback(only_created=True)
        self.assertEqual(calls, [])


if __name__ == "__main__":
    unittest.main()
