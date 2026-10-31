#!/usr/bin/env python3
"""Behavioural tests of the daemon-endpoint profiles on macOS (SPE-145 PR-b1): the generated sandbox-exec profiles are run for real with a
stand-in daemon and the generated relay, and probes run under each profile. Needs /usr/bin/sandbox-exec and /usr/bin/python3, no codex, no
model, no network, no privilege; it skips everywhere else. The real candidate daemon and the QA user are covered by the owner demo, not here.

The scratch tree lives under the user's home on purpose: the profiles deny reads under /Users and the shared temporary areas, so a scratch
tree there would hide the very directories the profiles must re-allow."""
import importlib.util
import os
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
spec = importlib.util.spec_from_file_location("qa_side_live_test", SCRIPTS / "qa-side.py")
side = importlib.util.module_from_spec(spec)
spec.loader.exec_module(side)

SB = "/usr/bin/sandbox-exec"
PY = "/usr/bin/python3"
STAND_IN = '''#!/usr/bin/python3 -I
import os, socket, sqlite3, threading
st, port, tmp = os.environ["FORNAX_HOME"], int(os.environ["FORNAX_HTTP_PORT"]), os.environ["TMPDIR"]
db = sqlite3.connect(st + "/fornax.db"); db.execute("create table if not exists t(x)"); db.execute("insert into t values('row')"); db.commit()
open(tmp + "/scratch", "w").write("x")
def http():
    s = socket.socket(); s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1); s.bind(("127.0.0.1", port)); s.listen(8)
    while True:
        c, _ = s.accept(); c.recv(65536); c.sendall(b"HTTP/1.1 200 OK\\r\\nContent-Length: 2\\r\\nConnection: close\\r\\n\\r\\nok"); c.close()
def uds():
    os.chdir(st); u = socket.socket(socket.AF_UNIX); u.bind("fornax.sock"); u.listen(8)
    while True:
        c, _ = u.accept(); d = c.recv(65536); c.sendall(b"UDS:" + d[::-1]); c.close()
threading.Thread(target=http, daemon=True).start(); uds()
'''


def sandbox_usable():
    if sys.platform != "darwin" or not os.path.exists(SB) or not os.path.exists(PY):
        return False
    with tempfile.NamedTemporaryFile("w", suffix=".sb") as profile:
        profile.write("(version 1)\n(allow default)\n")
        profile.flush()
        try:
            return subprocess.run([SB, "-f", profile.name, "/usr/bin/true"], capture_output=True, timeout=20).returncode == 0
        except (OSError, subprocess.SubprocessError):
            return False


def free_port():
    for _ in range(50):
        port = 20000 + int.from_bytes(os.urandom(2), "big") % 12000
        with socket.socket() as probe:
            if probe.connect_ex(("127.0.0.1", port)) != 0:
                return port
    raise unittest.SkipTest("no free port in the endpoint range")


