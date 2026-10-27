"""`inventory-check`: compare a KiCad design's BOM with stock files, then optionally check
market stock and look for R/C/L alternatives. Prompts in a terminal; the same answers are
flags (--boards, --check-market, --find-alternatives) so an AI agent or CI can drive it.

Read-only: no design edits, no orders. Reports go into a new directory.
"""

import argparse
import contextlib
import csv
import io
import shutil
import sys
import tempfile
from pathlib import Path

from ..core import digest, parse_netlist, read_json, write_json
from ..paths import CACHE_ROOT
from ..schematic import design_files, root_schematic
from .inventory import (NEEDS_PARTS, bom_lines, catalog_values, draft_request, match_line, required_quantity, select_lines,
                        schematic_spec, stock_alternatives, verification_request, verify_candidate,
                        baseline_from_digikey, requirement_summary)
from .providers import SUPPLIERS, manufacturer_key, utc_now
from .stockfile import ROLES, load_stock

STATUS_TEXT = {
    "in_stock": "in stock", "split_across_sources": "split across sources", "insufficient": "insufficient",
    "not_in_stock": "not in stock", "quantity_unknown": "quantity unknown", "unidentified": "no MPN/C-code",
}


def add_command(sub):
    c = sub.add_parser("inventory-check", help="Compare a design BOM with stock CSV/XLSX files; optional market check and R/C/L alternatives")
    c.add_argument("design", help="KiCad project (.kicad_pro, root .kicad_sch or its folder), exported XML netlist, or checker circuit.json")
    c.add_argument("--stock", action="append", required=True, metavar="[LABEL=]FILE",
                   help="Stock file (.csv or .xlsx), repeatable, e.g. --stock JLC=jlc.csv --stock Office=office.xlsx")
    c.add_argument("--column", action="append", default=[], metavar="ROLE=HEADER",
                   help=f"Override a detected column; roles: {', '.join(ROLES)}")
    c.add_argument("--sheet", help="XLSX sheet name (default: the sheet with the best stock header)")
    c.add_argument("--boards", type=int, help="Number of boards to build (asked when omitted in a terminal)")
    c.add_argument("--spare-percent", type=float, default=5.0, help="Attrition spare per line, percent (default 5)")
    c.add_argument("--spare-min", type=int, default=2, help="Minimum spare parts per line (default 2)")
    c.add_argument("--combine-sources", action="store_true", help="Treat all stock files as one pool (default: each source separately)")
    c.add_argument("--check-market", action=argparse.BooleanOptionalAction, default=None,
                   help="Check DigiKey/LCSC/JLCPCB stock for missing or short parts (asked when omitted in a terminal)")
    c.add_argument("--find-alternatives", action=argparse.BooleanOptionalAction, default=None,
                   help="Search DigiKey for R/C/L alternatives when a part is not available (asked when omitted in a terminal)")
    c.add_argument("--alternatives-even-if-available", action="store_true",
                   help="Second sources: search alternatives for every R/C/L line, also when it is in stock or "
                        "available on the market (implies --find-alternatives; uses the search budget)")
    c.add_argument("--alternatives-for", action="append", default=[], metavar="REF_OR_MPN[,...]",
                   help="Search alternatives only for these lines, e.g. R27,C2 or an MPN (repeatable); they are searched "
                        "even when in stock or available. Implies --find-alternatives")
    c.add_argument("--verify-stock-alternatives", action=argparse.BooleanOptionalAction, default=None,
                   help="Check in-stock R/C/L candidates against their exact DigiKey record (qualification, ratings, "
                        "lifecycle). Default: online when --check-market/--find-alternatives is used, else cache only")
    c.add_argument("--max-verify-lookups", type=int, default=40, help="DigiKey exact lookups for original-part baselines and in-stock candidates (default 40)")
    c.add_argument("--offline", action="store_true", help="Market checks and searches use caches only; no network requests")
    c.add_argument("--no-input", action="store_true", help="Never prompt (automatic when stdin is not a terminal)")
    c.add_argument("--max-market-parts", type=int, default=20, help="Supplier request budget for market checks (default 20 parts)")
    c.add_argument("--max-alternative-searches", type=int, default=10, help="DigiKey search budget for alternatives (default 10 lines)")
    c.add_argument("--max-pages", type=int, default=2, help="DigiKey pages (50 parts) per alternative search, 1–20")
    c.add_argument("--max-age-hours", type=float, default=24, help="Market stock freshness limit")
    c.add_argument("--cache", default=None, help="Sourcing cache folder (default cache/sourcing)")
    c.add_argument("--output", required=True, help="New report directory")


# --- input ---------------------------------------------------------------------------------

