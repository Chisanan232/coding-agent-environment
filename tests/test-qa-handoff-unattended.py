#!/usr/bin/env python3
"""Offline tests for `qa-handoff.py run --unattended` (SPE-153, ADR 0016 PR 3).

Unprivileged and offline: the installed delegation is a scratch tree owned by the current user (the root-uid constant of
the shared QA-side module is substituted), the QA user is the system `nobody` account whose uid is only compared, and
sudo, exec, signals and process launching are replaced by tripwires or recorders. No model, no keychain, no real sudo."""
import contextlib
import hashlib
import importlib.util
import io
import json
import os
import pwd
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "qa-handoff.py"
spec = importlib.util.spec_from_file_location("qa_handoff_unattended_under_test", SCRIPT)
qa = importlib.util.module_from_spec(spec)
spec.loader.exec_module(qa)

ME = os.getuid()
QA = pwd.getpwnam("nobody")
SIDE = qa._SIDE
ATTEMPT = "prod-T-1-run-ab12cd34"


def forbidden(*_a, **_k):
    raise AssertionError("a process or signal primitive was called")


class Install:
    """<base>/lib/qa-side.json, <base>/lib/qa-side (helper), <base>/lib/codex, exchange <base>/X."""

    def __init__(self):
        self.base = Path(os.path.realpath(tempfile.mkdtemp(prefix="qa-unattended-test-")))
        os.chmod(self.base, 0o755)
        self.lib = self.base / "lib"
        self.lib.mkdir(mode=0o755)
        os.chmod(self.lib, 0o755)
        self.helper = self.lib / "qa-side"
        self.helper.write_bytes(SIDE_SOURCE)
        os.chmod(self.helper, 0o555)
        self.codex = self.lib / "codex"
        self.codex.write_bytes(b"#!/bin/sh\nexit 0\n")
        os.chmod(self.codex, 0o555)
        self.x = self.base / "X"
        (self.x / "inbox").mkdir(parents=True)
        (self.x / "outbox").mkdir()
        self.config = {"qa_name": QA.pw_name, "qa_uid": QA.pw_uid, "producer_uid": ME, "exchange": str(self.x),
                       "codex_path": str(self.codex), "codex_sha256": hashlib.sha256(self.codex.read_bytes()).hexdigest()}
        self.cfg = self.lib / "qa-side.json"
        self.write_config()
        self.ident = qa.Identity(QA.pw_name, QA.pw_uid, Path(QA.pw_dir), self.x, 0)

    def write_config(self):
        if os.path.lexists(self.cfg):
            os.chmod(self.cfg, 0o644)
        self.cfg.write_text(json.dumps(self.config))
        os.chmod(self.cfg, 0o444)

    def verify(self):
        return qa.unattended_install(self.ident, str(self.cfg), str(self.helper), str(self.base))

    def close(self):
        for root, dirs, files in os.walk(self.base):
            for name in dirs + files:
                p = os.path.join(root, name)
                if not os.path.islink(p):
                    os.chmod(p, 0o700)
        shutil.rmtree(self.base, ignore_errors=True)


SIDE_SOURCE = (SCRIPT.parent / "qa-side.py").read_bytes()


class Case(unittest.TestCase):
    def setUp(self):
        stack = contextlib.ExitStack()
        self.addCleanup(stack.close)
        stack.enter_context(mock.patch.object(SIDE, "ROOT_UID", ME))
        self.installs = []
        self.addCleanup(lambda: [i.close() for i in self.installs])

    def install(self):
        i = Install()
        self.installs.append(i)
        return i


