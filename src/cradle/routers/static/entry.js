/* Upgrade every quick-entry panel's <input type=time name=ts> into an
 * iPhone-style scroll-wheel picker (task U22, vendored library: WheelPicker,
 * static/vendor/wheelpicker.min.js). The native <input type=time> stays the
 * element the form actually posts - WheelPicker's hiddenInput option keeps
 * it in the DOM (type=hidden) and writes back the same "HH:MM" string a
 * browser would have submitted natively, so a page where this script never
 * loads (or wheelpicker.min.js fails to load) still has a working native
 * time control that posts a value /api/feed and /api/nappy already accept.
 */
(function () {
  "use strict";

  function pad(n) {
    return String(n).padStart(2, "0");
  }

  var HOURS = [];
  for (var h = 0; h < 24; h++) HOURS.push(pad(h));
  var MINUTES = [];
  for (var m = 0; m < 60; m++) MINUTES.push(pad(m));

  function splitTime(value) {
    var parts = (value || "").split(":");
    return [HOURS.includes(parts[0]) ? parts[0] : HOURS[0], MINUTES.includes(parts[1]) ? parts[1] : MINUTES[0]];
  }

  function bindPickers(root) {
    if (typeof WheelPicker === "undefined") return;
    var scope = root && root.querySelectorAll ? root : document;
    var inputs = scope.querySelectorAll('input[type="time"][name="ts"]');
    inputs.forEach(function (input) {
      if (input.dataset.wheelBound) return;
      input.dataset.wheelBound = "1";
      new WheelPicker({
        el: input,
        hiddenInput: true,
        rows: 5,
        data: [HOURS, MINUTES],
        value: splitTime(input.value),
        // WheelPicker's own README documents these three option names
        // backwards from what dist/wheelpicker.min.js actually reads -
        // verified directly against the shipped source (not the docs):
        // parseValue splits the control's current text into a column-value
        // array (used once, at construction); formatValue joins the picked
        // array back into the visible clone's text; formatHiddenValue joins
        // it into the real posted `el` (there is no parseHiddenValue key -
        // passing that name is silently ignored and the untouched default,
        // a space-joined string, would go out over the wire instead).
        parseValue: splitTime,
        formatValue: function (value) {
          return value.join(":");
        },
        formatHiddenValue: function (value) {
          return value.join(":");
        },
      });
    });
  }

  document.addEventListener("DOMContentLoaded", function () {
    bindPickers(document);
  });
  document.body && document.body.addEventListener("htmx:afterSwap", function (evt) {
    bindPickers(evt.target);
  });
})();
