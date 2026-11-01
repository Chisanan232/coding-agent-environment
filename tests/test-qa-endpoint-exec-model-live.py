#!/usr/bin/env python3
"""The exec-path self-check with a REAL `codex exec` (SPE-145 PR-b6): one short model call proves that the tool commands of a real Codex see the
QA_ENDPOINT_* paths and can reach the daemon through the relay under the Unix-socket-only profile. Opt in with CAE_QA_LIVE_MODEL=1 and
CAE_QA_LIVE_CODEX=<pinned codex, with codex-code-mode-host beside it>; it spends one short call on your Codex login and skips otherwise.
Everything else (the fake-codex tests) lives in tests/test-qa-endpoint-exec-live.py."""
import importlib.util
import os
import sys
import unittest
from pathlib import Path

TESTS = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("qa_exec_live_base", TESTS / "test-qa-endpoint-exec-live.py")
base = importlib.util.module_from_spec(spec)
spec.loader.exec_module(base)
side, usable = base.side, base.usable


CODEX = os.environ.get("CAE_QA_LIVE_CODEX", "")
MODEL = os.environ.get("CAE_QA_LIVE_MODEL") == "1"


@unittest.skipUnless(usable() and MODEL and CODEX and os.access(CODEX, os.X_OK) and (Path.home() / ".codex" / "auth.json").exists(),
                     "set CAE_QA_LIVE_CODEX (pinned Codex, with codex-code-mode-host beside it) and CAE_QA_LIVE_MODEL=1: one short model call on your Codex login")
class EndpointExecRealCodexTests(base.EndpointExecLiveTests):
    """The exec-path self-check: a REAL `codex exec` (one short model call) must see the QA_ENDPOINT_* paths in its tool commands and be able to
    reach the daemon through the relay under the Unix-socket-only profile. Inherits the setUp (and, deliberately, none of the fake-codex tests)."""

    def setUp(self):
        real_login = Path.home() / ".codex" / "auth.json"
        original = Path.write_text

        def link_login(path, data, *a, **k):
            if path.name == "auth.json" and ".cae-exec-live-" in str(path):
                os.symlink(real_login, path)                 # the login as a link only: never read here
                return len(data)
            return original(path, data, *a, **k)
        Path.write_text = link_login
        try:
            super().setUp()
        finally:
            Path.write_text = original
        command = ("python3 -c \"import os,socket;print(os.environ.get('QA_ENDPOINT_HTTP_SOCK','UNSET'));s=socket.socket(socket.AF_UNIX);"
                   "s.connect(os.environ['QA_ENDPOINT_HTTP_SOCK']);s.sendall(b'GET / HTTP/1.0\\\\r\\\\n\\\\r\\\\n');print(s.recv(100))\"")
        (self.ex / "inbox" / "att" / "prompt.txt").write_text("Run exactly one shell command and report its output; run nothing else.\nCommand: %s\n"
                                                              "Then reply with the final verdict JSON, with the command's stdout in the summary.\n" % command)
        side.write_private(self.outbox / "verdict.schema.json", side.VERDICT_SCHEMA)
        self.args.codex_bin, self.args.timeout = CODEX, 300

    def test_a_real_codex_exec_reaches_the_daemon_through_the_relay(self):
        events = self.outbox / "events.out"
        pid = os.fork()
        if pid == 0:
            code = 98
            try:
                os.dup2(os.open(events, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), 1)
                side.HOME = self.qa_home
                side.real_codex_home = lambda: self.qa_home / ".codex"
                side.CANARY_ROOT = self.root / "canary"
                code = side.qa_exec(self.args, build_env=lambda: {"PATH": "/usr/bin:/bin", "HOME": str(self.qa_home), "TMPDIR": str(self.outbox / "tmp"),
                                                                  "CODEX_HOME": self.args.codex_home})
            finally:
                os._exit(code & 0xFF)
        _, status = os.waitpid(pid, 0)
        self.assertEqual(os.waitstatus_to_exitcode(status), 0, (self.outbox / "stderr.log").read_text()[-400:])
        text = events.read_text()
        self.assertIn(str(self.endpoint.relay_sock), text)          # the variable reached the tool command
        self.assertIn("HTTP/1.1 200 OK", text)                       # and the daemon answered through the relay
        self.assertTrue(self.record()["established"])

    # the fake-codex tests of the parent class are not repeated here
    test_the_daemon_is_up_for_codex_its_state_is_kept_and_everything_is_torn_down = None
    test_a_wrong_manifest_or_daemon_profile_digest_stops_before_anything_starts = None
    test_a_changed_profile_still_stops_codex_first = None
    test_a_daemon_lost_in_the_last_moments_is_still_caught = None
    test_a_daemon_that_dies_while_codex_runs_stops_codex = None


if __name__ == "__main__":
    unittest.main()
