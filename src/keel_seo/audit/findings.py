"""Validate what the specialist agents return and reconcile it per URL.

Agents write ``findings/<module>.json``. ``validate`` checks one file against
the schema and resolves each finding's targets to real URLs. ``reconcile``
drops what the verifier refuted, applies the strategist's decisions, leaves one
action per URL and lists every conflict and every risky removal. Decisions
live in ``findings/decisions.json``: ``{"findings": {"<id>": {"action": "drop"
or a new action, "severity": an optional new severity, "reason": ...}}, "urls": {"<url>": {"action": ...,
"merge_into": ..., "reason": ...}}}``. ``export``
writes the action workbook for the team that will do the work.
"""
from __future__ import annotations

import csv
import json
from collections import defaultdict

from . import MODULES
from .facts import _readonly
from .store import Workspace

SEVERITIES = ("critical", "high", "medium", "low")
CONFIDENCES = ("high", "medium", "low")
EFFORTS = ("S", "M", "L")
REMOVING = ("delete", "redirect", "merge", "noindex")
KEEPING = ("keep", "improve", "rewrite", "restructure", "relink", "fix")
ACTIONS = (*REMOVING, *KEEPING, "create", "investigate")
REQUIRED = ("id", "title", "severity", "confidence", "effort", "evidence", "targets", "action", "recommendation")
SITE_URL = "(site)"


def _resolve(conn, targets: dict) -> tuple[list, str]:
    """A finding's targets as URLs: a list, a family with a filter, or the site."""
    if not isinstance(targets, dict):
        return [], "targets must be an object"
    if targets.get("scope") == "site":
        return [SITE_URL], ""
    if "urls" in targets:
        urls = list(dict.fromkeys(targets["urls"]))
        known = {row["url"] for row in conn.execute(
            f"SELECT url FROM inventory WHERE url IN ({', '.join('?' for _url in urls)})", urls)} if urls else set()
        missing = [url for url in urls if url not in known]
        return urls, (f"{len(missing)} URL(s) are not in the inventory, first: {missing[0]}" if missing else "")
    if "family" in targets or "where" in targets:
        clauses, values = [], []
        if targets.get("family"):
            clauses.append("family = ?")
            values.append(targets["family"])
        if targets.get("where"):
            clauses.append(f"({targets['where']})")
        try:
            rows = conn.execute(f"SELECT url FROM inventory WHERE {' AND '.join(clauses)}", values).fetchall()
        except Exception as exc:  # noqa: BLE001 - reported back to the agent
            return [], f"the where filter does not run: {exc}"
        return [row["url"] for row in rows], ("" if rows else "the filter matches no page")
    return [], "targets needs urls, family (with an optional where) or scope: site"


def validate(workspace: Workspace, module: str) -> dict:
    path = workspace.findings / f"{module}.json"
    if not path.exists():
        return {"module": module, "ok": False, "errors": [f"{path} does not exist"]}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except ValueError as exc:
        return {"module": module, "ok": False, "errors": [f"not valid JSON: {exc}"]}
    errors, resolved = [], []
    for key in ("module", "summary", "findings"):
        if key not in payload:
            errors.append(f"top-level key {key} is missing")
    conn = _readonly(workspace)
    seen = set()
    try:
        for index, finding in enumerate(payload.get("findings", [])):
            label = finding.get("id") or f"finding #{index + 1}"
            for key in REQUIRED:
                if not finding.get(key):
                    errors.append(f"{label}: {key} is missing")
            if finding.get("id") in seen:
                errors.append(f"{label}: duplicate id")
            seen.add(finding.get("id"))
            for key, allowed in (("severity", SEVERITIES), ("confidence", CONFIDENCES),
                                 ("effort", EFFORTS), ("action", ACTIONS)):
                if finding.get(key) and finding[key] not in allowed:
                    errors.append(f"{label}: {key} must be one of {', '.join(allowed)}")
            urls, problem = _resolve(conn, finding.get("targets", {}))
            if problem:
                errors.append(f"{label}: {problem}")
            if finding.get("action") in ("merge", "redirect"):
                target = finding.get("merge_into", "")
                hit = conn.execute("SELECT status, indexable FROM inventory WHERE url=?", (target,)).fetchone()
                if not target:
                    errors.append(f"{label}: {finding['action']} needs merge_into")
                elif hit is None or not hit["indexable"]:
                    errors.append(f"{label}: merge_into {target} is not an indexable crawled page")
                elif target in urls:
                    errors.append(f"{label}: merge_into is also one of the targets")
            if finding.get("action") in (*REMOVING, "create") and finding.get("url_change") is not True:
                errors.append(f"{label}: an action that removes or creates a URL must set url_change to true")
            resolved.append({"id": finding.get("id"), "action": finding.get("action"), "pages": len(urls)})
    finally:
        conn.close()
    return {"module": module, "ok": not errors, "errors": errors, "findings": resolved,
            "unknowns": len(payload.get("unknowns", []))}


