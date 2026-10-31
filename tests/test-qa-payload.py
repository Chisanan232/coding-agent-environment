#!/usr/bin/env python3
"""Offline tests for the QA payload (SPE-145 part 9): the manifest rules, the descriptor-relative verify and copy, the model-free
self-test runner and verdict derivation, the new network and payload confinement controls, and the stage record. A fake `codex`
stands in for the sandbox; no model, no network beyond a loopback/UDS snippet run in a scratch directory, no privilege."""
import argparse
import contextlib
import hashlib
import importlib.util
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
spec = importlib.util.spec_from_file_location("qa_side_payload_test", SCRIPTS / "qa-side.py")
side = importlib.util.module_from_spec(spec)
spec.loader.exec_module(side)

ME = os.getuid()
SH = lambda data: hashlib.sha256(data).hexdigest()   # noqa: E731

# The sandbox double: `codex sandbox -P NAME -C DIR CMD...`. A probe control (first line "# CONTROL=name") is judged by the mode file;
# anything else is a self-test and is executed as given, stdin passed through, so the tests see the real argv, env and cwd.
FAKE = r'''#!/usr/bin/env python3
import json, os, subprocess, sys
d = "@FAKEDIR@"
argv = sys.argv[1:]
assert argv[0] == "sandbox" and argv[1:3] == ["-P", "qa_confined"], argv
cmd = argv[argv.index("-C") + 2:]
code = cmd[-1] if cmd and cmd[0] == "/usr/bin/python3" else ""
if code.startswith("# CONTROL="):
    control = code.splitlines()[0].replace("# CONTROL=", "")
    mode = open(d + "/mode").read().strip() if os.path.exists(d + "/mode") else "confined"
    if mode == "leak-canary" and control == "neg_read_canary":
        print("ALLOWED"); sys.exit(0)
    if control.startswith("neg_") and mode in ("confined", "leak-canary"):
        print("DENIED:1"); sys.exit(0)
    if control.startswith("neg_") and mode == "open-network" and control.endswith(("bind", "connect")):
        print("ALLOWED"); sys.exit(0)
    if control.startswith("neg_") and mode == "timeout" and control == "neg_testnet_connect":
        print("DENIED:None"); sys.exit(0)
    if control.startswith("neg_") and mode == "open-network":
        print("DENIED:1"); sys.exit(0)
    sys.exit(subprocess.run([sys.executable, "-c", code]).returncode)
seen = json.load(open(d + "/seen.json")) if os.path.exists(d + "/seen.json") else []
seen.append({"cmd": cmd, "env": dict(os.environ), "cwd": os.getcwd(), "stdin_tty": os.isatty(0)})
json.dump(seen, open(d + "/seen.json", "w"))
sys.exit(subprocess.run(cmd, stdin=sys.stdin, env=dict(os.environ)).returncode)
'''


def clean_env(env):
    """Python on macOS adds a couple of variables to every process it starts; they are not the caller's."""
    return {k: v for k, v in env.items() if not k.startswith("__CF") and k not in ("LC_CTYPE",)}


def build(base, files, selftests=(), scope="offline library-level checks", producer_run=()):
    """Write a payload directory (files: {path: (bytes, kind)}) and return (dir, manifest, raw)."""
    base = Path(base)
    for path, (data, kind) in files.items():
        target = base / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        os.chmod(target, 0o755 if kind == "exec" else 0o644)
    entries = [{"path": p, "sha256": SH(files[p][0]), "size": len(files[p][0]), "kind": files[p][1]} for p in sorted(files)]
    manifest = {"schema": "cae-qa-payload/1", "files": entries, "selftests": list(selftests), "scope": scope,
                "producer_run": list(producer_run)}
    raw = json.dumps(manifest, sort_keys=True).encode()
    (base / "MANIFEST.json").write_bytes(raw)
    os.chmod(base / "MANIFEST.json", 0o644)
    return base, manifest, raw


def test_entry(ident="t1", argv=("bin/tool", "x"), stdin=None, exit_=0, stdout=b"ok\n", timeout=20, cap=4096):
    return {"id": ident, "argv": list(argv), "stdin": stdin, "expect_exit": exit_, "expect_stdout_sha256": SH(stdout),
            "timeout_s": timeout, "max_output_bytes": cap}


TOOL = b'#!/bin/sh\necho ok\n'
FILES = {"bin/tool": (TOOL, "exec"), "vectors/a.json": (b'{"a": 1}\n', "data"), "harness/transcript.txt": (b"producer transcript\n", "data")}


