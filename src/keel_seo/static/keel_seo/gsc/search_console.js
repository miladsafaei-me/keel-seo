/* Search Console dashboard — Alpine component behaviour.
   Moved out of the template so the markup carries no inline script (project rule).
   Endpoint URLs + CSRF are read from the DOM (data-* on #sc-root, #sc-csrf) since a
   static file can't use {% url %}. All functions are plain globals referenced by
   x-data; they are defined before Alpine's deferred init runs. */

/* The host's own scheme://domain (data-site-base on #sc-root), used to shorten full
   GSC page URLs down to their path in a couple of inline template expressions. Read
   once at script-load time — this file loads after #sc-root in the DOM. */
const SC_SITE_BASE = (document.getElementById('sc-root') && document.getElementById('sc-root').dataset.siteBase) || '';

/* ---- shared helpers: CSRF + endpoint URLs from the DOM ---- */
function scCsrf() {
  const el = document.querySelector('#sc-csrf [name=csrfmiddlewaretoken]');
  return el ? el.value : '';
}
function scUrls() {
  const r = document.getElementById('sc-root');
  const d = (r && r.dataset) || {};
  return {
    dismiss: d.dismissUrl || '', restore: d.restoreUrl || '',
    queue: d.queueUrl || '', exclude: d.excludeUrl || '',
    dedicatedQueue: d.dedicatedQueueUrl || '',
    clusterQueue: d.clusterQueueUrl || '', clusterExclude: d.clusterExcludeUrl || '',
  };
}

/* The range the page was rendered for. Cluster actions post it back so the server
   re-derives the cluster's keywords from the SAME payload the reader was looking at,
   rather than from whatever the default window happens to be. */
function scRange() {
  const r = document.getElementById('sc-root');
  const d = (r && r.dataset) || {};
  return { window: d.window || '', start: d.start || '', end: d.end || '' };
}

/* ---- paginated / sortable data table (fed by a <script type=json>) ---- */
function scTable(dataId, perPage, dirAware) {
  const el = document.getElementById(dataId);
  let rows = [];
  try { rows = el ? JSON.parse(el.textContent) : []; } catch (e) { rows = []; }
  return {
    rows: rows,
    perPage: perPage,
    dirAware: !!dirAware,   // when set, filter rows by the shared directory store
    page: 1,
    sortKey: null,
    sortDir: -1,           // -1 desc, 1 asc
    sortBy(key) {
      if (this.sortKey === key) { this.sortDir = -this.sortDir; }
      else { this.sortKey = key; this.sortDir = -1; }
      this.page = 1;
    },
    // Rows after the (optional) directory filter — the source for sort/paginate/totals.
    get _source() {
      if (!this.dirAware) return this.rows;
      const dir = (this.$store.perf && this.$store.perf.dir) || 'all';
      return dir === 'all' ? this.rows : this.rows.filter(r => r.directory === dir);
    },
    get sorted() {
      const src = this._source;
      if (!this.sortKey) return src;
      const k = this.sortKey, d = this.sortDir;
      return [...src].sort((a, b) => {
        const av = a[k], bv = b[k];
        if (av == null) return 1;
        if (bv == null) return -1;
        if (av < bv) return -d; if (av > bv) return d;
        return 0;
      });
    },
    get total() { return this._source.length; },
    get pages() { return Math.max(1, Math.ceil(this.total / this.perPage)); },
    get _cp() { return Math.min(this.page, this.pages); },   // clamped current page
    get view() {
      const s = (this._cp - 1) * this.perPage;
      return this.sorted.slice(s, s + this.perPage);
    },
    get from() { return this.total ? (this._cp - 1) * this.perPage + 1 : 0; },
    get to() { return Math.min(this._cp * this.perPage, this.total); },
    next() { if (this.page < this.pages) this.page++; },
    prev() { if (this.page > 1) this.page--; },
    fmt(n) { return (n == null ? 0 : n).toLocaleString(); },
  };
}

/* ---- tabbed card: swap which grouping/table is visible ---- */
function scTabs(initial) {
  return {
    tab: initial,
    is(t) { return this.tab === t; },
    set(t) { this.tab = t; },
  };
}

