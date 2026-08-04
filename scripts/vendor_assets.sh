#!/usr/bin/env bash
# Fetch pinned front-end assets with checksum verification (task U1).
# The app works without them (progressive enhancement) but htmx makes quick
# entry feel instant. Run once on the Pi, or in a networked CI stage.
set -euo pipefail
cd "$(dirname "$0")/.."
DEST=src/cradle/routers/static/vendor
mkdir -p "$DEST"

HTMX_VERSION=2.0.4
HTMX_URL="https://unpkg.com/htmx.org@${HTMX_VERSION}/dist/htmx.min.js"
HTMX_SHA256=""   # populate on first vendoring, then CI verifies it

curl -fsSL "$HTMX_URL" -o "$DEST/htmx.min.js"
if [ -n "$HTMX_SHA256" ]; then
  echo "${HTMX_SHA256}  $DEST/htmx.min.js" | sha256sum -c -
else
  echo "WARNING: HTMX_SHA256 unset. Record it now:"
  sha256sum "$DEST/htmx.min.js"
fi
PLOTLY_VERSION=2.35.2
curl -fsSL "https://cdn.plot.ly/plotly-${PLOTLY_VERSION}.min.js" -o "$DEST/plotly.min.js"

echo "vendored htmx ${HTMX_VERSION} + plotly ${PLOTLY_VERSION} -> $DEST"
