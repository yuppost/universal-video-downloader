#!/bin/sh
# Installs Universal Video Studio for the current user (no root needed): ~/.local/opt/UniversalVideoStudio
# plus a menu entry. Run it from the extracted folder:  ./install.sh
# The app can then update itself (its folder is writable). Remove it with:  ./install.sh --uninstall
set -e
HERE="$(cd "$(dirname "$0")" && pwd)"
APPDIR="$HOME/.local/opt/UniversalVideoStudio"
DESKTOP="$HOME/.local/share/applications/UniversalVideoStudio.desktop"

if [ "$1" = "--uninstall" ]; then
  rm -rf "$APPDIR" "$DESKTOP"
  echo "Removed. Your settings and downloads were not touched ($HOME/.local/share/UniversalVideoStudio, ~/Videos)."
  exit 0
fi

if [ ! -x "$HERE/UniversalVideoStudio/UniversalVideoStudio" ]; then
  echo "Run this script from the extracted UniversalVideoStudio archive (next to the UniversalVideoStudio folder)." >&2
  exit 1
fi
mkdir -p "$(dirname "$APPDIR")" "$(dirname "$DESKTOP")"
rm -rf "$APPDIR"
cp -a "$HERE/UniversalVideoStudio" "$APPDIR"
cp "$HERE/icon.png" "$APPDIR/icon.png" 2>/dev/null || true
sed "s|@APPDIR@|$APPDIR|g" "$HERE/UniversalVideoStudio.desktop" > "$DESKTOP"
chmod +x "$DESKTOP" "$APPDIR/UniversalVideoStudio"
command -v update-desktop-database >/dev/null 2>&1 && update-desktop-database "$HOME/.local/share/applications" >/dev/null 2>&1 || true
echo "Installed to $APPDIR. Start it from your applications menu: Universal Video Studio."
