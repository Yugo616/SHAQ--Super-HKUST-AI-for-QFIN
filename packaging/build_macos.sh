#!/bin/zsh
set -euo pipefail

PROJECT_ROOT="${0:A:h:h}"
OUTPUT_ROOT="$(mktemp -d /private/tmp/shaq-daily-oracle-build.XXXXXX)"
APP_NAME="SHAQ Daily Oracle"

cd "${PROJECT_ROOT}"
python3 -m PyInstaller \
  --noconfirm \
  --clean \
  --windowed \
  --onedir \
  --name "${APP_NAME}" \
  --collect-all webview \
  --collect-all pandas_market_calendars \
  --hidden-import keyring.backends.macOS \
  --add-data "${PROJECT_ROOT}/pyproject.toml:." \
  --add-data "${PROJECT_ROOT}/config:config" \
  --add-data "${PROJECT_ROOT}/governance:governance" \
  --add-data "${PROJECT_ROOT}/schemas:schemas" \
  --add-data "${PROJECT_ROOT}/scripts:scripts" \
  --add-data "${PROJECT_ROOT}/skills:skills" \
  --add-data "${PROJECT_ROOT}/tests:tests" \
  --add-data "${PROJECT_ROOT}/src/shaq_daily_oracle:src/shaq_daily_oracle" \
  --add-data "${PROJECT_ROOT}/src/shaq_daily_oracle/desktop:shaq_daily_oracle/desktop" \
  --distpath "${OUTPUT_ROOT}" \
  --workpath "${PROJECT_ROOT}/build/desktop-macos" \
  --specpath "${PROJECT_ROOT}/build" \
  "${PROJECT_ROOT}/packaging/desktop_entry.py"

APP_PATH="${OUTPUT_ROOT}/${APP_NAME}.app"
/usr/bin/xattr -cr "${APP_PATH}"
/usr/bin/codesign --force --deep --sign - "${APP_PATH}"
"${APP_PATH}/Contents/MacOS/${APP_NAME}" --smoke

DMG_PATH="${PROJECT_ROOT}/dist/SHAQ-Daily-Oracle-macOS-Apple-Silicon.dmg"
hdiutil create -volname "${APP_NAME}" -srcfolder "${APP_PATH}" -ov -format UDZO "${DMG_PATH}"
shasum -a 256 "${DMG_PATH}" > "${DMG_PATH}.sha256"
