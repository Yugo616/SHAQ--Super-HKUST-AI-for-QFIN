from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path
from typing import Any


def file_revision(path: Path) -> tuple:
    """Display-cache identity; Windows ctime is creation, not change time."""
    stat = path.stat()
    stamp = (str(path), stat.st_size, stat.st_mtime_ns,
             stat.st_ctime_ns, stat.st_ino, stat.st_dev)
    # A restored mtime can hide a rewrite on Windows. Hash bytes there rather
    # than trusting creation time; keep POSIX's cheaper metadata-change check.
    return (*stamp, sha256_file(path)) if sys.platform == 'win32' else stamp


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def sha256_payload(value: Any) -> str:
    encoded = json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()