def load_design(path):
    """circuit.json, XML netlist, or a KiCad project (netlist exported with kicad-cli)."""
    path = Path(path).expanduser().resolve()
    if path.suffix == ".json":
        circuit = read_json(path)
        if not isinstance(circuit.get("components"), dict):
            raise ValueError("Expected a checker circuit.json with a components object")
        return circuit, {"type": "circuit.json", "path": str(path), "sha256": digest(path)}
    if path.suffix in (".xml", ".net"):
        return parse_netlist(path), {"type": "netlist", "path": str(path), "sha256": digest(path)}
    root = root_schematic(path)
    executable = shutil.which("kicad-cli")
    if not executable:
        raise ValueError("kicad-cli not found: install KiCad, or pass an exported XML netlist / circuit.json")
    from ..cli import run_kicad  # deferred: cli imports this module
    inputs = design_files(root)
    hashes = {str(p): digest(p) for p in inputs}
    with tempfile.TemporaryDirectory(prefix="kicad-inventory-") as tmp:
        netlist = Path(tmp) / "netlist.xml"
        run_kicad(executable, ["sch", "export", "netlist", "--format", "kicadxml", "--output", str(netlist), str(root)])
        if any(digest(p) != sha for p, sha in hashes.items()):
            raise ValueError("Project changed during export; save it and retry")
        circuit = parse_netlist(netlist)
    return circuit, {"type": "kicad_project", "path": str(root), "inputs": hashes}


def parse_stock_args(values):
    result = []
    for value in values:
        label, _, rest = value.partition("=")
        if rest and not Path(value).exists() and "/" not in label and "\\" not in label:
            result.append((label.strip(), Path(rest).expanduser()))
        else:
            result.append((None, Path(value).expanduser()))
    labels = [label or path.stem for label, path in result]
    if len(set(labels)) != len(labels):
        raise ValueError("Stock sources need distinct names; use LABEL=FILE")
    return result


def parse_columns(values):
    overrides = {}
    for value in values:
        role, _, header = value.partition("=")
        role = role.strip().casefold()
        if role not in ROLES or not header.strip():
            raise ValueError(f"--column needs ROLE=HEADER with ROLE one of {', '.join(ROLES)}")
        overrides[role] = header.strip()
    return overrides


# --- prompts -------------------------------------------------------------------------------

class Asker:
    def __init__(self, enabled):
        self.enabled = enabled

    def integer(self, question):
        while True:
            answer = input(question).strip()
            if answer.isdigit() and int(answer) > 0:
                return int(answer)
            print("  Enter a whole number greater than 0.")

    def text(self, question):
        return input(question).strip()

    def yes(self, question):
        answer = input(question + " [y/N] ").strip().casefold()
        return answer in ("y", "yes")


# --- market and alternatives -----------------------------------------------------------------

def pick_lines(ask, lines, excluded, default):
    """Terminal pick list. Enter = none (no API calls), a = the listed lines, e = every R/C/L line,
    or numbers / references / MPNs (any BOM line, also lines already in stock)."""
    if default:
        print(f"Lines that may need alternatives ({len(default)}):")
        for number, line in enumerate(default, 1):
            state = STATUS_TEXT.get(line["stock"]["status"], line["stock"]["status"])
            if line.get("market", {}).get("status") == "available":
                state += ", available on the market"
            print(f"  {number}. {', '.join(line['references'][:6])} — {line['mpn'] or line['value']} ({state})")
        question = ("Search DigiKey for alternatives? Enter = no · a = all listed · numbers or references/MPNs "
                    "(e.g. 1,3 or R27,C2) · e = every R/C/L line: ")
    else:
        question = ("No line needs alternatives. Search second sources anyway? Enter = no · references/MPNs "
                    "(e.g. R27,C2) · e = every R/C/L line: ")
    while True:
        answer = ask.text(question)
        word = answer.casefold()
        if word in ("", "n", "no"):
            return []
        if word in ("a", "all", "y", "yes") and default:
            return list(default)
        if word in ("e", "every"):
            return [l for l in lines if l["class"]]
        numbers, names = [], []
        for part in (p.strip() for p in answer.split(",") if p.strip()):
            (numbers if part.isdigit() else names).append(part)
        bad = [n for n in numbers if not 1 <= int(n) <= len(default)]
        chosen, problems = select_lines(lines, excluded, names) if names else ([], [])
        if bad or problems:
            print("  " + "; ".join(([f"no listed line {', '.join(bad)}"] if bad else []) + problems))
            continue
        ids = {id(default[int(n) - 1]) for n in numbers} | {id(l) for l in chosen}
        return [l for l in lines if id(l) in ids]


def _quiet(function, args):
    with contextlib.redirect_stdout(io.StringIO()):
        return function(args)


