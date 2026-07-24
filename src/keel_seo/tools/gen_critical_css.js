/*
 * gen_critical_css.js — generate the above-fold critical-CSS subset for any page
 * that loads its full stylesheet async (penthouse extraction). Inlining this subset
 * makes first paint match the final layout (no FOUC, no CLS).
 *
 * This is the NEUTRAL runner: it carries no project pages of its own. The host supplies
 * its page registry as a JS module (regex forceInclude patterns need JS, not JSON):
 *
 *   module.exports = { CSS_DIR, CHROME, PAGES, APPEND };
 *
 *   CSS_DIR  absolute dir holding the source sheets AND the written <name>-critical.css
 *   CHROME   selectors/regexes force-kept on every page (shared header/nav/tokens)
 *   PAGES    { <name>: { url, source: [<sheet>...], forceInclude: [selector|regex...] } }
 *   APPEND   { <name>: "<verbatim css appended below the penthouse output>" } (optional)
 *
 * Run from anywhere with Node + Chromium (penthouse installed):
 *   CRITICAL_CSS_CONFIG=/path/to/project/tools/critical-css.pages.js \
 *   ORIGIN=http://127.0.0.1:8082 \
 *   node gen_critical_css.js [page-name]        # omit name = all pages
 *
 * If REPO is set instead of CRITICAL_CSS_CONFIG, the registry is looked up at
 * $REPO/tools/critical-css.pages.js.
 *
 * forceInclude patterns are NOT ^-anchored — bundle selectors are descendant-scoped
 * (.page-x .hero) so an anchor would never match.
 */
const penthouse = require('penthouse');
const fs = require('fs');
const path = require('path');

const ORIGIN = process.env.ORIGIN || 'http://127.0.0.1:8082';
const CONFIG =
  process.env.CRITICAL_CSS_CONFIG ||
  (process.env.REPO && path.join(process.env.REPO, 'tools/critical-css.pages.js'));
if (!CONFIG) {
  console.error(
    'gen_critical_css: set CRITICAL_CSS_CONFIG (or REPO) to your page-registry module. '
    + 'It must export { CSS_DIR, CHROME, PAGES, APPEND }.'
  );
  process.exit(1);
}
const { CSS_DIR, CHROME = [], PAGES = {}, APPEND = {} } = require(path.resolve(CONFIG));

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
  // penthouse changed its resolve shape across versions: older returns {css},
  // newer resolves the CSS string directly. Tolerate both.
  const css = typeof result === 'string' ? result : result.css;
  const header =
    `/* ${name}-critical.css — above-fold subset of ${cfg.source.join(' + ')}.\n` +
    ` * Inlined into the page <head> via {% page_critical_css "${name}" %};\n` +
    ` * the full bundle loads async. Regenerate with\n` +
    ` *   node gen_critical_css.js ${name}\n` +
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
