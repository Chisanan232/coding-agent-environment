#!/usr/bin/env python3
"""Offline tests for scripts/qa-hop-install.py (SPE-153, ADR 0016 PR 4). Unprivileged only.

The installer has no path flag, environment variable or prefix. The tests build a Layout over a scratch tree whose "root" is
the current user, and inject the operations that would touch the system: chown/fchown are recorders that must only ever be
asked for the scratch root uid, `visudo -c -f` runs for real but only on files inside the scratch tree, and a bare
`visudo -c` (which would read the real /etc/sudoers) is answered by a stub. No sudo, no model, no real /Library or /etc."""
import contextlib
import hashlib
import importlib.util
import io
import json
import os
import pwd
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from collections import namedtuple
from pathlib import Path
from unittest import mock

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
spec = importlib.util.spec_from_file_location("qa_hop_install_under_test", SCRIPTS / "qa-hop-install.py")
inst = importlib.util.module_from_spec(spec)
spec.loader.exec_module(inst)

ME = os.getuid()
MY_NAME = pwd.getpwuid(ME).pw_name
FakePw = namedtuple("FakePw", "pw_name pw_uid pw_dir")
QA = FakePw("fornaxqa", 777, "/Users/fornaxqa")
HAVE_VISUDO = os.access("/usr/sbin/visudo", os.X_OK)


class Env:
    def __init__(self):
        self.base = Path(os.path.realpath(tempfile.mkdtemp(prefix="qa-hop-install-test-")))
        os.chmod(self.base, 0o755)
        (self.base / "Library").mkdir(mode=0o755)
        (self.base / "etc/sudoers.d").mkdir(parents=True, mode=0o755)
        for d in (self.base / "Library", self.base / "etc", self.base / "etc/sudoers.d"):
            os.chmod(d, 0o755)
        self.layout = inst.Layout(lib=self.base / "Library/cae-qa", sudoers=self.base / "etc/sudoers.d/cae-qa", root_uid=ME,
                                  wheel_gid=os.getgid(), anchor=self.base)
        self.source = self.base / "codex-src"
        self.source.write_bytes(b"#!/bin/sh\necho codex 0.163.0\n" * 100)
        os.chmod(self.source, 0o755)
        self.digest = hashlib.sha256(self.source.read_bytes()).hexdigest()
        self.events = []
        self.euid = ME
        self.caller = ME
        self.visudo_result = None
        self.ops = inst.Ops(run=self.run, fchown=self.fchown, chown=self.chown, geteuid=lambda: self.euid, caller_uid=lambda: self.caller)

    def fchown(self, fd, uid, gid):
        assert uid == self.layout.root_uid and gid == self.layout.wheel_gid, "chown to something other than the scratch root"
        self.events.append(("fchown", uid))

    def chown(self, path, uid, gid):
        assert uid == self.layout.root_uid, "chown to something other than the scratch root"
        assert str(path).startswith(str(self.base)), "chown outside the scratch tree"
        self.events.append(("chown", str(path)))

    def run(self, argv):
        self.events.append(("run", tuple(argv)))
        if self.visudo_result is not None and argv[-2:-1] != []:
            result = self.visudo_result(argv)
            if result is not None:
                return result
        assert argv[0] == "/usr/sbin/visudo", argv
        if argv[1:3] == ["-c", "-f"]:
            assert str(argv[3]).startswith(str(self.base)) or str(argv[3]).startswith(tempfile.gettempdir()) or "/var/folders" in str(argv[3]), argv
            if not HAVE_VISUDO:
                return 0, "%s: parsed OK" % argv[3]
            done = subprocess.run(argv, capture_output=True, text=True, stdin=subprocess.DEVNULL, timeout=30)
            return done.returncode, done.stdout + done.stderr
        assert argv == ["/usr/sbin/visudo", "-c"], argv
        return 0, "/etc/sudoers: parsed OK"            # stub: the real one would read the real sudoers

    def args(self, mode="plan", **over):
        values = dict(qa_name="fornaxqa", qa_uid=777, exchange=None, producer_uid=ME, codex_source=str(self.source),
                      codex_sha256=self.digest)
        values.update(over)
        argv = [mode, "--qa-name", values["qa_name"], "--qa-uid", str(values["qa_uid"])]
        if values["producer_uid"] is not None:
            argv += ["--producer-uid", str(values["producer_uid"])]
        if mode != "rollback":
            argv += ["--codex-source", values["codex_source"], "--codex-sha256", values["codex_sha256"]]
        return argv

    def main(self, mode="plan", **over):
        lines = []
        with fake_accounts():
            code = inst.main(self.args(mode, **over), layout=self.layout, ops=self.ops, out=lines.append)
        return code, "\n".join(lines)

    def close(self):
        for root, dirs, files in os.walk(self.base):
            for name in dirs + files:
                p = os.path.join(root, name)
                if not os.path.islink(p):
                    os.chmod(p, 0o700)
        shutil.rmtree(self.base, ignore_errors=True)


