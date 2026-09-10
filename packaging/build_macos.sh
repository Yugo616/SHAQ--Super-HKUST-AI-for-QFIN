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
"${PYTHON_BIN}" packaging/build_desktop.py --output "${OUTPUT_ROOT}" --name "${APP_NAME}"
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
DMG_PATH="${PROJECT_ROOT}/dist/SHAQ-Daily-Oracle-Lab-macOS-${PACKAGE_ARCH}.dmg"
hdiutil create -volname "${APP_NAME}" -srcfolder "${SIGN_ROOT}" -ov -format UDZO "${DMG_PATH}"
(cd "${PROJECT_ROOT}/dist"; shasum -a 256 "${DMG_PATH:t}") > "${DMG_PATH}.sha256"