class InstallVerificationTests(Case):
    def test_a_good_install_returns_the_recomputed_digests(self):
        i = self.install()
        record, problems = i.verify()
        self.assertEqual(problems, [])
        self.assertEqual(record["helper_sha256"], hashlib.sha256(SIDE_SOURCE).hexdigest())
        self.assertEqual(record["codex_sha256"], i.config["codex_sha256"])
        self.assertEqual(record["helper_path"], str(i.helper))

    def assertFails(self, i):
        record, problems = i.verify()
        self.assertIsNone(record)
        self.assertTrue(problems)
        self.assertNotIn(str(i.base), " ".join(problems))     # messages carry a type, never a path or file content

    def test_config_naming_another_identity_user_or_exchange(self):
        for key, value in (("qa_name", "somebodyelse"), ("qa_uid", QA.pw_uid + 1), ("producer_uid", ME + 1),
                           ("exchange", "/Users/Shared/other-qa")):
            i = self.install()
            i.config[key] = value
            i.write_config()
            self.assertFails(i)

    def test_codex_digest_drift(self):
        i = self.install()
        os.chmod(i.codex, 0o755)
        i.codex.write_bytes(b"tampered")
        os.chmod(i.codex, 0o555)
        self.assertFails(i)

    def test_helper_chain_failures(self):
        i = self.install()
        os.chmod(i.helper, 0o575)
        self.assertFails(i)
        i = self.install()
        os.chmod(i.lib, 0o775)
        self.assertFails(i)
        i = self.install()
        os.chmod(i.base, 0o757)
        self.assertFails(i)
        i = self.install()
        real = i.lib / "real-helper"
        os.rename(i.helper, real)
        os.symlink(real, i.helper)
        self.assertFails(i)
        i = self.install()
        os.unlink(i.helper)
        self.assertFails(i)
        i = self.install()
        moved = i.base / "lib-real"
        os.rename(i.lib, moved)
        os.symlink(moved, i.lib)
        self.assertFails(i)

    def test_not_root_owned_is_refused(self):
        i = self.install()
        with mock.patch.object(SIDE, "ROOT_UID", ME + 1):
            self.assertFails(i)

    def test_config_and_codex_chains_are_both_covered(self):
        i = self.install()
        os.chmod(i.cfg, 0o464)
        self.assertFails(i)
        i = self.install()
        os.chmod(i.codex, 0o557)
        self.assertFails(i)

    def test_missing_files(self):
        i = self.install()
        os.unlink(i.cfg)
        self.assertFails(i)
        i = self.install()
        os.unlink(i.codex)
        self.assertFails(i)


class ArgvTests(Case):
    def ident(self):
        return qa.Identity(QA.pw_name, QA.pw_uid, Path(QA.pw_dir), Path("/Users/Shared/x-qa"), 0)

    def test_every_allowed_shape_becomes_a_fixed_sudo_argv_as_the_qa_user(self):
        qis = qa._qis()
        for verb, shape in SIDE.ALLOWED_ARGV.items():
            attempt = ATTEMPT if SIDE.ATTEMPT_SLOT in shape else None
            argv = qa.helper_argv(self.ident(), verb, attempt)
            self.assertEqual(argv, [qis.SUDO, "-n", "-u", QA.pw_name, SIDE.HELPER_PATH, verb] + ([ATTEMPT] if attempt else []))
            for forbidden_word in ("-H", "-i", "-s", "-E", "-g", "root", "env", "sh", "bash", "--", "-c", "-I"):
                self.assertNotIn(forbidden_word, argv)

    def test_refusals(self):
        ident = self.ident()
        for bad in ("..", "a..b", "-x", "/etc", "a/b", "", "a" * 152, "x\n"):
            with self.assertRaises(qa.Refused, msg=repr(bad)):
                qa.helper_argv(ident, "exec", bad)
        with self.assertRaises(qa.Refused):
            qa.helper_argv(ident, "version", ATTEMPT)
        with self.assertRaises(qa.Refused):
            qa.helper_argv(ident, "exec", None)
        with self.assertRaises(KeyError):
            qa.helper_argv(ident, "bogus", ATTEMPT)
        for uid in (0, ME):
            with self.assertRaises(qa.Refused):
                qa.helper_argv(qa.Identity("x", uid, Path("/Users/x"), Path("/Users/Shared/x-qa"), 0), "exec", ATTEMPT)

    def test_the_allowed_table_covers_exactly_the_helper_verbs(self):
        self.assertEqual(set(SIDE.ALLOWED_ARGV), {"version", "probe", "stage", "exec"})

    def test_helper_hop_maps_refusal_and_silence_to_a_problem(self):
        ident = self.ident()
        for stderr, out, rc, expect in (("sudo: a password is required\n", "", 1, "refused"), ("", "", 2, "no output"),
                                        ("", '{"ok": 1}\n', 3, "exit 3"), ("", '{"ok": 1}\n', 0, None)):
            done = subprocess.CompletedProcess([], rc, stdout=out, stderr=stderr)
            with mock.patch.object(qa.subprocess, "run", return_value=done) as run:
                text, problem = qa.helper_hop(ident, "stage", ATTEMPT, 60)
            self.assertEqual(run.call_args.args[0], qa.helper_argv(ident, "stage", ATTEMPT))
            self.assertEqual(run.call_args.kwargs["stdin"], subprocess.DEVNULL)
            if expect is None:
                self.assertIsNone(problem)
                self.assertEqual(text, out)
            else:
                self.assertIn(expect, problem)
        with mock.patch.object(qa.subprocess, "run", side_effect=subprocess.TimeoutExpired("x", 1)):
            self.assertEqual(qa.helper_hop(ident, "exec", ATTEMPT, 60), ("", "timed out"))


