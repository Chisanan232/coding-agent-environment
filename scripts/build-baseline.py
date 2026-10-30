#!/usr/bin/env python3
"""Render docs/ENGINEERING-BASELINE.md blocks into the managed spans of the host instruction files.

    python3 scripts/build-baseline.py write   # rewrite the marked regions in place
    python3 scripts/build-baseline.py check   # exit 1 on drift, a missing marker, or a blown size budget

Each block `<!-- block:NAME -->…<!-- /block:NAME -->` in the baseline is copied verbatim between
`<!-- coding-agent-environment:baseline:NAME:start -->` and `…:end -->` in every target file that carries the
markers. Installers are untouched: they copy the managed span, markers included.
"""
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BASELINE = ROOT / "docs" / "ENGINEERING-BASELINE.md"
TARGETS = {
    ROOT / ".claude" / "CLAUDE.md": ("standing-contract", "merge-sequence", "decision-delegation"),
    ROOT / "codex" / "AGENTS.md": ("standing-contract", "merge-sequence", "decision-delegation"),
}
SIZE_BUDGET = 8 * 1024
BLOCK_RE = re.compile(r"<!-- block:([a-z0-9-]+) -->\n(.*?)\n<!-- /block:\1 -->", re.S)


def parse_blocks(text):
    blocks = {}
    for m in BLOCK_RE.finditer(text):
        if m.group(1) in blocks:
            raise ValueError("duplicate block %s" % m.group(1))
        blocks[m.group(1)] = m.group(2)
    return blocks


def marker_re(name):
    start = "<!-- coding-agent-environment:baseline:%s:start -->" % name
    end = "<!-- coding-agent-environment:baseline:%s:end -->" % name
    return start, end, re.compile(re.escape(start) + r"\n(.*?)\n" + re.escape(end), re.S)


def render(text, name, body):
    """Returns (new_text, error). Exactly one marker pair must exist."""
    start, end, rx = marker_re(name)
    if text.count(start) != 1 or text.count(end) != 1 or not rx.search(text):
        return text, "markers for block %s missing or not unique" % name
    return rx.sub(lambda _: start + "\n" + body + "\n" + end, text, count=1), None


def run(mode, baseline=BASELINE, targets=None):
    targets = TARGETS if targets is None else targets
    btext = baseline.read_text()
    blocks = parse_blocks(btext)
    problems = []
    if btext.count("<!-- block:") != len(blocks) or btext.count("<!-- /block:") != len(blocks):
        problems.append("baseline has a block tag that does not parse (name must match [a-z0-9-]+ and each block needs a matching close tag)")
    pending = {}
    size = sum(len(b.encode()) for b in blocks.values())
    if size > SIZE_BUDGET:
        problems.append("baseline blocks are %d bytes, budget is %d" % (size, SIZE_BUDGET))
    for path, names in targets.items():
        text = original = path.read_text()
        for name in names:
            if name not in blocks:
                problems.append("%s: block %s is not defined in the baseline" % (path.name, name))
                continue
            text, err = render(text, name, blocks[name])
            if err:
                problems.append("%s: %s" % (path.name, err))
        if text != original:
            if mode == "write":
                pending[path] = text
            else:
                problems.append("%s: drift from the baseline (run: python3 scripts/build-baseline.py write)" % path)
    if mode == "write" and not problems:  # all-or-nothing: nothing is written while any problem exists
        for path, text in pending.items():
            tmp = path.with_name(path.name + ".baseline-tmp")
            tmp.write_text(text)
            tmp.replace(path)
    return problems


def main(argv):
    if len(argv) != 2 or argv[1] not in ("write", "check"):
        print(__doc__)
        return 2
    problems = run(argv[1])
    for p in problems:
        print("BASELINE: " + p, file=sys.stderr)
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
