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
 *
 * Task U30 polish, both verified against the same shipped source rather than
 * AnyPicker's docs:
 * - the minute wheel's step is the real `intervals` option (an {h,m,s} object,
 *   default {h:1,m:1,s:1}; no separate minuteInterval/step key exists).
 * - which of the Date/Time columns opens active has no public init option at
 *   all - the source hardcodes tmp.sDateTimeTab="date" and, on every show,
 *   calls this._setDateTimeTabs(this.tmp.sDateTimeTab) *after* running the
 *   public onShowPicker(this) hook. Setting this.tmp.sDateTimeTab inside that
 *   hook is what that later call actually reads, so it opens on Time without
 *   touching the vendored file.
 *
 * Task U34 numeric scroll wheels (growth's Value field, temperature's temp_c):
 * AnyPicker's extra.sArrModes is exactly ["select","datetime"] - there is no
 * separate range/step mode, so "select" is the only fit. Its manual dataSource
 * is NOT a bare array of values, however plausible that looks - verified
 * against __setComponentsOfSelect, the source's own auto-derive branch that
 * builds this same shape from a plain <select>'s <option>s: one entry per
 * wheel column, `dataSource: [{component, data: [{val, label, selected}, ...]}]`,
 * with a matching `components: [{component}, ...]` sizing the picker (its
 * default is null, read only via .length, so a select-mode init with no
 * `components` renders nothing). `inputElement` and the "destroy" plugin
 * action (both used below to rebuild the wheel when Measure changes) are
 * likewise real options/actions, confirmed directly in the source rather than
 * assumed from AnyPicker's docs, per the same caveat U29/U30 already needed.
 */
(function () {
  "use strict";

  var DATETIME_FORMAT = "yyyy-MM-dd HH:mm";
  var MINUTE_INTERVAL = { h: 1, m: 5, s: 1 };

  function openToTimeTab() {
    this.tmp.sDateTimeTab = "time";
  }

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
        intervals: MINUTE_INTERVAL,
        onShowPicker: openToTimeTab,
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
        intervals: MINUTE_INTERVAL,
        onShowPicker: openToTimeTab,
        onSetOutput: function (sOutput) {
          nativeInput.value = sOutput.replace(" ", "T");
        },
      });
    });
  }

  function rangeValues(min, max, step) {
    var out = [];
    for (var v = min; v <= max; v += step) out.push(String(v));
    return out;
  }

  function decimalRangeValues(minTenths, maxTenths) {
    var out = [];
    for (var t = minTenths; t <= maxTenths; t++) out.push((t / 10).toFixed(1));
    return out;
  }

  function numericWheelRows(values, unit, current) {
    return values.map(function (v) {
      return { val: v, label: v + " " + unit, selected: v === current };
    });
  }

  // Binds (or rebuilds, on Measure change) a select-mode wheel over
  // `nativeInput`. `spec()` returns {values, unit, defaultValue} - called
  // fresh each time so the growth panel's Measure select can swap the
  // wheel's range/unit without a page reload. Returns the rebuild function.
  function bindNumericWheel(nativeInput, spec) {
    if (!anyPickerAvailable()) return function () {};
    var picker = null;

    function apply() {
      var s = spec();
      var current = s.values.indexOf(nativeInput.value) !== -1 ? nativeInput.value : s.defaultValue;
      nativeInput.value = current;

      if (picker) {
        jQuery(picker).AnyPicker("destroy");
      } else {
        picker = makePickerInput("");
        nativeInput.insertAdjacentElement("afterend", picker);
        nativeInput.type = "hidden";
      }
      picker.value = current + " " + s.unit;

      jQuery(picker).AnyPicker({
        mode: "select",
        theme: "iOS",
        inputElement: picker,
        componentsCoverFullWidth: true,
        components: [{ component: 1 }],
        dataSource: [{ component: 1, data: numericWheelRows(s.values, s.unit, current) }],
        onSetOutput: function (sOutput, selectedValues) {
          nativeInput.value = selectedValues.values[0].val;
        },
      });
    }

    apply();
    return apply;
  }

  var GROWTH_WHEELS = {
    weight: { values: rangeValues(200, 25000, 25), unit: "g", defaultValue: "3500" },
    length: { values: rangeValues(250, 1200, 5), unit: "mm", defaultValue: "500" },
    head_circ: { values: rangeValues(250, 600, 5), unit: "mm", defaultValue: "350" },
  };

  var TEMP_WHEEL = { values: decimalRangeValues(340, 420), unit: "°C", defaultValue: "37.0" };

  function bindGrowthValueWheel(root) {
    if (!anyPickerAvailable()) return;
    var scope = root && root.querySelectorAll ? root : document;
    var measureSelect = scope.querySelector("#growth-measure");
    var valueInput = scope.querySelector("#growth-value");
    if (!measureSelect || !valueInput || valueInput.dataset.apBound) return;
    valueInput.dataset.apBound = "1";

    var rebuild = bindNumericWheel(valueInput, function () {
      return GROWTH_WHEELS[measureSelect.value] || GROWTH_WHEELS.weight;
    });
    measureSelect.addEventListener("change", rebuild);
  }

  function bindTemperatureWheel(root) {
    if (!anyPickerAvailable()) return;
    var scope = root && root.querySelectorAll ? root : document;
    var tempInput = scope.querySelector("#temp-c");
    if (!tempInput || tempInput.dataset.apBound) return;
    tempInput.dataset.apBound = "1";
    bindNumericWheel(tempInput, function () {
      return TEMP_WHEEL;
    });
  }

  function bindPickers(root) {
    bindPanelPickers(root);
    bindHistoryPickers(root);
    bindGrowthValueWheel(root);
    bindTemperatureWheel(root);
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