def _jlc_code(line):
    """Schematic C-code, else the single C-code of the matching stock row (JLCPCB lookup lead)."""
    codes = line["lcsc"] or line["stock"].get("stock_lcsc", [])
    return codes[0] if len(codes) == 1 else None


class DigiKeyRecords:
    """Exact-MPN DigiKey records for baselines and in-stock candidates. One budget, one cache
    (shared with the market check); a record fetched within 24 h is reused instead of refetched."""

    def __init__(self, args, online, warnings):
        from .. import digikey
        from .providers import DigiKeyCatalog
        self.client = None
        if online:
            try:
                self.client = digikey.DigiKeyClient(*digikey.load_credentials())
            except ValueError:
                warnings.append("DigiKey credentials missing; baselines and in-stock candidates use cached DigiKey data only")
        self.catalog = DigiKeyCatalog(Path(args.cache) / "digikey", self.client)
        self.budget, self.lookups, self.memo = args.max_verify_lookups, 0, {}

    def get(self, mpn):
        """{"products", "observed_at"} or {"status": "not_checked", "note": ...}."""
        from .. import digikey
        from .service import fresh
        from datetime import datetime, timezone
        key = digikey.mpn_key(mpn)
        if key in self.memo:
            return self.memo[key]
        if self.lookups >= self.budget:
            return {"status": "not_checked", "note": "lookup budget reached (--max-verify-lookups)"}
        self.lookups += 1
        cached = None
        try:
            cached = self.catalog.page(mpn, 0, limit=10, refresh=False, exact=True)
        except LookupError:
            pass
        except (OSError, ValueError) as error:
            self.memo[key] = {"status": "not_checked", "note": f"DigiKey lookup failed ({type(error).__name__})"}
            return self.memo[key]
        result = cached
        if self.client is not None and (cached is None or not fresh(cached["observed_at"], 24, datetime.now(timezone.utc))):
            try:
                result = self.catalog.page(mpn, 0, limit=10, refresh=True, exact=True)
            except (LookupError, OSError, ValueError) as error:
                result = cached or {"status": "not_checked", "note": f"DigiKey lookup failed ({type(error).__name__})"}
        self.memo[key] = result or {"status": "not_checked", "note": "no cached DigiKey record (run online)"}
        return self.memo[key]


def digikey_online(args, check_market):
    return not args.offline and bool(args.verify_stock_alternatives or check_market or args.find_alternatives
                                     or args.alternatives_even_if_available)


def apply_baselines(lines, records, wanted):
    """Original part's DigiKey ratings fill what the schematic leaves out (see schematic_spec)."""
    for line in lines:
        if not wanted(line) or not line["class"] or not line["mpn"]:
            continue
        entry = records.get(line["mpn"])
        if "products" not in entry:
            line["baseline"] = {"status": "not_checked", "note": entry["note"]}
            continue
        private, public = baseline_from_digikey(line, entry["products"], entry["observed_at"])
        line["baseline"] = public
        if private:
            line["_baseline"] = private


def verify_stock_alternatives(lines, records):
    """Exact DigiKey record per in-stock candidate MPN, screened with the line's requirements."""
    for line in lines:
        candidates = [c for c in line.get("stock_alternatives", []) if c.get("mpn")]
        if not candidates:
            continue
        request, declared = verification_request(line, line["need"])
        if request is None:
            continue
        for candidate in candidates:
            entry = records.get(candidate["mpn"])
            if "products" not in entry:
                candidate["digikey"] = {"status": "not_checked", "note": entry["note"]}
            else:
                verify_candidate(candidate, entry["products"], entry["observed_at"], request, declared, line["manufacturer"])
        line["stock_alternatives"].sort(key=lambda a: ({"match": 0, "possible": 1, "rejected": 2}.get(a["spec_status"], 1),
                                                      not a["covers_need"], -(a["quantity"] or 0)))


def market_check(line, quantity, manufacturer, args, folder):
    from .cli import run as run_part
    namespace = argparse.Namespace(command="parts-stock", mpn=line["mpn"], manufacturer=manufacturer, quantity=quantity,
                                   min_stock=quantity, max_age_hours=args.max_age_hours,
                                   jlc_code=_jlc_code(line),
                                   output=str(folder), cache=args.cache, online=not args.offline, dry_run=False,
                                   stock_import=[], digikey_snapshot=None, max_stock_lookups=1, full_report=False)
    _quiet(run_part, namespace)
    report = read_json(folder / "sourcing.json")
    suppliers = {}
    for name in SUPPLIERS:
        summary = report["suppliers"][name]
        issues = sorted({i for o in summary["offers"] for i in o.get("issues", [])})
        suppliers[name] = {"status": summary["status"], "stock": summary["max_reported_stock"], "issues": issues}
    statuses = {s["status"] for s in suppliers.values()}
    overall = "available" if "available" in statuses else "stock_issues" if "needs_review" in statuses else "unknown"
    return {"status": overall, "quantity": quantity, "manufacturer": manufacturer, "suppliers": suppliers,
            "warnings": report["warnings"], "report": str(folder.relative_to(folder.parents[1]) / "sourcing.md")}


