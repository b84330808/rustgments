"""Run extract.py under every supported interpreter, then merge the results.

  python -I build_ir/build_all.py [--candidates | --core-only] PYTHON [PYTHON ...]

Each PYTHON is an interpreter (or venv python.exe) with Pygments 2.21.0
installed.  The merged IR goes to ir/pygments_ir.json; the per-interpreter
parts go to a temporary directory.
"""

import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
OUT = HERE.parent / 'ir' / 'pygments_ir.json'


def main():
    flags = [a for a in sys.argv[1:] if a.startswith('--')]
    pythons = [str(Path(p).resolve()) for p in sys.argv[1:] if not p.startswith('--')]
    if not pythons:
        sys.exit(__doc__)
    with tempfile.TemporaryDirectory() as tmp:
        parts = []
        for py in pythons:
            tag = subprocess.check_output([py, '-I', '-c', 'import sys; print("%d.%d" % sys.version_info[:2])'],
                                          text=True).strip()
            part = Path(tmp) / f'py{tag}.json'
            print(f'== extract with Python {tag}: {py}', flush=True)
            subprocess.check_call([py, '-I', str(HERE / 'extract.py'), '--out', str(part)] + flags)
            parts.append(str(part))
        subprocess.check_call([sys.executable, '-I', str(HERE / 'merge_variants.py'), str(OUT)] + parts)


if __name__ == '__main__':
    main()
