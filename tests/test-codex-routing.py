"""Black-box coverage for the owned Codex routing reconciler."""
import importlib.util
import contextlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / 'scripts' / 'codex-routing.py'
ROLES = ('architect', 'implementer', 'reviewer')


class RoutingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=REPO)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.home = self.root / 'home'
        self.home.mkdir()
        self.bin = self.root / 'codex-fake'
        self.bin.write_text('''#!/usr/bin/env python3
import json, sys
if sys.argv[1:3] == ["debug", "models"]:
 print(json.dumps({"models": [
  {"slug":"gpt-6-astra", "supported_reasoning_levels":[{"effort":e} for e in ("low","medium","high","xhigh","max")]},
  {"slug":"gpt-6-luna", "supported_reasoning_levels":[{"effort":e} for e in ("low","medium","high","xhigh","max")]}
 ]}))
elif sys.argv[1:2] == ["app-server"]:
 sys.stdin.buffer.read()
 sys.exit(0)
else: sys.exit(3)
''')
        self.bin.chmod(0o755)
        self.env = dict(os.environ, CODING_AGENT_SYNC_HOME=str(self.home),
                        CODING_AGENT_CODEX_BIN=str(self.bin))
        self.env.pop('CODEX_HOME', None)
        self.config = self.home / '.codex' / 'config.toml'
        self.state = self.home / '.codex'

    def run_cli(self, *args, ok=True, cwd=None):
        result = subprocess.run([sys.executable, str(SCRIPT), *args], env=self.env,
                                cwd=cwd, text=True, capture_output=True)
        if ok:
            self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        else:
            self.assertNotEqual(result.returncode, 0, result.stdout)
        return result

    def write_config(self, text):
        self.config.parent.mkdir(parents=True, exist_ok=True)
        self.config.write_bytes(text if isinstance(text, bytes) else text.encode())

    def install(self, *args):
        return self.run_cli('--apply', *args)

    def role_bytes(self):
        return {n: (REPO / 'codex' / 'agents' / (n + '.toml')).read_bytes() for n in ROLES}

    def test_apply_check_idempotent_remove_preserves_unrelated_bytes(self):
        original = (b'# user spacing and comments\r\nmodel = "gpt-6-astra"\r\nmodel_provider = "openai"\r\n'
          b'model_reasoning_effort = "high"\r\nreview_model = "local-review"\r\n'
          b'default_subagent_model = "gpt-6-luna"\r\nmax_subagent_concurrency = 3\r\n'
          b'\n[features]\nmulti_agent = true\n\n[auth]\nmode = "chatgpt"\n\n[notice]\nvalue = "keep"\n')
        self.write_config(original)
        claude = self.home / '.claude' / 'settings.json'
        claude.parent.mkdir(parents=True)
        claude.write_bytes(b'{  "private": true }\n')
        claude_private = self.home / '.claude' / 'private.fixture'
        claude_private.write_bytes(b'private fixture\0bytes')
        self.install()
        installed = self.config.read_bytes()
        for line in (b'model = "gpt-6-astra"', b'model_provider = "openai"',
                     b'model_reasoning_effort = "high"', b'review_model = "local-review"',
                     b'default_subagent_model = "gpt-6-luna"', b'max_subagent_concurrency = 3',
                     b'[auth]', b'multi_agent = true', b'value = "keep"'):
            self.assertIn(line, installed)
        self.assertEqual(claude.read_bytes(), b'{  "private": true }\n')
        self.assertEqual(claude_private.read_bytes(), b'private fixture\0bytes')
        roles = {n: (self.state / 'agents' / (n + '.toml')).read_bytes() for n in ROLES}
        self.run_cli('--check')
        self.install()
        self.assertEqual(self.config.read_bytes(), installed)
        self.assertEqual({n: (self.state / 'agents' / (n + '.toml')).read_bytes() for n in ROLES}, roles)
        self.run_cli('--remove')
        self.assertEqual(self.config.read_bytes(), original)
        self.assertFalse((self.state / 'routing-owned-state.json').exists())

    def test_unmarked_exact_roles_and_preexisting_owned_fields_require_adoption(self):
        self.write_config('[agents]\nenabled = true\nmax_concurrent_threads_per_session = 8\n')
        (self.state / 'agents').mkdir()
        for n, body in self.role_bytes().items():
            (self.state / 'agents' / (n + '.toml')).write_bytes(body.split(b'\n', 1)[1])
        before = self.config.read_bytes()
        self.run_cli('--apply', ok=False)
        self.assertEqual(self.config.read_bytes(), before)
        self.assertFalse((self.state / 'routing-owned-state.json').exists())
        self.install('--adopt-existing')
        self.run_cli('--check')

    def test_unequal_or_unknown_role_refuses_without_mutation(self):
        self.write_config('model = "keep"\n')
        (self.state / 'agents').mkdir()
        for n, body in self.role_bytes().items():
            (self.state / 'agents' / (n + '.toml')).write_bytes(body.split(b'\n', 1)[1] + b'\n# user')
        before = self.config.read_bytes()
        self.run_cli('--apply', '--adopt-existing', ok=False)
        self.assertEqual(self.config.read_bytes(), before)
        self.assertFalse((self.state / 'routing-owned-state.json').exists())

    def test_launch_provider_change_refuses_then_resolution_allows_mapped_roles(self):
        self.write_config('model_provider = "openai"\n')
        self.install()
        self.config.write_text(self.config.read_text().replace('model_provider = "openai"', 'model_provider = "custom"') +
          '\n[model_providers.custom]\nbase_url = "http://localhost"\n')
        changed = self.config.read_bytes()
        self.run_cli('--launch-check', ok=False)
        self.assertEqual(self.config.read_bytes(), changed)
        # Resolution is bound to the canonical fingerprint of this exact local provider context.
        spec = importlib.util.spec_from_file_location('codex_routing', SCRIPT)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        parsed = __import__('tomllib').loads(changed.decode())
        fingerprint, _ = module.provider(parsed)
        resolution = self.root / 'resolution.json'
        resolution.write_text(json.dumps({'provider_fingerprint': fingerprint,
          'verification_evidence': 'synthetic fake Codex catalog + strict schema probe',
          'models': {n: ('gpt-6-astra' if n != 'implementer' else 'gpt-6-luna') for n in ROLES}}))
        self.run_cli('--apply', '--resolution', str(resolution))
        self.run_cli('--check')

    def test_launch_overrides_that_can_change_routing_refuse_without_writes(self):
        self.write_config('model_provider = "openai"\n')
        self.install()
        before = self.config.read_bytes()
        project = self.root / 'project'
        (project / '.codex').mkdir(parents=True)
        (project / '.codex' / 'config.toml').write_text('[agents.custom]\nmodel = "gpt-6-luna"\n')
        native_cases = (
            ('--oss',), ('--local-provider',), ('--remote',),
            ('--enable=multi_agent_v2',), ('-c', 'agents.enabled=false'),
            ('-c', '"agents".enabled=false'), ('-c', 'profile=custom'),
            ('-c', 'profiles.custom.model_provider="custom"'),
            ('--profile=custom',), ('--ignore-user-config',),
            ('-C', str(project)), ('--cd', str(project)),
        )
        self.run_cli('--launch-check', cwd=self.root)
        for case in native_cases:
            with self.subTest(case=case):
                result = self.run_cli('--launch-check', '--', *case, ok=False, cwd=self.root)
                self.assertNotIn('Project routing override', result.stderr)
                self.assertEqual(self.config.read_bytes(), before)

        self.run_cli('--launch-check', ok=False, cwd=project)

    def test_interrupted_provider_remap_keeps_old_roles_until_recovery(self):
        self.write_config('model_provider = "openai"\n')
        self.install()
        old_roles = {n: (self.state / 'agents' / (n + '.toml')).read_bytes() for n in ROLES}
        self.config.write_text(self.config.read_text().replace('model_provider = \"openai\"', 'model_provider = \"custom\"') + '\n[model_providers.custom]\nbase_url = \"http://localhost\"\n')
        spec = importlib.util.spec_from_file_location('codex_routing_remap', SCRIPT)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        import tomllib
        fingerprint, _ = module.provider(tomllib.loads(self.config.read_text()))
        resolution = self.root / 'remap-resolution.json'
        resolution.write_text(json.dumps({'provider_fingerprint': fingerprint,
          'verification_evidence': 'synthetic verified model mapping',
          'models': {n: 'gpt-6-luna' for n in ROLES}}))
        real_atomic = module.atomic
        def fail_after_prepared(path, expected, desired):
            if path == self.state / 'routing-owned-state.json' and json.loads(desired)['phase'] == 'prepared':
                real_atomic(path, expected, desired)
                raise ValueError('injected stop after prepared journal')
            return real_atomic(path, expected, desired)
        with mock.patch.dict(os.environ, self.env, clear=False), \
             mock.patch.object(module, 'atomic', side_effect=fail_after_prepared), \
             mock.patch.object(sys, 'argv', [str(SCRIPT), '--apply', '--resolution', str(resolution)]), \
             contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            os.environ.pop('CODEX_HOME', None)
            with self.assertRaises(ValueError):
                module.main()
        self.assertEqual({n: (self.state / 'agents' / (n + '.toml')).read_bytes() for n in ROLES}, old_roles)
        self.assertEqual(json.loads((self.state / 'routing-owned-state.json').read_text())['phase'], 'prepared')
        self.install('--resolution', str(resolution))
        for n in ROLES:
            self.assertIn(b'model = "gpt-6-luna"', (self.state / 'agents' / (n + '.toml')).read_bytes())

    def test_provider_switch_preserves_custom_agent_routing_and_defaults(self):
        import tomllib
        fields = []
        for role, body in self.role_bytes().items():
            value = tomllib.loads(body.decode())
            fields.append(f"[agents.{role}]\ndescription = {json.dumps(value['description'])}\nconfig_file = \"agents/{role}.toml\"\n")
        self.write_config(('''model = "gpt-6-astra"
model_provider = "openai"
model_reasoning_effort = "high"
default_subagent_model = "gpt-6-luna"
default_subagent_reasoning_effort = "medium"
[agents]
enabled = true
max_concurrent_threads_per_session = 8
default_subagent_model = "gpt-6-luna"
default_subagent_reasoning_effort = "medium"
''') + ''.join(fields) + '''
[agents.custom]
name = "custom"
model = "gpt-6-astra"
model_reasoning_effort = "high"
config_file = "agents/custom.toml"
''')
        self.install('--adopt-existing')
        config = self.config.read_text()
        # Model/provider switchers own these provider defaults; named routing stays user-owned.
        switched = config.replace('model = "gpt-6-astra"', 'model = "gpt-6-luna"', 1)
        switched = switched.replace('model_provider = "openai"', 'model_provider = "custom"', 1)
        switched = switched.replace('model_reasoning_effort = "high"', 'model_reasoning_effort = "medium"', 1)
        switched = switched.replace('default_subagent_model = "gpt-6-luna"', 'default_subagent_model = "gpt-6-astra"')
        switched += '\n[model_providers.custom]\nbase_url = "http://localhost"\n'
        self.config.write_text(switched)
        data = __import__('tomllib').loads(switched)
        spec = importlib.util.spec_from_file_location('codex_routing', SCRIPT)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        fingerprint, _ = module.provider(data)
        resolution = self.root / 'resolution.json'
        resolution.write_text(json.dumps({'provider_fingerprint': fingerprint,
          'verification_evidence': 'test catalog and strict schema probe',
          'models': {n: ('gpt-6-astra' if n != 'implementer' else 'gpt-6-luna') for n in ROLES}}))
        self.install('--resolution', str(resolution))
        final = __import__('tomllib').loads(self.config.read_text())
        self.assertEqual(final['agents']['max_concurrent_threads_per_session'], 8)
        self.assertEqual(final['agents']['custom'], {'name': 'custom', 'model': 'gpt-6-astra',
          'model_reasoning_effort': 'high', 'config_file': 'agents/custom.toml'})
        self.assertEqual(final['default_subagent_model'], 'gpt-6-astra')
        self.assertEqual(final['default_subagent_reasoning_effort'], 'medium')
        self.assertEqual(final['agents']['default_subagent_model'], 'gpt-6-astra')
        self.assertEqual(final['agents']['default_subagent_reasoning_effort'], 'medium')
        self.assertEqual(self.config.read_text(), switched)
        omitted = switched.replace('default_subagent_model = "gpt-6-astra"\n', '').replace('default_subagent_reasoning_effort = "medium"\n', '')
        self.config.write_text(omitted)
        self.install()
        self.assertEqual(self.config.read_text(), omitted)
        self.assertNotIn('default_subagent_model', tomllib.loads(omitted)['agents'])

    def test_ambiguous_inline_and_dotted_owned_layouts_refuse_without_mutation(self):
        for config in ('agents = { enabled = true, max_concurrent_threads_per_session = 8 }\n',
                       'agents.enabled = true\n'):
            with self.subTest(config=config):
                self.write_config(config)
                before = self.config.read_bytes()
                self.run_cli('--apply', '--adopt-existing', ok=False)
                self.assertEqual(self.config.read_bytes(), before)
                self.assertFalse((self.state / 'routing-owned-state.json').exists())

    def test_missing_codex_binary_refuses_without_mutation(self):
        self.write_config('model = "gpt-6-astra"\n')
        before = self.config.read_bytes()
        self.env['CODING_AGENT_CODEX_BIN'] = str(self.root / 'absent-codex')
        self.run_cli('--apply', ok=False)
        self.assertEqual(self.config.read_bytes(), before)
        self.assertFalse((self.state / 'routing-owned-state.json').exists())

    def test_multiline_user_text_that_looks_like_owned_header_is_preserved(self):
        original = b"note = \'\'\'\n[agents.architect]\n[agents]\n\'\'\'\nmodel = \"gpt-6-astra\"\n"
        self.write_config(original)
        self.install()
        self.run_cli('--remove')
        self.assertEqual(self.config.read_bytes(), original)

    def test_remove_preserves_unrelated_external_changes(self):
        original = b'model = "gpt-6-astra"\nmodel_provider = "openai"\n'
        self.write_config(original)
        self.install()
        self.config.write_bytes(self.config.read_bytes() + b'\n# added by another writer\n[custom]\nvalue = "keep"\n')
        self.run_cli('--remove')
        result = self.config.read_bytes()
        self.assertIn(b'# added by another writer\n[custom]\nvalue = "keep"', result)
        self.assertIn(b'model = "gpt-6-astra"', result)
        self.assertNotIn(b'[agents.architect]', result)
        self.assertFalse((self.state / 'routing-owned-state.json').exists())

    def test_concurrent_change_before_config_replace_is_preserved_and_recoverable(self):
        self.write_config('model = "gpt-6-astra"\n')
        spec = importlib.util.spec_from_file_location('codex_routing_injected', SCRIPT)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        real_atomic = module.atomic
        injected = {'done': False}
        def inject(path, expected, desired):
            if path == self.config and not injected['done']:
                injected['done'] = True
                path.write_bytes(expected + b'\n# concurrent writer\n')
            return real_atomic(path, expected, desired)
        with mock.patch.dict(os.environ, self.env, clear=False), \
             mock.patch.object(module, 'atomic', side_effect=inject), \
             mock.patch.object(sys, 'argv', [str(SCRIPT), '--apply']), \
             contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            os.environ.pop('CODEX_HOME', None)
            with self.assertRaises(ValueError):
                module.main()
        self.assertTrue(injected['done'])
        self.assertIn(b'# concurrent writer', self.config.read_bytes())
        receipt_path = self.state / 'routing-owned-state.json'
        self.assertEqual(json.loads(receipt_path.read_text())['phase'], 'prepared')
        self.install()
        self.run_cli('--check')
        self.assertIn(b'# concurrent writer', self.config.read_bytes())

    def test_malformed_config_symlink_catalog_and_schema_fail_closed(self):
        self.write_config('model = [\n')
        before = self.config.read_bytes()
        self.run_cli('--apply', ok=False)
        self.assertEqual(self.config.read_bytes(), before)
        self.config.unlink()
        outside = self.root / 'outside.toml'
        outside.write_text('model = "keep"\n')
        self.config.symlink_to(outside)
        self.run_cli('--apply', ok=False)
        self.assertEqual(outside.read_text(), 'model = "keep"\n')
        self.config.unlink()
        self.write_config('model = "keep"\n')
        self.bin.write_text('#!/bin/sh\necho \'{"models": []}\'\n')
        self.bin.chmod(0o755)
        self.run_cli('--apply', ok=False)
        self.assertEqual(self.config.read_text(), 'model = "keep"\n')

    def test_native_warning_about_malformed_role_fails_before_mutation(self):
        self.write_config('model = "gpt-6-astra"\n')
        self.bin.write_text(self.bin.read_text().replace('sys.stdin.buffer.read()\n sys.exit(0)',
          'sys.stdin.buffer.read()\n print("Ignoring malformed agent role definition", file=sys.stderr)\n sys.exit(0)'))
        before = self.config.read_bytes()
        self.run_cli('--apply', ok=False)
        self.assertEqual(self.config.read_bytes(), before)
        self.assertFalse((self.state / 'routing-owned-state.json').exists())

    def test_catalog_effort_mismatch_fails_before_mutation(self):
        self.write_config('model = "gpt-6-astra"\n')
        catalog = self.bin.read_text().replace('("low","medium","high","xhigh","max")', '("low","medium")')
        self.bin.write_text(catalog)
        before = self.config.read_bytes()
        self.run_cli('--apply', ok=False)
        self.assertEqual(self.config.read_bytes(), before)
        self.assertFalse((self.state / 'routing-owned-state.json').exists())

    def test_native_strict_schema_rejection_fails_before_mutation(self):
        self.write_config('model = "gpt-6-astra"\n')
        self.bin.write_text(self.bin.read_text().replace('sys.exit(0)', 'sys.exit(9)'))
        before = self.config.read_bytes()
        result = self.run_cli('--apply', ok=False)
        self.assertEqual(self.config.read_bytes(), before)
        self.assertFalse((self.state / 'routing-owned-state.json').exists())

    def test_default_profile_cannot_mask_named_routing_drift(self):
        self.write_config('model_provider = "openai"\n')
        self.install()
        self.config.write_text('profile = "work"\n' + self.config.read_text())
        (self.state / 'work.config.toml').write_text('[agents]\nenabled = false\n')
        before = self.config.read_bytes()
        result = self.run_cli('--check', ok=False)
        self.assertIn('Profile-owned named routing', result.stderr)
        self.run_cli('--launch-check', ok=False)
        self.assertEqual(self.config.read_bytes(), before)

    def test_catalog_reads_the_selected_target_home(self):
        self.write_config('model_provider = "openai"\n')
        code = self.bin.read_text().replace('import json, sys', 'import json, sys, os')
        code = code.replace(' print(json.dumps', ' assert os.environ["CODEX_HOME"] == ' + repr(str(self.state)) + '\n print(json.dumps', 1)
        self.bin.write_text(code)
        self.install()
        self.run_cli('--check')

    def test_wrong_scalar_types_are_not_exact_adoption(self):
        for value in ('[agents]\nenabled = 1\n', '[agents]\nmax_concurrent_threads_per_session = 8.0\n'):
            self.write_config(value)
            self.run_cli('--apply', '--adopt-existing', ok=False)
            self.assertEqual(self.config.read_text(), value)

    def test_role_modification_after_install_is_not_overwritten(self):
        self.write_config('model = "keep"\n')
        self.install()
        role = self.state / 'agents' / 'architect.toml'
        role.write_bytes(role.read_bytes() + b'\n# external change\n')
        before_config = self.config.read_bytes()
        self.run_cli('--apply', ok=False)
        self.assertEqual(self.config.read_bytes(), before_config)
        self.run_cli('--remove', ok=False)
        self.assertTrue(role.exists())


if __name__ == '__main__':
    unittest.main()
