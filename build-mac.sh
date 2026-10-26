#!/bin/bash
# Build Cubao Acount cho macOS. Chạy TRÊN MÁY MAC: bash build-mac.sh
# Kết quả: dist-mac/Cubao Acount-1.0.0-<arch>.dmg (và dist-mac/mac*/Cubao Acount.app)
set -euo pipefail
cd "$(dirname "$0")"
HERE="$(pwd)"
BUILD="$HERE/build"

need() { command -v "$1" >/dev/null 2>&1 || { echo "Thiếu $1. $2"; exit 1; }; }
need python3 "Cài Python 3.11: brew install python@3.11"
need node "Cài Node.js: brew install node"
need npm "Cài Node.js: brew install node"

PY="${PYTHON:-python3}"
command -v python3.11 >/dev/null 2>&1 && PY="python3.11"
echo "== Dùng $($PY --version)"

mkdir -p "$BUILD/tools"

echo "== 1/5 Môi trường Python"
[ -d "$BUILD/venv" ] || "$PY" -m venv "$BUILD/venv"
# shellcheck disable=SC1091
source "$BUILD/venv/bin/activate"
pip install --upgrade pip
pip install "requests>=2.31,<3" "playwright==1.49.1" "pyinstaller>=6.10"

echo "== 2/5 Đóng gói lõi NoteGPTBackend"
rm -rf "$BUILD/dist/NoteGPTBackend" "$BUILD/pyi"
pyinstaller --noconfirm --clean --onedir --console \
  --name NoteGPTBackend \
  --paths "$HERE/backend" \
  --collect-all playwright \
  --distpath "$BUILD/dist" --workpath "$BUILD/pyi" --specpath "$BUILD/pyi" \
  "$HERE/backend/desktop_service.py"

echo "== 3/5 Tải Chromium cho Playwright"
if [ ! -d "$BUILD/browsers" ] || [ -z "$(ls -A "$BUILD/browsers" 2>/dev/null)" ]; then
  PLAYWRIGHT_BROWSERS_PATH="$BUILD/browsers" python -m playwright install chromium
fi

echo "== 4/5 ffmpeg (không bắt buộc với Cubao, chỉ dùng cho giọng)"
if command -v ffmpeg >/dev/null 2>&1; then
  cp "$(command -v ffmpeg)" "$BUILD/tools/ffmpeg"
  command -v ffprobe >/dev/null 2>&1 && cp "$(command -v ffprobe)" "$BUILD/tools/ffprobe"
else
  echo "   Không thấy ffmpeg, bỏ qua (cài nếu cần: brew install ffmpeg)"
  touch "$BUILD/tools/.keep"
fi
deactivate

echo "== 5/5 Đóng gói app Electron"
# electron-builder đòi icon >= 512px; icon gốc 128px nên phóng lên.
sips -z 512 512 "$HERE/app/icon.png" --out "$BUILD/icon.png" >/dev/null
cd "$HERE/app"
npm install
npx electron-builder --mac
cd "$HERE"

APP="$(find "$HERE/dist-mac" -maxdepth 2 -name 'Cubao Acount.app' | head -1)"
if [ -n "$APP" ]; then
  # Không có chứng chỉ Apple: ký ad-hoc để chip M chịu chạy, gỡ cờ cách ly.
  codesign --force --deep --sign - "$APP" || true
  xattr -cr "$APP" || true
  echo
  echo "XONG: $APP"
  ls "$HERE"/dist-mac/*.dmg 2>/dev/null || true
fi