/* ---- dashboard root: chart tooltips, insight dismiss (with reason), restore ---- */
function scDashboard() {
  return {
    dismissed: [],          // fingerprints hidden this session
    menuFp: null,           // fingerprint whose dismiss menu is open
    tip: { show: false, text: '', x: 0, y: 0 },
    showTip(e) {
      this.tip.text = e.target.getAttribute('data-tip') || '';
      this.tip.show = true;
      this.moveTip(e);
    },
    moveTip(e) {
      this.tip.x = e.clientX;
      this.tip.y = e.clientY - 10;
    },
    toggleMenu(fp) { this.menuFp = (this.menuFp === fp) ? null : fp; },
    async dismiss(fp, reason) {
      this.menuFp = null;
      if (this.dismissed.includes(fp)) return;
      const body = new URLSearchParams({ fingerprint: fp, reason: reason || 'done', csrfmiddlewaretoken: scCsrf() });
      try {
        const r = await fetch(scUrls().dismiss, { method: 'POST', body });
        if (r.ok) this.dismissed.push(fp);
      } catch (e) { /* leave the card visible on failure */ }
    },
    async restore() {
      const body = new URLSearchParams({ csrfmiddlewaretoken: scCsrf() });
      try {
        const r = await fetch(scUrls().restore, { method: 'POST', body });
        if (r.ok) window.location.reload();
      } catch (e) { /* no-op */ }
    },
  };
}

/* ---- "Send to ContentPlan" button — self-contained per opportunity ---- */
function scQueue(payload) {
  return {
    state: 'idle',          // idle | loading | done | error
    url: '',
    msg: '',
    async send() {
      if (this.state === 'loading' || this.state === 'done') return;
      this.state = 'loading';
      const body = new URLSearchParams({ ...payload, csrfmiddlewaretoken: scCsrf() });
      try {
        const r = await fetch(scUrls().queue, { method: 'POST', body });
        const j = await r.json().catch(() => ({}));
        if (r.ok && j.ok) {
          this.state = 'done';
          this.url = j.url || '';
          this.msg = j.outcome === 'created' ? 'Queued' : (j.outcome === 'locked' ? 'In production' : 'Updated');
        } else {
          this.state = 'error';
          this.msg = (j && j.error) || 'Failed';
        }
      } catch (e) {
        this.state = 'error';
        this.msg = 'Failed';
      }
    },
  };
}

/* ---- Dedicated-content candidate row — "Add to Plan" + permanent delete ----
   Adding sends the keyword to the CLUSTERING queue, not to ContentPlan. A lone query
   usually belongs in a cluster with other keywords, and picking it out alone would
   lose that grouping for good; a content plan also needs a title and a brief that
   only the intent analysis can produce. Picks accumulate into one pool per market and
   are clustered together.

   Holds a reference to the underlying row object so a successful delete can splice
   it straight out of the parent scTable's `rows`, removing it from the page without
   a reload (the server-side exclude list keeps it gone on every future refresh too). */
function scDedicatedRow(row) {
  return {
    state: row.already_queued ? 'done' : 'idle',
    url: row.already_queued ? (row.plan_url || '') : '',
    msg: row.already_queued ? 'Added to Plan' : '',
    removed: false,
    async send() {
      if (this.state === 'loading' || this.state === 'done') return;
      this.state = 'loading';
      const body = new URLSearchParams({
        query: row.query,
        market: row.market || '',
        impressions: row.impressions == null ? '' : row.impressions,
        clicks: row.clicks == null ? '' : row.clicks,
        position: row.position == null ? '' : row.position,
        csrfmiddlewaretoken: scCsrf(),
      });
      try {
        const r = await fetch(scUrls().dedicatedQueue, { method: 'POST', body });
        const j = await r.json().catch(() => ({}));
        if (r.ok && j.ok) {
          this.state = 'done';
          this.url = j.url || '';
          this.msg = 'Added to Plan';
        } else {
          this.state = 'error';
          this.msg = (j && j.error) || 'Failed';
        }
      } catch (e) {
        this.state = 'error';
        this.msg = 'Failed';
      }
    },
    async del() {
      const body = new URLSearchParams({ query: row.query, csrfmiddlewaretoken: scCsrf() });
      try {
        const r = await fetch(scUrls().exclude, { method: 'POST', body });
        if (r.ok) {
          this.removed = true;
          const rows = this.$parent.rows;
          const i = rows.indexOf(row);
          if (i >= 0) rows.splice(i, 1);
        }
      } catch (e) { /* leave the row visible on failure */ }
    },
  };
}

