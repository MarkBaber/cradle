/* Growth chart with centile playback (tasks U5, C3).
   No-ops without Plotly, so the page still renders its table fallback. */
(function () {
  var el = document.getElementById("chart");
  if (!el || !el.dataset.ready) return;

  function draw() {
    if (typeof Plotly === "undefined") return;
    fetch("/api/charts/" + el.dataset.measure)
      .then(function (r) { return r.json(); })
      .then(function (d) {
        if (d.unavailable_reason) return;
        var traces = Object.keys(d.curves).sort(function (a, b) { return a - b; })
          .map(function (k) {
            return {
              x: d.ages, y: d.curves[k], mode: "lines", name: k,
              line: { width: k === "50" ? 1.6 : 0.8, color: "#3d4b5c" },
              hoverinfo: "name"
            };
          });
        traces.push({
          x: d.trajectory.map(function (p) { return p[0]; }),
          y: d.trajectory.map(function (p) { return p[1]; }),
          mode: "lines+markers", name: d.measure,
          line: { color: "#5aa9e6", width: 2.5 }, marker: { size: 7 }
        });
        var layout = {
          paper_bgcolor: "#0d1117", plot_bgcolor: "#0d1117",
          font: { color: "#8b949e", size: 11 },
          margin: { l: 46, r: 8, t: 8, b: 34 }, showlegend: false,
          xaxis: { title: "age (days)", gridcolor: "#26303d", zeroline: false },
          yaxis: { title: d.unit, gridcolor: "#26303d", zeroline: false }
        };
        var frames = d.frames.map(function (n) {
          return {
            name: String(n),
            data: traces.slice(0, -1).concat([{
              x: d.trajectory.slice(0, n).map(function (p) { return p[0]; }),
              y: d.trajectory.slice(0, n).map(function (p) { return p[1]; })
            }])
          };
        });
        Plotly.newPlot(el, traces, layout, { displayModeBar: false, responsive: true })
          .then(function () {
            if (frames.length < 2) return;
            Plotly.addFrames(el, frames);
            var btn = document.createElement("button");
            btn.textContent = "▶ Watch them grow";
            btn.className = "play";
            btn.onclick = function () {
              Plotly.animate(el, null, {
                frame: { duration: 420, redraw: false },
                transition: { duration: 260 }
              });
            };
            el.parentNode.insertBefore(btn, el.nextSibling);
          });
      });
  }
  if (document.readyState === "complete") draw();
  else window.addEventListener("load", draw);
})();
