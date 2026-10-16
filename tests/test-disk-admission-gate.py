#!/usr/bin/env python3
"""The disk-admission PreToolUse gate classifies heavy builds and only ever denies."""
from importlib.util import module_from_spec, spec_from_file_location
import os
import io
import json
from pathlib import Path
import tempfile
import time
from unittest import mock
import unittest

ROOT = Path(__file__).resolve().parents[1]
spec = spec_from_file_location('disk_gate', ROOT / '.claude/hooks/disk-admission-gate.py')
gate = module_from_spec(spec)
spec.loader.exec_module(gate)
gate.AUDIT_ENABLED = False  # tests must not write the operator's audit file; audit tests re-enable it


class Classification(unittest.TestCase):
    def heavy(self, command):
        return gate.classify(command)['heavy']

    def test_light_commands_and_quoted_text_are_not_heavy(self):
        for command in ('cargo --version', "git commit -m 'cargo build'", 'grep cargo src/main.rs',
                        'echo cargo build', 'cargo fmt', 'cargo', 'npm run build', 'docker ps',
                        'docker buildx ls', 'ls -la', 'cat Cargo.toml', 'make build', 'command -v cargo',
                        'sh -c "echo cargo build"'):
            self.assertFalse(self.heavy(command), command)
        # Negative control: the same words as a real command are heavy.
        self.assertTrue(self.heavy('cargo build'))

    def test_each_supported_heavy_tool_is_heavy(self):
        for command in ('cargo build', 'cargo test -p x', 'cargo check', 'cargo doc', 'cargo clippy', 'cargo nextest run',
                        'cargo bench', 'cargo rustdoc', 'cargo +nightly build', 'cargo -C x test', '/opt/homebrew/bin/cargo build',
                        'maturin build', 'maturin develop', 'npm install', 'npm ci', 'npm build', 'pnpm install',
                        'pnpm build', 'yarn install', 'yarn', 'uv sync', 'uv pip install x', 'pip install x',
                        'python3 -m pip install x', 'docker build .', 'docker buildx build .', 'xcodebuild -scheme x'):
            self.assertTrue(self.heavy(command), command)

    def test_env_prefix_chaining_and_rtk_twin(self):
        for command in ('FOO=1 cargo build', 'A=1 B=2 cargo test', 'env FOO=1 cargo check', 'cd crates/x && cargo test',
                        'git pull; cargo build', 'ls | cargo build', '(cd x && cargo build)', 'time cargo build',
                        'rtk cargo build', 'rtk cargo nextest run -p x', 'FOO=1 rtk cargo test'):
            verdict = gate.classify(command)
            self.assertTrue(verdict['heavy'], command)
            self.assertEqual(verdict['confidence'], 'exact', command)
        self.assertEqual(gate.classify('rtk cargo build')['kind'], 'cargo')
        # Negative control: env prefix on a non-heavy command stays light.
        self.assertFalse(self.heavy('FOO=1 cargo --version'))
        self.assertFalse(self.heavy('cd cargo && ls build'))

    def test_shell_dash_c_is_heavy_but_best_effort(self):
        for command in ('bash -c "cargo build"', 'sh -c "cd x && cargo test"', 'zsh -lc "npm ci"'):
            verdict = gate.classify(command)
            self.assertTrue(verdict['heavy'], command)
            self.assertEqual(verdict['confidence'], 'best-effort', command)
        # Unparsable input naming a heavy tool is heavy but flagged unparsable.
        verdict = gate.classify("echo 'unterminated cargo build")
        self.assertEqual((verdict['heavy'], verdict['confidence']), (True, 'unparsable'))

    def test_documented_blind_spots_are_not_detected(self):
        for command in ('make build', 'just build', 'npm run build', './target/debug/tool', 'xargs cargo build < list'):
            self.assertFalse(self.heavy(command), command)
        doc = (ROOT / '.claude/hooks/disk-admission-gate.py').read_text()
        for name in ('make', 'just', 'npm run', 'direct binaries', '`!` shell', 'IDE'):
            self.assertIn(name, doc)


class TargetAdvice(unittest.TestCase):
    def advice(self, command, exists=lambda path: False):
        return gate.target_dir_advice(gate.classify(command), exists)

    def test_new_scratch_target_dir_gets_shared_target_advice(self):
        scratch = '/private/tmp/claude-501/x/scratchpad/target'
        for command in ('CARGO_TARGET_DIR=%s cargo build' % scratch, 'cargo test --target-dir %s' % scratch,
                        'cargo test --target-dir=%s' % scratch, 'env CARGO_TARGET_DIR=/tmp/t cargo check',
                        'rtk cargo build --target-dir /var/folders/ab/T/t'):
            message = self.advice(command)
            self.assertIn('shared target', message or '', command)
            self.assertIn('.cargo/shared-target', message)
        # Negative controls: existing dir, shared target, non-scratch path, no override, non-cargo.
        self.assertIsNone(self.advice('CARGO_TARGET_DIR=%s cargo build' % scratch, lambda path: True))
        self.assertIsNone(self.advice('cargo build --target-dir ~/.cargo/shared-target'))
        self.assertIsNone(self.advice('cargo build --target-dir /Users/someone/work/target'))
        self.assertIsNone(self.advice('cargo build'))
        self.assertIsNone(self.advice('npm ci'))
        self.assertIsNone(self.advice('cargo build --target-dir relative/target'))


