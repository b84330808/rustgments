"""Parity gate: which lexers may run natively.

  python -I tests/gate.py run OUT.json [--pygments-src DIR] [--mutants N] [--only NAME ...]
  python -I tests/gate.py combine build_ir/native_gate.json GATE.json [GATE.json ...]

`run` takes every lexer that the IR in ir/pygments_ir.json marks native
(build it with `build_ir/extract.py --candidates`), and compares rustgments
with Pygments on the lexer's own Pygments test inputs (tests/examplefiles and
tests/snippets of the pinned Pygments checkout) plus N generated mutants of
them: preprocessing, get_tokens_unprocessed (offsets, types, values) and
highlight() HTML.  The first mismatch, an engine error, no test input at all,
or a timeout keeps the lexer on Pygments.  Lexers run in two worker
processes; a worker that exceeds the time limit is killed and replaced.

`combine` merges the results of several interpreters: a lexer passes only if
it passed on every one of them.  Lexers whose tables do not fit the IR keep
the extractor's reason.  `summary` counts the lexers by reason bucket.

  python -I tests/gate.py summary build_ir/native_gate.json
"""

import os
import argparse
import json
import queue
import random
import subprocess
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_PYG_SRC = Path(os.environ.get('RUSTGMENTS_PYGMENTS_SRC', 'corpus/pygments'))
LEXER_TIMEOUT = 300  # seconds per lexer, all its inputs together

NOISE = ['\u00b2', '\u0663', '\u216b', '\u00e9', '\u4e2d', '\U0001f600', '\u00a0', '\u0301', '\u3000',
         '\r\n', '\r', '\t', '\x0c', '\ufeff', '"', "'", '`', '\\', '{', '}', '(', ')', '[', ']', '<', '>',
         '$', '#', '%', ';', '/*', '*/', '//', '--', '<!--', '\n', '\n\n', ' ', '0x1F', '1e-3', '_', '@']


# ---------------------------------------------------------------------------
# worker

def read_separate(path):
    with open(path, encoding='utf-8') as f:
        return f.read()


def read_inline(path):
    with open(path, encoding='utf-8') as f:
        content = f.read()
    content, _, _ = content.partition('\n---tokens---\n')
    if content.startswith('---input---\n'):
        content = '\n' + content
    _, _, inp = content.rpartition('\n---input---\n')
    if not inp.endswith('\n'):
        inp += '\n'
    return inp


def mutate(rng, text):
    if len(text) > 6000:
        a = rng.randrange(0, len(text) - 4000)
        text = text[a:a + rng.randrange(500, 4000)]
    for _ in range(rng.randrange(1, 10)):
        i = rng.randrange(0, len(text) + 1)
        text = text[:i] + rng.choice(NOISE) + text[i:]
    if rng.random() < 0.3 and len(text) > 2:
        # cut in the middle of something
        text = text[:rng.randrange(1, len(text))]
    return text


def check_one(name, inputs, mutants, seed):
    import pygments
    import pygments.lexers
    from pygments.formatters.html import HtmlFormatter
    import rustgments
    from rustgments import _native
    tts = rustgments._token_types()
    cls = getattr(pygments.lexers, name)
    texts = []
    for kind, path in inputs:
        try:
            texts.append((path, read_inline(path) if kind == 'snippet' else read_separate(path)))
        except UnicodeDecodeError:
            continue
    if not texts:
        return 'gate: no Pygments test input decodes as UTF-8', 0
    rng = random.Random(f'{seed}-{name}')
    base = [t for _, t in texts]
    for k in range(mutants):
        texts.append((f'mutant {k}', mutate(rng, rng.choice(base))))
    fmt = HtmlFormatter()
    n = 0
    for label, text in texts:
        n += 1
        lab = Path(label).name if not label.startswith('mutant') else label
        try:
            pyg = cls()
            pre = pyg._preprocess_lexer_input(text)
            if _native.preprocess(text) != pre:
                return f'gate: preprocess mismatch ({lab})', n
            exp = list(pyg.get_tokens_unprocessed(pre))
        except Exception as e:  # Pygments itself fails on this input: not a parity question
            return f'gate: Pygments raised {type(e).__name__} ({lab})', n
        try:
            got = [(i, tts[t], v) for i, t, v in _native.get_tokens_unprocessed(name, pre)]
            html = _native.highlight_html(name, text)
        except Exception as e:
            msg = str(e).splitlines()[0][:160] if str(e) else type(e).__name__
            return f'gate: engine error ({lab}): {msg}', n
        if got != exp:
            return f'gate: token mismatch ({lab})', n
        if html != pygments.highlight(text, cls(), fmt):
            return f'gate: HTML mismatch ({lab})', n
    return 'pass', n


