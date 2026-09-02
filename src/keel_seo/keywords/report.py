"""Write a crawl out four ways, for four different readers.

JSON is the record a later run or another script reads. CSV is the flat form.
Markdown is what a person reads to decide what to build. XLSX is the one people
actually work in — sheets for the keywords, the clusters, the provenance and the
contamination check, with filters already set up.

All of them carry the run's metadata, and the metadata always names the egress
country rather than a requested one, because that is the only geography an
autocomplete harvest actually has. The volume caveat travels inside every format
too: a file outlives the terminal that produced it.
"""
from __future__ import annotations

import csv
import datetime as dt
import json
import os

from .cluster import Cluster
from .crawl import Universe

CONTAMINATION_SAMPLE = 40


def metadata(universe: Universe, clusters: list[Cluster], egress: dict,
             client) -> dict:
    pool = getattr(client, "pool", None)
    return {
        "seed": universe.seed,
        "harvested_at": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "source": "google-autocomplete",
        "endpoint_client": client.client,
        "language": client.hl,
        "vertical": client.ds or "web",
        "egress_ip": egress.get("ip", ""),
        "egress_country": egress.get("country", "unknown"),
        "egress_org": egress.get("org", ""),
        "phrases": len(universe.phrases),
        "clusters": len(clusters),
        "queries_asked": universe.queries_asked,
        "network_calls": universe.network_calls,
        "cache_hits": universe.cache_hits,
        "errors": universe.errors,
        "levels_run": universe.levels_run,
        "unexpanded_phrases": universe.unexpanded,
        "exhausted": universe.exhausted,
        "stopped_by_rate_limit": universe.blocked,
        "rate_limited_responses": universe.rate_limited,
        "elapsed_seconds": round(universe.elapsed, 1),
        "off_seed_rejected": len(universe.off_seed),
        "proxy_pool": pool.stats() if pool is not None else None,
        "per_level": universe.per_level,
        "volume_note": (
            "Autocomplete never returns search volume. priority ranks demand "
            "shape (Google's own ordering, breadth of reach, relevance score, "
            "depth) and is not a volume estimate."
        ),
    }


def write_json(path: str, universe: Universe, clusters: list[Cluster],
               meta: dict) -> None:
    contamination = sorted(universe.off_seed.items(), key=lambda kv: -kv[1])
    payload = {
        "meta": meta,
        "clusters": [c.as_row() for c in clusters],
        "off_seed_sample": [
            {"phrase": phrase, "times_returned": count}
            for phrase, count in contamination[:CONTAMINATION_SAMPLE]
        ],
    }
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=1)


def write_csv(path: str, clusters: list[Cluster]) -> None:
    with open(path, "w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(
            ["priority", "keyword", "cluster", "cluster_label", "intent",
             "best_rank", "reach", "relevance", "level", "words"]
        )
        for cluster in clusters:
            for phrase in cluster.members:
                writer.writerow(
                    [round(phrase.priority, 1), phrase.text, cluster.index,
                     cluster.label, cluster.intent, phrase.best_rank, phrase.reach,
                     phrase.max_relevance, phrase.first_level, phrase.words]
                )