def _load(workspace: Workspace) -> tuple[list, dict, dict]:
    findings = []
    for module in MODULES:
        path = workspace.findings / f"{module}.json"
        if path.exists():
            payload = json.loads(path.read_text(encoding="utf-8"))
            for finding in payload.get("findings", []):
                findings.append({**finding, "module": module})
    checks, decisions = {}, {}
    verify = workspace.findings / "verify.json"
    if verify.exists():
        for check in json.loads(verify.read_text(encoding="utf-8")).get("checks", []):
            checks[check["finding_id"]] = check
    decided = workspace.findings / "decisions.json"
    if decided.exists():
        decisions = json.loads(decided.read_text(encoding="utf-8"))
    if not ({"findings", "urls"} & set(decisions)):
        decisions = {"urls": decisions, "findings": {}}
    return findings, checks, decisions


def reconcile(workspace: Workspace) -> dict:
    findings, checks, decisions = _load(workspace)
    conn = _readonly(workspace)
    inventory = {row["url"]: dict(row) for row in conn.execute(
        "SELECT url, family, status, hops, indexable, clicks_3m, impr_3m, clicks_16m, inlinks_content FROM inventory")}
    ruled = decisions.get("findings", {})
    url_decisions = decisions.get("urls", {})
    per_url: dict = defaultdict(list)
    merge_targets: dict = defaultdict(list)
    kept, dropped = [], []
    for finding in findings:
        check = checks.get(finding["id"], {})
        if check.get("verdict") == "refuted":
            dropped.append({"id": finding["id"], "reason": check.get("reason", "")})
            continue
        ruling = ruled.get(finding["id"])
        if ruling and ruling.get("action") == "drop":
            dropped.append({"id": finding["id"], "reason": "strategist: " + ruling.get("reason", "")})
            continue
        if ruling:
            finding["strategist_note"] = ruling.get("reason", "")
            if ruling.get("action"):
                finding["original_action"] = finding["action"]
                finding["action"] = ruling["action"]
                if ruling["action"] not in (*REMOVING, "create"):
                    finding["url_change"] = False
            if ruling.get("severity") in SEVERITIES and ruling["severity"] != finding["severity"]:
                finding["original_severity"] = finding["severity"]
                finding["severity"] = ruling["severity"]
        if check.get("verdict") == "weakened":
            finding["confidence"] = "low"
        finding["verification"] = check.get("verdict", "not checked")
        urls, _problem = _resolve(conn, finding.get("targets", {}))
        skip = set(check.get("exclude_urls", []))
        urls = [url for url in urls if url not in skip]
        if finding["action"] in KEEPING and "urls" not in finding.get("targets", {}):
            urls = [url for url in urls if url == SITE_URL
                    or (inventory.get(url, {}).get("status") == 200 and not inventory[url]["hops"])]
        finding["pages"] = len(urls)
        kept.append(finding)
        for url in urls:
            per_url[url].append(finding)
        if finding.get("merge_into"):
            merge_targets[finding["merge_into"]].append(finding["id"])
    conn.close()

    final, conflicts, risky = {}, [], []
    for url, items in per_url.items():
        if url == SITE_URL:
            continue
        removing = [item for item in items if item["action"] in REMOVING]
        keeping = [item for item in items if item["action"] in KEEPING]
        row = inventory.get(url, {})
        decision = url_decisions.get(url)
        flags = []
        if decision:
            action, merge_into = decision["action"], decision.get("merge_into", "")
            flags.append("decided by the strategist")
        elif removing:
            order = {name: index for index, name in enumerate(("merge", "redirect", "noindex", "delete"))}
            chosen = min(removing, key=lambda item: (order[item["action"]], SEVERITIES.index(item["severity"])))
            action, merge_into = chosen["action"], chosen.get("merge_into", "")
            problems = []
            if keeping:
                problems.append("one module removes it, another improves it")
            if len({item["action"] for item in removing}) > 1:
                problems.append("modules disagree on how to remove it")
            if len({item.get("merge_into", "") for item in removing if item.get("merge_into")}) > 1:
                problems.append("two different merge targets")
            if url in merge_targets:
                problems.append("it is also the merge target of another finding")
            if problems:
                conflicts.append({"url": url, "problems": problems,
                                  "findings": [{"id": item["id"], "action": item["action"],
                                                "merge_into": item.get("merge_into", "")} for item in items]})
                flags.append("conflict")
        else:
            order = {name: index for index, name in enumerate(("rewrite", "restructure", "improve", "fix", "relink", "keep"))}
            chosen = min(keeping, key=lambda item: order[item["action"]]) if keeping else items[0]
            action, merge_into = chosen["action"], ""
        if action in REMOVING and row:
            reasons = []
            if row["clicks_3m"] > 0:
                reasons.append(f"{row['clicks_3m']:.0f} clicks in the last 91 days")
            if row["impr_3m"] >= 100:
                reasons.append(f"{row['impr_3m']:.0f} impressions in the last 91 days")
            if row["inlinks_content"] >= 10:
                reasons.append(f"{row['inlinks_content']} content links point at it")
            if reasons and not any(checks.get(item["id"], {}).get("verdict") == "confirmed" for item in removing):
                risky.append({"url": url, "action": action, "why_risky": reasons,
                              "findings": [item["id"] for item in removing]})
                flags.append("risky removal, not confirmed")
        final[url] = {"url": url, "family": row.get("family", ""), "action": action, "merge_into": merge_into,
                      "findings": [item["id"] for item in items], "flags": flags,
                      "clicks_3m": row.get("clicks_3m", 0), "impr_3m": row.get("impr_3m", 0)}

    groups: dict = defaultdict(list)
    for conflict in conflicts:
        groups[tuple(sorted(f"{item['id']}:{item['action']}" for item in conflict["findings"]))].append(conflict["url"])
    conflict_groups = [{"findings": list(key), "pages": len(urls), "examples": urls[:3]}
                       for key, urls in sorted(groups.items(), key=lambda item: -len(item[1]))]
    summary = {
        "findings_kept": len(kept), "findings_refuted": dropped,
        "conflict_groups": conflict_groups,
        "findings_by_module": {module: sum(1 for item in kept if item["module"] == module) for module in MODULES},
        "findings_by_severity": {level: sum(1 for item in kept if item["severity"] == level) for level in SEVERITIES},
        "url_actions": {action: sum(1 for item in final.values() if item["action"] == action) for action in ACTIONS},
        "urls_with_an_action": len(final),
        "url_changes_needing_the_owner": sum(1 for item in final.values() if item["action"] in REMOVING),
        "new_pages_proposed": sum(1 for item in kept if item["action"] == "create"),
        "conflicts": conflicts, "risky_removals": risky,
    }
    (workspace.findings / "reconciled.json").write_text(json.dumps(
        {"summary": summary, "findings": kept, "urls": list(final.values())}, ensure_ascii=False, indent=1),
        encoding="utf-8")
    summary["conflicts"] = {"count": len(conflicts), "first": conflicts[:5]}
    summary["conflict_groups"] = conflict_groups[:20]
    summary["risky_removals"] = {"count": len(risky), "first": risky[:15]}
    return summary


