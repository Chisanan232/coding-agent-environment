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


def installer_owned(text):
    """Line numbers inside a coding-agent-environment marker pair: replaced by design, not founder text."""
    owned, start = set(), {}
    for number, line in enumerate(text.splitlines(), 1):
        match = re.search(r'<!-- coding-agent-environment:([\w-]+):(start|end) -->', line)
        if match and match.group(2) == 'start':
            start[match.group(1)] = number
        elif match and match.group(1) in start:
            owned.update(range(start.pop(match.group(1)), number + 1))
    return owned


present = {L.normalize(line) for _, line in L.content_lines(after)}
overlay_missing, open_conflicts, unledgered = [], [], []
managed_before = installer_owned(before)
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
bad = L.check_citations(rows, args.repo)
record(not bad, 'citations-exist-in-pinned-checkout', ','.join(bad[:10]) or '-')

for name, pattern in sorted(FORBIDDEN.items()):
    hits = []
    for label, text in (('main', after), ('skill', skill)):
        hits += ['%s:%d' % (label, n) for n, line in enumerate(text.splitlines(), 1) if pattern.search(line)]
    record(not hits, 'forbidden-' + name, ','.join(hits[:10]) or '-')

for name, where, pattern in REQUIRED[args.host]:
    text = skill if where == 'skill' else after
    hit = next((n for n, line in enumerate(text.splitlines(), 1) if re.search(pattern, line, re.IGNORECASE)), None)
    record(hit is not None, 'required-' + name, '%s:%s' % (where, hit or '-'))

titles = [l for l in after.splitlines() if l.startswith('## ')]
for failed, name, where in sorted(results):
    print('%s %s %s' % ('FAIL' if failed else 'PASS', name, where))
print('info installer-owned-before-lines-replaced=%d (review: marker-span text the installer rewrote) first=%s' % (
    len(replaced), ','.join(map(str, replaced[:12])) or '-'))
print('info lines before=%d after=%d duplicate-titles=%d' % (
    len(before.splitlines()), len(after.splitlines()), len(titles) - len(set(titles))))
sys.exit(1 if any(r[0] for r in results) else 0)