@contextlib.contextmanager
def fake_accounts():
    real_name, real_uid = pwd.getpwnam, pwd.getpwuid

    def by_name(name):
        return QA if name == QA.pw_name else real_name(name)

    def by_uid(uid):
        return QA if uid == QA.pw_uid else real_uid(uid)
    with mock.patch.object(pwd, "getpwnam", by_name), mock.patch.object(pwd, "getpwuid", by_uid):
        yield


class Case(unittest.TestCase):
    def setUp(self):
        self.env = Env()
        self.addCleanup(self.env.close)
        stack = contextlib.ExitStack()
        self.addCleanup(stack.close)
        # tripwires: no real sudo, no process launch from the module, no chown to uid 0
        for name in ("os.chown", "os.fchown", "os.lchown"):
            stack.enter_context(mock.patch(name, side_effect=AssertionError("unexpected real chown")))
        stack.enter_context(mock.patch.object(inst.Ops, "_run", side_effect=AssertionError("the real command runner was used")))

    def installed(self):
        code, out = self.env.main("apply")
        self.assertEqual(code, 0, out)
        return out


class RenderTests(Case):
    def render(self, helper_sha="a" * 64):
        return inst.render_sudoers(MY_NAME, "qauser", 599, "/Users/qauser", helper_sha)

    def test_render_equals_the_adr_drop_in(self):
        adr = (SCRIPTS.parent / "docs/adr/0016-unattended-qa-hop.md").read_text()
        i = adr.index("# cae-qa (SPE-153")
        block = adr[i:adr.index("```", i)]
        for old, new in (("QAUID", "599"), ("HELPERHEX", "a" * 64), ("Users/QA/", "Users/qauser/"), ("Defaults>QA", "Defaults>qauser"),
                         ("(QA)", "(qauser)"), ("PRODUCER", MY_NAME)):
            block = block.replace(old, new)
        self.assertEqual(self.render().rstrip("\n"), block.rstrip("\n"))

    def test_seven_commands_and_no_findings(self):
        text = self.render()
        self.assertEqual(inst.sudoers_findings(text, "qauser"), [])
        self.assertEqual(text.count("/bin/kill -KILL -1"), 1)
        self.assertEqual(text.count("/bin/launchctl print"), 3)
        self.assertEqual(len(re.findall(r"^\s*(?:Cmnd_Alias CAE_QA_\w+\s+= )?(?:/\S+|sha256:\S+ /\S+)", text, re.M)), 9)  # 7 + 2 helper shapes
        self.assertIn("NOPASSWD: NOSETENV:", text)
        self.assertIn("(qauser)", text)

    def test_widened_variants_are_flagged(self):
        good = self.render()
        for label, bad in (("nopasswd all", good.replace("CAE_QA_SWEEP, CAE_QA_SEE, CAE_QA_SIDE", "ALL")),
                           ("root target", good.replace("(qauser)", "(root)")),
                           ("all target", good.replace("(qauser)", "(ALL)")),
                           ("wildcard", good.replace("/bin/kill -KILL -1", "/bin/kill *")),
                           ("setenv", good.replace("NOSETENV:", "SETENV:")),
                           ("shell", good + "bryant ALL = (qauser) NOPASSWD: /bin/sh\n"),
                           ("interpreter", good.replace("/usr/bin/atq \"\"", "/usr/bin/python3")),
                           ("extra grant", good + good.splitlines()[-1] + "\n"),
                           ("dotdot", good.replace("user/599, ", "user/599/../0, "))):
            self.assertTrue(inst.sudoers_findings(bad, "qauser"), label)

    @unittest.skipUnless(HAVE_VISUDO, "visudo not available")
    def test_visudo_parses_the_rendered_text_unprivileged(self):
        path = self.env.base / "rendered"
        path.write_text(self.render())
        os.chmod(path, 0o440)
        done = subprocess.run(["/usr/sbin/visudo", "-c", "-f", str(path)], capture_output=True, text=True, timeout=30)
        self.assertIn("parsed OK", done.stdout + done.stderr)
        self.assertEqual(done.returncode, 0)


