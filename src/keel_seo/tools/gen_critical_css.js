/*
 * gen_critical_css.js — generate the above-fold critical-CSS subset for any
 * marketing page that loads marketing-bundle.css async.
 *
 * The base template (base_marketing.html) inlines `<name>-critical.css` when a
 * page overrides the `critical_css` block with
 *   {% page_critical_css "<name>" %}
 * and loads the full marketing-bundle.css async (preload + onload swap). This
 * script extracts that per-page subset with penthouse so first paint matches
 * the final layout (no FOUC, no CLS).
 *
 * Run on the host (needs Node + Chromium; penthouse installed in
 * /tmp/penthouse-work):
 *   cd /tmp/penthouse-work
 *   node /home/milad/www/signalbots/tools/gen_critical_css.js home
 *   node /home/milad/www/signalbots/tools/gen_critical_css.js          # all pages
 *
 * Output: backend/core/static/css/pages/<name>-critical.css
 *
 * forceInclude carries the above-fold component selectors that penthouse's
 * viewport pass can miss (JS-revealed rotator rows, the typewriter caret,
 * hide-by-default nav dropdowns, @media desktop variants). Patterns are NOT
 * ^-anchored — bundle selectors are descendant-scoped (.page-home .home-hero)
 * so an anchor would never match. See reference-critical-css-pitfalls.
 */
const penthouse = require('penthouse');
const fs = require('fs');
const path = require('path');

const REPO = process.env.REPO || '/home/milad/www/signalbots';
const CSS_DIR = path.join(REPO, 'backend/core/static/css/pages');
const ORIGIN = process.env.ORIGIN || 'http://127.0.0.1:8082';

// Shared above-fold chrome every dark-marketing page renders: site header,
// breadcrumb, hidden-by-default nav dropdowns, background glows, tokens.
const CHROME = [
  '.page-signalbots-home', '.container', '.bg-glow', '.bg-glow-2',
  // Header chrome only — exclude the footer (below-fold, never first-paint).
  /\.sb-chrome(?!-footer)/, /\.sb-crumbs/, /\.dsn-u/, /\.dsn-a/, /\.dsn-d/, /\.dsn-b/,
  /\.btn-/, /\.title-/,
];

