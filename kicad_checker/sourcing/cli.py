"""CLI wiring only. Offline by default; --online is an explicit read-only fetch."""

import hashlib
import json
from pathlib import Path

from .. import digikey
from ..core import read_json, write_json
from ..paths import CACHE_ROOT
from .providers import CategoryIndex, DigiKeyCatalog, configured_jlc, identity, load_offers, safe_product
from .lcsc import configured_lcsc
from .requirements import (api_category_name, keywords, parametric_request, search_filters, search_sort, stock_policy,
                           validate_request)
from .service import markdown, search_report, stock_report


def add_commands(sub):
    search = sub.add_parser("parts-search", help="Search any DigiKey category and screen stocked catalog candidates; offline by default")
    search.add_argument("request", help="Structured requirements JSON; examples/sourcing/resistor.json")
    search.add_argument("--replace", help="Exclude this exact existing MPN from the alternative count")
    search.add_argument("--max-pages", type=int, default=3, help="DigiKey request budget, 1–20 pages of 50")
    stock = sub.add_parser("parts-stock", help="Stock/lifecycle report for an exact manufacturer and MPN")
    stock.add_argument("mpn")
    stock.add_argument("--manufacturer", required=True, help="Exact DigiKey manufacturer spelling")
    stock.add_argument("--quantity", type=int, default=100)
    stock.add_argument("--min-stock", type=int, default=1000)
    stock.add_argument("--max-age-hours", type=float, default=24)
    stock.add_argument("--jlc-code", help="Explicit JLCPCB C-number for this manufacturer/MPN")
    for command in (search, stock):
        command.add_argument("--output", help="New report directory (required except with --dry-run)")
        command.add_argument("--cache", default=str(CACHE_ROOT / "sourcing"))
        command.add_argument("--online", action="store_true", help="Fetch fresh supplier data using configured credentials")
        command.add_argument("--dry-run", action="store_true", help="Print plan without network, credentials or writes")
        command.add_argument("--stock-import", action="append", default=[], help="Dated normalized stock observations, repeatable")
        command.add_argument("--digikey-snapshot", help="Recorded search results JSON (schema in SOURCING.md)")
        command.add_argument("--max-stock-lookups", type=int, default=10, help="Per-supplier lookup budget, 1–50")
        command.add_argument("--full-report", action="store_true", help="Also write sourcing-full.md with every row in detail")


def snapshot_records(path):
    data = read_json(path)
    if data.get("schema_version") != 1 or not isinstance(data.get("records"), list):
        raise ValueError("DigiKey snapshot needs schema_version=1 and records")
    records = []
    for record in data["records"]:
        if not isinstance(record, dict) or not isinstance(record.get("product"), dict) or not isinstance(record.get("observed_at"), str):
            raise ValueError("Each snapshot record needs product and observed_at")
        records.append({"product": safe_product(record["product"]), "observed_at": record["observed_at"]})
    return records


def jlc_observations(mappings, cache, online, warnings):
    observations = []
    client = configured_jlc() if online and mappings else None
    for mapping in mappings:
        key = hashlib.sha256(json.dumps(mapping, sort_keys=True).encode()).hexdigest()
        path = Path(cache) / "jlcpcb" / f"{key}.json"
        if online and client is not None:
            try:
                offer = client.lookup(mapping)
                path.parent.mkdir(parents=True, exist_ok=True)
                write_json(path, {"schema_version": 1, "offers": [offer]})
                observations.append(offer)
                continue
            except (OSError, ValueError, KeyError, TypeError):
                warnings.append(f"JLCPCB lookup failed for {mapping['jlcpcb']}; no fresh stock confirmed")
        elif online:
            warnings.append("JLCPCB credentials missing; stock stays unknown unless a dated observation exists")
        if path.is_file():
            observations.extend(load_offers(path))
    return observations


