"""Token and HTML parity of rustgments against Pygments 2.21.0.

Usage:
  python -I tests/parity.py examplefiles [--pygments-src DIR]
  python -I tests/parity.py snippets     [--pygments-src DIR]
  python -I tests/parity.py corpus       [--corpus DIR]
  python -I tests/parity.py file LEXERCLASS PATH...

Token parity compares get_tokens_unprocessed() on the text that get_tokens()
feeds it (Pygments' own preprocessing), plus the preprocessing itself.
HTML parity compares highlight(code, Lexer(), HtmlFormatter()) byte for byte.
"""

import os
import argparse
import json
import sys
import time
from collections import defaultdict
from pathlib import Path

import pygments
import pygments.lexers
from pygments.formatters.html import HtmlFormatter

import rustgments
from rustgments import _native

NATIVE = ['PythonLexer', 'JsonLexer', 'JavascriptLexer', 'BashLexer', 'RustLexer']
DEFAULT_PYG_SRC = Path(os.environ.get('RUSTGMENTS_PYGMENTS_SRC', 'corpus/pygments'))
DEFAULT_CORPUS = Path(os.environ.get('RUSTGMENTS_CORPUS', 'corpus/real'))
CORPUS_DIRS = {'python': 'PythonLexer', 'json': 'JsonLexer', 'js': 'JavascriptLexer',
               'bash': 'BashLexer', 'rust': 'RustLexer'}


def lexer_class_for_alias(alias):
    try:
        return type(pygments.lexers.get_lexer_by_name(alias)).__name__
    except Exception:
        return None


def first_diff(a, b):
    n = min(len(a), len(b))
    for i in range(n):
        if a[i] != b[i]:
            return i
    return n if len(a) != len(b) else None


def compare_tokens(cls_name, raw):
    """Return (ok, detail) for get_tokens_unprocessed parity on preprocessed text."""
    pyg = getattr(pygments.lexers, cls_name)()
    text = pyg._preprocess_lexer_input(raw)
    ours_pre = _native.preprocess(raw)
    if ours_pre != text:
        i = first_diff(ours_pre, text)
        return False, {'kind': 'preprocess', 'index': i,
                       'ours': ours_pre[max(0, i - 20):i + 20], 'pygments': text[max(0, i - 20):i + 20]}
    t0 = time.perf_counter()
    expected = list(pyg.get_tokens_unprocessed(text))
    t1 = time.perf_counter()
    lx = rustgments.Lexer(cls_name)
    assert lx.backend == 'native', lx.reason
    got = [(i, t, v) for i, t, v in
           ((i, rustgments._token_types()[t], v) for i, t, v in _native.get_tokens_unprocessed(cls_name, text))]
    t2 = time.perf_counter()
    i = first_diff(got, expected)
    if i is None:
        return True, {'ntok': len(got), 't_pyg': t1 - t0, 't_ours': t2 - t1}
    ctx = slice(max(0, i - 2), i + 3)
    return False, {
        'kind': 'tokens', 'index': i, 'ntok_ours': len(got), 'ntok_pygments': len(expected),
        'ours': [(o, str(t), v) for o, t, v in got[ctx]],
        'pygments': [(o, str(t), v) for o, t, v in expected[ctx]],
    }


def compare_html(cls_name, raw):
    expected = pygments.highlight(raw, getattr(pygments.lexers, cls_name)(), HtmlFormatter())
    got = _native.highlight_html(cls_name, raw)
    if got == expected:
        return True, None
    i = first_diff(got, expected)
    return False, {'index': i, 'ours': got[max(0, i - 60):i + 60], 'pygments': expected[max(0, i - 60):i + 60]}


def read_separate(path):
    # tests/conftest.py LexerSeparateTestItem: py.path read_text('utf-8') (text mode)
    with open(path, encoding='utf-8') as f:
        return f.read()


def read_inline(path):
    # tests/conftest.py LexerInlineTestItem
    with open(path, encoding='utf-8') as f:
        content = f.read()
    content, _, _expected = content.partition('\n---tokens---\n')
    if content.startswith('---input---\n'):
        content = '\n' + content
    _comment, _, inp = content.rpartition('\n---input---\n')
    if not inp.endswith('\n'):
        inp += '\n'
    return inp


def read_raw(path):
    """Decode like Pygments does for bytes input (encoding='guess')."""
    data = Path(path).read_bytes()
    try:
        return data.decode('utf-8')
    except UnicodeDecodeError:
        from pygments.util import guess_decode
        text, enc = guess_decode(data)
        NOT_UTF8.append((str(path), enc))
        return text


NOT_UTF8 = []