/* ---- Dedicated-content CLUSTER row — queue the whole pool + permanent delete ----
   Deliberately a different destination from the per-query button in the Detail tab: a
   single query is already a content idea and goes straight to ContentPlan, while a
   cluster is a pool of keywords nobody has analysed yet, so it goes one step earlier —
   into the clustering queue the content autopilot drains first. */
function scClusterRow(row) {
  return {
    state: row.already_queued ? 'done' : 'idle',
    url: row.job_url || '',
    msg: row.already_queued ? 'Added to Plan' : '',
    removed: false,
    async send() {
      if (this.state === 'loading' || this.state === 'done') return;
      this.state = 'loading';
      const body = new URLSearchParams({
        cluster: row.cluster, ...scRange(), csrfmiddlewaretoken: scCsrf(),
      });
      try {
        const r = await fetch(scUrls().clusterQueue, { method: 'POST', body });
        const j = await r.json().catch(() => ({}));
        if (r.ok && j.ok) {
          this.state = 'done';
          this.url = j.url || '';
          this.msg = 'Added to Plan';
        } else {
          this.state = 'error';
          this.msg = (j && j.error) || 'Failed';
        }
      } catch (e) {
        this.state = 'error';
        this.msg = 'Failed';
      }
    },
    async del() {
      const body = new URLSearchParams({ cluster: row.cluster, csrfmiddlewaretoken: scCsrf() });
      try {
        const r = await fetch(scUrls().clusterExclude, { method: 'POST', body });
        if (r.ok) {
          this.removed = true;
          const rows = this.$parent.rows;
          const i = rows.indexOf(row);
          if (i >= 0) rows.splice(i, 1);
        }
      } catch (e) { /* leave the row visible on failure */ }
    },
  };
}

/* Shared selected URL directory — the Performance dropdown writes it, the chart and
   the Ranking-pages table both read it, so filtering one filters the other. */
document.addEventListener('alpine:init', () => { Alpine.store('perf', { dir: 'all' }); });

/* ---- Performance chart: metric toggles + per-directory re-plot + hover ---- */
function scPerformance(chartsId, dirsId) {
  const parse = (id) => {
    const el = document.getElementById(id);
    try { return el ? JSON.parse(el.textContent) : null; } catch (e) { return null; }
  };
  const charts = parse(chartsId) || {};              // {slug: {metrics:[...], hover:[...]}}
  const dirs = parse(dirsId) || [];                  // [{slug, label}]
  const WD = ['Sunday', 'Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday'];
  const MO = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];
  return {
    dirs,
    charts,
    active: ['clicks', 'impressions'],               // GSC default: clicks + impressions
    hover: { show: false, i: 0, mx: 0, my: 0 },
    // Selected directory lives in the shared store so the pages table filters in sync.
    get dir() { return (this.$store.perf && this.$store.perf.dir) || 'all'; },
    // The chart for the selected directory (falls back to 'all').
    get _chart() { return this.charts[this.dir] || this.charts.all || { metrics: [], hover: [] }; },
    get metrics() { return this._chart.metrics || []; },
    get pts() { return this._chart.hover || []; },
    pathFor(key) { const m = this.metrics.find(x => x.key === key); return m ? m.path : ''; },
    toggle(k) {
      const i = this.active.indexOf(k);
      if (i >= 0) { if (this.active.length > 1) this.active.splice(i, 1); }  // keep >=1 on
      else this.active.push(k);
    },
    // Active metrics in the fixed metric order → first takes the left axis, second the right.
    get orderedActive() { return this.metrics.filter(m => this.active.includes(m.key)); },
    get leftMetric() { return this.orderedActive[0] || null; },
    get rightMetric() { return this.orderedActive[1] || null; },
    get leftTicks() { return this.leftMetric ? this.leftMetric.ticks : []; },
    get rightTicks() { return this.rightMetric ? this.rightMetric.ticks : []; },
    get leftAxClass() { return this.leftMetric ? ('sc-tl__ax--' + this.leftMetric.color) : ''; },
    get rightAxClass() { return this.rightMetric ? ('sc-tl__ax--' + this.rightMetric.color) : ''; },
    move(e) {
      const n = this.pts.length;
      if (!n) return;
      const r = this.$refs.svg.getBoundingClientRect();
      const frac = Math.min(1, Math.max(0, (e.clientX - r.left) / r.width));
      this.hover.i = Math.round(frac * (n - 1));
      this.hover.show = true;
      // Flip the tooltip left of the cursor near the right edge; clamp into the viewport.
      const TIP_W = 220;
      let mx = e.clientX + 16;
      if (mx + TIP_W > window.innerWidth - 8) mx = e.clientX - TIP_W - 16;
      this.hover.mx = Math.max(8, mx);
      this.hover.my = e.clientY + 12;
    },
    leave() { this.hover.show = false; },
    get cur() { return this.pts[this.hover.i] || { x: 0 }; },
    tipVal(m) {
      const c = this.cur;
      if (m.key === 'ctr') return (c.ctr == null ? 0 : c.ctr).toFixed(2) + '%';
      if (m.key === 'position') return (c.pos == null ? 0 : c.pos).toFixed(1);
      const v = m.key === 'clicks' ? c.clicks : c.impr;
      return (v == null ? 0 : v).toLocaleString();
    },
    fmtDate(iso) {
      if (!iso) return '';
      const [yy, mm, dd] = iso.split('-').map(Number);
      return `${WD[new Date(yy, mm - 1, dd).getDay()]}, ${MO[mm - 1]} ${dd}`;
    },
  };
}

