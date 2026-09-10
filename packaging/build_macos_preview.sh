#!/bin/zsh
# Uses exactly the release payload; never replaces an installed Preview.
set -euo pipefail
PROJECT_ROOT="${0:A:h:h}"
export SHAQ_APP_NAME="SHAQ Daily Oracle Lab Preview"
export SHAQ_OUTPUT_ROOT="${PROJECT_ROOT}/dist/preview"
export SHAQ_PREVIEW_ONLY=1
exec zsh "${PROJECT_ROOT}/packaging/build_macos.sh"
