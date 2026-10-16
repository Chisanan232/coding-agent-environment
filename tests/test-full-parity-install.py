#!/usr/bin/env python3
"""Exercise complete apply, omissions, native state isolation and callable gate outcomes."""
import hashlib
import json
import os
import re
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parent.parent

class FullParity(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='full parity ')
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name) / 'home'
        self.repo = Path(self.temp.name) / 'repo'
        for name in ['.claude', 'codex', 'scripts']:
            shutil.copytree(ROOT / name, self.repo / name)
        self.env = dict(os.environ, CODING_AGENT_SYNC_HOME=str(self.home))
        manifest_path = self.repo / 'codex/subtraction-skills.json'
        manifest = json.loads(manifest_path.read_text())
        for name, spec in manifest['skills'].items():
            folder = self.home / '.agents/skills' / name
            folder.mkdir(parents=True)
            body = b'Offline external canonical fixture'
            (folder / 'SKILL.md').write_bytes(body)
            spec['files'] = {'SKILL.md': hashlib.sha256(body).hexdigest()}
        manifest_path.write_text(json.dumps(manifest))

    def install(self, host='codex', *flags):
        return subprocess.run(['bash', str(self.repo / 'scripts/profile-install.sh'),
                               '--global' if host == 'codex' else '--claude-skills', *flags],
                              env=self.env, text=True, capture_output=True)

    def test_complete_both_hosts_omissions_and_resource_preflight(self):
        protected = {}
        for host in ['claude', 'codex']:
            for name in ['settings.json', 'config.toml', 'auth.json', 'hooks.json']:
                path = self.home / ('.' + host) / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(b'opaque unrelated state')
                protected[path] = path.read_bytes()
        claude_policy = self.home / '.claude/CLAUDE.md'
        local_policy = b'Unrelated private Claude instructions\r\n'
        claude_policy.write_bytes(local_policy)
        # Claude materialization must not depend on the unrelated Codex instruction override.
        override = self.home / '.codex/AGENTS.override.md'
        override.write_text('Private user override')
        result = self.install('claude')
        self.assertEqual(result.returncode, 0, result.stderr)
        override.unlink()
        result = self.install()
        self.assertEqual(result.returncode, 0, result.stderr)
        manifest = json.loads((self.repo / 'codex/shared-skills.json').read_text())
        for host in ['claude', 'codex']:
            for name in manifest['skills']:
                expected = (self.repo / '.claude/skills' / name / 'SKILL.md').read_bytes()
                if host == 'codex':
                    expected = re.sub(rb'<!-- claude-only:start -->.*?<!-- claude-only:end -->\n?', b'', expected, flags=re.S)
                self.assertEqual((self.home / ('.' + host) / 'skills' / name / 'SKILL.md').read_bytes(), expected)
            target = self.home / ('.' + host) / 'skills/engineering-runtime/scripts/workflow-state.sh'
            self.assertEqual(target.read_bytes(), (self.repo / '.claude/hooks/workflow-state.sh').read_bytes())
            self.assertEqual(self.install(host, '--check').returncode, 0)
        self.assertTrue(claude_policy.read_bytes().startswith(local_policy))
        before_check = {p: p.read_bytes() for p in self.home.rglob('*') if p.is_file()}
        full = subprocess.run(['bash', str(self.repo / 'scripts/sync-check.sh'), '--full-parity'],
                              env=self.env, capture_output=True, text=True)
        self.assertEqual(full.returncode, 0, full.stdout + full.stderr)
        self.assertEqual(before_check, {p: p.read_bytes() for p in self.home.rglob('*') if p.is_file()})
        claude_skill = self.home / '.claude/skills/release-watch/SKILL.md'
        saved = claude_skill.read_bytes()
        claude_skill.unlink()
        full = subprocess.run(['bash', str(self.repo / 'scripts/sync-check.sh'), '--full-parity'],
                              env=self.env, capture_output=True, text=True)
        self.assertEqual(full.returncode, 1, 'full parity check must include Claude managed practices')
        self.assertFalse(claude_skill.exists())
        claude_skill.write_bytes(saved)
        for path, prior in protected.items():
            self.assertEqual(path.read_bytes(), prior)
        missing = self.home / '.codex/skills/python-mypy-debugging/SKILL.md'
        missing.unlink()
        self.assertEqual(self.install('codex', '--check').returncode, 1)
        unsafe = self.home / '.codex/skills/engineering-runtime/scripts/workflow-state.sh'
        unsafe.unlink()
        other = self.home / 'unowned.sh'
        other.write_text('Unrelated private helper')
        unsafe.symlink_to(other)
        agents = self.home / '.codex/AGENTS.md'
        prior = agents.read_bytes()
        self.assertNotEqual(self.install().returncode, 0)
        self.assertFalse(missing.exists(), 'resource preflight cannot partially repair another skill')
        self.assertEqual(agents.read_bytes(), prior)
        self.assertEqual(other.read_text(), 'Unrelated private helper')
        unsafe.unlink()
        self.assertEqual(self.install().returncode, 0)
        (self.repo / '.claude/skills/python-mypy-debugging/SKILL.md').unlink()
        result = self.install('codex', '--check')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('complete authored inventory', result.stderr)

    def test_reference_profile_grants_only_bounded_public_reads(self):
        import tomllib
        config = tomllib.loads((self.repo / 'codex/engineering-reference.config.toml').read_text())
        server = config['mcp_servers']['context7']
        self.assertEqual(server['url'], 'https://mcp.context7.com/mcp')
        self.assertEqual(set(server['enabled_tools']), {'resolve-library-id', 'query-docs'})
        self.assertEqual(server['default_tools_approval_mode'], 'prompt')
        self.assertEqual(set(server['tools']), set(server['enabled_tools']))
        self.assertTrue(all(spec['approval_mode'] == 'approve' for spec in server['tools'].values()))
        self.assertFalse({'headers', 'http_headers', 'bearer_token_env_var'} & set(server))

    def test_native_context_isolation_state_resume_and_gate_outcomes(self):
        for host in ['claude', 'codex']:
            self.assertEqual(self.install(host).returncode, 0)
        sandbox = Path(self.temp.name) / 'disposable project'
        sandbox.mkdir()
        subprocess.run(['git', 'init', '-q', str(sandbox)], check=True)
        for host in ['claude', 'codex']:
            context = sandbox / ('.' + host)
            context.mkdir()
            (context / '.current-ticket').write_text(host.upper() + '-1')
        def run(host, script, args=(), payload=None, extra=None):
            runtime = self.home / ('.' + host) / 'skills/engineering-runtime/scripts'
            env = dict(self.env, CLAUDE_SESSION_NOTES_DIR=str(self.home / 'legacy-notes'),
                       CLAUDE_SKIP_TEST_GATE='1', CLAUDE_STRICT='1')
            env.update(extra or {})
            command = 'eval "$(python3 "$1/bootstrap.py")"; shift; bash "$ENGINEERING_RUNTIME/' + script + '" "$@"'
            return subprocess.run(['bash', '-c', command, 'test', str(runtime), *args],
                                  env=env, cwd=sandbox, input=payload, capture_output=True, text=True)
        result = run('codex', 'session-memory.sh', ['append', 'QA-1', 'Decision', 'Resume this evidence'])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue((self.home / '.codex/session-notes/QA-1.md').exists())
        self.assertFalse((self.home / 'legacy-notes').exists())
        self.assertIn('Resume this evidence', run('codex', 'session-memory.sh', ['read', 'QA-1']).stdout)
        result = run('claude', 'session-memory.sh', ['append', 'QA-2', 'Decision', 'Claude default override'])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue((self.home / 'legacy-notes/QA-2.md').exists())
        # Both installed host runtimes execute guarded archival; private roots stay separate.
        for host, ticket, note_path in (
                ('claude', 'QA-2', self.home / 'legacy-notes/QA-2.md'),
                ('codex', 'QA-1', self.home / '.codex/session-notes/QA-1.md')):
            original = note_path.read_bytes()
            self.assertNotEqual(run(host, 'session-memory.sh', ['clear', ticket]).returncode, 0)
            self.assertEqual(note_path.read_bytes(), original)
            token = run(host, 'session-memory.sh', ['snapshot', ticket]).stdout.strip()
            self.assertEqual(run(host, 'session-memory.sh', ['clear', ticket, token]).returncode, 0)
            self.assertFalse(note_path.exists())
            self.assertEqual(next((note_path.parent / 'archive').glob(ticket + '_*.md')).read_bytes(), original)
        result = run('codex', 'workflow-state.sh', ['write', 'QA-1', 'dev-impl-loop', '2', '5', 'in_progress'])
        self.assertEqual(result.returncode, 0, result.stderr)
        state = json.loads(run('codex', 'workflow-state.sh', ['read', 'QA-1']).stdout)
        self.assertEqual(state['step'], '2')
        self.assertFalse((self.home / '.claude/workflow-state/QA-1.json').exists())
        self.assertNotEqual(run('codex', 'workflow-state.sh', ['archive', 'QA-1']).returncode, 0)
        self.assertEqual(json.loads(run('codex', 'workflow-state.sh', ['read', 'QA-1']).stdout), state)
        token = run('codex', 'workflow-state.sh', ['snapshot', 'QA-1']).stdout.strip()
        self.assertEqual(run('codex', 'workflow-state.sh', ['archive', 'QA-1', token]).returncode, 0)
        self.assertNotEqual(run('codex', 'workflow-state.sh', ['read', 'QA-1']).returncode, 0)
        self.assertEqual(run('codex', 'circuit-breaker-gate.sh', ['record-failure', 'QA-1', '2']).returncode, 0)
        self.assertEqual(run('codex', 'circuit-breaker-gate.sh', ['record-failure', 'QA-1', '2']).returncode, 1)
        self.assertEqual(run('codex', 'circuit-breaker-gate.sh', ['check', 'QA-1', '2']).returncode, 1)
        # An inherited Claude skip flag cannot clear Codex's missing acceptance evidence.
        payload = json.dumps({'tool_input': {'command': 'git push'}})
        self.assertEqual(run('codex', 'full-test-gate.sh', payload=payload).returncode, 2)
        clean = sandbox / 'clean.py'
        clean.write_text('answer = 42\n')
        warning = sandbox / 'warning.py'
        warning.write_text('print("debug")\n')
        quality = lambda file, strict: run('codex', 'quality_gate.sh',
            payload=json.dumps({'tool_input': {'file_path': str(file)}}),
            extra={'ENGINEERING_STRICT': str(strict)})
        cooldown = self.home / '.codex/.quality-gate-last-run'
        self.assertEqual(quality(clean, 1).returncode, 0, 'clean files without TODO must pass pipefail')
        cooldown.unlink()
        self.assertEqual(quality(warning, 0).returncode, 0)
        cooldown.unlink()
        self.assertEqual(quality(warning, 1).returncode, 1)
    def test_codex_tree_excludes_host_specific_files_and_reports_stale_ones(self):
        shutil.copytree(ROOT / 'docs', self.repo / 'docs')
        host_specific = ['engineering-runtime/references/claude-runtime.md',
                         'ci-failure-triage/references/claude-ci-triage.md',
                         'architecture-design/references/model-routing-and-delegation.md',
                         'project-setup/references/project-claude-md.md']
        portable = ['bot-pr-maintainer/references/bot-pr-policy.md',
                    'feature-implementation/references/quality-policies.md',
                    'engineering-runtime/SKILL.md']
        self.assertEqual(self.install('claude').returncode, 0)
        self.assertEqual(self.install().returncode, 0)
        for name in host_specific:
            with self.subTest(host='claude', file=name):
                self.assertTrue((self.home / '.claude/skills' / name).is_file())
            with self.subTest(host='codex', file=name):
                self.assertFalse((self.home / '.codex/skills' / name).exists())
        for name in portable:
            with self.subTest(host='codex', portable=name):
                self.assertTrue((self.home / '.codex/skills' / name).is_file())
        self.assertEqual(self.install('codex', '--check').returncode, 0)
        # An existing install that already holds a stale file is reported, never deleted.
        stale = self.home / '.codex/skills' / host_specific[0]
        stale.write_text('stale')
        check = self.install('codex', '--check')
        self.assertNotEqual(check.returncode, 0)
        self.assertIn('STALE host-specific file', check.stdout)
        self.assertIn(str(stale), check.stdout)
        self.assertEqual(self.install().returncode, 0)
        self.assertEqual(stale.read_text(), 'stale')


    def overlay_path(self):
        return self.home / '.config/coding-agent-environment/local/baseline/claude/CLAUDE.private.md'

    def write_overlay(self, text, mode=0o600):
        path = self.overlay_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        path.chmod(mode)
        return path
    def test_private_overlay_span_is_merged_without_touching_unmarked_content(self):
        policy = self.home / '.claude/CLAUDE.md'
        policy.parent.mkdir(parents=True)
        live_only = 'SYNTHETIC-LIVE-ONLY-CLAUSE keep me\n'
        policy.write_text(live_only)
        self.write_overlay('## Synthetic Private Clause\nSYNTHETIC-PRIVATE-BODY\n')
        dry = self.install('claude', '--dry-run')
        self.assertEqual(dry.returncode, 0, dry.stderr)
        self.assertNotIn('SYNTHETIC-PRIVATE-BODY', dry.stdout + dry.stderr)
        self.assertEqual(policy.read_text(), live_only)
        self.assertEqual(self.install('claude').returncode, 0)
        merged = policy.read_text()
        self.assertTrue(merged.startswith(live_only))
        self.assertEqual(merged.count('SYNTHETIC-PRIVATE-BODY'), 1)
        self.assertEqual(self.install('claude', '--check').returncode, 0)
        # Editing the overlay replaces only its span; repeated installs do not duplicate it.
        self.write_overlay('## Synthetic Private Clause\nSYNTHETIC-PRIVATE-BODY-2\n')
        self.assertEqual(self.install('claude').returncode, 0)
        updated = policy.read_text()
        self.assertEqual(updated.count('private-overlay:start'), 1)
        self.assertIn('SYNTHETIC-PRIVATE-BODY-2', updated)
        self.assertNotIn('SYNTHETIC-PRIVATE-BODY\n', updated)
        self.assertTrue(updated.startswith(live_only))

    def test_private_overlay_must_be_private_and_not_a_symlink(self):
        path = self.write_overlay('SYNTHETIC', mode=0o644)
        result = self.install('claude', '--check')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('mode 0600', result.stderr)
        path.unlink()
        target = Path(self.temp.name) / 'elsewhere.md'
        target.write_text('SYNTHETIC')
        target.chmod(0o600)
        path.symlink_to(target)
        result = self.install('claude', '--check')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('symlinked private overlay', result.stderr)

    def test_overlay_containing_a_workflow_marker_is_refused_without_writing(self):
        policy = self.home / '.claude/CLAUDE.md'
        policy.parent.mkdir(parents=True)
        policy.write_text('SYNTHETIC-LIVE-ONLY-CLAUSE\n')
        self.write_overlay('x\n<!-- coding-agent-environment:engineering-workflow:start -->\n')
        result = self.install('claude')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('must not contain managed markers', result.stderr)
        self.assertEqual(policy.read_text(), 'SYNTHETIC-LIVE-ONLY-CLAUSE\n')

    def test_oversized_overlay_is_refused_without_writing(self):
        policy = self.home / '.claude/CLAUDE.md'
        policy.parent.mkdir(parents=True)
        policy.write_text('SYNTHETIC-LIVE-ONLY-CLAUSE\n')
        self.write_overlay('x' * (262144 + 1))
        result = self.install('claude')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('256 KiB', result.stderr)
        self.assertEqual(policy.read_text(), 'SYNTHETIC-LIVE-ONLY-CLAUSE\n')

    def test_overlay_files_are_gitignored_and_never_tracked(self):
        tracked = subprocess.check_output(['git', 'ls-files'], cwd=ROOT, text=True).splitlines()
        self.assertFalse([name for name in tracked if name.endswith('CLAUDE.private.md')])
        ignored = subprocess.run(['git', 'check-ignore', '-q', 'any/dir/claude/CLAUDE.private.md'], cwd=ROOT)
        self.assertEqual(ignored.returncode, 0)

    def dangling_codex_links(self):
        found = []
        for doc in sorted((self.home / '.codex/skills').rglob('*.md')):
            for link in re.findall(r'\]\((?!https?:|mailto:|#)([^)#\s]+)', doc.read_text()):
                if not (doc.parent / link).exists():
                    found.append((str(doc.relative_to(self.home)), link))
        return found

    def test_codex_install_has_no_dangling_relative_links_and_the_scan_detects_one(self):
        shutil.copytree(ROOT / 'docs', self.repo / 'docs')
        self.assertEqual(self.install().returncode, 0)
        self.assertEqual(self.dangling_codex_links(), [])
        # Negative control: a hard link to a host-specific file outside any claude-only span.
        skill = self.repo / '.claude/skills/ci-failure-triage/SKILL.md'
        skill.write_text(skill.read_text() + '\nSee [Claude only](references/claude-ci-triage.md).\n')
        self.assertEqual(self.install().returncode, 0)
        self.assertEqual(self.dangling_codex_links(),
                         [('.codex/skills/ci-failure-triage/SKILL.md', 'references/claude-ci-triage.md')])

if __name__ == '__main__':
    unittest.main()