class NoKeepaliveTests(Case):
    def test_no_credential_refresh_no_thread_no_subprocess(self):
        with mock.patch.object(qa.subprocess, "run", side_effect=forbidden), mock.patch.object(qa.subprocess, "Popen", side_effect=forbidden):
            k = qa.NoKeepalive()
            self.assertTrue(k.start())
            self.assertTrue(k.refresh())
            k.stop()
            self.assertEqual(k.summary()["refreshes"], 0)
            self.assertEqual(k.summary()["mode"], "unattended")


class RefusalTests(Case):
    def call(self, *argv):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = qa.main(list(argv))
        return code, out.getvalue()

    def test_unattended_needs_an_identity(self):
        with mock.patch.object(qa.subprocess, "run", side_effect=forbidden):
            code, out = self.call("run", "--run-dir", "/nonexistent", "--unattended")
        self.assertEqual(code, qa.EXIT["REFUSED"])
        self.assertIn("--unattended needs --qa-identity", out)

    def test_timeout_outside_the_helper_range_is_refused_before_anything_runs(self):
        for timeout in ("59", "14401", "0", "-5"):
            with mock.patch.object(qa.subprocess, "run", side_effect=forbidden):
                code, out = self.call("run", "--run-dir", "/nonexistent", "--qa-identity", "nobody", "--unattended", "--timeout", timeout)
            self.assertEqual(code, qa.EXIT["REFUSED"], timeout)
            self.assertIn("--timeout between 60 and 14400", out)

    def test_the_flag_exists_and_defaults_off(self):
        args = qa.build_parser().parse_args(["run", "--run-dir", "x"])
        self.assertFalse(args.unattended)
        self.assertTrue(qa.build_parser().parse_args(["run", "--run-dir", "x", "--unattended"]).unattended)


