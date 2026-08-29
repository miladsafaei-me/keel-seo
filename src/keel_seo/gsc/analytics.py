"""Search Analytics API — the complete query surface, not just the common case.

:mod:`keel_seo.gsc.connector` exposes the everyday query (dimensions, a window, a row
cap) and :mod:`keel_seo.gsc.live` pulls the dashboard's ranges. This module exposes
what those two deliberately leave out, so nothing in the API is unreachable:

* **dimensionFilterGroups** — filter by any dimension with ``equals``,
  ``notEquals``, ``contains``, ``notContains``, ``includingRegex`` or
  ``excludingRegex``. Written here as plain strings (``page contains /blog/``) so a
  filter is one CLI argument rather than a nested JSON body.
* **type** — ``web`` (default), ``image``, ``video``, ``news``, ``googleNews`` or
  ``discover``. Discover and Google News carry their own dimension restrictions:
  they have no ``query`` dimension at all, which is why a Discover pull that asks
  for queries returns a 400 rather than an empty list.
* **aggregationType** — ``auto`` (default), ``byPage`` or ``byProperty``. This
  changes what a "position" means, and it is the reason a by-page and a by-query
  total never reconcile exactly.
* **dataState** — ``final`` (default; ~2 days behind) or ``all`` (includes today's
  unfinalized "fresh" data, matching what the GSC UI shows for a recent range).
* **pagination** — the API caps one response at 25,000 rows; :func:`fetch_all`
  walks ``startRow`` until a short page arrives, so a large property's full query
  universe comes back whole.

CLI::

    python -m keel_seo.gsc.analytics --dimensions query,page --days 28
    python -m keel_seo.gsc.analytics --filter "page contains /blog/" --dimensions query
    python -m keel_seo.gsc.analytics --type discover --dimensions page --days 90
"""
from __future__ import annotations

import csv
import datetime as dt
import json
import sys

from .auth import DATA_LAG_DAYS, SCOPE_READONLY, GscError, execute, resolve_site, service

MAX_ROWS_PER_CALL = 25000

DIMENSIONS = ("query", "page", "country", "device", "date", "searchAppearance")
SEARCH_TYPES = ("web", "image", "video", "news", "googleNews", "discover")
AGGREGATION_TYPES = ("auto", "byPage", "byProperty")
DATA_STATES = ("final", "all")

OPERATORS = {
    "equals": "equals",
    "notequals": "notEquals",
    "contains": "contains",
    "notcontains": "notContains",
    "includingregex": "includingRegex",
    "excludingregex": "excludingRegex",
}


def parse_filter(expression: str) -> dict:
    """Turn ``"page contains /blog/"`` into one API filter dict.

    The expression is split into exactly three parts — dimension, operator, and the
    whole remainder as the expression — so a filter value containing spaces (a
    multi-word query, a URL with an encoded space) survives intact.
    """
    parts = expression.strip().split(None, 2)
    if len(parts) < 3:
        raise GscError(
            f"filter must read '<dimension> <operator> <value>', got: {expression!r}. "
            f"Operators: {', '.join(sorted(set(OPERATORS.values())))}"
        )
    dimension, operator, value = parts
    key = operator.replace("_", "").lower()
    if key not in OPERATORS:
        raise GscError(
            f"unknown filter operator {operator!r}. Use one of: "
            f"{', '.join(sorted(set(OPERATORS.values())))}"
        )
    if dimension not in DIMENSIONS:
        raise GscError(f"unknown filter dimension {dimension!r}. Use one of: {', '.join(DIMENSIONS)}")
    return {"dimension": dimension, "operator": OPERATORS[key], "expression": value}


def build_body(*, start_date: str, end_date: str, dimensions=("query",), filters=(),
               filter_group_type: str = "and", search_type: str = "web",
               aggregation_type: str = "auto", data_state: str = "final",
               row_limit: int = 1000, start_row: int = 0) -> dict:
    """Assemble a Search Analytics request body, validating every enum up front.

    Validating here rather than letting Google answer 400 keeps the error legible:
    the API's own message for a bad ``type`` names neither the field nor the allowed
    values.
    """
    if search_type not in SEARCH_TYPES:
        raise GscError(f"unknown type {search_type!r}. Use one of: {', '.join(SEARCH_TYPES)}")
    if aggregation_type not in AGGREGATION_TYPES:
        raise GscError(
            f"unknown aggregationType {aggregation_type!r}. Use one of: {', '.join(AGGREGATION_TYPES)}"
        )
    if data_state not in DATA_STATES:
        raise GscError(f"unknown dataState {data_state!r}. Use one of: {', '.join(DATA_STATES)}")
    unknown = [d for d in dimensions if d not in DIMENSIONS]
    if unknown:
        raise GscError(
            f"unknown dimension(s) {', '.join(unknown)}. Use any of: {', '.join(DIMENSIONS)}"
        )

    body = {
        "startDate": start_date,
        "endDate": end_date,
        "dimensions": list(dimensions),
        "type": search_type,
        "aggregationType": aggregation_type,
        "dataState": data_state,
        "rowLimit": min(row_limit, MAX_ROWS_PER_CALL),
        "startRow": start_row,
    }
    parsed = [f if isinstance(f, dict) else parse_filter(f) for f in filters]
    if parsed:
        body["dimensionFilterGroups"] = [
            {"groupType": filter_group_type, "filters": parsed}
        ]
    return body


