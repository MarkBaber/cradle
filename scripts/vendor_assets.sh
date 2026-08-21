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

# jQuery + AnyPicker (task U29): superseded the time-only wheel-picker (U22)
# with a combined iOS-style date+time picker for the entry panel and /history
# edit controls. Mark Baber named AnyPicker as a candidate and explicitly
# authorised its jQuery dependency (2026-08-21) - payload size is not a factor
# for this one seam (see docs/SPEC.md §1/§7.1); the modal, toggle buttons and
# defaults form still stay plain HTML/CSS/htmx.
JQUERY_VERSION=3.7.1
JQUERY_URL="https://cdn.jsdelivr.net/npm/jquery@${JQUERY_VERSION}/dist/jquery.min.js"
JQUERY_SHA256="fc9a93dd241f6b045cbff0481cf4e1901becd0e12fb45166a8f17f95823f0b1a"

curl -fsSL "$JQUERY_URL" -o "$DEST/jquery.min.js"
if [ -n "$JQUERY_SHA256" ]; then
  echo "${JQUERY_SHA256}  $DEST/jquery.min.js" | sha256sum -c -
else
  echo "WARNING: JQUERY_SHA256 unset. Record it now:"
  sha256sum "$DEST/jquery.min.js"
fi

ANYPICKER_VERSION=2.0.9
ANYPICKER_JS_URL="https://cdn.jsdelivr.net/npm/anypicker@${ANYPICKER_VERSION}/dist/anypicker.min.js"
ANYPICKER_CSS_URL="https://cdn.jsdelivr.net/npm/anypicker@${ANYPICKER_VERSION}/dist/anypicker-all.min.css"
ANYPICKER_JS_SHA256="9a4148a45206847c7cd72b50c3b9990e7f4dcb02bfefde6b035c20af4990c2e7"
ANYPICKER_CSS_SHA256="48c4fa29d9cd80b40eb55c60456051e31eaef4fd1f59b6e04f8ce0d126c59ebf"

curl -fsSL "$ANYPICKER_JS_URL" -o "$DEST/anypicker.min.js"
if [ -n "$ANYPICKER_JS_SHA256" ]; then
  echo "${ANYPICKER_JS_SHA256}  $DEST/anypicker.min.js" | sha256sum -c -
else
  echo "WARNING: ANYPICKER_JS_SHA256 unset. Record it now:"
  sha256sum "$DEST/anypicker.min.js"
fi

curl -fsSL "$ANYPICKER_CSS_URL" -o "$DEST/anypicker-all.min.css"
if [ -n "$ANYPICKER_CSS_SHA256" ]; then
  echo "${ANYPICKER_CSS_SHA256}  $DEST/anypicker-all.min.css" | sha256sum -c -
else
  echo "WARNING: ANYPICKER_CSS_SHA256 unset. Record it now:"
  sha256sum "$DEST/anypicker-all.min.css"
fi

echo "vendored htmx ${HTMX_VERSION} + plotly ${PLOTLY_VERSION} + jquery ${JQUERY_VERSION} + anypicker ${ANYPICKER_VERSION} -> $DEST"
