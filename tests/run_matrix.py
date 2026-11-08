"""Run the parity suite under several interpreters (two at a time).

  python -I tests/run_matrix.py OUTDIR PYTHON [PYTHON ...]

Each PYTHON needs Pygments 2.21.0 and rustgments importable (a wheel, or a
.pth file pointing at python/ next to the built abi3 _native module).  Logs go
to OUTDIR/<tag>/<step>.log; a summary prints at the end.
"""

import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
STEPS = [
    ('examplefiles', ['tests/parity.py', 'examplefiles']),
    ('snippets', ['tests/parity.py', 'snippets']),
    ('corpus', ['tests/parity.py', 'corpus']),
    ('stress', ['tests/stress.py', '--cases', '400']),
    ('unicode', ['tests/unicode_check.py', '--cases', '300']),
    ('independent', ['tests/independent_check.py', '{out}/independent']),
    ('api', ['tests/test_api.py']),
    ('dropin', ['tests/dropin_check.py', '--files', '40']),
]


def run_one(py, out):
    tag = subprocess.check_output([py, '-I', '-c', 'import sys; print("%d.%d" % sys.version_info[:2])'],
                                  text=True).strip()
    d = out / tag
    d.mkdir(parents=True, exist_ok=True)
    res = []
    for name, cmd in STEPS:
        cmd = [c.replace('{out}', str(d)) for c in cmd]
        t0 = time.time()
        with open(d / f'{name}.log', 'w', encoding='utf-8') as log:
            rc = subprocess.call([py, '-I'] + cmd, cwd=ROOT, stdout=log, stderr=subprocess.STDOUT)
        res.append((name, rc, time.time() - t0))
        print(f'[{tag}] {name}: rc={rc} {time.time() - t0:.0f}s', flush=True)
    return tag, res


def main():
    out = Path(sys.argv[1]).resolve()
    pythons = [str(Path(p).resolve()) for p in sys.argv[2:]]
    with ThreadPoolExecutor(max_workers=2) as ex:
        results = list(ex.map(lambda p: run_one(p, out), pythons))
    bad = 0
    for tag, res in results:
        line = ' '.join(f'{n}={"ok" if rc == 0 else "FAIL"}' for n, rc, _ in res)
        bad += sum(1 for _, rc, _ in res if rc)
        print(f'Python {tag}: {line}')
    sys.exit(1 if bad else 0)


if __name__ == '__main__':
    main()