const PAGES = {
  home: {
    url: '/',
    source: ['marketing-bundle.css'],
    forceInclude: [
      '.page-home',
      /\.home-page/, /\.home-hero/, /\.home-eyebrow/, /\.home-accent/,
      /\.home-typer/, /\.home-link/, /\.home-dot/, /\.home-btn/,
      /\.home-live/, /\.home-trust/,
    ],
  },
  'trading-bots-index': {
    url: '/trading-bots',
    source: ['marketing-bundle.css', 'trading-bots-index.css'],
    forceInclude: [
      '.page-chrome-ext-index',
      /\.cei-hero/, /\.cei-eyebrow/, /\.cei-title/, /\.cei-subtitle/,
      /\.cei-meta/,
    ],
  },
  // /signals umbrella hub (signals_index.html) — shares the cei-hero +
  // page-chrome-ext-index surface with trading-bots-index, plus its own
  // sig-hub-* market card grid. Critical carries the .dsn-* mega-nav hide
  // rules the default marketing critical omits (the FOUC cause).
  'signals-index': {
    url: '/signals',
    source: ['marketing-bundle.css', 'trading-bots-index.css', 'signals-index.css'],
    forceInclude: [
      '.page-chrome-ext-index', '.page-signals-index',
      /\.cei-hero/, /\.cei-eyebrow/, /\.cei-title/, /\.cei-subtitle/,
      /\.cei-meta/, /\.cei-section/, /\.sig-hub/,
    ],
  },
  // Trading glossary — listing (trading_glossary_index.html) + single-term
  // page (trading_glossary_term.html). Both share the cei-hero +
  // page-chrome-ext-index surface, plus their own .tg-gloss-* / .tg-term*
  // above-fold rules. Critical carries the .dsn-* mega-nav hide rules the
  // default marketing critical omits (the FOUC cause). The term url below is a
  // representative slug — swap for any live term when regenerating.
  'trading-glossary-index': {
    url: '/trading-glossary',
    source: ['marketing-bundle.css', 'trading-bots-index.css', 'trading-glossary.css'],
    forceInclude: [
      '.page-chrome-ext-index', '.page-trading-glossary', '.page-trading-glossary-index',
      /\.cei-hero/, /\.cei-eyebrow/, /\.cei-title/, /\.cei-subtitle/,
      /\.tg-gloss/,
    ],
  },
  'trading-glossary-term': {
    url: '/trading-glossary/trading-signal',
    source: ['marketing-bundle.css', 'trading-bots-index.css', 'trading-glossary.css'],
    forceInclude: [
      '.page-chrome-ext-index', '.page-trading-glossary', '.page-trading-glossary-term',
      /\.cei-hero/, /\.title-/, /\.tg-term/,
    ],
  },
  // MT5 Connector listing pages — the /connectors hub (index.html) and each
  // /connectors/<market> listing (category.html). Both share the cei-hero +
  // page-chrome-ext-index surface, plus their own cic-* category + connector
  // card grid. Critical carries the .dsn-* mega-nav hide rules the default
  // marketing critical omits (the FOUC cause).
  'connectors-listing': {
    url: '/connectors',
    source: ['marketing-bundle.css', 'trading-bots-index.css', 'connectors-index.css'],
    forceInclude: [
      '.page-chrome-ext-index', '.page-connectors-listing',
      /\.cei-hero/, /\.cei-eyebrow/, /\.cei-title/, /\.cei-subtitle/,
      /\.cei-meta/, /\.cei-section/, /\.cic-/,
    ],
  },
  // All legal/static pages (/disclaimer, /terms, /privacy, /cookies,
  // /risk-warning, /editorial-policy) share ONE critical. Each loads
  // static-pages.css as a render-blocking <link>, so the legal-hero / sticky
  // TOC / article are already styled at first paint — but marketing-bundle.css
  // loads async, so the .dsn-* mega-nav flashed (the default marketing critical
  // omits its hide rules). This subset carries marketing-bundle's chrome floor
  // only; it is identical for every legal page because the page-<slug> body
  // classes are styled by the blocking static-pages.css, not by the bundle.
  // Generated against /disclaimer; wired to every legal template via
  // {% page_critical_css "legal" %}.
  legal: {
    url: '/disclaimer',
    source: ['marketing-bundle.css'],
    forceInclude: [
      '.page-static', '.page-disclaimer', '.page-terms', '.page-privacy',
      '.page-cookie', '.page-risk-warning', '.page-editorial-policy',
    ],
  },
  // Shared by every /signals/<asset> listing (asset_signals_list.html) and
  // /signals/binary (binary_signals_list.html) — same fx-* hero structure and
  // the same forex-signals.css. One critical covers all nine landings.
  'forex-signals': {
    url: '/signals/forex',
    source: ['marketing-bundle.css', 'forex-signals.css'],
    forceInclude: [
      '.page-forex-signals', '.green',
      /\.fx-breadcrumb/, /\.fx-signals-hero/, /\.fx-signals-eyebrow/,
      /\.fx-signals-sub/, /\.fx-hero-cta/, /\.fx-pulse/,
      /\.fx-trust-strip/, /\.fx-trust-item/, /\.fx-disclaimer-strip/,
      /\.fx-section-eyebrow/, /\.fx-results-/, /\.fx-winrate-/, /\.fx-top-charts/,
    ],
  },
  // Binary options break-even win rate calculator
  // (/tools/binary-options/break-even-win-rate, break_even_win_rate.html).
  // tools-break-even.css loads async, so this critical carries the shared
  // chrome floor + the .dsn-* mega-nav hide rules plus the above-fold
  // calculator hero + card.
  'tools-break-even': {
    url: '/tools/binary-options/break-even-win-rate',
    source: ['marketing-bundle.css', 'tools-break-even.css'],
    forceInclude: [
      '.page-tools', '.page-tools-break-even',
      /\.bewr-hero/, /\.bewr-eyebrow/, /\.bewr-title/, /\.bewr-lede/,
      /\.bewr-card/, /\.bewr-field/, /\.bewr-slider/, /\.bewr-payout/,
      /\.bewr-result/, /\.bewr-gauge/, /\.bewr-risk/,
    ],
  },
  // Chinese Bot AI binary-option reader (/tools/binary-option/chinese-bot,
  // chinese-bot.html). chinese-bot.css loads async, so this critical carries the
  // chrome floor + .dsn-* mega-nav hide rules plus the above-fold hero, the live
  // AI-scan line, the control deck (broker/pair/timeframe chips) and the first
  // section divider, so first paint matches the final layout (no FOUC, no CLS).
  'chinese-bot': {
    url: '/tools/binary-option/chinese-bot',
    source: ['marketing-bundle.css', 'chinese-bot.css'],
    forceInclude: [
      '.page-chinese-bot',
      /\.cb-hero/, /\.cb-identity/, /\.cb-logo/, /\.cb-h1/, /\.cb-kicker/, /\.cb-status/,
      /\.cb-ai-live/, /\.cb-hero-meta/, /\.cb-hero-works/,
      /\.cb-app-grid/, /\.cb-controls/, /\.cb-panel/, /\.cb-eyebrow/,
      /\.cb-field/, /\.cb-chip/, /\.cb-lock/, /\.cb-tf-note/,
      /\.cei-section-divider/,
    ],
  },
  // Chinese Bot Telegram Mini App shell (/tg/chinese-bot, chinese-bot-tg.html).
  // STANDALONE template (not base_marketing) that loads marketing-bundle.css +
  // chinese-bot.css + chinese-bot-tg.css. Previously all three were render-blocking
  // with NO inline critical, so Telegram's webview flashed a blank/white frame
  // until ~247KB of CSS downloaded, then flipped to the dark app (the FOUC the
  // user reported). Now the sheets load async and this subset is inlined. The
  // whole Mini App is above the fold, so critical carries the dark shell bg, the
  // .cb-tg-* header + status pills, and the full .cb-* signal widget (controls,
  // chip grids, verdict card) so first paint matches the final layout.
  'chinese-bot-tg': {
    url: '/tg/chinese-bot',
    source: ['marketing-bundle.css', 'chinese-bot.css', 'chinese-bot-tg.css'],
    forceInclude: [
      '.page-chinese-bot', '.cb-tg', '.page-signalbots-home',
      /\.cb-tg-/, /\.cb-tgf-/, /\.cb-status/,
      /\.cb-app-grid/, /\.cb-controls/, /\.cb-panel/, /\.cb-eyebrow/,
      /\.cb-field/, /\.cb-chip/, /\.cb-lock/, /\.cb-tf-note/, /\.cb-ico/,
      /\.cb-card/, /\.cb-scanner/, /\.cb-analyzing/, /\.cb-verdict/,
      /\.cb-strength/, /\.cb-meter/, /\.cb-signal-col/, /\.cb-live/,
      /\.cb-countdown/, /\.cb-premium/, /\.cb-refresh/, /\.cb-dots/,
    ],
  },
  // Shared by every bucket-1 calculator landing under /tools/<market>/ (the
  // generic views.tool surface). tools.css loads async, so this critical
  // carries the chrome floor + .dsn-* mega-nav hide rules plus the above-fold
  // .tool-hero + .tool-card calculator shell. One critical covers all 15 tools
  // (identical hero/card structure). Representative URL below.
  tools: {
    url: '/tools/forex/position-size',
    source: ['marketing-bundle.css', 'tools.css'],
    forceInclude: [
      '.page-tools',
      /\.tool-hero/, /\.tool-eyebrow/, /\.tool-title/, /\.tool-lede/,
      /\.tool-card/, /\.tool-field/, /\.tool-slider/, /\.tool-input/,
      /\.tool-result/, /\.tool-gauge/, /\.tool-risk/,
    ],
  },
  // Binary connector landings (/connectors/binary-options/<broker>-otc,
  // binary_detail.html). connector-landing.css loads async, so this critical
  // carries the shared chrome floor + the .dsn-* mega-nav hide rules the default
  // marketing critical omits (the real FOUC cause) plus the .cl-* hero/MT5 window.
  connectors: {
    url: '/connectors/binary-options/mt5-pocket-option-otc',
    source: ['marketing-bundle.css', 'connector-landing.css'],
    forceInclude: [
      '.page-connector-landing', '.cl', '.cl-accent',
      /\.cl-hero/, /\.cl-mt5/, /\.cl-conn/, /\.cl-chart/, /\.cl-candles/,
      /\.cl-c/, /\.cl-dot/,
    ],
  },
  // Blog surfaces. All extend base_marketing and load their page CSS async, so
  // without a per-page critical the default marketing critical (which omits the
  // .dsn-* mega-nav hide rules) flashes the expanded mega-nav on hard refresh —
  // the same FOUC cause fixed on /signals and the connector listings.
  //
  // blog-detail covers the article page (post_detail.html, also reused by the
  // staff preview view); blog-list covers both the topic/category listing
  // (category_tag_list.html) and the paginated post list (post_list.html) —
  // both load blog-news.css and share the .blog-layout / .blog-card-h shell,
  // with .featured-* and .category-header force-included so either variant is
  // covered from one subset; magazine covers the /blog magazine index (mag.css).
  'blog-detail': {
    url: '/blog/telegram-vs-mobile-vs-extension-vs-ea-signal-delivery-2026',
    source: ['marketing-bundle.css', 'blog-news.css'],
    forceInclude: [
      '.page-blog', '.page-blog-detail',
      /\.blog-layout/, /\.breadcrumb/, /\.article-header/, /\.article-topics/,
      /\.article-title/, /\.article-meta/, /\.article-credit/, /\.article-cover/,
      /\.desk-icon/, /\.author-avatar/, /\.author-link/, /\.meta-/,
    ],
  },
  'blog-list': {
    url: '/blog/topic/connectors',
    source: ['marketing-bundle.css', 'blog-news.css'],
    forceInclude: [
      '.page-blog', '.page-blog-topic', '.page-blog-list',
      /\.category-header/, /\.featured-section/, /\.featured-card/,
      /\.featured-cover/, /\.featured-content/, /\.featured-meta/,
      /\.featured-title/, /\.featured-excerpt/, /\.featured-author/,
      /\.blog-layout/, /\.blog-list/, /\.blog-card-h/, /\.breadcrumb/,
      /\.tag/, /\.desk-icon/,
    ],
  },
  magazine: {
    url: '/blog/',
    source: ['marketing-bundle.css', 'mag.css'],
    forceInclude: [
      '.page-magazine',
      /\.mag-/,
    ],
  },
  // Branded error page (errors/site_error.html, served for 404/500/403/etc.).
  // Extends base_marketing and loads site-error.css async, so without a per-page
  // critical the default marketing critical (which omits the .dsn-* mega-nav hide
  // rules) flashed the expanded mega-nav on every error hit — the same FOUC cause
  // fixed across the other surfaces. The whole error page is above the fold, so
  // this critical carries the chrome floor + .dsn-* hide rules plus the full
  // .error-* hero/actions/grid. Generated against a live 404 URL.
  'site-error': {
    url: '/this-page-intentionally-404-for-critical-css',
    source: ['marketing-bundle.css', 'site-error.css'],
    forceInclude: [
      '.page-site-error', '.page-error-404',
      /\.error-page/, /\.error-shell/, /\.error-hero/, /\.error-badge/,
      /\.error-code/, /\.error-title/, /\.error-lead/, /\.error-actions/,
      /\.error-btn/, /\.error-grid/, /\.error-card/,
    ],
  },
  // Editorial desk page (/teams/<slug>, team_desk.html) — loads blog-news.css
  // async like the rest of the blog surface, so it needs its own critical to
  // carry the .dsn-* mega-nav hide rules plus the .author-hero above-fold.
  'team-desk': {
    url: '/teams/crypto-desk',
    source: ['marketing-bundle.css', 'blog-news.css'],
    forceInclude: [
      '.page-blog', '.page-team-desk',
      /\.author-hero/, /\.desk-hero-icon/, /\.desk-icon/,
      /\.author-role/, /\.blog-layout/, /\.section-title/,
    ],
  },
  // TradingView indicators VIP landing (/tradingview, tradingview_indicators.html)
  // — loads tradingview-indicators.css async, so this critical carries the shared
  // chrome floor + the .dsn-* mega-nav hide rules the default marketing critical
  // omits (the FOUC cause) plus the .tvx-* hero + chart window above the fold.
  'tradingview-indicators': {
    url: '/tradingview',
    source: ['marketing-bundle.css', 'tradingview-indicators.css'],
    forceInclude: [
      '.page-tvx', '.tvx', '.tvx-accent',
      /\.tvx-eyebrow/, /\.tvx-hero/, /\.tvx-chart/, /\.tvx-candles/, /\.tvx-c\b/,
      /\.tvx-grid/, /\.tvx-zone/, /\.tvx-ema/, /\.tvx-mark/, /\.tvx-scale/,
      /\.tvx-last/, /\.tvx-dot/,
    ],
  },
  // Per-indicator child landing (/tradingview/<slug>, tradingview_indicator_detail.html)
  // — one critical serves all 10 spokes (same template). Generated against one child
  // URL, but forceInclude carries EVERY archetype overlay class so any child's
  // above-fold hero chart is covered, plus the .dsn-* mega-nav hide (the FOUC cause).
  'tradingview-indicator': {
    url: '/tradingview/smart-money-trap-scanner',
    source: ['marketing-bundle.css', 'tradingview-indicators.css'],
    forceInclude: [
      '.page-tvx', '.page-tvx-detail', '.tvx', '.tvx-accent',
      /\.tvx-eyebrow/, /\.tvx-hero/, /\.tvx-chart/, /\.tvx-candles/, /\.tvx-c\b/,
      /\.tvx-grid/, /\.tvx-zone/, /\.tvx-ema/, /\.tvx-atr-band/, /\.tvx-mark/,
      /\.tvx-scale/, /\.tvx-last/, /\.tvx-dot/, /\.tvx-sweep/, /\.tvx-level/,
      /\.tvx-spike-hl/, /\.tvx-screener/, /\.tvx-zone-label/, /\.tvx-osc/,
    ],
  },
};

