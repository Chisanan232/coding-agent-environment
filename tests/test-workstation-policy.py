#!/usr/bin/env python3
"""Workstation profile policy tests (SPE-116): synthetic profiles, injected fingerprints, no host access."""
import importlib.util
import json
import os
import pathlib
import re
import sys
import tempfile
import unittest

sys.dont_write_bytecode = True
ROOT = pathlib.Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("wsp", ROOT / "scripts/workstation-policy.py")
wsp = importlib.util.module_from_spec(spec)
spec.loader.exec_module(wsp)

FP = "f" * 64
PERSONAL = {"schema": 1, "kind": "personal", "host_fingerprint_sha256": FP, "approved_destinations": ["github.com", "*.pkg.example"],
            "auth_hosts": ["github.com"]}
CORP = {"schema": 1, "kind": "corporate", "host_fingerprint_sha256": FP,
        "approved_destinations": ["git.corp.example", "*.registry.corp.example", "llm.corp.example"], "auth_hosts": ["idp.corp.example"]}


def out(dest, classes, **kw):
    return {"kind": "outbound", "destination": dest, "data_classes": classes, **kw}


class Personal(unittest.TestCase):
    def test_routine_source_to_private_approved_host_is_allowed(self):
        self.assertEqual(wsp.decide(PERSONAL, out("github.com", ["source_code"], visibility="private"), FP)["verdict"], "ALLOW")

    def test_sensitive_outbound_asks_with_a_complete_disclosure_packet(self):
        d = wsp.decide(PERSONAL, out("paste.example.net", ["logs", "screenshots"], data_source="local build logs", purpose="debug help"), FP)
        self.assertEqual(d["verdict"], "ASK")
        pk = d["ask_packet"]
        self.assertEqual((pk["source"], pk["destination"], pk["purpose"]), ("local build logs", "paste.example.net", "debug help"))
        self.assertTrue(pk["safer_alternatives"] and pk["risks"])

    def test_sensitive_to_an_approved_host_still_asks_each_time(self):
        d = wsp.decide(PERSONAL, out("github.com", ["documents"], visibility="private", data_source="notes", purpose="issue"), FP)
        self.assertEqual(d["verdict"], "ASK")

    def test_public_or_unknown_visibility_is_not_routine(self):
        d = wsp.decide(PERSONAL, out("github.com", ["source_code"], visibility="public", data_source="repo", purpose="push"), FP)
        self.assertEqual(d["verdict"], "ASK")

    def test_ask_without_source_or_purpose_cannot_be_asked_and_fails_closed(self):
        d = wsp.decide(PERSONAL, out("paste.example.net", ["logs"]), FP)
        self.assertEqual(d["verdict"], "DENY")
        self.assertIn("ASK_PACKET_INCOMPLETE", d["reasons"][0])

    def test_authentication_to_an_approved_host_stays_usable(self):
        self.assertEqual(wsp.decide(PERSONAL, out("github.com", ["auth_handshake"]), FP)["verdict"], "ALLOW")
        self.assertEqual(wsp.decide(PERSONAL, out("evil.example", ["auth_handshake"]), FP)["verdict"], "ASK")

    def test_raw_credentials_are_denied_everywhere(self):
        for prof in (PERSONAL, CORP):
            self.assertEqual(wsp.decide(prof, out("github.com", ["credentials"]), FP)["verdict"], "DENY")
            self.assertEqual(wsp.decide(prof, out("git.corp.example", ["source_code", "credentials"]), FP)["verdict"], "DENY")


