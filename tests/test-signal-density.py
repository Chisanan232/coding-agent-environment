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

    def test_style_warning_precision_is_insufficient(self) -> None:
        false_positives = sum(tempting_warning(c['text']) for c in FIXTURES['benign'])
        self.assertEqual(false_positives, 4)
        false_negatives = sum(not tempting_warning(c['candidate'].replace(c['critical'][-1]['anchor'], ''))
                              for c in FIXTURES['cases'])
        self.assertGreater(false_negatives, 0)


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
            env = dict(os.environ, CODING_AGENT_SYNC_HOME=str(live))

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
