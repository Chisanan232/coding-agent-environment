"""Disposition ledger: a local-only, hash-keyed record of what happens to each live policy line.

The ledger holds line hashes, a disposition and a citation id, never the private line text. The one
exception is the optional replacement column, which carries a PUBLIC canonical line (for example the
approved commit-subject format) used to rewrite a named superseded line.

Format (tab separated, `#` comments, mode 0600, owned by the operator):
    L:<12 hex>  DISPOSITION  citation  [replacement line]
"""
import hashlib
import os
import re
import stat
from pathlib import Path

SEMANTIC = 'SEMANTICALLY_PRESERVED'
OVERLAY = 'PRESERVE_IN_PRIVATE_OVERLAY'
SUPERSEDED = 'SUPERSEDED_BY_APPROVED_RULE'
CONFLICT = 'REAL_CONFLICT_REQUIRING_OWNER_DECISION'
DISPOSITIONS = (SEMANTIC, OVERLAY, SUPERSEDED, CONFLICT)
# A citation is `repo/path.md:123` (verified against a pinned checkout) or `owner-decision:<id>`.
CITATION = re.compile(r'^(?:owner-decision:[A-Za-z0-9._-]+|[A-Za-z0-9._/-]+:\d+)$')
TABLE_RULE = re.compile(r'^\s*\|?[\s:|-]+\|[\s:|-]*$')


class LedgerError(Exception):
    pass


def normalize(line):
    return ' '.join(line.split()).lower()


def line_key(line):
    return 'L:' + hashlib.sha256(normalize(line).encode()).hexdigest()[:12]


def is_structural(line):
    """Blank lines, code-fence delimiters, horizontal rules and table separator rows carry no rule."""
    stripped = line.strip()
    return (not stripped or stripped.startswith('```') or stripped == '---'
            or bool(TABLE_RULE.match(stripped) and set(stripped) <= set('|-: ')))


def content_lines(text):
    """[(1-based number, line)] for every line that can carry a rule."""
    return [(n, l) for n, l in enumerate(text.splitlines(), 1) if not is_structural(l)]


def check_private_file(path):
    info = path.lstat()
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise LedgerError('%s must be a regular file, not a symlink' % path.name)
    if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o600:
        raise LedgerError('%s must be owned by you with mode 0600' % path.name)


def load(path):
    """{key: (disposition, citation, replacement or None)}; fails closed on any malformed row."""
    path = Path(path)
    check_private_file(path)
    rows = {}
    for number, raw in enumerate(path.read_text(encoding='utf-8').splitlines(), 1):
        if not raw.strip() or raw.lstrip().startswith('#'):
            continue
        fields = raw.split('\t')
        if len(fields) not in (3, 4):
            raise LedgerError('ledger row %d: expected 3 or 4 tab separated fields' % number)
        key, disposition, citation = fields[:3]
        replacement = fields[3] if len(fields) == 4 and fields[3] else None
        if not re.fullmatch(r'L:[0-9a-f]{12}', key):
            raise LedgerError('ledger row %d: bad key' % number)
        if disposition not in DISPOSITIONS:
            raise LedgerError('ledger row %d: unknown disposition' % number)
        if not CITATION.match(citation):
            raise LedgerError('ledger row %d: bad citation' % number)
        if key in rows:
            raise LedgerError('ledger row %d: duplicate key' % number)
        if replacement is not None and disposition not in (SUPERSEDED, CONFLICT):
            raise LedgerError('ledger row %d: only a superseded or conflicting line may carry a replacement' % number)
        rows[key] = (disposition, citation, replacement)
    return rows


def default_path(home=None):
    local = Path(os.environ.get('CODING_AGENT_LOCAL_DIR')
                 or (Path(home) if home else Path.home()) / '.config/coding-agent-environment/local')
    return Path(os.environ.get('CODING_AGENT_LOCAL_BASELINE_DIR') or local / 'baseline') / 'claude/policy-ledger.tsv'


def check_citations(rows, repo):
    """Every file:line citation must exist in the pinned checkout. Existence only; meaning is a review."""
    bad = []
    for key, (_, citation, _) in sorted(rows.items()):
        if citation.startswith('owner-decision:'):
            continue
        name, _, number = citation.rpartition(':')
        target = Path(repo) / name
        if not target.is_file() or int(number) > len(target.read_text(encoding='utf-8').splitlines()) or int(number) < 1:
            bad.append(key)
    return bad