class Corporate(unittest.TestCase):
    def test_prohibited_destinations_are_denied_for_every_protected_class(self):
        for cls in sorted(wsp.PROTECTED_DATA):
            d = wsp.decide(CORP, out("api.thirdparty.example", [cls], data_source="x", purpose="y"), FP)
            self.assertEqual(d["verdict"], "DENY", cls)
            self.assertIn("CORPORATE_OUTBOUND_PROHIBITED", d["reasons"][0])

    def test_denied_even_when_the_owner_style_ask_fields_are_supplied(self):
        d = wsp.decide(CORP, out("github.com", ["source_code"], visibility="private", data_source="repo", purpose="mirror"), FP)
        self.assertEqual(d["verdict"], "DENY")
        self.assertNotIn("ask_packet", d)

    def test_approved_internal_destinations_remain_usable(self):
        for host in ("git.corp.example", "llm.corp.example", "pkg.registry.corp.example"):
            self.assertEqual(wsp.decide(CORP, out(host, ["source_code", "documents"]), FP)["verdict"], "ALLOW", host)
        self.assertEqual(wsp.decide(CORP, out("idp.corp.example", ["auth_handshake"]), FP)["verdict"], "ALLOW")

    def test_lookalike_and_malformed_hosts_do_not_match(self):
        for host in ("git.corp.example.evil.example", "evilgit.corp.example", "registry.corp.example", "git.corp.example@evil.example",
                     "git.corp.example:443", "GIT.CORP.EXAMPLE/../x", "", "evil.example#git.corp.example"):
            d = wsp.decide(CORP, out(host, ["source_code"]), FP)
            self.assertEqual(d["verdict"], "DENY", host)

    def test_unknown_or_missing_data_class_fails_closed(self):
        for classes in (None, [], ["mystery"], "logs"):
            self.assertEqual(wsp.decide(CORP, {"kind": "outbound", "destination": "git.corp.example", "data_classes": classes}, FP)["verdict"], "DENY")

    def test_public_data_is_allowed(self):
        self.assertEqual(wsp.decide(CORP, out("docs.thirdparty.example", ["public"]), FP)["verdict"], "ALLOW")


class Ambiguity(unittest.TestCase):
    def check_denied(self, profile, fp=FP):
        d = wsp.decide(profile, out("git.corp.example", ["source_code"]), fp)
        self.assertEqual((d["profile_state"], d["verdict"]), ("AMBIGUOUS", "DENY"), d)

    def test_mismatched_fingerprint_fails_closed(self):
        self.check_denied(CORP, "0" * 64)
        self.check_denied(CORP, None)

    def test_placeholder_fingerprint_template_is_unverified(self):
        for kind in ("personal", "corporate"):
            tpl = json.load(open(ROOT / f"docs/examples/workstation-profile-{kind}.example.json"))
            self.check_denied(tpl)

    def test_unknown_kind_or_schema_fails_closed(self):
        self.check_denied({**CORP, "kind": "work"})
        self.check_denied({**CORP, "schema": 2})
        self.check_denied({})
        self.check_denied(None)

    def test_username_is_never_trusted(self):
        self.check_denied({**CORP, "user": "alice-example", "host_fingerprint_sha256": "<x>"})

    def test_ambiguous_profile_may_only_ask_about_public_data(self):
        d = wsp.decide({**PERSONAL, "kind": "nope"}, out("x.example", ["public"]), FP)
        self.assertEqual(d["verdict"], "ASK")


class PolicyModification(unittest.TestCase):
    def test_protected_locations_and_keys_need_owner_approval(self):
        home = os.path.expanduser("~")
        paths = ["~/.claude/settings.json", f"{home}/.claude/hooks/gate.sh", "/Library/Application Support/ClaudeCode/managed-settings.json",
                 "/x/managed-settings.json", "repo/.claude/settings.json", "~/.claude/../.claude/settings.json", ""]
        for path in paths:
            for prof in (PERSONAL, CORP):
                d = wsp.decide(prof, {"kind": "modify_policy", "path": path}, FP)
                self.assertEqual((d["verdict"], d.get("owner_approval_required")), ("ASK", True), path)
        for key in ("autoMode.allow", "permissions", "hooks.PreToolUse", "sandbox.enabled"):
            self.assertEqual(wsp.decide(PERSONAL, {"kind": "modify_policy", "key": key}, FP)["verdict"], "ASK", key)
        self.assertEqual(wsp.decide(PERSONAL, {"kind": "modify_policy"}, FP)["verdict"], "ASK")

    def test_ordinary_files_are_not_policy(self):
        self.assertEqual(wsp.decide(PERSONAL, {"kind": "modify_policy", "path": "/work/app/src/main.py"}, FP)["verdict"], "ALLOW")

    def test_ambiguous_profile_does_not_lower_policy_protection(self):
        self.assertEqual(wsp.decide({}, {"kind": "modify_policy", "path": "~/.claude/settings.json"}, None)["verdict"], "ASK")


