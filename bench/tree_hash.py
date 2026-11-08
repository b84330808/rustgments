"""Hash every file of an output tree.

  python -I bench/tree_hash.py DIR MANIFEST

Writes MANIFEST (relative path, size, sha256 per line, sorted) and prints
one JSON line: file count, total bytes, and the sha256 of the manifest.
"""

import hashlib
import json
import os
import sys
from pathlib import Path

root = Path(sys.argv[1])
rows = []
total = 0
for dp, _, fn in os.walk(root):
    for f in fn:
        p = Path(dp) / f
        data = p.read_bytes()
        total += len(data)
        rows.append(f'{p.relative_to(root).as_posix()}\t{len(data)}\t{hashlib.sha256(data).hexdigest()}')
rows.sort()
text = '\n'.join(rows) + '\n'
Path(sys.argv[2]).write_text(text, encoding='utf-8')
print(json.dumps({'files': len(rows), 'bytes': total,
                  'tree_sha256': hashlib.sha256(text.encode('utf-8')).hexdigest()}))
