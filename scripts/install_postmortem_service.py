#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import plistlib
import sys
from datetime import date, datetime, time
from pathlib import Path
from zoneinfo import ZoneInfo


def _interval(value: datetime) -> dict[str, int]:
    return {
        "Month": value.month,
        "Day": value.day,
        "Hour": value.hour,
        "Minute": value.minute,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Install the local post-close review launchd job")
    parser.add_argument("--campaign-config", type=Path, required=True)
    parser.add_argument("--postmortem-config", type=Path, required=True)
    parser.add_argument("--daily-oracle", type=Path, required=True)
    parser.add_argument("--working-directory", type=Path, required=True)
    parser.add_argument("--inherit-environment-plist", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--local-timezone", default="Asia/Shanghai")
    args = parser.parse_args()
    campaign = json.loads(args.campaign_config.read_text(encoding="utf-8"))
    config = json.loads(args.postmortem_config.read_text(encoding="utf-8"))
    if not args.daily_oracle.is_file() or not args.working_directory.is_dir():
        raise ValueError("daily-oracle executable or working directory is unavailable")
    with args.inherit_environment_plist.open("rb") as handle:
        inherited = plistlib.load(handle).get("EnvironmentVariables", {})
    source_zone = ZoneInfo("America/New_York")
    local_zone = ZoneInfo(args.local_timezone)
    provisional_time = time.fromisoformat(config["provisional_capture_after_et"])
    final_time = time.fromisoformat(config["final_reobservation_after_et"])
    batch_time = time.fromisoformat(config["batch_review_after_et"])
    intervals = []
    for text in campaign["session_dates"]:
        session = date.fromisoformat(text)
        provisional = datetime.combine(session, provisional_time, source_zone).astimezone(local_zone)
        final = datetime.combine(
            session.fromordinal(session.toordinal() + 1), final_time, source_zone
        ).astimezone(local_zone)
        intervals.extend((_interval(provisional), _interval(final)))
    batch_review = datetime.combine(
        date.fromisoformat(config["first_batch_review_date"]), batch_time, source_zone
    ).astimezone(local_zone)
    intervals.append(_interval(batch_review))
    unique_intervals = []
    seen = set()
    for interval in sorted(intervals, key=lambda row: tuple(row[key] for key in ("Month", "Day", "Hour", "Minute"))):
        key = tuple(interval[name] for name in ("Month", "Day", "Hour", "Minute"))
        if key not in seen:
            unique_intervals.append(interval)
            seen.add(key)
    payload = {
        "Label": "com.shaq.dailyoracle.postmortem",
        "ProgramArguments": [
            "/usr/bin/caffeinate", "-dimsu", str(args.daily_oracle.resolve()),
            "postmortem", "--campaign-config", str(args.campaign_config.resolve()),
            "--phase", "auto",
        ],
        "WorkingDirectory": str(args.working_directory.resolve()),
        "EnvironmentVariables": inherited,
        "StartCalendarInterval": unique_intervals,
        "KeepAlive": {"SuccessfulExit": False},
        "ThrottleInterval": 60,
        "ProcessType": "Background",
        "StandardOutPath": str(Path.home() / "Library/Logs/SHAQ-Daily-Oracle-Postmortem.log"),
        "StandardErrorPath": str(Path.home() / "Library/Logs/SHAQ-Daily-Oracle-Postmortem.error.log"),
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    temporary = args.output.with_name(f".{args.output.name}.{os.getpid()}.tmp")
    with temporary.open("wb") as handle:
        plistlib.dump(payload, handle, sort_keys=True)
    os.replace(temporary, args.output)
    print(f"wrote {len(unique_intervals)} postmortem schedule entries: {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
