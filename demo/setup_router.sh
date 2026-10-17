#!/usr/bin/env bash
# Download the autorouter into runs/_tools (no system install): a Temurin Java 25 JRE
# (checksum-verified) and Freerouting. Needed for routed boards in the 3D view.
#   demo/setup_router.sh
set -euo pipefail
cd "$(dirname "$0")/.."
T=runs/_tools
FR_VERSION=2.4.1
JAVA_MAJOR=25
mkdir -p "$T"
case "$(uname -s)-$(uname -m)" in
  Darwin-arm64) OS=mac; ARCH=aarch64 ;;
  Darwin-x86_64) OS=mac; ARCH=x64 ;;
  Linux-x86_64) OS=linux; ARCH=x64 ;;
  Linux-aarch64) OS=linux; ARCH=aarch64 ;;
  *) echo "unsupported platform"; exit 1 ;;
esac
if [ ! -x "$T/jre/Contents/Home/bin/java" ] && [ ! -x "$T/jre/bin/java" ]; then
  META=$(curl -fsSL "https://api.adoptium.net/v3/assets/latest/$JAVA_MAJOR/hotspot?architecture=$ARCH&image_type=jre&os=$OS&vendor=eclipse")
  URL=$(echo "$META" | python3 -c "import json,sys; print(json.load(sys.stdin)[0]['binary']['package']['link'])")
  SUM=$(echo "$META" | python3 -c "import json,sys; print(json.load(sys.stdin)[0]['binary']['package']['checksum'])")
  mkdir -p "$T/dl" && curl -fsSL -o "$T/dl/jre.tar.gz" "$URL"
  echo "$SUM  $T/dl/jre.tar.gz" | shasum -a 256 -c -
  rm -rf "$T/jre" && mkdir -p "$T/jre" && tar -xzf "$T/dl/jre.tar.gz" -C "$T/jre" --strip-components=1 && rm -rf "$T/dl"
fi
JAR="$T/freerouting-$FR_VERSION.jar"
[ -s "$JAR" ] || curl -fsSL -o "$JAR" "https://github.com/freerouting/freerouting/releases/download/v$FR_VERSION/freerouting-$FR_VERSION.jar"
JAVA=$( [ -x "$T/jre/Contents/Home/bin/java" ] && echo "$T/jre/Contents/Home/bin/java" || echo "$T/jre/bin/java")
"$JAVA" -version 2>&1 | head -1
echo "Freerouting $FR_VERSION ready in $T"
