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
        self.install = self.base / "install"           # the "root-owned copy" of the four scripts
        self.install.mkdir(mode=0o755)
        os.chmod(self.install, 0o755)
        for name in inst.BUNDLE_NAMES:
            shutil.copy(SCRIPTS / name, self.install / name)
            os.chmod(self.install / name, 0o644)
        self.layout = inst.Layout(lib=self.base / "Library/cae-qa", sudoers=self.base / "etc/sudoers.d/cae-qa", root_uid=ME,
                                  wheel_gid=os.getgid(), anchor=self.base, bundle_dir=self.install)
        self.source = self.base / "codex-src"
        self.source.write_bytes(b"\xcf\xfa\xed\xfe" + b"codex 0.163.0 pretend Mach-O payload\n" * 100)
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
        if mode == "apply":
            bundle = inst.Bundle(self.install)
            argv += ["--bundle-sha256", over.get("bundle_sha256", bundle.combined), "--helper-sha256", over.get("helper_sha256", bundle.helper_sha)]
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

    def test_seven_commands_and_exact_line_comparison(self):
        text = self.render()
        self.assertEqual(inst.sudoers_findings(text, self.render()), [])
        self.assertEqual(text.count("/bin/kill -KILL -1"), 1)
        self.assertEqual(text.count("/bin/launchctl print"), 3)
        self.assertEqual(len(re.findall(r"^\s*(?:Cmnd_Alias CAE_QA_\w+\s+= )?(?:/\S+|sha256:\S+ /\S+)", text, re.M)), 9)  # 7 + 2 helper shapes
        self.assertIn("NOPASSWD: NOSETENV:", text)
        self.assertIn("(qauser)", text)

    def test_any_deviation_from_the_rendered_lines_is_a_finding(self):
        good = self.render()
        lines = good.splitlines()
        for label, bad in (("nopasswd all", good.replace("CAE_QA_SWEEP, CAE_QA_SEE, CAE_QA_SIDE", "ALL")),
                           ("root target", good.replace("(qauser)", "(root)")),
                           ("wildcard", good.replace("/bin/kill -KILL -1", "/bin/kill *")),
                           ("setenv", good.replace("NOSETENV:", "SETENV:")),
                           ("shell", good + "bryant ALL = (qauser) NOPASSWD: /bin/sh\n"),
                           ("interpreter", good.replace("/usr/bin/atq \"\"", "/usr/bin/python3")),
                           ("extra grant", good + lines[-1] + "\n"),
                           ("dropped line", "\n".join(l for l in lines if "crontab" not in l) + "\n"),
                           ("reordered", "\n".join(reversed(lines)) + "\n"),
                           ("other uid", good.replace("user/599", "user/0")),
                           ("dotdot", good.replace("user/599, ", "user/599/../0, "))):
            self.assertTrue(inst.sudoers_findings(bad, good), label)

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
        helper_sha = hashlib.sha256((SCRIPTS / "qa-side.py").read_bytes()).hexdigest()
        want = inst.render_sudoers(MY_NAME, "fornaxqa", 777, "/Users/fornaxqa", helper_sha, lay.helper)
        self.assertEqual(inst.sudoers_findings(Path(lay.sudoers).read_text(), want), [])
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

        def tamper(args, ops, bundle, need_codex=True):
            result = real(args, ops, bundle, need_codex)
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

    def test_an_untrusted_sudoers_directory_cannot_block_removing_the_grant(self):
        self.installed()
        sudoers_dir = os.path.dirname(self.env.layout.sudoers)
        os.chmod(sudoers_dir, 0o777)                          # group/other writable: strict resolution would refuse
        with self.assertRaises(inst.Stop):
            inst.sudoers_real(self.env.layout)
        code, out = self.env.main("rollback")
        self.assertEqual(code, 0, out)
        self.assertIn("WARNING", out)
        self.assertIn("not trustworthy", out)
        self.assertFalse(os.path.lexists(self.env.layout.sudoers))
        self.assertFalse(os.path.lexists(self.env.layout.helper))

    def test_an_unremovable_grant_is_reported_with_the_manual_command_and_a_non_zero_exit(self):
        self.installed()
        os.chmod(os.path.dirname(self.env.layout.sudoers), 0o777)
        real_unlink = os.unlink

        def deny(path, *a, **k):
            if os.path.basename(str(path)) == "cae-qa":
                raise PermissionError(1, "denied")
            return real_unlink(path, *a, **k)
        with mock.patch("os.unlink", side_effect=deny):
            code, out = self.env.main("rollback")
        self.assertEqual(code, 1)
        self.assertIn("STILL HAS THE GRANT", out)
        self.assertIn("sudo rm -f %s" % self.env.layout.sudoers, out)
        self.assertFalse(os.path.lexists(self.env.layout.helper))          # the rest was still removed

    def test_a_drop_in_without_our_marker_is_not_removed_and_the_exit_is_non_zero(self):
        self.installed()
        lay = self.env.layout
        os.unlink(lay.sudoers)
        Path(lay.sudoers).write_text("# someone else's\nuser ALL = (ALL) NOPASSWD: ALL\n")
        code, out = self.env.main("rollback")
        self.assertEqual(code, 1, out)
        self.assertTrue(os.path.lexists(lay.sudoers))
        self.assertIn("NOT removed", out)
        self.assertIn("sudo rm -f %s" % lay.sudoers, out)
        self.assertFalse(os.path.lexists(lay.helper))                 # our own files still went

    def test_a_link_or_unmarked_candidate_is_left_alone_but_a_marked_one_is_removed(self):
        self.installed()
        directory = os.path.dirname(self.env.layout.sudoers)
        candidate = os.path.join(directory, ".cae-qa.new")
        Path(candidate).write_text(inst.HEAD + "\nleftover\n")
        code, out = self.env.main("rollback")
        self.assertEqual(code, 0, out)
        self.assertFalse(os.path.lexists(candidate))
        self.installed()
        os.symlink(self.env.base / "victim", candidate)
        code, out = self.env.main("rollback")
        self.assertEqual(code, 1, out)
        self.assertTrue(os.path.islink(candidate))
        self.assertIn("sudo rm -f %s" % candidate, out)

    def test_the_marker_check_does_not_make_rollback_blockable_by_an_untrusted_directory(self):
        self.installed()
        os.chmod(os.path.dirname(self.env.layout.sudoers), 0o777)
        code, out = self.env.main("rollback")
        self.assertEqual(code, 0, out)
        self.assertFalse(os.path.lexists(self.env.layout.sudoers))

    def test_a_linked_sudoers_directory_is_not_followed_and_the_exit_is_non_zero(self):
        victim = self.env.base / "elsewhere"
        victim.mkdir()
        (victim / "cae-qa").write_text("keep")
        shutil.rmtree(os.path.dirname(self.env.layout.sudoers))
        os.symlink(victim, os.path.dirname(self.env.layout.sudoers))
        code, out = self.env.main("rollback")
        self.assertEqual(code, 1, out)
        self.assertEqual((victim / "cae-qa").read_text(), "keep")             # not touched
        self.assertIn("THE PRODUCER MAY STILL HAVE THE GRANT", out)
        target = os.path.join(os.path.realpath(victim), "cae-qa")
        self.assertIn("is a link", out)
        self.assertIn("sudo rm -f %s" % target, out)
        self.assertNotIn("being removed at its fixed paths anyway", out)

    def test_a_torn_or_empty_candidate_is_left_with_exit_one_and_the_manual_command(self):
        self.installed()
        candidate = os.path.join(os.path.dirname(self.env.layout.sudoers), ".cae-qa.new")
        Path(candidate).write_text("")
        code, out = self.env.main("rollback")
        self.assertEqual(code, 1, out)
        self.assertTrue(os.path.exists(candidate))
        self.assertIn("sudo rm -f %s" % candidate, out)

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


