#!/usr/bin/env python3
"""qa-exec with a daemon endpoint (SPE-145 PR-b5), run for real on macOS with a stand-in daemon and a FAKE `codex` (a script that answers
over the endpoint's relay socket): the daemon and relay start under their generated sandbox-exec profiles before the fake codex, the fake
codex sees the QA_ENDPOINT_* paths, the endpoint is supervised while it runs, the daemon's state is copied to the outbox before teardown,
and a lost endpoint stops codex with ENDPOINT_FAILED. No Codex, no model, no network, no privilege. The scratch tree is under the home
directory because the profiles deny the shared temporary areas."""
import hashlib
import importlib.util
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
import types
import unittest
from pathlib import Path
from unittest import mock

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
spec = importlib.util.spec_from_file_location("qa_side_exec_live_test", SCRIPTS / "qa-side.py")
side = importlib.util.module_from_spec(spec)
spec.loader.exec_module(side)

STAND_IN = '''#!/usr/bin/python3 -I
import os, socket, sqlite3, threading
st, port = os.environ["FORNAX_HOME"], int(os.environ["FORNAX_HTTP_PORT"])
db = sqlite3.connect(st + "/fornax.db"); db.execute("create table if not exists t(x)"); db.execute("insert into t values('row')"); db.commit()
def http():
    s = socket.socket(); s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1); s.bind(("127.0.0.1", port)); s.listen(8)
    while True:
        c, _ = s.accept(); c.recv(65536); c.sendall(b"HTTP/1.1 200 OK\\r\\nContent-Length: 2\\r\\nConnection: close\\r\\n\\r\\nok"); c.close()
def uds():
    os.chdir(st); u = socket.socket(socket.AF_UNIX); u.bind("fornax.sock"); u.listen(8)
    while True:
        c, _ = u.accept(); c.recv(65536); c.close()
threading.Thread(target=http, daemon=True).start(); uds()
'''
FAKE_CODEX = '''#!/usr/bin/python3 -I
import os, signal, socket, sys, time
sys.stdin.read()
out = os.path.join(os.path.dirname(os.getcwd()), "out")
s = socket.socket(socket.AF_UNIX); s.connect(os.environ["QA_ENDPOINT_HTTP_SOCK"]); s.sendall(b"GET / HTTP/1.0\\r\\n\\r\\n")
data = b""
while True:
    chunk = s.recv(4096)
    if not chunk:
        break
    data += chunk
open(os.path.join(out, "fake-codex.txt"), "w").write(data.split(b"\\r\\n\\r\\n", 1)[1].decode() + "|" + os.environ["QA_ENDPOINT_STATE"])
if os.environ.get("FAKE_CODEX_WAIT"):
    time.sleep(float(os.environ["FAKE_CODEX_WAIT"]))
'''
SB = "/usr/bin/sandbox-exec"


def usable():
    return sys.platform == "darwin" and os.path.exists(SB)


def free_port():
    for _ in range(50):
        port = 20000 + int.from_bytes(os.urandom(2), "big") % 12000
        with socket.socket() as probe:
            if probe.connect_ex(("127.0.0.1", port)) != 0:
                return port
    raise unittest.SkipTest("no free port in the endpoint range")