class RunBase(Case):
    """run_identity_locked with every boundary replaced: what is asked of sudo, what is written, what is never done."""

    def setUp(self):
        super().setUp()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(os.path.realpath(self.tmp.name))
        self.exchange = root / "X"
        (self.exchange / "inbox").mkdir(parents=True)
        (self.exchange / "outbox").mkdir()
        self.run_dir = root / "run"
        self.run_dir.mkdir()
        self.source = root / "source"
        (self.source / ".git").mkdir(parents=True)
        (self.source / "a.txt").write_text("a")
        self.ident = qa.Identity(QA.pw_name, QA.pw_uid, root / "qahome", self.exchange, 0)
        self.packet = {"product": "prod", "ticket": "T-1", "sha": "a" * 40, "criteria": "c", "producer_evidence": "e", "qa_notes": "n"}
        self.args = qa.build_parser().parse_args(["run", "--run-dir", str(self.run_dir), "--unattended", "--qa-identity", QA.pw_name,
                                                  "--timeout", "900"])
        self.calls = []
        self.install_record = {"helper_path": SIDE.HELPER_PATH, "helper_sha256": "1" * 64, "codex_path": "/Library/cae-qa/codex",
                               "codex_sha256": "2" * 64}
        self.profile = "3" * 64
        self.stage_record = {"confinement": {"controls": [{"control": n, "observed": "ALLOWED" if e == "ALLOWED" else "DENIED:1"}
                                                           for n, _, e in qa.CONTROLS]}, "profile_sha256": self.profile}
        self.launched = {}

    def hop(self, ident, verb, attempt, seconds):
        self.calls.append((verb, attempt))
        if verb == "version":
            return json.dumps({"protocol": SIDE.PROTOCOL, "helper_sha256": self.install_record["helper_sha256"],
                               "codex_sha256": self.install_record["codex_sha256"]}), None
        if verb == "stage":
            return json.dumps(self.stage_record) + "\n", None
        raise AssertionError(verb)

    def fake_isolation(self, setup, pw, argv_for=None):
        pid, sentinel = 4242, "qa-sentinel-" + "ab" * 16
        self.launched["probe_argv"] = argv_for(pid, sentinel)
        inbox = self.exchange / "inbox"
        (attempt,) = [p for p in inbox.iterdir() if p.is_dir()]
        self.launched["probe_request"] = json.loads((attempt / "request.json").read_text())
        return {"uid": QA.pw_uid}, []

    def fake_launch(self, args, run_dir, argv, env, cwd, stdin, **kw):
        (attempt,) = [p for p in (self.exchange / "inbox").iterdir() if p.is_dir()]
        self.launched.update(argv=argv, kw=kw, env=env, request=json.loads((attempt / "request.json").read_text()),
                             inbox=sorted(p.name for p in attempt.iterdir()), keepalive_seal=kw.get("seal"))
        return 0

    def patches(self, **over):
        qis = qa._qis()
        table = {
            "unattended_install": mock.patch.object(qa, "unattended_install", return_value=(self.install_record, [])),
            "helper_hop": mock.patch.object(qa, "helper_hop", side_effect=self.hop),
            "hop": mock.patch.object(qa, "hop", side_effect=forbidden),
            "sweep": mock.patch.object(qa, "sweep_identity", return_value=0),
            "persist": mock.patch.object(qa, "persistence_problems", return_value=[]),
            "isolation": mock.patch.object(qis.Setup, "isolation_record", autospec=True, side_effect=self.fake_isolation),
            "launch": mock.patch.object(qa, "launch", side_effect=self.fake_launch),
            "manifest": mock.patch.object(qa, "read_run_json", return_value={}),
            "run": mock.patch.object(qa.subprocess, "run", side_effect=forbidden),
            "popen": mock.patch.object(qa.subprocess, "Popen", side_effect=forbidden),
            "kill": mock.patch.object(qa.os, "kill", side_effect=forbidden),
            "killpg": mock.patch.object(qa.os, "killpg", side_effect=forbidden),
            "execv": mock.patch.object(qa.os, "execv", side_effect=forbidden),
        }
        table.update(over)
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        return {name: self.stack.enter_context(p) for name, p in table.items()}

    def go(self, **over):
        mocks = self.patches(**over)
        out = io.StringIO()
        try:
            with contextlib.redirect_stdout(out):
                code = qa.run_identity_locked(self.args, self.run_dir, self.packet, self.source, self.ident, qa.NoKeepalive())
        finally:
            self.stack.close()
        return code, out.getvalue(), mocks


