#!/usr/bin/env python3
"""Verify the EFFECTIVE final instruction files after a policy migration (read-only).

Checks, each reported as `PASS|FAIL name file:line` with no policy text:
  lossless  every content line of the pre-migration snapshot is still present in the final file, or is
            ledgered as SEMANTICALLY_PRESERVED / SUPERSEDED_BY_APPROVED_RULE (and cites a real repo line);
            a PRESERVE_IN_PRIVATE_OVERLAY line must still be present; no REAL_CONFLICT row may remain
  forbidden a superseded rule form must not be active in the instruction file or an installed skill
  required  the approved canonical rules are present in the installed skill / instruction file
Exit status is non-zero when any check fails. Output is sorted and byte-identical across runs.
Usage: verify-effective.py --host claude|codex --before SNAPSHOT --after FILE --skills-dir DIR \
           --ledger LEDGER --repo CHECKOUT
"""
import argparse
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import ledger as L  # noqa: E402

FORBIDDEN = {
    'legacy-commit-or-pr-format': re.compile(r'<emoji>\s*<scope>\s*:'),
}
# (name, where, pattern): `skill` = <skills-dir>/engineering-workflow/SKILL.md, `main` = the instruction file.
REQUIRED = {
    'claude': [
        ('branch-format', 'skill', r'<version-or-phase>/<ticket-id>/<type>/<short_summary>'),
        ('worktree-name', 'skill', r'<repo>-<phase>-<ticket>-<type>-<summary>'),
        ('commit-format', 'skill', r'<GitEmoji> \(<scope>\): <imperative summary>'),
        ('pr-title-format', 'skill', r'\[<TICKET>\] <GitEmoji> \(<scope>\): <imperative summary under 60 chars>'),
        ('commit-example', 'skill', r'✨ \(model\): Add property to TestModel'),
        ('pr-title-example', 'skill', r'\[PROJ-123\] ✨ \(restapi\): Add user authentication'),
        ('independent-review', 'skill', r'independent review'),
        ('subagent-ceiling', 'skill', r'at most seven concurrent subagents'),
        ('pr-only-delivery', 'skill', r'never push directly'),
        ('skill-is-linked', 'main', r'skills/engineering-workflow/SKILL\.md'),
        ('resource-admission', 'main', r'## Resource Admission'),
        ('release-authority', 'main', r'release owner'),
        ('secret-rules-imported', 'main', r'^@rules/secret-handling\.md'),
    ],
    'codex': [
        ('branch-format', 'skill', r'<version-or-phase>/<ticket-id>/<type>/<short_summary>'),
        ('worktree-name', 'skill', r'<repo>-<phase>-<ticket>-<type>-<summary>'),
        ('commit-format', 'skill', r'<GitEmoji> \(<scope>\): <imperative summary>'),
        ('pr-title-format', 'skill', r'\[<TICKET>\] <GitEmoji> \(<scope>\): <imperative summary under 60 chars>'),
        ('commit-example', 'skill', r'✨ \(model\): Add property to TestModel'),
        ('pr-title-example', 'skill', r'\[PROJ-123\] ✨ \(restapi\): Add user authentication'),
        ('independent-review', 'skill', r'independent review'),
        ('subagent-ceiling', 'skill', r'at most seven concurrent subagents'),
        ('pr-only-delivery', 'skill', r'never push directly'),
        ('skill-is-linked', 'main', r'engineering-workflow'),
        ('format-ownership', 'main', r'alone defines the commit, branch, worktree and pull-request formats'),
        ('release-authority', 'main', r'release owner'),
    ],
}

parser = argparse.ArgumentParser()
parser.add_argument('--host', required=True, choices=sorted(REQUIRED))
parser.add_argument('--before', required=True)
parser.add_argument('--after', required=True)
parser.add_argument('--skills-dir', required=True)
parser.add_argument('--ledger', required=True)
parser.add_argument('--repo', required=True)
parser.add_argument('--strict', action='store_true', help='also fail when any superseded line is still present (retire path)')
args = parser.parse_args()
try:
    rows = L.load(args.ledger)
except L.LedgerError as error:
    sys.exit('refusing: %s' % error)
