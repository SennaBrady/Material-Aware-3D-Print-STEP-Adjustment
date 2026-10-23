#!/usr/bin/env bash
# One-time setup on Linux: installs Material-Aware Print and adds it to your app menu.
# Double-click this file (choose "Run"), or run it once from a terminal. No sudo needed.
set -euo pipefail
cd "$(dirname "$0")/.."
REPO="$(pwd)"
export PATH="$HOME/.local/bin:$PATH"

supported() { "$1" -c 'import sys; sys.exit(0 if (3, 10) <= sys.version_info[:2] <= (3, 13) else 1)' 2>/dev/null; }

if [ ! -x .venv/bin/python ] || ! supported .venv/bin/python; then
  rm -rf .venv
  PY=""
  for c in python3.13 python3.12 python3.11 python3.10 python3; do
    if command -v "$c" >/dev/null 2>&1 && supported "$c"; then PY="$c"; break; fi
  done
  if [ -n "$PY" ] && "$PY" -m venv .venv 2>/dev/null; then
    .venv/bin/python -m pip install --upgrade pip >/dev/null
  else
    # System Python is too new (or has no venv module): get a private Python 3.12 with uv.
    command -v uv >/dev/null 2>&1 || curl -LsSf https://astral.sh/uv/install.sh | sh
    uv venv --python 3.12 .venv
  fi
fi

echo "Installing Material-Aware Print (the first time downloads about 300 MB)..."
if .venv/bin/python -m pip --version >/dev/null 2>&1; then
  .venv/bin/python -m pip install -e .
else
  uv pip install --python .venv/bin/python -e .
fi

APPS="$HOME/.local/share/applications"
ICONS="$HOME/.local/share/icons/hicolor/scalable/apps"
mkdir -p "$APPS" "$ICONS"
cp src/matprint/app/static/icon.svg "$ICONS/matprint.svg"
cat > "$APPS/matprint.desktop" <<DESKTOP
[Desktop Entry]
Type=Application
Name=Material-Aware Print
Comment=Adjust part models for material shrinkage before 3D printing
Exec="$REPO/.venv/bin/python" -m matprint.app
Icon=matprint
Terminal=false
Categories=Graphics;Engineering;
StartupNotify=true
DESKTOP
chmod +x "$APPS/matprint.desktop"
command -v update-desktop-database >/dev/null 2>&1 && update-desktop-database "$APPS" 2>/dev/null || true

DESKTOP_DIR="$(command -v xdg-user-dir >/dev/null 2>&1 && xdg-user-dir DESKTOP || echo "$HOME/Desktop")"
if [ -d "$DESKTOP_DIR" ]; then
  cp "$APPS/matprint.desktop" "$DESKTOP_DIR/"
  chmod +x "$DESKTOP_DIR/matprint.desktop"
  command -v gio >/dev/null 2>&1 && gio set "$DESKTOP_DIR/matprint.desktop" metadata::trusted true 2>/dev/null || true
fi

echo
echo "Done. Open \"Material-Aware Print\" from your app menu or desktop."
