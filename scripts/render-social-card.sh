#!/bin/sh
set -eu
root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
browser=${CHROME:-google-chrome}
expected='Google Chrome 143.0.7499.109'
actual=$($browser --version | sed 's/[[:space:]]*$//')
test "$actual" = "$expected" || { echo "renderer mismatch: expected $expected, got $actual" >&2; exit 1; }
profile=$(mktemp -d)
trap 'rm -rf "$profile"' EXIT
"$browser" --headless=new --hide-scrollbars --disable-gpu --no-sandbox --user-data-dir="$profile" --window-size=1200,630 --screenshot="$root/assets/og-card.png" "file://$root/assets/og-card.src.html"
test "$(file -b "$root/assets/og-card.png")" = 'PNG image data, 1200 x 630, 8-bit/color RGB, non-interlaced'
sha256sum "$root/assets/og-card.png"
