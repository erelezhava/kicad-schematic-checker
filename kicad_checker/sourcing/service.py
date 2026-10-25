"""Pure report generation: catalog constraints first, procurement separately.

Schema v1 counts fresh DigiKey offers meeting explicit catalog criteria in any category.
LCSC/JLCPCB quantities are separate observations, never added to DigiKey stock.
"""

import json
from datetime import datetime, timezone

from .. import digikey
from .providers import SUPPLIERS, digikey_offers, identity, integer
from .requirements import CATEGORIES, keywords, screen, stock_policy, validate_request


def fresh(observed_at, max_age_hours, now):
    try:
        observed = datetime.fromisoformat(str(observed_at).replace("Z", "+00:00"))
        if observed.tzinfo is None:
            return False
        age = (now - observed).total_seconds()
        return 0 <= age <= float(max_age_hours) * 3600
    except (ValueError, TypeError, OverflowError):
        return False


def assess_offer(offer, req, now):
    reasons = []
    if offer.get("observation_conflict"):
        reasons.append("conflicting stock observations at the same timestamp")
    if not fresh(offer.get("observed_at"), req["max_age_hours"], now):
        reasons.append("stock timestamp missing, invalid, future or stale")
    stock = integer(offer.get("stock"))
    if stock is None:
        reasons.append("stock unknown")
    elif stock < max(req["quantity"], req["min_stock"]):
        reasons.append("insufficient stock for quantity/min_stock")
    moq = integer(offer.get("moq"))
    if moq is None or moq < 1:
        reasons.append("minimum order quantity unknown")
    elif moq > req["quantity"]:
        reasons.append("minimum order quantity exceeds requested quantity")
    maximum = integer(offer.get("max_order"))
    if maximum is not None and maximum < req["quantity"]:
        reasons.append("distribution limit below requested quantity")
    multiple = integer(offer.get("order_multiple"))
    if multiple and req["quantity"] % multiple:
        reasons.append("requested quantity does not meet order multiple")
    if offer.get("identity_review"):
        reasons.append("supplier manufacturer identity needs review")
    if offer["supplier"] == "digikey":
        if offer.get("marketplace") is not False:
            reasons.append("marketplace seller or seller type unknown")
        if str(offer.get("lifecycle", "")).casefold() != "active" or offer.get("discontinued") or offer.get("end_of_life"):
            reasons.append("lifecycle not confirmed active")
        if req["normally_stocking"] and offer.get("normally_stocking") is not True:
            reasons.append("normally stocked not confirmed")
    return {**offer, "eligible": not reasons, "issues": reasons}


def supplier_summary(offers, req, now):
    # An older positive snapshot must not override a newer zero-stock observation.
    latest = {}
    for offer in offers:
        key = (offer["supplier"], offer.get("code"))
        try:
            observed = datetime.fromisoformat(str(offer.get("observed_at")).replace("Z", "+00:00"))
            timestamp = observed.timestamp() if observed.tzinfo else float("-inf")
        except (ValueError, TypeError, OverflowError):
            timestamp = float("-inf")
        previous = latest.get(key)
        if previous is None or timestamp > previous[0]:
            latest[key] = (timestamp, offer)
        elif timestamp == previous[0]:
            relevant = ("stock", "moq", "normally_stocking", "lifecycle", "discontinued", "end_of_life", "marketplace", "max_order", "order_multiple")
            if any(previous[1].get(k) != offer.get(k) for k in relevant):
                latest[key] = (timestamp, {**previous[1], "observation_conflict": True})
    offers = [o for _, o in latest.values()]
    result = {}
    for supplier in SUPPLIERS:
        observations = [assess_offer(o, req, now) for o in offers if o["supplier"] == supplier]
        quantities = [o["stock"] for o in observations if integer(o.get("stock")) is not None]
        result[supplier] = {"status": "available" if any(o["eligible"] for o in observations) else "needs_review" if observations else "unknown",
                            "max_reported_stock": max(quantities) if quantities else None, "offers": observations}
    return result


def spec_signature(product):
    return json.dumps({"parameters": sorted((str(p.get("ParameterText")), str(p.get("ValueText")))
                                             for p in product.get("Parameters", [])),
                       "category": digikey.category_path(product)}, sort_keys=True)