def lcsc_observations(parts, cache, online, warnings):
    observations = []
    client = configured_lcsc() if online and parts else None
    if online and parts and client is None:
        warnings.append("LCSC credentials missing; only previous dated stock observations are available")
    for manufacturer, mpn in parts:
        key = hashlib.sha256(json.dumps(identity(manufacturer, mpn)).encode()).hexdigest()
        path = Path(cache) / "lcsc" / f"{key}.json"
        if client is not None:
            try:
                offers, incomplete = client.lookup(manufacturer, mpn)
                path.parent.mkdir(parents=True, exist_ok=True)
                write_json(path, {"schema_version": 1, "offers": offers})
                observations.extend(offers)
                if incomplete:
                    warnings.append(f"LCSC results for {mpn} exceeded one page; supplier coverage incomplete")
                continue
            except (OSError, ValueError, KeyError, TypeError):
                warnings.append(f"LCSC lookup failed for {mpn}; no fresh stock confirmed")
        if path.is_file():
            observations.extend(load_offers(path))
    return observations


def parametric_pass(req, catalog, query, records, plan, args, warnings):
    """Phase B: when keyword search falls short for an R/C/L value, repeat it with DigiKey's
    parametric filter for the exact value (IDs taken from the first response). Results are added
    to the keyword results; screening stays local."""
    if req["kind"] not in ("resistor", "capacitor", "inductor") or plan["category_id"] is None:
        return records, None
    if search_report(req, records)["status"] == "target_met":
        return records, {"used": False, "reason": "keyword search already met the target"}
    try:
        options = catalog.page(query, 0, refresh=False, filters=plan["filters"], sort=plan["sort"]).get("parametric_filters") or []
    except (LookupError, OSError, ValueError):
        options = []
    parameter_request, reason = parametric_request(req, plan["category_id"], options)
    if parameter_request is None:
        return records, {"used": False, "reason": reason}
    filters = {**(plan["filters"] or {}), "ParameterFilterRequest": parameter_request}
    narrow = " ".join(x for x in (req["kind"], req.get("package", "")) if x)
    more, more_warnings = catalog.search(narrow, max_pages=args.max_pages, refresh=args.online, filters=filters, sort=plan["sort"])
    if not more and more_warnings:
        warnings.append("DigiKey parametric value filter unavailable or rejected; keyword results only")
    else:
        warnings.extend(w for w in more_warnings if "budget" in w)
    return records + more, {"used": True, "query": narrow, "parameter_filter": parameter_request, "added_records": len(more)}


