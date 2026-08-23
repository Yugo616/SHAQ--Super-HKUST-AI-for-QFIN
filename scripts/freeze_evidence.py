#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from daily_oracle_v6.lineage import build_lineage_graph  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="Verify files and freeze a V6 evidence lineage graph")
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--evidence-root", type=Path, required=True)
    parser.add_argument("--cutoff-et", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    manifest = json.loads(args.manifest.read_text(encoding="utf-8"))
    records = manifest["evidence"] if isinstance(manifest, dict) else manifest
    graph = build_lineage_graph(records, args.evidence_root, args.cutoff_et)
    if args.output.exists():
        raise FileExistsError("evidence lineage is immutable")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(graph, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"froze {len(graph['records'])} evidence records into {graph['independent_root_count']} roots")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