def search_report(request, records, *, imported_offers=(), warnings=(), now=None):
    req = validate_request(request)
    now = now or datetime.now(timezone.utc)
    groups = {}
    for record in records:
        p = record["product"]
        key = identity((p.get("Manufacturer") or {}).get("Name"), p.get("ManufacturerProductNumber"))
        if not all(key):
            continue  # Cannot count or cross-reference incomplete identities.
        groups.setdefault(key, []).append(record)
    rows, signatures = [], {}
    excluded = {digikey.mpn_key(mpn) for mpn in req["exclude_mpns"]}
    for key, entries in groups.items():
        p = entries[0]["product"]
        status, checks = screen(p, req)
        issues = []
        if len({spec_signature(e["product"]) for e in entries}) > 1:
            status = "needs_review"
            issues.append("Conflicting DigiKey specifications for this exact manufacturer/MPN")
        if key[1] in excluded:
            status = "excluded"
        observations = [o for e in entries for o in digikey_offers(e["product"], e["observed_at"])]
        observations += [o for o in imported_offers if identity(o.get("manufacturer"), o.get("mpn")) == key]
        # Identical listings are not independent alternatives or inventory pools.
        observations = list({json.dumps(o, sort_keys=True): o for o in observations}.values())
        suppliers = supplier_summary(observations, req, now)
        counted = status == "catalog_match" and suppliers["digikey"]["status"] == "available"
        signatures[key] = (key[0], spec_signature(p))
        rows.append({"_identity": key, "manufacturer": p["Manufacturer"]["Name"], "mpn": p["ManufacturerProductNumber"],
                     "description": (p.get("Description") or {}).get("DetailedDescription") or
                                    (p.get("Description") or {}).get("ProductDescription"),
                     "category": p.get("Category"),
                     "parameters": [{"name": param.get("ParameterText"), "value": param.get("ValueText")}
                                    for param in p.get("Parameters", [])],
                     "catalog_status": status, "counts_toward_target": counted, "checks": checks, "issues": issues,
                     "spec_source": {"publisher": "DigiKey catalog", "url": p.get("ProductUrl"),
                                     "observed_at": entries[0]["observed_at"], "datasheet_url": p.get("DatasheetUrl")},
                     "suppliers": suppliers, "engineering_status": "requires_review"})
    def order(r):
        return (not r["counts_toward_target"], r["catalog_status"] != "catalog_match",
                -(r["suppliers"]["digikey"]["max_reported_stock"] or 0), r["manufacturer"].casefold(), r["mpn"].casefold())

    rows.sort(key=order)
    # Same manufacturer + identical catalog specification = ordering/packaging variant
    # (reel size, tape suffix), not an independent alternative or second source.
    excluded_specs = {signatures[r["_identity"]]: r["mpn"] for r in rows if r["catalog_status"] == "excluded"}
    representative = {}
    for r in rows:
        signature = signatures[r.pop("_identity")]
        if r["catalog_status"] != "excluded" and signature in excluded_specs:
            r["catalog_status"], r["counts_toward_target"] = "excluded_variant", False
            r["variant_of"] = excluded_specs[signature]
            r["issues"].append(f"Same manufacturer and catalog specification as excluded {excluded_specs[signature]}; "
                               "an ordering/packaging variant of the part being replaced")
        elif signature in representative:
            r["counts_toward_target"] = False
            r["variant_of"] = representative[signature]
            r["issues"].append(f"Same manufacturer and catalog specification as {representative[signature]}; "
                               "counted once as an ordering/packaging variant (verify against the manufacturer's ordering code)")
        else:
            representative[signature] = r["mpn"]
    rows.sort(key=order)
    count = sum(r["counts_toward_target"] for r in rows)
    manufacturers = len({" ".join(r["manufacturer"].split()).casefold() for r in rows if r["counts_toward_target"]})
    met = count >= req["min_alternatives"] and manufacturers >= req["min_manufacturers"]
    return {"schema_version": 1, "generated_at": now.isoformat(timespec="seconds"), "mode": "alternatives",
            "request": req, "query": keywords(req), "status": "target_met" if met else "shortfall",
            "qualified_candidates": count, "target": req["min_alternatives"],
            "distinct_manufacturers": manufacturers, "min_manufacturers": req["min_manufacturers"],
            "manufacturer_shortfall": max(0, req["min_manufacturers"] - manufacturers),
            "target_basis": "supplied_catalog_criteria_and_digikey_stock",
            "discovered_candidates": len(rows),
            "shortfall": max(0, req["min_alternatives"] - count), "warnings": list(warnings), "results": rows,
            "scope": "Catalog screening only, for the supplied criteria. All component types require engineering review against manufacturer documents before substitution. No automatic BOM changes or drop-in approval. Normally stocked is a supplier flag, not a future supply guarantee."}