def _search(line, request, args, folder, name):
    from .cli import run as run_part
    write_json(folder / f"{name}.json", request)
    namespace = argparse.Namespace(command="parts-search", request=str(folder / f"{name}.json"), replace=line["mpn"],
                                   max_pages=args.max_pages, output=str(folder / name.replace("request", "search")),
                                   cache=args.cache, online=not args.offline, dry_run=False, stock_import=[],
                                   digikey_snapshot=None, max_stock_lookups=5, full_report=False)
    _quiet(run_part, namespace)
    return read_json(folder / name.replace("request", "search") / "sourcing.json")


def market_alternatives(line, request, args, folder, value=None):
    """DigiKey alternative search for one line; `value` (Decimal) overrides the schematic value
    used for the alternate-spelling retry (parts lists have no schematic component)."""
    folder.mkdir(parents=True)
    report, name = _search(line, request, args, folder, "request"), "request"
    tried = [request["value"]]
    # Keyword search depends on the value spelling; one retry with the alternate spelling on a shortfall.
    if value is None and line["class"]:
        value = schematic_spec(line)["value"]
    spec_values = catalog_values(line["class"], value) if line["class"] and value is not None else []
    for alternate in spec_values[1:2]:
        if report["status"] == "target_met" or alternate == request["value"]:
            break
        retry = _search(line, {**request, "value": alternate}, args, folder, "request-retry")
        tried.append(alternate)
        if retry["qualified_candidates"] > report["qualified_candidates"]:
            report, name = retry, "request-retry"
    original = manufacturer_key(line["manufacturer"]) if line["manufacturer"] else None
    top = [{"manufacturer": r["manufacturer"], "mpn": r["mpn"],
            "same_manufacturer": original is not None and manufacturer_key(r["manufacturer"]) == original,
            "digikey_stock": r["suppliers"]["digikey"]["max_reported_stock"],
            "lcsc_stock": r["suppliers"]["lcsc"]["max_reported_stock"]}
           for r in report["results"] if r["counts_toward_target"]][:5]
    root = folder.parents[1]
    return {"status": report["status"], "counted": report["qualified_candidates"], "target": report["target"],
            "distinct_manufacturers": report.get("distinct_manufacturers"),
            "other_manufacturers": len({manufacturer_key(r["manufacturer"]) for r in report["results"]
                                        if r["counts_toward_target"]} - {original}),
            "top": top, "warnings": report["warnings"], "value_spellings_tried": tried,
            "discovered": report.get("discovered_candidates"),
            "request": str((folder / f"{name}.json").relative_to(root)),
            "report": str((folder / name.replace("request", "search") / "sourcing.md").relative_to(root))}


# --- reports -------------------------------------------------------------------------------

def _public(line):
    return {k: v for k, v in line.items() if not k.startswith("_")}


def _cell(value):
    return str(value if value is not None else "").replace("|", "\\|").replace("\n", " ")


def _baseline_note(line):
    baseline = line.get("baseline") or {}
    return f" ⚠ {baseline['note']}" if baseline.get("status") in ("conflict", "unavailable", "not_checked") else ""


def _verdict(alt):
    dk = alt.get("digikey")
    if not dk:
        unknown = [c["check"] for c in alt["checks"] if c["status"] == "unknown"]
        return " (stock description only" + (f"; unknown: {', '.join(unknown)}" if unknown else "") + ")"
    if dk["status"] in ("not_found", "ambiguous", "not_checked"):
        return f" (stock description only; DigiKey {dk['status'].replace('_', ' ')}: {dk['note']})"
    parts = [f"DigiKey: {dk['lifecycle']}"]
    if dk.get("qualification"):
        parts.append(dk["qualification"])
    bad = [i["note"] + (f" (DigiKey: {i['actual']})" if i.get("actual") else "") if i.get("note")
           else f"{i['parameter']} {i['status']} ({i['actual']})" for i in dk["issues"]]
    if bad:
        parts.append("; ".join(bad))
    if alt.get("same_manufacturer"):
        parts.append("same manufacturer (not an independent source)")
    return " (" + " · ".join(_cell(x) for x in parts) + ")"


