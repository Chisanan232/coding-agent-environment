#!/usr/bin/env python3
"""A daemon-endpoint stage run end to end with a REAL `codex sandbox` (SPE-145 PR-b4): the UDS-only Codex profile, the base confinement
probe, the generated daemon and relay profiles, a stand-in daemon, the Codex-side endpoint controls and a needs_endpoint self-test.
No model runs. Opt in with CAE_QA_LIVE_CODEX=/path/to/codex (the pinned 0.163.0 copy); it skips without it, on a non-macOS host and
without a Codex login, because the run needs all three. It runs as the current user, so it proves the confinement design but not the
QA account (that is the owner demo)."""
import hashlib
import importlib.util
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
spec = importlib.util.spec_from_file_location("qa_side_stage_live_test", SCRIPTS / "qa-side.py")
side = importlib.util.module_from_spec(spec)
spec.loader.exec_module(side)

CODEX = os.environ.get("CAE_QA_LIVE_CODEX", "")
STAND_IN = '''#!/usr/bin/python3 -I
import os, socket, threading
st, port = os.environ["FORNAX_HOME"], int(os.environ["FORNAX_HTTP_PORT"])
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
PROBE = '''#!/usr/bin/python3 -I
import os, socket
s = socket.socket(socket.AF_UNIX); s.connect(os.environ["QA_ENDPOINT_HTTP_SOCK"])
s.sendall(b"GET / HTTP/1.0\\r\\n\\r\\n")
data = b""
while True:
    chunk = s.recv(4096)
    if not chunk:
        break
    data += chunk
print(data.split(b"\\r\\n\\r\\n", 1)[1].decode())
'''


def free_port():
    for _ in range(50):
        port = 20000 + int.from_bytes(os.urandom(2), "big") % 12000
        with socket.socket() as probe:
            if probe.connect_ex(("127.0.0.1", port)) != 0:
                return port
    raise unittest.SkipTest("no free port in the endpoint range")


def usable():
    return sys.platform == "darwin" and CODEX and os.access(CODEX, os.X_OK) and (side.real_codex_home() / "auth.json").exists()


@unittest.skipUnless(usable(), "set CAE_QA_LIVE_CODEX to a Codex binary on macOS with a Codex login")
class EndpointStageLiveTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix=".cae-stage-live-", dir=str(Path.home())))
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.qa_home, self.ex = self.root / "qa", self.root / "x"
        att = self.ex / "inbox" / "att"
        (att / "source").mkdir(parents=True)
        (self.qa_home / ".codex").mkdir(parents=True)
        os.symlink(side.real_codex_home() / "auth.json", self.qa_home / ".codex" / "auth.json")      # the login, as a link only: never read here
        patches = [mock.patch.object(side, "HOME", self.qa_home), mock.patch.object(side, "real_codex_home", lambda: self.qa_home / ".codex")]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)
        subprocess.run(["git", "init", "-q", str(att / "source")], check=True)
        subprocess.run(["git", "-C", str(att / "source"), "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "--allow-empty", "-m", "x"], check=True)
        self.files = {"bin/daemon": (STAND_IN.encode(), "exec"), "bin/probe": (PROBE.encode(), "exec"), "harness/transcript.txt": (b"t\n", "data")}
        payload = att / "payload"
        entries = []
        for path, (data, kind) in sorted(self.files.items()):
            target = payload / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
            target.chmod(0o755 if kind == "exec" else 0o644)
            entries.append({"path": path, "sha256": hashlib.sha256(data).hexdigest(), "size": len(data), "kind": kind})
        test = {"id": "http", "argv": ["bin/probe"], "stdin": None, "expect_exit": 0, "expect_stdout_sha256": hashlib.sha256(b"ok\n").hexdigest(),
                "timeout_s": 30, "max_output_bytes": 4096, "needs_endpoint": True}
        manifest = {"schema": "cae-qa-payload/2", "files": entries, "selftests": [test], "scope": "offline library-level checks",
                    "producer_run": ["harness/transcript.txt"], "endpoint": {"daemon": "bin/daemon", "contract": "fornax-daemon/1"}}
        (payload / "MANIFEST.json").write_text(json.dumps(manifest, sort_keys=True))
        (payload / "MANIFEST.json").chmod(0o644)
        self.args = types.SimpleNamespace(inbox=str(att), outbox=str(self.ex / "outbox" / "att"), codex_home=str(self.root / "codexhome"), codex_bin=CODEX,
                                          network="daemon-endpoint", port=free_port(), qa_home=str(self.qa_home), exchange=str(self.ex), attempt="att")
        (self.ex / "outbox").mkdir()

    def test_an_endpoint_run_is_proved_served_and_torn_down(self):
        result = side.stage_outbox(self.args)
        self.assertTrue(result["confinement"]["established"], result["confinement"])
        endpoint = result["endpoint"]
        self.assertTrue(endpoint["established"], endpoint)
        self.assertEqual([c["control"] for c in endpoint["codex_controls"]], [c[0] for c in side.RELAY_CODEX_CONTROLS])
        self.assertTrue(all(c["ok"] for c in endpoint["codex_controls"]), endpoint["codex_controls"])
        self.assertTrue(all(c["ok"] for c in endpoint["controls"]))
        self.assertEqual(len(endpoint["selftests"]), 1)
        self.assertEqual(endpoint["selftests"][0]["exit"], 0)
        self.assertEqual(endpoint["selftests"][0]["stdout_sha256"], hashlib.sha256(b"ok\n").hexdigest())
        self.assertTrue(endpoint["teardown"]["clean"], endpoint["teardown"])
        self.assertFalse(os.path.lexists(self.args.qa_home + "/e/" + os.listdir(self.args.qa_home + "/e")[0]) if os.listdir(self.args.qa_home + "/e") else False)
        with socket.socket() as probe:
            self.assertNotEqual(probe.connect_ex(("127.0.0.1", self.args.port)), 0)

    def test_a_payload_that_names_an_endpoint_is_refused_in_none_mode(self):
        self.args.network = "none"
        result = side.stage_outbox(self.args)
        self.assertIn("not started with one", result["payload"]["problems"][0])
        self.assertNotIn("endpoint", result)


if __name__ == "__main__":
    unittest.main()