class FailClosedKinds(unittest.TestCase):
    def test_unknown_or_malformed_action_kind_is_denied(self):
        for kind in ("upload", "", None, 5, ["outbound"]):
            d = wsp.decide(CORP, {"kind": kind, "data_classes": ["credentials"], "destination": "evil.example"}, FP)
            self.assertEqual(d["verdict"], "DENY", kind)
        for act in (None, [], "outbound"):
            self.assertEqual(wsp.decide(CORP, act, FP)["verdict"], "DENY")

    def test_kind_is_case_and_space_normalized_not_skipped(self):
        d = wsp.decide(CORP, {"kind": " Outbound ", "data_classes": ["credentials"], "destination": "x.example"}, FP)
        self.assertIn("RAW_CREDENTIAL_OUTBOUND", d["reasons"][0])
        self.assertEqual(wsp.decide(CORP, {"kind": "local"}, FP)["verdict"], "ALLOW")


class PathProtection(unittest.TestCase):
    def test_relative_case_variant_and_symlinked_paths_are_protected(self):
        with tempfile.TemporaryDirectory() as d:
            link = os.path.join(os.path.realpath(d), "innocent.json")
            os.symlink(os.path.expanduser("~/.claude/settings.json"), link)
            paths = [".claude/settings.json", "./.claude/hooks/x.sh", "a/b/.claude/settings.local.json", ".claude/CLAUDE.md",
                     "~/.CLAUDE/settings.json", "~/.claude/Settings.json", "/library/application support/claudecode/x",
                     "~/.claude/bin/sync-neon-secret", "/p/.claude/agents/a.md", "/p/.claude/skills/s/SKILL.md", "/p/.claude/commands/c.md", "/p/.claude/rules/r.md", "~/.claude/rules/secret-handling.md", "~/.claude/skills/x/SKILL.md",
                     "/private/etc/claude-code/x", "/etc/claude-code/x", ".mcp.json", "sub/.mcp.json", "MANAGED-SETTINGS.JSON", link]
            for path in paths:
                self.assertTrue(wsp.is_protected_path(path), path)

    def test_ordinary_paths_are_not_protected(self):
        for path in ("src/main.py", "/work/app/.claudeignore", "notes/claude.md", "docs/policy.md", "x/.github/settings.json"):
            self.assertFalse(wsp.is_protected_path(path), path)

    def test_every_settings_key_except_a_benign_few_needs_approval(self):
        for key in ("apiKeyHelper", "statusLine.command", "mcpServers.x", "enabledPlugins", "defaultMode", "awsAuthRefresh", "AUTOMODE.allow"):
            self.assertEqual(wsp.decide(PERSONAL, {"kind": "modify_policy", "key": key}, FP)["verdict"], "ASK", key)
        self.assertEqual(wsp.decide(PERSONAL, {"kind": "modify_policy", "key": "model"}, FP)["verdict"], "ALLOW")