def markdown(report):
    s = report["summary"]
    lines = ["# Inventory check", "",
             f"Design: `{report['design']['path']}`  ",
             f"Boards: **{report['boards']}** · spare {report['spare']['percent']}% (min {report['spare']['minimum']} per line)"
             f" · sources: {', '.join(src['source'] for src in report['stock_sources'])}"
             + (" (combined)" if report["combine_sources"] else " (each separately)"), "",
             "| Status | Lines |", "| --- | --- |"]
    lines += [f"| {STATUS_TEXT.get(k, k)} | {v} |" for k, v in s["by_status"].items()]
    lines += ["", f"Lines needing a decision or review: **{s['needs_attention']}**. "
              f"Excluded (DNP/not in BOM/power symbols): {s['excluded_references']} references.", ""]
    if report["warnings"]:
        lines += ["## Warnings", ""] + [f"- {w}" for w in report["warnings"]] + [""]
    lines += ["## BOM vs stock", "", "| # | References | MPN | C-code | Per board | Needed | Best source | Status | Notes |",
              "| --- | --- | --- | --- | --- | --- | --- | --- | --- |"]
    for i, line in enumerate(report["lines"], 1):
        stock = line["stock"]
        best = next((f"{src['source']}: {src['quantity'] if src['quantity'] is not None else '?'}"
                     for src in sorted(stock["sources"], key=lambda x: -(x["quantity"] or 0)) if src["counted_rows"]), "—")
        notes = line["issues"] + stock["notes"]
        status = STATUS_TEXT.get(stock["status"], stock["status"]) + (" ⚠" if stock["review"] else "")
        lines.append("| " + " | ".join(_cell(v) for v in (
            i, ", ".join(line["references"]), line["mpn"] or line["value"],
            ", ".join(line["lcsc"]) or (f"(stock: {', '.join(stock['stock_lcsc'])})" if stock["stock_lcsc"] else ""),
            line["need"]["per_board"], line["need"]["needed"], best, status, "; ".join(notes))) + " |")
    shortlisted = [l for l in report["lines"] if l.get("stock_alternatives")]
    if shortlisted:
        lines += ["", "## Alternatives already in stock (R/C/L, engineering review required)", "",
                  "Lines that are already in stock appear here only with --alternatives-even-if-available (second sources).", "",
                  "First screened by value, package, tolerance, voltage/power and dielectric parsed from the stock file, then "
                  "(when available) against the exact DigiKey record: qualification (AEC-Q200), ratings and lifecycle. "
                  "`match` = every check passed on DigiKey data and the part is Active; `possible` = something could not be "
                  "confirmed; `rejected` = DigiKey data contradicts a requirement. MLCC capacitance under DC bias differs between "
                  "parts with the same code: review before swapping.", ""]
        for line in shortlisted:
            lines.append(f"**{', '.join(line['references'])}** ({line['mpn'] or line['value']}, need {line['need']['needed']}) — "
                         f"requires {line.get('requirements') or 'value/package only'}{_baseline_note(line)}:")
            for alt in line["stock_alternatives"]:
                lines.append(f"- {(alt.get('manufacturer') or '') + ' ' if alt.get('manufacturer') else ''}{alt['mpn'] or '?'} / {alt['lcsc'] or '?'} — {alt['source']}: "
                             f"{alt['quantity']} pcs{'' if alt['covers_need'] else ' (not enough alone)'} · **{alt['spec_status']}**"
                             + _verdict(alt))
            lines.append("")
    marketed = [l for l in report["lines"] if l.get("market")]
    if marketed:
        lines += ["## Market stock", "", "| References | MPN | Buy qty | Result | DigiKey | LCSC | JLCPCB | Report |",
                  "| --- | --- | --- | --- | --- | --- | --- | --- |"]
        for line in marketed:
            m = line["market"]
            if m.get("status") == "skipped":
                lines.append(f"| {_cell(', '.join(line['references']))} | {_cell(line['mpn'] or line['value'])} | | skipped: {_cell(m['reason'])} | | | | |")
                continue
            cells = [f"{m['suppliers'][s]['stock'] if m['suppliers'][s]['stock'] is not None else '?'} ({m['suppliers'][s]['status']})"
                     for s in SUPPLIERS]
            lines.append("| " + " | ".join(_cell(v) for v in (", ".join(line["references"]), line["mpn"], m["quantity"], m["status"],
                                                               *cells, f"[report]({m['report']})")) + " |")
        lines.append("")
    searched = [l for l in report["lines"] if l.get("market_alternatives")]
    if searched:
        lines += ["## Market alternatives (DigiKey catalog screening, engineering review required)", ""]
        skipped = {}
        for line in searched:
            a = line["market_alternatives"]
            head = (f"**{', '.join(line['references'])}** ({line['mpn'] or line['value']}"
                    + (f"; requires {line['requirements']}" if line.get("requirements") else "") + "): ")
            if a.get("status") in ("skipped", "not_needed", "not_selected"):
                prefix = "Not searched" if a["status"] in ("not_needed", "not_selected") else "Skipped"
                skipped.setdefault((prefix, a["reason"]), []).append(", ".join(line["references"]))
                continue
            purpose = " (second source)" if a.get("purpose") == "second_source" else ""
            lines.append(head + f"{a['status']}{purpose}, {a['counted']}/{a['target']} counted, "
                         f"{a.get('other_manufacturers', '?')} other manufacturer(s), {a.get('discovered', '?')} discovered"
                         f" (value searched as {' / '.join(a.get('value_spellings_tried', []))}) · [report]({a['report']}) · request `{a['request']}`")
            lines += [f"- {t['manufacturer']} {t['mpn']} — DigiKey {t['digikey_stock']}, LCSC {t['lcsc_stock'] if t['lcsc_stock'] is not None else '?'}"
                      + (" · same manufacturer (not an independent source)" if t.get("same_manufacturer") else "")
                      for t in a["top"]]
            lines.append("")
        lines += [f"- {prefix} ({'; '.join(refs)}): {reason}" for (prefix, reason), refs in skipped.items()]
        lines.append("")
    if report["excluded"]:
        lines += ["## Excluded", ""] + [f"- {', '.join(e['references'])}: {e['reason']}" for e in report["excluded"]] + [""]
    lines += ["## Next steps", ""] + [f"- {step}" for step in report["next_steps"]] + ["", report["scope"], ""]
    return "\n".join(lines)


