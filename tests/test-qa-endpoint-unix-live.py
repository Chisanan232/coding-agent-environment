#!/usr/bin/env python3
"""A circinus-daemon/1 (unix-socket) endpoint for real (SPE-155): a stand-in daemon that speaks the Circinus ping protocol (read to the
client's half-close, one JSON line back, then close) runs under the REAL generated daemon.sb via sandbox-exec, with its state seeded from
the payload and a payload extension module (mode 0444) loaded by dlopen; a full stage run then puts the REAL `codex sandbox -P` profile in
front of it (the Codex-side controls, among them the product's exact ping with half-close, and a needs_endpoint self-test that finds the
socket through XDG_STATE_HOME). It also MEASURES what getsockopt(SOL_LOCAL, LOCAL_PEERPID) reports for three listener shapes of the
daemon's process group; the result is recorded in ADR 0016. No model runs. Opt in with CAE_QA_LIVE_CODEX=/path/to/codex (the pinned
0.163.0 copy); it skips without it, on a non-macOS host and without a Codex login. It runs as the current user, so it proves the design,
not the QA account (that is the owner demo)."""
import hashlib
import importlib.util
import json
import os
import shutil
import socket
import struct
import subprocess
import sys
import tempfile
import time
import types
import unittest
from pathlib import Path
from unittest import mock

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
spec = importlib.util.spec_from_file_location("qa_side_unix_live_test", SCRIPTS / "qa-side.py")
side = importlib.util.module_from_spec(spec)
spec.loader.exec_module(side)

CODEX = os.environ.get("CAE_QA_LIVE_CODEX", "")
EXTENSION = side.TRUSTED_PYTHON_TREE + "/lib/python3.9/lib-dynload/_bisect.cpython-39-darwin.so"
SEED = b'{"mode": "shadow", "seeded": true}\n'
# The daemon is the payload's "interpreter": it is started as <bin/python3.13> -I -B -c <bootstrap> daemon-run and refuses anything else.
# /usr/bin/python3 resolves to the Command Line Tools 3.9 interpreter (DEVELOPER_DIR is pinned), so the copied 3.9 extension module loads.
STAND_IN = r'''#!/usr/bin/python3 -I
import hashlib, importlib.machinery, importlib.util, json, os, socket, sys
SHAPE, BOOT = %r, %r
if sys.argv[1:] != ["-I", "-B", "-c", BOOT, "daemon-run"]:
    sys.exit(3)
st = os.environ["XDG_STATE_HOME"]
d = os.path.join(st, "circinus")
os.makedirs(d, mode=0o700, exist_ok=True)
extra = {}
with open(os.path.join(d, "mode.json"), "rb") as seeded:
    extra["seed_sha256"] = hashlib.sha256(seeded.read()).hexdigest()
lib = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(sys.argv[0]))), "lib", "_bisect.cpython-39-darwin.so")
loader = importlib.machinery.ExtensionFileLoader("_bisect", lib)
extra["dylib"] = loader.create_module(importlib.util.spec_from_loader("_bisect", loader)).bisect_left([1, 2, 3], 2)
def serve(s):
    while True:
        c, _ = s.accept()
        data = b""
        while len(data) < (4 << 20):
            chunk = c.recv(65536)
            if not chunk:
                break
            data += chunk
        if data == b'{"event": "ping"}\n':
            c.sendall((json.dumps(dict({"ok": True, "mode": "shadow"}, **extra)) + "\n").encode())
        c.close()
def listen():
    os.chdir(d)
    s = socket.socket(socket.AF_UNIX)
    s.bind("circinusd.sock")
    os.chmod("circinusd.sock", 0o600)
    s.listen(8)
    return s
if SHAPE == "leader":
    serve(listen())
elif SHAPE == "child":
    if os.fork() == 0:
        serve(listen())
    os.wait()
else:
    s = listen()
    if os.fork() == 0:
        serve(s)
    s.close()
    os.wait()
'''
# Inside the Codex sandbox: dial the socket the way `circinus hook` does (by the XDG_STATE_HOME path), and report what the state holds.
PROBE = r'''#!/usr/bin/python3 -I
import json, os, socket
st = os.environ["XDG_STATE_HOME"]
s = socket.socket(socket.AF_UNIX)
s.connect(os.path.join(st, "circinus", "circinusd.sock"))
s.sendall(b'{"event": "ping"}\n')
s.shutdown(socket.SHUT_WR)
data = b""
while True:
    chunk = s.recv(4096)
    if not chunk:
        break
    data += chunk
reply = json.loads(data)
print(json.dumps({"ok": reply["ok"], "seed": reply.get("seed_sha256"), "dylib": reply.get("dylib"), "top": sorted(os.listdir(st)),
                  "circinus": sorted(os.listdir(os.path.join(st, "circinus")))}, sort_keys=True))
'''


def usable():
    return sys.platform == "darwin" and CODEX and os.access(CODEX, os.X_OK) and (side.real_codex_home() / "auth.json").exists()