class Candidates(unittest.TestCase):
    def test_rendered_candidates_are_clean_and_match_the_committed_examples(self):
        for kind in ("personal", "corporate"):
            cand = wsp.candidate(kind)
            self.assertEqual(wsp.lint_settings(cand), [])
            self.assertEqual(cand, json.load(open(ROOT / f"docs/examples/candidate-settings-{kind}.example.json")))
            am = cand["autoMode"]
            self.assertTrue(all(am[k][0] == "$defaults" for k in am))
            self.assertEqual(am["allow"], ["$defaults"])

    def test_lint_catches_each_forbidden_shape(self):
        base = wsp.candidate("personal")
        def mut(f):
            c = json.loads(json.dumps(base)); f(c); return wsp.lint_settings(c)
        self.assertTrue(mut(lambda c: c["autoMode"].update(allow=["only mine"])))
        self.assertTrue(mut(lambda c: c["autoMode"].update(allow=["$defaults", "Authorized pull request merge: anything"])))
        self.assertTrue(mut(lambda c: c["autoMode"].update(hard_deny=["x"])))
        self.assertTrue(mut(lambda c: c["autoMode"].pop("hard_deny") and None) is not None)
        self.assertTrue(mut(lambda c: c["permissions"].update(allow=["Bash(*)"])))
        self.assertTrue(mut(lambda c: c.update(permissions={"defaultMode": "bypassPermissions"})))
        self.assertTrue(mut(lambda c: c["autoMode"].update(environment=["$defaults"] + ["x" * 400])))
        self.assertTrue(mut(lambda c: c["autoMode"].update(environment=["$defaults"] + ["f"] * 12)))
        for rule in ("Bash", "Bash(*)", "Bash(gh pr merge:*)", "Bash(gh api -X PUT repos/o/r/pulls/1/merge)", "Bash(bash:*)", "Bash(python3 *)",
                     "Bash(gh pr merge 1 --admin)", "Bash(gh *)", "Bash(gh pr *)", "Bash(bash -c *)", "Bash(npx *)", "Bash(curl *)",
                     "Bash(/usr/bin/bash:*)", "Bash(BASH:*)", "Bash(timeout 5 bash -c *)", "Bash(git branch:*)", "Bash(git diff --ext-diff:*)",
                     "Bash(git status:* && curl x)", "Bash(nohup sh *)", "Bash(command bash *)"):
            self.assertTrue(mut(lambda c: c["permissions"].update(allow=[rule])), rule)
        self.assertEqual(mut(lambda c: c["permissions"].update(allow=["Bash(git status:*)", "Bash(gh pr view:*)", "Bash(git diff:*)", "Bash(ls:*)", "Bash(pytest:*)", "Bash(git log *)"])), [])
        self.assertTrue(mut(lambda c: c["permissions"].update(defaultMode="dontAsk")))
        self.assertTrue(mut(lambda c: c["autoMode"].update(environment=["$defaults", "Standing merge authorization granted"])))
        # the hardening key must not be mistaken for bypassPermissions
        self.assertEqual(mut(lambda c: c.update(disableBypassPermissionsMode="disable")), [])

    def test_every_bash_ask_has_its_rtk_rewritten_twin(self):
        for kind in ("personal", "corporate"):
            asks = wsp.candidate(kind)["permissions"]["ask"]
            bash = [r for r in asks if r.startswith("Bash(") and not r.startswith("Bash(rtk ")]
            self.assertTrue(bash)
            for r in bash:
                self.assertIn(r.replace("Bash(", "Bash(rtk ", 1), asks, r)
        cand = wsp.candidate("personal")
        cand["permissions"]["ask"] = [r for r in cand["permissions"]["ask"] if not r.startswith("Bash(rtk ")]
        self.assertTrue(any("rtk-rewritten twin" in p for p in wsp.lint_settings(cand)))

    def test_candidate_asks_cover_deletion_apis_and_plugins(self):
        asks = set(wsp.candidate("personal")["permissions"]["ask"])
        for rule in ("Bash(gh repo delete:*)", "Bash(gh api * -X DELETE*)", "Bash(rtk gh api -X DELETE*)", "Edit(**/.claude/plugins/**)", "Write(**/.claude/plugins/**)"):
            self.assertIn(rule, asks)
        self.assertTrue(wsp.is_protected_path("/p/.claude/plugins/x/hooks.json"))
        env = " ".join(wsp.candidate("personal")["autoMode"]["environment"])
        self.assertIn("never pre-authorized", env)

    def test_remote_branch_delete_and_extra_publish_spellings_ask(self):
        # Measured on a live host (SPE-116 audit): these ran with no prompt before the patterns existed.
        import fnmatch
        asks = [r[len("Bash("):-1] for r in wsp.candidate("personal")["permissions"]["ask"] if r.startswith("Bash(") and not r.startswith("Bash(rtk ")]

        def hit(cmd):
            return any(fnmatch.fnmatchcase(cmd, a.replace(":*", " *") if a.endswith(":*") else a) or (a.endswith(":*") and cmd == a[:-2]) for a in asks)
        for cmd in ("git push --delete origin x", "git push -d origin x", "git push origin -d x", "git push origin :x", "git push :x",
                    "git push origin :refs/heads/x", "yarn publish --dry-run", "python3 -m twine upload dist/*", "python -m twine upload dist/*"):
            self.assertTrue(hit(cmd), cmd)
        # routine pushes and local branch deletion must stay unprompted
        for cmd in ("git push origin feature/x", "git push -u origin feature/x", "git push origin HEAD:refs/heads/feature/x",
                    "git push origin main", "git push origin main:main", "git push origin src:dst", "git push --follow-tags origin main",
                    "git branch -d old", "git status"):
            self.assertFalse(hit(cmd), cmd)

    def test_gh_api_delete_asks_match_attached_equals_and_case_variants(self):
        import fnmatch
        asks = [r[len("Bash("):-1] for r in wsp.candidate("personal")["permissions"]["ask"] if r.startswith("Bash(gh api")]
        for cmd in ("gh api -X DELETE repos/o/r/git/refs/heads/x", "gh api -XDELETE repos/o/r", "gh api --method=DELETE repos/o/r",
                    "gh api repos/o/r -X=DELETE", "gh api repos/o/r --method delete", "gh api repos/o/r -X Delete"):
            self.assertTrue(any(fnmatch.fnmatchcase(cmd, a.replace(":*", " *") if a.endswith(":*") else a) for a in asks), cmd)

    def test_unapproved_auth_host_on_personal_asks_with_a_packet(self):
        res = wsp.decide(PERSONAL, out("evil.example", ["auth_handshake"], data_source="gh login", purpose="sign in"), FP)
        self.assertEqual(res["verdict"], "ASK")
        self.assertTrue(res["ask_packet"]["safer_alternatives"] and res["ask_packet"]["risks"])

    def test_candidate_asks_protect_security_paths_in_home_and_projects(self):
        asks = set(wsp.candidate("personal")["permissions"]["ask"])
        for tool in ("Edit", "Write"):
            for path in ("~/.claude/settings.json", "~/.claude/hooks/**", "~/.claude/bin/**", "**/.claude/settings.json", "**/.mcp.json"):
                self.assertIn(f"{tool}({path})", asks)

    def test_templates_hold_placeholders_only(self):
        for kind in ("personal", "corporate"):
            raw = open(ROOT / f"docs/examples/workstation-profile-{kind}.example.json").read()
            tpl = json.loads(raw)
            self.assertTrue(wsp.PLACEHOLDER.fullmatch(tpl["host_fingerprint_sha256"]))
            self.assertFalse(re.search(r"[0-9a-f]{64}", raw))
            for host in tpl["approved_destinations"] + tpl["auth_hosts"]:
                self.assertTrue(wsp.PLACEHOLDER.search(host) or host.endswith("github.com"), host)

    def test_render_refuses_protected_destinations(self):
        for bad in ("~/.claude/settings.json", "~/.claude/anything.json", "/Library/Application Support/ClaudeCode/managed-settings.json",
                    "/tmp/x/.claude/candidate.json", "/tmp/settings.json", "~/.CLAUDE/CLAUDE.md", "/tmp/notjson.txt"):
            self.assertEqual(wsp.main(["render-settings", "--kind", "personal", "--out", bad]), 3, bad)
        with tempfile.TemporaryDirectory() as d:
            target = os.path.join(d, "cand.json")
            self.assertEqual(wsp.main(["render-settings", "--kind", "corporate", "--out", target]), 0)
            self.assertEqual(wsp.lint_settings(json.load(open(target))), [])
            self.assertEqual(wsp.main(["render-settings", "--kind", "corporate", "--out", target]), 3)  # never overwrites

    def test_fingerprint_override_is_a_test_hook_only(self):
        with tempfile.TemporaryDirectory() as d:
            prof = os.path.join(d, "p.json")
            json.dump(CORP, open(prof, "w"))
            args = ["evaluate", "--profile", prof, "--action", json.dumps(out("git.corp.example", ["source_code"])), "--fingerprint", FP]
            os.environ.pop("WORKSTATION_POLICY_TEST_FINGERPRINT", None)
            self.assertEqual(wsp.main(args), 3)

    def test_cli_exit_codes(self):
        os.environ["WORKSTATION_POLICY_TEST_FINGERPRINT"] = "1"
        self.addCleanup(os.environ.pop, "WORKSTATION_POLICY_TEST_FINGERPRINT", None)
        with tempfile.TemporaryDirectory() as d:
            prof = os.path.join(d, "p.json")
            json.dump(CORP, open(prof, "w"))
            run = lambda act: wsp.main(["evaluate", "--profile", prof, "--action", json.dumps(act), "--fingerprint", FP])
            self.assertEqual(run(out("git.corp.example", ["source_code"])), 0)
            self.assertEqual(run({"kind": "modify_policy", "key": "autoMode"}), 1)
            self.assertEqual(run(out("evil.example", ["source_code"])), 2)
            self.assertEqual(wsp.main(["evaluate", "--profile", os.path.join(d, "missing"), "--action", "{}", "--fingerprint", FP]), 3)


if __name__ == "__main__":
    unittest.main()
