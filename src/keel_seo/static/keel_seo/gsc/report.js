/*
 * The Search Console report (keel_seo/gsc/report.html): metric tiles, the line
 * charts, the sortable tables, the tabs and the filter dialogs. Plain JavaScript, no
 * framework, so any host shell can carry it. Reads the payload from the JSON script
 * #gsc-report-data; a page without data still gets working dialogs.
 */
(function () {
  "use strict";

  var root = document.getElementById("gsc-report");
  if (!root) return;
  var dataNode = document.getElementById("gsc-report-data");
  var data = dataNode ? JSON.parse(dataNode.textContent || "null") : null;
  var SVG = "http://www.w3.org/2000/svg";
  var METRICS = {
    clicks: { label: "Clicks", kind: "count" },
    impressions: { label: "Impressions", kind: "count" },
    ctr: { label: "CTR", kind: "ctr" },
    position: { label: "Position", kind: "position" }
  };
  var ORDER = ["clicks", "impressions", "ctr", "position"];
  var STORE = "gsc-report-metrics";

  /* ---- small helpers ---- */

  function el(tag, cls, text) {
    var n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text !== undefined && text !== null) n.textContent = text;
    return n;
  }
  function svg(tag, attrs) {
    var n = document.createElementNS(SVG, tag);
    Object.keys(attrs || {}).forEach(function (k) { n.setAttribute(k, attrs[k]); });
    return n;
  }
  function icon(path) {
    var s = svg("svg", { viewBox: "0 0 24 24", "aria-hidden": "true" });
    s.appendChild(svg("path", { d: path }));
    return s;
  }
  var ICONS = {
    down: "M20 12l-1.41-1.41L13 16.17V4h-2v12.17l-5.58-5.59L4 12l8 8 8-8z",
    up: "M4 12l1.41 1.41L11 7.83V20h2V7.83l5.58 5.59L20 12l-8-8-8 8z",
    prev: "M15.41 7.41 14 6l-6 6 6 6 1.41-1.41L10.83 12z",
    next: "M10 6 8.59 7.41 13.17 12l-4.58 4.59L10 18l6-6z",
    open: "M19 19H5V5h7V3H5a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h14c1.1 0 2-.9 2-2v-7h-2v7zM14 3v2h3.59l-9.83 9.83 1.41 1.41L19 6.41V10h2V3h-7z"
  };
  function compact(v, kind) {
    if (v === null || v === undefined) return "-";
    if (kind === "ctr") return v.toFixed(1) + "%";
    if (kind === "position") return v.toFixed(1);
    var a = Math.abs(v), units = [[1e9, "B"], [1e6, "M"], [1e3, "K"]];
    for (var i = 0; i < units.length; i++) {
      if (a >= units[i][0]) {
        var n = v / units[i][0];
        var d = n < 10 ? 2 : (n < 100 ? 1 : 0);
        return parseFloat(n.toFixed(d)) + units[i][1];
      }
    }
    return Math.round(v).toLocaleString("en-US");
  }
  function full(v, kind) {
    if (v === null || v === undefined) return "-";
    if (kind === "ctr") return v.toFixed(1) + "%";
    if (kind === "position") return v.toFixed(1);
    return Math.round(v).toLocaleString("en-US");
  }
  function parseDay(iso) { var p = iso.split("-"); return new Date(+p[0], +p[1] - 1, +p[2]); }
  function shortDay(iso) { var d = parseDay(iso); return (d.getMonth() + 1) + "/" + d.getDate() + "/" + String(d.getFullYear()).slice(2); }
  function longDay(iso) {
    return parseDay(iso).toLocaleDateString("en-US", { weekday: "short", month: "short", day: "numeric", year: "numeric" });
  }
  function readMetrics() {
    try {
      var saved = JSON.parse(window.localStorage.getItem(STORE) || "null");
      if (Array.isArray(saved) && saved.length) return saved.filter(function (m) { return METRICS[m]; });
    } catch (e) { /* storage blocked: use the default */ }
    return ["clicks", "impressions"];
  }
  function saveMetrics(list) {
    try { window.localStorage.setItem(STORE, JSON.stringify(list)); } catch (e) { /* not kept */ }
  }
  // The top of an axis with three equal, round steps: 0, 5, 10, 15 rather than 0, 6.7, 13.3, 20.
  function niceMax(v) {
    if (!v || v <= 0) return 3;
    var raw = v / 3, p = Math.pow(10, Math.floor(Math.log10(raw)));
    var steps = [1, 2, 2.5, 5, 10];
    for (var i = 0; i < steps.length; i++) {
      var step = steps[i] * p;
      if (p >= 1 && step < 1) continue;
      if (step * 3 >= v) return step * 3;
    }
    return 30 * p;
  }
  function delta(cur, prev, kind) {
    if (prev === null || prev === undefined || cur === null || cur === undefined) return null;
    if (kind === "position") return { value: prev - cur, text: (prev - cur > 0 ? "+" : "") + (prev - cur).toFixed(1) };
    if (kind === "ctr") return { value: cur - prev, text: (cur - prev > 0 ? "+" : "") + (cur - prev).toFixed(1) + " pp" };
    var d = cur - prev;
    return { value: d, text: (d > 0 ? "+" : "") + d.toLocaleString("en-US") };
  }

  /* ---- tooltip ---- */

  var tip = document.getElementById("gsc-tooltip");
  function showTip(x, y, nodes) {
    if (!tip) return;
    tip.replaceChildren.apply(tip, nodes);
    tip.hidden = false;
    var w = tip.offsetWidth, h = tip.offsetHeight;
    var left = Math.min(window.innerWidth - w - 8, Math.max(8, x + 16));
    var top = Math.min(window.innerHeight - h - 8, Math.max(8, y - h / 2));
    tip.style.setProperty("left", left + "px");
    tip.style.setProperty("top", top + "px");
  }
  function hideTip() { if (tip) tip.hidden = true; }

  /* ---- the line chart ---- */

  /*
   * spec: { host, labels: [iso], prevLabels: [iso] | null, series: [{metric, values,
   * prev: [] | null}], marks: [{index, num, kind}], dashedFrom: index | null }
   */
  function drawChart(spec) {
    var host = spec.host;
    var W = host.clientWidth, H = host.clientHeight;
    if (!W || !H) return;
    var n = spec.labels.length;
    var pad = { l: 52, r: 52, t: 28, b: 30 };
    var pw = Math.max(10, W - pad.l - pad.r), ph = Math.max(10, H - pad.t - pad.b);
    var s = svg("svg", { viewBox: "0 0 " + W + " " + H, width: W, height: H });
    function x(i) { return pad.l + (n <= 1 ? pw / 2 : i / (n - 1) * pw); }

    var scales = {};
    spec.series.forEach(function (sr) {
      var kind = METRICS[sr.metric].kind;
      var vals = sr.values.concat(sr.prev || []).filter(function (v) { return v !== null && v !== undefined; });
      if (kind === "position") {
        var lo = vals.length ? Math.min.apply(null, vals) : 1, hi = vals.length ? Math.max.apply(null, vals) : 10;
        lo = Math.max(1, Math.floor(lo)); hi = Math.ceil(hi);
        if (hi === lo) hi = lo + 1;
        scales[sr.metric] = { lo: lo, hi: hi, y: function (v) { return pad.t + (v - lo) / (hi - lo) * ph; },
          ticks: [0, 1, 2, 3].map(function (k) { return lo + (hi - lo) * k / 3; }) };
      } else {
        var mx = niceMax(vals.length ? Math.max.apply(null, vals) : 0);
        scales[sr.metric] = { lo: 0, hi: mx, y: function (v) { return pad.t + ph - v / mx * ph; },
          ticks: [3, 2, 1, 0].map(function (k) { return mx * k / 3; }) };
      }
    });

    // Gridlines follow the first metric's ticks; axes label the first two metrics.
    var first = spec.series[0];
    for (var g = 0; g < 4; g++) {
      var gy = Math.round(pad.t + ph * g / 3) + 0.5;
      s.appendChild(svg("line", { x1: pad.l, x2: pad.l + pw, y1: gy, y2: gy, "class": "gsc-chart__grid" }));
    }
    spec.series.slice(0, 2).forEach(function (sr, side) {
      var sc = scales[sr.metric], kind = METRICS[sr.metric].kind;
      var ax = side === 0 ? pad.l - 8 : pad.l + pw + 8;
      var anchor = side === 0 ? "end" : "start";
      var title = svg("text", { x: side === 0 ? 0 : W, y: pad.t - 16, "text-anchor": side === 0 ? "start" : "end", "class": "gsc-chart__axis-title" });
      title.textContent = METRICS[sr.metric].label;
      s.appendChild(title);
      for (var k = 0; k < 4; k++) {
        var v = sc.ticks[k];
        var ty = pad.t + ph * k / 3;
        var t = svg("text", { x: ax, y: ty, "text-anchor": anchor, "dominant-baseline": "central", "class": "gsc-chart__axis" });
        t.textContent = kind === "position" ? Math.round(v) : (kind === "ctr" ? (v < 1 && v > 0 ? v.toFixed(1) : Math.round(v)) + "%" : compact(v, "count"));
        s.appendChild(t);
      }
    });

    // X ticks: about one every 120 pixels.
    var want = Math.max(2, Math.min(n, Math.floor(pw / 120)));
    var seen = {};
    for (var w = 0; w < want; w++) {
      var i = want === 1 ? 0 : Math.round(w / (want - 1) * (n - 1));
      if (seen[i]) continue;
      seen[i] = true;
      var xt = svg("text", { x: x(i), y: pad.t + ph + 20, "text-anchor": w === 0 ? "start" : (w === want - 1 ? "end" : "middle"), "class": "gsc-chart__axis" });
      xt.textContent = shortDay(spec.labels[i]);
      s.appendChild(xt);
    }

    function path(values, sc, from, to) {
      var d = "", pen = false;
      for (var i = from; i <= to; i++) {
        var v = values[i];
        if (v === null || v === undefined) { pen = false; continue; }
        d += (pen ? "L" : "M") + x(i).toFixed(1) + "," + sc.y(v).toFixed(1);
        pen = true;
      }
      return d;
    }
    // Draw back to front, so the first metric sits on top as in Search Console.
    spec.series.slice().reverse().forEach(function (sr) {
      var sc = scales[sr.metric];
      if (sr.prev) s.appendChild(svg("path", { d: path(sr.prev, sc, 0, n - 1), "class": "gsc-chart__line gsc-chart__line--prev gsc-chart__line--" + sr.metric }));
      var end = spec.dashedFrom !== null && spec.dashedFrom !== undefined ? spec.dashedFrom : n - 1;
      s.appendChild(svg("path", { d: path(sr.values, sc, 0, end), "class": "gsc-chart__line gsc-chart__line--" + sr.metric }));
      if (end < n - 1) s.appendChild(svg("path", { d: path(sr.values, sc, end, n - 1), "class": "gsc-chart__line gsc-chart__line--partial gsc-chart__line--" + sr.metric }));
    });

    (spec.marks || []).forEach(function (m) {
      var mx = x(m.index), my = pad.t + ph - 10;
      s.appendChild(svg("line", { x1: mx, x2: mx, y1: pad.t, y2: pad.t + ph, "class": "gsc-chart__grid" }));
      s.appendChild(svg("circle", { cx: mx, cy: my, r: 9, "class": "gsc-chart__mark gsc-chart__mark--" + m.kind }));
      var mt = svg("text", { x: mx, y: my, "class": "gsc-chart__mark-text" });
      mt.textContent = m.num;
      s.appendChild(mt);
    });

    var cursor = svg("line", { y1: pad.t, y2: pad.t + ph, "class": "gsc-chart__cursor", visibility: "hidden" });
    s.appendChild(cursor);
    var dots = spec.series.map(function (sr) {
      var c = svg("circle", { r: 5, "class": "gsc-chart__dot gsc-chart__dot--" + sr.metric, visibility: "hidden" });
      s.appendChild(c);
      return c;
    });
    var hit = svg("rect", { x: pad.l, y: pad.t, width: pw, height: ph, "class": "gsc-chart__hit" });
    s.appendChild(hit);
    hit.addEventListener("mousemove", function (ev) {
      var r = s.getBoundingClientRect();
      var i = n <= 1 ? 0 : Math.round((ev.clientX - r.left - pad.l) / pw * (n - 1));
      i = Math.max(0, Math.min(n - 1, i));
      cursor.setAttribute("x1", x(i)); cursor.setAttribute("x2", x(i)); cursor.setAttribute("visibility", "visible");
      var nodes = [el("p", "gsc-tooltip__date", (spec.labelPrefix || "") + longDay(spec.labels[i]))];
      spec.series.forEach(function (sr, k) {
        var v = sr.values[i], kind = METRICS[sr.metric].kind;
        if (v === null || v === undefined) { dots[k].setAttribute("visibility", "hidden"); }
        else {
          dots[k].setAttribute("cx", x(i)); dots[k].setAttribute("cy", scales[sr.metric].y(v)); dots[k].setAttribute("visibility", "visible");
        }
        var row = el("p", "gsc-tooltip__row");
        row.appendChild(el("span", "gsc-tooltip__swatch gsc-tooltip__swatch--" + sr.metric));
        row.appendChild(el("span", null, METRICS[sr.metric].label));
        row.appendChild(el("span", "gsc-tooltip__value", full(v, kind)));
        nodes.push(row);
        if (sr.prev) {
          var pr = el("p", "gsc-tooltip__row gsc-tooltip__prev");
          pr.appendChild(el("span", null, "Before (" + shortDay(spec.prevLabels[i]) + ")"));
          pr.appendChild(el("span", "gsc-tooltip__value", full(sr.prev[i], kind)));
          nodes.push(pr);
        }
      });
      showTip(ev.clientX, ev.clientY, nodes);
    });
    hit.addEventListener("mouseleave", function () {
      cursor.setAttribute("visibility", "hidden");
      dots.forEach(function (d) { d.setAttribute("visibility", "hidden"); });
      hideTip();
    });
    host.replaceChildren(s);
  }

  function watch(host, draw) {
    draw();
    if (window.ResizeObserver) {
      var last = host.clientWidth;
      new ResizeObserver(function () { if (host.clientWidth !== last) { last = host.clientWidth; draw(); } }).observe(host);
    }
  }

  /* ---- tables ---- */

  /*
   * spec: { host, columns: [{key, label, kind, name, render, sub}], rows, sort, dir,
   * pageSize, onRow(row) }
   */
  function renderTable(spec) {
    var state = { sort: spec.sort, dir: spec.dir || "desc", page: 0, size: spec.pageSize || 10 };
    function value(row, col) { return col.get ? col.get(row) : row[col.key]; }
    function draw() {
      var rows = spec.rows.slice();
      var col = spec.columns.filter(function (c) { return c.key === state.sort; })[0];
      if (col) {
        rows.sort(function (a, b) {
          var va = value(a, col), vb = value(b, col);
          if (va === null || va === undefined) return 1;
          if (vb === null || vb === undefined) return -1;
          var r = typeof va === "string" ? va.localeCompare(vb) : va - vb;
          return state.dir === "desc" ? -r : r;
        });
      }
      var pages = Math.max(1, Math.ceil(rows.length / state.size));
      state.page = Math.min(state.page, pages - 1);
      var slice = rows.slice(state.page * state.size, (state.page + 1) * state.size);

      var wrap = el("div", "gsc-table-scroll");
      var table = el("table", "gsc-table");
      var thead = el("thead"), hr = el("tr");
      spec.columns.forEach(function (c) {
        var th = el("th", c.name ? "gsc-table__name" : "");
        th.scope = "col";
        if (c.sortable === false) th.textContent = c.label;
        else {
          var b = el("button", "gsc-table__sort");
          b.type = "button";
          if (state.sort === c.key) {
            b.setAttribute("aria-sort", state.dir === "desc" ? "descending" : "ascending");
            b.appendChild(icon(state.dir === "desc" ? ICONS.down : ICONS.up)).setAttribute("class", "gsc-table__arrow");
          }
          b.appendChild(document.createTextNode(c.label));
          b.addEventListener("click", function () {
            if (state.sort === c.key) state.dir = state.dir === "desc" ? "asc" : "desc";
            else { state.sort = c.key; state.dir = c.name ? "asc" : "desc"; }
            state.page = 0;
            draw();
          });
          th.appendChild(b);
        }
        hr.appendChild(th);
      });
      thead.appendChild(hr);
      table.appendChild(thead);
      var tbody = el("tbody");
      slice.forEach(function (row) {
        var tr = el("tr");
        if (spec.onRow && spec.onRow(row)) {
          tr.dataset.filter = "1";
          tr.tabIndex = 0;
          tr.addEventListener("click", function (ev) { if (!ev.target.closest("a")) window.location.search = spec.onRow(row); });
          tr.addEventListener("keydown", function (ev) { if (ev.key === "Enter") window.location.search = spec.onRow(row); });
        }
        spec.columns.forEach(function (c) {
          var td = el(c.name ? "th" : "td", c.name ? "gsc-table__name" : (c.cls || ""));
          if (c.name) td.scope = "row";
          if (c.render) c.render(td, row);
          else if (c.name) td.textContent = value(row, c);
          else td.textContent = full(value(row, c), c.kind);
          tr.appendChild(td);
        });
        tbody.appendChild(tr);
      });
      if (!slice.length) {
        var tr0 = el("tr"), td0 = el("td", "gsc-table__name", spec.empty || "No rows.");
        td0.colSpan = spec.columns.length;
        tr0.appendChild(td0);
        tbody.appendChild(tr0);
      }
      table.appendChild(tbody);
      wrap.appendChild(table);

      var pager = el("div", "gsc-pager");
      var size = el("label", "gsc-pager__size");
      size.appendChild(document.createTextNode("Rows per page:"));
      var sel = el("select", "gsc-pager__select");
      [10, 25, 50, 100, 500].forEach(function (k) {
        var o = el("option", null, String(k)); o.value = k; if (k === state.size) o.selected = true; sel.appendChild(o);
      });
      sel.addEventListener("change", function () { state.size = +sel.value; state.page = 0; draw(); });
      size.appendChild(sel);
      pager.appendChild(size);
      var from = rows.length ? state.page * state.size + 1 : 0;
      var to = Math.min(rows.length, (state.page + 1) * state.size);
      pager.appendChild(el("span", "gsc-pager__range", from + "-" + to + " of " + rows.length.toLocaleString("en-US")));
      var btns = el("span", "gsc-pager__buttons");
      [["prev", -1, "Previous page"], ["next", 1, "Next page"]].forEach(function (p) {
        var b = el("button", "gsc-pager__button"); b.type = "button"; b.setAttribute("aria-label", p[2]);
        b.appendChild(icon(ICONS[p[0]]));
        b.disabled = p[1] < 0 ? state.page === 0 : state.page >= pages - 1;
        b.addEventListener("click", function () { state.page += p[1]; draw(); });
        btns.appendChild(b);
      });
      pager.appendChild(btns);
      spec.host.replaceChildren(wrap, pager);
    }
    draw();
  }

  function withFilter(dim, value) {
    var q = new URLSearchParams(window.location.search);
    q.set(dim, value);
    q.set(dim + "_op", "equals");
    return q.toString();
  }
  function nameCell(kind) {
    return function (td, row) {
      var text = row.label || row.key;
      if (kind === "page") {
        td.appendChild(el("span", "gsc-url", text));
        var a = el("a", "gsc-cell-link"); a.href = row.key; a.target = "_blank"; a.rel = "noopener";
        a.setAttribute("aria-label", "Open the page"); a.appendChild(icon(ICONS.open)); td.appendChild(a);
      } else if (kind === "date") {
        td.textContent = longDay(row.key);
      } else {
        td.textContent = text;
      }
      if (row.page) td.appendChild(el("span", "gsc-cell-sub", row.page));
      if (row.pages) {
        var ul = el("ul", "gsc-cell-list");
        row.pages.forEach(function (p) { ul.appendChild(el("li", null, p.page + "  (" + full(p.impressions) + " impressions, position " + full(p.position, "position") + ")")); });
        td.appendChild(ul);
      }
    };
  }

  /* ---- tabs ---- */

  var tabHandlers = {};
  function tabs(group, handler) { tabHandlers[group] = handler; }
  root.querySelectorAll("[data-tabs]").forEach(function (bar) {
    bar.addEventListener("click", function (ev) {
      var t = ev.target.closest("[role=tab]");
      if (!t) return;
      bar.querySelectorAll("[role=tab]").forEach(function (b) { b.setAttribute("aria-selected", b === t ? "true" : "false"); });
      var h = tabHandlers[bar.dataset.tabs];
      if (h) h(t.dataset.tab);
    });
  });
  function selectedTab(group) {
    var t = root.querySelector("[data-tabs=" + group + "] [aria-selected=true]");
    return t ? t.dataset.tab : null;
  }

  /* ---- dialogs and filters ---- */

  root.querySelectorAll("[data-open-dialog]").forEach(function (b) {
    b.addEventListener("click", function () { document.getElementById(b.dataset.openDialog).showModal(); });
  });
  root.querySelectorAll("[data-close-dialog]").forEach(function (b) {
    b.addEventListener("click", function () { b.closest("dialog").close(); });
  });
  tabs("date-dialog", function (name) {
    root.querySelectorAll("#gsc-date-dialog [data-panel]").forEach(function (p) { p.hidden = p.dataset.panel !== name; });
  });
  var dateForm = root.querySelector("[data-apply=date]");
  if (dateForm) {
    dateForm.querySelectorAll("input[type=date]").forEach(function (i) {
      i.addEventListener("input", function () { dateForm.querySelector("input[name=range][value=custom]").checked = true; });
    });
  }
  root.querySelectorAll("dialog form[data-apply]").forEach(function (form) {
    form.addEventListener("submit", function (ev) {
      ev.preventDefault();
      var q = new URLSearchParams(window.location.search);
      var f = new FormData(form);
      q.delete("window");
      if (form.dataset.apply === "date") {
        var range = f.get("range");
        if (range === "custom" && f.get("start") && f.get("end")) {
          q.delete("range"); q.set("start", f.get("start")); q.set("end", f.get("end"));
        } else { q.set("range", range === "custom" ? "28d" : range); q.delete("start"); q.delete("end"); }
        q.set("compare", f.get("compare") || "previous");
      } else if (form.dataset.apply === "type") {
        q.set("type", f.get("type"));
      } else {
        var dim = f.get("dim");
        var value = dim === "country" ? f.get("country_value") : (dim === "device" ? f.get("device_value") : (f.get("value") || "").trim());
        if (!value) { form.closest("dialog").close(); return; }
        q.set(dim, value); q.set(dim + "_op", f.get("op"));
      }
      window.location.search = q.toString();
    });
  });

  var filterDialog = document.getElementById("gsc-filter-dialog");
  var FILTER_OPS = { query: ["contains", "notContains", "equals"], page: ["contains", "notContains", "equals"], country: ["equals"], device: ["equals"] };
  var FILTER_NAMES = { query: "Query", page: "Page", country: "Country", device: "Device" };
  function openFilter(dim, op, value) {
    var form = filterDialog.querySelector("form");
    form.elements.dim.value = dim;
    filterDialog.querySelector(".gsc-dialog__title").textContent = FILTER_NAMES[dim];
    Array.prototype.forEach.call(form.elements.op.options, function (o) { o.hidden = FILTER_OPS[dim].indexOf(o.value) < 0; });
    form.elements.op.value = op || FILTER_OPS[dim][0];
    form.querySelectorAll("[data-field]").forEach(function (f) {
      f.hidden = f.dataset.field !== (dim === "country" || dim === "device" ? dim : "text");
    });
    form.elements.value.value = dim === "query" || dim === "page" ? (value || "") : "";
    if (dim === "country") {
      var sel = form.elements.country_value;
      sel.replaceChildren();
      ((data && data.tables.country.rows) || []).slice().sort(function (a, b) { return (a.label || "").localeCompare(b.label || ""); }).forEach(function (r) {
        var o = el("option", null, r.label || r.key); o.value = r.key; sel.appendChild(o);
      });
      if (value) sel.value = value;
    }
    if (dim === "device" && value) form.elements.device_value.value = value;
    filterDialog.showModal();
    if (dim === "query" || dim === "page") form.elements.value.focus();
  }
  root.querySelectorAll("[data-open-filter]").forEach(function (b) {
    b.addEventListener("click", function () {
      closeMenu();
      openFilter(b.dataset.openFilter, b.dataset.op, b.dataset.value);
    });
  });
  var menu = document.getElementById("gsc-add-filter");
  function closeMenu() {
    if (!menu) return;
    menu.querySelector(".gsc-menu__list").hidden = true;
    menu.querySelector(".gsc-menu__trigger").setAttribute("aria-expanded", "false");
  }
  if (menu) {
    var trigger = menu.querySelector(".gsc-menu__trigger");
    trigger.addEventListener("click", function () {
      var list = menu.querySelector(".gsc-menu__list");
      list.hidden = !list.hidden;
      trigger.setAttribute("aria-expanded", String(!list.hidden));
    });
    document.addEventListener("click", function (ev) { if (!menu.contains(ev.target)) closeMenu(); });
    document.addEventListener("keydown", function (ev) { if (ev.key === "Escape") closeMenu(); });
  }

  /* ---- bars sized by their share ---- */

  root.querySelectorAll("[data-share]").forEach(function (n) { n.style.setProperty("--share", n.dataset.share || 0); });
  var ctrFills = root.querySelectorAll("[data-ctr]");
  var ctrMax = 0;
  ctrFills.forEach(function (n) { ctrMax = Math.max(ctrMax, parseFloat(n.dataset.ctr) || 0); });
  ctrFills.forEach(function (n) { n.style.setProperty("--share", ctrMax ? (parseFloat(n.dataset.ctr) || 0) / ctrMax * 100 : 0); });
  root.querySelectorAll("td[data-num]").forEach(function (n) { n.textContent = full(+n.dataset.num); });

  if (!data) return;

  /* ---- events as numbered marks ---- */

  var EVENT_TEXT = function (e) {
    var verb = e.kind === "drop" ? "fell" : "rose";
    var metric = e.metric.charAt(0).toUpperCase() + e.metric.slice(1);
    return parseDay(e.date).toLocaleDateString("en-US", { month: "short", day: "numeric", year: "numeric" }) + ": " +
      metric + " " + verb + " " + Math.abs(e.change_pct) + "%, from " + full(e.before) + " to " + full(e.after) + " a day" +
      (e.recovered ? ", since back to the earlier level." : (e.kind === "drop" ? ", not recovered." : ", and it has held."));
  };
  function listEvents(list, events) {
    if (!list || !events.length) return;
    list.replaceChildren();
    events.forEach(function (e) {
      var li = el("li", "gsc-annotations__item");
      li.appendChild(el("span", "gsc-annotations__num", String(e.num)));
      li.appendChild(el("span", null, EVENT_TEXT(e)));
      list.appendChild(li);
    });
    list.hidden = false;
  }
  var allEvents = (data.events || []).slice().sort(function (a, b) { return a.date < b.date ? -1 : 1; });
  allEvents.forEach(function (e, i) { e.num = i + 1; });

  /* ---- performance chart and tiles ---- */

  var selected = readMetrics();
  var tiles = root.querySelectorAll(".gsc-tile");
  function syncTiles() {
    tiles.forEach(function (t) { t.setAttribute("aria-pressed", selected.indexOf(t.dataset.metric) >= 0 ? "true" : "false"); });
  }
  syncTiles();
  var chartHost = document.getElementById("gsc-chart");
  var hasPrev = data.series.length && data.series[0].prev;
  var rangeEvents = allEvents.filter(function (e) { return e.date >= data.range.start && e.date <= data.range.end; });
  function drawPerformance() {
    var labels = data.series.map(function (d) { return d.date; });
    drawChart({
      host: chartHost,
      labels: labels,
      prevLabels: hasPrev ? data.series.map(function (d) { return d.prev.date; }) : null,
      series: ORDER.filter(function (m) { return selected.indexOf(m) >= 0; }).map(function (m) {
        return {
          metric: m,
          values: data.series.map(function (d) { return d.impressions || m !== "position" ? d[m] : null; }),
          prev: hasPrev ? data.series.map(function (d) { return d.prev.impressions || m !== "position" ? d.prev[m] : null; }) : null
        };
      }),
      marks: rangeEvents.map(function (e) { return { index: labels.indexOf(e.date), num: e.num, kind: e.kind }; })
    });
  }
  if (chartHost) {
    watch(chartHost, drawPerformance);
    listEvents(document.getElementById("gsc-annotations"), rangeEvents);
  }
  tiles.forEach(function (t) {
    t.addEventListener("click", function () {
      var m = t.dataset.metric, i = selected.indexOf(m);
      if (i >= 0) { if (selected.length > 1) selected.splice(i, 1); } else selected.push(m);
      saveMetrics(selected);
      syncTiles();
      drawPerformance();
      drawBreakdown(selectedTab("breakdown") || "query");
    });
  });

  /* ---- breakdown table ---- */

  var NAMES = { query: "Top queries", page: "Top pages", country: "Country", device: "Device", appearance: "Search appearance", date: "Date" };
  var FILTERABLE = { query: true, page: true, country: true, device: true };
  function metricColumns() {
    var cols = [];
    ORDER.filter(function (m) { return selected.indexOf(m) >= 0; }).forEach(function (m) {
      var kind = METRICS[m].kind;
      cols.push({ key: m, label: METRICS[m].label, kind: kind });
      if (hasPrev) {
        cols.push({ key: m + "_prev", label: METRICS[m].label + " before", kind: kind, cls: "gsc-table__prev",
          get: function (r) { return r.prev ? r.prev[m] : null; } });
        cols.push({ key: m + "_diff", label: METRICS[m].label + " difference", kind: kind,
          get: function (r) { var d = delta(r[m], r.prev ? r.prev[m] : null, kind); return d ? d.value : null; },
          render: function (td, r) {
            var d = delta(r[m], r.prev ? r.prev[m] : null, kind);
            td.textContent = d ? d.text : "-";
            if (d && d.value) td.className = d.value > 0 ? "gsc-table__diff--up" : "gsc-table__diff--down";
          } });
      }
    });
    return cols;
  }
  function drawBreakdown(dim) {
    var t = data.tables[dim];
    if (!t) return;
    var first = ORDER.filter(function (m) { return selected.indexOf(m) >= 0; })[0] || "clicks";
    renderTable({
      host: document.getElementById("gsc-breakdown-table"),
      columns: [{ key: dim === "date" ? "key" : "label_or_key", label: NAMES[dim], name: true,
        get: function (r) { return r.label || r.key; }, render: nameCell(dim) }].concat(metricColumns()),
      rows: t.rows,
      sort: dim === "date" ? "label_or_key" : first,
      dir: dim === "date" ? "desc" : (first === "position" ? "asc" : "desc"),
      onRow: FILTERABLE[dim] && !(data.filters && data.filters[dim]) ? function (r) { return withFilter(dim, r.key); } : null,
      empty: "No data for this range."
    });
  }
  tabs("breakdown", drawBreakdown);
  drawBreakdown("query");

  /* ---- history ---- */

  var histHost = document.getElementById("gsc-history-chart");
  if (histHost && data.history.length) {
    var weeks = data.history.map(function (w) { return w.week; });
    var histMarks = allEvents.map(function (e) {
      var idx = -1;
      weeks.forEach(function (w, i) { if (w <= e.date) idx = i; });
      return { index: idx, num: e.num, kind: e.kind };
    }).filter(function (m) { return m.index >= 0; });
    var lastPartial = data.history[data.history.length - 1].partial ? data.history.length - 2 : null;
    watch(histHost, function () {
      drawChart({
        host: histHost,
        labels: weeks,
        labelPrefix: "Week of ",
        series: ["clicks", "impressions"].map(function (m) {
          return { metric: m, values: data.history.map(function (w) { return w[m]; }), prev: null };
        }),
        marks: histMarks,
        dashedFrom: lastPartial !== null && lastPartial >= 0 ? lastPartial : null
      });
    });
    listEvents(document.getElementById("gsc-history-events"), allEvents);
  }

  /* ---- opportunities ---- */

  var OPP = {
    striking: {
      note: "Queries in positions 4 to 20 with at least " + data.min_impressions + " impressions: a better page or a few internal links can move them onto the top of page 1.",
      columns: [
        { key: "key", label: "Query", name: true, render: nameCell("query") },
        { key: "clicks", label: "Clicks", kind: "count" },
        { key: "impressions", label: "Impressions", kind: "count" },
        { key: "ctr", label: "CTR", kind: "ctr" },
        { key: "position", label: "Position", kind: "position" }
      ], sort: "impressions"
    },
    low_ctr: {
      note: "Page-1 queries earning under 60% of this property's own click rate for their position. The fix is usually the title and description. Clicks missed is the gap times the impressions.",
      columns: [
        { key: "key", label: "Query", name: true, render: nameCell("query") },
        { key: "position", label: "Position", kind: "position" },
        { key: "impressions", label: "Impressions", kind: "count" },
        { key: "ctr", label: "CTR", kind: "ctr" },
        { key: "expected_ctr", label: "Usual CTR here", kind: "ctr" },
        { key: "missed", label: "Clicks missed", kind: "count" }
      ], sort: "missed"
    },
    cannibal: {
      note: "Queries where two or more pages each take at least 10% of the impressions, over " + data.query_page_span + ". Google splits the query between them; merge them or make one the clear answer.",
      columns: [
        { key: "key", label: "Query", name: true, render: nameCell("query") },
        { key: "count", label: "Pages", kind: "count" },
        { key: "impressions", label: "Impressions", kind: "count" },
        { key: "clicks", label: "Clicks", kind: "count" }
      ], sort: "impressions"
    },
    "new": {
      note: "Queries with impressions now and none in " + (data.compare && data.compare.span ? data.compare.span : "the earlier period") + ".",
      columns: [
        { key: "key", label: "Query", name: true, render: nameCell("query") },
        { key: "clicks", label: "Clicks", kind: "count" },
        { key: "impressions", label: "Impressions", kind: "count" },
        { key: "position", label: "Position", kind: "position" }
      ], sort: "impressions"
    },
    lost: {
      note: "Queries that brought clicks in the earlier period and none now.",
      columns: [
        { key: "key", label: "Query", name: true, render: nameCell("query") },
        { key: "clicks", label: "Clicks before", kind: "count" },
        { key: "impressions", label: "Impressions before", kind: "count" },
        { key: "now_impr", label: "Impressions now", kind: "count", get: function (r) { return r.now ? r.now.impressions : 0; } }
      ], sort: "clicks"
    }
  };
  function drawOpportunity(name) {
    var o = OPP[name];
    document.getElementById("gsc-opportunities-note").textContent = o.note;
    renderTable({
      host: document.getElementById("gsc-opportunities-table"),
      columns: o.columns, rows: data.opportunities[name] || [], sort: o.sort, dir: "desc",
      onRow: function (r) { return withFilter("query", r.key); },
      empty: "Nothing qualifies in this range."
    });
  }
  if (document.getElementById("gsc-opportunities-table")) {
    tabs("opportunities", drawOpportunity);
    drawOpportunity("striking");
  }

  /* ---- movers ---- */

  function drawMovers(dim) {
    var m = data.movers[dim];
    ["up", "down"].forEach(function (side) {
      renderTable({
        host: document.getElementById("gsc-movers-" + side),
        columns: [
          { key: "key", label: dim === "page" ? "Page" : "Query", name: true, render: nameCell(dim) },
          { key: "clicks", label: "Clicks", kind: "count" },
          { key: "prev_clicks", label: "Before", kind: "count", cls: "gsc-table__prev" },
          { key: "delta", label: "Change", kind: "count", render: function (td, r) {
            td.textContent = (r.delta > 0 ? "+" : "") + r.delta.toLocaleString("en-US");
            td.className = r.delta > 0 ? "gsc-table__diff--up" : "gsc-table__diff--down";
          } }
        ],
        rows: m[side], sort: "delta", dir: side === "up" ? "desc" : "asc",
        onRow: function (r) { return withFilter(dim, r.key); },
        empty: "No " + (side === "up" ? "gains" : "losses") + "."
      });
    });
  }
  if (document.getElementById("gsc-movers-up")) {
    tabs("movers", drawMovers);
    drawMovers("query");
  }

  /* ---- site sections ---- */

  var secHost = document.getElementById("gsc-sections-table");
  if (secHost) {
    renderTable({
      host: secHost,
      columns: [
        { key: "key", label: "Section", name: true },
        { key: "pages", label: "Pages", kind: "count" },
        { key: "clicks", label: "Clicks", kind: "count" },
        { key: "clicks_pct", label: "Share", kind: "count", render: function (td, r) {
          var s = el("span", "gsc-share"), b = el("span", "gsc-share__bar");
          b.style.setProperty("--share", r.clicks_pct);
          s.appendChild(b); s.appendChild(el("span", null, r.clicks_pct + "%"));
          td.appendChild(s);
        } },
        { key: "impressions", label: "Impressions", kind: "count" },
        { key: "change", label: "Clicks before", kind: "count", cls: "gsc-table__prev", get: function (r) { return r.prev ? r.prev.clicks : null; } }
      ].filter(function (c) { return c.key !== "change" || hasPrev; }),
      rows: data.sections.rows, sort: "clicks", dir: "desc",
      onRow: function (r) { return r.other || r.key === "/" ? null : withFilter("page", r.key + "/").replace("page_op=equals", "page_op=contains"); },
      empty: "No page has impressions in this range."
    });
  }
})();