def peer_pid(conn):
    return struct.unpack("i", conn.getsockopt(side.SOL_LOCAL, side.LOCAL_PEERPID, 4))[0]


class Base(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix=".cae-unix-live-", dir=str(Path.home())))
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.qa_home, self.ex = self.root / "qa", self.root / "x"
        (self.qa_home / ".codex").mkdir(parents=True)
        os.symlink(side.real_codex_home() / "auth.json", self.qa_home / ".codex" / "auth.json")      # the login, as a link only: never read here
        for patch in (mock.patch.object(side, "HOME", self.qa_home), mock.patch.object(side, "real_codex_home", lambda: self.qa_home / ".codex"),
                      mock.patch.object(side, "CANARY_ROOT", self.qa_home / ".local/state/coding-agent-environment/qa-canary")):
            patch.start()
            self.addCleanup(patch.stop)

    def write_payload(self, payload, shape="leader"):
        files = {"bin/python3.13": ((STAND_IN % (shape, side.CIRCINUS_BOOTSTRAP)).encode(), "exec"), "bin/probe": (PROBE.encode(), "exec"),
                 "harness/transcript.txt": (b"t\n", "data"), "lib/_bisect.cpython-39-darwin.so": (Path(EXTENSION).read_bytes(), "data"),
                 "state-seed/circinus/mode.json": (SEED, "data")}
        entries = []
        for path, (data, kind) in sorted(files.items()):
            target = payload / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
            target.chmod(0o755 if kind == "exec" else 0o644)
            entries.append({"path": path, "sha256": hashlib.sha256(data).hexdigest(), "size": len(data), "kind": kind})
        return entries


@unittest.skipUnless(usable(), "set CAE_QA_LIVE_CODEX to a Codex binary on macOS with a Codex login")
class PeerMeasurementLiveTests(Base):
    """The stand-in under the real daemon.sb, outside Codex, in three listener shapes: the socket proof must hold in each, and the
    measured LOCAL_PEERPID values are printed (the ADR records them)."""

    def lifecycle(self, shape):
        payload = self.ex / "outbox" / shape / "payload"
        entries = self.write_payload(payload, shape)
        for entry in entries:                                        # what the outbox copy looks like: exec 0555, data 0444
            (payload / entry["path"]).chmod(0o555 if entry["kind"] == "exec" else 0o444)
        manifest = {"files": entries, "endpoint": {"daemon": "bin/python3.13", "contract": "circinus-daemon/1"}}
        endpoint = side.Endpoint(str(self.qa_home), str(self.ex), shape, 0, "circinus-daemon/1", str(payload), str(payload / "bin/python3.13"))
        life = side.EndpointLifecycle(endpoint, self.ex / "outbox" / shape / "endpoint", manifest=manifest)
        self.addCleanup(life.teardown)
        return life, endpoint

    def test_the_socket_proof_holds_and_local_peerpid_is_measured_for_each_shape(self):
        canary = self.root / "canary.txt"
        canary.write_text("synthetic")
        decoy_dir = self.root / "d"
        decoy_dir.mkdir()
        rows = []
        for shape in ("leader", "child", "accept-child"):
            life, e = self.lifecycle(shape)
            life.create()
            self.assertEqual(life.record["seeded"], [{"path": "circinus/mode.json", "sha256": hashlib.sha256(SEED).hexdigest()}])
            if shape == "leader":                                    # once is enough to show the generated profile's controls pass here
                life.prove_profiles(canary, self.qa_home / ".codex" / "auth.json", decoy_dir)
                self.assertTrue(all(c["ok"] for c in life.record["controls"]), [c for c in life.record["controls"] if not c["ok"]])
                life.release_probes()
            life.start()
            proof = life.record["socket_proof"]
            daemon = life.daemon.pid
            self.assertEqual((proof["peer_pgid"], proof["daemon_pid"], proof["peer_uid"], proof["inet"]), (daemon, daemon, os.getuid(), []))
            with socket.socket(socket.AF_UNIX) as conn:
                conn.settimeout(10)
                conn.connect(str(e.daemon_sock))
                at_connect = peer_pid(conn)
                conn.sendall(side.PING_REQUEST)
                time.sleep(0.5)                                      # the daemon has accepted and read, and waits for the half-close
                after_io = peer_pid(conn)
                conn.shutdown(socket.SHUT_WR)
                reply = b""
                while True:
                    chunk = conn.recv(65536)
                    if not chunk:
                        break
                    reply += chunk
            doc = json.loads(reply)
            self.assertEqual((doc["ok"], doc["mode"], doc["dylib"], doc["seed_sha256"]), (True, "shadow", 1, hashlib.sha256(SEED).hexdigest()))
            rows.append((shape, daemon, at_connect, after_io, os.getpgid(at_connect)))
            self.assertTrue(life.supervise(final=True))
            steps = life.teardown()
            self.assertTrue(steps["clean"], steps)
        sys.stderr.write("\nLOCAL_PEERPID under daemon.sb (shape, daemon pid, at connect, after the daemon's I/O, pgid of the peer):\n")
        for row in rows:
            sys.stderr.write("  %-13s %7d %7d %7d %7d\n" % row)
        leader, child, accept_child = rows
        self.assertEqual(leader[2], leader[1])                       # the leader listens and accepts: its own pid
        self.assertNotEqual(child[2], child[1])                      # a child listens: the child's pid, not the daemon's
        # The leader listens and a child accepts: LOCAL_PEERPID is the last process that did I/O on the listener, so the child as soon as
        # it has accepted once (here it already served the readiness ping and the proof), the leader only before the first accept.
        self.assertIn(accept_child[2], (accept_child[1], accept_child[3]))
        self.assertNotEqual(accept_child[3], accept_child[1])
        self.assertTrue(all(row[4] == row[1] for row in rows))       # the peer's process group is the daemon's in every shape


