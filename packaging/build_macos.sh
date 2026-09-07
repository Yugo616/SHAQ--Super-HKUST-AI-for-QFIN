#!/bin/zsh
set -euo pipefail

PROJECT_ROOT="${0:A:h:h}"
OUTPUT_ROOT="$(mktemp -d /private/tmp/shaq-daily-oracle-lab-build.XXXXXX)"
APP_NAME="SHAQ Daily Oracle Lab"
MACHINE_ARCH="$(uname -m)"
case "${MACHINE_ARCH}" in
  arm64) PACKAGE_ARCH="Apple-Silicon" ;;
  x86_64) PACKAGE_ARCH="Intel" ;;
  *) echo "Unsupported macOS architecture: ${MACHINE_ARCH}" >&2; exit 2 ;;
esac

cd "${PROJECT_ROOT}"
PYTHON_BIN="${SHAQ_BUILD_PYTHON:-python3}"
"${PYTHON_BIN}" -m PyInstaller \
  --noconfirm \
  --clean \
  --windowed \
  --onedir \
  --name "${APP_NAME}" \
  --collect-all webview \
  --collect-all pandas_market_calendars \
  --collect-all yfinance \
  --collect-all keyring \
  --hidden-import openai \
  --hidden-import keyring.backends.macOS \
  --add-data "${PROJECT_ROOT}/pyproject.toml:." \
  --add-data "${PROJECT_ROOT}/config:config" \
  --add-data "${PROJECT_ROOT}/governance:governance" \
  --add-data "${PROJECT_ROOT}/schemas:schemas" \
  --add-data "${PROJECT_ROOT}/skills:skills" \
  --add-data "${PROJECT_ROOT}/src/shaq_daily_oracle/desktop:shaq_daily_oracle/desktop" \
  --distpath "${OUTPUT_ROOT}" \
  --workpath "${PROJECT_ROOT}/build/desktop-macos-${MACHINE_ARCH}" \
  --specpath "${PROJECT_ROOT}/build" \
  "${PROJECT_ROOT}/packaging/desktop_entry.py"

APP_PATH="${OUTPUT_ROOT}/${APP_NAME}.app"
/usr/bin/xattr -cr "${APP_PATH}"
/usr/bin/codesign --force --deep --sign - "${APP_PATH}"
"${APP_PATH}/Contents/MacOS/${APP_NAME}" --smoke

DMG_PATH="${PROJECT_ROOT}/dist/SHAQ-Daily-Oracle-Lab-macOS-${PACKAGE_ARCH}.dmg"
mkdir -p "${PROJECT_ROOT}/dist"
hdiutil create -volname "${APP_NAME}" -srcfolder "${APP_PATH}" -ov -format UDZO "${DMG_PATH}"
(
  cd "${PROJECT_ROOT}/dist"
  shasum -a 256 "$(basename "${DMG_PATH}")"
) > "${DMG_PATH}.sha256"