class RunTests(RunBase):
    def test_the_happy_path_asks_for_exactly_the_fixed_verbs_and_never_for_a_credential(self):
        code, out, mocks = self.go()
        self.assertEqual(code, 0)
        verbs = [v for v, _ in self.calls]
        self.assertEqual(verbs, ["version", "stage"])
        attempt = self.calls[1][1]
        self.assertEqual(self.calls[0][1], None)
        self.assertEqual(self.launched["probe_argv"], qa.helper_argv(self.ident, "probe", attempt))
        self.assertEqual(self.launched["probe_request"], {"verb": "probe", "attempt": attempt, "pid": 4242,
                                                           "sentinel": "qa-sentinel-" + "ab" * 16})
        self.assertEqual(self.launched["argv"], qa.helper_argv(self.ident, "exec", attempt))
        self.assertEqual(self.launched["request"], {"verb": "exec", "attempt": attempt, "timeout": 900, "profile_sha256": self.profile})
        mocks["hop"].assert_not_called()                     # the attended terminal-detaching hop is never used
        self.assertNotIn("qa-side.py", self.launched["inbox"])  # no helper copy is handed over: the pinned install runs
        self.assertIn("packet.json", self.launched["inbox"])
        self.assertIn("prompt.txt", self.launched["inbox"])
        self.assertIn("request.json", self.launched["inbox"])
        self.assertNotIn("source", self.launched["inbox"])     # the clone copy is removed once the QA side has its own
        self.assertEqual(self.launched["env"], qa.HOP_ENV)
        self.assertFalse(self.launched["kw"]["new_session"])   # process ownership/kill semantics as attended

    def test_info_sealed_with_the_run_records_the_helper_identity(self):
        captured = {}
        with mock.patch.object(qa, "seal", side_effect=lambda rd, ident, outbox, manifest, info, proc, ka: captured.update(info=info) or {}):
            self.go()
            self.launched["keepalive_seal"](None)
        info = captured["info"]
        self.assertEqual(info["mode"], "unattended")
        self.assertEqual(info["script_sha256"], self.install_record["helper_sha256"])
        self.assertEqual(info["codex_sha256"], self.install_record["codex_sha256"])

    def test_failed_install_verification_starts_nothing(self):
        code, out, mocks = self.go(unattended_install=mock.patch.object(qa, "unattended_install", return_value=(None, ["bad chain"])))
        self.assertEqual(code, qa.EXIT["QA_INCOMPLETE"])
        self.assertIn("delegated helper not verified", out)
        self.assertEqual(self.calls, [])
        mocks["sweep"].assert_not_called()
        mocks["launch"].assert_not_called()

    def test_version_mismatch_is_incomplete(self):
        original = self.hop

        def bad(ident, verb, attempt, seconds):
            if verb == "version":
                return json.dumps({"protocol": SIDE.PROTOCOL, "helper_sha256": "9" * 64, "codex_sha256": "2" * 64}), None
            return original(ident, verb, attempt, seconds)
        code, out, mocks = self.go(helper_hop=mock.patch.object(qa, "helper_hop", side_effect=bad))
        self.assertEqual(code, qa.EXIT["QA_INCOMPLETE"])
        mocks["sweep"].assert_not_called()
        mocks["launch"].assert_not_called()

    def test_helper_refusal_or_silence_at_version_or_stage_is_incomplete(self):
        def refuse_at(where):
            def hop(ident, verb, attempt, seconds):
                self.calls.append((verb, attempt))
                if verb == where:
                    return "", "produced no output (exit 2)"
                return self.hop(ident, verb, attempt, seconds)
            return hop
        for where in ("version", "stage"):
            self.calls.clear()
            self.run_dir_clean()
            code, out, mocks = self.go(helper_hop=mock.patch.object(qa, "helper_hop", side_effect=refuse_at(where)))
            self.assertEqual(code, qa.EXIT["QA_INCOMPLETE"], where)
            mocks["launch"].assert_not_called()

    def run_dir_clean(self):
        for p in (self.exchange / "inbox").iterdir():
            shutil.rmtree(p, ignore_errors=True)

    def test_leaky_confinement_stops_before_exec(self):
        self.stage_record = {"confinement": {"controls": [{"control": n, "observed": "ALLOWED"} for n, _, _ in qa.CONTROLS]},
                             "profile_sha256": self.profile}
        code, out, mocks = self.go()
        self.assertEqual(code, qa.EXIT["QA_INVALID"])
        mocks["launch"].assert_not_called()

    def test_sweep_failure_before_the_run_is_incomplete(self):
        code, out, mocks = self.go(sweep=mock.patch.object(qa, "sweep_identity", return_value=None))
        self.assertEqual(code, qa.EXIT["QA_INCOMPLETE"])
        mocks["launch"].assert_not_called()


