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

MV = os.path.expanduser("~/Bryant-Developments/coding-agent-environment-main-view")
# Shapes taken from real Remote Control prompts (Agentic Rust Engineering, Circinus), then equivalents.
ALLOW = [
    'S=/tmp/x; grep -v "waiting for a heavy lane" $S/ws-run2.log | tail -40; echo ---; grep -c foo $S/ws-run2.log',
    "sed -n 40,80p README.md; sed -n 170,235p README.md; cat .gitignore",
    "cd /tmp && git log --oneline -5 && git status --short | head",
    "tail -n 2 README.md | cut -c1-160; date -u; df -g /tmp | tail -1 | awk '{print $4}'; cd %s && python3 scripts/disk-admission.py queue status --json" % MV,
    "python3 %s/scripts/disk-admission.py queue status --json ." % MV,  # rtk appends a bare ` .`
    'until grep -q "^HEAVY_RUN_EXIT=" /tmp/a.log; do sleep 30; done; grep -v x /tmp/a.log | tail -15',
    'until grep -q x /tmp/a.log; do sleep 1; if [ -f /tmp/stop ]; then break; fi; done; echo done',
    'tail -n 0 -F /tmp/a.out | grep --line-buffered -E "a|b"',
    "rtk read README.md --tail-lines 2 | cut -c1-160", "rtk read README.md --max-lines 20", "rtk grep -v x README.md | tail -40", "rtk git status", "rtk wc -l README.md",
    "rtk grep marker src", "wc -l README.md | head", "git ls-files | wc -l", "stat README.md", "head -c 100 README.md", "cat README.md 2>/dev/null | head -3",
    "sed -n '1,5p' README.md", "sed -n -e 1p README.md", "sort README.md | uniq -c | head", "sort -k2,2 -n README.md | head", "sort -u README.md",
    "cargo metadata --offline --format-version 1 | head -c 200", "printf '%s\n' a", "[ -f README.md ] && echo yes", "git diff --stat | tail -3", "git log --oneline -5 --format=%H", "gh pr view 5 --repo o/r --json state",
    "find . -name '*.py' -type f | wc -l", "jq .a /tmp/x.json", "diff -u README.md /tmp/x", "python3 %s/scripts/memory-admission.py check --weight heavy" % MV,
]
FALLTHROUGH = [
    "printf -v 'a[$(echo X)]' x", "[ -v 'a[$(echo X)]' ]", "test -v x", "for PATH in /tmp; do cat x; done", "for HOME in /tmp; do ls; done", "rtk grep x . -- --pre ./a.sh",
    "rtk grep x . -- --hidden", "rtk grep . . -g '.[e]nv'", "rtk grep . ~/Bryant-Developments", "gh pr view 1 --json body -cq env", "git diff --help", "git log -h",
    "cd ~bryant; cat .claude.json", "cargo metadata --offline --locked ; frobnicate", "env X=1 ls", "FOO=1 ls; echo hi",
    "sed -n 1p README.md > /tmp/o", "sed -i s/a/b/ README.md", "sed -n 'w /tmp/x' README.md", "sed -n '1e rm -rf /tmp/x' README.md", "sed -ni 1p README.md",
    "sed --in-pl s/a/b/ README.md", "rm sed-notes.txt", "./sed-notes.txt", "python3 sed-notes.py", "git push --force origin main", "gh pr merge 5 --repo a/b",
 "echo $(whoami)", "cat `ls`", "FOO=1 cat README.md", "find . -name x -delete", "awk 'BEGIN{system(\"rm x\")}'",
    "sort -o x README.md", "sort --out=/tmp/x README.md", "sort --outp /tmp/x README.md", "sort -ro /tmp/x README.md", "sort --compress-program=sh README.md",
    "uniq README.md /tmp/x", "uniq README.md - ", "rtk proxy git status", "echo hi >> /tmp/x", "cat README.md &", "git -c core.pager=x log", "xargs rm", "ps aux",
    "git diff --output=/tmp/x", "git diff --ou=x", "git diff --ex x", "cargo metadata", "cargo test", "cargo build --artifact-dir x", "pytest -x", "python3 -m pytest -qp evil",
    "echo $UNSET", "jq -n env", "jq -f x.jq", "jq --rawfile a README.md .", "cat README.md; rm x", "cat README.md | tee /tmp/x", "git branch -D x",
    "git branch --set-upstream-to=origin/main", "git branch -uorigin/main", "git tag -n -d v1", "git remote get-url origin", "gh api -X DELETE /repos/a/b",
    "gh pr view 1 --json title --jq env", "gh pr view 1 -q env", "python3 scripts/disk-admission.py queue advance", "python3 /tmp/evil/scripts/disk-admission.py status",
    "python3 %s/scripts/memory-admission.py check --reserve x" % CWD, "cat <<EOF\nx\nEOF", "(cat README.md)", "rtk read README.md --skip-env", "date -s 1200",
    "cat a # '\nrm -rf /tmp/zz\n# '", "PATH=/tmp/bin; ls", "GIT_EXTERNAL_DIFF=/tmp/x; git diff", "IFS=a; ls", "HOME=/x; cat ~/known_hosts", "diff -rN /nonexistent ~",
    "diff -r ~ /tmp", "grep --rec -n AKIA ~", "grep --dir=recurse x ~", "grep -r x .", "rtk grep -. foo ~", "rtk grep foo ~", "rtk grep foo /etc", "tree -ao /tmp/x",
    "tree --output=/tmp/x", "file -C -m /tmp/x", "cd -", "cd -P x", "git branch newname", "git branch -f x", "awk 'BEGIN{ARGV[1]=\".env\"}1' README.md", "tree -I -o x", "echo a;; echo b",
]
ASK = [
    "cat ~/.CLAUDE.json", "cat ~/.Claude/.credentials.json", "cat ~/.claude.json.backup", "cat ~/**/.claude.json", "cat ~/.codex/auth.json", "cat ~/.wrangler/config/default.toml",
    "cat ~/.cache/gh/x", "FOO=1 ls; pkill x", "ls > o; pkill x", "ls $(x); rm -rf /", "env X=1 rm -rf x", "sudo -n rm -rf x", "/bin/rm -rf x",
    "xargs rm -rf", "rm --rec x", "find . -delete", "git push --force-with-lease", "git push origin +main", "git push -f", "git reset --hard HEAD~1", "git clean -fd",
    "git branch -D x", "git tag -d v1", "gh pr merge 5", "git stash drop",
    "git worktree add ../x", "rtk git worktree add ../x", "git -C /tmp/r worktree remove x", "pkill -TERM -f foo", "kill 99999", "rm -rf build", "rm -f x", "rtk proxy pkill x",
    "cat README.md; git worktree add ../x", "frobnicate; pkill x",  # credential stores: never silent, never a hook allow; the owner can authorize a disclosure
    "cat ~/.ssh/id_rsa", "cat .env", "cat config/production.env", "cat app.env", "cat ~/.claude/settings.json", "rtk read ~/.claude.json", "cat *.pem", "cat ~/.ss?/id_rsa",
    "git show HEAD:.env", "git show HEAD:config/prod.env", "cat ~/.vault-token", "cat ~/.boto", "cat ~/.m2/settings.xml", "cat ~/.zsh_sessions/x", "cat ~/.zshrc", "cd; cat .ssh/config", "cat /dev/stdin",
    "grep x ~/.aws/credentials", "file -f.env x", "cat --  ~/.ssh/config", "cd ~/.ssh && ls", "S='a b'; cat $S",
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
            self.assertIn(decide("Bash", command=c), (False, "ask"), c)  # never allow; a dangerous keyword may turn the fall-through into an ask

    def test_credential_reads_ask(self):
        for c in ASK:
            ok = decide("Bash", command=c)
            self.assertIn(ok, ("ask", False), c)  # an unknown-variable or syntax reject may fall through instead; it must never allow
            self.assertNotEqual(ok, True, c)
        for c in ASK[:60]:
            if c.startswith(('cat ~/**', 'S=')):
                continue
            self.assertEqual(decide("Bash", command=c), "ask", c)

    def test_filename_containing_sed_grants_nothing(self):
        self.assertFalse(decide("Bash", command="rm sed-notes.txt"))
        self.assertFalse(decide("Bash", command="cat README.md; ./sed -n 1p x"))

    def test_glob_expanding_to_a_credential_file_asks(self):
        with tempfile.TemporaryDirectory() as t:
            pathlib.Path(t, ".env").write_text("K=v\n")
            pathlib.Path(t, "notes.txt").write_text("x\n")
            for cmd in ("cat .e*", "cat .en?"):
                self.assertEqual(hook.decide({"tool_name": "Bash", "tool_input": {"command": cmd}, "cwd": t})[0], "ask", cmd)
            self.assertTrue(hook.decide({"tool_name": "Bash", "tool_input": {"command": "cat n*.txt"}, "cwd": t})[0])

    def test_cd_through_a_symlink_and_dotdot(self):
        with tempfile.TemporaryDirectory() as t:
            os.makedirs(os.path.join(t, "real", "sub"))
            os.symlink(os.path.expanduser("~/.ssh"), os.path.join(t, "link"))
            for cmd in ("cd link && ls", "cd link/.. && cat .ssh/config", "cd real/sub/../../link; cat config"):
                self.assertNotEqual(hook.decide({"tool_name": "Bash", "tool_input": {"command": cmd}, "cwd": t})[0], True, cmd)

    def test_failed_cd_keeps_the_original_directory(self):
        home = os.path.expanduser("~")
        for cmd in ("cd /nonexistent/x || cat .claude.json", "cd /nonexistent/x; cat .claude.json"):
            self.assertEqual(hook.decide({"tool_name": "Bash", "tool_input": {"command": cmd}, "cwd": home})[0], "ask", cmd)

    def test_other_tools_untouched(self):
        self.assertIsNone(decide("Edit", file_path="x"))


class Agent(unittest.TestCase):
    def test_dispatch(self):
        self.assertTrue(decide("Agent", subagent_type="general-purpose", prompt="READ-ONLY adversarial review of PR 25 ..."))
        self.assertTrue(decide("Agent", subagent_type="Explore", prompt="find x"))
        self.assertFalse(decide("Agent", subagent_type="Explore", prompt="find x", mode="bypassPermissions"))
        self.assertFalse(decide("Agent", subagent_type="Explore", prompt="find x", isolation="remote"))
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

    def test_allow_or_ask_never_deny(self):
        r, log = self.run_hook({"tool_name": "Bash", "tool_input": {"command": "cat README.md"}, "cwd": CWD})
        self.assertEqual(json.loads(r.stdout)["hookSpecificOutput"]["permissionDecision"], "allow")
        self.assertNotIn("README", log)  # the log keeps a hash, never the command text
        r, _ = self.run_hook({"tool_name": "Bash", "tool_input": {"command": "frobnicate --now"}, "cwd": CWD})
        self.assertEqual((r.returncode, r.stdout), (0, ""))
        r, _ = self.run_hook({"tool_name": "Bash", "tool_input": {"command": "cat ~/.ssh/id_rsa"}, "cwd": CWD})
        self.assertEqual(json.loads(r.stdout)["hookSpecificOutput"]["permissionDecision"], "ask")
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
