from __future__ import annotations

import argparse
import json
import os
from datetime import date, datetime, time as clock_time
from pathlib import Path
from zoneinfo import ZoneInfo

from .workflow import Workflow
from .campaign import CampaignConfig, run_campaign, write_campaign_views
from .postmortem_runner import PostmortemRunner
from .batch_review_runner import BatchReviewRunner


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="daily-oracle", description="SHAQ Daily Oracle single entrypoint")
    commands = parser.add_subparsers(dest="command", required=True)
    run = commands.add_parser("run", help="run or resume one six-domain session")
    run.add_argument("--mode", choices=("paper", "shadow"), default="shadow")
    run.add_argument("--session-date", type=date.fromisoformat)
    run.add_argument("--runtime-root", type=Path)
    run.add_argument("--no-wait", action="store_true", help=argparse.SUPPRESS)
    campaign = commands.add_parser("campaign", help=argparse.SUPPRESS)
    campaign.add_argument("--config", type=Path, required=True)
    campaign.add_argument("--preflight-only", action="store_true")
    postmortem = commands.add_parser("postmortem", help=argparse.SUPPRESS)
    postmortem.add_argument("--campaign-config", type=Path, required=True)
    postmortem.add_argument("--phase", choices=("auto", "provisional", "final"), default="auto")
    postmortem.add_argument("--session-date", type=date.fromisoformat)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    package_root = Path(__file__).resolve().parents[2]
    if args.command == "campaign":
        try:
            return run_campaign(
                package_root=package_root,
                config=CampaignConfig.load(args.config),
                preflight_only=args.preflight_only,
            )
        except Exception as exc:
            print(json.dumps({
                "status": "fail_closed", "error_type": type(exc).__name__,
                "message": str(exc),
            }, sort_keys=True))
            return 2
    if args.command == "postmortem":
        try:
            campaign = CampaignConfig.load(args.campaign_config)
            now_et = datetime.now(ZoneInfo("America/New_York"))
            phase = args.phase
            session_date = args.session_date
            if phase == "auto":
                postmortem_config = json.loads(
                    (package_root / "config/postmortem.json").read_text(encoding="utf-8")
                )
                provisional_after = clock_time.fromisoformat(
                    postmortem_config["provisional_capture_after_et"]
                )
                provisional_candidates = [
                    value for value in campaign.session_dates
                    if value == now_et.date()
                    and now_et.time() >= provisional_after
                ]
                if provisional_candidates:
                    session_date = provisional_candidates[-1]
                    phase = "provisional"
                else:
                    final_candidates = []
                    for value in campaign.session_dates:
                        matches = sorted(campaign.runtime_root.glob(
                            f"SHAQ-CANARY-{value.isoformat()}-*"
                        ))
                        if not matches:
                            continue
                        post_root = matches[-1] / "postmortem"
                        if (
                            value < now_et.date()
                            and (post_root / "postmortem_provisional.json").is_file()
                            and not (post_root / "postmortem_final.json").exists()
                        ):
                            final_candidates.append(value)
                    if final_candidates:
                        session_date = final_candidates[-1]
                        phase = "final"
                if session_date is None and now_et >= datetime.combine(
                    date.fromisoformat(postmortem_config["first_batch_review_date"]),
                    clock_time.fromisoformat(postmortem_config["batch_review_after_et"]),
                    ZoneInfo("America/New_York"),
                ):
                    output = BatchReviewRunner(
                        package_root=package_root, runtime_root=campaign.runtime_root
                    ).run(generated_at_et=now_et)
                    write_campaign_views(campaign)
                    print(json.dumps({
                        "status": "complete", "phase": "batch_review",
                        "output": str(output),
                    }, sort_keys=True))
                    return 0
            if session_date is None or phase == "auto":
                print(json.dumps({
                    "status": "idle", "reason": "no_postmortem_phase_is_due"
                }, sort_keys=True))
                return 0
            runtime = PostmortemRunner(
                package_root=package_root,
                runtime_root=campaign.runtime_root,
                host=campaign.host,
                port=campaign.port,
            ).run(session_date=session_date, phase=phase)
            write_campaign_views(campaign)
            print(json.dumps({
                "status": "complete", "phase": phase,
                "session_date": session_date.isoformat(), "runtime": str(runtime),
            }, sort_keys=True))
            return 0
        except Exception as exc:
            print(json.dumps({
                "status": "fail_closed", "error_type": type(exc).__name__,
                "message": str(exc),
            }, sort_keys=True))
            return 2
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
