#!/usr/bin/env python3
"""Tests for the owner-pinned gh in scripts/release-line.py (SPE-184): a gh whose directory chain is group-writable runs only from a verified private copy.

The fixture gh lives in a 0775 directory (the Homebrew layout on a single-user Mac), the owner's release-tools.json is a file under the fixture home, and the
private copy lands under the fixture home's .local/state. Nothing here writes under the real home. No network.
"""
import hashlib
import importlib.util
import json
import os
import pathlib
import stat
import subprocess
import sys
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("rl_train_tests_for_pin", ROOT / "tests/test-release-line-train.py")
train = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(train)


def digest(b):
    return hashlib.sha256(b).hexdigest()


class ToolPin(train.TrainBase):
    def setUp(self):
        super().setUp()
        self.gbin = pathlib.Path(self.tmp.name) / "gbin"
        self.gbin.mkdir()
        self.gbin.chmod(0o775)  # group-writable, as /opt/homebrew/bin is
        self.marker = pathlib.Path(self.tmp.name) / "gh-ran.log"
        self.src = self.gbin / "gh"
        self.pinned = self.script("PINNED")
        self.set_src(self.pinned)
        self.hook(trusted=[self.gbin])
        self.pins = self.local / "release-tools.json"
        self.tools_dir = self.home / ".local/state/coding-agent-environment/release-line/tools"

    def script(self, label):
        return f'#!/bin/sh\necho "$0 {label}" >> {self.marker}\necho "{{}}"\n'.encode()

    def set_src(self, content, mode=0o755):
        tmp = self.gbin / ".swap"
        tmp.write_bytes(content)
        tmp.chmod(mode)
        os.replace(tmp, self.src)

    def hook(self, trusted):
        h = self.local / "release-line-test-env.json"
        h.write_text(json.dumps({"schema": 1, "pass": ["GIT_LOG"], "tools": {"git": str(self.bin / "git")}, "trusted_path": [str(d) for d in trusted]}))
        os.chmod(h, 0o600)

    def pin(self, *digests, raw=None, mode=0o600):
        body = raw if raw is not None else json.dumps({"schema": 1, "tools": {"gh": [{"sha256": d, "path": str(self.src), "version": "0"} for d in digests]}})
        self.pins.write_text(body)
        os.chmod(self.pins, mode)

    def run_gh(self):
        """One CLI process that needs gh; returns (refusal code or None, the refusal detail, gh invocations recorded so far)."""
        rc, out, err = self.cli("protection", "snapshot", "--branch", "main")
        step = next((s for s in (out or {}).get("steps", []) if s["step"] == "refused"), None)
        ran = self.marker.read_text().splitlines() if self.marker.exists() else []
        return (step["state"] if step else None), (step["evidence"]["detail"] if step else ""), ran

    def copies(self):
        return sorted(p for p in self.tools_dir.iterdir()) if self.tools_dir.exists() else []

    def assertRefused(self, why=""):
        before = self.marker.read_text().splitlines() if self.marker.exists() else []
        code, detail, ran = self.run_gh()
        self.assertEqual((code, ran), ("TOOL_PATH_UNTRUSTED", before), detail)
        self.assertIn(why, detail)
        return detail

    def test_pinned_gh_in_a_group_writable_directory_runs_from_a_private_copy(self):
        self.pin(digest(self.pinned))
        code, detail, ran = self.run_gh()
        self.assertEqual((code, len(ran)), (None, 1), detail)
        copy = self.tools_dir / f"gh-{digest(self.pinned)}"
        self.assertEqual(ran[0], f"{copy} PINNED")
        self.assertEqual((stat.S_IMODE(copy.stat().st_mode), copy.read_bytes()), (0o500, self.pinned))
        self.assertEqual(self.copies(), [copy])  # no temp file left
        self.assertEqual(self.run_gh()[0], None)  # the cached copy is reused
        self.assertEqual(self.copies(), [copy])

    def test_swapping_the_source_after_resolution_cannot_change_what_runs(self):
        self.pin(digest(self.pinned))
        old = os.environ.get("HOME")
        with mock.patch.dict(os.environ, {"HOME": str(self.home)}):
            spec = importlib.util.spec_from_file_location("rl_pin_mod", ROOT / "scripts/release-line.py")
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            path = mod.tool_path("gh")
            self.set_src(self.script("DECOY"))
            subprocess.run([path], check=True, capture_output=True)
            self.assertTrue(self.marker.read_text().endswith("PINNED\n"))
            self.assertTrue(path.startswith(str(self.tools_dir)))
        self.assertEqual(os.environ.get("HOME"), old)
        self.assertRefused("not pinned")  # a fresh process hashes the source again: the decoy is not pinned
        self.assertNotIn("DECOY", self.marker.read_text())

    def test_hash_mismatch_is_refused_with_the_digest_and_leaves_nothing_behind(self):
        self.pin("0" * 64)
        detail = self.assertRefused("not pinned")
        self.assertIn(digest(self.pinned), detail)
        self.assertIn(str(self.src), detail)
        self.assertIn('"sha256": "' + digest(self.pinned) + '"', detail)
        self.assertEqual(self.copies(), [])

    def test_a_group_writable_source_binary_is_refused_even_with_a_correct_pin(self):
        self.pin(digest(self.pinned))
        self.set_src(self.pinned, mode=0o775)
        self.assertRefused("source binary not trusted")
        self.assertEqual(self.copies(), [])

    def test_retargeting_a_symlink_to_an_unpinned_binary_is_refused(self):
        a, b = self.gbin / "real-a", self.gbin / "real-b"
        a.write_bytes(self.pinned)
        b.write_bytes(self.script("OTHER"))
        for f in (a, b):
            f.chmod(0o755)
        self.src.unlink()
        self.src.symlink_to(a)
        self.pin(digest(self.pinned))
        self.assertEqual(self.run_gh()[0], None)
        self.src.unlink()
        self.src.symlink_to(b)
        self.assertRefused("not pinned")
        self.assertNotIn("OTHER", self.marker.read_text())

    def test_every_pin_file_problem_is_refused_and_gh_never_runs(self):
        good = json.dumps({"schema": 1, "tools": {"gh": [{"sha256": digest(self.pinned)}]}})
        cases = {
            "missing": lambda: None,
            "unreadable": lambda: (self.pin(raw=good), os.chmod(self.pins, 0)),
            "group-writable": lambda: self.pin(raw=good, mode=0o664),
            "symlink": lambda: ((self.local / "real-pins.json").write_text(good), os.chmod(self.local / "real-pins.json", 0o600), self.pins.symlink_to(self.local / "real-pins.json")),
            "malformed": lambda: self.pin(raw="{not json"),
            "schema": lambda: self.pin(raw=good.replace('"schema": 1', '"schema": 2')),
            "short digest": lambda: self.pin(raw=good.replace(digest(self.pinned), digest(self.pinned)[:63])),
            "non-hex digest": lambda: self.pin(raw=good.replace(digest(self.pinned), "Z" * 64)),
            "uppercase digest": lambda: self.pin(raw=good.replace(digest(self.pinned), digest(self.pinned).upper())),
            "git key": lambda: self.pin(raw=json.dumps({"schema": 1, "tools": {"gh": [{"sha256": digest(self.pinned)}], "git": [{"sha256": digest(self.pinned)}]}})),
            "not an object": lambda: self.pin(raw="[]"),
        }
        for name, setup in cases.items():
            with self.subTest(name):
                if self.pins.is_symlink() or self.pins.exists():
                    os.chmod(self.pins, 0o600) if not self.pins.is_symlink() else None
                    self.pins.unlink()
                setup()
                self.assertRefused()
                self.assertEqual(self.copies(), [])

    def test_git_in_a_group_writable_chain_stays_refused_with_a_pin_file_present(self):
        self.pin(digest(self.pinned))
        wrapper = self.bin / "git"
        gitdir = pathlib.Path(self.tmp.name) / "gitbin"
        gitdir.mkdir()
        (gitdir / "git").write_bytes(wrapper.read_bytes())
        (gitdir / "git").chmod(0o755)
        gitdir.chmod(0o775)
        h = self.local / "release-line-test-env.json"
        h.write_text(json.dumps({"schema": 1, "pass": [], "trusted_path": [str(gitdir), str(self.gbin)]}))
        os.chmod(h, 0o600)
        self.install(self.v0_only_policy())
        rc, out, _ = self.cli("route", "--kind", "fix", "--version", "0.3.0", "--clone", str(self.work))
        step = next(s for s in out["steps"] if s["step"] == "refused")
        self.assertEqual(step["state"], "TOOL_PATH_UNTRUSTED")
        self.assertIn("git resolves to", step["evidence"]["detail"])
        self.assertEqual(self.copies(), [])

    def test_a_tampered_or_hard_linked_cached_copy_is_rebuilt(self):
        self.pin(digest(self.pinned))
        self.run_gh()
        copy = self.tools_dir / f"gh-{digest(self.pinned)}"
        copy.chmod(0o700)
        copy.write_bytes(self.pinned.replace(b"PINNED", b"EVIL00"))
        copy.chmod(0o500)
        self.assertEqual(self.run_gh()[2][-1], f"{copy} PINNED")
        self.assertEqual(copy.read_bytes(), self.pinned)
        other = pathlib.Path(self.tmp.name) / "second-link"
        os.link(copy, other)
        self.assertEqual(self.run_gh()[2][-1], f"{copy} PINNED")
        self.assertEqual((copy.stat().st_nlink, copy.read_bytes()), (1, self.pinned))
        self.assertEqual(self.copies(), [copy])

    def test_a_group_writable_copy_directory_chain_is_refused(self):
        self.pin(digest(self.pinned))
        (self.home / ".local/state").mkdir(parents=True)
        (self.home / ".local/state").chmod(0o775)
        detail = self.assertRefused("copy directory untrusted")
        self.assertIn(str(self.home / ".local/state"), detail)

    def test_a_trusted_chain_without_a_pin_file_runs_from_the_source_and_makes_no_copy(self):
        self.gbin.chmod(0o755)
        code, detail, ran = self.run_gh()
        self.assertEqual((code, ran), (None, [f"{self.src} PINNED"]), detail)
        self.assertFalse(self.tools_dir.exists())

    def test_the_copy_of_an_unpinned_digest_is_pruned(self):
        old, new = self.pinned, self.script("NEWER")
        self.pin(digest(old))
        self.run_gh()
        old_copy = self.tools_dir / f"gh-{digest(old)}"
        self.assertTrue(old_copy.exists())
        self.set_src(new)
        self.pin(digest(new))  # the owner re-pins after `brew upgrade gh`
        self.assertEqual(self.run_gh()[0], None)
        self.assertEqual(self.copies(), [self.tools_dir / f"gh-{digest(new)}"])


if __name__ == "__main__":
    unittest.main()
