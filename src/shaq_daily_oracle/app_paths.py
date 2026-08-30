from __future__ import annotations

import sys
import shutil
from dataclasses import dataclass
from pathlib import Path

from platformdirs import user_config_dir, user_data_dir, user_log_dir


@dataclass(frozen=True)
class AppPaths:
    package_root: Path
    data_root: Path
    config_root: Path
    log_root: Path
    runtime_root: Path
    dashboard_db: Path
    settings_file: Path
    effective_ai_config: Path

    def ensure(self) -> "AppPaths":
        for path in (self.data_root, self.config_root, self.log_root, self.runtime_root):
            path.mkdir(parents=True, exist_ok=True)
        return self


def installed_package_root() -> Path:
    bundled = getattr(sys, "_MEIPASS", None)
    if bundled:
        return Path(str(bundled)).resolve()
    return Path(__file__).resolve().parents[2]


def app_paths(*, package_root: Path | None = None) -> AppPaths:
    package = (package_root or installed_package_root()).resolve()
    data = Path(user_data_dir("SHAQ Daily Oracle", "SHAQ Research")).resolve()
    config = Path(user_config_dir("SHAQ Daily Oracle", "SHAQ Research")).resolve()
    logs = Path(user_log_dir("SHAQ Daily Oracle", "SHAQ Research")).resolve()
    return AppPaths(
        package_root=package,
        data_root=data,
        config_root=config,
        log_root=logs,
        runtime_root=data / "runtime",
        dashboard_db=data / "dashboard.sqlite3",
        settings_file=config / "settings.json",
        effective_ai_config=config / "ai-backend.json",
    )


def migrate_legacy_runtime(paths: AppPaths) -> int:
    """Copy legacy repository runs once without overwriting app-owned records."""
    legacy = paths.package_root / "runtime"
    if not legacy.is_dir() or legacy.resolve() == paths.runtime_root.resolve():
        return 0
    copied = 0
    for source in sorted(legacy.glob("SHAQ-CANARY-*-*")):
        destination = paths.runtime_root / source.name
        if source.is_dir() and not destination.exists():
            shutil.copytree(source, destination)
            copied += 1
    return copied