@unittest.skipUnless(usable(), "needs macOS with sandbox-exec")
class EndpointExecLiveTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix=".cae-exec-live-", dir=str(Path.home())))
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.qa_home, self.ex = self.root / "qa", self.root / "x"
        (self.qa_home / ".codex").mkdir(parents=True)
        (self.qa_home / ".codex" / "auth.json").write_text("synthetic-login")
        att = self.ex / "outbox" / "att"
        for sub in ("out", "tmp", "home", "cargo-target", "source"):
            (att / sub).mkdir(parents=True)
        (self.ex / "inbox" / "att").mkdir(parents=True)
        (self.ex / "inbox" / "att" / "prompt.txt").write_text("review\n")
        src = self.root / "payload-src"
        (src / "bin").mkdir(parents=True)
        (src / "bin" / "daemon").write_text(STAND_IN)
        (src / "bin" / "daemon").chmod(0o755)
        daemon = (src / "bin" / "daemon").read_bytes()
        manifest = {"schema": "cae-qa-payload/2", "files": [{"path": "bin/daemon", "sha256": hashlib.sha256(daemon).hexdigest(), "size": len(daemon), "kind": "exec"}],
                    "selftests": [], "scope": "offline library-level checks", "producer_run": [],
                    "endpoint": {"daemon": "bin/daemon", "contract": "fornax-daemon/1"}}
        self.raw = json.dumps(manifest, sort_keys=True).encode()
        (src / "MANIFEST.json").write_bytes(self.raw)
        (src / "MANIFEST.json").chmod(0o644)
        self.manifest = side.parse_manifest(self.raw)
        fd = os.open(src, os.O_RDONLY | os.O_DIRECTORY)
        side.copy_payload(fd, str(att / "payload"), self.manifest, self.raw, side.OUTBOX_MODES)
        os.close(fd)
        self.fake = self.root / "fake-codex"
        self.fake.write_text(FAKE_CODEX)
        self.fake.chmod(0o755)
        self.port = free_port()
        self.outbox = att
        self.endpoint = side.Endpoint(str(self.qa_home), str(self.ex), "att", self.port, "fornax-daemon/1", str(att / "payload"), str(att / "payload/bin/daemon"))
        with mock.patch.object(side, "HOME", self.qa_home), mock.patch.object(side, "real_codex_home", lambda: self.qa_home / ".codex"):
            home = side.prepare_codex_home(att, [att / "payload"], home=self.root / "codexhome", endpoint=self.endpoint)
        self.profile = hashlib.sha256((home / "config.toml").read_bytes()).hexdigest()
        self.args = types.SimpleNamespace(inbox=str(self.ex / "inbox" / "att"), outbox=str(att), codex_home=str(home), codex_bin=str(self.fake),
                                          timeout=60, profile_sha256=self.profile, network="daemon-endpoint", port=self.port, qa_home=str(self.qa_home),
                                          exchange=str(self.ex), attempt="att", manifest_sha256=side.manifest_sha256(self.raw),
                                          daemon_profile_sha256=side.endpoint_digests(self.endpoint)["daemon_profile_sha256"])

    def run_exec(self, **env):
        """qa_exec in a forked child: it moves fd 2 to the outbox, installs signal handlers and umask, none of which belongs in this process."""
        pid = os.fork()
        if pid == 0:
            code = 99
            try:
                side.HOME = self.qa_home
                side.real_codex_home = lambda: self.qa_home / ".codex"
                side.CANARY_ROOT = self.root / "canary"
                code = side.qa_exec(self.args, build_env=lambda: {"PATH": "/usr/bin:/bin", **env})
            except BaseException:
                code = 98
            finally:
                os._exit(code & 0xFF)
        _, status = os.waitpid(pid, 0)
        return os.waitstatus_to_exitcode(status)

    def record(self):
        return json.loads((self.outbox / "endpoint-exec" / "record.json").read_text())

    def test_the_daemon_is_up_for_codex_its_state_is_kept_and_everything_is_torn_down(self):
        code = self.run_exec()
        self.assertEqual(code, 0, (self.outbox / "stderr.log").read_text())
        answer, state = (self.outbox / "out" / "fake-codex.txt").read_text().split("|")
        self.assertEqual(answer, "ok")
        self.assertEqual(state, str(self.endpoint.state))
        record = self.record()
        self.assertTrue(record["established"], record)
        self.assertTrue(record["teardown"]["clean"], record["teardown"])
        self.assertTrue(record["teardown"]["state_snapshot"]["copied"])
        self.assertTrue((self.outbox / "endpoint-exec" / "state" / "fornax.db").exists())
        self.assertFalse(os.path.lexists(self.endpoint.dir))
        with socket.socket() as probe:
            self.assertNotEqual(probe.connect_ex(("127.0.0.1", self.port)), 0)

    def test_a_wrong_manifest_or_daemon_profile_digest_stops_before_anything_starts(self):
        for field in ("manifest_sha256", "daemon_profile_sha256"):
            self.setUp()
            setattr(self.args, field, "0" * 64)
            self.assertEqual(self.run_exec(), side.ENDPOINT_FAILED, field)
            self.assertFalse((self.outbox / "out" / "fake-codex.txt").exists(), field)
            self.assertFalse(os.path.lexists(self.endpoint.dir), field)

    def test_a_changed_profile_still_stops_codex_first(self):
        self.args.profile_sha256 = "0" * 64
        self.assertEqual(self.run_exec(), side.PROFILE_CHANGED)
        self.assertFalse((self.outbox / "out" / "fake-codex.txt").exists())

    def test_a_daemon_lost_in_the_last_moments_is_still_caught(self):
        """The loop polls every two seconds; codex returning inside that window must not hide a dead daemon (the final check)."""
        killer = subprocess.Popen([sys.executable, "-I", "-c",
                                   "import os,signal,subprocess,time\nfor _ in range(300):\n    out = subprocess.run(['/usr/sbin/lsof','-nP','-iTCP:%d','-sTCP:LISTEN','-Fp'],capture_output=True,text=True).stdout\n    pids=[int(l[1:]) for l in out.split() if l.startswith('p')]\n    if pids:\n        time.sleep(0.4)\n        os.kill(pids[0], signal.SIGKILL)\n        break\n    time.sleep(0.05)\n" % self.port])
        self.addCleanup(killer.wait)
        code = self.run_exec(FAKE_CODEX_WAIT="1.2")
        self.assertEqual(code, side.ENDPOINT_FAILED)
        record = self.record()
        self.assertFalse(record["established"])
        self.assertTrue(record["daemon_exited_early"] or record.get("port_proof_lost"), record)

    def test_a_daemon_that_dies_while_codex_runs_stops_codex(self):
        killer = subprocess.Popen([sys.executable, "-I", "-c",
                                   "import os,signal,subprocess,time\nfor _ in range(300):\n    out = subprocess.run(['/usr/sbin/lsof','-nP','-iTCP:%d','-sTCP:LISTEN','-Fp'],capture_output=True,text=True).stdout\n    pids=[int(l[1:]) for l in out.split() if l.startswith('p')]\n    if pids:\n        time.sleep(1.0)\n        os.kill(pids[0], signal.SIGKILL)\n        break\n    time.sleep(0.1)\n" % self.port])
        self.addCleanup(killer.wait)
        code = self.run_exec(FAKE_CODEX_WAIT="30")
        self.assertEqual(code, side.ENDPOINT_FAILED)
        record = self.record()
        self.assertFalse(record["established"])
        self.assertTrue(any("stopped holding" in p for p in record["problems"]), record["problems"])
        self.assertTrue(record["teardown"]["clean"], record["teardown"])


if __name__ == "__main__":
    unittest.main()
