from __future__ import annotations

import argparse
import json
import os
from datetime import date
from pathlib import Path

from .workflow import Workflow


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="daily-oracle", description="Daily Oracle V6 single entrypoint")
    commands = parser.add_subparsers(dest="command", required=True)
    run = commands.add_parser("run", help="run or resume one six-domain session")
    run.add_argument("--mode", choices=("paper", "shadow"), default="shadow")
    run.add_argument("--session-date", type=date.fromisoformat)
    run.add_argument("--runtime-root", type=Path)
    run.add_argument("--no-wait", action="store_true", help=argparse.SUPPRESS)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    package_root = Path(__file__).resolve().parents[2]
    workflow = Workflow(
        package_root=package_root,
        runtime_root=args.runtime_root,
        host=os.environ.get("FUTU_OPEND_HOST"),
        port=int(os.environ.get("FUTU_OPEND_PORT", "11111")),
    )
    try:
        runtime = workflow.run(
            requested_mode=args.mode, session_date=args.session_date, wait=not args.no_wait
        )
    except Exception as exc:
        runtime = workflow.failure_record(exc)
        print(json.dumps({
            "status": "fail_closed", "error_type": type(exc).__name__,
            "runtime": str(runtime), "professor_report": str(runtime / "professor_report.html"),
        }, sort_keys=True))
        return 2
    frozen = json.loads((runtime / "frozen_run.json").read_text(encoding="utf-8"))
    print(json.dumps({
        "run_id": frozen["run_id"],
        "mode": frozen["mode"],
        "prediction_count": len(frozen["predictions"]),
        "runtime": str(runtime),
        "professor_report": str(runtime / "professor_report.html"),
    }, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