class InputTests(Case):
    def refused(self, mode="plan", **over):
        code, out = self.env.main(mode, **over)
        self.assertEqual(code, 1, out)
        self.assertIn("REFUSED", out)
        return out

    def test_identity_and_pin_refusals(self):
        self.refused(qa_name="Root")
        self.refused(qa_name="nosuchaccountxyz")
        self.refused(qa_uid=778)
        self.refused(qa_uid=0)
        self.refused(producer_uid=777)                    # the QA user cannot be the producer
        self.refused(producer_uid=0)
        self.refused(codex_sha256="0" * 64)
        self.refused(codex_sha256="ABC")
        self.refused(codex_source="relative/codex")
        self.refused(codex_source=str(self.env.base / "x/../codex-src"))

    def test_symlinked_or_missing_codex_source_is_refused(self):
        link = self.env.base / "codex-link"
        os.symlink(self.env.source, link)
        self.refused(codex_source=str(link))
        code, out = self.env.main("plan", codex_source=str(self.env.base / "absent"))
        self.assertEqual(code, 1)
        self.assertIn("FAILED", out)

    def test_no_flag_can_move_paths_or_loosen_the_pin(self):
        for flag in (["--root-prefix", "/tmp"], ["--lib", "/tmp"], ["--sudoers", "/tmp/x"], ["--shell", "/bin/sh"]):
            with self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()):
                inst.build_parser().parse_args(self.env.args("plan") + flag)
        with self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()):
            inst.build_parser().parse_args(["plan", "--qa-name", "x", "--qa-uid", "7"])      # the pin is required, no default
        self.assertEqual(inst.Layout().lib, "/Library/cae-qa")
        self.assertEqual(inst.Layout().sudoers, "/etc/sudoers.d/cae-qa")


class PlanTests(Case):
    def test_plan_prints_everything_and_changes_nothing(self):
        before = sorted(str(p) for p in self.env.base.rglob("*"))
        code, out = self.env.main("plan")
        self.assertEqual(code, 0, out)
        self.assertEqual(sorted(str(p) for p in self.env.base.rglob("*")), before)
        helper_sha = hashlib.sha256((SCRIPTS / "qa-side.py").read_bytes()).hexdigest()
        for needle in (str(self.env.layout.helper), str(self.env.layout.config), str(self.env.layout.codex), str(self.env.layout.sudoers),
                       "root:wheel 0555", "root:wheel 0444", "root:wheel 0440", helper_sha, self.env.digest, '"qa_uid": 777',
                       "NOPASSWD: NOSETENV:", "sha256:" + helper_sha, "nothing is downloaded", "Rollback", "Eliminated", "Still yours",
                       "apply --qa-name fornaxqa", "rollback --qa-name fornaxqa", "visudo -c -f", "parsed OK"):
            self.assertIn(needle, out)
        self.assertEqual([e for e in self.env.events if e[0] in ("fchown", "chown")], [])

    def test_plan_runs_only_visudo_on_a_scratch_file(self):
        self.env.main("plan")
        runs = [e[1] for e in self.env.events if e[0] == "run"]
        self.assertEqual(len(runs), 1)
        self.assertEqual(runs[0][:3], ("/usr/sbin/visudo", "-c", "-f"))


