#!/bin/sh
# Builds the Linux app: dist/UniversalVideoStudio/ (+ UniversalVideoStudio-linux-<cpu>.tar.gz). Like build.bat.
# FFmpeg and Deno are NOT bundled: the app downloads them (checksum verified) on first launch.
# Needs Python 3.11+ with venv. On a bare Debian/Ubuntu also: sudo apt install libegl1 libgl1 libxkbcommon-x11-0 libxcb-cursor0 libnss3 libasound2
set -e
cd "$(dirname "$0")"

# CI sets CI=true and provides Python; otherwise use the project's .venv (created by run.sh).
if [ -n "$CI" ]; then
  VPY=python3
else
  [ -x .venv/bin/python ] || { echo "Run ./run.sh once first to create the virtual environment."; exit 1; }
  VPY=.venv/bin/python
fi
$VPY -m pip install -r requirements-dev.txt

# Tests must pass (judged by pytest's own summary, like build.bat)
export QT_QPA_PLATFORM=offscreen UVS_NO_AUDIO=1
TESTLOG="$(mktemp)"
$VPY -m pytest -q > "$TESTLOG" 2>&1 || true
cat "$TESTLOG"
if grep -Eq "[0-9]+ (failed|error)" "$TESTLOG"; then echo "Tests failed - build aborted."; exit 1; fi
grep -Eq "[0-9]+ passed" "$TESTLOG" || { echo "Tests did not finish - build aborted."; exit 1; }
unset QT_QPA_PLATFORM

$VPY -m PyInstaller --noconfirm --clean --windowed --name UniversalVideoStudio \
  --collect-submodules yt_dlp --collect-data yt_dlp --copy-metadata yt-dlp --collect-all curl_cffi --collect-all certifi \
  --collect-submodules PySide6.QtMultimedia \
  --hidden-import PySide6.QtWebEngineWidgets --hidden-import PySide6.QtWebEngineCore \
  main.py

# SECURITY: the build must never contain sign-in cookies or personal data.
rm -rf dist/UniversalVideoStudio/data dist/UniversalVideoStudio/cookies
[ -x dist/UniversalVideoStudio/UniversalVideoStudio ] || { echo "The program was not built."; exit 1; }

# Release archive: the app folder + installer script + menu entry + icon
case "$(uname -m)" in x86_64|amd64) ARCH=x86_64;; aarch64|arm64) ARCH=aarch64;; *) ARCH="$(uname -m)";; esac
STAGE="dist/package"
rm -rf "$STAGE"
mkdir -p "$STAGE"
cp -a dist/UniversalVideoStudio "$STAGE/UniversalVideoStudio"
cp packaging/linux/install.sh packaging/linux/UniversalVideoStudio.desktop assets/icon.png "$STAGE/"
chmod +x "$STAGE/install.sh"        # git on Windows / zip files may have dropped the executable bit
tar -C "$STAGE" -czf "UniversalVideoStudio-linux-$ARCH.tar.gz" .
rm -rf "$STAGE"

# .deb for Debian / Ubuntu / Mint (needs dpkg-deb, present on those systems and on the GitHub Ubuntu runner)
if command -v dpkg-deb >/dev/null 2>&1; then
  case "$ARCH" in x86_64) DEBARCH=amd64;; aarch64) DEBARCH=arm64;; *) DEBARCH="";; esac
  VERSION="$($VPY -c 'from app import __version__; print(__version__)')"
  if [ -n "$DEBARCH" ]; then
    sh packaging/linux/make_deb.sh dist/UniversalVideoStudio "$VERSION" "$DEBARCH" "UniversalVideoStudio-linux-$ARCH.deb"
  fi
fi
echo
echo "Build ready: dist/UniversalVideoStudio/UniversalVideoStudio  and  UniversalVideoStudio-linux-$ARCH.tar.gz"
echo "Install for the current user: extract the archive, then run ./install.sh   (Debian/Ubuntu: sudo apt install ./UniversalVideoStudio-linux-$ARCH.deb)"
