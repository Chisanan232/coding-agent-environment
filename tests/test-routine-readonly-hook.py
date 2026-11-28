#!/usr/bin/env python3
"""SPE-119 routine read-only hook and its installer. Offline: no claude process, settings copies in a temp dir."""
import importlib.util
import json
import os
import pathlib
import subprocess
import sys
import tempfile
import unittest

sys.dont_write_bytecode = True
ROOT = pathlib.Path(__file__).resolve().parents[1]
HOOK = ROOT / ".claude/hooks/routine-readonly-hook.py"
spec = importlib.util.spec_from_file_location("hook", HOOK)
hook = importlib.util.module_from_spec(spec)
spec.loader.exec_module(hook)
ispec = importlib.util.spec_from_file_location("inst", ROOT / "scripts/readonly-hook-install.py")
inst = importlib.util.module_from_spec(ispec)
ispec.loader.exec_module(inst)
CWD = str(ROOT)

# Shapes taken from real Remote Control prompts (Agentic Rust Engineering, Circinus), then equivalents.
ALLOW = [
    'S=/tmp/x; grep -v "waiting for a heavy lane" $S/ws-run2.log | tail -40; echo ---; grep -c foo $S/ws-run2.log',
    "sed -n 40,80p README.md; sed -n 170,235p README.md; cat .gitignore",
    "cd /tmp && git log --oneline -5 && git status --short | head",
    "tail -n 2 README.md | cut -c1-160; date -u; df -g /tmp | tail -1 | awk '{print $4}'; cd %s && python3 scripts/disk-admission.py queue status --json" % CWD,
    'until grep -q "^HEAVY_RUN_EXIT=" /tmp/a.log; do sleep 30; done; grep -v x /tmp/a.log | tail -15',
    'tail -n 0 -F /tmp/a.out | grep --line-buffered -E "a|b"',
    "rtk read README.md --tail-lines 2 | cut -c1-160", "rtk grep -v x README.md | tail -40", "rtk git status", "rtk wc -l README.md",
    "wc -l README.md | head", "git ls-files | wc -l", "stat README.md", "head -c 100 README.md", "cat README.md 2>/dev/null | head -3",
    "sed -n '1,5p' README.md", "sed -n -e 1p README.md", "sort README.md | uniq -c | head", "sort -k2,2 -n README.md | head", "sort -u README.md", "cargo metadata --offline --format-version 1 | head -c 200",
    "python3 %s/scripts/disk-admission.py queue status --json ." % CWD,  # rtk appends a bare ` .`
    'until grep -q x /tmp/a.log; do sleep 1; if [ -f /tmp/stop ]; then break; fi; done; echo done',
    "git diff --stat | tail -3", "gh pr view 5 --repo o/r --json state", "find . -name '*.py' -type f | wc -l", "jq .a /tmp/x.json",
]
FALLTHROUGH = [
    "sed -n 1p README.md > /tmp/o", "sed -i s/a/b/ README.md", "sed -n 'w /tmp/x' README.md", "sed -n '1e rm -rf /tmp/x' README.md", "sed -ni 1p README.md",
    "rm sed-notes.txt", "./sed-notes.txt", "python3 sed-notes.py", "cat ~/.ssh/id_rsa", "cat .env", "cat ~/.claude/settings.json", "rtk read ~/.claude.json",
    "grep -r TOKEN .", "cat *.pem", "git push --force origin main", "gh pr merge 5 --repo a/b", "git worktree add ../x", "pkill -TERM -f foo",
    "echo $(whoami)", "cat `ls`", "FOO=1 cat README.md", "find . -name x -delete", "awk 'BEGIN{system(\"rm x\")}'", "sort -o x README.md", "rtk proxy git status",
    "echo hi >> /tmp/x", "cat README.md &", "git -c core.pager=x log", "xargs rm", "ps aux", "git diff --output=/tmp/x", "git diff --ou=x", "cargo metadata",
    "echo $UNSET", "jq -n env", "cat README.md; rm x", "cat README.md | tee /tmp/x", "git branch -D x", "gh api -X DELETE /repos/a/b", "python3 scripts/disk-admission.py queue advance",
    "sort --out=/tmp/x README.md", "sort --outp /tmp/x README.md", "sort -ro /tmp/x README.md", "sort --compress-program=sh README.md", "uniq README.md - ",
    "uniq README.md /tmp/x", "git show HEAD:.env", "cat ~/.ss?/id_rsa", "S='a b'; cat $S", "pytest -c evil.ini", "cargo test --config x=y",
    "python3 /tmp/evil/scripts/disk-admission.py status", "cat <<EOF\nx\nEOF", "(cat README.md)", "rtk read README.md --skip-env", "date -s 1200", "cat /dev/stdin",
]


def decide(tool, **tin):
    return hook.decide({"tool_name": tool, "tool_input": tin, "cwd": CWD})[0]


