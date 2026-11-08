"""Drop-in mode parity: Pygments' own API before and after rustgments.install().

  python -I tests/dropin_check.py [--files N] [--seed S]

For real files and mutants of every native lexer, records Pygments' results
with the original code, then calls rustgments.install() and compares:

* highlight() with many HtmlFormatter option sets (the ones Sphinx and
  pymdownx pass, line numbers, hl_lines, anchors, noclasses, ...);
* Sphinx's call shape: lexer with stripnl=False and the raiseonerror filter
  (exception type, message and partial outfile content must match);
* get_tokens(), get_tokens(unfiltered=True), get_tokens_unprocessed(), other
  filters, bytes input;
* lexers that delegate to a native lexer in Python (pycon, NumPyLexer).
"""

import os
import argparse
import io
import random
import sys
from pathlib import Path

import pygments
import pygments.lexers as L
from pygments.formatters.html import HtmlFormatter
from pygments.styles import get_style_by_name

import rustgments

CORPUS = Path(os.environ.get('RUSTGMENTS_CORPUS', 'corpus/real'))
SUBDIRS = {'python': 'PythonLexer', 'js': 'JavascriptLexer', 'bash': 'BashLexer',
           'rust': 'RustLexer', 'json': 'JsonLexer'}

FORMATTER_OPTIONS = [
    {},
    {'style': get_style_by_name('trac'), 'linenos': False},          # Sphinx
    {'style': get_style_by_name('default'), 'linenos': 'inline'},    # Sphinx :linenos:
    {'style': get_style_by_name('default'), 'linenos': 'table', 'linenostart': 7},
    {'hl_lines': [1, 3, 4, 40], 'linenos': False},                   # :emphasize-lines:
    {'hl_lines': [2], 'linenos': 'inline', 'linenostep': 2, 'linenospecial': 3},
    {'cssclass': 'highlight', 'wrapcode': True, 'lineanchors': 'L', 'linenos': 'table',
     'anchorlinenos': True},
    {'linespans': 'line', 'filename': 'a<b>.py'},
    {'nowrap': True},
    {'noclasses': True, 'style': get_style_by_name('monokai')},
    {'noclasses': True, 'hl_lines': [1, 2], 'linenos': 'inline'},
    {'classprefix': 'tok-', 'cssclass': 'code', 'cssstyles': 'x: 1', 'prestyles': 'y: 2'},
    {'debug_token_types': True},
    {'lineseparator': '<br>'},
    {'full': True, 'title': 'T'},
]

# pymdownx.highlight's formatter subclasses (override wrap / _wrap_div /
# _wrap_tablelinenos), with the option set it passes; only if installed.
try:
    from pymdownx.highlight import BlockHtmlFormatter, InlineHtmlFormatter
except ImportError:
    BlockHtmlFormatter = InlineHtmlFormatter = None
PYMDOWNX = [
    (BlockHtmlFormatter, dict(cssclass='highlight', linenos=False, linenostart=1, linenostep=1,
                              linenospecial=-1, style='default', noclasses=False, hl_lines=[],
                              wrapcode=True, filename='', linespans='', lineanchors='',
                              anchorlinenos=False)),
    (BlockHtmlFormatter, dict(cssclass='highlight', linenos='pymdownx-inline', linenostart=3,
                              linenostep=1, linenospecial=-1, style='default', noclasses=False,
                              hl_lines=[2, 5], wrapcode=True, filename='x.py', linespans='__span-0',
                              lineanchors='__codelineno-0', anchorlinenos=True)),
    (BlockHtmlFormatter, dict(cssclass='highlight', linenos='table', linenostart=1, linenostep=2,
                              linenospecial=-1, style='default', noclasses=False, hl_lines=[1],
                              wrapcode=True, filename='', linespans='', lineanchors='',
                              anchorlinenos=False)),
    (InlineHtmlFormatter, dict(cssclass='highlight', linenos=False, style='default', noclasses=False,
                               hl_lines=[], wrapcode=True, filename='')),
] if BlockHtmlFormatter else []

NOISE = ['\u00b2', '\u0663', '\u216b', '\u00e9', '\u4e2d', '\U0001f600', '\u00a0', '\r\n', '\r', '\t',
         '\ufeff', '"', "'", '`', '\\', '{', '}', '$', '#', '/*', '<', '&', '>', '\n\n']


def mutate(rng, text):
    if len(text) > 4000:
        a = rng.randrange(0, len(text) - 3000)
        text = text[a:a + rng.randrange(300, 3000)]
    for _ in range(rng.randrange(0, 8)):
        i = rng.randrange(0, len(text) + 1)
        text = text[:i] + rng.choice(NOISE) + text[i:]
    return text


def sphinx_highlight(cls, code, fmt_opts, outfile=None):
    """Sphinx's highlight_block call shape (minus the retry)."""
    lexer = cls(stripnl=False)
    lexer.add_filter('raiseonerror')
    fmt = HtmlFormatter(**fmt_opts)
    buf = outfile if outfile is not None else io.StringIO()
    try:
        r = pygments.highlight(code, lexer, fmt, buf)
        return ('ok', r, buf.getvalue())
    except Exception as e:  # ErrorToken
        return ('raise', type(e).__name__, str(e), buf.getvalue())


