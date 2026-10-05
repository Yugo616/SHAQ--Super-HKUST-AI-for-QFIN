"""Offline filing blocks and balanced, source-addressable excerpts.

BeautifulSoup parses frozen HTML, SQLite FTS5 ranks complete blocks. No model,
network, invented XBRL normalization, or return label participates in selection.
Tables split between rows with repeated headers/context, never across columns.
"""
from __future__ import annotations

import csv
from contextlib import closing
import hashlib
import io
import json
import re
import sqlite3
from pathlib import Path


def document_policy(package_root: Path | None = None) -> dict:
    from .app_paths import installed_package_root
    root = package_root or installed_package_root()
    value = json.loads((root / 'config/filing-input.json').read_text(encoding='utf-8'))
    if (value.get('schema_version') != 1 or type(value.get('block_characters')) is not int
            or value['block_characters'] <= 0 or type(value.get('neighbour_blocks')) is not int
            or type(value.get('table_context_rows')) is not int or value['table_context_rows'] < 1
            or not 0 <= value['neighbour_blocks'] <= 2 or not isinstance(value.get('topics'), dict) or not value['topics']
            or not all(isinstance(q, str) and q.strip() for q in value['topics'].values())):
        raise ValueError('Invalid filing input policy')
    return value


def _text(element) -> str:
    return ' '.join(element.get_text(' ', strip=True).split())


def _parts(text: str, limit: int):
    """Split only prose, retaining every normalized character in indexed blocks."""
    start = 0
    while start < len(text):
        end = min(len(text), start + limit)
        if end < len(text):
            boundary = max(text.rfind('. ', start, end), text.rfind('; ', start, end),
                           text.rfind(' ', start, end))
            if boundary > start:
                end = boundary + 1
        yield start, text[start:end]
        start = end


def parse_document(content: bytes, *, source_uri: str, policy: dict) -> dict:
    from bs4 import BeautifulSoup, Comment, NavigableString, Tag
    if content.lstrip().startswith(b'%PDF'):
        raise ValueError('PDF requires a verified PDF parser; cannot decode binary as filing text')
    # Explicit UTF-8 preserves the existing SEC decoding contract; no URL fetch.
    soup = BeautifulSoup(content.decode('utf-8', errors='replace'), 'html.parser')
    for node in list(soup.find_all(True)):
        if node.name is None or node.attrs is None:
            continue
        style = re.sub(r'\s+', '', str(node.get('style', '')).lower())
        if (node.name.lower() in {'script', 'style', 'noscript', 'head', 'ix:hidden', 'ix:header'}
                or node.has_attr('hidden') or 'display:none' in style or 'visibility:hidden' in style):
            node.decompose()
    blocks, section = [], ''
    block_tags = {'p', 'div', 'li', 'blockquote', 'pre', 'h1', 'h2', 'h3', 'h4', 'h5', 'h6', 'table'}
    stack = list(reversed(list((soup.body or soup).children)))

    def add(text, kind, locator, extra=None):
        if not text.strip():
            return
        fragments = [(0, text)] if kind == 'table' else _parts(text, policy['block_characters'])
        for offset, fragment in fragments:
            index = len(blocks)
            blocks.append({'block_id': f'b{index:06d}', 'kind': kind, 'section': section,
                           'locator': locator, 'normalized_text_offset': offset,
                           'text': fragment, **(extra or {})})

    table_number, element_number = 0, 0
    while stack:
        node = stack.pop()
        if isinstance(node, Comment):
            continue
        if isinstance(node, NavigableString):
            add(' '.join(str(node).split()), 'text', f'text:{element_number}')
            element_number += 1
            continue
        if not isinstance(node, Tag):
            continue
        element_number += 1
        locator = f'element:{element_number}'
        if node.get('id'):
            locator += '#' + str(node['id'])
        if node.name == 'table' and not node.find('table'):
            table_number += 1
            rows, spans, header_count = [], [], 0
            for row in node.find_all('tr'):
                cells = row.find_all(['td', 'th'], recursive=False)
                if not cells:
                    continue
                row_number = len(rows)
                if len(rows) == header_count and (row.find('th') or row.find_parent('thead')):
                    header_count += 1
                rows.append([_text(cell) for cell in cells])
                for column, cell in enumerate(cells):
                    if cell.get('colspan', '1') != '1' or cell.get('rowspan', '1') != '1':
                        spans.append({'row': row_number, 'cell': column,
                                      'colspan': str(cell.get('colspan', '1')),
                                      'rowspan': str(cell.get('rowspan', '1')), 'origin_text': _text(cell)})
            if rows:
                caption = _text(node.caption) if node.caption else ''
                context_count = header_count or min(policy['table_context_rows'], len(rows))
                def csv_text(selected_rows):
                    output = io.StringIO()
                    csv.writer(output, lineterminator='\n').writerows(selected_rows)
                    return output.getvalue()
                # No inferred column grid for merged cells. Original spans retain origin text.
                descriptor = 'original table cells in row order; repeated ' + (
                    'HTML header rows' if header_count else 'leading context rows (not inferred headers)')
                prefix = f'{caption}\n{descriptor}\n' + csv_text(rows[:context_count])
                if spans:
                    prefix += 'Merged cells retain original row order; span geometry is in the source archive.\n'
                start = context_count
                if start == len(rows):
                    add(prefix, 'table', locator + f'/table:{table_number}',
                        {'table_number': table_number, 'table_row_range': [0, len(rows)], 'merged_cells': spans})
                while start < len(rows):
                    end, size = start, len(prefix)
                    while end < len(rows):
                        row_size = len(csv_text([rows[end]]))
                        if end > start and size + row_size > policy['block_characters']:
                            break
                        end += 1
                        size += row_size
                    add(prefix + csv_text(rows[start:end]), 'table',
                        locator + f'/table:{table_number}/rows:{start}-{end}',
                        {'table_number': table_number, 'table_row_range': [start, end],
                         'merged_cells': [s for s in spans if s['row'] < context_count or start <= s['row'] < end]})
                    start = end
                continue
        if node.name in block_tags and node.name != 'table' and not node.find(block_tags):
            text = _text(node)
            if node.name.startswith('h') and node.name[1:].isdigit():
                section = text
            elif node.find(['b', 'strong']) and _text(node.find(['b', 'strong'])) == text:
                section = text
            add(text, 'heading' if section == text else 'text', locator)
        else:
            stack.extend(reversed(list(node.children)))
    raw_hash = hashlib.sha256(content).hexdigest()
    return {'schema_version': 1, 'processor': policy['processor'], 'source_uri': source_uri,
            'raw_sha256': raw_hash, 'blocks': blocks,
            'normalized_characters': sum(len(b['text']) for b in blocks)}


