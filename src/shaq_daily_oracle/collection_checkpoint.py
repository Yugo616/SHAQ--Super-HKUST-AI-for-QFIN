"""Per-request daily-bar recovery; never a replacement for fresh intraday quotes."""
from pathlib import Path
import json
import math

from .hashing import sha256_payload
from .settings import _atomic_json


def _valid_prices(rows):
    if not isinstance(rows, list) or not rows:
        return False
    try:
        return all(isinstance(row, dict) and isinstance(row.get('timestamp'), str)
                   and math.isfinite(float(row.get('close'))) and float(row['close']) > 0
                   for row in rows)
    except (TypeError, ValueError):
        return False


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
            if (saved['request'] == self.request and _valid_prices(rows)
                    and saved['rows_sha256'] == sha256_payload(rows)):
                return rows
        except (OSError, ValueError, KeyError, TypeError):
            pass
        # Damaged or incomplete checkpoints are re-fetched, never accepted as data.
        return None

    def save(self, rows):
        if self.path is not None and _valid_prices(rows):
            _atomic_json(self.path, {'request': self.request, 'rows': rows,
                                    'rows_sha256': sha256_payload(rows)})
