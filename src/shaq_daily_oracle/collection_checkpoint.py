"""Per-request daily-bar recovery; never a replacement for fresh intraday quotes."""
from pathlib import Path
import json

from .hashing import sha256_payload
from .settings import _atomic_json


class HistoryCheckpoint:
    def __init__(self, root, request):
        self.request = request
        self.path = Path(root) / (sha256_payload(request) + '.json') if root else None

    def read(self):
        if self.path is None:
            return None
        try:
            saved = json.loads(self.path.read_text(encoding='utf-8'))
            rows = saved['rows']
            if (saved['request'] == self.request and isinstance(rows, list) and rows
                    and saved['rows_sha256'] == sha256_payload(rows)):
                return rows
        except (OSError, ValueError, KeyError, TypeError):
            pass
        # Damaged or incomplete checkpoints are re-fetched, never accepted as data.
        return None

    def save(self, rows):
        if self.path is not None and rows:
            _atomic_json(self.path, {'request': self.request, 'rows': rows,
                                    'rows_sha256': sha256_payload(rows)})
