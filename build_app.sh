#!/usr/bin/env bash
# Build Autoreview.app for macOS (PyInstaller). Run from repo root.
#
# Usage:
#   ./build_app.sh              # output: ./dist/Autoreview.app only
#   ./build_app.sh --install    # also copy to /Applications/Autoreview.app
#   ./build_app.sh --fast       # skip deleting build/ dist/ (iterative rebuilds)
#
# PyInstaller is invoked with paths relative to ROOT; Autoreview.spec (if generated) is removed
# on a clean build. For distribution beyond dev machines, add codesign + notarization separately.
set -euo pipefail

if [[ "$(uname -s)" != "Darwin" ]]; then
  echo "This script builds a macOS .app; run it on macOS."
  exit 1
fi

ROOT="$(cd "$(dirname "$0")" && pwd)"
cd "$ROOT"

INSTALL_APPS=false
FAST=false
for arg in "$@"; do
  if [[ "$arg" == "--install" ]]; then
    INSTALL_APPS=true
  elif [[ "$arg" == "--fast" ]]; then
    FAST=true
  fi
done

if [[ ! -d .venv ]]; then
  python3 -m venv .venv
fi
# shellcheck disable=SC1091
source .venv/bin/activate
pip install -q -U pip
pip install -q -e ".[dev,gui]"

python scripts/make_icon.py

ICNS="$ROOT/assets/Autoreview.icns"
ICON_ARG=()
if [[ -f "$ICNS" ]]; then
  ICON_ARG=(--icon "$ICNS")
fi

if [[ "$FAST" != true ]]; then
  rm -rf build dist Autoreview.spec
fi

python -m PyInstaller \
  --noconfirm \
  --clean \
  --windowed \
  --name Autoreview \
  --osx-bundle-identifier "ai.autoreview.app" \
  "${ICON_ARG[@]}" \
  --add-data "${ROOT}/assets:assets" \
  --hidden-import autoreview.engine \
  --hidden-import autoreview.keychain \
  --hidden-import autoreview.cli \
  --collect-all keyring \
  --collect-all certifi \
  "${ROOT}/autoreview/gui.py"

if [[ ! -d "${ROOT}/dist/Autoreview.app" ]]; then
  echo "Build failed: ${ROOT}/dist/Autoreview.app not found."
  exit 1
fi
echo "Built: ${ROOT}/dist/Autoreview.app"
if [[ "$INSTALL_APPS" == true ]]; then
  if [[ ! -w /Applications ]]; then
    echo "Cannot write to /Applications (try: sudo cp -R dist/Autoreview.app /Applications/)."
    exit 1
  fi
  rm -rf /Applications/Autoreview.app
  # ditto preserves extended attributes; cp -R is fine for unsigned dev builds
  cp -R "${ROOT}/dist/Autoreview.app" /Applications/
  echo "Installed: /Applications/Autoreview.app"
else
  echo "Tip: open dist here, or run ./build_app.sh --install to copy to /Applications/"
fi
