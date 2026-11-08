"""Combine per-interpreter IRs (extract.py --out) into one IR with variants.

  python -I build_ir/merge_variants.py OUT IR [IR ...]

Every field except `variants` must be identical across the inputs: the lexer
tables, pattern text (with class placeholders), token types and class keys do
not depend on the Python version.  Only the members of symbolic classes and
the \\w / \\s tables do, and those live in the variants.  Inputs whose variant
content is identical are merged into one variant listing all their Python
versions.
"""

import json
import sys
from pathlib import Path


def variant_body(v):
    return json.dumps([v['unicode_version'], v['py_word'], v['py_space'], v['classes']])


def main():
    out = Path(sys.argv[1])
    paths = [Path(p) for p in sys.argv[2:]]
    if not paths:
        sys.exit(__doc__)
    irs = [json.loads(p.read_text(encoding='ascii')) for p in paths]
    base = irs[0]
    for p, ir in zip(paths, irs):
        if set(ir) != set(base):
            sys.exit(f'{p}: different top-level fields')
        for k in base:
            if k != 'variants' and ir[k] != base[k]:
                sys.exit(f'{p}: field {k!r} differs from {paths[0]}; the tables depend on the '
                         f'Python version beyond the symbolic classes')
        if len(ir['variants']) != 1:
            sys.exit(f'{p}: expected exactly one variant (an extract.py output)')
    merged = {}
    for ir in irs:
        v = ir['variants'][0]
        key = variant_body(v)
        if key in merged:
            m = merged[key]
            m['pythons'] += v['pythons']
            m['python_versions'] += v['python_versions']
        else:
            merged[key] = dict(v)
    variants = sorted(merged.values(), key=lambda v: tuple(int(x) for x in v['pythons'][0].split('.')))
    seen = set()
    for v in variants:
        dup = seen.intersection(v['pythons'])
        if dup:
            sys.exit(f'Python {sorted(dup)} appears in two variants')
        seen.update(v['pythons'])
    # The ASCII twins feed the prebuilt DFAs; report whether they agree.
    twins = {json.dumps([[i, a] for i, _, a in v['classes']]) for v in variants}
    result = dict(base)
    result['variants'] = variants
    # Move the bulky parts to OUT.bulk (concatenated JSON texts) and leave
    # [offset, length] pairs behind: the engine then parses only the small
    # index at load time and each lexer's states on first use.
    bulk = []
    size = 0

    def put(obj):
        nonlocal size
        text = json.dumps(obj, ensure_ascii=True, separators=(',', ':'))
        bulk.append(text)
        size += len(text)
        return [size - len(text), len(text)]
    result['lexers'] = {n: dict(i) for n, i in base['lexers'].items()}
    for info in result['lexers'].values():
        if 'states' in info:
            info['states_at'] = put(info.pop('states'))
    result['variants'] = [dict(v) for v in variants]
    for v in result['variants']:
        v['classes_at'] = put(v.pop('classes'))
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(json.dumps(result, ensure_ascii=True, separators=(',', ':')).encode('ascii'))
    out.with_suffix('.bulk').write_bytes(''.join(bulk).encode('ascii'))
    nsym = sum(1 for k in base['class_keys'] if k)
    print(f'{len(irs)} inputs, {len(variants)} variants, {nsym} symbolic classes, '
          f'ASCII twins identical across variants: {len(twins) == 1}')
    first = variants[0]
    for v in variants:
        ndiff = sum(1 for a, b in zip(v['classes'], first['classes']) if a[1] != b[1])
        print(f"  Python {','.join(v['pythons']):10} Unicode {v['unicode_version']:7} "
              f"classes differing from the first variant: {ndiff}")
    print('wrote', out, out.stat().st_size, 'bytes +', out.with_suffix('.bulk').stat().st_size, 'bytes bulk')


if __name__ == '__main__':
    main()
