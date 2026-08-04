#!/usr/bin/env bash
set -euo pipefail

desktop_root="$(cd "$(dirname "$0")/.." && pwd)"
bundle_root="$desktop_root/src-tauri/target/release/bundle"
app_path="$bundle_root/macos/REC Coach.app"
app_version="$(node -p "JSON.parse(require('fs').readFileSync('$desktop_root/src-tauri/tauri.conf.json', 'utf8')).version")"
dmg_path="$bundle_root/dmg/REC Coach_${app_version}_aarch64.dmg"

cd "$desktop_root"
LC_ALL=C LANG=C ./node_modules/.bin/tauri build --bundles app

if [[ ! -d "$app_path" ]]; then
  echo "REC Coach.app was not produced at $app_path" >&2
  exit 1
fi

dmg_staging="$(mktemp -d "${TMPDIR:-/tmp}/rec-coach-dmg.XXXXXX")"
trap 'rm -rf "$dmg_staging"' EXIT
cp -R "$app_path" "$dmg_staging/REC Coach.app"
ln -s /Applications "$dmg_staging/Applications"
mkdir -p "$(dirname "$dmg_path")"
hdiutil create -volname "REC Coach" -srcfolder "$dmg_staging" -ov -format UDZO "$dmg_path"
hdiutil verify "$dmg_path"

echo "Built $app_path"
echo "Built $dmg_path"