def collect(kind, args):
    items = []  # (cls_name, label, text)
    if kind == 'examplefiles':
        base = args.pygments_src / 'tests' / 'examplefiles'
        for d in sorted(base.iterdir()):
            if not d.is_dir():
                continue
            cls = lexer_class_for_alias(d.name)
            if cls not in NATIVE:
                continue
            for f in sorted(d.iterdir()):
                if f.suffix == '.output' or f.name == 'conftest.py':
                    continue
                items.append((cls, f'examplefiles/{d.name}/{f.name}', read_separate(f)))
    elif kind == 'snippets':
        base = args.pygments_src / 'tests' / 'snippets'
        for d in sorted(base.iterdir()):
            if not d.is_dir():
                continue
            cls = lexer_class_for_alias(d.name)
            if cls not in NATIVE:
                continue
            for f in sorted(d.glob('*.txt')):
                items.append((cls, f'snippets/{d.name}/{f.name}', read_inline(f)))
    elif kind == 'corpus':
        for sub, cls in CORPUS_DIRS.items():
            d = args.corpus / sub
            if not d.is_dir():
                continue
            for f in sorted(p for p in d.rglob('*') if p.is_file()):
                text = read_raw(f)
                if text is None:
                    items.append((cls, f'corpus/{sub}/{f.relative_to(d).as_posix()}', None))
                else:
                    items.append((cls, f'corpus/{sub}/{f.relative_to(d).as_posix()}', text))
    elif kind == 'file':
        for p in args.paths:
            items.append((args.lexer, p, read_raw(p)))
    return items


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('kind', choices=['examplefiles', 'snippets', 'corpus', 'file'])
    ap.add_argument('lexer', nargs='?')
    ap.add_argument('paths', nargs='*')
    ap.add_argument('--pygments-src', type=Path, default=DEFAULT_PYG_SRC)
    ap.add_argument('--corpus', type=Path, default=DEFAULT_CORPUS)
    ap.add_argument('--no-html', action='store_true')
    ap.add_argument('--only', help='lexer class to restrict to')
    ap.add_argument('--json', type=Path, help='write a result summary here')
    ap.add_argument('--show', type=int, default=5, help='mismatches to print per lexer')
    args = ap.parse_args()
    assert pygments.__version__ == '2.21.0', pygments.__version__

    items = collect(args.kind, args)
    if args.only:
        items = [it for it in items if it[0] == args.only]
    stats = defaultdict(lambda: defaultdict(int))
    fails = defaultdict(list)
    timing = defaultdict(lambda: [0.0, 0.0, 0])
    for cls, label, text in items:
        st = stats[cls]
        st['files'] += 1
        if text is None:
            st['skipped_not_utf8'] += 1
            continue
        st['bytes'] += len(text.encode('utf-8'))
        ok, det = compare_tokens(cls, text)
        if ok:
            st['tokens_ok'] += 1
            timing[cls][0] += det['t_pyg']
            timing[cls][1] += det['t_ours']
            timing[cls][2] += det['ntok']
        else:
            st['tokens_fail'] += 1
            fails[cls].append({'file': label, 'what': 'tokens', **det})
        if not args.no_html:
            hok, hdet = compare_html(cls, text)
            if hok:
                st['html_ok'] += 1
            else:
                st['html_fail'] += 1
                fails[cls].append({'file': label, 'what': 'html', **hdet})

    print(f'== {args.kind} (Pygments {pygments.__version__}, Python {sys.version.split()[0]})')
    for cls in NATIVE:
        if cls not in stats:
            continue
        st = stats[cls]
        tp, to, nt = timing[cls]
        print(f"{cls:16} files={st['files']:5} bytes={st['bytes']:10} "
              f"tokens ok={st['tokens_ok']:5} fail={st['tokens_fail']:4} "
              f"html ok={st['html_ok']:5} fail={st['html_fail']:4} "
              f"not-utf8={st['skipped_not_utf8']}  (lex time pyg {tp:.2f}s ours {to:.2f}s, {nt} tokens)")
        for f in fails[cls][:args.show]:
            print('   MISMATCH', json.dumps(f, ensure_ascii=True)[:1500])
    for p, enc in NOT_UTF8:
        print(f'   note: {p} is not UTF-8; compared after guess_decode ({enc})')
    if args.json:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(json.dumps({'kind': args.kind, 'stats': stats, 'fails': fails},
                                        ensure_ascii=True, indent=1), encoding='utf-8')
    total_fail = sum(st['tokens_fail'] + st['html_fail'] for st in stats.values())
    sys.exit(1 if total_fail else 0)


if __name__ == '__main__':
    main()
