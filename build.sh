#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
VERSION_FILE="$SCRIPT_DIR/VERSION"
PLUGIN_NAME="OctoPrint-PSUControl-HomeAssistant-WS"
OUT_DIR="$SCRIPT_DIR/dist"

# ── Auto-increment patch version ──────────────────────────────
OLD_VERSION="$(tr -d '[:space:]' < "$VERSION_FILE")"

MAJOR="$(echo "$OLD_VERSION" | cut -d. -f1)"
MINOR="$(echo "$OLD_VERSION" | cut -d. -f2)"
PATCH="$(echo "$OLD_VERSION" | cut -d. -f3)"

# Handle negative or missing patch (e.g. 1.0.-1 → 1.0.0)
if [ -z "$PATCH" ] || [ "$PATCH" -lt 0 ] 2>/dev/null; then
    PATCH=0
else
    PATCH=$((PATCH + 1))
fi

VERSION="${MAJOR}.${MINOR}.${PATCH}"

# Write new version back
echo "$VERSION" > "$VERSION_FILE"

echo "==> Version: ${OLD_VERSION} -> ${VERSION}"

# ── Build ─────────────────────────────────────────────────────
ZIP_NAME="${PLUGIN_NAME}-${VERSION}.zip"

echo "==> Building ${ZIP_NAME}"

# Clean previous builds
rm -rf "$OUT_DIR"
mkdir -p "$OUT_DIR"

# Create a temp staging directory
STAGING=$(mktemp -d)
trap 'rm -rf "$STAGING"' EXIT

DEST="$STAGING/${PLUGIN_NAME}-${VERSION}"
mkdir -p "$DEST"

# Copy plugin files
cp "$SCRIPT_DIR/setup.py"          "$DEST/"
cp "$SCRIPT_DIR/requirements.txt"  "$DEST/"
cp "$SCRIPT_DIR/VERSION"           "$DEST/"
cp "$SCRIPT_DIR/README.md"         "$DEST/"
cp "$SCRIPT_DIR/LICENSE"           "$DEST/"
cp -r "$SCRIPT_DIR/octoprint_psucontrol_hass_ws" "$DEST/"

# Remove caches
find "$DEST" -type d -name "__pycache__" -exec rm -rf {} + 2>/dev/null || true
find "$DEST" -name "*.pyc" -delete 2>/dev/null || true

# Build zip
(cd "$STAGING" && zip -r "$OUT_DIR/$ZIP_NAME" "${PLUGIN_NAME}-${VERSION}")

echo "==> Done: dist/${ZIP_NAME} ($(du -h "$OUT_DIR/$ZIP_NAME" | cut -f1))"
