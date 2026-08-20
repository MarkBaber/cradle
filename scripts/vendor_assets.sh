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

WHEELPICKER_VERSION=1.1.0
WHEELPICKER_JS_URL="https://unpkg.com/wheel-picker@${WHEELPICKER_VERSION}/dist/wheelpicker.min.js"
WHEELPICKER_CSS_URL="https://unpkg.com/wheel-picker@${WHEELPICKER_VERSION}/dist/wheelpicker.min.css"
# Mark Baber explicitly authorised one external JS library for the iPhone-
# style scroll-wheel time picker (task U22, SPEC 6 closed dependency set) -
# kept to this one seam; the modal, toggle buttons and defaults form stay
# plain HTML/CSS/htmx.
WHEELPICKER_JS_SHA256="2d039b7af3616ba8cdd5156e3f386733fe527b0f889725c153670b311897a1d7"
WHEELPICKER_CSS_SHA256="22d775f993fab74b69c52e4b70230db93b363ff39c8142223eaa50f764e98d2b"

curl -fsSL "$WHEELPICKER_JS_URL" -o "$DEST/wheelpicker.min.js"
if [ -n "$WHEELPICKER_JS_SHA256" ]; then
  echo "${WHEELPICKER_JS_SHA256}  $DEST/wheelpicker.min.js" | sha256sum -c -
else
  echo "WARNING: WHEELPICKER_JS_SHA256 unset. Record it now:"
  sha256sum "$DEST/wheelpicker.min.js"
fi

curl -fsSL "$WHEELPICKER_CSS_URL" -o "$DEST/wheelpicker.min.css"
if [ -n "$WHEELPICKER_CSS_SHA256" ]; then
  echo "${WHEELPICKER_CSS_SHA256}  $DEST/wheelpicker.min.css" | sha256sum -c -
else
  echo "WARNING: WHEELPICKER_CSS_SHA256 unset. Record it now:"
  sha256sum "$DEST/wheelpicker.min.css"
fi

echo "vendored htmx ${HTMX_VERSION} + plotly ${PLOTLY_VERSION} + wheel-picker ${WHEELPICKER_VERSION} -> $DEST"