def export(workspace: Workspace) -> dict:
    path = workspace.findings / "reconciled.json"
    if not path.exists():
        return {"error": "run findings reconcile first"}
    data = json.loads(path.read_text(encoding="utf-8"))
    finding_columns = ("id", "module", "severity", "confidence", "effort", "action", "pages", "title", "evidence",
                       "recommendation", "expected_effect", "depends_on", "url_change", "verification")
    url_columns = ("url", "family", "action", "merge_into", "findings", "flags", "clicks_3m", "impr_3m")
    order = {level: index for index, level in enumerate(SEVERITIES)}
    finding_rows = [[_cell(finding.get(column, "")) for column in finding_columns]
                    for finding in sorted(data["findings"], key=lambda item: order.get(item["severity"], 9))]
    url_rows = [[_cell(item.get(column, "")) for column in url_columns]
                for item in sorted(data["urls"], key=lambda item: (item["action"], -item["impr_3m"]))]
    workspace.report.mkdir(parents=True, exist_ok=True)
    written = []
    for name, columns, rows in (("findings", finding_columns, finding_rows), ("url-actions", url_columns, url_rows)):
        target = workspace.report / f"{name}.csv"
        with target.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.writer(handle)
            writer.writerow(columns)
            writer.writerows(rows)
        written.append(str(target))
    try:
        from openpyxl import Workbook
    except ImportError:
        return {"written": written, "xlsx": "openpyxl is not installed; the CSV files hold the same rows"}
    book = Workbook()
    for index, (title, columns, rows) in enumerate((("Findings", finding_columns, finding_rows),
                                                    ("URL actions", url_columns, url_rows))):
        sheet = book.active if index == 0 else book.create_sheet()
        sheet.title = title
        sheet.append(list(columns))
        for row in rows:
            sheet.append(row)
        sheet.freeze_panes = "A2"
    target = workspace.report / "actions.xlsx"
    book.save(target)
    written.append(str(target))
    return {"written": written, "findings": len(finding_rows), "url_actions": len(url_rows)}


def _cell(value):
    if isinstance(value, (list, tuple)):
        return ", ".join(str(item) for item in value)
    if isinstance(value, dict):
        return json.dumps(value, ensure_ascii=False)
    return value


def run(workspace: Workspace, action: str, module: str = "") -> dict:
    if action == "validate":
        modules = [module] if module else [name for name in MODULES if (workspace.findings / f"{name}.json").exists()]
        results = [validate(workspace, name) for name in modules]
        return {"ok": all(result["ok"] for result in results), "modules": results}
    if action == "reconcile":
        return reconcile(workspace)
    return export(workspace)