def run(args):
    if not 1 <= args.max_stock_lookups <= 50:
        raise ValueError("max-stock-lookups must be 1–50")
    if args.command == "parts-search":
        req = validate_request(read_json(args.request))
        if args.replace:
            req["exclude_mpns"].append(args.replace)
        if not 1 <= args.max_pages <= 20:
            raise ValueError("max-pages must be 1–20")
        query = keywords(req)
        mappings = [dict(m) for m in req["supplier_codes"]]
    else:
        if not args.manufacturer.strip() or not args.mpn.strip():
            raise ValueError("Exact manufacturer and MPN are required")
        req = stock_policy({"quantity": args.quantity, "min_stock": args.min_stock, "max_age_hours": args.max_age_hours})
        query = args.mpn
        mappings = [{"manufacturer": args.manufacturer, "mpn": args.mpn, "jlcpcb": args.jlc_code}] if args.jlc_code else []
        if mappings:
            import re
            if not re.fullmatch(r"C\d+", args.jlc_code):
                raise ValueError("jlc-code must be C followed by digits")
    if args.online and args.digikey_snapshot:
        raise ValueError("Use either --online or --digikey-snapshot")
    if args.dry_run:
        print(json.dumps({"mode": args.command, "query": query, "policy": req,
                          "digikey_filters": search_filters(req) if args.command == "parts-search" else None,
                          "digikey_sort": search_sort(req) if args.command == "parts-search" else None,
                          "category_name_to_resolve": (api_category_name(req) if args.command == "parts-search"
                                                       and type(req.get("category")) is not int else None),
                          "online_requested": args.online, "network_requests_sent": 0,
                          "jlcpcb_mappings": mappings,
                          "lcsc": "official API exact manufacturer/MPN lookups",
                          "max_stock_lookups_per_supplier": args.max_stock_lookups}, indent=2))
        return 0
    if not args.output:
        raise ValueError("--output is required except with --dry-run")
    out = Path(args.output).resolve()
    if out.exists():
        raise ValueError("Choose a new output directory to preserve previous reports")
    warnings, search_plan = [], None
    observations = [o for p in args.stock_import for o in load_offers(p)]
    client = None
    if args.online:
        try:
            client = digikey.DigiKeyClient(*digikey.load_credentials())
        except ValueError:
            warnings.append("DigiKey credentials missing; only cached/supplied observations can be examined")
    catalog = DigiKeyCatalog(Path(args.cache) / "digikey", client)
    if args.digikey_snapshot:
        records = snapshot_records(args.digikey_snapshot)
        warnings.append("DigiKey results supplied from a snapshot; no live stock check was performed.")
    elif args.command == "parts-search":
        category_id, name = None, api_category_name(req)
        if name is not None:
            category_id, warning = CategoryIndex(Path(args.cache) / "digikey", client).resolve(name, refresh=args.online)
            if warning:
                warnings.append(warning)
        search_plan = {"category_name": name, "category_id": category_id if category_id is not None else
                       (req["category"] if type(req.get("category")) is int else None),
                       "filters": search_filters(req, category_id), "sort": search_sort(req)}
        records, search_warnings = catalog.search(query, max_pages=args.max_pages, refresh=args.online,
                                                  filters=search_plan["filters"], sort=search_plan["sort"])
        warnings.extend(search_warnings)
        records, search_plan["parametric"] = parametric_pass(req, catalog, query, records, search_plan, args, warnings)
    else:
        try:
            entry = catalog.page(query, 0, limit=10, refresh=args.online, exact=True)
            records = [{"product": p, "observed_at": entry["observed_at"]} for p in entry["products"]]
        except (LookupError, OSError, ValueError):
            warnings.append("DigiKey exact lookup unavailable; examining previous checker cache if present")
            entry = digikey.load(digikey.DEFAULT_CACHE, query)
            records = [{"product": p, "observed_at": entry.get("fetched_at")} for p in (entry or {}).get("exact_matches", [])]
    if args.command == "parts-search":
        preliminary = search_report(req, records)
        parts = [(r["manufacturer"], r["mpn"]) for r in preliminary["results"]
                 if r["catalog_status"] in ("catalog_match", "needs_review")]
    else:
        parts = [(args.manufacturer, args.mpn)]
    if len(parts) > args.max_stock_lookups:
        warnings.append("LCSC lookup budget reached; remaining candidates have incomplete supplier coverage")
    observations.extend(lcsc_observations(parts[:args.max_stock_lookups], args.cache, args.online, warnings))
    # A shared C-code is only a lookup lead. JLCPCB must return the same code AND exact MPN.
    mapped = {identity(m["manufacturer"], m["mpn"]) for m in mappings}
    requested_identities = {identity(manufacturer, mpn) for manufacturer, mpn in parts}
    import re
    for o in observations:
        key = identity(o["manufacturer"], o["mpn"])
        if o["supplier"] == "lcsc" and key in requested_identities and key not in mapped and re.fullmatch(r"C\d+", str(o.get("code", ""))):
            mappings.append({"manufacturer": o["manufacturer"], "mpn": o["mpn"], "jlcpcb": o["code"]})
            mapped.add(key)
    if not mappings:
        warnings.append("JLCPCB: no exact C-number mappings found; catalog availability is unknown.")
    if len(mappings) > args.max_stock_lookups:
        warnings.append("JLCPCB lookup budget reached; remaining mappings were not checked")
    observations.extend(jlc_observations(mappings[:args.max_stock_lookups], args.cache, args.online, warnings))
    if args.command == "parts-search":
        report = search_report(req, records, imported_offers=observations, warnings=warnings)
        if search_plan:
            report["search"] = search_plan  # Exactly what was asked of DigiKey (screening is still local).
    else:
        report = stock_report(args.manufacturer, args.mpn, records, req, imported_offers=observations, warnings=warnings)
    out.mkdir(parents=True, exist_ok=False)
    write_json(out / "sourcing.json", report)
    (out / "sourcing.md").write_text(markdown(report), encoding="utf-8")
    if getattr(args, "full_report", False):
        (out / "sourcing-full.md").write_text(markdown(report, full=True), encoding="utf-8")
    print(f"Sourcing: {report['status']}; report {out / 'sourcing.md'}")
    return 0 if report["status"] in ("target_met", "available") else 2
