"""Find the Google Play phrases whose demand is answered by weak apps.

    python -m keel_seo.playstore --seed "prop firm" --out <dir>
    python -m keel_seo.playstore --seed "aquarium" --seed "fish tank" --gl gb --out <dir>

Four steps, each cached in ``<out>/.playstore-cache.jsonl`` so a stopped run
resumes for free:

1. expand the seed through Play's autocomplete (:mod:`.expand`),
2. search every phrase and read the details of its top apps,
3. score each phrase for demand and weakness (:mod:`.score`),
4. pull the low-star reviews of the weak, installed apps behind the best
   phrases, and count what they repeat (:mod:`.complaints`).
"""
from __future__ import annotations

import argparse
import datetime as dt
import sys
from concurrent.futures import ThreadPoolExecutor

from .client import DEFAULT_RATE, Blocked, Cache, PlayClient
from .complaints import complaint_terms
from .expand import expand
from .report import CAVEAT, app_rows, keyword_rows, write_all
from .score import WEAK_RATING, score_keyword


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m keel_seo.playstore",
                                     description=__doc__.split("\n")[0])
    parser.add_argument("--seed", action="append", required=True,
                        help="a seed phrase; repeat for several")
    parser.add_argument("--out", required=True, help="directory for the outputs")
    parser.add_argument("--hl", default="en", help="language to ask in (default en)")
    parser.add_argument("--gl", default="us", help="store country (default us)")
    parser.add_argument("--depth", type=int, default=2,
                        help="autocomplete drilling levels (default 2)")
    parser.add_argument("--drill", type=int, default=8,
                        help="phrases per level that earn their own alphabet (default 8)")
    parser.add_argument("--max-phrases", type=int, default=200,
                        help="cap on phrases scored (default 200)")
    parser.add_argument("--top", type=int, default=10,
                        help="apps per phrase that are measured and scored (default 10)")
    parser.add_argument("--review-phrases", type=int, default=10,
                        help="best phrases whose weak apps get their reviews read (default 10)")
    parser.add_argument("--review-apps", type=int, default=3,
                        help="weak apps per phrase whose reviews are read (default 3)")
    parser.add_argument("--reviews", type=int, default=150,
                        help="low-star reviews per app (default 150)")
    parser.add_argument("--min-installs", type=int, default=10_000,
                        help="an app needs this many installs to have its reviews read")
    parser.add_argument("--rate", type=float, default=DEFAULT_RATE,
                        help=f"requests per second (default {DEFAULT_RATE:g})")
    parser.add_argument("--workers", type=int, default=4)
    return parser


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    import os  # noqa: PLC0415
    os.makedirs(args.out, exist_ok=True)
    client = PlayClient(hl=args.hl, gl=args.gl, rate=args.rate,
                        cache=Cache(os.path.join(args.out, ".playstore-cache.jsonl")))
    say = lambda msg: print(msg, file=sys.stderr, flush=True)  # noqa: E731
    try:
        phrases = expand(client, args.seed, depth=args.depth, drill=args.drill,
                         max_phrases=args.max_phrases, workers=args.workers, progress=say)
        say(f"{len(phrases)} phrases; searching each")
        with ThreadPoolExecutor(max_workers=args.workers) as pool:
            results = dict(zip([p.text for p in phrases],
                               pool.map(lambda p: _safe(client.search, p.text, say) or [],
                                        phrases)))
        ranks: dict[str, list] = {}
        for phrase, rows in results.items():
            for pos, row in enumerate(rows[: args.top], 1):
                ranks.setdefault(row["appId"], []).append((phrase, pos))
        say(f"{len(ranks)} distinct apps in the top {args.top}; reading details")
        with ThreadPoolExecutor(max_workers=args.workers) as pool:
            apps = dict(zip(ranks, pool.map(lambda a: _safe(client.details, a, say), ranks)))
        scores = [score_keyword(p, [apps.get(r["appId"]) for r in rows[: args.top]],
                                top=args.top) for p, rows in results.items()]
        keywords = keyword_rows(scores, phrases)
        complaints, terms = _complaints(client, keywords, scores, apps, args, say)
    except Blocked as exc:
        say(f"STOPPED: {exc}. Everything fetched is cached; rerun later to resume.")
        return 2
    meta = {
        "seeds": args.seed, "hl": args.hl, "gl": args.gl,
        "date": dt.date.today().isoformat(), "phrases": len(keywords),
        "apps": len(ranks), "top_per_phrase": args.top, "depth": args.depth,
        "network_calls": client.calls, "cache_hits": client.cache.hits,
        "errors": client.errors, "complaint_reviews": len(complaints),
        "source": "Google Play autocomplete (batchexecute IJ4APc) + google-play-scraper",
    }
    paths = write_all(args.out, args.seed[0], meta, keywords,
                      app_rows(apps, ranks), complaints, terms)
    say(f"\n{CAVEAT}\n")
    print("rank  opp  demand weak  phrase")
    for i, row in enumerate(keywords[:25], 1):
        print(f"{i:>4} {row['opportunity']:>5} {row['demand']:>6} {row['weakness']:>5}  "
              f"{row['phrase']}  [{row['reasons']}]")
    for kind, path in paths.items():
        print(f"{kind}: {path}")
    return 0


def _safe(fn, arg, say):
    try:
        return fn(arg)
    except Blocked:
        raise
    except Exception as exc:  # noqa: BLE001 - one app or phrase is not the run
        say(f"  {exc}")
        return None


def _complaints(client, keywords, scores, apps, args, say):
    by_phrase = {s.phrase: s for s in scores}
    chosen: dict[str, str] = {}
    for row in keywords[: args.review_phrases]:
        weak = [a for a in by_phrase[row["phrase"]].relevant
                if (a.get("realInstalls") or 0) >= args.min_installs
                and (not a.get("score") or a["score"] < WEAK_RATING + 0.3)]
        for app in weak[: args.review_apps]:
            chosen.setdefault(app["appId"], app.get("title") or app["appId"])
    say(f"reading low-star reviews of {len(chosen)} apps")
    complaints, terms = [], []
    for app_id, title in chosen.items():
        reviews = _safe(lambda a: client.low_reviews(a, args.reviews), app_id, say) or []
        complaints += [{"app": title, "appId": app_id, **r} for r in reviews]
        terms += [{"app": title, "appId": app_id, "term": t, "reviews": n}
                  for t, n in complaint_terms(reviews)]
    complaints.sort(key=lambda r: (r["appId"], -(r.get("thumbs") or 0)))
    return complaints, terms


if __name__ == "__main__":
    sys.exit(main())
