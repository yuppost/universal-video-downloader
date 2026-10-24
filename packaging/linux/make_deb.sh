#!/bin/sh
# Builds a .deb from the PyInstaller folder.
#   packaging/linux/make_deb.sh <app folder> <version> <amd64|arm64> <output .deb>
# Installs to /opt/universal-video-studio, adds /usr/bin/universal-video-studio, a menu entry and an icon.
set -e
SRC="$1"; VERSION="$2"; ARCH="$3"; OUT="$4"
[ -x "$SRC/UniversalVideoStudio" ] && [ -n "$VERSION" ] && [ -n "$ARCH" ] && [ -n "$OUT" ] || {
  echo "usage: $0 <app folder> <version> <amd64|arm64> <output.deb>" >&2; exit 1; }
HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "$HERE/../.." && pwd)"
STAGE="$(mktemp -d)"
trap 'rm -rf "$STAGE"' EXIT

PKG="$STAGE/pkg"
mkdir -p "$PKG/DEBIAN" "$PKG/opt" "$PKG/usr/bin" "$PKG/usr/share/applications" "$PKG/usr/share/icons/hicolor/256x256/apps"
cp -a "$SRC" "$PKG/opt/universal-video-studio"
ln -s /opt/universal-video-studio/UniversalVideoStudio "$PKG/usr/bin/universal-video-studio"
cp "$ROOT/assets/icon.png" "$PKG/usr/share/icons/hicolor/256x256/apps/universal-video-studio.png"
cat > "$PKG/usr/share/applications/UniversalVideoStudio.desktop" <<DESK
[Desktop Entry]
Type=Application
Name=Universal Video Studio
Comment=Download, trim and crop videos
Exec=/opt/universal-video-studio/UniversalVideoStudio
Icon=universal-video-studio
Terminal=false
Categories=AudioVideo;Video;
StartupWMClass=UniversalVideoStudio
DESK
SIZE="$(du -sk "$PKG/opt" | cut -f1)"
cat > "$PKG/DEBIAN/control" <<CTRL
Package: universal-video-studio
Version: $VERSION
Section: video
Priority: optional
Architecture: $ARCH
Installed-Size: $SIZE
Maintainer: Universal Video Studio <noreply@example.com>
Depends: libc6, libegl1, libgl1, libxkbcommon-x11-0, libxcb-cursor0, libnss3, libasound2t64 | libasound2
Recommends: ffmpeg
Description: Download, trim and crop videos
 Paste a video link, mark several sections on a timeline and export each as its own
 file. FFmpeg (if not installed) and the YouTube helper are downloaded on first start.
 The app does not update itself when installed from this package: install the newer .deb.
CTRL
cat > "$PKG/DEBIAN/postinst" <<'POST'
#!/bin/sh
set -e
command -v update-desktop-database >/dev/null 2>&1 && update-desktop-database -q /usr/share/applications || true
command -v gtk-update-icon-cache >/dev/null 2>&1 && gtk-update-icon-cache -q -t /usr/share/icons/hicolor || true
exit 0
POST
cp "$PKG/DEBIAN/postinst" "$PKG/DEBIAN/postrm"
chmod 755 "$PKG/DEBIAN/postinst" "$PKG/DEBIAN/postrm"
find "$PKG" -type d -exec chmod 755 {} +
dpkg-deb -Zxz --root-owner-group --build "$PKG" "$OUT" >/dev/null
echo "Built $OUT"