class ApplyTests(Case):
    def test_apply_installs_everything_in_order_with_exact_modes(self):
        out = self.installed()
        lay = self.env.layout
        for path, mode in ((lay.helper, 0o555), (lay.config, 0o444), (lay.codex, 0o555), (lay.sudoers, 0o440), (lay.lib, 0o755)):
            st = os.lstat(path)
            self.assertFalse(stat.S_ISLNK(st.st_mode))
            self.assertEqual(stat.S_IMODE(st.st_mode), mode, path)
        self.assertEqual(Path(lay.helper).read_bytes(), (SCRIPTS / "qa-side.py").read_bytes())
        self.assertEqual(hashlib.sha256(Path(lay.codex).read_bytes()).hexdigest(), self.env.digest)
        config = json.loads(Path(lay.config).read_text())
        self.assertEqual(config, {"qa_name": "fornaxqa", "qa_uid": 777, "producer_uid": ME, "exchange": "/Users/Shared/fornaxqa-qa",
                                  "codex_path": lay.codex, "codex_sha256": self.env.digest})
        planned = inst.sudoers_findings(Path(lay.sudoers).read_text(), "fornaxqa")
        self.assertEqual(planned, [])
        self.assertEqual(sorted(os.listdir(lay.lib)), ["codex", "qa-side", "qa-side.json"])
        self.assertEqual(sorted(os.listdir(os.path.dirname(lay.sudoers))), ["cae-qa"])           # no temp files left
        runs = [e[1] for e in self.env.events if e[0] == "run"]
        self.assertEqual([r[:3] for r in runs], [("/usr/sbin/visudo", "-c", "-f"), ("/usr/sbin/visudo", "-c")])   # candidate, then live
        self.assertTrue(runs[0][3].endswith("/.cae-qa.new"))
        self.assertIn("verified:", out)
        self.assertTrue(all(e[1] == lay.root_uid for e in self.env.events if e[0] == "fchown"))

    def test_the_drop_in_is_installed_last_and_validated_before_the_move(self):
        order = []
        real_install, real_rename = inst.install_file, os.rename
        with mock.patch.object(inst, "install_file", side_effect=lambda lay, ops, dest, *a, **k: (order.append(os.path.basename(dest)), real_install(lay, ops, dest, *a, **k))[1]), \
                mock.patch("os.rename", side_effect=lambda a, b: (order.append("rename:" + os.path.basename(b)), real_rename(a, b))[1]):
            self.installed()
        self.assertEqual([o for o in order if not o.startswith("rename")], ["qa-side", "qa-side.json", "codex", ".cae-qa.new"])
        self.assertLess(order.index("rename:codex"), order.index("rename:cae-qa"))
        self.assertEqual(order[-1], "rename:cae-qa")
        runs = [i for i, e in enumerate(self.env.events) if e[0] == "run"]
        self.assertTrue(runs)

    def test_apply_refuses_unless_root_and_the_caller_is_known(self):
        self.env.euid = ME + 1
        code, out = self.env.main("apply")
        self.assertEqual(code, 1)
        self.assertIn("must run as root", out)
        self.assertFalse(os.path.lexists(self.env.layout.lib))
        self.env.euid = ME
        self.env.caller = None
        code, out = self.env.main("apply")
        self.assertEqual(code, 1, out)
        self.assertFalse(os.path.lexists(self.env.layout.lib))
        self.env.caller = ME + 5
        code, out = self.env.main("apply")
        self.assertEqual(code, 1, out)
        self.assertFalse(os.path.lexists(self.env.layout.sudoers))

    def test_the_real_default_layout_refuses_a_non_root_apply_without_touching_anything(self):
        existed = os.path.lexists("/Library/cae-qa"), os.path.lexists("/etc/sudoers.d/cae-qa")
        out = io.StringIO()
        with fake_accounts():
            code = inst.main(self.env.args("apply"), ops=inst.Ops(run=self.env.run), out=lambda t: out.write(t + "\n"))
        self.assertEqual(code, 1)
        self.assertEqual((os.path.lexists("/Library/cae-qa"), os.path.lexists("/etc/sudoers.d/cae-qa")), existed)

    def test_visudo_rejection_installs_no_drop_in(self):
        self.env.visudo_result = lambda argv: (1, "syntax error") if argv[1:3] == ["-c", "-f"] else None
        code, out = self.env.main("apply")
        self.assertEqual(code, 1)
        self.assertIn("visudo rejected", out)
        self.assertFalse(os.path.lexists(self.env.layout.sudoers))
        self.assertEqual(os.listdir(os.path.dirname(self.env.layout.sudoers)), [])

    def test_live_check_failure_removes_the_drop_in_again(self):
        self.env.visudo_result = lambda argv: (1, "live config broken") if argv == ["/usr/sbin/visudo", "-c"] else None
        code, out = self.env.main("apply")
        self.assertEqual(code, 1)
        self.assertIn("removed again", out)
        self.assertFalse(os.path.lexists(self.env.layout.sudoers))

    def test_source_changed_after_the_plan_is_caught_while_copying(self):
        real = inst.resolve_inputs

        def tamper(args, ops, need_codex=True):
            result = real(args, ops, need_codex)
            self.env.source.write_bytes(b"swapped after hashing")
            return result
        with mock.patch.object(inst, "resolve_inputs", side_effect=tamper):
            code, out = self.env.main("apply")
        self.assertEqual(code, 1)
        self.assertIn("digest mismatch", out)
        self.assertFalse(os.path.lexists(self.env.layout.sudoers))
        self.assertFalse(os.path.lexists(self.env.layout.codex))
        self.assertEqual([n for n in os.listdir(self.env.layout.lib) if n.startswith(".")], [])

    def test_unsafe_existing_lib_directory_is_refused(self):
        lay = self.env.layout
        os.mkdir(lay.lib, 0o777)
        os.chmod(lay.lib, 0o777)
        code, out = self.env.main("apply")
        self.assertEqual(code, 1, out)
        self.assertEqual(os.listdir(lay.lib), [])
        os.rmdir(lay.lib)
        elsewhere = self.env.base / "elsewhere"
        elsewhere.mkdir()
        os.symlink(elsewhere, lay.lib)
        code, out = self.env.main("apply")
        self.assertEqual(code, 1, out)
        self.assertEqual(os.listdir(elsewhere), [])

    def test_a_writable_parent_is_refused(self):
        os.chmod(self.env.base / "Library", 0o777)
        code, out = self.env.main("apply")
        self.assertEqual(code, 1, out)
        self.assertFalse(os.path.lexists(self.env.layout.lib))

    def test_reapply_is_safe(self):
        self.installed()
        self.installed()
        self.assertEqual(sorted(os.listdir(self.env.layout.lib)), ["codex", "qa-side", "qa-side.json"])


