#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")"
if [[ "$(uname -s)" != "Darwin" ]]; then
  echo "build-macos.sh must run on macOS" >&2
  exit 1
fi

python3 -m venv .venv-macos
.venv-macos/bin/python -m pip install -r requirements-macos.txt
.venv-macos/bin/python -X utf8 -m unittest -v \
  test_app_identity.py test_mac_menu.py test_mac_panel.py test_settings_mac.py test_quota_sources.py test_tps_sources.py test_claude.py \
  test_claude_status.py test_features.py test_model_iq.py test_mimo.py test_process_jail.py

.venv-macos/bin/python -m PyInstaller --noconfirm --onedir --windowed \
  --name Circlow --osx-bundle-identifier com.antiqiusama.circlow --icon "$PWD/assets/circlow-fox-tray-mascot.icns" \
  --collect-data tzdata --distpath dist-macos --workpath work/pyinstaller-macos \
  --specpath work mac_menu.py

bundle="dist-macos/Circlow.app"
test -d "$bundle"
/usr/libexec/PlistBuddy -c 'Add :LSUIElement bool true' "$bundle/Contents/Info.plist"
/usr/libexec/PlistBuddy -c 'Set :CFBundleDisplayName 环流' "$bundle/Contents/Info.plist" 2>/dev/null \
  || /usr/libexec/PlistBuddy -c 'Add :CFBundleDisplayName string 环流' "$bundle/Contents/Info.plist"
codesign --force --deep --sign - "$bundle"
codesign --verify --deep --strict "$bundle"
echo "Built: $bundle"
