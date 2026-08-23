/* Domain timeseries (tasks C1, C2). No-ops without Plotly; every page that
   uses this also renders the same numbers as a table. */
(function () {
  var DARK = {
    paper_bgcolor: "#0d1117", plot_bgcolor: "#0d1117",
    font: { color: "#8b949e", size: 11 },
    margin: { l: 40, r: 10, t: 8, b: 34 },
    xaxis: { gridcolor: "#26303d", zeroline: false },
    yaxis: { gridcolor: "#26303d", zeroline: false }
  };

  function plot(el, traces, extra) {
    if (typeof Plotly === "undefined") return;
    var layout = JSON.parse(JSON.stringify(DARK));
    Object.keys(extra || {}).forEach(function (k) { layout[k] = extra[k]; });
    Plotly.newPlot(el, traces, layout, { displayModeBar: false, responsive: true });
  }

  function daily(el) {
    fetch("/api/series/daily?days=" + (el.dataset.days || 14))
      .then(function (r) { return r.json(); })
      .then(function (d) {
        plot(el, [
          { x: d.days, y: d.feeds, type: "bar", name: "feeds",
            marker: { color: "#5aa9e6" } },
          { x: d.days, y: d.wet, type: "bar", name: "wet",
            marker: { color: "#6ea8fe" } },
          { x: d.days, y: d.dirty, type: "bar", name: "dirty",
            marker: { color: "#c99a5b" } },
          { x: d.days, y: d.sleep_hours, type: "scatter", mode: "lines",
            name: "sleep h", yaxis: "y2", line: { color: "#9b8cf0", width: 2 } }
        ], {
          barmode: "group", showlegend: true,
          legend: { orientation: "h", y: 1.18 },
          yaxis2: { overlaying: "y", side: "right", gridcolor: "transparent" }
        });
      });
  }

  function sleep(el) {
    fetch("/api/series/daily?days=" + (el.dataset.days || 14))
      .then(function (r) { return r.json(); })
      .then(function (d) {
        plot(el, [
          { x: d.days, y: d.longest_sleep_hours, type: "scatter",
            mode: "lines+markers", name: "longest sleep",
            line: { color: "#9b8cf0", width: 2.5 } },
          { x: d.days, y: d.night_wakings, type: "bar", name: "night wakings",
            marker: { color: "#26303d" } }
        ], { showlegend: true, legend: { orientation: "h", y: 1.18 } });
      });
  }

  var MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];

  function formatDate(isoStr) {
    if (!isoStr) return "";
    var parts = isoStr.split("-");
    if (parts.length < 3) return isoStr;
    var day = parseInt(parts[2], 10);
    var monthIdx = parseInt(parts[1], 10) - 1;
    return day + " " + (MONTHS[monthIdx] || "");
  }

  function hasSomeValues(arr) {
    if (!arr || !arr.length) return false;
    for (var i = 0; i < arr.length; i++) {
      if (arr[i] !== null && arr[i] !== undefined) return true;
    }
    return false;
  }

  function renderPerMetricCharts(feedsEl, wetEl, dirtyEl, sleepTargetEl) {
    var sampleEl = feedsEl || wetEl || dirtyEl || sleepTargetEl;
    var days = sampleEl.dataset.days || 14;
    fetch("/api/series/daily?days=" + days)
      .then(function (r) { return r.json(); })
      .then(function (d) {
        if (!d.days || !d.days.length) return;
        var indices = [];
        for (var i = 0; i < d.days.length; i++) indices.push(i);
        var dateLabels = d.days.map(formatDate);
        var ageLabels = (d.age_days || []).map(function (a) { return "day " + a; });
        var hoverTexts = indices.map(function (i) {
          var dt = dateLabels[i] || d.days[i];
          var age = (d.age_days && d.age_days[i] !== undefined) ? (" (day " + d.age_days[i] + ")") : "";
          return dt + age;
        });

        function makeDualAxisLayout(titleText) {
          return {
            title: titleText ? { text: titleText, font: { color: "#8b949e", size: 12 }, x: 0.01, y: 0.98 } : undefined,
            margin: { l: 40, r: 10, t: 32, b: 34 },
            showlegend: true,
            legend: { orientation: "h", y: 1.25 },
            xaxis: {
              gridcolor: "#26303d",
              zeroline: false,
              tickvals: indices,
              ticktext: dateLabels
            },
            xaxis2: {
              overlaying: "x",
              side: "top",
              tickvals: indices,
              ticktext: ageLabels,
              gridcolor: "transparent",
              zeroline: false
            }
          };
        }

        if (feedsEl) {
          var feedTraces = [
            { x: indices, y: d.bottle_ml, type: "bar", name: "bottle ml",
              marker: { color: "#5aa9e6" }, text: hoverTexts,
              hovertemplate: "%{text}<br>bottle: %{y} ml<extra></extra>" }
          ];
          if (d.targets && hasSomeValues(d.targets.feed_volume_ml)) {
            feedTraces.push({
              x: indices, y: d.targets.feed_volume_ml, type: "scatter", mode: "lines",
              name: "target volume", line: { color: "#5aa9e6", dash: "dash", width: 2 },
              connectgaps: false, text: hoverTexts,
              hovertemplate: "%{text}<br>target: %{y} ml<extra></extra>"
            });
          }
          plot(feedsEl, feedTraces, makeDualAxisLayout("Bottle feed volume (ml)"));
        }

        if (wetEl) {
          var wetTraces = [
            { x: indices, y: d.wet, type: "bar", name: "wet nappies",
              marker: { color: "#6ea8fe" }, text: hoverTexts,
              hovertemplate: "%{text}<br>wet: %{y}<extra></extra>" }
          ];
          if (d.targets && hasSomeValues(d.targets.wet_min)) {
            wetTraces.push({
              x: indices, y: d.targets.wet_min, type: "scatter", mode: "lines",
              name: "recommended min", line: { color: "rgba(110, 168, 254, 0.6)", dash: "dash", width: 1.5 },
              showlegend: false, connectgaps: false, text: hoverTexts,
              hovertemplate: "%{text}<br>rec min: %{y}<extra></extra>"
            });
            if (hasSomeValues(d.targets.wet_max)) {
              wetTraces.push({
                x: indices, y: d.targets.wet_max, type: "scatter", mode: "lines",
                fill: "tonexty", fillcolor: "rgba(110, 168, 254, 0.15)",
                name: "recommended range", line: { color: "rgba(110, 168, 254, 0.6)", dash: "dash", width: 1.5 },
                connectgaps: false, text: hoverTexts,
                hovertemplate: "%{text}<br>rec max: %{y}<extra></extra>"
              });
            }
          }
          plot(wetEl, wetTraces, makeDualAxisLayout("Wet nappies"));
        }

        if (dirtyEl) {
          var dirtyTraces = [
            { x: indices, y: d.dirty, type: "bar", name: "dirty nappies",
              marker: { color: "#c99a5b" }, text: hoverTexts,
              hovertemplate: "%{text}<br>dirty: %{y}<extra></extra>" }
          ];
          if (d.targets && hasSomeValues(d.targets.dirty_min)) {
            dirtyTraces.push({
              x: indices, y: d.targets.dirty_min, type: "scatter", mode: "lines",
              name: "recommended min", line: { color: "rgba(201, 154, 91, 0.6)", dash: "dash", width: 1.5 },
              showlegend: false, connectgaps: false, text: hoverTexts,
              hovertemplate: "%{text}<br>rec min: %{y}<extra></extra>"
            });
            if (hasSomeValues(d.targets.dirty_max)) {
              dirtyTraces.push({
                x: indices, y: d.targets.dirty_max, type: "scatter", mode: "lines",
                fill: "tonexty", fillcolor: "rgba(201, 154, 91, 0.15)",
                name: "recommended range", line: { color: "rgba(201, 154, 91, 0.6)", dash: "dash", width: 1.5 },
                connectgaps: false, text: hoverTexts,
                hovertemplate: "%{text}<br>rec max: %{y}<extra></extra>"
              });
            }
          }
          plot(dirtyEl, dirtyTraces, makeDualAxisLayout("Dirty nappies"));
        }

        if (sleepTargetEl) {
          var sleepTraces = [];
          if (d.targets && hasSomeValues(d.targets.sleep_min_hours)) {
            sleepTraces.push({
              x: indices, y: d.targets.sleep_min_hours, type: "scatter", mode: "lines",
              name: "recommended min", line: { color: "rgba(155, 140, 240, 0.6)", dash: "dash", width: 1.5 },
              showlegend: false, connectgaps: false, text: hoverTexts,
              hovertemplate: "%{text}<br>rec min: %{y}h<extra></extra>"
            });
            if (hasSomeValues(d.targets.sleep_max_hours)) {
              sleepTraces.push({
                x: indices, y: d.targets.sleep_max_hours, type: "scatter", mode: "lines",
                fill: "tonexty", fillcolor: "rgba(155, 140, 240, 0.15)",
                name: "recommended sleep", line: { color: "rgba(155, 140, 240, 0.6)", dash: "dash", width: 1.5 },
                connectgaps: false, text: hoverTexts,
                hovertemplate: "%{text}<br>rec max: %{y}h<extra></extra>"
              });
            }
          }
          sleepTraces.push({
            x: indices, y: d.sleep_hours, type: "scatter", mode: "lines+markers",
            name: "sleep hours", line: { color: "#9b8cf0", width: 2.5 }, marker: { size: 6 },
            text: hoverTexts, hovertemplate: "%{text}<br>sleep: %{y}h<extra></extra>"
          });
          plot(sleepTargetEl, sleepTraces, makeDualAxisLayout("Total sleep (hours)"));
        }
      });
  }

  function start() {
    var d = document.getElementById("dailychart");
    if (d) daily(d);
    var s = document.getElementById("sleepchart");
    if (s) sleep(s);

    var feedsEl = document.getElementById("feedschart");
    var wetEl = document.getElementById("wetchart");
    var dirtyEl = document.getElementById("dirtychart");
    var sleepTargetEl = document.getElementById("sleeptargetchart");

    if (feedsEl || wetEl || dirtyEl || sleepTargetEl) {
      renderPerMetricCharts(feedsEl, wetEl, dirtyEl, sleepTargetEl);
    }
  }
  if (document.readyState === "complete") start();
  else window.addEventListener("load", start);
})();