def write_markdown(path: str, universe: Universe, clusters: list[Cluster],
                   meta: dict, *, per_cluster: int = 12) -> None:
    lines: list[str] = []
    add = lines.append
    add(f"# Keyword universe — `{universe.seed}`")
    add("")
    if meta.get("egress_country") == "mixed":
        add(f"**Source:** Google autocomplete only ({meta['endpoint_client']} client, "
            f"`hl={meta['language']}`, {meta['vertical']} vertical). "
            f"**Egress: mixed** — {meta['egress_org']}. Autocomplete answers by "
            "requesting IP, so a rotating pool produces a deliberately "
            "multi-country harvest. Read this as *what the term is asked, broadly* "
            "rather than as one market's demand: some phrases below will be local "
            "to a single country, and the exact mix is not reproducible.")
    else:
        add(f"**Source:** Google autocomplete only ({meta['endpoint_client']} client, "
            f"`hl={meta['language']}`, {meta['vertical']} vertical). "
            f"**Egress:** {meta['egress_country']} ({meta['egress_ip']}) — autocomplete "
            "geography is the requesting IP, never a parameter, so this is the market "
            "the harvest actually reflects.")
    add("")
    add(f"**Harvested:** {meta['harvested_at']} · "
        f"**{meta['phrases']:,} phrases** in **{meta['clusters']} clusters** from "
        f"{meta['queries_asked']:,} queries in {meta['elapsed_seconds']}s.")
    add("")
    add("> Autocomplete returns no search volume, and no parameter exists that "
        "would make it. `priority` below ranks demand *shape* — Google's own "
        "ordering, how many independent expansions surfaced a phrase, its "
        "relevance score and its depth. It is not a volume estimate and must not "
        "be presented as one.")
    add("")
    if universe.blocked:
        add(f"⚠️ **Google rate-limited this harvest** after "
            f"{meta['network_calls']:,} requests and it stopped early, with "
            f"{meta['unexpanded_phrases']:,} phrases left unexpanded. What is below "
            "was collected before the block and is sound; it is not the complete "
            "universe. Re-run later with a lower `--rate` — the cache means the "
            "work already done is not repeated.")
        add("")
    elif not universe.exhausted:
        add(f"⚠️ The crawl stopped with **{meta['unexpanded_phrases']:,} phrases "
            "left unexpanded** (budget or level cap reached), so this universe is "
            "wide but not closed. Re-run with a higher `--budget` / `--levels` to "
            "continue; the cache makes the repeat work free.")
        add("")
    else:
        add("✅ The crawl closed on its own: every phrase it found was re-seeded "
            "and produced nothing new. This is the complete universe at this "
            "grammar, language and egress.")
        add("")

    add("## Clusters, most valuable first")
    add("")
    add("| # | Cluster | Intent | Phrases | Priority | Head phrase |")
    add("|---|---|---|---|---|---|")
    for cluster in clusters:
        add(f"| {cluster.index} | {cluster.label} | {cluster.intent} | "
            f"{cluster.size} | {cluster.priority:.0f} | {cluster.head.text} |")
    add("")

    add("## Inside each cluster")
    add("")
    for cluster in clusters:
        add(f"### {cluster.index}. {cluster.label} "
            f"({cluster.intent}, {cluster.size} phrases)")
        add("")
        add("| Priority | Keyword | Rank | Reach | Level |")
        add("|---|---|---|---|---|")
        for phrase in cluster.members[:per_cluster]:
            add(f"| {phrase.priority:.0f} | {phrase.text} | {phrase.best_rank} | "
                f"{phrase.reach} | {phrase.first_level} |")
        if cluster.size > per_cluster:
            add(f"| | *…{cluster.size - per_cluster} more in the CSV* | | | |")
        add("")

    contamination = sorted(universe.off_seed.items(), key=lambda kv: -kv[1])
    if contamination:
        add("## Off-seed neighbours (contamination check)")
        add("")
        add("Phrases Google returned that do **not** contain the seed. A term "
            "whose neighbours belong to another industry is a term whose traffic "
            "will too.")
        add("")
        add("| Times returned | Phrase |")
        add("|---|---|")
        for phrase, count in contamination[:CONTAMINATION_SAMPLE]:
            add(f"| {count} | {phrase} |")
        add("")

    with open(path, "w", encoding="utf-8") as handle:
        handle.write("\n".join(lines) + "\n")


