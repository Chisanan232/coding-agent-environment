#!/usr/bin/env python3
"""Offline contract examples; fixture anchors do not grade arbitrary prose."""
import json
import os
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

FIXTURES = json.loads((Path(__file__).parent / 'fixtures/signal-density.json').read_text())


def findings(case: dict, text: str) -> list[str]:
    first = text.split('. ', 1)[0]
    problems = [] if case['critical'][0]['anchor'].casefold() in first.casefold() else ['outcome first']
    problems += [fact['meaning'] for fact in case['critical']
                if fact['anchor'].casefold() not in text.casefold()]
    problems += ['unsupported: ' + claim for claim in case['unsupported']
                 if claim.casefold() in text.casefold()]
    problems += ['noise: ' + phrase for phrase in case['noise']
                 if phrase.casefold() in text.casefold()]
    return problems


def tempting_warning(text: str) -> bool:
    return bool(re.search(r'everything looks great|awesome|files changed|\bthen\b', text, re.I)) or len(text.split()) > 150


class SignalDensityExamples(unittest.TestCase):
    def test_preservation_and_negative_controls(self) -> None:
        for case in FIXTURES['cases']:
            with self.subTest(case=case['id']):
                self.assertEqual(findings(case, case['candidate']), [])
                self.assertTrue(findings(case, case['source']))
                self.assertIn('outcome first', findings(case, 'First I opened a file. ' + case['candidate']))
                for fact in case['critical']:
                    omitted = case['candidate'].replace(fact['anchor'], '')
                    self.assertIn(fact['meaning'], findings(case, omitted))
                for claim in case['unsupported']:
                    self.assertIn('unsupported: ' + claim,
                                  findings(case, case['candidate'] + ' ' + claim))
                # More words can preserve every fact and still reintroduce a diary.
                for phrase in case['noise']:
                    self.assertIn('noise: ' + phrase,
                                  findings(case, case['candidate'] + ' ' + phrase))

    def test_code_comment_decisions(self) -> None:
        import ast
        for case in FIXTURES['cases']:
            if 'code' not in case:
                continue
            with self.subTest(case=case['id']):
                code = re.search(r'```python\n(.*?)\n```', case['candidate'], re.S).group(1)
                tree = ast.parse(code)
                self.assertEqual(any(line.lstrip().startswith('#') for line in code.splitlines()),
                                 case['code']['comment'])
                for node in ast.walk(tree):
                    if isinstance(node, ast.FunctionDef):
                        self.assertIsNone(ast.get_docstring(node))
                namespace = {}
                exec(compile(tree, '<signal-density-fixture>', 'exec'), namespace)
                for expression in case['code']['assertions']:
                    self.assertTrue(eval(expression, namespace))

    def test_style_warning_precision_is_insufficient(self) -> None:
        false_positives = sum(tempting_warning(c['text']) for c in FIXTURES['benign'])
        self.assertEqual(false_positives, 4)
        false_negatives = sum(not tempting_warning(c['candidate'].replace(c['critical'][-1]['anchor'], ''))
                              for c in FIXTURES['cases'])
        self.assertGreater(false_negatives, 0)


