#!/usr/bin/env bash
# One-time setup on the Mac you build on: a self-signed code-signing identity
# called "Circlow Development" in the login keychain. build-macos.sh signs
# every local build with it, so the app keeps one identity across rebuilds:
# the keychain's "always allow" for the MiMo session (and the system's
# notification permission) then survives a rebuild. An ad hoc signature, the
# fallback without this identity, is a hash of the binary and changes with
# every build, so the keychain asks again each time.
#
# Run it yourself: macOS asks for your login password once to trust the
# certificate for code signing in your account. The first build signed with
# it may ask once more for the key (choose "始终允许" / Always Allow), and the
# first run of that build asks once for the MiMo session: choose "始终允许".
set -euo pipefail

name="${1:-Circlow Development}"
keychain="$HOME/Library/Keychains/login.keychain-db"
openssl=/usr/bin/openssl        # the system LibreSSL: its PKCS#12 defaults import cleanly

if security find-identity -v -p codesigning 2>/dev/null | grep -q "\"$name\""; then
  echo "已有签名身份：$name"
  exit 0
fi

work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT
"$openssl" req -x509 -newkey rsa:2048 -sha256 -days 3650 -nodes \
  -keyout "$work/key.pem" -out "$work/cert.pem" -subj "/CN=$name" \
  -addext "keyUsage=critical,digitalSignature" -addext "extendedKeyUsage=critical,codeSigning" 2>/dev/null
pass="$(head -c 24 /dev/urandom | base64)"
"$openssl" pkcs12 -export -inkey "$work/key.pem" -in "$work/cert.pem" -name "$name" \
  -out "$work/identity.p12" -passout "pass:$pass"
security import "$work/identity.p12" -k "$keychain" -P "$pass" -T /usr/bin/codesign -T /usr/bin/security >/dev/null
# Trust the certificate for code signing in this account (the password prompt).
security add-trusted-cert -r trustRoot -p codeSign -k "$keychain" "$work/cert.pem"

if ! security find-identity -v -p codesigning | grep -q "\"$name\""; then
  echo "未能建立可用的签名身份：$name" >&2
  exit 1
fi
echo "已创建签名身份：$name（有效期 10 年）。下次 build-macos.sh 会用它签名。"
