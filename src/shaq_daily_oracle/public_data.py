"""Public-source adapters and incremental daily-bar storage."""
from __future__ import annotations
import csv
import io
import json
from datetime import datetime, timedelta
from html.parser import HTMLParser
from pathlib import Path
from zoneinfo import ZoneInfo
import httpx
from .hashing import sha256_payload
from .settings import _atomic_json


class DailyBarCache:
    def __init__(self, provider, root: Path, *, overlap_days: int):
        self.provider, self.root, self.overlap = provider, root, overlap_days

    def __getattr__(self, name):
        return getattr(self.provider, name)

    def history(self, symbols, *, start, end, interval="1d", prepost=False):
        if interval != "1d" or prepost:
            return self.provider.history(symbols, start=start, end=end, interval=interval, prepost=prepost)
        output, groups = {}, {}
        for symbol in symbols:
            path = self.root / (sha256_payload(symbol) + ".json")
            cached = json.loads(path.read_text()) if path.exists() else {}
            rows = cached.get("rows", [])
            recent = max((str(r.get("timestamp", ""))[:10] for r in rows), default="")
            beginning = start
            if recent and cached.get("start", "9999") <= start.isoformat():
                beginning = max(start, datetime.fromisoformat(recent).date() - timedelta(days=self.overlap))
            groups.setdefault(beginning, []).append(symbol)
            output[symbol] = rows
        for beginning, group in groups.items():
            fresh = self.provider.history(group, start=beginning, end=end, interval="1d")
            for symbol in group:
                rows = fresh.get(symbol, [])
                if not rows:
                    # A failed provider update must not silently present old bars as fresh.
                    output[symbol] = []
                    continue
                merged = {str(r["timestamp"]): r for r in output[symbol]}
                merged.update({str(r["timestamp"]): r for r in rows})
                full = [merged[k] for k in sorted(merged)]
                _atomic_json(self.root / (sha256_payload(symbol)+".json"), {"start": min(start.isoformat(), full[0]["timestamp"][:10]), "rows": full})
                output[symbol] = [r for r in full if start.isoformat() <= r["timestamp"][:10] < end.isoformat()]
        return output


class TableText(HTMLParser):
    def __init__(self):
        super().__init__(); self.depth = 0; self.parts = []
    def handle_starttag(self, tag, attrs):
        if tag == 'table': self.depth += 1
        if self.depth and tag in {'tr', 'td', 'th'}: self.parts.append(' | ')
    def handle_endtag(self, tag):
        if tag == 'table': self.depth = max(0, self.depth-1)
    def handle_data(self, data):
        if self.depth and data.strip(): self.parts.append(data.strip())


def earnings_expectations(rows):
    allowed = {'symbol', 'name', 'date', 'time', 'epsForecast', 'noOfEsts', 'fiscalQuarterEnding'}
    return [{key: value for key, value in row.items() if key in allowed} for row in rows]


def collect_public_context(config, *, cutoff):
    packets = []
    for name, source in config['sources'].items():
        if not source.get('enabled'):
            packets.append({'provider': name, 'status': 'not_enabled', 'source_uri': source['url']})
            continue
        try:
            response = httpx.get(source['url'], params={'date': cutoff.date().isoformat()} if name == 'nasdaq_earnings' else None,
                                 timeout=config['timeout_seconds'], follow_redirects=True,
                                 headers={'User-Agent': 'SHAQ Daily Oracle Research', 'Accept': 'application/json,text/csv,text/html'})
            response.raise_for_status()
            captured = datetime.now(ZoneInfo('America/New_York'))
            if source['kind'] == 'csv':
                rows = list(csv.DictReader(io.StringIO(response.text)))
                rows = [r for r in rows if datetime.strptime(r['DATE'], '%m/%d/%Y').date() < cutoff.date()]
                data = rows[-30:]
            elif source['kind'] == 'html':
                parser = TableText(); parser.feed(response.text); data = ''.join(parser.parts)
                if not data: raise ValueError('没有找到利率数据表')
            else:
                data = earnings_expectations((response.json().get('data') or {}).get('rows') or [])
            packets.append({'provider': name, 'source_uri': source['url'], 'captured_at': captured.isoformat(),
                            'status': 'collected' if captured <= cutoff else 'after_cutoff', 'data': data,
                            'raw': response.content})
        except Exception as exc:
            packets.append({'provider': name, 'source_uri': source['url'], 'status': 'provider_error', 'error': str(exc)})
    return packets
