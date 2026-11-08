"""One Sphinx HTML build, with plain Pygments or with rustgments.install().

  python -I bench/docs_build.py MODE SRCDIR OUTDIR DOCTREEDIR [sphinx args...]

MODE is "pygments" or "rustgments"; the two differ only in the install()
call.  With RUSTGMENTS_MEASURE=1 the runner also times every
PygmentsBridge.highlight_block call (for the breakdown, not for timed runs).
Prints one JSON line on stdout.
"""

import json
import os
import sys
import time

mode, src, out, doctrees = (os.path.abspath(a) if i else a for i, a in enumerate(sys.argv[1:5]))
extra = sys.argv[5:]
# run from the docs directory, as `make html` does (some conf.py files and
# extensions use relative paths)
os.chdir(src)
t0 = time.perf_counter()
# A docs tree that must import its own checkout of a package (the Pygments
# docs read tests/examplefiles next to the imported pygments) names it here.
if os.environ.get('RUSTGMENTS_DOCS_SYSPATH'):
    sys.path.insert(0, os.environ['RUSTGMENTS_DOCS_SYSPATH'])
installed = None
if mode == 'rustgments':
    import rustgments
    installed = rustgments.install()
    if not installed:
        sys.exit('rustgments.install() refused')
elif mode != 'pygments':
    sys.exit('MODE must be pygments or rustgments')

measure = os.environ.get('RUSTGMENTS_MEASURE') == '1'
hl = {'blocks': 0, 'seconds': 0.0, 'by_lang': {}}
if measure:
    from sphinx.highlighting import PygmentsBridge
    orig = PygmentsBridge.highlight_block

    def timed(self, source, lang, *a, **kw):
        t = time.perf_counter()
        try:
            return orig(self, source, lang, *a, **kw)
        finally:
            dt = time.perf_counter() - t
            hl['blocks'] += 1
            hl['seconds'] += dt
            b = hl['by_lang'].setdefault(lang, [0, 0.0, 0])
            b[0] += 1
            b[1] += dt
            b[2] += len(source)
    PygmentsBridge.highlight_block = timed

    # which HtmlFormatter options and lexer options the build passes
    from collections import Counter
    from pygments.formatters.html import HtmlFormatter
    from pygments.lexer import Lexer
    fmt_opts = Counter()
    lex_opts = Counter()
    orig_fmt_init = HtmlFormatter.__init__
    orig_lex_init = Lexer.__init__

    def fmt_init(self, **options):
        fmt_opts[repr(sorted((k, v if k != 'style' else 'STYLE')
                             for k, v in options.items()))] += 1
        orig_fmt_init(self, **options)

    def lex_init(self, **options):
        lex_opts[repr((type(self).__name__, sorted(options.items())))] += 1
        orig_lex_init(self, **options)
    HtmlFormatter.__init__ = fmt_init
    Lexer.__init__ = lex_init
    hl['formatter_options'] = fmt_opts
    hl['lexer_options'] = lex_opts

from sphinx.cmd.build import build_main

rc = build_main(['-b', 'html', '-E', '-a', '-q', '-d', doctrees, src, out] + extra)
res = {'mode': mode, 'rc': rc, 'seconds': time.perf_counter() - t0}
if measure:
    res['highlight'] = hl
if installed:
    from rustgments._dropin import STATS
    res['rustgments_paths'] = dict(STATS)
print(json.dumps(res))
sys.exit(rc)
