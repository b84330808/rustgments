"""Mutation stress test: random slices of real files with hostile characters
spliced in, compared token-for-token and HTML-for-HTML with Pygments.

  python -I tests/stress.py [--cases N] [--seed S]

The inserted characters target the engine's tricky paths: characters where
Python's \\w and the regex crate's \\w disagree (exact word-boundary mode),
non-ASCII bytes (ASCII-tier fallback), CR/CRLF/BOM (preprocessing), quotes and
brackets (unbalanced states), and newlines (Error/Whitespace recovery).
"""

import os
import argparse
import random
import sys
from pathlib import Path

import pygments
import pygments.lexers
from pygments.formatters.html import HtmlFormatter

import rustgments
from rustgments import _native

CORPUS = Path(os.environ.get('RUSTGMENTS_CORPUS', 'corpus/real'))
SUBDIRS = {'python': 'PythonLexer', 'js': 'JavascriptLexer', 'bash': 'BashLexer',
           'rust': 'RustLexer', 'json': 'JsonLexer'}

HOSTILE = [
    '\u0301',  # combining acute: \w in the regex crate, not in Python
    '\u00b2',  # superscript two: \w in Python (isnumeric), not in the regex crate
    '\u203f',  # undertie (Pc): regex crate \w only
    '\u200d',  # zero width joiner: regex crate \w only
    '\u2167',  # roman numeral eight (Nl)
    '\u00e9', '\u4e2d', '\U0001f600', '\u0660',  # e-acute, CJK, emoji, Arabic-Indic zero
    '\u00a0', '\u2028', '\x1c', '\x0b', '\x0c',  # odd whitespace
    '\r', '\r\n', '\n', '\t', '\ufeff',
    '"', "'", '`', '\\', '{', '}', '(', ')', '[', ']', '$', '#', '/', '*',
    '"""', "'''", '<<EOF\n', '${', '$((', '/*', '//', 'r#"', '0x', '1e', '.', '_',
]


def files_for(sub):
    d = CORPUS / sub
    fs = sorted(p for p in d.rglob('*') if p.is_file() and p.stat().st_size < 400_000)
    return fs


def mutate(rng, text):
    n = len(text)
    if n > 3000:
        a = rng.randrange(0, n - 2000)
        text = text[a:a + rng.randrange(200, 2000)]
    for _ in range(rng.randrange(1, 12)):
        i = rng.randrange(0, len(text) + 1)
        text = text[:i] + rng.choice(HOSTILE) + text[i:]
    if rng.random() < 0.2:
        text = '\n' * rng.randrange(1, 3) + text
    if rng.random() < 0.2:
        text = text.rstrip('\n')
    return text


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--cases', type=int, default=400, help='per lexer')
    ap.add_argument('--seed', type=int, default=20261108)
    args = ap.parse_args()
    rng = random.Random(args.seed)
    fmt = HtmlFormatter()
    total_fail = 0
    for sub, cls in SUBDIRS.items():
        files = files_for(sub)
        pyg = getattr(pygments.lexers, cls)()
        ok = fail = 0
        shown = 0
        for _ in range(args.cases):
            src = rng.choice(files).read_bytes().decode('utf-8', 'replace')
            text = mutate(rng, src)
            pre = pyg._preprocess_lexer_input(text)
            exp = [(i, str(t), v) for i, t, v in pyg.get_tokens_unprocessed(pre)]
            got = [(i, str(rustgments._token_types()[t]), v) for i, t, v in _native.get_tokens_unprocessed(cls, pre)]
            html_ok = _native.highlight_html(cls, text) == pygments.highlight(text, pyg, fmt)
            pre_ok = _native.preprocess(text) == pre
            if exp == got and html_ok and pre_ok:
                ok += 1
            else:
                fail += 1
                if shown < 3:
                    shown += 1
                    k = next((k for k in range(min(len(exp), len(got))) if exp[k] != got[k]),
                             min(len(exp), len(got)))
                    print(f'  FAIL {cls}: tokens_equal={exp == got} html_equal={html_ok} pre_equal={pre_ok}')
                    print('    pygments:', exp[max(0, k - 2):k + 3])
                    print('    ours    :', got[max(0, k - 2):k + 3])
                    print('    input   :', repr(pre[:300]))
        total_fail += fail
        print(f'{cls:16} cases={args.cases} ok={ok} fail={fail}')
    sys.exit(1 if total_fail else 0)


if __name__ == '__main__':
    main()