@unittest.skipUnless(usable(), "set CAE_QA_LIVE_CODEX to a Codex binary on macOS with a Codex login")
class UnixStageLiveTests(Base):
    def setUp(self):
        super().setUp()
        att = self.ex / "inbox" / "att"
        (att / "source").mkdir(parents=True)
        subprocess.run(["git", "init", "-q", str(att / "source")], check=True)
        subprocess.run(["git", "-C", str(att / "source"), "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "--allow-empty", "-m", "x"], check=True)
        entries = self.write_payload(att / "payload")
        endpoint = side.Endpoint(str(self.qa_home), str(self.ex), "att", 0, "circinus-daemon/1", str(self.ex / "outbox/att/payload"),
                                 str(self.ex / "outbox/att/payload/bin/python3.13"))
        # What the self-test must see: the seeded file, the stand-in's socket, and in the state only what cae and the daemon put there.
        self.expected = (json.dumps({"ok": True, "seed": hashlib.sha256(SEED).hexdigest(), "dylib": 1,
                                     "top": ["circinus", "qa-probe", "qa-probe.db", "qa-probe.sock"], "circinus": ["circinusd.sock", "mode.json"]},
                                    sort_keys=True) + "\n").encode()
        test = {"id": "ping", "argv": ["bin/probe"], "stdin": None, "expect_exit": 0, "expect_stdout_sha256": hashlib.sha256(self.expected).hexdigest(),
                "timeout_s": 30, "max_output_bytes": 4096, "needs_endpoint": True}
        manifest = {"schema": "cae-qa-payload/2", "files": entries, "selftests": [test], "scope": "offline library-level checks",
                    "producer_run": ["harness/transcript.txt"], "endpoint": {"daemon": "bin/python3.13", "contract": "circinus-daemon/1"}}
        (att / "payload" / "MANIFEST.json").write_text(json.dumps(manifest, sort_keys=True))
        (att / "payload" / "MANIFEST.json").chmod(0o644)
        self.endpoint = endpoint
        self.args = types.SimpleNamespace(inbox=str(att), outbox=str(self.ex / "outbox" / "att"), codex_home=str(self.root / "codexhome"), codex_bin=CODEX,
                                          network="daemon-endpoint", port=0, qa_home=str(self.qa_home), exchange=str(self.ex), attempt="att")
        (self.ex / "outbox").mkdir()

    def test_a_unix_endpoint_run_is_proved_served_through_codex_and_torn_down(self):
        result = side.stage_outbox(self.args)
        self.assertTrue(result["confinement"]["established"], result["confinement"])
        record = result["endpoint"]
        self.assertTrue(record["established"], record)
        self.assertEqual([c["control"] for c in record["codex_controls"]], [c[0] for c in side.UNIX_CODEX_CONTROLS])
        self.assertTrue(all(c["ok"] for c in record["codex_controls"]), record["codex_controls"])
        self.assertTrue(all(c["ok"] for c in record["controls"]), [c for c in record["controls"] if not c["ok"]])
        self.assertEqual(record["seeded"], side.seed_record(json.loads((Path(self.args.inbox) / "payload/MANIFEST.json").read_text())))
        for key in ("socket_proof", "socket_recheck"):
            self.assertEqual(record[key]["peer_pgid"], record[key]["daemon_pid"], key)
            self.assertEqual(record[key]["ping_mode"], "shadow", key)
        (selftest,) = record["selftests"]
        self.assertEqual(selftest["exit"], 0, selftest)
        self.assertEqual(selftest["stdout_sha256"], hashlib.sha256(self.expected).hexdigest(), selftest)
        self.assertTrue(record["teardown"]["clean"], record["teardown"])
        self.assertEqual(record["teardown"]["socket_removed_by_helper"], ["circinusd.sock"])
        self.assertFalse(self.endpoint.dir.exists())
        self.assertEqual(record["daemon_profile_sha256"], side.endpoint_digests(self.endpoint)["daemon_profile_sha256"])


if __name__ == "__main__":
    unittest.main()