def stock_report(manufacturer, mpn, records, request, *, imported_offers=(), warnings=(), now=None):
    """Exact-part procurement view, including obsolete parts that need alternatives."""
    req = stock_policy(request)
    now = now or datetime.now(timezone.utc)
    key = identity(manufacturer, mpn)
    matches = [r for r in records if identity((r["product"].get("Manufacturer") or {}).get("Name"),
                                            r["product"].get("ManufacturerProductNumber")) == key]
    offers = [o for r in matches for o in digikey_offers(r["product"], r["observed_at"])]
    offers += [o for o in imported_offers if identity(o.get("manufacturer"), o.get("mpn")) == key]
    suppliers = supplier_summary(offers, req, now)
    warnings = list(warnings)
    others = sorted({(r["product"].get("Manufacturer") or {}).get("Name") or "?" for r in records
                     if digikey.mpn_key(r["product"].get("ManufacturerProductNumber") or "") == key[1]
                     and r not in matches})
    if others and not matches:
        warnings.append(f"DigiKey lists {mpn} under {', '.join(others)}, not {manufacturer!r}; not counted. "
                        "Check the Manufacturer field (or add a reviewed alias)")
    return {"schema_version": 1, "generated_at": now.isoformat(timespec="seconds"), "mode": "stock",
            "manufacturer": manufacturer, "mpn": mpn, "request": req, "warnings": warnings,
            "status": suppliers["digikey"]["status"], "suppliers": suppliers,
            "scope": "Exact manufacturer/MPN only. Stock is a timestamped snapshot; supplier quantities are not combined."}


def full_markdown(report):
    """Every discovered row with all checks, parameters and offers (large)."""
    def cell(value):
        return str(value if value is not None else "unknown").replace("|", "\\|").replace("\n", " ")

    lines = ["# Component sourcing", "", report["scope"], "", f"Generated: {report['generated_at']}", ""]
    if report["mode"] == "stock":
        lines += [f"{report['manufacturer']} / {report['mpn']}: **{report['status']}**", ""]
        rows = [{"manufacturer": report["manufacturer"], "mpn": report["mpn"], "catalog_status": "exact stock lookup",
                 "counts_toward_target": False, "suppliers": report["suppliers"]}]
    else:
        lines += [f"Query: {report['query']}", "", f"**{report['qualified_candidates']} / {report['target']} candidates meet the catalog and DigiKey stock criteria.** Shortfall: {report['shortfall']}.",
                  f"Distinct manufacturers: {report['distinct_manufacturers']} / {report['min_manufacturers']} required.", ""]
        rows = report["results"]
        if report["request"]["kind"] not in (*CATEGORIES, "diode") and "category" not in report["request"]:
            lines += ["Discovery query: specify an exact category to count catalog matches. Returned descriptions and parameters can help define the next request.", ""]
    if report["warnings"]:
        lines += ["## Incomplete coverage", ""] + ["- " + cell(w) for w in report["warnings"]] + [""]
    lines += ["| Manufacturer | MPN | Catalog | Counts | DigiKey stock | LCSC stock | JLCPCB stock |",
              "| --- | --- | --- | --- | --- | --- | --- |"]
    for row in rows:
        values = [row["manufacturer"], row["mpn"], row["catalog_status"], "yes" if row["counts_toward_target"] else "no"]
        values += [f"{row['suppliers'][s]['max_reported_stock'] if row['suppliers'][s]['max_reported_stock'] is not None else 'unknown'} ({row['suppliers'][s]['status']})" for s in SUPPLIERS]
        lines.append("| " + " | ".join(cell(v) for v in values) + " |")
    for row in rows:
        lines += ["", "## " + cell(row["manufacturer"] + " / " + row["mpn"]), ""]
        if row.get("description"):
            lines += [cell(row["description"]), ""]
        if row.get("category"):
            lines += ["Category: " + cell(" / ".join(digikey.category_path({"Category": row["category"]}))), ""]
        for issue in row.get("issues", []):
            lines.append("- " + cell(issue))
        for c in row.get("checks", []):
            lines.append(f"- {cell(c['parameter'])}: {c['status']}; observed {cell(c['actual'])}; required {cell(c['expected'])}"
                         + (f"; {cell(c['note'])}" if c.get("note") else ""))
        source = row.get("spec_source", {})
        if source:
            lines += ["", f"Catalog source: {cell(source['url'])} ({cell(source['observed_at'])})"]
            if source.get("datasheet_url"):
                lines.append("Datasheet: " + cell(source["datasheet_url"]))
        if row.get("parameters"):
            lines += ["", "### Catalog parameters", "", "| Parameter | Observed value |", "| --- | --- |"]
            lines += [f"| {cell(p['name'])} | {cell(p['value'])} |" for p in row["parameters"]]
        for supplier, summary in row["suppliers"].items():
            lines += ["", f"### {supplier}: {summary['status']}", ""]
            if not summary["offers"]:
                lines.append("No stock observation; access, mapping or snapshot is missing.")
            for o in summary["offers"]:
                lines.append(f"- {cell(o.get('code'))}: stock {cell(o.get('stock'))}; MOQ {cell(o.get('moq'))}; normally stocked {cell(o.get('normally_stocking'))}; lifecycle {cell(o.get('lifecycle'))}; observed {cell(o.get('observed_at'))}")
                lines.append(f"  Source: {cell(o.get('url'))}; {cell(o.get('origin'))}")
                for issue in o["issues"]:
                    lines.append("  - " + cell(issue))
    return "\n".join(lines) + "\n"