def worker():
    for line in sys.stdin:
        task = json.loads(line)
        t0 = time.time()
        try:
            verdict, n = check_one(task['name'], task['inputs'], task['mutants'], task['seed'])
        except Exception as e:
            verdict, n = f'gate: worker error {type(e).__name__}: {e}'[:200], 0
        print(json.dumps({'name': task['name'], 'verdict': verdict, 'inputs': n,
                          'seconds': round(time.time() - t0, 2)}), flush=True)


# ---------------------------------------------------------------------------
# orchestrator

def lexer_inputs(pyg_src):
    import pygments.lexers
    out = {}

    def cls_of(alias):
        try:
            return type(pygments.lexers.get_lexer_by_name(alias)).__name__
        except Exception:
            return None
    for d in sorted((pyg_src / 'tests' / 'examplefiles').iterdir()):
        if d.is_dir():
            c = cls_of(d.name)
            if c:
                for f in sorted(d.iterdir()):
                    if f.is_file() and f.suffix != '.output' and f.name != 'conftest.py':
                        out.setdefault(c, []).append(('example', str(f)))
    for d in sorted((pyg_src / 'tests' / 'snippets').iterdir()):
        if d.is_dir():
            c = cls_of(d.name)
            if c:
                for f in sorted(d.glob('*.txt')):
                    out.setdefault(c, []).append(('snippet', str(f)))
    return out


class Worker:
    def __init__(self):
        self.p = subprocess.Popen([sys.executable, '-I', __file__, 'worker'], cwd=ROOT,
                                  stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                  text=True, encoding='utf-8', bufsize=1)
        self.q = queue.Queue()
        threading.Thread(target=self._read, daemon=True).start()

    def _read(self):
        for line in self.p.stdout:
            self.q.put(line)
        self.q.put(None)

    def run(self, task, timeout):
        self.p.stdin.write(json.dumps(task) + '\n')
        self.p.stdin.flush()
        try:
            line = self.q.get(timeout=timeout)
        except queue.Empty:
            return None
        return json.loads(line) if line else None

    def kill(self):
        self.p.kill()


