#!/usr/bin/env python3
"""The endpoint lifecycle with its REAL operations on macOS (SPE-145 PR-b3): sandbox-exec, lsof, real sockets, a stand-in daemon and the
generated relay. Proves the whole sequence on this machine: directories, all profile controls, start, loopback-only port proof, relay HTTP,
supervision, teardown with the socket the stand-in leaves behind. Skips without sandbox-exec, lsof and /usr/bin/python3. No model, no
network beyond loopback, no privilege. The real candidate daemon and the QA user are covered by the owner demo."""
import importlib.util
import os
import shutil
import socket
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("qa_side_lifecycle_live", HERE.parent / "scripts" / "qa-side.py")
side = importlib.util.module_from_spec(spec)
spec.loader.exec_module(side)
spec = importlib.util.spec_from_file_location("endpoint_live_helpers", HERE / "test-qa-endpoint-live.py")
live = importlib.util.module_from_spec(spec)
spec.loader.exec_module(live)


@unittest.skipUnless(live.sandbox_usable() and os.path.exists("/usr/sbin/lsof"), "needs macOS with sandbox-exec, lsof and /usr/bin/python3")
class RealLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix=".cae-lifecycle-live-", dir=str(Path.home())))
        self.addCleanup(shutil.rmtree, self.root, True)
        self.home, self.ex = self.root / "qahome", self.root / "exchange"
        self.pay = self.ex / "outbox" / "att-1" / "payload"
        (self.pay / "bin").mkdir(parents=True)
        self.home.mkdir(mode=0o700)
        (self.ex / "inbox").mkdir()
        (self.home / ".codex").mkdir()
        (self.home / ".codex" / "auth.json").write_text("synthetic-login")
        (self.home / "canary.txt").write_text("synthetic-canary")
        (self.pay / "bin" / "daemon").write_text(live.STAND_IN)
        (self.pay / "bin" / "daemon").chmod(0o755)
        self.port = live.free_port()
        self.e = side.Endpoint(str(self.home), str(self.ex), "att-1", self.port, "fornax-daemon/1", str(self.pay), str(self.pay / "bin" / "daemon"))
        self.life = side.EndpointLifecycle(self.e, self.ex / "outbox" / "att-1" / "endpoint")
        self.addCleanup(self.life.teardown)

    def test_the_whole_lifecycle_with_real_operations(self):
        self.life.create()
        self.life.prove_profiles(self.home / "canary.txt", self.home / ".codex" / "auth.json", self.ex / "inbox")
        failed = [c for c in self.life.record["controls"] if not c["ok"]]
        self.assertEqual(failed, [])
        self.assertGreater(len(self.life.record["controls"]), 35)
        self.assertEqual(self.life.record["daemon_started"], False)           # nothing of the candidate ran during the proof
        self.life.start()
        self.assertEqual(self.life.record["port_proof"]["listeners"], ["127.0.0.1:%d" % self.port])
        self.assertTrue(self.life.supervise())
        s = socket.socket(socket.AF_UNIX)
        s.settimeout(10)
        cwd = os.getcwd()
        try:
            os.chdir(self.e.rl)
            s.connect(self.e.relay_sock.name)
        finally:
            os.chdir(cwd)
        s.sendall(b"GET / HTTP/1.0\r\n\r\n")
        self.assertTrue(s.recv(100).startswith(b"HTTP/1.1 200"))
        s.close()
        steps = self.life.teardown()
        self.assertTrue(steps["clean"], steps)
        self.assertFalse(self.e.dir.exists())
        self.assertNotEqual(socket.socket().connect_ex(("127.0.0.1", self.port)), 0)

    def test_a_wildcard_listener_is_caught_by_the_port_proof(self):
        """The sandbox rule `localhost:P` also admits 0.0.0.0, so a daemon that binds the wildcard passes the profile and must fail the proof."""
        (self.pay / "bin" / "daemon").write_text(live.STAND_IN.replace('s.bind(("127.0.0.1", port))', 's.bind(("0.0.0.0", port))'))
        self.life.create()
        self.life.prove_profiles(self.home / "canary.txt", self.home / ".codex" / "auth.json", self.ex / "inbox")
        with self.assertRaises(side.EndpointFailed) as caught:
            self.life.start()
        self.assertIn("127.0.0.1", str(caught.exception))
        self.assertIsNone(self.life.relay)


if __name__ == "__main__":
    unittest.main()
