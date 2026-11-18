# keel_seo.audit: the site audit engine

The deterministic half of the keel-kit `/seo-strategy` skill. It audits a site from the outside,
so it works for an agency client whose code we never see. No model is called here: the engine
collects, joins and filters, and specialist agents judge only the candidates it hands them.

Install: `pip install 'keel-seo[gsc,proxies,audit]'`.

## Commands

```bash
python -m keel_seo.audit init <workspace>        # starter project.json
python -m keel_seo.audit gsc <workspace>         # Search Console: six spans, query by page, 16 months
python -m keel_seo.audit crawl <workspace>       # sitemaps, links and Search Console URLs, through the proxy pool
python -m keel_seo.audit build <workspace>       # page families, the inventory table, every fact file
python -m keel_seo.audit build <workspace> --only intent,competitor   # refresh fact files, inventory untouched
python -m keel_seo.audit psi <workspace>         # PageSpeed for one page per family
python -m keel_seo.audit serp-plan <workspace>   # the queries worth a result lookup
python -m keel_seo.audit facts <workspace> <module | overview>
python -m keel_seo.audit facts <workspace> page <url>
python -m keel_seo.audit facts <workspace> family <name>
python -m keel_seo.audit facts <workspace> sql "<SELECT ...>"
python -m keel_seo.audit findings <workspace> validate [module]
python -m keel_seo.audit findings <workspace> reconcile
python -m keel_seo.audit findings <workspace> export
python -m keel_seo.audit event <workspace> <YYYY-MM-DD>   # a dated break: 14 days before against 14 after
python -m keel_seo.audit report-data <workspace>          # every number the report shows, checklist statuses included
```

## What each stage does

| Module | Does |
|---|---|
| `crawl.py`, `egress.py`, `parse.py` | Reads robots.txt and every sitemap, fetches each declared URL, each internal link and each URL Search Console has shown, and stores status, redirect hops, canonical, directives, title, description, heading outline, main text, JSON-LD types and faults, and every link with its anchor and the page zone it sits in. Every request leaves through keel-crawler's proxy pool; there is no direct mode. Resumable. |
| `gscdata.py` | Six spans (28 days and the 28 before, 91 days, the 91 before and the same 91 a year earlier, 16 months), query-by-page rows, monthly cuts per page, daily totals, and country, device, appearance and query totals. |
| `analyze.py` | Assigns page families from URL shapes, computes inlinks, depth and internal rank, joins everything into `inventory` (one row per URL) and writes one fact file per module with family summaries and candidate lists. |
| `facts.py` | Narrow reads for an agent: a fact file, one page, one family, or one read-only SELECT with a row cap. |
| `findings.py` | Validates an agent's findings file, resolves its targets (URL list, family with a filter, or the site), applies the verifier's verdicts and the strategist's decisions, leaves one action per URL, and lists conflicts and risky removals. Exports CSV and a workbook. |
| `psi.py` | Field and lab numbers from Google's PageSpeed API for the top page of each family. |

## The workspace

`project.json` (the card), `data/` (the database and raw files), `facts/`, `findings/`,
`report/`. `data/` holds query strings and page text: keyword and client data, never committed.

## Limits worth knowing

- Raw server HTML only. Content that needs JavaScript is not seen; such pages show up as thin and
  are listed for a rendered check.
- Index coverage is inferred from impressions. URL Inspection needs a Full user and is not called.
- No backlink data and no search volume. Nothing in the fact files states either.
- gzip sitemap files are reported and skipped.
- Page families come from URL shapes. A site whose URLs do not separate its page types needs
  `families` overrides in `project.json` (`name` plus a regex).

The finding schema the agents write is in keel-kit,
`skills/seo-strategy/references/specialist-contract.md`.
