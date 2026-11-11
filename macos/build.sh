#!/bin/sh
set -eu
project_dir=$(CDPATH='' cd -- "$(dirname -- "$0")/.." && pwd)
target_dir=${1:?Usage: macos/build.sh OUTPUT_DIRECTORY [universal]}
mkdir -p "$target_dir"
app="$target_dir/Codex Sidebar Sync.app"
mkdir -p "$app/Contents/MacOS" "$app/Contents/Resources"
cp "$project_dir/recover.py" "$project_dir/sync_sidebar.py" "$app/Contents/Resources/"
cat > "$app/Contents/Info.plist" <<'PLIST'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "https://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>CFBundleExecutable</key><string>CodexSidebarSync</string>
  <key>CFBundleIdentifier</key><string>local.codex.sidebar.sync</string>
  <key>CFBundleName</key><string>Codex Sidebar Sync</string>
  <key>CFBundleDisplayName</key><string>Codex 侧栏同步</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>CFBundleShortVersionString</key><string>0.2.0</string>
  <key>CFBundleVersion</key><string>2</string>
  <key>LSMinimumSystemVersion</key><string>13.0</string>
  <key>NSHighResolutionCapable</key><true/>
</dict>
</plist>
PLIST
executable="$app/Contents/MacOS/CodexSidebarSync"
if [ "${2:-}" = universal ]; then
  temp=$(mktemp -d)
  trap 'rm -rf "$temp"' EXIT
  for architecture in arm64 x86_64; do
    xcrun swiftc -target "$architecture-apple-macos13.0" -O "$project_dir/macos/main.swift" -o "$temp/$architecture"
  done
  xcrun lipo -create "$temp/arm64" "$temp/x86_64" -output "$executable"
else
  architecture=$(uname -m)
  xcrun swiftc -target "$architecture-apple-macos13.0" -O "$project_dir/macos/main.swift" -o "$executable"
fi
codesign --force --sign - "$app"
printf '%s\n' "$app"
