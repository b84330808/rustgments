"""Many highlight() calls in one process, through Pygments' own API.

  python -I bench/many_blocks.py MODE LISTFILE

MODE is "pygments" or "rustgments" (the latter calls rustgments.install()
first; nothing else differs).  LISTFILE has "LexerClass<TAB>path" lines.
Each file is highlighted once with HtmlFormatter(), the way a docs build or
notebook converter calls Pygments.  Prints one JSON line: wall time of the
loop, number of calls, total input bytes, sha256 over all HTML.
"""

import hashlib
import json
import sys
import time

mode, listfile = sys.argv[1:3]
t0 = time.perf_counter()
import pygments
import pygments.lexers
from pygments.formatters.html import HtmlFormatter
if mode == 'rustgments':
    import rustgments
    if not rustgments.install():
        sys.exit('install() refused')
elif mode != 'pygments':
    sys.exit('MODE must be pygments or rustgments')
items = []
for line in open(listfile, encoding='utf-8'):
    if line.strip() and not line.startswith('#'):
        cls, path = line.rstrip('\n').split('\t')
        with open(path, encoding='utf-8', errors='replace', newline='') as f:
            items.append((getattr(pygments.lexers, cls), f.read()))
t1 = time.perf_counter()
h = hashlib.sha256()
nbytes = 0
for cls, code in items:
    html = pygments.highlight(code, cls(), HtmlFormatter())
    h.update(html.encode('utf-8'))
    nbytes += len(code.encode('utf-8'))
t2 = time.perf_counter()
print(json.dumps({'mode': mode, 'calls': len(items), 'bytes': nbytes, 'import_s': t1 - t0,
                  'loop_s': t2 - t1, 'sha': h.hexdigest()[:16]}))
