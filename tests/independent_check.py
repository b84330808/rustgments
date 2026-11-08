"""Independent parity check of rustgments against Pygments, via public APIs only.

Corpus: local real files (not the subagent's corpus) + random mutants.
Usage: python -I tests/independent_check.py OUTDIR [--per-lang N] [--mutants N] [--seed S]
"""
import argparse
import json
import os
import random
import sys
import threading
import time
from pathlib import Path

import pygments
import pygments.lexers
from pygments.formatters.html import HtmlFormatter as PygHtml

import rustgments

LANGS = {
    'python': 'PythonLexer',
    'json': 'JsonLexer',
    'javascript': 'JavascriptLexer',
    'bash': 'BashLexer',
    'rust': 'RustLexer',
}

PYLIB = Path(os.environ.get('RUSTGMENTS_PYLIB', Path(os.__file__).parent))
CARGO_SRC = Path(os.environ.get('RUSTGMENTS_CARGO_SRC', Path.home() / '.cargo' / 'registry' / 'src'))
# JS/JSON: a node_modules tree, for example from
#   npm install --ignore-scripts eslint@9 webpack@5 @babel/core@7 express@4 jest@29
NODE_MODULES = Path(os.environ.get('RUSTGMENTS_NODE_MODULES', 'corpus/npm/node_modules'))
# Bash: a Git for Windows install has plenty of real shell scripts.
GIT = Path(os.environ.get('RUSTGMENTS_GIT_ROOT', 'C:/Program Files/Git'))

state = {'cur': None, 'since': time.time()}


def watchdog():
    while True:
        time.sleep(30)
        if state['cur'] and time.time() - state['since'] > 120:
            print(f'SLOW >120s on {state["cur"]}', flush=True)


def read(p):
    try:
        b = Path(p).read_bytes()
    except OSError:
        return None
    if b'\x00' in b[:4096]:
        return None
    try:
        return b.decode('utf-8')
    except UnicodeDecodeError:
        return b.decode('utf-8', errors='replace')


def sample_dirs(root, k, rng):
    try:
        subs = [d for d in root.iterdir() if d.is_dir()]
    except OSError:
        return []
    rng.shuffle(subs)
    return subs[:k]


def files_under(d, exts, limit, rng):
    out = []
    for dp, dn, fn in os.walk(d):
        dn[:] = [x for x in dn if x not in ('.git', 'node_modules')]
        for f in fn:
            if f.endswith(exts):
                out.append(Path(dp) / f)
    rng.shuffle(out)
    return out[:limit]


def collect(per_lang, rng):
    c = {}
    py = [p for p in PYLIB.rglob('*.py') if 'site-packages' not in p.parts]
    rng.shuffle(py)
    big = sorted(py, key=lambda p: p.stat().st_size, reverse=True)[:40]
    c['python'] = list(dict.fromkeys(big + py[:per_lang]))

    rs = []
    for reg in CARGO_SRC.iterdir():
        for crate in sample_dirs(reg, 200, rng):
            rs += files_under(crate, ('.rs',), 4, rng)
    rng.shuffle(rs)
    c['rust'] = rs[:per_lang]

    js, js_json = [], []
    pkgs = []
    for d in NODE_MODULES.iterdir():
        if d.name.startswith('@') and d.is_dir():
            pkgs += [x for x in d.iterdir() if x.is_dir()]
        elif d.is_dir() and not d.name.startswith('.'):
            pkgs.append(d)
    rng.shuffle(pkgs)
    for pkg in pkgs[:400]:
        js += files_under(pkg, ('.js', '.mjs', '.cjs'), 3, rng)
        js_json += files_under(pkg, ('.json',), 2, rng)
    rng.shuffle(js)
    rng.shuffle(js_json)
    bigjs = sorted(js, key=lambda p: p.stat().st_size, reverse=True)[:30]
    c['javascript'] = list(dict.fromkeys(bigjs + js[:per_lang]))
    c['json'] = js_json[:per_lang]

    sh = list((GIT / 'mingw64/share/git/completion').glob('*.bash')) + \
        list((GIT / 'mingw64/share/git/completion').glob('*.sh')) + list((GIT / 'etc/profile.d').glob('*.sh'))
    for d in (GIT / 'mingw64/libexec/git-core', GIT / 'usr/bin', GIT / 'usr/share', GIT / 'usr/lib'):
        for dp, dn, fn in os.walk(d):
            for f in fn:
                p = Path(dp) / f
                if f.endswith(('.dll', '.exe')):
                    continue
                if f.endswith(('.sh', '.bash')):
                    sh.append(p)
                    continue
                try:
                    with open(p, 'rb') as fh:
                        head = fh.read(40)
                except OSError:
                    continue
                if head.startswith(b'#!') and (b'/sh' in head.split(b'\n')[0] or b'bash' in head.split(b'\n')[0]):
                    sh.append(p)
    c['bash'] = list(dict.fromkeys(sh))[:per_lang * 2]
    return c