@unittest.skipUnless(sandbox_usable(), "needs macOS with a working sandbox-exec and /usr/bin/python3")
class EndpointProfilesBehaveTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.root = Path(tempfile.mkdtemp(prefix=".cae-endpoint-live-", dir=str(Path.home())))
        cls.home, cls.ex = cls.root / "qahome", cls.root / "exchange"
        cls.port = free_port()
        cls.pay = cls.ex / "outbox" / "att-1" / "payload"
        (cls.pay / "bin").mkdir(parents=True)
        cls.home.mkdir()
        (cls.ex / "inbox").mkdir()
        (cls.home / ".codex").mkdir()
        (cls.home / ".codex" / "auth.json").write_text("synthetic-login")
        (cls.home / "canary.txt").write_text("synthetic-canary")
        (cls.pay / "bin" / "daemon").write_text(STAND_IN)
        (cls.pay / "bin" / "daemon").chmod(0o755)
        cls.e = side.Endpoint(str(cls.home), str(cls.ex), "att-1", cls.port, "fornax-daemon/1", str(cls.pay), str(cls.pay / "bin" / "daemon"))
        for d in (cls.e.state, cls.e.tmp, cls.e.rl):
            d.mkdir(parents=True)
        (cls.e.dir / "daemon.sb").write_text(side.daemon_sb(cls.e))
        (cls.e.dir / "relay.sb").write_text(side.relay_sb(cls.e))
        cls.procs = []
        cls.daemon = subprocess.Popen([SB, "-f", str(cls.e.dir / "daemon.sb"), str(cls.pay / "bin" / "daemon")], env=cls.e.daemon_env(),
                                      stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
        cls.procs.append(cls.daemon)
        cls.wait_for(cls.e.daemon_sock)
        cls.relay = subprocess.Popen([SB, "-f", str(cls.e.dir / "relay.sb"), PY, "-I", "-c", side.RELAY_SRC, str(cls.e.relay_sock), str(cls.port)],
                                     env={"PATH": "/usr/bin:/bin"}, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
        cls.procs.append(cls.relay)
        cls.wait_for(cls.e.relay_sock)

    @classmethod
    def wait_for(cls, path):
        for _ in range(100):
            if path.exists():
                return
            time.sleep(0.1)
        raise AssertionError("%s did not appear: the generated profile may have stopped the process from starting" % path.name)

    @classmethod
    def tearDownClass(cls):
        for proc in cls.procs:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except OSError:
                pass
            proc.wait()
        shutil.rmtree(cls.root, ignore_errors=True)

    def probe(self, profile, body):
        """Run python `body` under a generated profile; return {label: 'OK'|'ERR n'} for every `t(label, fn)` the body makes."""
        code = ("import os, socket, sqlite3\nout = []\n"
                "def t(l, f):\n    try: f(); out.append(l + ' OK')\n    except OSError as x: out.append(l + ' ERR %s' % x.errno)\n"
                + body + "\nprint('\\n'.join(out))\n")
        done = subprocess.run([SB, "-f", str(self.e.dir / profile), PY, "-I", "-c", code], capture_output=True, text=True, timeout=60,
                              env={"PATH": "/usr/bin:/bin", "HOME": "/nonexistent"})
        got = dict(line.rsplit(" ", 1) if line.endswith(" OK") else line.rsplit(" ERR ", 1) for line in done.stdout.splitlines() if " " in line)
        return {k: (v if v == "OK" else "ERR " + v) for k, v in got.items()}

    def body(self):
        e = self.e
        return "\n".join([
            "t('read_login', lambda: open(%r).read())" % str(self.home / ".codex" / "auth.json"),
            "t('read_canary', lambda: open(%r).read())" % str(self.home / "canary.txt"),
            "t('list_home', lambda: os.listdir(%r))" % str(self.home),
            "t('list_exchange', lambda: os.listdir(%r))" % str(self.ex),
            "t('list_users', lambda: os.listdir('/Users'))",
            "t('list_tmp', lambda: os.listdir('/private/tmp'))",
            "t('stat_home', lambda: os.stat(%r))" % str(self.home),
            "t('write_home', lambda: open(%r, 'w').write('x'))" % str(self.home / "w"),
            "t('write_tmp_shared', lambda: open('/private/tmp/cae-endpoint-live-w', 'w').write('x'))",
            "t('testnet4', lambda: socket.create_connection(('192.0.2.1', 443), 3))",
            "t('udp', lambda: socket.socket(socket.AF_INET, socket.SOCK_DGRAM).sendto(b'x', ('192.0.2.1', 9)))",
            "t('resolver', lambda: socket.socket(socket.AF_UNIX).connect('/var/run/mDNSResponder'))",
            "t('other_port', lambda: socket.create_connection(('127.0.0.1', %d), 3))" % (self.port + 1),
            "t('bind_other_port', lambda: socket.socket().bind(('127.0.0.1', %d)))" % (self.port + 1),
            "t('write_state', lambda: open(%r, 'w').write('x'))" % str(e.state / "w"),
            "t('write_tmp', lambda: open(%r, 'w').write('x'))" % str(e.tmp / "w"),
            "t('write_rl', lambda: open(%r, 'w').write('x'))" % str(e.rl / "w"),
            "t('read_state_db', lambda: open(%r, 'rb').read(8))" % str(e.state / "fornax.db"),
            "t('read_payload', lambda: open(%r).read(2))" % str(self.pay / "bin" / "daemon"),
            "t('list_endpoint_dir', lambda: os.listdir(%r))" % str(e.dir),
            "t('own_port', lambda: socket.create_connection(('127.0.0.1', %d), 3).close())" % self.port,
        ])

    def test_daemon_is_up_with_sqlite_and_tmp_working_under_its_profile(self):
        self.assertTrue(self.e.daemon_sock.exists())
        self.assertTrue((self.e.state / "fornax.db").exists())
        self.assertTrue((self.e.tmp / "scratch").exists())

    def test_daemon_profile_confines_reads_writes_and_network(self):
        got = self.probe("daemon.sb", self.body())
        for denied in ("read_login", "read_canary", "list_home", "list_exchange", "list_users", "list_tmp", "write_home", "write_tmp_shared",
                       "testnet4", "udp", "resolver", "other_port", "bind_other_port", "write_rl", "list_endpoint_dir", "own_port"):
            self.assertEqual(got.get(denied), "ERR 1", (denied, got))
        for allowed in ("stat_home", "write_state", "write_tmp", "read_state_db", "read_payload"):
            self.assertEqual(got.get(allowed), "OK", (allowed, got))

    def test_relay_profile_reaches_only_the_daemon_port_and_writes_only_its_directory(self):
        got = self.probe("relay.sb", self.body())
        for denied in ("read_login", "read_canary", "list_home", "list_exchange", "list_users", "list_tmp", "write_home", "write_tmp_shared",
                       "testnet4", "udp", "resolver", "other_port", "bind_other_port", "write_state", "write_tmp", "read_state_db",
                       "read_payload", "list_endpoint_dir"):
            self.assertEqual(got.get(denied), "ERR 1", (denied, got))
        for allowed in ("stat_home", "write_rl", "own_port"):
            self.assertEqual(got.get(allowed), "OK", (allowed, got))

    def test_the_relay_carries_http_to_the_daemon_and_nothing_else_is_reachable_from_outside(self):
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

    def test_without_the_ancestor_metadata_a_process_cannot_even_stat_its_own_directories(self):
        """Why the literal file-read-metadata allowances exist: drop them and stat of every ancestor of an allowed directory fails with EPERM,
        which is what made a Rust daemon's sqlite open fail with code 14 in the review measurement (canonicalising a path stats each parent)."""
        text = "\n".join(l for l in side.daemon_sb(self.e).splitlines() if not l.startswith("(allow file-read-metadata")) + "\n"
        scratch = self.e.dir / "nometa.sb"
        scratch.write_text(text)
        probe = ("import os\nfor p in (%r, %r, %r):\n    try:\n        os.stat(p); print('STAT_OK')\n    except OSError as x:\n        print('STAT_ERR', x.errno)\n"
                 % (str(self.home), str(self.e.dir), str(self.ex / "outbox")))
        env = {"PATH": "/usr/bin:/bin"}
        without = subprocess.run([SB, "-f", str(scratch), PY, "-I", "-c", probe], capture_output=True, text=True, timeout=60, env=env)
        self.assertEqual(without.stdout.split(), ["STAT_ERR", "1"] * 3, without.stdout + without.stderr)
        with_meta = subprocess.run([SB, "-f", str(self.e.dir / "daemon.sb"), PY, "-I", "-c", probe], capture_output=True, text=True, timeout=60, env=env)
        self.assertEqual(with_meta.stdout.split(), ["STAT_OK"] * 3, with_meta.stdout + with_meta.stderr)

    def test_the_keychain_is_not_reachable_through_the_security_service(self):
        base = subprocess.run(["/usr/bin/security", "list-keychains"], capture_output=True, text=True, timeout=30)
        if base.returncode != 0 or not base.stdout.strip():
            self.skipTest("no keychain visible without the sandbox on this machine")
        confined = subprocess.run([SB, "-f", str(self.e.dir / "daemon.sb"), "/usr/bin/security", "list-keychains"], capture_output=True, text=True, timeout=30)
        self.assertNotEqual(confined.returncode, 0, confined.stdout)


if __name__ == "__main__":
    unittest.main()
