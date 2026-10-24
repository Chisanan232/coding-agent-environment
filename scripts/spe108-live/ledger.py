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
FENCE = re.compile(r'^\s*```[A-Za-z0-9_+.-]*\s*$')
MARKER = re.compile(r'^\s*<!-- coding-agent-environment:([\w-]+):(start|end) -->\s*$')
# Spans the installer rewrites by design. The private overlay is NOT one: its text is founder text.
REPLACED_BY_DESIGN = ('engineering-workflow', 'signal-first')


class LedgerError(Exception):
    pass


def normalize(line):
    return ' '.join(line.split()).lower()


def line_key(line):
    return 'L:' + hashlib.sha256(normalize(line).encode()).hexdigest()[:12]


def split_lines(text):
    """Split on newline only, like an editor; str.splitlines also breaks on \\x0c, \\x85, \\u2028 and friends."""
    parts = text.split('\n')
    return parts[:-1] if parts and parts[-1] == '' else parts


def split_keepends(text):
    return re.findall(r'[^\n]*\n|[^\n]+', text)


def is_structural(line):
    """Blank lines, bare code-fence delimiters, horizontal rules and table separator rows carry no rule.
    A fence opener with trailing words is content."""
    stripped = line.strip()
    return (not stripped or bool(FENCE.match(line)) or stripped == '---'
            or bool(TABLE_RULE.match(stripped) and set(stripped) <= set('|-: ')))


def content_lines(text):
    """[(1-based number, line)] for every line that can carry a rule."""
    return [(n, l) for n, l in enumerate(split_lines(text), 1) if not is_structural(l)]


def marker_lines(text, kinds=None):
    """1-based line numbers inside a coding-agent-environment marker pair (inclusive); `kinds` limits which."""
    owned, start = set(), {}
    for number, line in enumerate(split_lines(text), 1):
        match = MARKER.search(line)
        if not match or (kinds is not None and match.group(1) not in kinds):
            continue
        if match.group(2) == 'start':
            start[match.group(1)] = number
        elif match.group(1) in start:
            owned.update(range(start.pop(match.group(1)), number + 1))
    return owned


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
        if replacement is not None and disposition != SUPERSEDED:
            raise LedgerError('ledger row %d: only a superseded line may carry a replacement' % number)
        rows[key] = (disposition, citation, replacement)
    return rows


def default_path(home=None):
    local = Path(os.environ.get('CODING_AGENT_LOCAL_DIR')
                 or (Path(home) if home else Path.home()) / '.config/coding-agent-environment/local')
    return Path(os.environ.get('CODING_AGENT_LOCAL_BASELINE_DIR') or local / 'baseline') / 'claude/policy-ledger.tsv'


def check_citations(rows, repo):
    """Every file:line citation must exist in the pinned checkout. Existence only; meaning is a review."""
    bad, root = [], Path(repo).resolve()
    for key, (_, citation, _) in sorted(rows.items()):
        if citation.startswith('owner-decision:'):
            continue
        name, _, number = citation.rpartition(':')
        try:
            target = (root / name).resolve()
            ok = (not name.startswith('/') and '..' not in Path(name).parts and root in target.parents
                  and target.is_file() and 1 <= int(number) <= len(split_lines(target.read_text(encoding='utf-8'))))
        except (OSError, UnicodeDecodeError, ValueError):
            ok = False
        if not ok:
            bad.append(key)
    return bad