class Mode(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'mode'

    def mode(self, content=None):
        if content is not None:
            self.path.write_bytes(content)
        return gate.read_mode(self.path)

    def test_mode_file_defaults_to_warn_and_only_exact_enforce_enforces(self):
        self.assertEqual(self.mode(), 'warn')
        self.assertEqual(self.mode(b'enforce\n'), 'enforce')
        self.assertEqual(self.mode(b'  enforce  '), 'enforce')
        # Negative controls: warn, anything else, wrong case, empty and binary all fall back to warn.
        for content in (b'warn\n', b'', b'ENFORCE', b'enforce please', b'\xff\xfe', b'off'):
            self.assertEqual(self.mode(content), 'warn', content)
        self.path.unlink()
        os.mkfifo(self.path)
        self.assertEqual(gate.read_mode(self.path), 'warn')
        self.path.unlink()
        self.path.symlink_to(Path(self.temp.name) / 'elsewhere')
        (Path(self.temp.name) / 'elsewhere').write_text('enforce')
        self.assertEqual(gate.read_mode(self.path), 'warn')


class Decisions(unittest.TestCase):
    def run_gate(self, command, mode, runner, tool='Bash'):
        payload = json.dumps({'tool_name': tool, 'tool_input': {'command': command}, 'session_id': 's'})
        out, err = io.StringIO(), io.StringIO()
        code = gate.main(payload, out, err, mode_fn=lambda: mode, runner=runner)
        return code, out.getvalue(), err.getvalue()

    @staticmethod
    def refuses():
        return 77, 'heavy refused in tier low; tier=low free=35.9 GiB'

    def test_warn_mode_allows_and_adds_context_when_admission_would_refuse(self):
        code, out, err = self.run_gate('cargo build', 'warn', self.refuses)
        self.assertEqual((code, err), (0, ''))
        context = json.loads(out)['hookSpecificOutput']
        self.assertEqual(context['hookEventName'], 'PreToolUse')
        self.assertIn('would block', context['additionalContext'])
        self.assertIn('tier=low', context['additionalContext'])
        # Negative control: an admitted heavy command is allowed silently.
        self.assertEqual(self.run_gate('cargo build', 'warn', lambda: (0, 'admitted')), (0, '', ''))

    def test_enforce_mode_denies_with_exit_2_and_a_clear_message(self):
        for command in ('cargo build', 'rtk cargo test', 'cd x && npm ci', 'bash -c "cargo check"'):
            code, out, err = self.run_gate(command, 'enforce', self.refuses)
            self.assertEqual((code, out), (2, ''), command)
            self.assertIn('BLOCKED by disk-admission-gate', err)
            self.assertIn('tier=low', err)
        # Negative controls: admitted heavy and any non-heavy command are allowed.
        self.assertEqual(self.run_gate('cargo build', 'enforce', lambda: (0, 'ok')), (0, '', ''))
        self.assertEqual(self.run_gate('git status', 'enforce', self.refuses), (0, '', ''))

    def test_fail_closed_only_in_enforce(self):
        def raises():
            raise RuntimeError('adapter exploded')
        for runner in (lambda: (None, 'timed out'), lambda: (2, 'usage'), lambda: (1, 'crash'), raises):
            code, out, err = self.run_gate('cargo build', 'enforce', runner)
            self.assertEqual((code, out), (2, ''))
            self.assertIn('could not be evaluated', err)
            code, out, err = self.run_gate('cargo build', 'warn', runner)
            self.assertEqual((code, err), (0, ''))
            self.assertIn('could not be evaluated', json.loads(out)['hookSpecificOutput']['additionalContext'])
        # A non-heavy command never reaches the adapter, so its failure cannot block it.
        self.assertEqual(self.run_gate('ls', 'enforce', raises), (0, '', ''))

    def test_gate_never_rewrites_the_command(self):
        outputs = []
        for mode in ('warn', 'enforce'):
            for runner in (self.refuses, lambda: (0, 'ok'), lambda: (None, 'down')):
                outputs.append(self.run_gate('FOO=1 rtk cargo build --target-dir /tmp/new-x', mode, runner))
        for code, out, err in outputs:
            for text in (out, err):
                self.assertNotIn('updatedInput', text)
                self.assertNotIn('"command"', text)
                self.assertNotIn('permissionDecision', text)
        self.assertEqual({code for code, _, _ in outputs}, {0, 2})

    def test_unusable_input_is_allowed_and_target_advice_never_denies(self):
        def forbidden():
            raise AssertionError('adapter must not run')
        for raw in ('', 'not json', '[]', '{"tool_name": "Bash"}', '{"tool_name": "Bash", "tool_input": {"command": 5}}'):
            out, err = io.StringIO(), io.StringIO()
            self.assertEqual(gate.main(raw, out, err, mode_fn=lambda: 'enforce', runner=forbidden), 0, raw)
            self.assertEqual((out.getvalue(), err.getvalue()), ('', ''))
        self.assertEqual(self.run_gate('cargo build', 'enforce', forbidden, tool='Read'), (0, '', ''))
        code, out, err = self.run_gate('CARGO_TARGET_DIR=/private/tmp/never-existed-x cargo build', 'enforce',
                                       lambda: (0, 'ok'))
        self.assertEqual((code, err), (0, ''))
        self.assertIn('shared target', json.loads(out)['hookSpecificOutput']['additionalContext'])

    def test_adapter_is_resolved_from_this_checkout_and_asks_for_heavy(self):
        self.assertEqual(gate.adapter_path(), ROOT / 'scripts/disk-admission.py')
        with mock.patch.object(gate.subprocess, 'run', return_value=mock.Mock(returncode=77, stdout='refused\n', stderr='')) as run:
            self.assertEqual(gate.run_adapter(), (77, 'refused'))
        self.assertEqual(run.call_args.args[0][2:], ['check', '--weight', 'heavy'])
        with mock.patch.object(gate.subprocess, 'run', side_effect=gate.subprocess.TimeoutExpired('x', 1)):
            self.assertEqual(gate.run_adapter()[0], None)


class Newlines(unittest.TestCase):
    def heavy(self, command):
        return gate.classify(command)['heavy']

    def test_newline_chain_is_heavy(self):
        self.assertTrue(self.heavy('echo a\ncargo build'))
        self.assertTrue(self.heavy('ls\n\n  npm ci\n'))
        # Negative control: the same lines without a heavy command stay light.
        self.assertFalse(self.heavy('echo a\necho cargo build'))

    def test_heredoc_body_is_data(self):
        self.assertFalse(self.heavy('cat <<EOF\ncargo build\nEOF'))
        self.assertFalse(self.heavy("cat <<'EOF'\ncargo build\nEOF\nls"))
        self.assertFalse(self.heavy('cat <<-EOF\n\tcargo build\n\tEOF\nls'))
        # Negative control: a command after the terminator is real.
        self.assertTrue(self.heavy('cat <<EOF\nx\nEOF\ncargo test'))

    def test_heredoc_with_trailing_command_on_the_operator_line_is_heavy(self):
        self.assertTrue(self.heavy('cat <<EOF && cargo build\nx\nEOF'))
        self.assertTrue(self.heavy('cargo build <<EOF\nx\nEOF'))

    def test_crlf_line_endings_separate_commands(self):
        self.assertTrue(self.heavy('echo a\r\ncargo build'))
        self.assertFalse(self.heavy('cat <<EOF\r\ncargo build\r\nEOF\r\nls'))

    def test_apostrophe_inside_heredoc_body_does_not_break_parsing(self):
        verdict = gate.classify("cat <<EOF\nit's a trap\nEOF\ncargo build")
        self.assertEqual((verdict['heavy'], verdict['confidence']), (True, 'exact'))
        verdict = gate.classify("cat <<EOF\nit's a trap\nEOF\nls")
        self.assertEqual((verdict['heavy'], verdict['confidence']), (False, 'exact'))

    def test_multi_line_heredoc_body_is_skipped_to_the_terminator(self):
        self.assertFalse(self.heavy('cat <<EOF\nfirst\ncargo build\nlast\nEOF'))
        self.assertTrue(self.heavy('cat <<EOF\nfirst\ncargo build\nEOF\ncargo test'))
    def test_crlf_heredoc_terminator_is_recognised(self):
        self.assertTrue(self.heavy('cat <<EOF\r\nfirst\r\ncargo build\r\nEOF\r\ncargo test'))
        self.assertFalse(self.heavy('cat <<EOF\r\nfirst\r\ncargo build\r\nEOF\r\nls'))


class Compound(unittest.TestCase):
    def heavy(self, command):
        return gate.classify(command)['heavy']

    def test_brace_group_is_heavy(self):
        self.assertTrue(self.heavy('{ cargo build; }'))
        self.assertFalse(self.heavy('{ echo cargo build; }'))

    def test_if_then_body_is_heavy_but_a_light_condition_is_not(self):
        self.assertTrue(self.heavy('if x; then cargo build; fi'))
        self.assertTrue(self.heavy('if true; then echo a; else cargo test; fi'))
        self.assertFalse(self.heavy('if cargo --version; then echo ok; fi'))

    def test_loop_body_is_heavy(self):
        self.assertTrue(self.heavy('for crate in a b; do cargo test -p $crate; done'))
        self.assertTrue(self.heavy('while true; do npm ci; done'))
        self.assertFalse(self.heavy('for x in a b; do echo $x; done'))

    def test_negated_command_is_heavy(self):
        self.assertTrue(self.heavy('! cargo build'))
        self.assertFalse(self.heavy('! cargo --version'))

    def test_backtick_substitution_is_heavy(self):
        self.assertTrue(self.heavy('echo `cargo build`'))
        self.assertFalse(self.heavy('echo `date`'))

    def test_nice_with_adjustment_is_heavy(self):
        self.assertTrue(self.heavy('nice -n 10 cargo build'))
        self.assertTrue(self.heavy('nice -10 cargo build'))
        self.assertFalse(self.heavy('nice -n 10 ls'))

    def test_sudo_with_user_option_is_heavy(self):
        self.assertTrue(self.heavy('sudo -u builder cargo build'))
        self.assertTrue(self.heavy('sudo -E npm ci'))
        self.assertFalse(self.heavy('sudo -u builder ls'))

    def test_timeout_with_duration_is_heavy(self):
        self.assertTrue(self.heavy('timeout 600 cargo test'))
        self.assertTrue(self.heavy('timeout -s KILL 5 npm ci'))
        self.assertFalse(self.heavy('timeout 5 echo cargo build'))

    def test_watch_is_heavy(self):
        self.assertTrue(self.heavy('watch -n 5 cargo check'))
        self.assertFalse(self.heavy('watch -n 5 df -h'))

    def test_rtk_proxy_is_heavy(self):
        self.assertTrue(self.heavy('rtk proxy cargo build'))
        self.assertFalse(self.heavy('rtk proxy git status'))

    def test_shell_option_cluster_before_dash_c_is_heavy_best_effort(self):
        for command in ('bash -euo pipefail -c "cargo build"', 'zsh -o errexit -c "npm ci"', 'bash -ec "cargo test"'):
            verdict = gate.classify(command)
            self.assertEqual((verdict['heavy'], verdict['confidence']), (True, 'best-effort'), command)
        self.assertFalse(self.heavy('bash -euo pipefail -c "echo cargo build"'))
        self.assertFalse(self.heavy('bash -x script.sh cargo build'))

    def test_eval_is_heavy_best_effort(self):
        for command in ('eval "cargo build"', 'eval cargo build'):
            verdict = gate.classify(command)
            self.assertEqual((verdict['heavy'], verdict['confidence']), (True, 'best-effort'), command)
        self.assertFalse(self.heavy('eval "echo cargo build"'))

    def test_cargo_manifest_path_before_the_subcommand_is_heavy(self):
        self.assertTrue(self.heavy('cargo --manifest-path x/Cargo.toml build'))
        self.assertTrue(self.heavy('cargo --manifest-path=x/Cargo.toml test'))
        self.assertFalse(self.heavy('cargo --manifest-path x/Cargo.toml metadata'))

    def test_cargo_toolchain_selector_before_the_subcommand_is_heavy(self):
        self.assertTrue(self.heavy('cargo +nightly build'))
        self.assertTrue(self.heavy('cargo +1.80 -C x --locked test'))
        self.assertFalse(self.heavy('cargo +nightly fmt'))

    def test_cargo_run_install_rustc_and_aliases_are_heavy(self):
        for command in ('cargo run', 'cargo r', 'cargo install ripgrep', 'cargo rustc', 'cargo bench', 'cargo t', 'cargo c'):
            self.assertTrue(self.heavy(command), command)
        for command in ('cargo tree', 'cargo metadata', 'cargo help build', 'cargo fmt', 'cargo --version'):
            self.assertFalse(self.heavy(command), command)

    def test_package_manager_test_is_heavy(self):
        for command in ('npm test', 'pnpm test', 'yarn test', 'npm t'):
            self.assertTrue(self.heavy(command), command)
        self.assertFalse(self.heavy('npm --version'))
        self.assertFalse(self.heavy('npm run test'))


class Uncertain(unittest.TestCase):
    UNPARSABLE = "echo 'unterminated && cargo build"

    @staticmethod
    def refuses():
        return 77, 'heavy refused in tier low'

    def run_gate(self, command, mode, runner):
        payload = json.dumps({'tool_name': 'Bash', 'tool_input': {'command': command}})
        out, err = io.StringIO(), io.StringIO()
        code = gate.main(payload, out, err, mode_fn=lambda: mode, runner=runner)
        return code, out.getvalue(), err.getvalue()

    def test_unparsable_heavy_command_is_denied_in_enforce(self):
        code, out, err = self.run_gate(self.UNPARSABLE, 'enforce', self.refuses)
        self.assertEqual((code, out), (2, ''))
        self.assertIn('unparsable', err)
        self.assertEqual(self.run_gate(self.UNPARSABLE, 'enforce', lambda: (0, 'ok')), (0, '', ''))

    def test_unparsable_heavy_command_is_allowed_with_advice_in_warn(self):
        def forbidden():
            raise AssertionError('warn mode must not call the adapter for an unparsable command')
        code, out, err = self.run_gate(self.UNPARSABLE, 'warn', forbidden)
        self.assertEqual((code, err), (0, ''))
        self.assertIn('unparsable', json.loads(out)['hookSpecificOutput']['additionalContext'])

    def test_unparsable_command_without_a_heavy_word_is_light(self):
        for mode in ('warn', 'enforce'):
            self.assertEqual(self.run_gate("echo 'unterminated", mode, self.refuses), (0, '', ''))

    def test_oversize_input_is_scanned_linearly_and_fast(self):
        filler = 'echo hello world; ' * 60000
        started = time.monotonic()
        heavy = gate.classify(filler + 'cargo build')
        light = gate.classify(filler)
        self.assertLess(time.monotonic() - started, 5)
        self.assertGreater(len(filler), 1000000)
        self.assertEqual((heavy['heavy'], heavy['confidence']), (True, 'oversize'))
        self.assertEqual((light['heavy'], light['confidence']), (False, 'oversize'))
        self.assertEqual(self.run_gate(filler + 'cargo build', 'enforce', self.refuses)[0], 2)

    def test_undecodable_bytes_in_the_payload_do_not_hide_a_heavy_command(self):
        payload = b'{"tool_name": "Bash", "tool_input": {"command": "echo \xff\xfe; cargo build"}}'
        out, err = io.StringIO(), io.StringIO()
        code = gate.main(payload, out, err, mode_fn=lambda: 'enforce', runner=self.refuses)
        self.assertEqual(code, 2)
        self.assertIn('BLOCKED', err.getvalue())
    def test_warn_advice_for_an_unparsable_command_explains_the_uncertainty(self):
        code, out, err = self.run_gate(self.UNPARSABLE, 'warn', self.refuses)
        self.assertEqual((code, err), (0, ''))
        self.assertIn('may be a heavy build', json.loads(out)['hookSpecificOutput']['additionalContext'])
        self.assertNotIn('refuses', out)


class Tamper(unittest.TestCase):
    DIR = '~/.cache/coding-agent-environment/admission/'

    def run_gate(self, command, mode):
        payload = json.dumps({'tool_name': 'Bash', 'tool_input': {'command': command}})
        out, err = io.StringIO(), io.StringIO()
        code = gate.main(payload, out, err, mode_fn=lambda: mode, runner=lambda: (0, 'ok'))
        return code, out.getvalue(), err.getvalue()

    def writes(self):
        d = self.DIR
        return ['echo warn > %smode' % d, 'echo x >> %smode' % d, 'printf warn | tee %smode' % d,
                'mv /tmp/m %smode' % d, 'rm %sstate.json' % d, 'cp /tmp/m %smode' % d,
                "sed -i '' s/enforce/warn/ %smode" % d, 'chflags nouchg %smode' % d,
                "python3 -c \"open('/Users/u/.cache/coding-agent-environment/admission/mode','w')\"",
                "bash -c 'echo warn > %smode'" % d]

    def test_tamper_suspect_commands_are_denied_in_enforce(self):
        for command in self.writes():
            code, out, err = self.run_gate(command, 'enforce')
            self.assertEqual((code, out), (2, ''), command)
            self.assertIn('tamper-suspect', err)

    def test_tamper_suspect_commands_only_advise_in_warn(self):
        for command in self.writes():
            code, out, err = self.run_gate(command, 'warn')
            self.assertEqual((code, err), (0, ''), command)
            self.assertIn('tamper-suspect', json.loads(out)['hookSpecificOutput']['additionalContext'])

    def test_reading_the_state_directory_is_not_tamper(self):
        d = self.DIR
        for command in ('cat %smode' % d, 'ls -la %s' % d, 'grep enforce %smode' % d, 'sed s/a/b/ %smode' % d,
                        'echo hi', 'echo %smode' % d):
            self.assertEqual(self.run_gate(command, 'enforce'), (0, '', ''), command)


class GateInputs(unittest.TestCase):
    @staticmethod
    def forbidden():
        raise AssertionError('adapter must not run')

    def gate_main(self, raw, mode='enforce', runner=None, tool='Bash'):
        out, err = io.StringIO(), io.StringIO()
        code = gate.main(raw, out, err, mode_fn=lambda: mode, runner=runner or self.forbidden)
        return code, out.getvalue(), err.getvalue()

    def test_malformed_payloads_are_allowed_silently(self):
        for raw in ('', 'not json', '[]', '{"tool_name": "Bash"}', '{"tool_name": "Bash", "tool_input": {"command": 5}}'):
            self.assertEqual(self.gate_main(raw), (0, '', ''), raw)


    def test_non_bash_tools_are_ignored(self):
        raw = json.dumps({'tool_name': 'Read', 'tool_input': {'command': 'cargo build'}})
        self.assertEqual(self.gate_main(raw), (0, '', ''))
        # Negative control: the same payload for Bash reaches the adapter.
        raw = json.dumps({'tool_name': 'Bash', 'tool_input': {'command': 'cargo build'}})
        self.assertEqual(self.gate_main(raw)[0], 2)  # the adapter was reached and its failure closed


    def test_target_dir_advice_never_denies_in_enforce(self):
        raw = json.dumps({'tool_name': 'Bash',
                          'tool_input': {'command': 'CARGO_TARGET_DIR=/private/tmp/never-existed-x cargo build'}})
        code, out, err = self.gate_main(raw, runner=lambda: (0, 'ok'))
        self.assertEqual((code, err), (0, ''))
        self.assertIn('shared target', json.loads(out)['hookSpecificOutput']['additionalContext'])


    def test_adapter_is_resolved_from_this_checkout(self):
        self.assertEqual(gate.adapter_path(), ROOT / 'scripts/disk-admission.py')


    def test_adapter_is_asked_for_heavy_work_only(self):
        with mock.patch.object(gate.subprocess, 'run', return_value=mock.Mock(returncode=77, stdout='refused\n', stderr='')) as run:
            self.assertEqual(gate.run_adapter(), (77, 'refused'))
        self.assertEqual(run.call_args.args[0][2:], ['check', '--weight', 'heavy'])


    def test_adapter_timeout_is_a_failure_not_an_admission(self):
        with mock.patch.object(gate.subprocess, 'run', side_effect=gate.subprocess.TimeoutExpired('x', 1)):
            code, text = gate.run_adapter()
        self.assertIsNone(code)
        self.assertTrue(text)


class Documentation(unittest.TestCase):
    def test_same_user_limit_and_operator_hardening_are_documented(self):
        for relative in ('.claude/hooks/disk-admission-gate.py', 'docs/STORAGE-INTEGRATION.md'):
            text = (ROOT / relative).read_text()
            self.assertIn('not a security boundary', text, relative)
            self.assertIn('chflags uchg', text, relative)
            self.assertIn('0444', text, relative)
        self.assertIn('--check-fresh', (ROOT / 'docs/STORAGE-INTEGRATION.md').read_text())


class AdmissionDirFlag(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.dir = Path(self.temp.name).resolve() / 'sandbox'
        self.dir.mkdir()

    def run_gate(self, command, runner):
        payload = json.dumps({'tool_name': 'Bash', 'tool_input': {'command': command}})
        out, err = io.StringIO(), io.StringIO()
        code = gate.main(payload, out, err, runner=runner, admission_dir=self.dir)
        return code, out.getvalue(), err.getvalue()

    def test_flag_forms_are_parsed_and_relative_paths_ignored(self):
        self.assertEqual(gate.parse_admission_dir(['--admission-dir', '/x/y']), Path('/x/y'))
        self.assertEqual(gate.parse_admission_dir(['--admission-dir=/x/y']), Path('/x/y'))
        self.assertIsNone(gate.parse_admission_dir(['--admission-dir', 'relative']))
        self.assertIsNone(gate.parse_admission_dir(['--admission-dir']))
        self.assertIsNone(gate.parse_admission_dir([]))

    def test_mode_is_read_from_the_sandbox_directory(self):
        refuses = lambda: (77, 'heavy refused in tier low')
        self.assertEqual(self.run_gate('cargo build', refuses)[0], 0)  # no mode file: warn
        (self.dir / 'mode').write_text('enforce\n')
        code, out, err = self.run_gate('cargo build', refuses)
        self.assertEqual((code, out), (2, ''))
        self.assertIn('BLOCKED', err)

    def test_sandbox_directory_is_protected_from_tampering(self):
        command = 'echo warn > %s/mode' % self.dir
        (self.dir / 'mode').write_text('enforce')
        code, out, err = self.run_gate(command, lambda: (0, 'ok'))
        self.assertEqual(code, 2)
        self.assertIn('tamper-suspect', err)
        # Negative control: reading the same file is fine.
        self.assertEqual(self.run_gate('cat %s/mode' % self.dir, lambda: (0, 'ok')), (0, '', ''))

    def test_sandbox_adapter_run_writes_state_only_inside_the_sandbox(self):
        code, text = gate.run_adapter(self.dir)
        self.assertIn(code, (0, 77))
        self.assertIn('tier=', text)
        self.assertTrue((self.dir / 'state.json').exists())


class AuditTrail(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.dir = Path(self.temp.name).resolve() / 'sandbox'
        patcher = mock.patch.object(gate, 'AUDIT_ENABLED', True)
        patcher.start()
        self.addCleanup(patcher.stop)

    @property
    def log(self):
        return self.dir / 'gate-audit.jsonl'

    def lines(self):
        return [json.loads(line) for line in self.log.read_text().splitlines()] if self.log.exists() else []

    def run_gate(self, command, mode='warn', runner=None):
        self.dir.mkdir(exist_ok=True)
        (self.dir / 'mode').write_text(mode)
        payload = json.dumps({'tool_name': 'Bash', 'tool_input': {'command': command}})
        runner = runner or (lambda: (77, 'heavy refused in tier low; tier=low free=35.9 GiB bytes=38512345678'))
        return gate.main(payload, io.StringIO(), io.StringIO(), runner=runner, admission_dir=self.dir)

    def test_heavy_command_writes_exactly_one_audit_line(self):
        self.run_gate('cargo build')
        (entry,) = self.lines()
        self.assertEqual((entry['classification'], entry['tool'], entry['confidence']), ('heavy', 'cargo', 'exact'))
        self.assertEqual((entry['mode'], entry['decision']), ('warn', 'warn'))
        self.assertEqual((entry['tier'], entry['free_bytes']), ('low', 38512345678))
        self.assertRegex(entry['ts'], r'^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$')

    def test_benign_commands_write_no_audit_line(self):
        for command in ('git status', 'echo cargo build', 'cargo --version', 'ls -la', 'cat Cargo.toml'):
            self.run_gate(command)
        self.assertEqual(self.lines(), [])
        self.assertFalse(self.log.exists())

    def test_audit_line_never_contains_the_command_text(self):
        command = 'cargo build --features SECRET_MARKER_8431 -p hidden-crate'
        self.run_gate(command)
        raw = self.log.read_text()
        for fragment in ('SECRET_MARKER_8431', 'hidden-crate', '--features', 'cargo build'):
            self.assertNotIn(fragment, raw)
        import hashlib
        self.assertEqual(self.lines()[0]['cmd_sha12'], hashlib.sha256(command.encode()).hexdigest()[:12])

    def test_audit_file_rotates_at_the_cap_keeping_one_backup(self):
        with mock.patch.object(gate, 'AUDIT_MAX_BYTES', 700):
            for _ in range(12):
                self.run_gate('cargo build')
        backup = Path(str(self.log) + '.1')
        self.assertTrue(backup.exists())
        self.assertLessEqual(self.log.stat().st_size, 700)
        self.assertLessEqual(backup.stat().st_size, 700)
        self.assertEqual(sorted(entry.name for entry in self.dir.iterdir() if entry.name.startswith('gate-audit')),
                         ['gate-audit.jsonl', 'gate-audit.jsonl.1'])
        # Negative control: below the cap nothing rotates.
        other = self.dir.parent / 'other'
        other.mkdir()
        gate.audit_append(other, {'classification': 'heavy'}, 'x')
        self.assertEqual(sorted(entry.name for entry in other.iterdir()), ['gate-audit.jsonl'])

    def test_audit_file_is_private_and_a_symlink_is_not_followed(self):
        self.run_gate('cargo build')
        self.assertEqual(self.log.stat().st_mode & 0o777, 0o600)
        self.log.unlink()
        victim = self.dir.parent / 'victim'
        victim.write_text('untouched')
        self.log.symlink_to(victim)
        self.run_gate('cargo build')
        self.assertEqual(victim.read_text(), 'untouched')

    def test_decisions_allow_warn_and_deny_and_tamper_are_recorded(self):
        self.run_gate('cargo build', 'warn', lambda: (0, 'heavy admitted; tier=stable free=150.0 GiB bytes=161061273600'))
        self.run_gate('cargo build', 'warn')
        self.run_gate('cargo build', 'enforce')
        self.run_gate('echo warn > %s/mode' % self.dir, 'enforce')
        decisions = [(e['classification'], e['mode'], e['decision']) for e in self.lines()]
        self.assertEqual(decisions, [('heavy', 'warn', 'allow'), ('heavy', 'warn', 'warn'),
                                     ('heavy', 'enforce', 'deny'), ('tamper-suspect', 'enforce', 'deny')])
        self.assertEqual(self.lines()[0]['tier'], 'stable')

    def test_an_unwritable_audit_location_never_changes_the_decision(self):
        self.dir.mkdir()
        (self.dir / 'mode').write_text('enforce')
        os.chmod(self.dir, 0o500)
        self.addCleanup(os.chmod, self.dir, 0o700)
        payload = json.dumps({'tool_name': 'Bash', 'tool_input': {'command': 'cargo build'}})
        out, err = io.StringIO(), io.StringIO()
        code = gate.main(payload, out, err, runner=lambda: (77, 'refused'), admission_dir=self.dir)
        self.assertEqual(code, 2)
        self.assertIn('BLOCKED', err.getvalue())

    def test_an_unwritable_audit_location_keeps_the_normal_refusal_message(self):
        self.dir.mkdir()
        (self.dir / 'mode').write_text('enforce')
        os.chmod(self.dir, 0o500)
        self.addCleanup(os.chmod, self.dir, 0o700)
        payload = json.dumps({'tool_name': 'Bash', 'tool_input': {'command': 'cargo build'}})
        err = io.StringIO()
        gate.main(payload, io.StringIO(), err, runner=lambda: (77, 'heavy refused in tier low'), admission_dir=self.dir)
        self.assertIn('disk admission refuses heavy command cargo', err.getvalue())
        self.assertNotIn('internal error', err.getvalue())


class RolloutDocumentation(unittest.TestCase):
    def test_docs_state_the_staged_rollout_audit_and_scope(self):
        text = (ROOT / 'docs/STORAGE-INTEGRATION.md').read_text()
        for needle in ('hook-rollout.py plan|apply|verify|rollback', 'gate-audit.jsonl', '--admission-dir',
                       'WARN, verify, ENFORCE', 'not yet verifiable', 'Nothing here\nclaims host-wide coverage'):
            self.assertIn(needle.lower(), text.lower().replace('  ', ' '), needle)


class AcceptanceHarness(unittest.TestCase):
    def test_harness_loads_stays_out_of_ci_and_never_fakes_the_admitted_path(self):
        path = ROOT / 'tests/acceptance/gate-acceptance.py'
        harness = module_from_spec(spec_from_file_location('gate_acceptance', path))
        harness.__spec__.loader.exec_module(harness)
        self.assertIn('cargo build --help', harness.prompt('cargo build --help'))
        self.assertFalse(path.name.startswith('test-'))
        text = path.read_text()
        self.assertIn('NOT_YET_VERIFIABLE', text)
        self.assertIn('--admission-dir', text)


class HarnessWorkdir(unittest.TestCase):
    def setUp(self):
        path = ROOT / 'tests/acceptance/gate-acceptance.py'
        self.harness = module_from_spec(spec_from_file_location('gate_acceptance_wd', path))
        self.harness.__spec__.loader.exec_module(self.harness)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name).resolve()

    def test_home_root_and_repository_paths_are_refused(self):
        home = Path(gate.account_home())
        for path in (home, Path('/'), home.parent, ROOT, ROOT / 'tests'):
            with self.assertRaises(self.harness.RefusedWorkdir, msg=str(path)):
                self.harness.claim_workdir(path)
        self.assertTrue(home.exists() and ROOT.exists())

    def test_an_existing_directory_without_the_sentinel_is_refused_and_untouched(self):
        victim = self.base / 'important'
        victim.mkdir()
        (victim / 'fx').mkdir()
        (victim / 'fx/precious.txt').write_text('keep')
        (victim / 'notes.txt').write_text('keep')
        with self.assertRaises(self.harness.RefusedWorkdir):
            self.harness.claim_workdir(victim)
        self.assertEqual((victim / 'fx/precious.txt').read_text(), 'keep')
        self.assertEqual((victim / 'notes.txt').read_text(), 'keep')

    def test_a_fresh_directory_is_claimed_and_reused_without_touching_other_files(self):
        work = self.base / 'run'
        self.assertEqual(self.harness.claim_workdir(work), work)
        self.assertTrue((work / self.harness.SENTINEL).is_file())
        (work / 'sandbox-admission').mkdir()
        (work / 'sandbox-admission/old').write_text('stale')
        (work / 'unrelated.txt').write_text('keep')
        self.harness.claim_workdir(work)
        self.assertFalse((work / 'sandbox-admission').exists())
        self.assertEqual((work / 'unrelated.txt').read_text(), 'keep')

    def test_a_directory_inside_a_git_working_tree_is_refused(self):
        repo = self.base / 'repo'
        repo.mkdir()
        gate_subprocess = __import__('subprocess')
        gate_subprocess.run(['git', 'init', '-q'], cwd=repo, check=True)
        with self.assertRaises(self.harness.RefusedWorkdir):
            self.harness.claim_workdir(repo)
        with self.assertRaises(self.harness.RefusedWorkdir):
            self.harness.claim_workdir(repo / 'child')
        self.assertFalse((repo / 'child').exists())


class HungAdapter(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.stub = Path(self.temp.name) / 'disk-admission.py'
        self.stub.write_text('import time\ntime.sleep(60)\n')
        for patcher in (mock.patch.object(gate, 'adapter_path', return_value=self.stub),
                        mock.patch.object(gate, 'ADAPTER_TIMEOUT_S', 1)):
            patcher.start()
            self.addCleanup(patcher.stop)

    def run_gate(self, mode):
        mode_dir = Path(self.temp.name)
        (mode_dir / 'mode').write_text(mode)
        payload = json.dumps({'tool_name': 'Bash', 'tool_input': {'command': 'cargo build'}})
        out, err = io.StringIO(), io.StringIO()
        started = time.monotonic()
        # No admission_dir and no injected runner: this is the production subprocess path.
        code = gate.main(payload, out, err, mode_fn=lambda: mode)
        return code, out.getvalue(), err.getvalue(), time.monotonic() - started

    def test_hung_adapter_is_a_refusal_in_enforce_and_never_a_hang(self):
        code, out, err, took = self.run_gate('enforce')
        self.assertEqual((code, out), (2, ''))
        self.assertIn('could not be evaluated', err)
        self.assertLess(took, 10)


if __name__ == '__main__':
    unittest.main()