class SmallFindingsTests(Case):
    def test_64_bit_fat_mach_o_magics_are_accepted_and_others_refused(self):
        for magic in (b"\xca\xfe\xba\xbf", b"\xbf\xba\xfe\xca", b"\xca\xfe\xba\xbe", b"\xcf\xfa\xed\xfe"):
            self.env.source.write_bytes(magic + b"payload" * 20)
            digest = hashlib.sha256(self.env.source.read_bytes()).hexdigest()
            code, out = self.env.main("plan", codex_sha256=digest)
            self.assertEqual(code, 0, (magic, out))
        self.env.source.write_bytes(b"#!/bin/sh\n" + b"x" * 40)
        code, out = self.env.main("plan", codex_sha256=hashlib.sha256(self.env.source.read_bytes()).hexdigest())
        self.assertEqual(code, 1)
        self.assertIn("not a Mach-O", out)

    def test_the_read_back_candidate_is_compared_as_exact_bytes(self):
        real = inst.read_regular

        def altered(path, *a, **k):
            data = real(path, *a, **k)
            return data.replace(b"\n", b"\r\n") if str(path).endswith(".cae-qa.new") else data
        with mock.patch.object(inst, "read_regular", altered):
            code, out = self.env.main("apply")
        self.assertEqual(code, 1)
        self.assertIn("written bytes differ", out)
        self.assertFalse(os.path.lexists(self.env.layout.sudoers))
        self.assertEqual(os.listdir(os.path.dirname(self.env.layout.sudoers)), [])

    def test_the_smoke_test_changes_directory_first(self):
        code, out = self.env.main("plan")
        self.assertIn("cd / && sudo -u fornaxqa", out)
        self.assertIn("cd / && sudo -u NAME /Library/cae-qa/codex --version", (SCRIPTS.parent / "docs/QA-HANDOFF.md").read_text())