def _cell(value):
    return str(value if value is not None else "unknown").replace("|", "\\|").replace("\n", " ")


def _names(rows, limit=15):
    names = [r["mpn"] for r in rows[:limit]]
    return ", ".join(names) + (f", ... and {len(rows) - limit} more" if len(rows) > limit else "")


def _stock(row, supplier):
    summary = row["suppliers"][supplier]
    stock = summary["max_reported_stock"]
    return f"{stock if stock is not None else '?'} ({summary['status']})"


def markdown(report, *, full=False, details=10):
    """Compact by default: everything is in sourcing.json; the Markdown is for reading.

    Counted alternatives get a table and short details; other rows get one line or a
    grouped count with the reason. full=True (or a stock report) renders everything.
    """
    if full or report["mode"] != "alternatives":
        return full_markdown(report)
    rows = report["results"]
    counted = [r for r in rows if r["counts_toward_target"]]
    variants = [r for r in rows if r.get("variant_of") and r["catalog_status"] != "rejected"]
    excluded = [r for r in rows if r["catalog_status"] == "excluded"]
    rejected = [r for r in rows if r["catalog_status"] == "rejected"]
    other = [r for r in rows if not r["counts_toward_target"] and not r.get("variant_of")
             and r["catalog_status"] in ("catalog_match", "needs_review")]
    lines = ["# Component sourcing", "", report["scope"], "",
             f"Generated: {report['generated_at']} | Query: {report['query']}", "",
             f"**{report['qualified_candidates']} / {report['target']} alternatives, "
             f"{report['distinct_manufacturers']} / {report['min_manufacturers']} manufacturers: {report['status']}.** "
             f"Discovered {report['discovered_candidates']}: {len(counted)} counted, {len(variants)} variants, "
             f"{len(other)} not counted, {len(rejected)} rejected, {len(excluded)} excluded.", ""]
    if report["request"]["kind"] not in (*CATEGORIES, "diode") and "category" not in report["request"]:
        lines += ["Discovery query: specify an exact category to count catalog matches. Returned descriptions and parameters can help define the next request.", ""]
    plan = report.get("search")
    if plan:
        f = plan.get("filters") or {}
        name = f"{plan['category_name']} " if plan.get("category_name") else ""
        parts = [f"category {name}(id {plan['category_id']})" if plan.get("category_id") is not None else "no category filter"]
        if "MinimumQuantityAvailable" in f:
            parts.append(f"stock >= {f['MinimumQuantityAvailable']}")
        parts += [str(x) for x in f.get("SearchOptions", [])] + ([f["MarketPlaceFilter"]] if "MarketPlaceFilter" in f else [])
        if plan.get("sort"):
            parts.append(f"sorted by {plan['sort']['Field']} {plan['sort']['SortOrder'].lower()}")
        lines += ["DigiKey filters requested: " + _cell(", ".join(parts)), ""]
        parametric = plan.get("parametric")
        if parametric:
            lines += [("Parametric value filter: used (" + _cell(parametric["query"]) + f", +{parametric['added_records']} records)"
                       if parametric["used"] else "Parametric value filter: not used — " + _cell(parametric["reason"])), ""]
    if report["warnings"]:
        lines += ["## Incomplete coverage", ""] + ["- " + _cell(w) for w in report["warnings"]] + [""]
    if counted:
        lines += [f"## Counted alternatives ({len(counted)})", "",
                  "| # | Manufacturer | MPN | DigiKey | LCSC | JLCPCB | Description |", "| --- | --- | --- | --- | --- | --- | --- |"]
        for i, r in enumerate(counted, 1):
            lines.append("| " + " | ".join(_cell(v) for v in (i, r["manufacturer"], r["mpn"], _stock(r, "digikey"),
                                                              _stock(r, "lcsc"), _stock(r, "jlcpcb"), r.get("description"))) + " |")
        shown = counted[:details]
        lines += ["", f"## Details: top {len(shown)} of {len(counted)} counted", ""]
        for r in shown:
            source = r.get("spec_source", {})
            lines += [f"### {_cell(r['manufacturer'])} / {_cell(r['mpn'])}", ""]
            lines.append("Checks: " + "; ".join(f"{_cell(c['parameter'])} {c['status']} ({_cell(c['actual'])})" for c in r["checks"]))
            if r.get("category"):
                lines.append("Category: " + _cell(" / ".join(digikey.category_path({"Category": r["category"]}))))
            lines.append(f"Catalog source: {_cell(source.get('url'))} ({_cell(source.get('observed_at'))})")
            if source.get("datasheet_url"):
                lines.append("Datasheet: " + _cell(source["datasheet_url"]))
            if r.get("parameters"):
                lines.append("Catalog parameters: " + "; ".join(f"{_cell(p['name'])}={_cell(p['value'])}" for p in r["parameters"]))
            for o in r["suppliers"]["digikey"]["offers"]:
                if o["eligible"]:
                    lines.append(f"- DigiKey {_cell(o.get('code'))}: stock {_cell(o.get('stock'))}, MOQ {_cell(o.get('moq'))}, {_cell(o.get('packaging'))}")
            lines.append("")
    if other:
        lines += [f"## Not counted ({len(other)})", ""]
        for r in other:
            reasons = [f"{c['parameter']} unknown ({_cell(c['actual'])}" + (f"; {c['note']})" if c.get("note") else ")")
                       for c in r["checks"] if c["status"] == "unknown"]
            reasons += r["issues"]
            if r["catalog_status"] == "catalog_match":
                offers = r["suppliers"]["digikey"]["offers"]
                best = min(offers, key=lambda o: len(o["issues"])) if offers else None
                reasons += best["issues"] if best else ["no DigiKey offer"]
            lines.append(f"- {_cell(r['manufacturer'])} / {_cell(r['mpn'])} [{r['catalog_status']}, DigiKey {_stock(r, 'digikey')}]: "
                         + "; ".join(_cell(x) for x in dict.fromkeys(reasons)))
        lines.append("")
    if variants:
        lines += [f"## Ordering/packaging variants ({len(variants)}), not counted separately", ""]
        lines += [f"- {_cell(r['mpn'])} → {_cell(r['variant_of'])}" + (" (variant of the excluded original)" if r["catalog_status"] == "excluded_variant" else "")
                  for r in variants] + [""]
    if excluded:
        lines += ["## Excluded originals", "", _names(excluded), ""]
    if rejected:
        groups = {}
        for r in rejected:
            failed = tuple(sorted({c["parameter"] for c in r["checks"] if c["status"] == "mismatch"}))
            groups.setdefault(failed, []).append(r)
        lines += [f"## Rejected ({len(rejected)})", ""]
        for failed, members in sorted(groups.items(), key=lambda g: -len(g[1])):
            lines.append(f"- {', '.join(failed) or 'mismatch'} ({len(members)}): {_cell(_names(members))}")
        lines.append("")
    lines += ["Full data for every row: sourcing.json. Use --full-report for a complete Markdown listing."]
    return "\n".join(lines) + "\n"
