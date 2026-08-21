/* Combined iOS-style date+time picker (task U29) over two existing controls:
 * - the quick-entry panel's <input type=time name=ts> (still posts "HH:MM"
 *   only, unchanged - the server's _panel_ts (api.py) always combines that
 *   with *today*, so a picked date can only reach the server via a follow-up
 *   /api/adjust-time call, using its own unchanged datetime.fromisoformat
 *   parsing, fired right after a successful Save)
 * - /history's <input type=datetime-local name=ts>, whose /api/adjust-time
 *   target already accepts a full date+time string unchanged, so the picker
 *   there just writes straight back into that same field
 *
 * Both are progressive enhancement (task U19/U22's no-JS contract carried
 * forward): the native input stays in the DOM (switched to type=hidden once
 * the picker binds, same trick U22's wheel-picker used) so a page where this
 * script, jQuery or AnyPicker never loads still has a working native control
 * that posts a value the server already accepts.
 *
 * Vendored library: AnyPicker (task U29, replaces U22's wheel-picker), a
 * jQuery plugin - Mark Baber explicitly authorised the jQuery dependency.
 * AnyPicker's own README documents CDN usage only; its actual callback name
 * (onSetOutput, not e.g. onSet/afterDateSet) and default inputChangeEvent
 * ("onSet" - only fires on the picker's own Set button, not while scrolling)
 * were verified directly against the shipped dist/anypicker.min.js source.
 */
(function () {
  "use strict";

  var DATETIME_FORMAT = "yyyy-MM-dd HH:mm";

  function pad2(n) {
    return String(n).padStart(2, "0");
  }

  function todayStr() {
    var d = new Date();
    return d.getFullYear() + "-" + pad2(d.getMonth() + 1) + "-" + pad2(d.getDate());
  }

  function combinedFromTime(timeValue) {
    var hm = /^([0-1]?\d|2[0-3]):([0-5]\d)$/.test(timeValue || "") ? timeValue : "00:00";
    return todayStr() + " " + hm;
  }

  function anyPickerAvailable() {
    return typeof jQuery !== "undefined" && typeof jQuery.fn.AnyPicker === "function";
  }

  function makePickerInput(initialValue) {
    var picker = document.createElement("input");
    picker.type = "text";
    picker.readOnly = true;
    picker.className = "ap-ts-picker";
    picker.value = initialValue;
    return picker;
  }

  // Quick-entry panel: <input type=time name=ts> gains a date column. The
  // native field keeps posting "HH:MM" (parsed by api.py's _panel_ts,
  // unchanged); the full picked date+time is stashed on the form (dataset.
  // tsCombined) for the submit/htmx:afterSwap listeners below to correct via
  // /api/adjust-time once Save succeeds.
  function bindPanelPickers(root) {
    if (!anyPickerAvailable()) return;
    var scope = root && root.querySelectorAll ? root : document;
    var inputs = scope.querySelectorAll('input[type="time"][name="ts"]');
    inputs.forEach(function (nativeInput) {
      if (nativeInput.dataset.apBound) return;
      nativeInput.dataset.apBound = "1";

      var form = nativeInput.closest("form");
      var initial = combinedFromTime(nativeInput.value);
      if (form) form.dataset.tsCombined = initial;

      var picker = makePickerInput(initial);
      nativeInput.insertAdjacentElement("afterend", picker);
      nativeInput.type = "hidden";

      jQuery(picker).AnyPicker({
        mode: "datetime",
        theme: "iOS",
        dateTimeFormat: DATETIME_FORMAT,
        selectedDate: initial,
        onSetOutput: function (sOutput) {
          var timePart = sOutput.split(" ")[1];
          if (timePart) nativeInput.value = timePart;
          if (form) form.dataset.tsCombined = sOutput;
        },
      });
    });
  }

  // /history inline edit: every <input type=datetime-local> (the adjust-time
  // row's ts field, and the sleep row's ts_end "Set wake" field) already
  // posts a full date+time string that /api/adjust-time and /api/edit-field
  // parse unchanged via datetime.fromisoformat - the picker just writes
  // straight back into it.
  function bindHistoryPickers(root) {
    if (!anyPickerAvailable()) return;
    var scope = root && root.querySelectorAll ? root : document;
    var inputs = scope.querySelectorAll('input[type="datetime-local"]');
    inputs.forEach(function (nativeInput) {
      if (nativeInput.dataset.apBound) return;
      nativeInput.dataset.apBound = "1";

      var rawValue = (nativeInput.value || "").replace("T", " ");
      var initial = /^\d{4}-\d{2}-\d{2} \d{2}:\d{2}$/.test(rawValue)
        ? rawValue
        : todayStr() + " " + new Date().toTimeString().slice(0, 5);
      var picker = makePickerInput(initial);
      nativeInput.insertAdjacentElement("afterend", picker);
      nativeInput.type = "hidden";

      jQuery(picker).AnyPicker({
        mode: "datetime",
        theme: "iOS",
        dateTimeFormat: DATETIME_FORMAT,
        selectedDate: initial,
        onSetOutput: function (sOutput) {
          nativeInput.value = sOutput.replace(" ", "T");
        },
      });
    });
  }

  function bindPickers(root) {
    bindPanelPickers(root);
    bindHistoryPickers(root);
  }

  // Exactly one panel form can be open at a time (quick_entry.html's
  // open_panel Jinja conditional). If Save was submitted with a picked date
  // other than today, correct it via the same /api/adjust-time route
  // /history already uses, unchanged, once we know the new event's id from
  // the toast response (data-table/data-event-id, per api.py's _toast()).
  // Consumed exactly once per submit so an unrelated later toast (Undo,
  // Sleep, Express) can never replay a stale correction.
  var pendingCorrection = null;

  document.body &&
    document.body.addEventListener(
      "submit",
      function (evt) {
        var form = evt.target;
        if (!(form instanceof HTMLFormElement) || !form.closest("#panel")) return;
        var combined = form.dataset.tsCombined;
        pendingCorrection = combined && combined.slice(0, 10) !== todayStr() ? combined : null;
      },
      true
    );

  document.body &&
    document.body.addEventListener("htmx:afterSwap", function (evt) {
      bindPickers(evt.target);

      if (evt.target.id !== "toast" || !pendingCorrection) return;
      var combined = pendingCorrection;
      pendingCorrection = null;
      var toastEl = evt.target.querySelector(".toast[data-event-id]");
      if (!toastEl) return;
      var toastWrap = evt.target;
      fetch("/api/adjust-time", {
        method: "POST",
        headers: { "Content-Type": "application/x-www-form-urlencoded" },
        body: new URLSearchParams({
          table: toastEl.dataset.table,
          event_id: toastEl.dataset.eventId,
          ts: combined,
        }),
      })
        .then(function (resp) {
          if (!resp.ok) throw new Error("adjust-time " + resp.status);
        })
        .catch(function () {
          // The event is already logged (at today's date/now) - only the
          // backdate correction failed, so surface that rather than let it
          // silently diverge from what the picker showed.
          toastWrap.innerHTML =
            '<div class="toast err">Logged, but the picked date could not be saved - fix it in History</div>';
        });
    });

  document.addEventListener("DOMContentLoaded", function () {
    bindPickers(document);
  });
})();