class RootRunsOnlyTrustedCodeTests(Case):
    """H1: root must never execute or install code a less-privileged user can change."""

    def refused_apply(self, expect=None):
        code, out = self.env.main("apply")
        self.assertEqual(code, 1, out)
        self.assertIn("REFUSED", out)
        if expect:
            self.assertIn(expect, out)
        self.assertFalse(os.path.lexists(self.env.layout.lib))
        self.assertFalse(os.path.lexists(self.env.layout.sudoers))
        self.assertEqual([e for e in self.env.events if e[0] in ("fchown", "chown")], [])
        return out

    def test_a_writable_script_is_refused(self):
        for name in inst.BUNDLE_NAMES:
            env = Env()
            self.addCleanup(env.close)
            os.chmod(env.install / name, 0o664)
            code, out = (lambda lines: (inst.main(env.args("apply"), layout=env.layout, ops=env.ops, out=lines.append), "\n".join(lines)))([])
            self.assertEqual(code, 1, name)
            self.assertIn("writable by group or other", out)
            self.assertFalse(os.path.lexists(env.layout.lib))

    def test_a_writable_or_foreign_directory_in_the_chain_is_refused(self):
        os.chmod(self.env.install, 0o775)
        self.refused_apply("writable by group or other")
        os.chmod(self.env.install, 0o755)
        os.chmod(self.env.base, 0o757)
        self.refused_apply("writable by group or other")

    def test_a_chain_that_root_does_not_own_is_refused(self):
        self.env.layout.root_uid = ME + 1            # the scratch tree is owned by the current user, not by "root"
        self.env.euid = ME + 1
        self.refused_apply("not root-owned")

    def test_links_in_the_chain_or_as_scripts_are_refused(self):
        real = self.env.base / "install-real"
        os.rename(self.env.install, real)
        os.symlink(real, self.env.install)
        self.refused_apply("is a link")
        os.unlink(self.env.install)
        os.rename(real, self.env.install)
        approved = self.env.args("apply")
        victim = self.env.install / "qa-side.py"
        os.rename(victim, self.env.install / "qa-side.real")
        os.symlink(self.env.install / "qa-side.real", victim)
        lines = []
        with fake_accounts():
            code = inst.main(approved, layout=self.env.layout, ops=self.env.ops, out=lines.append)
        self.assertEqual(code, 1, lines)
        self.assertFalse(os.path.lexists(self.env.layout.lib))

    def test_rollback_also_refuses_a_writable_installer(self):
        self.installed()
        os.chmod(self.env.install / "qa-hop-install.py", 0o666)
        code, out = self.env.main("rollback")
        self.assertEqual(code, 1)
        self.assertTrue(os.path.lexists(self.env.layout.sudoers))

    def test_the_pins_are_required_checked_and_bound_to_the_bytes(self):
        bundle = inst.Bundle(self.env.install)
        for over in ({"bundle_sha256": "0" * 64}, {"helper_sha256": "0" * 64}, {"bundle_sha256": "xyz"}, {"helper_sha256": bundle.combined},
                     {"bundle_sha256": bundle.combined.upper()}):
            code, out = self.env.main("apply", **over)
            self.assertEqual(code, 1, over)
            self.assertFalse(os.path.lexists(self.env.layout.lib))
        for flag in ("--bundle-sha256", "--helper-sha256"):
            argv = self.env.args("apply")
            i = argv.index(flag)
            with self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()):
                inst.build_parser().parse_args(argv[:i] + argv[i + 2:])

    def test_a_script_changed_between_plan_and_apply_is_caught(self):
        code, plan = self.env.main("plan")
        self.assertEqual(code, 0)
        bundle = inst.Bundle(self.env.install)
        self.assertIn("--bundle-sha256 " + bundle.combined, plan)
        approved = self.env.args("apply")
        os.chmod(self.env.install / "qa-handoff.py", 0o644)
        with open(self.env.install / "qa-handoff.py", "ab") as handle:
            handle.write(b"\n# changed after approval\n")
        lines = []
        with fake_accounts():
            code = inst.main(approved, layout=self.env.layout, ops=self.env.ops, out=lines.append)
        self.assertEqual(code, 1)
        self.assertIn("do not match the digests you approved", "\n".join(lines))
        self.assertFalse(os.path.lexists(self.env.layout.lib))

    def test_the_installed_helper_is_the_bytes_the_digest_covers(self):
        self.installed()
        bundle = inst.Bundle(self.env.install)
        self.assertEqual(hashlib.sha256(Path(self.env.layout.helper).read_bytes()).hexdigest(), bundle.helper_sha)
        self.assertEqual(Path(self.env.layout.helper).read_bytes(), bundle.data["qa-side.py"])

    def test_plan_prints_every_digest_the_combined_digest_and_the_copy_step(self):
        code, out = self.env.main("plan")
        self.assertEqual(code, 0, out)
        bundle = inst.Bundle(self.env.install)
        for name, digest in bundle.sha.items():
            self.assertIn(name, out)
            self.assertIn(digest, out)
        lines = "".join("%s %s\n" % (n, bundle.sha[n]) for n in sorted(bundle.sha))
        self.assertEqual(bundle.combined, hashlib.sha256(lines.encode()).hexdigest())
        for needle in ('git -C "${REPO}" archive "${H}"', 'sudo tar -x -o -C "${D}" --strip-components=1', "chown -R -h root:wheel",
                       "chmod -h 644", "AFTER the copy", 'git -C "${REPO}" show "${H}:scripts/${f}.py" | shasum -a 256', "SHA-1 blob ids",
                       "cd / && sudo -u fornaxqa", "REFUSED: the extract holds a link",
                       "sudo /usr/bin/python3 -I -B /Library/cae-qa-install/qa-hop-install.py apply", "--bundle-sha256 " + bundle.combined,
                       "--helper-sha256 " + bundle.helper_sha, "--version", "Unverified until the owner pilot", "Mach-O"):
            self.assertIn(needle, out)

    def make_repo(self, link=None):
        repo = self.env.base / "repo"
        (repo / "scripts").mkdir(parents=True)
        for name in inst.BUNDLE_NAMES:
            shutil.copy(self.env.install / name, repo / "scripts" / name)
        if link:
            (repo / "scripts" / link).unlink()
            os.symlink("/etc/hosts", repo / "scripts" / link)
        git = ["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@example.invalid", "-c", "commit.gpgsign=false"]
        for argv in (["init", "-q"], ["add", "-A"], ["commit", "-q", "-m", "x"]):
            subprocess.run(git + argv, check=True, capture_output=True)
        head = subprocess.run(git + ["rev-parse", "HEAD"], check=True, capture_output=True, text=True).stdout.strip()
        return repo, head

    def flow(self, repo, head, target, preload=None):
        """Run the printed block with sudo and the root-only options removed, against a scratch target."""
        if preload:
            target.mkdir()
            (target / preload).write_text("old")
        text = "\n".join(inst.copy_flow())
        text = text.replace("<REPO>", str(repo)).replace("<REVIEWED_HEAD>", head).replace("D=/Library/cae-qa-install", "D='%s'" % target)
        text = text.replace("sudo install -d -m 755 -o root -g wheel", "mkdir -p").replace("sudo ", "")
        text = text.replace("chown -R -h root:wheel", "true").replace("chmod -h 644", "chmod 644")
        return subprocess.run(["/bin/sh", "-c", text], capture_output=True, text=True)

    def test_the_printed_git_flow_reproduces_the_pins_from_objects(self):
        repo, head = self.make_repo()
        target = self.env.base / "copy dir"                    # a space: every expansion must be quoted
        done = self.flow(repo, head, target)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertNotIn("REFUSED", done.stdout + done.stderr)
        for name in inst.BUNDLE_NAMES:
            self.assertEqual((target / name).read_bytes(), (self.env.install / name).read_bytes())
        lines = done.stdout.splitlines()
        shas = [l.split()[0] for l in lines]
        self.assertEqual(shas[0:8:2], shas[1:8:2], done.stdout)               # copy digest == git object digest, per file
        self.assertEqual(shas[8], inst.Bundle(self.env.install).combined)

    def test_the_printed_flow_refuses_an_extracted_link_and_a_non_empty_directory(self):
        repo, head = self.make_repo(link="qa-side.py")
        done = self.flow(repo, head, self.env.base / "copy")
        self.assertIn("REFUSED: the extract holds a link", done.stdout)
        self.assertNotEqual(done.returncode, 0)
        self.assertEqual(done.stdout.count("\n"), 1, done.stdout)          # nothing after the refusal ran: no hashes were printed
        done = self.flow(repo, head, self.env.base / "full", preload="stale.txt")
        self.assertIn("REFUSED: ", done.stdout)
        self.assertIn("already has content", done.stdout)
        self.assertNotEqual(done.returncode, 0)
        self.assertEqual(done.stdout.count("\n"), 1, done.stdout)
        self.assertEqual(sorted(p.name for p in (self.env.base / "full").iterdir()), ["stale.txt"])        # no extraction happened

    def test_the_printed_block_has_no_unquoted_expansion(self):
        def unquoted(line):
            """Positions of a '$' outside any double or single quote (command substitutions inside double quotes are protected)."""
            stack, found, i = [], [], 0
            while i < len(line):
                c, top = line[i], (stack[-1] if stack else None)
                if top == "sq":
                    stack.pop() if c == "'" else None
                elif c == "\\":
                    i += 1
                elif top == "dq" and c == '"':
                    stack.pop()
                elif top != "dq" and c == "'":
                    stack.append("sq")
                elif c == '"':
                    stack.append("dq")
                elif c == "$" and line[i + 1:i + 2] == "(":
                    if "dq" not in stack:
                        found.append(i)
                    stack.append("cmd")
                    i += 1
                elif c == ")" and top == "cmd":
                    stack.pop()
                elif c == "$" and "dq" not in stack:
                    found.append(i)
                i += 1
            return found
        flow = [l for l in inst.copy_flow() if not l.startswith("#")]
        self.assertTrue(flow)
        for line in flow:
            self.assertEqual(unquoted(line), [], line)
        self.assertEqual(unquoted("rm -rf $D"), [7])                      # the checker itself catches a bare expansion
        self.assertEqual(unquoted('echo "$(ls $D)" $E'), [16])

    def test_the_docs_block_is_the_printed_block(self):
        docs = (SCRIPTS.parent / "docs/QA-HANDOFF.md").read_text()
        self.assertIn("```\n" + "\n".join(inst.copy_flow()) + "\n```", docs)
        _, out = self.env.main("plan")
        self.assertIn("\n".join("  " + l for l in inst.copy_flow()), out)

    def test_bytecode_is_never_written(self):
        self.assertTrue(sys.dont_write_bytecode)
        self.installed()
        self.env.main("verify")
        found = [str(p) for p in self.env.base.rglob("__pycache__")] + [str(p) for p in self.env.base.rglob("*.pyc")]
        self.assertEqual(found, [])
        self.assertIn("sys.dont_write_bytecode = True", (SCRIPTS / "qa-hop-install.py").read_text())
        self.assertIn("python3 -I -B", (SCRIPTS.parent / "docs/QA-HANDOFF.md").read_text())

    def test_loaded_modules_are_executed_from_the_bytes_that_were_hashed(self):
        bundle = inst.Bundle(self.env.install)
        before = bundle.sha["qa-identity-setup.py"]
        (self.env.install / "qa-identity-setup.py").write_text("raise SystemExit('swapped after the bundle was read')\n")
        module = bundle.module("qa-identity-setup")           # still the original bytes
        self.assertTrue(hasattr(module, "NAME_RE"))
        self.assertEqual(bundle.sha["qa-identity-setup.py"], before)