def write_shortfall(path, report):
    with open(path, "w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(["References", "Manufacturer", "MPN", "LCSC", "Stock C-code", "Needed", "Best source qty", "Shortfall", "Status"])
        for line in report["lines"]:
            stock = line["stock"]
            if stock["status"] == "in_stock":
                continue
            writer.writerow([" ".join(line["references"]), line["manufacturer"] or "", line["mpn"] or "", " ".join(line["lcsc"]),
                             " ".join(stock["stock_lcsc"]), line["need"]["needed"], stock["best_source_quantity"], stock["shortfall"] or line["need"]["needed"],
                             stock["status"]])


# --- command -------------------------------------------------------------------------------

def run(args):
    if args.spare_percent < 0 or args.spare_min < 0:
        raise ValueError("Spare percent and minimum cannot be negative")
    if not 1 <= args.max_pages <= 20 or args.max_market_parts < 0 or args.max_alternative_searches < 0:
        raise ValueError("Check budgets: --max-pages 1–20, other budgets ≥ 0")
    args.cache = args.cache or str(CACHE_ROOT / "sourcing")
    output = Path(args.output).resolve()
    if output.exists():
        raise ValueError(f"{output} already exists (reports are never overwritten); choose a new --output, e.g. -002")
    overrides = parse_columns(args.column)
    stock_files = parse_stock_args(args.stock)
    interactive = not args.no_input and sys.stdin.isatty()
    ask = Asker(interactive)

    circuit, design = load_design(args.design)
    lines, excluded = bom_lines(circuit)
    sources = [load_stock(path, overrides, label, args.sheet) for label, path in stock_files]
    warnings = [w for s in sources for w in s["warnings"]]
    print(f"Design: {len(lines)} BOM lines ({sum(len(l['references']) for l in lines)} references), "
          f"{sum(len(e['references']) for e in excluded)} excluded")
    for s in sources:
        columns = ", ".join(f"{role}={header!r}" for role, header in s["columns"].items())
        print(f"Stock {s['source']}: {len(s['entries'])} parts (header row {s['header_row']}; {columns})")

    boards = args.boards
    if boards is None:
        if not interactive:
            raise ValueError("--boards is required when not running in a terminal")
        boards = ask.integer("How many boards do you need? ")
    if boards < 1:
        raise ValueError("--boards must be at least 1")

    selected = None  # ids of lines the user chose for alternatives (None = automatic choice)
    if args.alternatives_for:
        if args.alternatives_even_if_available:
            raise ValueError("Use either --alternatives-for (chosen lines) or --alternatives-even-if-available (all R/C/L lines)")
        if args.find_alternatives is False:
            raise ValueError("--alternatives-for contradicts --no-find-alternatives")
        chosen, problems = select_lines(lines, excluded, args.alternatives_for)
        if problems:
            raise ValueError("; ".join(problems))
        selected = {id(l) for l in chosen}
        args.find_alternatives = True

    def alternatives_wanted(line):
        return (line["stock"]["status"] in NEEDS_PARTS or (line["class"] and args.alternatives_even_if_available)
                or (selected is not None and id(line) in selected))

    for line in lines:
        line["need"] = required_quantity(len(line["references"]), boards, args.spare_percent, args.spare_min)
        line["stock"] = match_line(line, sources, line["need"], args.combine_sources)
        if line["identity"].startswith("none") and line["stock"]["status"] == "not_in_stock":
            line["stock"]["status"] = "unidentified"
        if alternatives_wanted(line) and (line["class"] or line["stock"]["status"] in NEEDS_PARTS):
            alternatives, reason = stock_alternatives(line, sources, line["need"])
            if alternatives:
                line["stock_alternatives"] = alternatives
            elif reason and line["class"]:
                line["stock_alternatives_note"] = reason

    open_lines = [l for l in lines if l["stock"]["status"] in NEEDS_PARTS]
    counts = {}
    for l in lines:
        counts[l["stock"]["status"]] = counts.get(l["stock"]["status"], 0) + 1
    print("Stock: " + ", ".join(f"{STATUS_TEXT.get(k, k)} {v}" for k, v in counts.items()))
    with_alternatives = sum(bool(l.get("stock_alternatives")) for l in open_lines)
    if with_alternatives:
        print(f"In-stock R/C/L candidates found for {with_alternatives} of {len(open_lines)} open lines (see report)")

    # Market stock: only lines with an exact MPN can be checked.
    market_lines = [l for l in open_lines if l["mpn"]]
    check_market = args.check_market
    if check_market is None:
        check_market = bool(market_lines) and interactive and ask.yes(
            f"{len(market_lines)} part(s) are missing or short. Check market stock (DigiKey/LCSC/JLCPCB"
            f"{', cache only' if args.offline else ', sends read-only API requests'})?")
    if check_market:
        for index, line in enumerate(market_lines):
            if index >= args.max_market_parts:
                line["market"] = {"status": "skipped", "reason": "market request budget reached (--max-market-parts)"}
                continue
            manufacturer = line["manufacturer"] or next(
                (row["manufacturer"] for src in line["stock"]["sources"] for row in src["rows"]
                 if row["counts"] and row.get("manufacturer")), None)
            if not manufacturer:
                line["market"] = {"status": "skipped", "reason": "manufacturer unknown (fill the Manufacturer field)"}
                continue
            quantity = line["stock"]["shortfall"] or line["need"]["needed"]
            print(f"  market: {line['mpn']} × {quantity} ...")
            try:
                line["market"] = market_check(line, quantity, manufacturer, args, output / "market" / f"line-{lines.index(line) + 1:03d}")
            except (OSError, ValueError, KeyError, TypeError) as error:
                line["market"] = {"status": "skipped", "reason": f"lookup failed ({type(error).__name__}); other lines continued"}
        for line in open_lines:
            if not line["mpn"]:
                line["market"] = {"status": "skipped", "reason": "no MPN: market lookups need manufacturer + exact MPN"}

    # Baselines from the original parts, then (re)screen stock alternatives and check them on DigiKey.
    records = (DigiKeyRecords(args, digikey_online(args, check_market), warnings)
               if args.verify_stock_alternatives is not False else None)
    prepared = set()

    def prepare(subset):
        subset = [l for l in subset if id(l) not in prepared]
        prepared.update(id(l) for l in subset)
        if records is None or not subset:
            return
        apply_baselines(subset, records, lambda line: True)
        for line in subset:
            if line.get("_baseline") or (line["class"] and "stock_alternatives" not in line):
                alternatives, _ = stock_alternatives(line, sources, line["need"])
                line.pop("stock_alternatives", None)
                if alternatives:
                    line["stock_alternatives"] = alternatives
        verify_stock_alternatives(subset, records)

    prepare([l for l in lines if alternatives_wanted(l)])
    if records is not None and records.lookups:
        print(f"  DigiKey records (original parts + in-stock candidates): {records.lookups} lookup(s)"
              f"{'' if records.client else ' (cache only)'}")

    # Alternatives: lines not available on the market (or market not checked) and not
    # already covered by a fully matching in-stock candidate.
    def wants_search(line):
        if line.get("market", {}).get("status") == "available":
            return False
        return not any(a["spec_status"] == "match" and a["covers_need"] for a in line.get("stock_alternatives", []))

    alt_lines = [l for l in open_lines if wants_search(l)]
    if selected is not None:
        alt_lines = [l for l in lines if id(l) in selected]
    elif args.alternatives_even_if_available:
        if args.find_alternatives is False:
            raise ValueError("--alternatives-even-if-available contradicts --no-find-alternatives")
        second = {id(l) for l in lines if l["class"] and l not in alt_lines}
        alt_lines = [l for l in lines if l in alt_lines or id(l) in second]
        args.find_alternatives = True
    find = args.find_alternatives
    if find is None and interactive and any(l["class"] for l in lines):
        chosen = pick_lines(ask, lines, excluded, alt_lines)
        if chosen:
            if [id(l) for l in chosen] != [id(l) for l in alt_lines]:
                selected = {id(l) for l in chosen}
            alt_lines = chosen
            prepare(alt_lines)  # baseline + in-stock check for lines picked beyond the default set
        find = bool(chosen)
    if find:
        for line in open_lines:
            if line not in alt_lines:
                if selected is not None:
                    why, status = "not selected", "not_selected"
                else:
                    status = "not_needed"
                    why = ("available on the market" if line.get("market", {}).get("status") == "available"
                           else "a fully matching part is already in stock")
                line["market_alternatives"] = {"status": status, "reason": why}
        searches = 0
        for line in alt_lines:
            request, reason = draft_request(line, line["need"])
            if not request:
                line["market_alternatives"] = {"status": "skipped", "reason": reason}
                continue
            if searches >= args.max_alternative_searches:
                line["market_alternatives"] = {"status": "skipped", "reason": "search budget reached (--max-alternative-searches)"}
                continue
            searches += 1
            print(f"  alternatives: {', '.join(line['references'][:4])} ({request['kind']} {request['value']} {request['package']}) ...")
            try:
                line["market_alternatives"] = market_alternatives(line, request, args, output / "alternatives" / f"line-{lines.index(line) + 1:03d}")
                line["market_alternatives"]["purpose"] = ("replacement" if line["stock"]["status"] in NEEDS_PARTS
                                                          else "second_source")
            except (OSError, ValueError, KeyError, TypeError) as error:
                line["market_alternatives"] = {"status": "skipped", "reason": f"search failed ({type(error).__name__}: {error})"}

    for line in lines:
        spec = schematic_spec(line) if line["class"] else None
        if spec:
            line["requirements"] = requirement_summary(spec)
            line["requirement_sources"] = spec["provenance"]
    next_steps = []
    if any(l["stock"]["status"] == "unidentified" for l in lines):
        next_steps.append("Fill Manufacturer + MPN (or at least the LCSC C-code) for lines marked 'no MPN/C-code'.")
    if any("JLCPCB assembly consumes stock by C-code" in n for l in lines for n in l["stock"]["notes"]):
        next_steps.append("Update the LCSC field where the stock C-code differs, or JLCPCB will not use your stock.")
    missing_codes = [l for l in lines if not l["lcsc"] and l["stock"]["stock_lcsc"]]
    if missing_codes:
        next_steps.append(f"{len(missing_codes)} line(s) have no LCSC field but match stock with a C-code (shown as '(stock: C…)'); "
                          "JLCPCB assembly BOMs need the C-code to use your stock.")
    if open_lines and not check_market:
        next_steps.append("Rerun with --check-market to see supplier stock for missing/short parts.")
    if any(l.get("stock_alternatives") for l in lines) or any(l.get("market_alternatives", {}).get("top") for l in lines):
        next_steps.append("Review alternatives against your circuit (ratings, DC bias, qualification) before changing the schematic.")
    if not next_steps:
        next_steps.append("Nothing open in what was checked.")
    report = {
        "schema_version": 1, "mode": "inventory_check", "generated_at": utc_now(), "design": design, "boards": boards,
        "spare": {"percent": args.spare_percent, "minimum": args.spare_min}, "combine_sources": args.combine_sources,
        "stock_sources": [{k: v for k, v in s.items() if k != "entries"} | {"parts": len(s["entries"])} for s in sources],
        "market_checked": bool(check_market), "alternatives_searched": bool(find), "online": not args.offline,
        "summary": {"by_status": counts, "needs_attention": sum(l["stock"]["status"] != "in_stock" or l["stock"]["review"] for l in lines),
                    "excluded_references": sum(len(e["references"]) for e in excluded)},
        "warnings": warnings, "lines": [_public(l) for l in lines], "excluded": excluded, "next_steps": next_steps,
        "scope": ("Quantities = populated references × boards + spare. MPN identifies a part; C-code matches with a different MPN "
                  "never count. Stock files are read as supplied (their date is not verified). Alternatives are candidates "
                  "for engineering review, not approvals. No schematic edits or orders."),
    }
    output.mkdir(parents=True, exist_ok=True)
    write_json(output / "inventory.json", report)
    (output / "inventory.md").write_text(markdown(report), encoding="utf-8")
    write_shortfall(output / "shortfall.csv", report)
    print(f"Report: {output / 'inventory.md'}")
    print(f"Shortfall list: {output / 'shortfall.csv'}")
    return 0 if all(l["stock"]["status"] == "in_stock" and not l["stock"]["review"] for l in lines) else 2
