# Vendored assets

`htmx.min.js` is fetched by `scripts/vendor_assets.sh` (pinned version + SHA-256).
It is **not** committed, and the app degrades gracefully without it: every quick
action is a real `<form method="post">`, so taps still log with a full page
round-trip when htmx is absent or JavaScript is off.