class SudoersDirectoryTests(Case):
    def test_symlinked_or_writable_sudoers_dir_is_refused(self):
        d = self.env.base / "etc/sudoers.d"
        real = self.env.base / "etc/real.d"
        os.rename(d, real)
        os.symlink(real, d)
        code, out = self.env.main("apply")
        self.assertEqual(code, 1, out)
        self.assertEqual(os.listdir(real), [])
        os.unlink(d)
        os.rename(real, d)
        os.chmod(d, 0o777)
        code, out = self.env.main("apply")
        self.assertEqual(code, 1, out)
        self.assertEqual(os.listdir(d), [])

    def test_only_the_known_etc_link_is_accepted(self):
        lay = inst.Layout(sudoers="/etc/sudoers.d/cae-qa")
        with mock.patch.object(inst.os.path, "isdir", return_value=True), mock.patch.object(inst, "chain_problems", return_value=[]):
            with mock.patch.object(inst.os.path, "realpath", return_value="/private/etc/sudoers.d"):
                self.assertEqual(inst.sudoers_real(lay), "/private/etc/sudoers.d/cae-qa")
            with mock.patch.object(inst.os.path, "realpath", return_value="/Users/Shared/evil"):
                with self.assertRaises(inst.Stop):
                    inst.sudoers_real(lay)