def write_xlsx(path: str, universe: Universe, clusters: list[Cluster],
               meta: dict) -> bool:
    """Write the workbook people actually work in. False if openpyxl is absent.

    Four sheets rather than one, because a harvest gets read three different ways
    and a single flat dump serves none of them well:

    * **Keywords** — every phrase in priority order, carrying its cluster and
      intent, with a frozen header and an autofilter so it can be sorted and
      sliced without setting anything up first. This is the working sheet.
    * **Clusters** — one row per topic, which is the unit a page is built
      against. Reading it off the Keywords sheet would mean scrolling 2,000 rows
      to see 700 topics.
    * **Run** — where the numbers came from: source, egress, counts, and the
      volume caveat in full. A spreadsheet outlives the terminal it was produced
      in, so the caveat has to travel inside the file rather than beside it.
    * **Off-seed** — what Google returned that did *not* contain the seed, which
      is how a term whose neighbours belong to another industry shows itself.

    openpyxl is an optional dependency (`pip install 'keel-seo[xlsx]'`); the other
    three formats are stdlib and always written, so a missing library costs the
    workbook and nothing else.
    """
    try:
        from openpyxl import Workbook
        from openpyxl.styles import Alignment, Font, PatternFill
        from openpyxl.utils import get_column_letter
    except ImportError:
        return False

    book = Workbook()
    header_font = Font(bold=True, color="FFFFFF")
    header_fill = PatternFill("solid", fgColor="1F3864")

    def style_header(sheet, widths):
        for index, width in enumerate(widths, 1):
            sheet.column_dimensions[get_column_letter(index)].width = width
        for cell in sheet[1]:
            cell.font = header_font
            cell.fill = header_fill
            cell.alignment = Alignment(vertical="center")
        sheet.freeze_panes = "A2"
        sheet.auto_filter.ref = sheet.dimensions

    keywords = book.active
    keywords.title = "Keywords"
    keywords.append(["Priority", "Keyword", "Cluster", "Cluster label", "Intent",
                     "Best rank", "Reach", "Relevance", "Level", "Words"])
    for cluster in clusters:
        for phrase in cluster.members:
            keywords.append([round(phrase.priority, 1), phrase.text, cluster.index,
                             cluster.label, cluster.intent, phrase.best_rank,
                             phrase.reach, phrase.max_relevance, phrase.first_level,
                             phrase.words])
    style_header(keywords, [9, 52, 9, 30, 15, 11, 8, 11, 8, 8])

    topics = book.create_sheet("Clusters")
    topics.append(["Cluster", "Label", "Intent", "Phrases", "Priority", "Head phrase"])
    for cluster in clusters:
        topics.append([cluster.index, cluster.label, cluster.intent, cluster.size,
                       round(cluster.priority, 1), cluster.head.text])
    style_header(topics, [9, 32, 15, 10, 10, 52])

    run = book.create_sheet("Run")
    run.append(["Field", "Value"])
    for key in ("seed", "harvested_at", "source", "endpoint_client", "language",
                "vertical", "egress_country", "egress_ip", "egress_org", "phrases",
                "clusters", "queries_asked", "network_calls", "cache_hits", "errors",
                "levels_run", "unexpanded_phrases", "exhausted", "stopped_by_rate_limit",
                "elapsed_seconds", "off_seed_rejected"):
        run.append([key, str(meta.get(key, ""))])
    run.append(["volume_note", meta.get("volume_note", "")])
    if meta.get("proxy_pool"):
        run.append(["proxy_pool", str(meta["proxy_pool"])])
    style_header(run, [26, 110])

    contamination = sorted(universe.off_seed.items(), key=lambda kv: -kv[1])
    if contamination:
        off = book.create_sheet("Off-seed")
        off.append(["Times returned", "Phrase Google returned that lacks the seed"])
        for phrase, count in contamination[:200]:
            off.append([count, phrase])
        style_header(off, [16, 60])

    book.save(path)
    return True


def write_all(outdir: str, universe: Universe, clusters: list[Cluster],
              meta: dict) -> dict[str, str]:
    os.makedirs(outdir, exist_ok=True)
    stem = "".join(c if c.isalnum() else "-" for c in universe.seed.lower()).strip("-")
    paths = {
        "json": os.path.join(outdir, f"{stem}.json"),
        "csv": os.path.join(outdir, f"{stem}.csv"),
        "md": os.path.join(outdir, f"{stem}.md"),
    }
    write_json(paths["json"], universe, clusters, meta)
    write_csv(paths["csv"], clusters)
    write_markdown(paths["md"], universe, clusters, meta)

    xlsx_path = os.path.join(outdir, f"{stem}.xlsx")
    if write_xlsx(xlsx_path, universe, clusters, meta):
        paths["xlsx"] = xlsx_path
    return paths
