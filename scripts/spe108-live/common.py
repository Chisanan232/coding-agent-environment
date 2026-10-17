"""Shared helpers for the SPE-108 operator scripts (kept in one file so the operator reviews one place)."""
import re

SPAN_START = '<!-- coding-agent-environment:engineering-workflow:start -->'
SPAN_END = '<!-- coding-agent-environment:engineering-workflow:end -->'


def split_sections(lines):
    """[(title, start, end)] for each '## ' heading outside code fences; end is exclusive."""
    heads, fenced = [], False
    for index, line in enumerate(lines):
        if line.lstrip().startswith('```'):
            fenced = not fenced
        elif not fenced:
            match = re.match(r'^## (.+?)\s*$', line)
            if match:
                heads.append((index, match.group(1)))
    return [(title, start, heads[n + 1][0] if n + 1 < len(heads) else len(lines))
            for n, (start, title) in enumerate(heads)]


def managed_ranges(lines):
    """Line index ranges [start, end] covered by managed-span or private-overlay marker pairs (inclusive)."""
    ranges = []
    for kind in ('engineering-workflow', 'private-overlay'):
        begin = end = None
        for index, line in enumerate(lines):
            if 'coding-agent-environment:%s:start' % kind in line:
                begin = index
            elif 'coding-agent-environment:%s:end' % kind in line and begin is not None:
                end = index
        if begin is not None and end is not None:
            ranges.append((begin, end))
    return ranges


def is_marker_line(line):
    return 'coding-agent-environment:' in line and line.lstrip().startswith('<!--')
