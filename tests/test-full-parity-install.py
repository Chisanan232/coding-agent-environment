#!/usr/bin/env python3
"""Exercise complete apply, omissions, native state isolation and callable gate outcomes."""
import hashlib
import json
import os
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
                self.assertEqual((self.home / ('.' + host) / 'skills' / name / 'SKILL.md').read_bytes(),
                                 (self.repo / '.claude/skills' / name / 'SKILL.md').read_bytes())
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
        self.assertEqual(run('claude', 'session-memory.sh', ['append', 'QA-2', 'Decision', 'Claude default override']).returncode, 0)
        self.assertTrue((self.home / 'legacy-notes/QA-2.md').exists())
        result = run('codex', 'workflow-state.sh', ['write', 'QA-1', 'dev-impl-loop', '2', '5', 'in_progress'])
        self.assertEqual(result.returncode, 0, result.stderr)
        state = json.loads(run('codex', 'workflow-state.sh', ['read', 'QA-1']).stdout)
        self.assertEqual(state['step'], '2')
        self.assertFalse((self.home / '.claude/workflow-state/QA-1.json').exists())
        self.assertEqual(run('codex', 'workflow-state.sh', ['archive', 'QA-1']).returncode, 0)
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

if __name__ == '__main__':
    unittest.main()
