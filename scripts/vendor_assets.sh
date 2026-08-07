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
HTMX_SHA256="e209dda5c8235479f3166defc7750e1dbcd5a5c1808b7792fc2e6733768fb447"   # populate on first vendoring, then CI verifies it

curl -fsSL "$HTMX_URL" -o "$DEST/htmx.min.js"
if [ -n "$HTMX_SHA256" ]; then
  echo "${HTMX_SHA256}  $DEST/htmx.min.js" | sha256sum -c -
else
  echo "WARNING: HTMX_SHA256 unset. Record it now:"
  sha256sum "$DEST/htmx.min.js"
fi

PLOTLY_VERSION=2.35.2
PLOTLY_URL="https://cdn.plot.ly/plotly-${PLOTLY_VERSION}.min.js"
PLOTLY_SHA256="6d21266ce1bd7d9e5ab4e115989c70c20de0382fd973a8f26ab58619eba4d603"

curl -fsSL "$PLOTLY_URL" -o "$DEST/plotly.min.js"
if [ -n "$PLOTLY_SHA256" ]; then
  echo "${PLOTLY_SHA256}  $DEST/plotly.min.js" | sha256sum -c -
else
  echo "WARNING: PLOTLY_SHA256 unset. Record it now:"
  sha256sum "$DEST/plotly.min.js"
fi

echo "vendored htmx ${HTMX_VERSION} + plotly ${PLOTLY_VERSION} -> $DEST"