def run_all(cases):
    out = []
    for cls, code in cases:
        res = []
        for opts in FORMATTER_OPTIONS:
            res.append(pygments.highlight(code, cls(), HtmlFormatter(**opts)))
        for fcls, opts in PYMDOWNX:
            res.append(pygments.highlight(code, cls(stripnl=False), fcls(**dict(opts))))
        res.append(sphinx_highlight(cls, code, FORMATTER_OPTIONS[1]))
        res.append(sphinx_highlight(cls, code, FORMATTER_OPTIONS[4]))
        lx = cls()
        res.append(list(lx.get_tokens(code)))
        res.append(list(lx.get_tokens(code, unfiltered=True)))
        res.append(list(lx.get_tokens_unprocessed(lx._preprocess_lexer_input(code))))
        lx2 = cls(stripall=True, tabsize=4, ensurenl=False)
        lx2.add_filter('whitespace', spaces=True)
        lx2.add_filter('keywordcase', case='upper')
        res.append(list(lx2.get_tokens(code)))
        res.append(pygments.highlight(code.encode('utf-8'), cls(), HtmlFormatter()))
        # a partly consumed stream must still format the rest the same way
        ts = cls().get_tokens(code)
        first = next(ts, None)
        buf = io.StringIO()
        HtmlFormatter().format(ts, buf)
        res.append((first, buf.getvalue()))
        out.append(res)
    return out


def delegating_cases(rng):
    texts = ['>>> import os\n>>> os.getcwd()\n\'/x\'\n>>> 1/0\nTraceback (most recent call last):\n'
             '  File "<stdin>", line 1, in <module>\nZeroDivisionError: division by zero\n',
             '>>> def f(x):\n...     return x  # \u00e9\n...\n>>> f(\u00b2)\n']
    out = []
    for t in texts:
        out.append(pygments.highlight(t, L.PythonConsoleLexer(), HtmlFormatter()))
        out.append(pygments.highlight(t, L.NumPyLexer(), HtmlFormatter()))
        out.append(list(L.PythonLexer().get_tokens_unprocessed('x = "a"\n', stack=('root', 'dqs'))))
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--files', type=int, default=40, help='real files per lexer (each also mutated)')
    ap.add_argument('--seed', type=int, default=20261108)
    args = ap.parse_args()
    rng = random.Random(args.seed)
    cases = []
    for sub, name in SUBDIRS.items():
        files = sorted(p for p in (CORPUS / sub).rglob('*') if p.is_file() and p.stat().st_size < 60_000)
        cls = getattr(L, name)
        for p in rng.sample(files, min(args.files, len(files))):
            text = p.read_bytes().decode('utf-8', 'replace')
            cases.append((cls, text))
            cases.append((cls, mutate(rng, text)))
    for t in ('', '\n', 'x', 'plain text\n\n  indented\n', '<b>&amp;</b>\n'):
        cases.append((L.TextLexer, t))
    for t in ('  \n\n', '{"a": [1, 2]}', '"unterminated', '\u00e9\u00b2'):
        for name in SUBDIRS.values():
            cases.append((getattr(L, name), t))
    ref = run_all(cases)
    ref_del = delegating_cases(rng)

    if not rustgments.install():
        print('install() refused (no native tables for this interpreter)')
        sys.exit(1)
    from rustgments._dropin import STATS
    got = run_all(cases)
    got_del = delegating_cases(rng)
    stats = dict(STATS)
    rustgments.uninstall()
    again = run_all(cases[:20])

    fails = 0
    labels = ([f'highlight {o}' for o in FORMATTER_OPTIONS] +
              [f'pymdownx {f.__name__} {o}' for f, o in PYMDOWNX] +
              ['sphinx', 'sphinx hl_lines', 'get_tokens', 'get_tokens unfiltered', 'get_tokens_unprocessed',
               'options+filters', 'bytes input', 'partly consumed'])
    for (cls, code), r, g in zip(cases, ref, got):
        for lab, a, b in zip(labels, r, g):
            if a != b:
                fails += 1
                if fails <= 8:
                    print(f'FAIL {cls.__name__} [{lab}] input={ascii(code[:80])}')
    for i, (a, b) in enumerate(zip(ref_del, got_del)):
        if a != b:
            fails += 1
            print(f'FAIL delegating case {i}')
    if again != ref[:20]:
        fails += 1
        print('FAIL after uninstall()')
    n = len(cases) * len(labels) + len(ref_del) + 1
    print(f'dropin_check Python {sys.version.split()[0]}: {len(cases)} inputs x {len(labels)} call shapes, '
          f'{n} comparisons, {fails} mismatches; paths taken: {stats}')
    sys.exit(1 if fails else 0)


if __name__ == '__main__':
    main()
