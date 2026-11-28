"""Emit one benchmark prompt. Guidance arguments must be locally reviewed files."""

import argparse
import json
from pathlib import Path

root = Path(__file__).resolve().parent
corpus = json.loads((root / 'corpus.json').read_text())
parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('task', choices=corpus['tasks'])
parser.add_argument('guidance', nargs='*', type=Path, help='pinned primary followed by an optional recipe')
args = parser.parse_args()
print(corpus['shared'])
print(corpus['tasks'][args.task]['brief'])
print('The reference asset available to the final HTML as reference.svg is:')
print((root / 'reference.svg').read_text())
for path in args.guidance:
    print('Use the following design guidance within the task and resource constraints.')
    print(path.read_text())
