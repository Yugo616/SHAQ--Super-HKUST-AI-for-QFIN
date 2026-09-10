"""Read-only inventory of the already-built diagnostic installer."""
import hashlib
import json
from pathlib import Path
import re
import sys

root = Path(sys.argv[1])
files = []
for path in sorted(root.rglob('*')):
    if not path.is_file() or path.is_symlink():
        continue
    data = path.read_bytes()
    normalized = data.replace(b'\\', b'/').lower()
    contexts = []
    for match in re.finditer(rb'c:/users/runneradmin|d:/a/|c:/a/', normalized):
        contexts.append(normalized[max(0, match.start()-80):match.end()+500].decode('utf-8', 'replace'))
    files.append({'path': path.relative_to(root).as_posix(),
                  'sha256': hashlib.sha256(data).hexdigest(), 'size': len(data),
                  'native': data[:2] == b'MZ', 'contexts': contexts})
Path(sys.argv[2]).write_text(json.dumps({'files': files, 'native_count': sum(f['native'] for f in files)}, ensure_ascii=True), encoding='utf-8')