class VerifyTests(Case):
    def test_verified_after_apply_and_each_tamper_is_caught(self):
        self.installed()
        code, out = self.env.main("verify")
        self.assertEqual(code, 0, out)
        self.assertIn("VERIFIED", out)
        lay = self.env.layout
        for label, tamper in (
                ("helper writable", lambda: os.chmod(lay.helper, 0o575)),
                ("codex drift", lambda: (os.chmod(lay.codex, 0o755), Path(lay.codex).write_bytes(b"x"), os.chmod(lay.codex, 0o555))),
                ("drop-in mode", lambda: os.chmod(lay.sudoers, 0o444)),
                ("drop-in gone", lambda: os.unlink(lay.sudoers)),
                ("config gone", lambda: os.unlink(lay.config))):
            fresh = Env()
            self.addCleanup(fresh.close)
            with fake_accounts():
                self.assertEqual(inst.main(fresh.args("apply"), layout=fresh.layout, ops=fresh.ops, out=lambda t: None), 0)
                fl = fresh.layout
                {"helper writable": lambda: os.chmod(fl.helper, 0o575),
                 "codex drift": lambda: (os.chmod(fl.codex, 0o755), Path(fl.codex).write_bytes(b"x"), os.chmod(fl.codex, 0o555)),
                 "drop-in mode": lambda: os.chmod(fl.sudoers, 0o444), "drop-in gone": lambda: os.unlink(fl.sudoers),
                 "config gone": lambda: os.unlink(fl.config)}[label]()
                lines = []
                self.assertEqual(inst.main(fresh.args("verify"), layout=fl, ops=fresh.ops, out=lines.append), 1, label)
                self.assertIn("NOT VERIFIED", "\n".join(lines))

    def test_verify_changes_nothing_and_needs_no_root(self):
        self.installed()
        self.env.euid = ME + 1
        before = sorted((str(p), p.stat().st_mtime_ns) for p in self.env.base.rglob("*"))
        code, out = self.env.main("verify")
        self.assertEqual(code, 0, out)
        self.assertEqual(sorted((str(p), p.stat().st_mtime_ns) for p in self.env.base.rglob("*")), before)