// Rules appended verbatim below the penthouse output for specific pages, so
// they survive regeneration without a manual re-add. content-pipeline.css is
// not in any page's `source` (different dir, loaded async/separately), so
// penthouse can't extract from it — this is where its first-paint-critical
// rules live. The mermaid FOUC guard hides the raw-source <pre> until the
// library mounts the SVG (mirror of the rule in content-pipeline.css).
const MERMAID_FOUC =
  '\n/* mermaid FOUC guard — mirror of content-pipeline.css; hides the raw-source\n' +
  ' * <pre> until mermaid marks it data-processed so the source never flashes. */\n' +
  '.cp-figure-mermaid pre.mermaid:not([data-processed="true"]){visibility:hidden;overflow:hidden}\n';
const APPEND = {
  'blog-detail': MERMAID_FOUC,
  'trading-glossary-term': MERMAID_FOUC,
};

async function gen(name) {
  const cfg = PAGES[name];
  if (!cfg) throw new Error(`Unknown page "${name}". Known: ${Object.keys(PAGES).join(', ')}`);
  const combined = cfg.source
    .map((f) => fs.readFileSync(path.join(CSS_DIR, f), 'utf8'))
    .join('\n');
  const out = path.join(CSS_DIR, `${name}-critical.css`);
  const result = await penthouse({
    url: ORIGIN + cfg.url,
    cssString: combined,
    width: 412,
    height: 823,
    timeout: 90000,
    forceInclude: [...CHROME, ...cfg.forceInclude],
    propertiesToRemove: ['cursor'],
  });
  // penthouse changed its resolve shape across versions: older returns
  // {css}, newer resolves the CSS string directly. Tolerate both.
  const css = typeof result === 'string' ? result : result.css;
  const header =
    `/* ${name}-critical.css — above-fold subset of ${cfg.source.join(' + ')}.\n` +
    ` * Inlined into the page <head> via {% page_critical_css "${name}" %};\n` +
    ` * the full bundle loads async. Regenerate with\n` +
    ` *   node tools/gen_critical_css.js ${name}\n` +
    ` * after touching any above-fold rule. Hand-appended CLS reservations and\n` +
    ` * specificity bumps below the penthouse output are preserved by re-adding\n` +
    ` * them after a regen — see reference-critical-css-pitfalls. */\n`;
  fs.writeFileSync(out, header + css + (APPEND[name] || ''));
  console.log(`${name}: ${css.length} bytes (${(css.length / combined.length * 100).toFixed(1)}% of ${combined.length})`);
}

(async () => {
  const names = process.argv[2] ? [process.argv[2]] : Object.keys(PAGES);
  for (const n of names) await gen(n);
})().catch((e) => { console.error(e); process.exit(1); });
