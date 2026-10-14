#!/usr/bin/env python3
"""Private launcher deployment preserves user wrappers and routing dependencies."""
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
ROOT=Path(__file__).resolve().parents[1]
class Runtime(unittest.TestCase):
    def test_complete_runtime_idempotent_and_unrelated_wrappers_preserved(self):
        with tempfile.TemporaryDirectory() as temp:
            home=Path(temp);wrapper=home/'.local/bin/ca-codex';wrapper.parent.mkdir(parents=True)
            wrapper.write_text('user-owned wrapper')
            def run(*args):
                return subprocess.run([sys.executable,str(ROOT/'scripts/launcher-install.py'),'--home',temp,*args],capture_output=True,text=True)
            self.assertEqual(run().returncode,0)
            self.assertEqual(run('--check').returncode,0)
            runtime=home/'.local/lib/coding-agent-environment'
            for item in ('bin/ca-codex','bin/ca-claude','bin/coding-agent-resource','scripts/codex-routing.py','codex/agents/reviewer.toml'):
                self.assertEqual((runtime/item).read_bytes(),(ROOT/item).read_bytes())
            self.assertEqual(wrapper.read_text(),'user-owned wrapper')
            self.assertEqual(run().returncode,0)
            self.assertFalse((home/'.local/state/coding-agent-environment/backups').exists())
            (runtime/'bin/ca-codex').write_text('drift')
            self.assertNotEqual(run().returncode,0)
            self.assertEqual((runtime/'bin/ca-codex').read_text(),'drift')
    def test_redirected_asset_parent_refused(self):
        with tempfile.TemporaryDirectory() as temp:
            home=Path(temp);runtime=home/'.local/lib/coding-agent-environment';runtime.mkdir(parents=True)
            destination=home/'unowned';destination.mkdir();(runtime/'scripts').symlink_to(destination)
            result=subprocess.run([sys.executable,str(ROOT/'scripts/launcher-install.py'),'--home',temp],capture_output=True,text=True)
            self.assertNotEqual(result.returncode,0)
            self.assertFalse((destination/'codex-routing.py').exists())
if __name__=='__main__':unittest.main()
