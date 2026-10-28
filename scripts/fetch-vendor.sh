#!/usr/bin/env bash
# Copy the pinned browser libraries into the package so the UI never loads
# code from a CDN. Rerun after bumping a version below.
set -euo pipefail

NOVNC=1.7.0
XTERM=6.0.0
XTERM_FIT=0.11.0
BARLOW=5.3.0

root="$(cd "$(dirname "$0")/.." && pwd)"
dest="$root/src/ssh_portal/static/vendor"
work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT

cd "$work"
npm pack --silent "@novnc/novnc@$NOVNC" "@xterm/xterm@$XTERM" "@xterm/addon-fit@$XTERM_FIT" \
    "@fontsource/barlow@$BARLOW" "@fontsource/barlow-semi-condensed@$BARLOW" >/dev/null
for f in *.tgz; do mkdir "${f%.tgz}" && tar -xzf "$f" -C "${f%.tgz}"; done

rm -rf "$dest"
mkdir -p "$dest/novnc" "$dest/xterm" "$dest/fonts"

cp -r "novnc-novnc-$NOVNC/package/core" "novnc-novnc-$NOVNC/package/vendor" "$dest/novnc/"
cp "novnc-novnc-$NOVNC/package/LICENSE.txt" "$dest/novnc/"

cp "xterm-xterm-$XTERM/package/lib/xterm.mjs" "xterm-xterm-$XTERM/package/css/xterm.css" "$dest/xterm/"
cp "xterm-xterm-$XTERM/package/LICENSE" "$dest/xterm/LICENSE"
cp "xterm-addon-fit-$XTERM_FIT/package/lib/addon-fit.mjs" "$dest/xterm/"

for family in barlow barlow-semi-condensed; do
    for weight in 400 500 600; do
        cp "fontsource-$family-$BARLOW/package/files/$family-latin-$weight-normal.woff2" "$dest/fonts/"
    done
done
cp "fontsource-barlow-$BARLOW/package/LICENSE" "$dest/fonts/LICENSE-OFL.txt"

echo "Vendored noVNC $NOVNC, xterm $XTERM, addon-fit $XTERM_FIT, Barlow $BARLOW into $dest"