class RollbackTests(Case):
    def test_removes_the_drop_in_first_then_the_files_and_is_idempotent(self):
        self.installed()
        order = []
        real_unlink = os.unlink
        with mock.patch("os.unlink", side_effect=lambda p, *a, **k: (order.append(os.path.basename(str(p))), real_unlink(p, *a, **k))[1]):
            code, out = self.env.main("rollback")
        self.assertEqual(code, 0, out)
        self.assertEqual(order[0], "cae-qa")
        self.assertEqual(set(order), {"cae-qa", "qa-side", "qa-side.json", "codex"})
        self.assertFalse(os.path.lexists(self.env.layout.lib))
        self.assertFalse(os.path.lexists(self.env.layout.sudoers))
        self.assertIn("removed:", out)
        code, out = self.env.main("rollback")
        self.assertEqual(code, 0)
        self.assertIn("nothing (already rolled back)", out)

    def test_requires_root(self):
        self.installed()
        self.env.euid = ME + 1
        code, out = self.env.main("rollback")
        self.assertEqual(code, 1)
        self.assertTrue(os.path.lexists(self.env.layout.sudoers))

    def test_foreign_files_and_links_are_not_followed_or_deleted(self):
        self.installed()
        lay = self.env.layout
        Path(lay.lib, "mine.txt").write_text("not ours")
        victim = self.env.base / "victim"
        victim.write_text("keep")
        os.unlink(lay.codex)
        os.symlink(victim, lay.codex)
        code, out = self.env.main("rollback")
        self.assertEqual(code, 0, out)
        self.assertEqual(victim.read_text(), "keep")                      # the link was removed, its target untouched
        self.assertTrue(Path(lay.lib, "mine.txt").exists())
        self.assertIn("left", out)
        self.assertFalse(os.path.lexists(lay.sudoers))

    def test_a_directory_in_place_of_a_file_is_refused(self):
        self.installed()
        os.unlink(self.env.layout.codex)
        os.mkdir(self.env.layout.codex)
        code, out = self.env.main("rollback")
        self.assertEqual(code, 1)
        self.assertFalse(os.path.lexists(self.env.layout.sudoers))          # the grant was already gone before the refusal


class SourceHygieneTests(unittest.TestCase):
    def test_no_password_handling_no_shell_and_sudo_never_invoked(self):
        text = (SCRIPTS / "qa-hop-install.py").read_text()
        for word in ("getpass", "input(", "shell=True", "os.system", "os.popen", "Popen"):
            self.assertNotIn(word, text)
        self.assertEqual(text.count("subprocess.run"), 1)             # the one injectable runner, fixed argv, no shell

    def test_pinned_paths_match_the_producer_side_constants(self):
        side_spec = importlib.util.spec_from_file_location("qa_side_for_hop_test", SCRIPTS / "qa-side.py")
        side = importlib.util.module_from_spec(side_spec)
        side_spec.loader.exec_module(side)
        lay = inst.Layout()
        self.assertEqual((lay.helper, lay.config, os.path.dirname(lay.codex)), (side.HELPER_PATH, side.CONFIG_PATH, "/Library/cae-qa"))


if __name__ == "__main__":
    unittest.main()