class BriefingInstallation(unittest.TestCase):
    def test_shared_body_report_apply_and_drift(self) -> None:
        root = Path(__file__).resolve().parent.parent
        with tempfile.TemporaryDirectory(prefix='codex-briefing-') as temp:
            live = Path(temp) / 'home'
            desired_root = Path(temp) / 'repo'
            for rel in ['.claude', 'codex', 'scripts']:
                shutil.copytree(root / rel, desired_root / rel)
            manifest = json.loads((desired_root / 'codex/subtraction-skills.json').read_text())
            import hashlib
            for name, spec in manifest['skills'].items():
                folder = live / '.agents/skills' / name
                folder.mkdir(parents=True)
                (folder / 'SKILL.md').write_text('Disposable external skill fixture')
                spec['files'] = {'SKILL.md': hashlib.sha256((folder / 'SKILL.md').read_bytes()).hexdigest()}
            (desired_root / 'codex/subtraction-skills.json').write_text(json.dumps(manifest))
            config = live / '.codex/config.toml'
            config.parent.mkdir(parents=True)
            config.write_text('local_setting = "preserve"\n')
            agents = live / '.codex/AGENTS.md'
            local_bytes = b'Local unrelated instructions\r\n'
            agents.write_bytes(local_bytes)
            protected = [live / '.codex/auth.json', live / '.codex/hooks.json', live / '.codex/private-overlay.toml']
            for path in protected:
                path.write_bytes(b'Unrelated private/generated fixture state')
            env = dict(os.environ, CODING_AGENT_SYNC_HOME=str(live))
            cmd = ['bash', str(desired_root / 'scripts/profile-install.sh'), '--global']
            target = live / '.codex/skills/evidence-first-briefing/SKILL.md'
            source = root / '.claude/skills/evidence-first-briefing/SKILL.md'
            workflow_target = live / '.codex/skills/engineering-workflow/SKILL.md'
            workflow_source = root / '.claude/skills/engineering-workflow/SKILL.md'
            def run(*args):
                return subprocess.run(cmd + list(args), env=env, capture_output=True, text=True)
            before_report = {p: p.read_bytes() for p in live.rglob('*') if p.is_file()}
            self.assertEqual(run('--dry-run').returncode, 0)
            self.assertEqual(before_report, {p: p.read_bytes() for p in live.rglob('*') if p.is_file()})
            self.assertFalse(target.exists())
            self.assertEqual(run('--check').returncode, 1)
            self.assertEqual(run().returncode, 0)
            self.assertEqual(target.read_bytes(), source.read_bytes())
            self.assertEqual(workflow_target.read_bytes(), workflow_source.read_bytes())
            before = {p: p.read_bytes() for p in live.rglob('*') if p.is_file()}
            self.assertEqual(run().returncode, 0)
            self.assertEqual(before, {p: p.read_bytes() for p in live.rglob('*') if p.is_file()})
            target.write_text('manual drift')
            self.assertEqual(run('--check').returncode, 1)
            self.assertEqual(run().returncode, 0)
            self.assertEqual(run('--check').returncode, 0)
            workflow_target.write_text('workflow drift')
            self.assertEqual(run('--check').returncode, 1)
            self.assertEqual(run().returncode, 0)
            self.assertEqual(workflow_target.read_bytes(), workflow_source.read_bytes())
            self.assertEqual(config.read_text(), 'local_setting = "preserve"\n')
            backups = [path.read_text() for path in (live / '.codex/backups').rglob('SKILL.md')]
            self.assertIn('manual drift', backups)
            self.assertIn('workflow drift', backups)
            self.assertTrue(agents.read_bytes().startswith(local_bytes))
            for path in protected:
                self.assertEqual(path.read_bytes(), b'Unrelated private/generated fixture state')
            policy = agents.read_bytes()
            agents.write_bytes(policy.replace(b'Create only', b'Manual drift: create only'))
            self.assertEqual(run('--check').returncode, 1)
            self.assertEqual(run().returncode, 0)
            self.assertEqual(agents.read_bytes(), policy)
            # Preflight errors must leave every surface unchanged, including a drifted skill.
            for broken in [b'<!-- coding-agent-environment:signal-first:end --><!-- coding-agent-environment:signal-first:start -->',
                           b'<!-- coding-agent-environment:signal-first:start -->',
                           policy + policy[policy.index(b'<!-- coding-agent-environment:signal-first:start -->'):]]:
                agents.write_bytes(broken)
                target.write_text('do not partially repair')
                before = {p: p.read_bytes() for p in live.rglob('*') if p.is_file()}
                self.assertNotEqual(run().returncode, 0)
                self.assertEqual(before, {p: p.read_bytes() for p in live.rglob('*') if p.is_file()})
            agents.write_bytes(policy)
            elsewhere = live / 'unowned.md'
            elsewhere.write_text('private unrelated skill')
            target.unlink()
            target.symlink_to(elsewhere)
            self.assertNotEqual(run().returncode, 0)
            self.assertEqual(elsewhere.read_text(), 'private unrelated skill')
            target.unlink()
            self.assertEqual(run().returncode, 0)
            override = live / '.codex/AGENTS.override.md'
            override.write_text('user override')
            self.assertNotEqual(run().returncode, 0)
            override.unlink()
            self.assertNotEqual(run('--check', '--dry-run').returncode, 0)
            external = live / '.agents/skills/codebase-zero/SKILL.md'
            external.write_text('external drift')
            self.assertEqual(run('--check').returncode, 1)
            before = agents.read_bytes()
            self.assertNotEqual(run().returncode, 0)
            self.assertEqual(agents.read_bytes(), before)