def run(args):
    import pygments
    ir = json.loads((ROOT / 'ir' / 'pygments_ir.json').read_text(encoding='ascii'))
    inputs = lexer_inputs(args.pygments_src)
    verdicts = {}
    todo = []
    for name, info in ir['lexers'].items():
        if info['backend'] == 'native':
            if args.only and name not in args.only:
                continue
            if name not in inputs:
                verdicts[name] = {'verdict': 'gate: no Pygments test inputs', 'inputs': 0}
            else:
                todo.append(name)
        elif info['backend'] == 'fallback':
            verdicts[name] = {'verdict': 'tables: ' + info.get('reason', '?')}
    tasks = queue.Queue()
    for name in todo:
        tasks.put(name)
    lock = threading.Lock()
    t_start = time.time()

    def drive():
        w = Worker()
        while True:
            try:
                name = tasks.get_nowait()
            except queue.Empty:
                break
            task = {'name': name, 'inputs': inputs[name], 'mutants': args.mutants, 'seed': args.seed}
            res = w.run(task, LEXER_TIMEOUT)
            if res is None:
                w.kill()
                w = Worker()
                res = {'name': name, 'verdict': f'gate: timeout (> {LEXER_TIMEOUT} s) or worker crash',
                       'inputs': None}
            with lock:
                verdicts[name] = {k: v for k, v in res.items() if k != 'name'}
                done = sum(1 for n in todo if n in verdicts)
                if res['verdict'] != 'pass' or done % 25 == 0:
                    print(f'[{done}/{len(todo)} {time.time() - t_start:.0f}s] {name}: {res["verdict"]}',
                          flush=True)
        w.kill()
    threads = [threading.Thread(target=drive) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    tag = '%d.%d' % sys.version_info[:2]
    out = {'pygments_version': pygments.__version__, 'python': sys.version.split()[0], 'python_tag': tag,
           'mutants': args.mutants, 'seed': args.seed, 'lexers': dict(sorted(verdicts.items()))}
    args.out.write_text(json.dumps(out, indent=1, ensure_ascii=True) + '\n', encoding='utf-8')
    npass = sum(1 for v in verdicts.values() if v['verdict'] == 'pass')
    print(f'Python {tag}: {len(todo)} candidates gated, {npass} pass; wrote {args.out}')


def combine(args):
    gates = [json.loads(p.read_text(encoding='utf-8')) for p in args.inputs]
    names = sorted(set().union(*(g['lexers'] for g in gates)))
    res = {}
    for n in names:
        verdict = 'pass'
        for g in gates:
            v = g['lexers'].get(n, {'verdict': 'gate: not run'})['verdict']
            if v != 'pass':
                verdict = v if v.startswith('tables: ') else f'{v} [Python {g["python_tag"]}]'
                break
        res[n] = verdict
    out = {'pygments_version': gates[0]['pygments_version'],
           'pythons': [g['python'] for g in gates], 'mutants': gates[0]['mutants'],
           'lexers': res}
    args.out.write_text(json.dumps(out, indent=1, ensure_ascii=True) + '\n', encoding='utf-8')
    print(f'{sum(1 for v in res.values() if v == "pass")} of {len(res)} lexers pass on '
          f'{", ".join(out["pythons"])}; wrote {args.out}')


BUCKETS = [
    ('pass', r'^pass$'),
    ('tables: not a RegexLexer (hand-written lexer)', r'^tables: not a RegexLexer'),
    ('tables: ExtendedRegexLexer (context callbacks)', r'^tables: ExtendedRegexLexer'),
    ('tables: overrides get_tokens_unprocessed / get_tokens / preprocessing',
     r'^tables: overrides (get_tokens|_preprocess)'),
    ('tables: overrides __init__ (lexer options)', r'^tables: overrides __init__'),
    ('tables: custom callback function in a rule', r'^tables: callback'),
    ('tables: using() with lexer options', r'^tables: using\(\.\.\.\) with lexer options'),
    ('tables: using() a lexer outside the mapping', r'^tables: using\(.*\) delegates'),
    ('tables: delegates (using) to a lexer that is not native', r'(^tables: )?using\(.*\) and that lexer'),
    ('tables: nested bygroups', r'^tables: bygroups nested'),
    ('tables: case-insensitive backreference', r'^tables: case-insensitive backreference'),
    ('tables: other regex/state construct', r'^tables: '),
    ('gate: no Pygments test inputs', r'^gate: no Pygments test input'),
    ('gate: token mismatch', r'^gate: token mismatch'),
    ('gate: HTML mismatch', r'^gate: HTML mismatch'),
    ('gate: preprocess mismatch', r'^gate: preprocess mismatch'),
    ('gate: engine error (pattern the regex engines reject or a limit)', r'^gate: engine error'),
    ('gate: Pygments itself raised', r'^gate: Pygments raised'),
    ('gate: timeout or worker crash', r'^gate: timeout'),
    ('other', r''),
]


def summary(args):
    import re
    from collections import Counter, defaultdict
    g = json.loads(args.gate.read_text(encoding='utf-8'))
    if 'ir_version' in g:
        # an IR: native/handwritten count as pass, fallback lexers carry the
        # gate's reason or the extractor's demotion reason
        g = {'pygments_version': g['pygments_version'],
             'pythons': sorted({p for v in g['variants'] for p in v['python_versions']}),
             'lexers': {n: 'pass' if i['backend'] != 'fallback' else i.get('reason', '?')
                        for n, i in g['lexers'].items()}}
    c = Counter()
    examples = defaultdict(list)
    for name, v in g['lexers'].items():
        for label, rx in BUCKETS:
            if re.search(rx, v):
                c[label] += 1
                examples[label].append(name)
                break
    print(f"{len(g['lexers'])} lexers (Pygments {g['pygments_version']}, gated on {', '.join(g['pythons'])})")
    for label, _ in BUCKETS:
        if c[label]:
            print(f'{c[label]:5}  {label}   e.g. {", ".join(examples[label][:4])}')


def main():
    if len(sys.argv) > 1 and sys.argv[1] == 'worker':
        return worker()
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest='cmd', required=True)
    r = sub.add_parser('run')
    r.add_argument('out', type=Path)
    r.add_argument('--pygments-src', type=Path, default=DEFAULT_PYG_SRC)
    r.add_argument('--mutants', type=int, default=30)
    r.add_argument('--seed', type=int, default=20261108)
    r.add_argument('--only', nargs='*')
    c = sub.add_parser('combine')
    c.add_argument('out', type=Path)
    c.add_argument('inputs', type=Path, nargs='+')
    m = sub.add_parser('summary')
    m.add_argument('gate', type=Path)
    args = ap.parse_args()
    {'run': run, 'combine': combine, 'summary': summary}[args.cmd](args)


if __name__ == '__main__':
    main()
