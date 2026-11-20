#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")"
if [[ "$(uname -s)" != "Darwin" ]]; then
  echo "build-macos.sh must run on macOS" >&2
  exit 1
fi

python3 -m venv .venv-macos
.venv-macos/bin/python -m pip install -r requirements-macos.txt
# Every test file: Windows-only tests (the pystray tray, Win32 calls) skip
# themselves on macOS, so a test that fails here is a Mac failure.
.venv-macos/bin/python -X utf8 -m unittest discover -v -p 'test_*.py'

.venv-macos/bin/python -m PyInstaller --noconfirm --onedir --windowed \
  --name Circlow --osx-bundle-identifier com.antiqiusama.circlow --icon "$PWD/assets/circlow-fox-tray-mascot.icns" \
  --add-data "$PWD/assets/circlow-fox-tray-mascot.png:." --add-data "$PWD/claude_status_receiver.py:." \
  --collect-data tzdata --distpath dist-macos --workpath work/pyinstaller-macos \
  --specpath work mac_menu.py

bundle="dist-macos/Circlow.app"
test -d "$bundle"
# AppKit gives an app the macOS 26 Liquid Glass controls (the settings
# window's NSSwitch) only when its executable says it was built with the 26
# SDK; Python and PyInstaller's bootloader say 15. Record 26, keep the
# minimum system version; older macOS ignores the field.
executable="$bundle/Contents/MacOS/$(/usr/libexec/PlistBuddy -c 'Print :CFBundleExecutable' "$bundle/Contents/Info.plist")"
minos="$(otool -l "$executable" | awk '/LC_BUILD_VERSION/{f=1} f&&/minos/{print $2; exit}')"
xcrun vtool -set-build-version macos "${minos:-11.0}" 26.0 -replace -output "$executable" "$executable"
/usr/libexec/PlistBuddy -c 'Add :LSUIElement bool true' "$bundle/Contents/Info.plist"
/usr/libexec/PlistBuddy -c 'Set :CFBundleDisplayName 环流' "$bundle/Contents/Info.plist" 2>/dev/null \
  || /usr/libexec/PlistBuddy -c 'Add :CFBundleDisplayName string 环流' "$bundle/Contents/Info.plist"
# The MiMo session's keychain helper (mac_keychain_helper.c). The keychain
# remembers "Always Allow" by code hash for apps without an Apple Team ID, and
# the app's hash changes with every build; the helper's does not (same
# source, compiler, flags and output name, signed without a timestamp), so
# its one "Always Allow" survives updates.
mkdir -p work/keychain-helper
clang -O2 -Wall -Wextra -Wno-deprecated-declarations -mmacosx-version-min="${minos:-11.0}" \
  -framework Security -framework CoreFoundation -o work/keychain-helper/circlow-keychain mac_keychain_helper.c
cp work/keychain-helper/circlow-keychain "$bundle/Contents/MacOS/circlow-keychain"
# Sign with the "Circlow Development" identity when the login keychain has
# one (macos-signing-identity.sh creates it), or with the identity named in
# CIRCLOW_CODESIGN_IDENTITY, so every local build keeps the same signature:
# the system's permissions then survive a rebuild, and the keychain helper
# serves the app only when both carry this signature. Ad hoc otherwise (CI),
# whose signature is a hash of the binary and changes with every build; the
# app then reads the keychain itself.
identity="${CIRCLOW_CODESIGN_IDENTITY:-}"
if [[ -z "$identity" ]] && security find-identity -v -p codesigning 2>/dev/null | grep -q '"Circlow Development"'; then
  identity="Circlow Development"
fi
if [[ -n "$identity" ]]; then
  codesign --force --deep --timestamp=none --sign "$identity" "$bundle"
else
  codesign --force --deep --sign - "$bundle"
fi
codesign --verify --deep --strict "$bundle"
echo "Built: $bundle (signed: ${identity:-ad hoc})"