/* ---- date-range picker: preset pills + custom-range calendar ---- */
function scRangePicker() {
  const pad = (n) => String(n).padStart(2, '0');
  const iso = (y, m, d) => `${y}-${pad(m + 1)}-${pad(d)}`;   // m is 0-based
  const now = new Date();
  const max = new Date(now.getFullYear(), now.getMonth(), now.getDate());  // dataState=all reaches today
  const maxIso = iso(max.getFullYear(), max.getMonth(), max.getDate());
  const MONTHS = ['January', 'February', 'March', 'April', 'May', 'June', 'July',
                  'August', 'September', 'October', 'November', 'December'];
  return {
    open: false,
    view: { y: max.getFullYear(), m: max.getMonth() },
    start: null,
    end: null,
    hover: null,
    loading: false,
    loadingLabel: '',
    // Show the waiting overlay while the range navigation + (live) render runs.
    // A short delay keeps cached/instant loads from flashing it; the page unloads
    // on navigation, so it never needs to be hidden manually.
    hold(label) {
      this.loadingLabel = label || '';
      setTimeout(() => { this.loading = true; }, 160);
    },
    toggle() { this.open = !this.open; },
    reset() { this.start = null; this.end = null; this.hover = null; },
    shift(delta) {
      let m = this.view.m + delta, y = this.view.y;
      if (m < 0) { m = 11; y--; }
      if (m > 11) { m = 0; y++; }
      this.view = { y, m };
    },
    get atMaxMonth() { return this.view.y === max.getFullYear() && this.view.m === max.getMonth(); },
    get monthTitle() { return `${MONTHS[this.view.m]} ${this.view.y}`; },
    get cells() {
      const y = this.view.y, m = this.view.m;
      const first = new Date(y, m, 1).getDay();          // 0 = Sunday
      const dim = new Date(y, m + 1, 0).getDate();
      const out = [];
      for (let i = 0; i < first; i++) out.push({ key: 'b' + i, day: '', iso: null, disabled: true, cls: 'is-blank' });
      const lo = this.start;
      const hi = this.end || (this.start ? this.hover : null);
      const a = lo && hi ? (lo <= hi ? lo : hi) : lo;
      const b = lo && hi ? (lo <= hi ? hi : lo) : null;
      for (let d = 1; d <= dim; d++) {
        const s = iso(y, m, d);
        const disabled = s > maxIso;
        let cls = '';
        if (disabled) cls = 'is-off';
        else if (s === this.start || s === this.end) cls = 'is-edge';
        else if (a && b && s > a && s < b) cls = 'is-in';
        out.push({ key: s, day: d, iso: s, disabled, cls });
      }
      return out;
    },
    pick(s) {
      if (!s) return;
      if (!this.start || this.end) { this.start = s; this.end = null; }
      else if (s < this.start) { this.end = this.start; this.start = s; }
      else { this.end = s; }
    },
    get footLabel() {
      if (this.start && this.end) return `${this.start}  →  ${this.end}`;
      if (this.start) return `${this.start}  →  …`;
      return 'Select a start and end day';
    },
    apply() {
      if (!(this.start && this.end)) return;
      this.open = false;
      this.loadingLabel = `${this.start} → ${this.end}`;
      this.loading = true;                 // a custom range is always a live pull — show at once
      window.location.search = `?start=${this.start}&end=${this.end}`;
    },
  };
}
