/*
 * Small daily lines for a page of property cards (the overview of many Search Console
 * properties). Each [data-spark] element names a JSON script holding a list of
 * {date, clicks, impressions}; clicks and impressions are drawn on their own scales,
 * 2px wide, in the report's metric colours.
 */
(function () {
  "use strict";
  var SVG = "http://www.w3.org/2000/svg";
  function draw(host, days) {
    var W = host.clientWidth, H = host.clientHeight, pad = 4;
    if (!W || !H || !days.length) return;
    var s = document.createElementNS(SVG, "svg");
    s.setAttribute("viewBox", "0 0 " + W + " " + H);
    s.setAttribute("width", W);
    s.setAttribute("height", H);
    s.setAttribute("aria-hidden", "true");
    ["impressions", "clicks"].forEach(function (m) {
      var max = Math.max.apply(null, days.map(function (d) { return d[m]; })) || 1;
      var d = days.map(function (day, i) {
        var x = pad + (days.length === 1 ? (W - 2 * pad) / 2 : i / (days.length - 1) * (W - 2 * pad));
        var y = pad + (H - 2 * pad) * (1 - day[m] / max);
        return (i ? "L" : "M") + x.toFixed(1) + "," + y.toFixed(1);
      }).join("");
      var p = document.createElementNS(SVG, "path");
      p.setAttribute("d", d);
      p.setAttribute("class", "gsc-chart__line gsc-chart__line--" + m);
      s.appendChild(p);
    });
    host.replaceChildren(s);
  }
  document.querySelectorAll("[data-spark]").forEach(function (host) {
    var node = document.getElementById(host.dataset.spark);
    var days = node ? JSON.parse(node.textContent || "[]") : [];
    draw(host, days);
    if (window.ResizeObserver) new ResizeObserver(function () { draw(host, days); }).observe(host);
  });
})();
