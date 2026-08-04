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

  function start() {
    var d = document.getElementById("dailychart");
    if (d) daily(d);
    var s = document.getElementById("sleepchart");
    if (s) sleep(s);
  }
  if (document.readyState === "complete") start();
  else window.addEventListener("load", start);
})();