class Bash(unittest.TestCase):
    def test_allow(self):
        for c in ALLOW:
            self.assertTrue(decide("Bash", command=c), c)
            self.assertTrue(decide("Monitor", command=c), c)

    def test_fall_through(self):
        for c in FALLTHROUGH:
            self.assertFalse(decide("Bash", command=c), c)

    def test_filename_containing_sed_grants_nothing(self):
        self.assertFalse(decide("Bash", command="rm sed-notes.txt"))
        self.assertFalse(decide("Bash", command="cat README.md; ./sed -n 1p x"))

    def test_glob_expanding_to_a_credential_file_falls_through(self):
        with tempfile.TemporaryDirectory() as t:
            pathlib.Path(t, ".env").write_text("K=v\n")
            pathlib.Path(t, "notes.txt").write_text("x\n")
            for cmd in ("cat .e*", "cat .en?"):
                self.assertFalse(hook.decide({"tool_name": "Bash", "tool_input": {"command": cmd}, "cwd": t})[0], cmd)
            self.assertTrue(hook.decide({"tool_name": "Bash", "tool_input": {"command": "cat n*.txt"}, "cwd": t})[0])

    def test_other_tools_untouched(self):
        self.assertIsNone(decide("Edit", file_path="x"))


class Agent(unittest.TestCase):
    def test_dispatch(self):
        self.assertTrue(decide("Agent", subagent_type="general-purpose", prompt="READ-ONLY adversarial review of PR 25 ..."))
        self.assertTrue(decide("Agent", subagent_type="Explore", prompt="find x"))
        self.assertFalse(decide("Agent", subagent_type="general-purpose", prompt="Fix the bug and push"))
        self.assertFalse(decide("Agent", subagent_type="opus-architect", prompt="design"))
        self.assertFalse(decide("Agent", subagent_type="haiku-worker", prompt="READ-ONLY"))


class Output(unittest.TestCase):
    def run_hook(self, event):
        with tempfile.TemporaryDirectory() as t:
            env = dict(os.environ, ROUTINE_READONLY_HOOK_LOG=os.path.join(t, "d.jsonl"))
            r = subprocess.run([sys.executable, str(HOOK)], input=json.dumps(event), capture_output=True, text=True, env=env)
            log = open(os.path.join(t, "d.jsonl")).read() if os.path.exists(os.path.join(t, "d.jsonl")) else ""
        return r, log

    def test_allow_only_never_deny_or_ask(self):
        r, log = self.run_hook({"tool_name": "Bash", "tool_input": {"command": "cat README.md"}, "cwd": CWD})
        self.assertEqual(json.loads(r.stdout)["hookSpecificOutput"]["permissionDecision"], "allow")
        self.assertNotIn("README", log)  # the log keeps a hash, never the command text
        r, _ = self.run_hook({"tool_name": "Bash", "tool_input": {"command": "git push --force"}, "cwd": CWD})
        self.assertEqual((r.returncode, r.stdout), (0, ""))
        r, _ = self.run_hook({"garbage": 1})
        self.assertEqual((r.returncode, r.stdout), (0, ""))


class Installer(unittest.TestCase):
    def test_plan_apply_verify_rollback(self):
        with tempfile.TemporaryDirectory() as t:
            t = pathlib.Path(t)
            live = t / "settings.json"
            live.write_text(json.dumps({"permissions": {"allow": ["A"], "ask": ["B"]}, "hooks": {"PreToolUse": [{"matcher": "Bash", "hooks": [{"type": "command", "command": "x"}]}]}}, indent=2) + "\n")
            dest = t / "hooks" / "routine-readonly-hook.py"
            base = ["--live", str(live), "--hook-dest", str(dest), "--hook-src", str(HOOK), "--backup-dir", str(t / "bk")]
            before = live.read_bytes()
            self.assertEqual(inst.main(["apply", *base, "--unmerged-ok", "--expect-hook-sha", "0" * 64, "--expect-live-sha", "0" * 64]), 3)
            self.assertEqual(live.read_bytes(), before)
            hs, ls = inst.hr.sha_bytes(HOOK.read_bytes()), inst.hr.sha_bytes(before)
            self.assertEqual(inst.main(["apply", *base, "--unmerged-ok", "--expect-hook-sha", hs, "--expect-live-sha", ls]), 0)
            new = json.loads(live.read_text())
            self.assertEqual(new["permissions"], {"allow": ["A"], "ask": ["B"]})  # no permission rule touched
            self.assertEqual([g["matcher"] for g in new["hooks"]["PreToolUse"]], ["Bash", "Bash|Monitor", "Agent"])
            self.assertEqual(inst.main(["verify", *base]), 0)
            self.assertEqual(inst.main(["apply", *base, "--unmerged-ok", "--expect-hook-sha", hs, "--expect-live-sha", ls]), 0)  # idempotent
            self.assertEqual(len(json.loads(live.read_text())["hooks"]["PreToolUse"]), 3)
            self.assertEqual(inst.main(["rollback", *base]), 0)
            self.assertEqual(json.loads(live.read_text()), json.loads(before))
            self.assertFalse(dest.exists())


if __name__ == "__main__":
    unittest.main()