def _block_render(block: dict) -> str:
    heading = f" · {block['section']}" if block['section'] else ''
    return f"[{block['block_id']}{heading}]\n{block['text']}"


def select_documents(documents: list[dict], *, maximum_characters: int, policy: dict) -> list[dict]:
    """One budget for a filing AND its exhibits, not one budget per attachment."""
    if type(maximum_characters) is not int or maximum_characters <= 0:
        raise ValueError('Filing text budget must be a positive integer')
    rows = [(doc_index, block_index, block) for doc_index, document in enumerate(documents)
            for block_index, block in enumerate(document['blocks'])]
    selected, used, rankings = set(), 0, []
    costs = [len(_block_render(block)) + 2 for _, _, block in rows]
    if sum(costs) <= maximum_characters:
        selected = set(range(len(rows)))
    elif rows:
        with closing(sqlite3.connect(':memory:')) as connection:
            connection.execute('CREATE VIRTUAL TABLE blocks USING fts5(section, text)')
            connection.executemany('INSERT INTO blocks(rowid,section,text) VALUES(?,?,?)',
                [(i + 1, b['section'], b['text']) for i, (_, _, b) in enumerate(rows)])
            for query in policy['topics'].values():
                rankings.append([r[0] - 1 for r in connection.execute(
                    'SELECT rowid FROM blocks WHERE blocks MATCH ? ORDER BY bm25(blocks),rowid', (query,))])
        # Topic round-robin prevents positive performance matches crowding out risks.
        candidates = []
        for rank in range(max(map(len, rankings), default=0)):
            candidates.extend(result[rank] for result in rankings if rank < len(result))
        # With no topic hit, spread fallback coverage across document positions, not a prefix.
        queue = [(0, len(rows))]
        for lo, hi in queue:
            if lo >= hi:
                continue
            mid = (lo + hi) // 2
            candidates.append(mid)
            if lo < mid:
                queue.append((lo, mid))
            if mid + 1 < hi:
                queue.append((mid + 1, hi))
        for index in candidates:
            if index in selected or costs[index] > maximum_characters - used:
                continue
            selected.add(index)
            used += costs[index]
            # Nearby text contains units, qualifications and table footnotes.
            for neighbour in range(max(0, index-policy['neighbour_blocks']),
                                   min(len(rows), index+policy['neighbour_blocks']+1)):
                if (neighbour not in selected and rows[neighbour][0] == rows[index][0]
                        and costs[neighbour] <= maximum_characters-used):
                    selected.add(neighbour)
                    used += costs[neighbour]
    result = []
    for doc_index, document in enumerate(documents):
        kept = [block for i, (d, _, block) in enumerate(rows) if d == doc_index and i in selected]
        oversized = [b['block_id'] for d, _, b in rows if d == doc_index
                     and len(_block_render(b)) + 2 > maximum_characters]
        result.append({'document_text': '\n\n'.join(_block_render(b) for b in kept),
                       'document_coverage': {
                           'processor': policy['processor'], 'raw_sha256': document['raw_sha256'],
                           'policy_sha256': hashlib.sha256(json.dumps(policy, sort_keys=True).encode()).hexdigest(),
                           'total_blocks': len(document['blocks']), 'selected_blocks': len(kept),
                           'omitted_blocks': len(document['blocks']) - len(kept),
                           'complete_document': len(kept) == len(document['blocks']),
                           'oversized_atomic_blocks': oversized,
                           'selection': 'full_text' if len(kept) == len(document['blocks']) else 'balanced_topic_bm25',
                           'references': [{'block_id': b['block_id'], 'locator': b['locator']} for b in kept],
                           'limitation': 'Selected original excerpts, not a summary or proof of absence in omitted text.'}})
    return result