class CodexPinTests(Case):
    def test_only_mach_o_binaries_are_accepted(self):
        for magic in inst.MACHO_MAGIC:
            self.env.source.write_bytes(magic + b"payload")
            digest = hashlib.sha256(self.env.source.read_bytes()).hexdigest()
            code, out = self.env.main("plan", codex_sha256=digest)
            self.assertEqual(code, 0, (magic, out))
        for data in (b"#!/bin/sh\necho hi\n", b"#!/usr/bin/env node\n", b"MZ\x90\x00", b"\x7fELF", b""):
            self.env.source.write_bytes(data)
            digest = hashlib.sha256(data).hexdigest()
            code, out = self.env.main("plan", codex_sha256=digest)
            self.assertEqual(code, 1, data)
            self.assertIn("Mach-O", out)


class FailClosedTests(Case):
    def test_producer_account_names(self):
        bundle = inst.Bundle(self.env.install)
        for name, good in (("Bryant", True), ("bryant.l-2", True), ("_svc", True), ("a b", False), ("x,y", False), ("#0", False),
                           ("ALL", False), ("root", False), ("1abc", False), ("a" * 40, False), ("a:b", False), ("a\nb", False)):
            record = FakePw(name, ME, "/Users/x")
            with mock.patch.object(pwd, "getpwnam", lambda n: QA if n == QA.pw_name else pwd.getpwnam.__wrapped__(n) if False else QA), \
                    mock.patch.object(pwd, "getpwuid", lambda uid: record if uid == ME else QA):
                if good:
                    self.assertEqual(inst.validate_identity(bundle, "fornaxqa", 777, ME, None)[2], name)
                else:
                    with self.assertRaises(inst.Stop, msg=repr(name)):
                        inst.validate_identity(bundle, "fornaxqa", 777, ME, None)

    def test_an_undeletable_drop_in_after_a_failed_live_check_is_reported_loudly(self):
        self.env.visudo_result = lambda argv: (1, "broken") if argv == ["/usr/sbin/visudo", "-c"] else None
        real = os.unlink

        def failing(path, *a, **k):
            if str(path).endswith("/cae-qa") and "sudoers.d" in str(path):
                raise PermissionError(1, "denied")
            return real(path, *a, **k)
        with mock.patch("os.unlink", side_effect=failing):
            code, out = self.env.main("apply")
        self.assertEqual(code, 1)
        self.assertIn("STILL ACTIVE", out)
        self.assertIn("sudo rm -f " + str(self.env.base / "etc/sudoers.d/cae-qa"), out)

    def test_failed_post_install_verification_removes_the_drop_in(self):
        with mock.patch.object(inst, "verify_installed", return_value=(None, ["pretend problem"])):
            code, out = self.env.main("apply")
        self.assertEqual(code, 1)
        self.assertIn("removed again (fail closed)", out)
        self.assertFalse(os.path.lexists(self.env.layout.sudoers))

    def test_failed_post_install_verification_and_undeletable_drop_in_is_loud(self):
        real = os.unlink

        def failing(path, *a, **k):
            if str(path).endswith("/sudoers.d/cae-qa"):
                raise PermissionError(1, "denied")
            return real(path, *a, **k)
        with mock.patch.object(inst, "verify_installed", return_value=(None, ["pretend problem"])), mock.patch("os.unlink", side_effect=failing):
            code, out = self.env.main("apply")
        self.assertEqual(code, 1)
        self.assertIn("STILL ACTIVE", out)

    def test_a_drop_in_that_reads_back_differently_is_never_installed(self):
        real = inst.read_regular
        with mock.patch.object(inst, "read_regular", side_effect=lambda path, *a: b"tampered\n" if str(path).endswith(".cae-qa.new") else real(path, *a)):
            code, out = self.env.main("apply")
        self.assertEqual(code, 1)
        self.assertFalse(os.path.lexists(self.env.layout.sudoers))
        self.assertEqual(os.listdir(os.path.dirname(self.env.layout.sudoers)), [])


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