class Scratch(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(os.path.realpath(self.tmp.name))
        self.addCleanup(self.unlock)

    def unlock(self):
        for current, dirs, files in os.walk(self.root):
            for name in dirs + files:
                p = os.path.join(current, name)
                if not os.path.islink(p):
                    os.chmod(p, 0o700)

    def payload(self, **kw):
        directory = self.root / ("p%d" % len(list(self.root.iterdir())))
        directory.mkdir()
        files = kw.pop("files", FILES)
        return build(directory, files, **kw)

    def open_dir(self, directory):
        fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
        self.addCleanup(os.close, fd)
        return fd


class ManifestRuleTests(Scratch):
    def doc(self, **over):
        _, manifest, _ = self.payload(selftests=[test_entry()], producer_run=["harness/transcript.txt"])
        manifest.update(over)
        return manifest

    def parse(self, manifest):
        return side.parse_manifest(json.dumps(manifest).encode())

    def refused(self, manifest, word=None):
        with self.assertRaises(side.PayloadError) as caught:
            self.parse(manifest)
        if word:
            self.assertIn(word, str(caught.exception))

    def test_a_valid_manifest_parses(self):
        self.assertEqual(self.parse(self.doc())["schema"], "cae-qa-payload/1")

    def test_top_level_shape(self):
        good = self.doc()
        self.refused(dict(good, extra=1), "unknown keys")
        self.refused({k: v for k, v in good.items() if k != "scope"}, "missing keys")
        self.refused(dict(good, schema="cae-qa-payload/3"), "schema")
        self.refused(dict(good, schema="cae-qa-payload/2"), "missing keys")      # /2 needs its endpoint block and per-test flag
        self.refused(dict(good, scope=""), "scope")
        self.refused(dict(good, scope="x" * 2049), "scope")
        self.refused(dict(good, scope=5), "scope")
        self.refused(dict(good, scope="bell\x07"), "scope")
        for raw in (b"", b"[]", b"not json", b"\xff\xfe", b"x" * 70000):
            with self.assertRaises(side.PayloadError):
                side.parse_manifest(raw)

    def test_file_entries(self):
        good = self.doc()
        entry = dict(good["files"][0])
        for label, bad in (("unknown key", dict(entry, mode=1)), ("bad sha", dict(entry, sha256="AB" * 32)), ("short sha", dict(entry, sha256="a" * 63)),
                           ("neg size", dict(entry, size=-1)), ("bool size", dict(entry, size=True)), ("float size", dict(entry, size=1.0)),
                           ("kind", dict(entry, kind="script")), ("abs path", dict(entry, path="/etc/passwd")), ("dotdot", dict(entry, path="a/../b")),
                           ("dot", dict(entry, path="./a")), ("empty comp", dict(entry, path="a//b")), ("trailing slash", dict(entry, path="a/")),
                           ("backslash", dict(entry, path="a\\b")), ("control", dict(entry, path="a\nb")), ("too long", dict(entry, path="a" * 201)),
                           ("too deep", dict(entry, path="/".join("d" * 1 for _ in range(9)))), ("manifest name", dict(entry, path="MANIFEST.json"))):
            self.refused(dict(good, files=[bad] + good["files"][1:]), None) if False else None
            files = [bad] + good["files"][1:]
            with self.assertRaises(side.PayloadError, msg=label):
                self.parse(dict(good, files=files, selftests=[], producer_run=[]))

    def test_ordering_uniqueness_and_file_directory_clash(self):
        good = self.doc()
        files = good["files"]
        self.refused(dict(good, files=list(reversed(files)), selftests=[], producer_run=[]), "sorted")
        self.refused(dict(good, files=files + [files[-1]], selftests=[], producer_run=[]), "sorted")
        clash = [dict(files[0], path="bin"), dict(files[0], path="bin/tool")]
        self.refused(dict(good, files=clash, selftests=[], producer_run=[]), "both a file and a directory")

    def test_caps(self):
        base = {"sha256": "a" * 64, "size": 1, "kind": "data"}
        many = [dict(base, path="f%04d" % i) for i in range(4)]
        with mock.patch.object(side, "PAYLOAD_MAX_FILES", 3):
            self.refused(dict(self.doc(), files=many, selftests=[], producer_run=[]), "at most")
        # NB: 2000 entries cannot fit the 64 KiB manifest cap (about 110 bytes each), so the size cap is what binds in practice
        self.assertGreater(2000 * len(json.dumps(dict(base, path="f0000"))), side.MANIFEST_MAX)
        big = [dict(base, path="a", size=200 << 20), dict(base, path="b", size=100 << 20)]
        self.refused(dict(self.doc(), files=big, selftests=[], producer_run=[]), "larger than")
        deep = [dict(base, path="/".join("d%d" % i for i in range(8)))]
        self.assertTrue(self.parse(dict(self.doc(), files=deep, selftests=[], producer_run=[])))

    def test_selftest_entries(self):
        good = self.doc()
        test = good["selftests"][0]
        for label, bad in (("unknown key", dict(test, shell=True)), ("bad id", dict(test, id="Bad Id")), ("long id", dict(test, id="a" * 41)),
                           ("argv0 not listed", dict(test, argv=["bin/missing"])), ("argv0 is data", dict(test, argv=["vectors/a.json"])),
                           ("argv0 absolute", dict(test, argv=["/bin/sh", "-c", "x"])), ("argv empty", dict(test, argv=[])),
                           ("argv non-string", dict(test, argv=["bin/tool", 3])), ("arg too long", dict(test, argv=["bin/tool", "a" * 257])),
                           ("arg NUL", dict(test, argv=["bin/tool", "a\x00b"])), ("payload dotdot", dict(test, argv=["bin/tool", "@payload/../x"])),
                           ("stdin unlisted", dict(test, stdin="vectors/none.json")), ("stdin is exec", dict(test, stdin="bin/tool")),
                           ("exit missing", {k: v for k, v in test.items() if k != "expect_exit"}), ("exit bool", dict(test, expect_exit=False)),
                           ("exit 256", dict(test, expect_exit=256)), ("exit negative", dict(test, expect_exit=-1)),
                           ("hash upper", dict(test, expect_stdout_sha256="A" * 64)), ("timeout 0", dict(test, timeout_s=0)),
                           ("timeout 121", dict(test, timeout_s=121)), ("timeout float", dict(test, timeout_s=1.5)),
                           ("cap 0", dict(test, max_output_bytes=0)), ("cap too big", dict(test, max_output_bytes=(1 << 20) + 1))):
            with self.assertRaises(side.PayloadError, msg=label):
                self.parse(dict(good, selftests=[bad]))

    def test_selftest_counts_and_total_wall(self):
        good = self.doc()
        many = [test_entry("t%d" % i, timeout=1) for i in range(33)]
        self.refused(dict(good, selftests=many), "at most 32")
        slow = [test_entry("s%d" % i, timeout=120) for i in range(6)]
        self.refused(dict(good, selftests=slow), "in total")
        self.refused(dict(good, selftests=[test_entry("dup"), test_entry("dup")]), "repeated")
        ok = [test_entry("s%d" % i, timeout=120) for i in range(5)]
        self.assertEqual(len(self.parse(dict(good, selftests=ok))["selftests"]), 5)

    def test_nonzero_expected_exit_and_a_stdin_file_are_supported(self):
        good = self.doc()
        entry = test_entry("fails", exit_=3, stdin="vectors/a.json")
        self.assertEqual(self.parse(dict(good, selftests=[entry]))["selftests"][0]["expect_exit"], 3)

    def test_producer_run_must_be_listed_data(self):
        good = self.doc()
        self.refused(dict(good, producer_run=["bin/tool"]), "producer_run")
        self.refused(dict(good, producer_run=["nope.txt"]), "producer_run")
        self.refused(dict(good, producer_run=["harness/transcript.txt"] * 2), "unique")

    def test_duplicate_json_keys_are_refused_at_any_depth(self):
        good = self.doc()
        raw = json.dumps(good).encode()
        top = raw.replace(b'"scope"', b'"scope": "first", "scope"', 1)
        nested = raw.replace(b'"kind": "exec"', b'"kind": "data", "kind": "exec"', 1)
        for label, data in (("top level", top), ("nested", nested)):
            with self.assertRaises(side.PayloadError, msg=label) as caught:
                side.parse_manifest(data)
            self.assertIn("duplicate", str(caught.exception))

    def test_case_colliding_and_hiding_paths_are_refused(self):
        good = self.doc()
        entry = good["files"][0]
        for label, paths in (("case collision", ["A.txt", "a.txt"]), ("bidi override", ["bin/\u202etxt.exe"]), ("zero width", ["a\u200bb"]),
                             ("bidi isolate", ["a\u2066b"]), ("line separator", ["a\u2028b"]), ("DEL", ["a\x7fb"]), ("newline", ["a\nb"]),
                             ("unassigned", ["a\u0378b"])):
            files = [dict(entry, path=p, kind="data") for p in sorted(paths)]
            with self.assertRaises(side.PayloadError, msg=label):
                self.parse(dict(good, files=files, selftests=[], producer_run=[]))
        for text in ("scope \u202e hidden", "scope \u200b", "bell\x07"):
            self.refused(dict(good, scope=text), "scope")
        self.assertTrue(self.parse(dict(good, scope="plain text\nsecond line\twith a tab")))
        self.assertTrue(self.parse(dict(good, files=[dict(entry, path="d\u00e9j\u00e0/vu.txt", kind="data")], selftests=[], producer_run=[])))

    def test_payload_token_expansion_is_a_whole_prefix_only(self):
        test = test_entry(argv=("bin/tool", "@payload", "@payload/vectors/a.json", "@payloadX", "x@payload/y", "--f=@payload/z", "/etc/hosts"))
        out = side.expand_selftest_argv(test, "/run/payload")
        self.assertEqual(out, ["/run/payload/bin/tool", "/run/payload", "/run/payload/vectors/a.json", "@payloadX", "x@payload/y",
                               "--f=@payload/z", "/etc/hosts"])


class TreeTests(Scratch):
    def setUp(self):
        super().setUp()
        self.dir, self.manifest, self.raw = self.payload(selftests=[test_entry()])
        self.fd = self.open_dir(self.dir)

    def problems(self, **kw):
        return side.verify_tree(self.fd, self.manifest, self.raw, kw.pop("owner", ME), **kw)

    def test_exact_tree_verifies(self):
        self.assertEqual(self.problems(), [])

    def test_extra_missing_changed_and_resized(self):
        (self.dir / "extra.txt").write_text("x")
        self.assertIn("unexpected file extra.txt", self.problems())
        os.unlink(self.dir / "extra.txt")
        os.unlink(self.dir / "vectors/a.json")
        self.assertIn("missing vectors/a.json", self.problems())
        (self.dir / "vectors/a.json").write_bytes(b'{"a": 2}\n')
        self.assertTrue(any("SHA-256" in p for p in self.problems()))
        (self.dir / "vectors/a.json").write_bytes(b"short")
        self.assertTrue(any("size" in p for p in self.problems()))

    def test_manifest_bytes_must_be_the_ones_held(self):
        (self.dir / "MANIFEST.json").write_bytes(self.raw + b" ")
        self.assertTrue(any("MANIFEST.json" in p for p in self.problems()))

    def test_links_special_files_and_extra_directories(self):
        for label, make, word in (("link", lambda: os.symlink("/etc/hosts", self.dir / "link"), "link is a link or not a regular file"),
                                  ("fifo", lambda: os.mkfifo(self.dir / "fifo"), "fifo is a link or not a regular file"),
                                  ("emptydir", lambda: (self.dir / "emptydir").mkdir(), "unexpected directory emptydir"),
                                  ("nested", lambda: (self.dir / "bin/nested").mkdir(), "unexpected directory bin/nested")):
            make()
            self.assertIn(word, self.problems(), label)
            target = self.dir / label if label != "nested" else self.dir / "bin/nested"
            os.rmdir(target) if target.is_dir() and not target.is_symlink() else os.unlink(target)
        self.assertEqual(self.problems(), [])
        for i in range(30):                                  # a flood of extras is bounded, not walked forever
            (self.dir / ("x%d" % i)).write_text("x")
        self.assertIn("more entries than the manifest lists", " | ".join(self.problems()))

    def test_a_listed_file_replaced_by_a_link_is_refused(self):
        os.unlink(self.dir / "vectors/a.json")
        os.symlink("/etc/hosts", self.dir / "vectors/a.json")
        found = self.problems()
        self.assertIn("vectors/a.json is a link or not a regular file", found)

    def test_hard_links_and_wrong_owner(self):
        os.link(self.dir / "vectors/a.json", self.root / "second-name")
        self.assertTrue(any("more than one link" in p for p in self.problems()))
        os.unlink(self.root / "second-name")
        self.assertTrue(any("wrong owner" in p for p in self.problems(owner=ME + 1)))

    def test_modes_are_exact_when_asked(self):
        found = self.problems(check_modes=True)
        self.assertTrue(any("mode" in p for p in found))
        side.copy_payload(self.fd, str(self.root / "out"), self.manifest, self.raw, side.OUTBOX_MODES)
        out = self.open_dir(self.root / "out")
        self.assertEqual(side.verify_tree(out, self.manifest, self.raw, ME, check_modes=True), [])
        os.chmod(self.root / "out" / "bin/tool", 0o755)
        self.assertTrue(any("bin/tool has mode 755" in p for p in side.verify_tree(out, self.manifest, self.raw, ME, check_modes=True)))


class CopyTests(Scratch):
    def setUp(self):
        super().setUp()
        self.dir, self.manifest, self.raw = self.payload(selftests=[test_entry()])
        self.fd = self.open_dir(self.dir)

    def test_copy_sets_modes_by_kind_and_directories_last(self):
        dst = self.root / "outbox-payload"
        side.copy_payload(self.fd, str(dst), self.manifest, self.raw, side.OUTBOX_MODES)
        modes = {str(p.relative_to(dst)): stat.S_IMODE(p.stat().st_mode) for p in dst.rglob("*")}
        self.assertEqual(modes["bin/tool"], 0o555)
        self.assertEqual(modes["vectors/a.json"], 0o444)
        self.assertEqual(modes["MANIFEST.json"], 0o444)
        self.assertEqual(modes["bin"], 0o555)
        self.assertEqual(stat.S_IMODE(dst.stat().st_mode), 0o555)
        self.assertEqual((dst / "MANIFEST.json").read_bytes(), self.raw)
        self.assertEqual((dst / "bin/tool").read_bytes(), TOOL)

    def test_inbox_modes_are_owner_only(self):
        dst = self.root / "inbox-payload"
        side.copy_payload(self.fd, str(dst), self.manifest, self.raw, side.INBOX_MODES)
        self.assertEqual(stat.S_IMODE((dst / "bin/tool").stat().st_mode), 0o500)
        self.assertEqual(stat.S_IMODE((dst / "vectors/a.json").stat().st_mode), 0o400)
        self.assertEqual(stat.S_IMODE((dst / "bin").stat().st_mode), 0o700)

    def test_a_file_changed_after_verification_is_caught_while_copying(self):
        (self.dir / "vectors/a.json").write_bytes(b'{"a": 9}\n')
        with self.assertRaises(side.PayloadError):
            side.copy_payload(self.fd, str(self.root / "o"), self.manifest, self.raw, side.OUTBOX_MODES)

    def test_a_listed_file_swapped_for_a_link_or_second_link_is_refused(self):
        os.unlink(self.dir / "vectors/a.json")
        os.symlink("/etc/hosts", self.dir / "vectors/a.json")
        with self.assertRaises(OSError):
            side.copy_payload(self.fd, str(self.root / "o1"), self.manifest, self.raw, side.OUTBOX_MODES)
        os.unlink(self.dir / "vectors/a.json")
        (self.dir / "vectors/a.json").write_bytes(b'{"a": 1}\n')
        os.link(self.dir / "vectors/a.json", self.root / "twin")
        with self.assertRaises(side.PayloadError):
            side.copy_payload(self.fd, str(self.root / "o2"), self.manifest, self.raw, side.OUTBOX_MODES)

    def test_a_directory_swapped_for_a_link_is_refused(self):
        shutil.rmtree(self.dir / "vectors")
        os.symlink(self.root, self.dir / "vectors")
        with self.assertRaises(OSError):
            side.copy_payload(self.fd, str(self.root / "o3"), self.manifest, self.raw, side.OUTBOX_MODES)


class Env:
    """CODEX_HOME with a login, a canary root, a fake codex and one outbox."""

    def __init__(self, test, manifest=None, raw=None, directory=None):
        self.root = Path(tempfile.mkdtemp(dir=test.root))
        self.fake_dir = self.root / "fake"
        self.fake_dir.mkdir()
        self.codex = self.root / "codex"
        self.codex.write_text(FAKE.replace("@FAKEDIR@", str(self.fake_dir)).replace("#!/usr/bin/env python3", "#!" + sys.executable))
        os.chmod(self.codex, 0o755)
        self.codex_home_real = self.root / "real-codex-home"
        self.codex_home_real.mkdir()
        (self.codex_home_real / "auth.json").write_text("{}")
        self.inbox = self.root / "inbox"
        self.inbox.mkdir()
        self.outbox = self.root / "outbox"
        self.private_home = self.root / "private-home"
        (self.inbox / "source" / ".git").mkdir(parents=True)
        (self.inbox / "source" / ".git" / "HEAD").write_text("ref: refs/heads/x\n")
        if directory is not None:
            shutil.copytree(directory, self.inbox / "payload")
        self.args = argparse.Namespace(inbox=str(self.inbox), outbox=str(self.outbox), codex_home=str(self.private_home), codex_bin=str(self.codex))
        self.patches = [mock.patch.dict(os.environ, {"CODEX_HOME": str(self.codex_home_real)}),
                        mock.patch.object(side, "CANARY_ROOT", self.root / "canary"),
                        mock.patch.object(side, "HOME", self.root / "home")]

    def __enter__(self):
        for p in self.patches:
            p.start()
        return self

    def __exit__(self, *exc):
        for p in self.patches:
            p.stop()

    def mode(self, value):
        (self.fake_dir / "mode").write_text(value)

    def seen(self):
        return json.loads((self.fake_dir / "seen.json").read_text())


class ControlTests(Scratch):
    def test_controls_for_is_decided_by_the_manifest(self):
        base = {c[0] for c in side.CONTROLS}
        self.assertTrue({"neg_loopback_bind", "neg_uds_bind", "neg_testnet_connect"} <= base)
        self.assertEqual(side.controls_for(None), side.CONTROLS)
        _, with_exec, _ = self.payload()
        names = {c[0] for c in side.controls_for(with_exec)}
        self.assertEqual(names - base, {"pos_exec_payload", "neg_write_payload"})
        _, data_only, _ = self.payload(files={"d.txt": (b"x", "data")})
        self.assertEqual({c[0] for c in side.controls_for(data_only)} - base, {"neg_write_payload"})

    def test_network_denial_counts_only_as_eperm_or_eacces(self):
        with Env(self) as env:
            for mode, ok in (("confined", True), ("open-network", False), ("timeout", False)):
                env.mode(mode)
                (env.outbox).mkdir(exist_ok=True)
                for sub in ("source", "out", "tmp", "home"):
                    (env.outbox / sub).mkdir(exist_ok=True)
                (env.outbox / "source" / ".git").mkdir(exist_ok=True)
                (env.outbox / "source" / ".git" / "HEAD").write_text("x")
                side.prepare_codex_home(env.outbox, home=env.private_home)
                record, problems = side.confinement_probe(str(env.codex), env.outbox, {"PATH": "/usr/bin:/bin"})
                self.assertEqual(record["established"], ok, (mode, problems))
                if not ok:
                    self.assertTrue(any(w in " ".join(problems) for w in ("neg_loopback_bind", "neg_uds_bind", "neg_testnet_connect")), problems)

    def test_the_uds_snippet_uses_a_short_relative_name_and_cleans_up(self):
        snippet = dict((n, s) for n, s, _ in side.CONTROLS)["neg_uds_bind"].format(out=str(self.root))
        self.assertIn("name = 'qa-uds.sock'", snippet)
        done = subprocess.run([sys.executable, "-c", snippet], capture_output=True, text=True, timeout=30)
        self.assertEqual(done.stdout.strip(), "ALLOWED")          # unsandboxed: the bind works, which is what the profile must prevent
        self.assertFalse((self.root / "qa-uds.sock").exists())

    def test_profile_never_enables_network_and_mounts_the_payload_read_only(self):
        with Env(self) as env:
            text = side.profile_toml(env.outbox, [env.outbox / "payload"])
        self.assertIn("[permissions.qa_confined.network]\nenabled = false", text)
        self.assertIn('"%s" = "read"' % (env.outbox / "payload"), text)
        self.assertNotIn('"%s" = "write"' % (env.outbox / "payload"), text)
        self.assertNotIn("enabled = true", text)


class SelftestRunnerTests(Scratch):
    def run_tests(self, tests, files=None, stdout_override=None):
        directory, manifest, raw = self.payload(selftests=tests, files=files or {**FILES, "bin/fail": (b"#!/bin/sh\necho no >&2\nexit 3\n", "exec"),
                                                                                  "bin/cat": (b"#!/bin/sh\ncat\n", "exec"),
                                                                                  "bin/sleep": (b"#!/bin/sh\nsleep 30\n", "exec"),
                                                                                  "bin/flood": (b"#!/bin/sh\nyes | head -c 200000\n", "exec")})
        with Env(self, directory=directory) as env:
            record = side.stage_outbox_for_test(env) if False else None
            (env.outbox).mkdir()
            for sub in ("out", "tmp", "home", "source"):
                (env.outbox / sub).mkdir()
            fd = side.open_payload_path(env.inbox / "payload")
            side.copy_payload(fd[0], str(env.outbox / "payload"), manifest, raw, side.OUTBOX_MODES)
            os.close(fd[0])
            records = side.run_selftests(str(env.codex), env.outbox, manifest, side.selftest_env(env.outbox, env.private_home))
            return env, manifest, records

    def test_a_passing_test_and_the_exact_invocation(self):
        env, manifest, records = self.run_tests([test_entry("good", argv=("bin/tool", "@payload/vectors/a.json", "lit"))])
        verdicts, first = side.derive_selftest_verdicts(manifest, records)
        self.assertIsNone(first)
        self.assertEqual(records[0]["exit"], 0)
        self.assertEqual(records[0]["stdout_sha256"], SH(b"ok\n"))
        (seen,) = env.seen()
        outbox = str(env.outbox)
        self.assertEqual(seen["cmd"], [outbox + "/payload/bin/tool", outbox + "/payload/vectors/a.json", "lit"])
        self.assertEqual(seen["cwd"], outbox + "/tmp")
        self.assertEqual(clean_env(seen["env"]), {"PATH": "/usr/bin:/bin:/usr/sbin:/sbin", "HOME": outbox + "/home", "TMPDIR": outbox + "/tmp",
                                       "CODEX_HOME": str(env.private_home)})

    def test_poisoned_caller_environment_never_reaches_the_test(self):
        with mock.patch.dict(os.environ, {"PATH": "/tmp/evil", "LANG": "xx", "SECRET_TOKEN": "nope", "HOME": "/tmp/evilhome"}):
            env, manifest, records = self.run_tests([test_entry("good")])
        (seen,) = env.seen()
        self.assertEqual(set(clean_env(seen["env"])), {"PATH", "HOME", "TMPDIR", "CODEX_HOME"})
        self.assertNotIn("evil", json.dumps(seen["env"]))

    def test_stdin_file_is_fed(self):
        env, manifest, records = self.run_tests([test_entry("viastdin", argv=("bin/cat",), stdin="vectors/a.json", stdout=b'{"a": 1}\n')])
        verdicts, first = side.derive_selftest_verdicts(manifest, records)
        self.assertIsNone(first, verdicts)

    def test_nonzero_exit_that_is_expected_passes_and_stderr_is_recorded_not_compared(self):
        env, manifest, records = self.run_tests([test_entry("failsok", argv=("bin/fail",), exit_=3, stdout=b"")])
        verdicts, first = side.derive_selftest_verdicts(manifest, records)
        self.assertIsNone(first)
        self.assertEqual(records[0]["stderr_sha256"], SH(b"no\n"))

    def test_each_kind_of_failure_names_the_test_and_the_reason(self):
        cases = (("wrongexit", test_entry("wrongexit", exit_=0, argv=("bin/fail",), stdout=b""), "exited 3, expected 0"),
                 ("wronghash", test_entry("wronghash", stdout=b"different\n"), "stdout SHA-256"),
                 ("toomuch", test_entry("toomuch", argv=("bin/flood",), cap=100, stdout=b""), "more than 100 bytes"),
                 ("slow", test_entry("slow", argv=("bin/sleep",), timeout=1, stdout=b""), "timed out after 1 s"))
        for ident, entry, word in cases:
            env, manifest, records = self.run_tests([entry])
            verdicts, first = side.derive_selftest_verdicts(manifest, records)
            self.assertIsNotNone(first, ident)
            self.assertTrue(first.startswith("self-test %s " % ident), first)
            self.assertIn(word, first)
        self.assertEqual(records[0]["timed_out"], True)

    def test_verdicts_are_recomputed_not_taken_from_the_qa_report(self):
        _, manifest, _ = self.payload(selftests=[test_entry("a"), test_entry("b")])
        forged = [{"id": "a", "ok": True, "verdict": "PASS", "exit": 0, "stdout_bytes": 3, "stdout_sha256": "0" * 64},
                  {"id": "b", "exit": 0, "stdout_bytes": 3, "stdout_sha256": SH(b"ok\n")}]
        verdicts, first = side.derive_selftest_verdicts(manifest, forged)
        self.assertEqual([v["ok"] for v in verdicts], [False, True])
        self.assertIn("self-test a ", first)
        for records, word in (([], "not run"), ([forged[1], dict(forged[1])], "more than once"), ("garbage", "not run"),
                              ([dict(forged[1], unrunnable=True)], "could not be run"), ([dict(forged[1], exit=True)], "exited")):
            _, one = self.payload(selftests=[test_entry("b")])[1], None
            v, f = side.derive_selftest_verdicts({"selftests": [test_entry("b")]}, records)
            self.assertIn(word, f, records)

    def test_unrunnable_when_the_sandbox_cannot_start(self):
        directory, manifest, raw = self.payload(selftests=[test_entry("x")])
        outbox = self.root / "ob"
        for sub in ("source", "tmp", "home", "payload"):
            (outbox / sub).mkdir(parents=True)
        records = side.run_selftests(str(self.root / "no-such-codex"), outbox, manifest, {})
        self.assertTrue(records[0]["unrunnable"])
        _, first = side.derive_selftest_verdicts(manifest, records)
        self.assertIn("could not be run", first)

    def test_a_flood_of_output_is_cut_off_by_the_kernel_not_the_timeout(self):
        flood = {**FILES, "bin/yes": (b"#!/bin/sh\nexec yes\n", "exec")}
        with mock.patch.object(side, "SELFTEST_FSIZE_FLOOR", 64 << 10):
            env, manifest, records = self.run_tests([test_entry("flood", argv=("bin/yes",), timeout=20, cap=100, stdout=b"")], files=flood)
        record = records[0]
        self.assertFalse(record["timed_out"], record)
        self.assertLess(record["elapsed"], 15)
        self.assertLessEqual(record["stdout_bytes"], side.fsize_limit(100))
        verdicts, first = side.derive_selftest_verdicts(manifest, records)
        self.assertIn("more than 100 bytes", first)

    def test_the_exec_control_has_a_file_size_limit_too(self):
        seen = {}
        real = subprocess.Popen

        def spy(argv, **kw):
            seen["preexec"] = kw.get("preexec_fn")
            return real(argv, **kw)
        with mock.patch.object(side.subprocess, "Popen", side_effect=spy):
            side.run_group([sys.executable, "-c", "pass"], {"PATH": "/usr/bin:/bin"}, 10)
        self.assertIsNotNone(seen["preexec"])
        self.assertEqual(side.fsize_limit(100), side.SELFTEST_FSIZE_FLOOR)

    def test_labels_are_sanitized_before_they_reach_a_result(self):
        spec2 = importlib.util.spec_from_file_location("qa_handoff_for_label_test", SCRIPTS / "qa-handoff.py")
        qa = importlib.util.module_from_spec(spec2)
        spec2.loader.exec_module(qa)
        packet = {"payload": {"manifest_sha256": "a" * 64, "scope": "s", "producer_run": ["ok.txt", "bad\x1b[31m\u202ename.txt"]}}
        labels = qa.payload_result(packet)["producer_run"]
        self.assertEqual(labels[0], "PRODUCER-RUN: ok.txt")
        self.assertTrue(all(c.isprintable() and "\x1b" not in c and "\u202e" not in c for c in labels[1]) or "\u202e" not in labels[1])
        self.assertNotIn("\x1b", labels[1])

    def test_stage_budget_covers_every_test(self):
        _, manifest, _ = self.payload(selftests=[test_entry("a", timeout=100), test_entry("b", timeout=20)])
        self.assertEqual(side.payload_stage_seconds(manifest), 120 + 110 + 30)


class PayloadCodeOnlyRunsOnAProvedProfileTests(Scratch):
    """The security rule: producer-supplied code is executed under the sandbox only after every other control ran and passed."""

    def payload_dir(self, marker, extra=""):
        tool = ("#!/bin/sh\ntouch %s\n%secho ok\n" % (marker, extra)).encode()
        return self.payload(files={"bin/tool": (tool, "exec")}, selftests=[test_entry("good", argv=("bin/tool",))])

    def test_a_failing_base_control_means_the_payload_binary_never_runs(self):
        marker = self.root / "EXECUTED"
        directory, manifest, raw = self.payload_dir(marker)
        with Env(self, directory=directory) as env, mock.patch.object(side, "run_group", side_effect=AssertionError("payload exec control ran")), \
                mock.patch.object(side, "run_selftests", side_effect=AssertionError("self-tests ran")):
            env.mode("leak-canary")
            record = side.stage_outbox(env.args)
        self.assertFalse(marker.exists())
        self.assertFalse(record["confinement"]["established"])
        self.assertNotIn("selftests", record)
        names = [c["control"] for c in record["confinement"]["controls"]]
        self.assertEqual(names[-1], "pos_exec_payload")
        last = record["confinement"]["controls"][-1]
        self.assertTrue(last["observed"].startswith("NOT RUN") and last["unrunnable"] and not last["ok"])

    def test_with_every_base_control_passing_it_runs_and_the_order_is_base_then_payload(self):
        marker = self.root / "EXECUTED"
        directory, manifest, raw = self.payload_dir(marker)
        with Env(self, directory=directory) as env:
            record = side.stage_outbox(env.args)
        self.assertTrue(marker.exists())
        self.assertTrue(record["confinement"]["established"], record["confinement"])
        names = [c["control"] for c in record["confinement"]["controls"]]
        self.assertEqual(names[:len(side.CONTROLS)], [c[0] for c in side.CONTROLS])
        self.assertEqual(names[len(side.CONTROLS):], ["neg_write_payload", "pos_exec_payload"])
        self.assertEqual(len(record["selftests"]), 1)

    def test_the_exec_control_uses_the_allowlist_environment_and_leaves_no_process_behind(self):
        pidfile = self.root / "pid"
        marker = self.root / "EXECUTED"
        directory, manifest, raw = self.payload_dir(marker, "sleep 60 &\necho $! > %s\n" % pidfile)
        with mock.patch.dict(os.environ, {"LANG": "xx", "SECRET_TOKEN": "nope"}):
            with Env(self, directory=directory) as env:
                record = side.stage_outbox(env.args)
        self.assertTrue(record["confinement"]["established"], record["confinement"])
        child = int(pidfile.read_text())
        self.assertFalse(self.alive(child), "the background child of the payload survived the control")
        env_seen = [e["env"] for e in env.seen()]
        self.assertTrue(env_seen)
        for seen in env_seen:
            self.assertEqual(set(clean_env(seen)), {"PATH", "HOME", "TMPDIR", "CODEX_HOME"})

    @staticmethod
    def alive(pid):
        import time
        for _ in range(40):
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                return False
            except PermissionError:
                return True
            time.sleep(0.05)
        return True

    def test_a_selftest_that_forks_and_sleeps_is_killed_by_group_at_the_timeout(self):
        pidfile = self.root / "pid2"
        tool = ("#!/bin/sh\nsleep 60 &\necho $! > %s\nsleep 60\n" % pidfile).encode()
        directory, manifest, raw = self.payload(files={"bin/tool": (tool, "exec")}, selftests=[test_entry("slow", argv=("bin/tool",), timeout=1, stdout=b"")])
        with Env(self, directory=directory) as env:
            env.outbox.mkdir()
            for sub in ("out", "tmp", "home", "source"):
                (env.outbox / sub).mkdir()
            fd = side.open_payload_path(env.inbox / "payload")
            side.copy_payload(fd[0], str(env.outbox / "payload"), manifest, raw, side.OUTBOX_MODES)
            os.close(fd[0])
            records = side.run_selftests(str(env.codex), env.outbox, manifest, side.selftest_env(env.outbox, env.private_home))
        self.assertTrue(records[0]["timed_out"])
        self.assertFalse(self.alive(int(pidfile.read_text())))


class StageTests(Scratch):
    def stage(self, env, **kw):
        return side.stage_outbox(env.args, **kw)

    def test_stage_copies_verifies_mounts_proves_and_runs_the_self_tests(self):
        directory, manifest, raw = self.payload(selftests=[test_entry("good")], producer_run=["harness/transcript.txt"])
        with Env(self, directory=directory) as env:
            record = self.stage(env)
            self.assertEqual(record["payload"], {"manifest_sha256": SH(raw), "staged": True, "problems": []})
            self.assertTrue(record["confinement"]["established"], record["confinement"])
            names = {c["control"] for c in record["confinement"]["controls"]}
            self.assertTrue({"pos_exec_payload", "neg_write_payload", "neg_loopback_bind", "neg_uds_bind", "neg_testnet_connect"} <= names)
            self.assertEqual(len(record["selftests"]), 1)
            profile = (env.private_home / "config.toml").read_text()
            self.assertIn('"%s" = "read"' % (env.outbox / "payload"), profile)
            self.assertEqual((env.outbox / "payload/bin/tool").read_bytes(), TOOL)
            self.assertEqual(stat.S_IMODE((env.outbox / "payload/bin/tool").stat().st_mode), 0o555)

    def test_no_payload_means_no_payload_keys_in_the_stage_record(self):
        with Env(self) as env:
            record = self.stage(env)
        self.assertEqual(set(record), {"confinement", "profile_sha256"})
        self.assertNotIn("payload", {c["control"] for c in record["confinement"]["controls"]})

    def test_a_tampered_payload_stops_staging_with_the_reasons(self):
        directory, manifest, raw = self.payload(selftests=[test_entry("good")])
        (directory / "vectors/a.json").write_bytes(b"tampered")
        with Env(self, directory=directory) as env:
            record = self.stage(env)
        self.assertEqual(set(record), {"payload"})
        self.assertFalse(record["payload"]["staged"])
        self.assertTrue(any("vectors/a.json" in p for p in record["payload"]["problems"]))
        self.assertFalse((env.outbox / "payload").exists())

    def test_a_manifest_rule_violation_is_reported_not_raised(self):
        directory, manifest, raw = self.payload()
        (directory / "MANIFEST.json").write_text(json.dumps(dict(manifest, extra=1)))
        with Env(self, directory=directory) as env:
            record = self.stage(env)
        self.assertIn("unknown keys", record["payload"]["problems"][0])

    def test_no_self_tests_run_when_the_profile_is_not_proved(self):
        directory, manifest, raw = self.payload(selftests=[test_entry("good")])
        with Env(self, directory=directory) as env:
            env.mode("open-network")
            record = self.stage(env)
        self.assertFalse(record["confinement"]["established"])
        self.assertNotIn("selftests", record)
        self.assertFalse((env.fake_dir / "seen.json").exists())

    def test_installed_mode_opens_the_payload_relative_to_the_attempt_descriptor(self):
        directory, manifest, raw = self.payload(selftests=[test_entry("good")])
        with Env(self, directory=directory) as env:
            attempt = os.open(env.inbox, os.O_RDONLY | os.O_DIRECTORY)
            self.addCleanup(os.close, attempt)
            record = self.stage(env, open_payload=lambda: side.open_payload_at(attempt, ME))
        self.assertTrue(record["payload"]["staged"])
        self.assertIsNone(side.open_payload_at(self.open_dir(self.root), ME))

    def test_payload_symlink_in_the_inbox_is_not_followed(self):
        directory, manifest, raw = self.payload()
        with Env(self) as env:
            os.symlink(directory, env.inbox / "payload")
            with self.assertRaises(OSError):
                self.stage(env)


class EndpointSchemaTests(Scratch):
    """cae-qa-payload/2 (SPE-145 PR-b2): the endpoint block and needs_endpoint. /1 stays valid and means no endpoint. Until the endpoint
    lifecycle lands (ENDPOINT_RUNS_SUPPORTED False) a payload that names an endpoint is refused where payloads are accepted."""

    def v2(self, endpoint="default", tests=None, **over):
        files = dict(FILES)
        files["bin/daemon"] = (b"#!/bin/sh\nexit 0\n", "exec")
        _, manifest, _ = self.payload(files=files, selftests=[], producer_run=["harness/transcript.txt"])
        manifest["schema"] = "cae-qa-payload/2"
        manifest["endpoint"] = {"daemon": "bin/daemon", "contract": "fornax-daemon/1"} if endpoint == "default" else endpoint
        manifest["selftests"] = [dict(test_entry("t1"), needs_endpoint=False)] if tests is None else tests
        manifest.update(over)
        return manifest

    def parse(self, manifest):
        return side.parse_manifest(json.dumps(manifest).encode())

    def refused(self, manifest, word):
        with self.assertRaises(side.PayloadError) as caught:
            self.parse(manifest)
        self.assertIn(word, str(caught.exception))

    def test_a_v2_manifest_with_and_without_an_endpoint_parses(self):
        self.assertEqual(self.parse(self.v2())["endpoint"], {"daemon": "bin/daemon", "contract": "fornax-daemon/1"})
        self.assertIsNone(self.parse(self.v2(endpoint=None))["endpoint"])

    def test_v1_gets_no_endpoint_and_refuses_the_v2_keys(self):
        v1 = side.parse_manifest(self.payload(selftests=[test_entry()])[2])
        self.assertIsNone(v1["endpoint"])
        self.assertFalse(v1["selftests"][0]["needs_endpoint"])
        _, manifest, _ = self.payload(selftests=[test_entry()])
        self.refused(dict(manifest, endpoint=None), "unknown keys")
        self.refused(dict(manifest, selftests=[dict(test_entry(), needs_endpoint=False)]), "unknown keys")

    def test_v2_requires_both_new_keys(self):
        manifest = self.v2()
        del manifest["endpoint"]
        self.refused(manifest, "missing keys")
        self.refused(self.v2(tests=[test_entry("t1")]), "missing keys")

    def test_endpoint_block_rules(self):
        self.refused(self.v2(endpoint={"daemon": "bin/daemon"}), "missing keys")
        self.refused(self.v2(endpoint={"daemon": "bin/daemon", "contract": "fornax-daemon/1", "port": 1}), "unknown keys")
        self.refused(self.v2(endpoint={"daemon": "bin/daemon", "contract": "other/1"}), "contract")
        self.refused(self.v2(endpoint={"daemon": "bin/daemon", "contract": None}), "contract")
        self.refused(self.v2(endpoint={"daemon": "vectors/a.json", "contract": "fornax-daemon/1"}), "kind=exec")
        self.refused(self.v2(endpoint={"daemon": "bin/missing", "contract": "fornax-daemon/1"}), "kind=exec")
        self.refused(self.v2(endpoint={"daemon": 7, "contract": "fornax-daemon/1"}), "kind=exec")
        self.refused(self.v2(endpoint="x"), "not a JSON object")

    def test_needs_endpoint_must_be_a_boolean_and_needs_an_endpoint_block(self):
        for bad in (1, 0, "true", None, [], {}):
            self.refused(self.v2(tests=[dict(test_entry("t1"), needs_endpoint=bad)]), "needs_endpoint")
        self.refused(self.v2(endpoint=None, tests=[dict(test_entry("t1"), needs_endpoint=True)]), "needs an endpoint block")
        self.assertTrue(self.parse(self.v2(tests=[dict(test_entry("t1"), needs_endpoint=True)]))["selftests"][0]["needs_endpoint"])

    def test_schema_must_be_exactly_one_of_the_two(self):
        for bad in ("cae-qa-payload/3", "cae-qa-payload/1 ", "", None, 2):
            self.refused(self.v2(schema=bad), "schema")

    def test_the_manifest_digest_covers_the_endpoint_and_the_flag(self):
        a = json.dumps(self.v2()).encode()
        b = json.dumps(self.v2(endpoint={"daemon": "bin/tool", "contract": "fornax-daemon/1"})).encode()
        c = json.dumps(self.v2(tests=[dict(test_entry("t1"), needs_endpoint=True)])).encode()
        self.assertEqual(len({side.manifest_sha256(x) for x in (a, b, c)}), 3)

    def test_the_exec_probe_skips_endpoint_tests_and_never_picks_the_daemon(self):
        needs = dict(test_entry("t2", argv=("bin/tool", "y")), needs_endpoint=True)
        m = self.parse(self.v2(tests=[needs, dict(test_entry("t1"), needs_endpoint=False)]))
        self.assertEqual(side.exec_probe_argv(m, "/p"), ["/p/bin/tool", "x"])
        m = self.parse(self.v2(tests=[needs]))
        self.assertEqual(side.exec_probe_argv(m, "/p"), ["/p/bin/tool"])           # first exec file that is not the daemon
        only = self.v2(tests=[], endpoint={"daemon": "bin/tool", "contract": "fornax-daemon/1"})
        only["files"] = [f for f in only["files"] if f["path"] != "bin/daemon"]
        self.assertIsNone(side.exec_probe_argv(self.parse(only), "/p"))

    def test_this_build_refuses_a_payload_that_names_an_endpoint(self):
        self.assertFalse(side.ENDPOINT_RUNS_SUPPORTED)
        self.assertIsNone(side.endpoint_refusal(self.parse(self.v2(endpoint=None))))
        self.assertIn("cannot start", side.endpoint_refusal(self.parse(self.v2())))
        self.assertIsNone(side.endpoint_refusal(side.parse_manifest(self.payload(selftests=[test_entry()])[2])))

    def test_stage_refuses_before_copying_anything(self):
        directory, manifest, _ = self.payload(files=dict(FILES, **{"bin/daemon": (b"#!/bin/sh\n", "exec")}), selftests=[])
        manifest.update(schema="cae-qa-payload/2", endpoint={"daemon": "bin/daemon", "contract": "fornax-daemon/1"})
        (directory / "MANIFEST.json").write_bytes(json.dumps(manifest).encode())
        outbox = self.root / "outbox"
        outbox.mkdir()
        fd = self.open_dir(directory)
        parsed, record = side.stage_payload((os.dup(fd), ME), outbox)
        self.assertIsNone(parsed)
        self.assertFalse(record["staged"])
        self.assertIn("cannot start", record["problems"][0])
        self.assertFalse((outbox / "payload").exists())


class EndpointManifestHelperTests(Scratch):
    HELPER = SCRIPTS / "qa-payload-manifest.py"

    def run_helper(self, *argv):
        return subprocess.run([sys.executable, "-I", str(self.HELPER), *argv], capture_output=True, text=True, timeout=60)

    def make_dir(self):
        directory = self.root / "payload-dir"
        for path, (data, kind) in dict(FILES, **{"bin/daemon": (b"#!/bin/sh\n", "exec")}).items():
            (directory / path).parent.mkdir(parents=True, exist_ok=True)
            (directory / path).write_bytes(data)
            os.chmod(directory / path, 0o755 if kind == "exec" else 0o644)
        return directory

    def test_plan_writes_v2_only_when_an_endpoint_is_given(self):
        directory = self.make_dir()
        plain = self.run_helper("plan", "--dir", str(directory), "--scope", "s")
        self.assertEqual(json.loads(plain.stdout)["schema"], "cae-qa-payload/1")
        tests = self.root / "t.json"
        tests.write_text(json.dumps([test_entry("t1"), dict(test_entry("t2"), needs_endpoint=True)]))
        out = self.run_helper("plan", "--dir", str(directory), "--scope", "s", "--selftests", str(tests),
                              "--endpoint-daemon", "bin/daemon", "--endpoint-contract", "fornax-daemon/1")
        self.assertEqual(out.returncode, 0, out.stderr)
        doc = json.loads(out.stdout)
        self.assertEqual(doc["schema"], "cae-qa-payload/2")
        self.assertEqual(doc["endpoint"], {"daemon": "bin/daemon", "contract": "fornax-daemon/1"})
        self.assertEqual([t["needs_endpoint"] for t in doc["selftests"]], [False, True])

    def test_plan_refuses_half_an_endpoint_and_flag_without_endpoint(self):
        directory = self.make_dir()
        self.assertNotEqual(self.run_helper("plan", "--dir", str(directory), "--scope", "s", "--endpoint-daemon", "bin/daemon").returncode, 0)
        self.assertNotEqual(self.run_helper("plan", "--dir", str(directory), "--scope", "s", "--endpoint-contract", "fornax-daemon/1").returncode, 0)
        tests = self.root / "t.json"
        tests.write_text(json.dumps([dict(test_entry("t1"), needs_endpoint=True)]))
        out = self.run_helper("plan", "--dir", str(directory), "--scope", "s", "--selftests", str(tests))
        self.assertNotEqual(out.returncode, 0)


class ManifestHelperTests(Scratch):
    HELPER = SCRIPTS / "qa-payload-manifest.py"

    def run_helper(self, *argv):
        return subprocess.run([sys.executable, "-I", str(self.HELPER), *argv], capture_output=True, text=True, timeout=60)

    def make_dir(self):
        directory = self.root / "payload-dir"
        for path, (data, kind) in FILES.items():
            (directory / path).parent.mkdir(parents=True, exist_ok=True)
            (directory / path).write_bytes(data)
            os.chmod(directory / path, 0o755 if kind == "exec" else 0o644)
        return directory

    def test_plan_then_write_then_verify_round_trip(self):
        directory = self.make_dir()
        tests = self.root / "tests.json"
        tests.write_text(json.dumps([test_entry("t1")]))
        planned = self.run_helper("plan", "--dir", str(directory), "--scope", "offline checks", "--producer-run", "harness/transcript.txt",
                                  "--selftests", str(tests))
        self.assertEqual(planned.returncode, 0, planned.stderr)
        manifest = json.loads(planned.stdout)
        self.assertEqual([f["path"] for f in manifest["files"]], sorted(FILES))
        self.assertEqual({f["path"]: f["kind"] for f in manifest["files"]}["bin/tool"], "exec")
        self.assertIn("manifest sha256 " + SH(planned.stdout.encode()), planned.stderr)
        self.assertFalse((directory / "MANIFEST.json").exists())
        written = self.run_helper("plan", "--dir", str(directory), "--scope", "offline checks", "--producer-run", "harness/transcript.txt",
                                  "--selftests", str(tests), "--write")
        self.assertEqual(written.returncode, 0, written.stderr)
        self.assertEqual((directory / "MANIFEST.json").read_bytes(), planned.stdout.encode())
        checked = self.run_helper("verify", "--dir", str(directory))
        self.assertEqual(checked.returncode, 0, checked.stdout)
        self.assertIn("OK manifest sha256 " + SH(planned.stdout.encode()), checked.stdout)
        # the QA side's own verification agrees with the helper's
        fd = self.open_dir(directory)
        raw = (directory / "MANIFEST.json").read_bytes()
        self.assertEqual(side.verify_tree(fd, side.parse_manifest(raw), raw, ME), [])

    def test_verify_catches_every_deviation(self):
        directory = self.make_dir()
        self.run_helper("plan", "--dir", str(directory), "--scope", "s", "--write")
        (directory / "extra.txt").write_text("x")
        self.assertEqual(self.run_helper("verify", "--dir", str(directory)).returncode, 1)
        os.unlink(directory / "extra.txt")
        (directory / "vectors/a.json").write_bytes(b"changed")
        out = self.run_helper("verify", "--dir", str(directory))
        self.assertEqual(out.returncode, 1)
        self.assertIn("vectors/a.json", out.stdout)

    def test_plan_refuses_links_bad_selftests_and_unlisted_references(self):
        directory = self.make_dir()
        os.symlink("/etc/hosts", directory / "link")
        out = self.run_helper("plan", "--dir", str(directory), "--scope", "s")
        self.assertNotEqual(out.returncode, 0)
        os.unlink(directory / "link")
        tests = self.root / "bad.json"
        tests.write_text(json.dumps([test_entry("t1", argv=("bin/missing",))]))
        out = self.run_helper("plan", "--dir", str(directory), "--scope", "s", "--selftests", str(tests))
        self.assertEqual(out.returncode, 1)
        self.assertIn("kind=exec", out.stdout)
        out = self.run_helper("plan", "--dir", str(directory), "--scope", "s", "--producer-run", "bin/tool")
        self.assertEqual(out.returncode, 1)
        self.assertIn("producer_run", out.stdout)

    def test_the_helper_carries_no_second_copy_of_the_rules(self):
        text = self.HELPER.read_text()
        self.assertNotIn("cae-qa-payload/1'", text.replace("side.PAYLOAD_SCHEMA", ""))
        self.assertIn("qa-side.py", text)


if __name__ == "__main__":
    unittest.main()