NOISE = ['\r', '\r\n', '\t', '\x00', '\x0c', '\x0b', '\u00a0', '\u3000', '\u2028', '\u0301', '\U0001F600',
         '\ud7ff', '\\', '"', "'", '`', '${', '$(', '#', '/*', '*/', '//', '<<EOF\n', 'r#"', '"#', '{', '}',
         '[', ']', '(', ')', '\n', ' ', '٣', 'é', '中文', 'ß', 'İ', 'ﬁ', 'Ⅻ', '0x', '1e', '.5', '_', '@', '%',
         'f"', 'b\'', 'u8"', '\\u', '\\x', '/', '=~', ';;', 'fn ', 'def ', 'class ', 'let ', 'function ']


def mutant(src, rng):
    if not src:
        return rng.choice(NOISE)
    parts = []
    for _ in range(rng.randint(1, 4)):
        a = rng.randrange(len(src))
        b = min(len(src), a + rng.randint(0, 600))
        parts.append(src[a:b])
        for _ in range(rng.randint(0, 6)):
            parts.append(rng.choice(NOISE))
    rng.shuffle(parts)
    return ''.join(parts)


def check(alias, cls, text):
    rl = rustgments.get_lexer_by_name(alias)
    if rl.backend != 'native':
        return 'not-native', rl.reason
    pl = getattr(pygments.lexers, cls)()
    exp_tok = list(pl.get_tokens(text))
    got_tok = list(rl.get_tokens(text))
    if exp_tok != got_tok:
        n = min(len(exp_tok), len(got_tok))
        i = next((k for k in range(n) if exp_tok[k] != got_tok[k]), n)
        return 'tokens', {'i': i, 'exp': [(str(t), v) for t, v in exp_tok[max(0, i-2):i+3]],
                          'got': [(str(t), v) for t, v in got_tok[max(0, i-2):i+3]]}
    pre = pl._preprocess_lexer_input(text)
    exp_u = list(pl.get_tokens_unprocessed(pre))
    got_u = list(rl.get_tokens_unprocessed(pre))
    if exp_u != got_u:
        n = min(len(exp_u), len(got_u))
        i = next((k for k in range(n) if exp_u[k] != got_u[k]), n)
        return 'offsets', {'i': i, 'exp': [(o, str(t), v) for o, t, v in exp_u[max(0, i-2):i+3]],
                           'got': [(o, str(t), v) for o, t, v in got_u[max(0, i-2):i+3]]}
    exp_h = pygments.highlight(text, getattr(pygments.lexers, cls)(), PygHtml())
    got_h = rustgments.highlight(text, rustgments.get_lexer_by_name(alias), rustgments.HtmlFormatter())
    if exp_h != got_h:
        n = min(len(exp_h), len(got_h))
        i = next((k for k in range(n) if exp_h[k] != got_h[k]), n)
        return 'html', {'i': i, 'exp': exp_h[max(0, i-80):i+80], 'got': got_h[max(0, i-80):i+80]}
    return 'ok', len(exp_tok)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('out', type=Path)
    ap.add_argument('--per-lang', type=int, default=500)
    ap.add_argument('--mutants', type=int, default=400)
    ap.add_argument('--seed', type=int, default=20261108)
    a = ap.parse_args()
    a.out.mkdir(parents=True, exist_ok=True)
    rng = random.Random(a.seed)
    threading.Thread(target=watchdog, daemon=True).start()
    print('pygments', pygments.__version__, 'native set check:',
          {al: rustgments.get_lexer_by_name(al).backend for al in LANGS}, flush=True)
    corpus = collect(a.per_lang, rng)
    summary = {}
    fails = []
    for alias, cls in LANGS.items():
        files = corpus[alias]
        texts = []
        cnt = {'ok': 0, 'fail': 0, 'bytes': 0, 'files': 0, 'mutants_ok': 0, 'mutants_fail': 0}
        t0 = time.time()
        for p in files:
            text = read(p)
            if text is None:
                continue
            texts.append(text)
            state['cur'], state['since'] = str(p), time.time()
            kind, det = check(alias, cls, text)
            cnt['files'] += 1
            cnt['bytes'] += len(text.encode('utf-8', 'surrogatepass'))
            if kind == 'ok':
                cnt['ok'] += 1
            else:
                cnt['fail'] += 1
                fails.append({'lang': alias, 'file': str(p), 'kind': kind, 'detail': det})
        for m in range(a.mutants if texts else 0):
            text = mutant(rng.choice(texts), rng)
            state['cur'], state['since'] = f'{alias} mutant {m}', time.time()
            kind, det = check(alias, cls, text)
            if kind == 'ok':
                cnt['mutants_ok'] += 1
            else:
                cnt['mutants_fail'] += 1
                (a.out / f'mutant-{alias}-{m}.txt').write_text(text, encoding='utf-8', errors='surrogatepass', newline='')
                fails.append({'lang': alias, 'file': f'mutant-{alias}-{m}.txt', 'kind': kind, 'detail': det})
        cnt['secs'] = round(time.time() - t0, 1)
        summary[alias] = cnt
        print(alias, cnt, flush=True)
    state['cur'] = None
    (a.out / 'summary.json').write_text(json.dumps({'summary': summary, 'fails': fails}, ensure_ascii=False, indent=1),
                                       encoding='utf-8')
    print('FAILS', len(fails), flush=True)
    for f in fails[:15]:
        print(json.dumps(f, ensure_ascii=False)[:700], flush=True)
    print('DONE', flush=True)
    sys.exit(1 if fails else 0)


if __name__ == '__main__':
    main()
