"""One benchmark process: highlight one file with Pygments or rustgments.

  python -I bench/bench_one.py IMPL LEXERCLASS FILE [WARM_REPS]

IMPL is "pygments" or "rustgments".  Prints one JSON line:
  first_s      the first highlight() call in a fresh process (includes lexer
               module import / table processing for Pygments, regex
               compilation for rustgments)
  warm_*_s     WARM_REPS further highlight() calls in the same process
  sha          sha256 of the HTML, to check both sides produce the same bytes
The PowerShell driver measures the whole process wall time around this.
"""

import hashlib
import json
import sys
import time

impl, cls, path = sys.argv[1:4]
reps = int(sys.argv[4]) if len(sys.argv) > 4 else 5

with open(path, encoding='utf-8', newline='') as f:
    code = f.read()

if impl == 'pygments':
    import pygments
    import pygments.lexers
    from pygments.formatters.html import HtmlFormatter

    def run():
        return pygments.highlight(code, getattr(pygments.lexers, cls)(), HtmlFormatter())
    backend = 'pygments'
elif impl == 'rustgments':
    import rustgments

    def run():
        return rustgments.highlight(code, rustgments.Lexer(cls), rustgments.HtmlFormatter())
    backend = rustgments.Lexer(cls).backend
else:
    sys.exit('impl must be pygments or rustgments')

t0 = time.perf_counter()
out = run()
first = time.perf_counter() - t0
warm = []
for _ in range(reps):
    t0 = time.perf_counter()
    run()
    warm.append(time.perf_counter() - t0)
warm.sort()
print(json.dumps({
    'impl': impl, 'backend': backend, 'lexer': cls, 'bytes': len(code.encode('utf-8')),
    'first_s': first, 'warm_median_s': warm[len(warm) // 2] if warm else None,
    'warm_min_s': warm[0] if warm else None, 'warm_max_s': warm[-1] if warm else None,
    'sha': hashlib.sha256(out.encode('utf-8')).hexdigest()[:16],
}))
