#!/usr/bin/env bash
# Builds "Oak.app" in the repo root: a double-clickable macOS app that runs app.py with this repo's Python.
set -euo pipefail
cd "$(dirname "$0")/.."
ROOT="$(pwd)"
APP="Oak.app"
rm -rf "$APP"
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"
cat > "$APP/Contents/Info.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>CFBundleName</key><string>Oak</string>
  <key>CFBundleDisplayName</key><string>Oak</string>
  <key>CFBundleIdentifier</key><string>local.oak</string>
  <key>CFBundleExecutable</key><string>launch</string>
  <key>CFBundleIconFile</key><string>icon</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>CFBundleVersion</key><string>1.0</string>
  <key>NSHighResolutionCapable</key><true/>
</dict></plist>
PLIST
cat > "$APP/Contents/MacOS/launch" <<LAUNCH
#!/usr/bin/env bash
cd "$ROOT"
exec "$ROOT/.venv/bin/python" "$ROOT/app.py" >> "$ROOT/runs/app.log" 2>&1
LAUNCH
chmod +x "$APP/Contents/MacOS/launch"
mkdir -p runs
# icon: end-on log with growth rings (drawn with the standard library, no image tools needed)
python3 - <<'PY'
import math, struct, zlib
N = 1024
def px(x, y):
    # rounded-square tile
    cx = min(max(x, 190), N - 190); cy = min(max(y, 190), N - 190)
    if math.hypot(x - cx, y - cy) > 190: return (0, 0, 0, 0)
    # capital O: an elliptical ring in cream
    d = math.hypot((x - N / 2) / 290, (y - N / 2) / 350)
    if 0.56 < d < 1.0: return (246, 238, 222, 255)
    return (29, 26, 22, 255)
raw = b"".join(b"\x00" + b"".join(bytes(px(x, y)) for x in range(N)) for y in range(N))
def chunk(t, d): c = struct.pack(">I", len(d)) + t + d; return c + struct.pack(">I", zlib.crc32(t + d) & 0xffffffff)
open("/tmp/oak_icon.png", "wb").write(b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", N, N, 8, 6, 0, 0, 0)) + chunk(b"IDAT", zlib.compress(raw, 6)) + chunk(b"IEND", b""))
PY
ICONSET=/tmp/oak.iconset; rm -rf "$ICONSET"; mkdir -p "$ICONSET"
for s in 16 32 64 128 256 512; do sips -z $s $s /tmp/oak_icon.png --out "$ICONSET/icon_${s}x${s}.png" >/dev/null; sips -z $((s*2)) $((s*2)) /tmp/oak_icon.png --out "$ICONSET/icon_${s}x${s}@2x.png" >/dev/null; done
iconutil -c icns "$ICONSET" -o "$APP/Contents/Resources/icon.icns" || echo "(icon skipped)"
touch "$APP"
echo "Built $ROOT/$APP  (double-click it, or: open \"$APP\")"