def window(days: int = 28, start: str = "", end: str = "", *, data_state: str = "final"):
    """Resolve a date window: an explicit start/end pair, else a trailing ``days``.

    A ``final`` window ends at the last finalized day (two days back); an ``all``
    window may end today, because that is exactly what ``dataState="all"`` buys.
    """
    if start or end:
        if not (start and end):
            raise GscError("start and end must be given together")
        return start, end
    last = dt.date.today() if data_state == "all" else dt.date.today() - dt.timedelta(days=DATA_LAG_DAYS)
    return (last - dt.timedelta(days=days - 1)).isoformat(), last.isoformat()


def query(site: str = "", **kwargs) -> list:
    """One Search Analytics call. Returns the raw rows (``keys`` + metrics)."""
    body = build_body(**kwargs)
    request = service(scopes=(SCOPE_READONLY,)).searchanalytics().query(
        siteUrl=resolve_site(site), body=body
    )
    return execute(request, what="search analytics query").get("rows", []) or []


def fetch_all(site: str = "", *, max_rows: int = 0, **kwargs) -> list:
    """Every row for a query, walking ``startRow`` past the 25,000-row response cap.

    ``max_rows=0`` means "no ceiling beyond what the property has". The loop stops on
    the first short page, which is the API's own end-of-data signal.
    """
    rows: list = []
    start_row = kwargs.pop("start_row", 0)
    kwargs.pop("row_limit", None)
    resolved = resolve_site(site)
    svc = service(scopes=(SCOPE_READONLY,)).searchanalytics()
    while True:
        body = build_body(row_limit=MAX_ROWS_PER_CALL, start_row=start_row, **kwargs)
        page = execute(
            svc.query(siteUrl=resolved, body=body), what="search analytics query"
        ).get("rows", []) or []
        rows.extend(page)
        if len(page) < MAX_ROWS_PER_CALL:
            break
        start_row += MAX_ROWS_PER_CALL
        if max_rows and len(rows) >= max_rows:
            break
    return rows[:max_rows] if max_rows else rows


def to_records(rows, dimensions) -> list:
    """Zip raw ``keys`` arrays back onto their dimension names, one dict per row."""
    out = []
    for row in rows:
        record = dict(zip(dimensions, row.get("keys", [])))
        record.update(
            {
                "clicks": row.get("clicks", 0),
                "impressions": row.get("impressions", 0),
                "ctr": row.get("ctr", 0.0),
                "position": row.get("position", 0.0),
            }
        )
        out.append(record)
    return out


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(
        prog="keel-seo-analytics", description="Search Analytics API (full surface)"
    )
    parser.add_argument("--site", default="")
    parser.add_argument("--days", type=int, default=28)
    parser.add_argument("--start", default="")
    parser.add_argument("--end", default="")
    parser.add_argument("--dimensions", default="query")
    parser.add_argument("--filter", action="append", default=[],
                        help="'<dimension> <operator> <value>', repeatable")
    parser.add_argument("--filter-group-type", default="and", choices=["and", "or"])
    parser.add_argument("--type", dest="search_type", default="web", choices=list(SEARCH_TYPES))
    parser.add_argument("--aggregation", default="auto", choices=list(AGGREGATION_TYPES))
    parser.add_argument("--data-state", default="final", choices=list(DATA_STATES))
    parser.add_argument("--limit", type=int, default=1000)
    parser.add_argument("--all", action="store_true", help="paginate past the 25,000-row cap")
    parser.add_argument("--json", dest="json_out")
    parser.add_argument("--csv", dest="csv_out")

    args = parser.parse_args()
    dimensions = [d.strip() for d in args.dimensions.split(",") if d.strip()]
    try:
        start, end = window(args.days, args.start, args.end, data_state=args.data_state)
        common = dict(
            start_date=start,
            end_date=end,
            dimensions=dimensions,
            filters=args.filter,
            filter_group_type=args.filter_group_type,
            search_type=args.search_type,
            aggregation_type=args.aggregation,
            data_state=args.data_state,
        )
        if args.all:
            rows = fetch_all(args.site, max_rows=args.limit if args.limit else 0, **common)
        else:
            rows = query(args.site, row_limit=args.limit, **common)
        records = to_records(rows, dimensions)
    except GscError as exc:
        print(f"error: {exc}", file=sys.stderr)
        sys.exit(1)

    print(
        f"# {resolve_site(args.site)}  {start} -> {end}  type={args.search_type}  "
        f"dims={','.join(dimensions)}  rows={len(records)}"
    )
    if not records:
        print("(no rows — widen the window, relax the filters, or check the property has traffic)")
        return
    header = dimensions + ["clicks", "impressions", "ctr", "position"]
    widths = {h: max(len(h), 8) for h in header}
    table = []
    for record in records:
        line = [str(record.get(d, "")) for d in dimensions] + [
            str(int(record["clicks"])),
            str(int(record["impressions"])),
            f"{record['ctr'] * 100:.2f}%",
            f"{record['position']:.1f}",
        ]
        table.append(line)
        for name, cell in zip(header, line):
            widths[name] = max(widths[name], len(cell))
    fmt = "  ".join("{:<" + str(widths[h]) + "}" for h in header)
    print(fmt.format(*header))
    for line in table:
        print(fmt.format(*line))

    if args.json_out:
        with open(args.json_out, "w") as handle:
            json.dump(records, handle, indent=2)
        print(f"wrote {args.json_out}", file=sys.stderr)
    if args.csv_out:
        with open(args.csv_out, "w", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=header)
            writer.writeheader()
            writer.writerows(records)
        print(f"wrote {args.csv_out}", file=sys.stderr)


if __name__ == "__main__":
    main()
