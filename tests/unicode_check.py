"""Parity on the characters whose Python classes differ between Unicode versions.

  python -I tests/unicode_check.py [--cases N] [--seed S]

Reads the variants in ir/pygments_ir.json, collects every code point whose
membership in some symbolic class (\\w, \\d, \\s, case-insensitive sets) or in
Python's \\w / \\s tables differs between any two variants, and splices such
characters into real code: after identifier characters, inside numbers, at
random positions.  Each case is compared with the running interpreter's
Pygments (tokens with offsets, preprocessing, HTML).

On the matching variant every case must agree.  With RUSTGMENTS_FORCE_PYTHON
set to another variant's Python version (a test hook), mismatches show that
the cases do reach the version-dependent classes.
"""

import os
import argparse
import json
import random
import sys
from pathlib import Path

import pygments
import pygments.lexers
from pygments.formatters.html import HtmlFormatter

import rustgments
from rustgments import _native

ROOT = Path(__file__).resolve().parent.parent
CORPUS = Path(os.environ.get('RUSTGMENTS_CORPUS', 'corpus/real'))
SUBDIRS = {'python': 'PythonLexer', 'js': 'JavascriptLexer', 'bash': 'BashLexer',
           'rust': 'RustLexer', 'json': 'JsonLexer'}


def parse_class(text):
    """Code point set of a class text as emitted by build_ir/extract.py."""
    if text == '(?s:.)':
        return None  # everything
    neg = False
    if text.startswith('[^'):
        neg, body = True, text[2:-1]
    elif text.startswith('['):
        body = text[1:-1]
    else:
        body = text
    cps = []
    i = 0

    def one(i):
        if body[i] == '\\':
            j = body.index('}', i)
            return int(body[i + 3:j], 16), j + 1
        return ord(body[i]), i + 1
    while i < len(body):
        lo, i = one(i)
        hi = lo
        if i < len(body) and body[i] == '-':
            hi, i = one(i + 1)
        cps.append((lo, hi))
    return neg, cps


def members(text):
    p = parse_class(text)
    if p is None:
        return set(range(0x110000))
    neg, ranges = p
    s = set()
    for lo, hi in ranges:
        s.update(range(lo, hi + 1))
    if neg:
        return set(range(0x110000)) - s
    return s


def load_ir():
    """The IR with the variant class tables read back from the bulk file."""
    ir = json.loads((ROOT / 'ir' / 'pygments_ir.json').read_text(encoding='ascii'))
    bulk = (ROOT / 'ir' / 'pygments_ir.bulk').read_text(encoding='ascii')
    for v in ir['variants']:
        a, n = v['classes_at']
        v['classes'] = json.loads(bulk[a:a + n])
    return ir


def changed_code_points(ir):
    variants = ir['variants']
    changed = set()
    for idx, key in enumerate(ir['class_keys']):
        if not key:
            continue
        texts = {next(t for j, t, _ in v['classes'] if j == idx) for v in variants}
        if len(texts) > 1:
            sets = [members(t) for t in texts]
            union = set().union(*sets)
            inter = set.intersection(*sets)
            changed |= union - inter
    for field in ('py_word', 'py_space'):
        sets = []
        for v in variants:
            s = set()
            for lo, hi in v[field]:
                s.update(range(lo, hi + 1))
            sets.append(s)
        changed |= set().union(*sets) - set.intersection(*sets)
    return sorted(c for c in changed if not 0xD800 <= c <= 0xDFFF)


def mutate(rng, text, pool):
    n = len(text)
    if n > 3000:
        a = rng.randrange(0, n - 2000)
        text = text[a:a + rng.randrange(200, 2000)]
    for _ in range(rng.randrange(1, 10)):
        ch = chr(rng.choice(pool))
        r = rng.random()
        if r < 0.5:
            # right after an identifier character or digit: exercises \w, \d, \b
            spots = [k + 1 for k, c in enumerate(text) if c.isalnum() or c == '_']
            i = rng.choice(spots) if spots else rng.randrange(0, len(text) + 1)
        else:
            i = rng.randrange(0, len(text) + 1)
        text = text[:i] + ch * rng.randrange(1, 3) + text[i:]
    return text


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--cases', type=int, default=300, help='per lexer')
    ap.add_argument('--seed', type=int, default=20261108)
    args = ap.parse_args()
    ir = load_ir()
    pool = changed_code_points(ir)
    tag = '%d.%d' % sys.version_info[:2]
    print(f'Python {sys.version.split()[0]} Pygments {pygments.__version__} variant {rustgments._INFO["variant_pythons"]} '
          f'Unicode {rustgments._INFO["unicode_version"]} ok={rustgments._VERSION_OK}; '
          f'{len(pool)} version-dependent code points', flush=True)
    rng = random.Random(args.seed)
    fmt = HtmlFormatter()
    total_fail = 0
    for sub, cls in SUBDIRS.items():
        files = sorted(p for p in (CORPUS / sub).rglob('*') if p.is_file() and p.stat().st_size < 400_000)
        pyg = getattr(pygments.lexers, cls)()
        ok = fail = 0
        shown = 0
        for _ in range(args.cases):
            src = rng.choice(files).read_bytes().decode('utf-8', 'replace')
            text = mutate(rng, src, pool)
            pre = pyg._preprocess_lexer_input(text)
            exp = [(i, str(t), v) for i, t, v in pyg.get_tokens_unprocessed(pre)]
            got = [(i, str(rustgments._token_types()[t]), v)
                   for i, t, v in _native.get_tokens_unprocessed(cls, pre)]
            html_ok = _native.highlight_html(cls, text) == pygments.highlight(text, pyg, fmt)
            if exp == got and html_ok and _native.preprocess(text) == pre:
                ok += 1
            else:
                fail += 1
                if shown < 2:
                    shown += 1
                    k = next((k for k in range(min(len(exp), len(got))) if exp[k] != got[k]),
                             min(len(exp), len(got)))
                    print(f'  FAIL {cls}: tokens_equal={exp == got} html_equal={html_ok}')
                    print('    pygments:', ascii(exp[max(0, k - 1):k + 2]))
                    print('    ours    :', ascii(got[max(0, k - 1):k + 2]))
        total_fail += fail
        print(f'{cls:16} cases={args.cases} ok={ok} fail={fail}', flush=True)
    print(f'unicode_check Python {tag}: {"PASS" if not total_fail else "FAIL"} ({total_fail} failing cases)')
    sys.exit(1 if total_fail else 0)


if __name__ == '__main__':
    main()
