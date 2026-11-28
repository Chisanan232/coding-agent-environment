"""Inventory portable HTML specimens for a B14 source review without executing them."""

import argparse
from collections import Counter
from hashlib import sha256
from html.parser import HTMLParser
import json
from pathlib import Path
import re
from urllib.parse import urlsplit


class Structure(HTMLParser):
    def __init__(self):
        super().__init__()
        self.ids = []
        self.idrefs = []
        self.tags = Counter()
        self.assets = set()
        self.inline_handlers = 0
        self.styles = []
        self.scripts = []
        self.block = ''

    def handle_starttag(self, tag, attrs):
        self.tags[tag] += 1
        self.block = tag if tag in ('style', 'script') else ''
        for key, value in attrs:
            if value is None:
                continue
            if key == 'id':
                self.ids.append(value)
            elif key in ('aria-controls', 'aria-labelledby', 'aria-describedby', 'for'):
                self.idrefs.extend(value.split())
            elif key in ('src', 'href', 'poster') and not value.startswith('#'):
                self.assets.add(value)
            elif key.lower().startswith('on'):
                self.inline_handlers += 1

    def handle_endtag(self, tag):
        if self.block == tag:
            self.block = ''

    def handle_data(self, data):
        if self.block == 'style':
            self.styles.append(data)
        if self.block == 'script':
            self.scripts.append(data)


def audit(path):
    raw = path.read_bytes()
    doc = raw.decode('utf-8')
    parser = Structure()
    parser.feed(doc)
    parser.close()
    css = '\n'.join(parser.styles)
    assets = set(parser.assets)
    assets.update(match.group(2) for match in re.finditer(r'''url\(\s*(['"]?)(.*?)\1\s*\)''', css))
    ids = Counter(parser.ids)
    return {
        'sha256': sha256(raw).hexdigest(),
        'bytes': len(raw),
        'landmarks': {name: parser.tags[name] for name in ('main', 'nav', 'header', 'footer')},
        'controls': {name: parser.tags[name] for name in ('button', 'input', 'select', 'a')},
        'inlineCssBytes': len(css.encode()),
        'inlineJsBytes': len('\n'.join(parser.scripts).encode()),
        'inlineEventAttributes': parser.inline_handlers,
        'duplicateIds': sorted(k for k, count in ids.items() if count > 1),
        'unresolvedIdReferences': sorted(set(parser.idrefs) - ids.keys()),
        'localAssets': sorted(a for a in assets if not urlsplit(a).scheme and not urlsplit(a).netloc),
        'externalAssetCount': sum(bool(urlsplit(a).scheme or urlsplit(a).netloc) for a in assets),
        'declaresReducedMotion': 'prefers-reduced-motion' in css,
    }


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('html', nargs='+', type=Path)
    inputs = parser.parse_args().html
    print(json.dumps([audit(path) for path in inputs], indent=2))
