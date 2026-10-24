#!/usr/bin/env python3
"""Check the UI/UX policy, its templates, and the smoke fixture stay consistent."""
import json
from pathlib import Path
import re
import unittest

SOURCE = Path(__file__).resolve().parents[1]
POLICY = SOURCE / 'docs/UI-UX-POLICY.md'
TEMPLATES = ('DESIGN.md', 'DESIGN-BRIEF.md', 'UI-QA-CHECKLIST.md')
AUTHORITY = (
    'Product requirements and real users',
    'Product-owned Design System, brand, and Design Constitution',
    'Platform conventions',
    'Impeccable art direction, critique, and refinement',
    'Vercel Web Interface Guidelines review',
    'Optional references',
    'Rendered verification',
)
OPTIONAL = ('21st', 'UI/UX Pro Max', 'brandkit', 'canvas-design', 'design-taste-frontend', 'remotion-best-practices')


class UiUxPolicy(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.text = POLICY.read_text()

    def test_authority_layers_appear_in_order(self):
        section = self.text.split('## Authority order', 1)[1].split('\n## ', 1)[0]
        rows = re.findall(r'^(\d)\. \*\*(.+?)\*\*', section, re.MULTILINE)
        self.assertEqual([int(number) for number, _ in rows], list(range(1, 8)))
        for (_, title), expected in zip(rows, AUTHORITY):
            self.assertTrue(title.startswith(expected), f'{title!r} should start with {expected!r}')

    def test_exactly_one_preferred_figma_route(self):
        self.assertEqual(len(re.findall(r'one preferred Figma route', self.text)), 1)
        row = next(line for line in self.text.splitlines() if line.startswith('| Design source'))
        self.assertIn('official **Figma plugin**', row)
        self.assertIn('Framelink', row)
        self.assertIn('non-preferred', row)

    def test_optional_tools_are_marked_not_installed(self):
        section = self.text.split('### Optional, not installed', 1)[1]
        self.assertIn('NOT INSTALLED', section)
        for name in OPTIONAL:
            self.assertIn(name, section)

    def test_one_primary_art_direction_skill_rule(self):
        self.assertIn('Exactly **one** primary skill per task', self.text)

    def test_links_visual_skills_doc_without_duplicating_its_pins(self):
        self.assertIn('(VISUAL-SKILLS.md)', self.text)
        self.assertNotIn('2ab3cae7ac2c2a55d7386ca789d03c4fcd31816c', self.text)
        self.assertNotIn('6376b7727bb9b1d5d37ad3e4d96d969a6499917b', self.text)

    def test_every_relative_link_resolves(self):
        for target in re.findall(r'\]\((?!https?://|#)([^)#]+)', self.text):
            self.assertTrue((POLICY.parent / target).resolve().exists(), target)

    def test_templates_exist_and_link_back_to_the_policy(self):
        for name in TEMPLATES:
            template = SOURCE / 'docs/templates' / name
            self.assertTrue(template.is_file(), name)
            self.assertIn('UI-UX-POLICY.md', template.read_text(), name)

    def test_manifest_has_no_unvetted_design_packages(self):
        manifest = json.loads((SOURCE / 'codex/subtraction-skills.json').read_text())
        pinned = set(manifest['visuals']['packages'])
        self.assertFalse({'impeccable', 'web-design-guidelines'} & pinned)
        self.assertEqual(pinned, {'archify', 'visual-explainer'})

    def test_smoke_fixture_screenshots_are_never_tracked(self):
        self.assertIn('.qa-artifacts/', (SOURCE / '.gitignore').read_text().splitlines())
        fixture = SOURCE / 'tests/fixtures/ui-smoke'
        self.assertEqual(sorted(path.name for path in fixture.iterdir()), ['checks.js', 'index.html'])
        html = (fixture / 'index.html').read_text()
        self.assertIn('prefers-reduced-motion', html)
        self.assertIn(':focus-visible', html)


if __name__ == '__main__':
    unittest.main()