before = Path(args.before).read_text(encoding='utf-8')
after_path = Path(args.after)
after = after_path.read_text(encoding='utf-8')
skill_path = Path(args.skills_dir) / 'engineering-workflow/SKILL.md'
skill = skill_path.read_text(encoding='utf-8') if skill_path.is_file() else ''
results = []


def record(ok, name, where):
    results.append((0 if ok else 1, name, where))


present = {L.normalize(line) for _, line in L.content_lines(after)}
overlay_missing, open_conflicts, unledgered = [], [], []
managed_before = L.marker_lines(before, L.REPLACED_BY_DESIGN)
replaced = []
for number, line in L.content_lines(before):
    if number in managed_before:
        if L.normalize(line) not in present:
            replaced.append(number)
        continue
    key, here = L.line_key(line), L.normalize(line) in present
    disposition = rows.get(key, (None,))[0]
    if disposition == L.CONFLICT:
        open_conflicts.append(number)
    elif disposition == L.OVERLAY and not here:
        overlay_missing.append(number)
    elif not here and disposition not in (L.SEMANTIC, L.SUPERSEDED):
        unledgered.append(number)
for name, numbers in (('lossless-unaccounted-line', unledgered), ('lossless-overlay-line-missing', overlay_missing),
                      ('open-conflict-in-ledger', open_conflicts)):
    record(not numbers, name, 'before:' + (','.join(map(str, numbers[:20])) or '-'))
after_owned = L.marker_lines(after, L.REPLACED_BY_DESIGN)
after_active = {}  # key -> first line number, outside installer-replaced spans
for number, line in L.content_lines(after):
    if number not in after_owned:
        after_active.setdefault(L.line_key(line), number)
before_keys = {L.line_key(l) for n, l in L.content_lines(before) if n not in managed_before}
still_there, tolerated = [], 0
for key, (disposition, _, replacement) in sorted(rows.items()):
    if disposition != L.SUPERSEDED:
        continue
    if replacement and key in before_keys:
        if key in after_active:
            still_there.append(after_active[key])
    elif not replacement and key in after_active:
        if args.strict:
            still_there.append(after_active[key])
        else:
            tolerated += 1
record(not still_there, 'superseded-line-still-active', 'after:' + (','.join(map(str, sorted(set(still_there))[:20])) or '-'))
bad = L.check_citations(rows, args.repo)
record(not bad, 'citations-exist-in-pinned-checkout', ','.join(bad[:10]) or '-')

skill_files = sorted(Path(args.skills_dir).rglob('*.md')) if Path(args.skills_dir).is_dir() else []
for name, pattern in sorted(FORBIDDEN.items()):
    hits = []
    for label, text in [('main', after)] + [(f.relative_to(args.skills_dir).as_posix(), f.read_text(encoding='utf-8', errors='replace')) for f in skill_files]:
        hits += ['%s:%d' % (label, n) for n, line in enumerate(L.split_lines(text), 1) if pattern.search(line)]
    record(not hits, 'forbidden-' + name, ','.join(hits[:10]) or '-')

for name, where, pattern in REQUIRED[args.host]:
    text = skill if where == 'skill' else after
    hit = next((n for n, line in enumerate(L.split_lines(text), 1) if re.search(pattern, line, re.IGNORECASE)), None)
    record(hit is not None, 'required-' + name, '%s:%s' % (where, hit or '-'))

titles = [l for l in L.split_lines(after) if l.startswith('## ')]
for failed, name, where in sorted(results):
    print('%s %s %s' % ('FAIL' if failed else 'PASS', name, where))
print('info installer-owned-before-lines-replaced=%d (review: marker-span text the installer rewrote) first=%s' % (
    len(replaced), ','.join(map(str, replaced[:12])) or '-'))
print('info superseded-lines-still-present-tolerated=%d (use --strict on the retire path)' % tolerated)
print('info lines before=%d after=%d duplicate-titles=%d' % (
    len(L.split_lines(before)), len(L.split_lines(after)), len(titles) - len(set(titles))))
sys.exit(1 if any(r[0] for r in results) else 0)
