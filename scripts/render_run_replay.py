#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from shaq_daily_oracle.replay import write_run_replay  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Render one verified SHAQ Daily Oracle run as a standalone HTML replay."
    )
    parser.add_argument("--runtime", type=Path, required=True)
    args = parser.parse_args()
    runtime = args.runtime.resolve()
    frozen_path = runtime / "frozen_run.json"
    if not frozen_path.is_file():
        parser.error(f"missing frozen run: {frozen_path}")
    frozen = json.loads(frozen_path.read_text(encoding="utf-8"))
    manifest = write_run_replay(runtime=runtime, frozen=frozen)
    print(json.dumps({
        "output": str(runtime / "run_replay.html"),
        "status": manifest["status"],
        "sha256": manifest["run_replay_sha256"],
    }, ensure_ascii=False, sort_keys=True))
    return 0 if manifest["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