class DesiredStateDrift(unittest.TestCase):
    def test_bootstrap_and_drift_are_report_only(self) -> None:
        root = Path(__file__).resolve().parent.parent
        with tempfile.TemporaryDirectory(prefix='signal-sync-') as temp:
            live = Path(temp)
            shutil.copytree(root / '.claude', live / '.claude')
            shutil.copy2(root / '.mcp.json', live / '.claude/.mcp.json')
            (live / '.codex').mkdir()
            shutil.copy2(root / 'codex/AGENTS.md', live / '.codex/AGENTS.md')
            shutil.copytree(root / 'bin', live / '.local/bin')
            settings = live / '.claude/settings.json'
            data = json.loads(settings.read_text())
            original_model = data['model']
            desired = json.loads((root / '.claude/settings.global-only.json').read_text())
            data['enabledPlugins'] = desired['enabledPlugins']
            data['extraKnownMarketplaces'] = desired['extraKnownMarketplaces']
            settings.write_text(json.dumps(data))
            desired_root = live / 'desired-repo'
            for rel in ['.claude', 'bin', 'scripts', 'codex']:
                shutil.copytree(root / rel, desired_root / rel)
            shutil.copy2(root / '.mcp.json', desired_root / '.mcp.json')
            manifest_path = desired_root / 'codex/subtraction-skills.json'
            manifest = json.loads(manifest_path.read_text())
            import hashlib
            for name, spec in manifest['skills'].items():
                folder = live / '.agents/skills' / name
                folder.mkdir(parents=True)
                (folder / 'SKILL.md').write_text('Disposable canonical drift fixture')
                spec['files'] = {'SKILL.md': hashlib.sha256((folder / 'SKILL.md').read_bytes()).hexdigest()}
            manifest_path.write_text(json.dumps(manifest))
            dest = live / '.codex/skills/evidence-first-briefing'
            dest.mkdir(parents=True)
            shutil.copy2(root / '.claude/skills/evidence-first-briefing/SKILL.md', dest / 'SKILL.md')
            workflow_dest = live / '.codex/skills/engineering-workflow'
            workflow_dest.mkdir(parents=True)
            shutil.copy2(root / '.claude/skills/engineering-workflow/SKILL.md',
                         workflow_dest / 'SKILL.md')
            root = desired_root
            env = dict(os.environ, CODING_AGENT_SYNC_HOME=str(live))
            applied = subprocess.run(['bash', str(root / 'scripts/profile-install.sh'), '--global'],
                                     env=env, capture_output=True, text=True)
            self.assertEqual(applied.returncode, 0, applied.stdout + applied.stderr)

            def check(expected: int, marker: str = '') -> None:
                before = {p.relative_to(live): p.read_bytes()
                          for p in live.rglob('*') if p.is_file()}
                result = subprocess.run(['bash', str(root / 'scripts/sync-check.sh')],
                                        env=env, capture_output=True, text=True)
                self.assertEqual(result.returncode, expected, result.stdout + result.stderr)
                self.assertIn(marker, result.stdout)
                after = {p.relative_to(live): p.read_bytes()
                         for p in live.rglob('*') if p.is_file()}
                self.assertEqual(before, after, 'drift checking must never overwrite live state')

            check(0, '0 differ, 0 missing-on-live')
            skill = live / '.claude/skills/evidence-first-briefing/SKILL.md'
            original = skill.read_text()
            skill.write_text(original + '\nUnexpected changed contract.\n')
            check(1, 'DIFFERS        .claude/skills/evidence-first-briefing/SKILL.md')
            skill.write_text(original)
            data['enabledPlugins']['requirement-zero@requirement-zero'] = False
            settings.write_text(json.dumps(data))
            check(1, 'canonical subtraction plugin desired state')
            data['enabledPlugins']['requirement-zero@requirement-zero'] = True
            data['extraKnownMarketplaces']['requirement-zero']['source']['repo'] = 'other/fork'
            settings.write_text(json.dumps(data))
            check(1, 'canonical subtraction plugin desired state')
            data['extraKnownMarketplaces']['requirement-zero']['source']['repo'] = 'Chisanan232/requirement-zero'
            data['model'] = 'unexpected-model'
            settings.write_text(json.dumps(data))
            check(1, '.claude/settings.json (owned settings)')
            data['model'] = original_model
            settings.write_text(json.dumps(data))
            skill.unlink()
            check(1, 'MISSING-LIVE   .claude/skills/evidence-first-briefing/SKILL.md')
            skill.write_text(original)
            check(0, '0 differ, 0 missing-on-live')


if __name__ == '__main__':
    result = unittest.TextTestRunner(verbosity=1).run(unittest.defaultTestLoader.loadTestsFromModule(__import__(__name__)))
    noisy = sum(len(c['source'].split()) for c in FIXTURES['cases'])
    signal = sum(len(c['candidate'].split()) for c in FIXTURES['cases'])
    facts = sum(len(c['critical']) for c in FIXTURES['cases'])
    print(f"{len(FIXTURES['cases'])} surfaces/examples; {facts} critical fact anchors; {noisy} → {signal} words.")
    print('Curated examples and omission/unsupported/diary controls only; no general semantic or measured read-time claim.')
    raise SystemExit(not result.wasSuccessful())
