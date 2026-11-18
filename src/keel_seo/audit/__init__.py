"""Site audit engine: the deterministic half of the /seo-strategy skill.

It audits any site from the outside (a crawl plus Search Console), so it works
for an agency client whose code we never see as well as for a Keel consumer.
Nothing here calls a model. The engine collects, joins and filters; specialist
agents then judge only the candidates it hands them, and its ``findings``
commands validate and reconcile what they return.

A run lives in one workspace folder (``project.json`` plus ``data/``,
``facts/``, ``findings/`` and ``report/``). Stages, each a CLI subcommand of
``python -m keel_seo.audit``:

    init       write a starter project.json
    crawl      sitemap discovery plus a pooled crawl into data/audit.sqlite
    gsc        Search Console pull (16 months) into the same database
    psi        PageSpeed field and lab numbers for one sample page per family
    build      page families, the one-row-per-URL inventory, every candidate list
    facts      print one module's fact file, or one page's full record
    serp-plan  the priority queries a SERP collector should look up
    findings   validate agent findings, reconcile them to one verdict per URL

See README.md in this folder for the workspace contract and the finding schema.
"""

__all__ = ["MODULES"]

# Specialist modules, in the order the skill runs them. Each one has a fact
# file (facts/<module>.json) and expects a findings file (findings/<module>.json).
MODULES = (
    "technical",
    "architecture",
    "inventory",
    "cannibalization",
    "scaled",
    "intent",
    "structure",
    "snippet",
    "performance",
    "competitor",
)