class LowFindingsTests(RunBase):
    def test_a_failed_probe_removes_the_precreated_inbox(self):
        def failing(setup, pw, argv_for=None):
            argv_for(1, "qa-sentinel-" + "ab" * 16)
            return {"uid": QA.pw_uid}, ["write_inbox must be denied"]
        qis = qa._qis()
        code, out, mocks = self.go(isolation=mock.patch.object(qis.Setup, "isolation_record", autospec=True, side_effect=failing))
        self.assertEqual(code, qa.EXIT["QA_INVALID"])
        self.assertEqual(list((self.exchange / "inbox").iterdir()), [])
        mocks["launch"].assert_not_called()

    def test_a_probe_that_did_not_run_also_removes_it(self):
        def silent(setup, pw, argv_for=None):
            argv_for(1, "qa-sentinel-" + "ab" * 16)
            return None, ["probe did not run"]
        qis = qa._qis()
        code, out, mocks = self.go(isolation=mock.patch.object(qis.Setup, "isolation_record", autospec=True, side_effect=silent))
        self.assertEqual(code, qa.EXIT["QA_INCOMPLETE"])
        self.assertEqual(list((self.exchange / "inbox").iterdir()), [])

    def test_a_run_directory_name_that_makes_an_unacceptable_attempt_id_is_incomplete_early(self):
        run_dir = self.run_dir.parent / ("r" * 64)
        run_dir.mkdir()
        packet = dict(self.packet, ticket="T" * 60, product="p" * 40)
        self.patches()
        out = io.StringIO()
        try:
            with contextlib.redirect_stdout(out), mock.patch.object(qa, "exchange_problems", side_effect=forbidden):
                code = qa.run_identity(self.args, run_dir, packet, self.source, self.ident)
        finally:
            self.stack.close()
        self.assertEqual(code, qa.EXIT["QA_INCOMPLETE"])
        self.assertIn("not one the delegated helper accepts", out.getvalue())
        self.assertEqual(self.calls, [])

    def cleanup(self, keepalive, swept):
        out = io.StringIO()
        with mock.patch.object(qa, "sweep_identity", return_value=swept), contextlib.redirect_stdout(out):
            unclean = qa.cleanup_sweep(self.run_dir, self.ident, keepalive)
        return unclean, out.getvalue(), json.loads((self.run_dir / "CLEANUP.json").read_text())

    def test_unattended_cleanup_note_does_not_tell_the_owner_to_run_sudo_v(self):
        unclean, out, note = self.cleanup(qa.NoKeepalive(), None)
        self.assertTrue(unclean)
        self.assertNotIn("sudo -v", out)
        self.assertNotIn("sudo -v", note["note"])
        self.assertIn("delegation", note["note"])

    def test_attended_cleanup_note_is_unchanged(self):
        class Stub:
            def refresh(self):
                return True

            def stop(self):
                pass

            def summary(self):
                return {}
        unclean, out, note = self.cleanup(Stub(), None)
        self.assertTrue(unclean)
        self.assertIn("run `sudo -v` in the owner Terminal", out)
        self.assertIn("run `sudo -v` in the owner Terminal", note["note"])


class AttendedUnchangedTests(Case):
    def test_attended_mode_json_and_keepalive_are_untouched(self):
        text = SCRIPT.read_text()
        self.assertIn("SudoKeepalive()", text)
        args = qa.build_parser().parse_args(["run", "--run-dir", "x", "--qa-identity", "nobody"])
        self.assertFalse(args.unattended)
        # MODE.json gains the key only for an unattended run
        self.assertEqual(text.count("mode['unattended'] = True"), 1)

    def test_stat_is_a_required_runtime_file_for_an_identity_run(self):
        if sys.platform != "darwin":
            self.skipTest("identity runtime tools are macOS only")
        wanted = []
        real_lstat = os.lstat

        def spy(path, *a, **k):
            wanted.append(str(path))
            return real_lstat(path, *a, **k)
        with mock.patch.object(qa.os, "lstat", side_effect=spy):
            qa.runtime_problems(True)
        self.assertIn(qa.STAT, wanted)

    def test_script_hash_is_taken_from_the_bytes_that_were_written(self):
        text = SCRIPT.read_text()
        self.assertIn("hashlib.sha256(SIDE_BYTES).hexdigest()", text)
        self.assertNotIn("sha256_file(script)", text)


class LoginCommandTests(unittest.TestCase):
    def test_login_command_tightens_codex_dir_and_the_next_step_is_check(self):
        spec2 = importlib.util.spec_from_file_location("qis_login", SCRIPT.parent / "qa-identity-setup.py")
        qis = importlib.util.module_from_spec(spec2)
        spec2.loader.exec_module(qis)
        command = qis.Setup("fornaxqa", 777, Path("/Users/fornaxqa"), Path("/Users/Shared/fornaxqa-qa")).login_command()
        self.assertTrue(command.endswith("-n -u fornaxqa /bin/chmod 700 /Users/fornaxqa/.codex"), command)
        self.assertIn("login --device-auth", command)
        text = (SCRIPT.parent / "qa-identity-setup.py").read_text()
        self.assertIn("re-run the check below", text)


if __name__ == "__main__":
    unittest.main()
