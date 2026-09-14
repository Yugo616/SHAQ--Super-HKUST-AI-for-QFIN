#!/bin/zsh
set -euo pipefail
PROJECT_ROOT="${0:A:h:h}"
cd "${PROJECT_ROOT}"
PYTHON_BIN="${SHAQ_BUILD_PYTHON:-python3}"
APP_NAME="${SHAQ_APP_NAME:-SHAQ Daily Oracle Lab}"
OUTPUT_ROOT="${SHAQ_OUTPUT_ROOT:-${PROJECT_ROOT}/dist/native}"
case "$(uname -m)" in
  arm64) PACKAGE_ARCH="Apple-Silicon" ;;
  x86_64) PACKAGE_ARCH="Intel" ;;
  *) echo "Unsupported native architecture" >&2; exit 2 ;;
esac
BUILD_VERSION="${SHAQ_BUILD_VERSION:-$("${PYTHON_BIN}" -c 'import tomllib; print(tomllib.load(open("pyproject.toml", "rb"))["project"]["version"])')}"
"${PYTHON_BIN}" packaging/build_desktop.py --output "${OUTPUT_ROOT}" --name "${APP_NAME}" --version "${BUILD_VERSION}"
APP_PATH="${OUTPUT_ROOT}/${APP_NAME}.app"
# Finder/iCloud may restore FinderInfo in a Documents checkout during signing.
# Stage only this newly built app outside synced storage; never touch installed apps.
SIGN_ROOT="$(mktemp -d "${TMPDIR:-/tmp}/shaq-native-sign.XXXXXX")"
ditto --norsrc --noextattr "${APP_PATH}" "${SIGN_ROOT}/${APP_NAME}.app"
APP_PATH="${SIGN_ROOT}/${APP_NAME}.app"
/usr/bin/xattr -cr "${APP_PATH}"
/usr/bin/xattr -d com.apple.FinderInfo "${APP_PATH}" 2>/dev/null || true
/usr/bin/xattr -d com.apple.FinderInfo "${APP_PATH}/Contents/Frameworks/Python.framework" 2>/dev/null || true
/usr/bin/codesign --force --deep --sign - "${APP_PATH}"
/usr/bin/codesign --verify --deep --strict "${APP_PATH}"
"${APP_PATH}/Contents/MacOS/${APP_NAME}" --smoke --smoke-output "${OUTPUT_ROOT}/smoke.json"
"${PYTHON_BIN}" packaging/audit_payload.py "${APP_PATH}" --output "${OUTPUT_ROOT}/native-audit.json"
if [[ "${SHAQ_PREVIEW_ONLY:-0}" == 1 ]]; then
  echo "${APP_PATH}"
  exit 0
fi
FEED_ROOT="${SHAQ_FEED_ROOT:-${PROJECT_ROOT}/dist/update-feed}"
"${PYTHON_BIN}" packaging/build_desktop.py --manage-existing "${APP_PATH}" --output "${FEED_ROOT}" --version "${BUILD_VERSION}"
case "$(uname -m)" in
  arm64) UPDATE_CHANNEL="osx-arm64-stable" ;;
  x86_64) UPDATE_CHANNEL="osx-x64-stable" ;;
esac
MANAGED_ROOT="$(mktemp -d "${TMPDIR:-/tmp}/shaq-managed-dmg.XXXXXX")"
ditto -xk "${FEED_ROOT}/SHAQDailyOracleLab-${UPDATE_CHANNEL}-Portable.zip" "${MANAGED_ROOT}"
APP_PATH="${MANAGED_ROOT}/${APP_NAME}.app"
/usr/bin/codesign --verify --deep --strict "${APP_PATH}"
"${PYTHON_BIN}" packaging/audit_payload.py "${APP_PATH}" --output "${OUTPUT_ROOT}/managed-native-audit.json"
DMG_PATH="${PROJECT_ROOT}/dist/SHAQ-Daily-Oracle-Lab-macOS-${PACKAGE_ARCH}.dmg"
hdiutil create -volname "${APP_NAME}" -srcfolder "${MANAGED_ROOT}" -ov -format UDZO "${DMG_PATH}"
(cd "${PROJECT_ROOT}/dist"; shasum -a 256 "${DMG_PATH:t}") > "${DMG_PATH}.sha256"
