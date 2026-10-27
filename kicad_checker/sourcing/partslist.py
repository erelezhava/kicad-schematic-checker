"""`parts-list`: check a recommended parts list (CSV/XLSX of exact MPNs) on DigiKey, optionally
against your stock files, and look for alternatives for problem parts.

Typical inputs: a chip vendor's reference-design BOM, an approved-parts list, a colleague's
"use these" sheet. One exact part per row (MPN, ideally with Manufacturer). LCSC/JLCPCB
columns join automatically once those API keys are configured.

Read-only: offline by default, `--online` sends read-only supplier requests. Reports go into
a new directory. Alternatives are engineering-review leads, never approvals.
"""

import argparse
import csv
import re
from pathlib import Path

from .. import digikey
from ..core import write_json
from ..paths import CACHE_ROOT
from .inventory import (UNIT, _request, inductor_current_known, match_line, requirement_summary, required_quantity, screen_stock_for_spec,
                        spec_from_product, verify_candidate)
from .providers import SUPPLIERS, manufacturer_key, utc_now
from .stockfile import ROLES, load_stock

STATUS_TEXT = {"not_fitted": "not fitted", "not_a_part": "not a part (PCB)", "demand_unknown": "quantity unreadable",
               "available": "available", "stock_issues": "stock issues", "not_active": "not active",
               "not_found": "not found", "ambiguous": "ambiguous", "manufacturer_differs": "manufacturer differs",
               "not_checked": "not checked", "no_mpn": "no MPN"}
PROBLEMS = ("not_active", "stock_issues")
# Primary-part states for which the vendor's listed alternates are checked.
CHECK_ALTERNATES = PROBLEMS + ("not_found", "manufacturer_differs", "ambiguous")
NOT_FITTED_TEXT = re.compile(r"(?<![\w-])(DNP|DNF|DNI|NF|N/F|NOT\s+FITTED|NOT\s+POPULATED|DO\s+NOT\s+(POPULATE|PLACE|FIT|MOUNT)"
                             r"|NO\s*POP|NOT\s+MOUNTED)(?![\w-])", re.I)


def add_command(sub):
    c = sub.add_parser("parts-list", help="Check a recommended parts list (CSV/XLSX of MPNs) on DigiKey; compare with stock; "
                                          "alternatives for obsolete or unstocked parts")
    c.add_argument("partslist", help="CSV or XLSX, one exact part per row (MPN column required, Manufacturer recommended)")
    c.add_argument("--column", action="append", default=[], metavar="ROLE=HEADER",
                   help=f"Override a detected column; roles: {', '.join(ROLES)}")
    c.add_argument("--sheet", help="XLSX sheet name")
    c.add_argument("--boards", type=int, default=1, help="Boards to build (quantity = per-board quantity × boards + spare)")
    c.add_argument("--quantity", type=int, default=1, help="Per-board quantity for rows without a quantity column (default 1)")
    c.add_argument("--spare-percent", type=float, default=5.0)
    c.add_argument("--spare-min", type=int, default=2)
    c.add_argument("--min-stock", type=int, default=None,
                   help="Supplier stock a part should have (default: the needed quantity)")
    c.add_argument("--stock", action="append", default=[], metavar="[LABEL=]FILE", help="Also compare with stock files (repeatable)")
    c.add_argument("--combine-sources", action="store_true")
    c.add_argument("--online", action="store_true", help="Fetch fresh supplier data (default: cache only)")
    c.add_argument("--find-alternatives", action=argparse.BooleanOptionalAction, default=True,
                   help="Search DigiKey alternatives for R/C/L problem rows (default on; uses the search budget)")
    c.add_argument("--max-lookups", type=int, default=60, help="DigiKey exact lookups (list rows + in-stock candidates)")
    c.add_argument("--max-alternative-searches", type=int, default=10)
    c.add_argument("--max-pages", type=int, default=2)
    c.add_argument("--max-age-hours", type=float, default=24, help="Stock freshness limit")
    c.add_argument("--cache", default=None)
    c.add_argument("--output", help="New report directory (required except with --dry-run)")
    c.add_argument("--against", metavar="DESIGN",
                   help="Compare your schematic with this reference BOM: KiCad project, netlist XML or circuit.json. "
                        "Flags parts whose MPN or value differs from the reference")
    c.add_argument("--dry-run", action="store_true",
                   help="Only show how the list's columns and first rows are read; no lookups, nothing written")
    c.add_argument("--preview-rows", type=int, default=10, help="Rows shown by --dry-run (default 10)")


# --- pure helpers --------------------------------------------------------------------------

def find_product(mpn, manufacturer, products):
    """(product, status, note) for one list row against a DigiKey exact-lookup result."""
    key = digikey.mpn_key(mpn)
    same = [p for p in products if digikey.mpn_key(p.get("ManufacturerProductNumber") or "") == key]
    if not same:
        return None, "not_found", "exact MPN not listed by DigiKey"
    names = sorted({(p.get("Manufacturer") or {}).get("Name") or "?" for p in same})
    if manufacturer:
        named = [p for p in same if manufacturer_key((p.get("Manufacturer") or {}).get("Name")) == manufacturer_key(manufacturer)]
        if not named:
            return None, "manufacturer_differs", f"DigiKey lists {mpn} under {', '.join(names)}, not {manufacturer!r}"
        same = named
    elif len({manufacturer_key(n) for n in names}) > 1:
        return None, "ambiguous", f"{mpn} is listed under {', '.join(names)}; add a Manufacturer column"
    return same[0], "found", None


def count_references(text):
    """'C1, C2' → 2, 'C1-C4' → 4, 'R1 R3-R5' → 4. None when there are no designators."""
    count = 0
    for token in (x for x in re.split(r"[,;\s]+", text or "") if x):
        span = re.fullmatch(r"([A-Za-z_]+)(\d+)\s*[-–]\s*(?:\1)?(\d+)", token)
        count += int(span.group(3)) - int(span.group(2)) + 1 if span and int(span.group(3)) >= int(span.group(2)) else 1
    return count or None


def per_board_quantity(entry, has_quantity, default):
    """(quantity, note, known). An explicit quantity wins; a blank cell falls back to the designator
    count or --quantity (with a note); an unreadable explicit cell is unknown demand, never a guess."""
    designators = count_references(entry["references"])
    text = (entry.get("quantity_text") or "").strip()
    if has_quantity and entry["quantity"]:
        note = (f"quantity {entry['quantity']} but {designators} designators; quantity used"
                if designators and designators != entry["quantity"] else None)
        return entry["quantity"], note, True
    if has_quantity and text and entry["quantity"] is None:
        return designators or default, f"quantity {text!r} is unreadable: demand unknown, fix the cell", False
    fallback = designators or default
    if has_quantity and not text:
        return fallback, ("quantity cell blank; designator count used" if designators else
                          f"quantity cell blank; --quantity {default} used"), True
    return fallback, None, True


def fitting(entry, has_quantity):
    """'not_fitted' / 'not_a_part' with a reason, or (None, None) for a part to source."""
    if (entry["references"] or "").startswith("!") or re.search(r"printed\s+circuit\s+board|\bbare\s+pcb\b",
                                                                 entry["description"] or "", re.I):
        return "not_a_part", "the bare PCB (TI '!PCB' rows), not a component"
    if entry.get("fitted") is False:
        return "not_fitted", "marked not fitted in the list"
    if has_quantity and entry["quantity"] == 0:
        return "not_fitted", "quantity 0 (reference BOMs list not-fitted parts this way)"
    if NOT_FITTED_TEXT.search(entry["value"] or "") or NOT_FITTED_TEXT.search(entry["description"] or ""):
        return "not_fitted", "Value/Description says not fitted (DNP)"
    return None, None


ROLE_HELP = {"mpn": "exact part number, looked up", "manufacturer": "manufacturer", "references": "designators",
             "quantity": "per-board quantity", "fitted": "fitted / DNP flag", "lcsc": "LCSC/JLCPCB C-code",
             "value": "checked for DNP", "description": "checked for DNP", "package": "not used", "category": "not used"}
ROLE_ORDER = ("mpn", "manufacturer", "references", "quantity", "fitted", "lcsc", "value", "description", "package",
              "category", "alternates")


def dry_run_lines(parts, sources, args, warnings):
    """Plain-text preview: column mapping, ignored headers, the first rows as they will be read,
    and what a real run would look up. No network, no files."""
    columns = parts["columns"]
    has_quantity = "quantity" in columns
    lines = ["Dry run: nothing is looked up or written.", "",
             f"Parts list: {parts['path']}" + (f" (sheet {parts['sheet']!r})" if parts.get("sheet") else "")
             + f", header row {parts['header_row']}, {len(parts['entries'])} part rows", "", "Columns used:"]
    for role, header in sorted(columns.items(), key=lambda item: ROLE_ORDER.index(item[0]) if item[0] in ROLE_ORDER else 99):
        if role == "alternates":
            lines += [f"  {'alternate':<13}← {h}   (vendor alternate, checked if the main part has a problem)" for h in header]
        else:
            lines.append(f"  {role:<13}← {header}   ({ROLE_HELP.get(role, role)})")
    used = {h for role, h in columns.items() if role != "alternates"} | {
        part.strip() for pair in columns.get("alternates", []) for part in pair.split(" + ")}
    ignored = [h for h in parts.get("headers", []) if h and h not in used]
    if ignored:
        lines.append("Columns ignored: " + ", ".join(ignored))
    hints = []
    if "manufacturer" not in columns:
        hints.append("no manufacturer column: rows whose MPN DigiKey lists under several makers will be 'ambiguous'")
    if not has_quantity:
        hints.append("no quantity column: per-board quantity = designator count, else --quantity "
                     f"({args.quantity})" if "references" in columns else
                     f"no quantity column: per-board quantity = --quantity ({args.quantity})")
    if "references" not in columns:
        hints.append("no designator column")
    lines += [f"Note: {h}" for h in hints]
    lines += ["If a column is wrong or missing: --column ROLE=\"Header\" (roles: " + ", ".join(ROLES) + ")", ""]

    plan = []
    for entry in parts["entries"]:
        skip, reason = fitting(entry, has_quantity)
        per_board, quantity_note, quantity_known = per_board_quantity(entry, has_quantity, args.quantity)
        if not quantity_known:
            per_board = "?"
        if skip:
            action = f"skip: {reason}"
        elif not entry["mpn"]:
            action = "no MPN (C-code only): not looked up"
        else:
            action = "look up" + (f" (+{len(entry['alternates'])} alternate)" if entry["alternates"] else "")
            if not quantity_known:
                action += f"; {quantity_note}"
        plan.append((entry, per_board, skip, action))
    shown = plan[:max(args.preview_rows, 0)]
    if shown:
        width = {"refs": max([4] + [len(_refs(e["references"], 3)) for e, *_ in shown]),
                 "mpn": max([3] + [len(e["mpn"] or "") for e, *_ in shown]),
                 "mfr": max([12] + [len(e["manufacturer"] or "") for e, *_ in shown])}
        lines.append(f"First {len(shown)} rows as read:")
        lines.append(f"  {'Row':<5}{'Refs':<{width['refs'] + 2}}{'MPN':<{width['mpn'] + 2}}{'Manufacturer':<{width['mfr'] + 2}}"
                     f"{'Per board':<11}Will do")
        for entry, per_board, _, action in shown:
            lines.append(f"  {entry['row']:<5}{_refs(entry['references'], 3):<{width['refs'] + 2}}"
                         f"{entry['mpn'] or '':<{width['mpn'] + 2}}{entry['manufacturer'] or '':<{width['mfr'] + 2}}"
                         f"{per_board:<11}{action}")
        if len(plan) > len(shown):
            lines.append(f"  … {len(plan) - len(shown)} more rows (--preview-rows N to show more)")
        lines.append("")
    lookups = sum(1 for e, _, skip, _ in plan if not skip and e["mpn"])
    alternates = sum(len(e["alternates"]) for e, _, skip, _ in plan if not skip and e["mpn"])
    skipped = {}
    for _, _, skip, _ in plan:
        if skip:
            skipped[skip] = skipped.get(skip, 0) + 1
    no_mpn = sum(1 for e, _, skip, _ in plan if not skip and not e["mpn"])
    lines.append(f"Summary: {len(plan)} rows → {lookups} to look up"
                 + "".join(f", {n} {STATUS_TEXT[k]}" for k, n in skipped.items())
                 + (f", {no_mpn} without MPN" if no_mpn else "")
                 + (f"; {alternates} vendor alternate(s), looked up only when the main part has a problem" if alternates else ""))
    lines.append(f"DigiKey lookups for a real run: {lookups}" + (f" to {lookups + alternates}" if alternates else "")
                 + f" plus alternative checks (budget --max-lookups {args.max_lookups}); cached results from the last 24 h are reused")
    for source in sources:
        lines.append(f"Stock {source['source']}: header row {source['header_row']}, {len(source['entries'])} parts; "
                     + ", ".join(f"{r}={h!r}" for r, h in source["columns"].items() if r != "alternates"))
    if warnings:
        lines += ["", "Warnings:"] + [f"  - {w}" for w in dict.fromkeys(warnings)]
    return lines


# --- your schematic vs the reference BOM -------------------------------------------------------

AGAINST_TEXT = {"same_part": "same part", "vendor_alternate": "vendor alternate",
                "unconfirmed": "same MPN, manufacturer not confirmed", "manufacturer_differs": "same MPN, other manufacturer",
                "substituted": "substituted", "value_differs": "value differs", "incomplete": "positions missing",
                "not_in_design": "not in your schematic"}
# Row states that are fine without further review (anything else keeps exit code 2).
AGAINST_OK = ("same_part", "vendor_alternate")
REVIEW_NOTE = ("reference-design part: a substitution needs re-verification in this circuit "
               "(stability, DC bias, current, timing)")
VERDICT_RANK = {"rejected": 0, "unknown": 1, "review": 2, "possible": 3, "match": 4}


def expand_references(text):
    """'C1-C3, C7' → ['C1', 'C2', 'C3', 'C7']."""
    result = []
    for token in (x for x in re.split(r"[,;\s]+", text or "") if x):
        span = re.fullmatch(r"([A-Za-z_]+)(\d+)\s*[-–]\s*(?:\1)?(\d+)", token)
        if span and int(span.group(3)) >= int(span.group(2)) and int(span.group(3)) - int(span.group(2)) < 1000:
            result += [f"{span.group(1)}{n}" for n in range(int(span.group(2)), int(span.group(3)) + 1)]
        else:
            result.append(token)
    return result


def design_parts(circuit):
    """Populated, purchasable components of the design: ref → {mpn, manufacturer, value, footprint, class}."""
    from ..core import component_exemption, identity_candidates
    from ..description import component_class
    from .inventory import _excluded
    parts = {}
    for ref, component in circuit["components"].items():
        if component_exemption(ref, component) or _excluded(component):
            continue
        mpns = list(dict.fromkeys(identity_candidates(component, "part_number").values()))
        makers = list(dict.fromkeys(identity_candidates(component, "manufacturer").values()))
        parts[ref] = {"ref": ref, "mpn": mpns[0] if len(mpns) == 1 else None,
                      "manufacturer": makers[0] if len(makers) == 1 else None, "value": component.get("value") or "",
                      "footprint": component.get("footprint") or "", "class": component_class(ref)}
    return parts


def _same_value(cls, design_value, r):
    """Compare a design Value with the reference row's value (Value column, else the reference
    part's DigiKey value). None when either side is unreadable."""
    from ..units import schematic_quantity
    unit = UNIT.get(cls)
    if not unit:
        return None
    x, _ = schematic_quantity(design_value or "", unit)
    y, _ = schematic_quantity(r.get("value") or "", unit)
    if y is None and r.get("_product"):
        spec = spec_from_product(r["_product"])
        y = spec["value"] if spec and spec["class"] == cls else None
    return None if x is None or y is None else x == y


def _size(footprint, package=""):
    from ..description import IMPERIAL_CODES, footprint_size, size_codes
    size = footprint_size(footprint)
    if size:
        return size[0]
    codes = size_codes(package or "")[0]
    return package if package in IMPERIAL_CODES else (next(iter(codes)) if len(codes) == 1 else None)


def _identity(part, mpn, manufacturer):
    """How a design part relates to one reference identity: 'same', 'unconfirmed' (same MPN, a
    manufacturer missing on one side), 'other_maker' (same MPN, different manufacturer) or None."""
    from ..core import compact
    if not part["mpn"] or not mpn or compact(part["mpn"]) != compact(mpn):
        return None
    if not part["manufacturer"] or not manufacturer:
        return "unconfirmed"
    return "same" if manufacturer_key(part["manufacturer"]) == manufacturer_key(manufacturer) else "other_maker"


def classify_position(r, part):
    """Status of one design position against a reference row (before substitution screening)."""
    for kind, mpn, maker in [("reference", r["mpn"], r["manufacturer"])] + [
            ("alternate", a["mpn"], a.get("manufacturer")) for a in r["vendor_alternates"]]:
        relation = _identity(part, mpn, maker)
        if relation == "same":
            return "same_part" if kind == "reference" else "vendor_alternate"
        if relation == "unconfirmed":
            return "unconfirmed"
        if relation == "other_maker":
            return "manufacturer_differs"
    return "substituted"


def compare_design(rows, circuit, records):
    """One result per fitted reference row. Every expected position is allocated to at most one
    design part (declared designators first, then the reference/alternate MPN, then R/C/L by value
    and size), and every position is classified and screened on its own."""
    from ..core import compact
    from ..description import component_class
    parts = design_parts(circuit)
    rows = [r for r in rows if r["status"] not in ("not_fitted", "not_a_part") and (r["mpn"] or r["vendor_alternates"])]
    used, slots = set(), {}
    for r in rows:
        refs = expand_references(r.get("references"))
        slots[id(r)] = {"refs": refs, "expected": len(refs) or r["need"]["per_board"],
                        "class": component_class(refs[0]) if refs else None, "positions": []}

    def take(r, part, how):
        used.add(part["ref"])
        slots[id(r)]["positions"].append((part, how))

    def agrees(r, part):
        slot_class = slots[id(r)]["class"]
        return (classify_position(r, part) != "substituted"
                or (slot_class in UNIT and part["class"] == slot_class and _same_value(slot_class, part["value"], r)))

    # Pass 1: declared designators whose part agrees (same identity, or same R/C/L value), for every
    # row before any row may borrow by MPN or value. A pasted reference circuit can collide with
    # your own unrelated C1/U1, so a conflicting part at the same designator waits for pass 4.
    for r in rows:
        for ref in slots[id(r)]["refs"]:
            if ref in parts and ref not in used and agrees(r, parts[ref]):
                take(r, parts[ref], "designator")
    # Pass 2: the reference or a vendor-alternate MPN at other designators (renumbered copies).
    identities = {}
    for part in parts.values():
        if part["mpn"]:
            identities.setdefault(compact(part["mpn"]), []).append(part)
    for r in rows:
        slot = slots[id(r)]
        wanted = [m for m in [r["mpn"]] + [a["mpn"] for a in r["vendor_alternates"]] if m]
        for mpn in wanted:
            for part in sorted(identities.get(compact(mpn), []), key=lambda p: p["ref"]):
                if len(slot["positions"]) >= slot["expected"]:
                    break
                if part["ref"] not in used:
                    take(r, part, "MPN (designators differ)")
    # Pass 3: R/C/L with the same value and size (renumbered substitutes).
    for r in rows:
        slot = slots[id(r)]
        if slot["class"] not in UNIT or len(slot["positions"]) >= slot["expected"]:
            continue
        size = _size("", r.get("package")) or spec_package(r)
        for part in sorted(parts.values(), key=lambda p: p["ref"]):
            if len(slot["positions"]) >= slot["expected"]:
                break
            if (part["ref"] not in used and part["class"] == slot["class"] and _same_value(slot["class"], part["value"], r)
                    and (size is None or _size(part["footprint"]) == size)):
                take(r, part, "value and size (designators differ)")
    # Pass 4: last resort, a different part at the declared designator (likely a substitution,
    # possibly an unrelated part of your own design: the report says which).
    for r in rows:
        slot = slots[id(r)]
        for ref in slot["refs"]:
            if len(slot["positions"]) >= slot["expected"]:
                break
            if ref in parts and ref not in used:
                take(r, parts[ref], "designator only (different part; could be unrelated)")

    verdicts, results = {}, []
    for r in rows:
        slot = slots[id(r)]
        result = {"row": r["row"], "references": slot["refs"], "reference_mpn": r["mpn"],
                  "reference_manufacturer": (r.get("catalog") or {}).get("manufacturer") or r["manufacturer"],
                  "class": slot["class"], "expected_positions": slot["expected"], "positions": [], "notes": []}
        for part, how in slot["positions"]:
            position = {"ref": part["ref"], "mpn": part["mpn"], "manufacturer": part["manufacturer"],
                        "value": part["value"], "located_by": how, "status": classify_position(r, part)}
            if slot["class"] in UNIT and _same_value(slot["class"], part["value"], r) is False:
                position["status"] = "value_differs"
                position["note"] = f"reference value {r.get('value') or 'per DigiKey record'!r}, your schematic {part['value']!r}"
            if position["status"] in ("substituted", "value_differs"):
                key = (manufacturer_key(part["manufacturer"]), compact(part["mpn"] or ""))
                if key not in verdicts:
                    verdicts[key] = substitution_verdict(r, part, records)
                position["verdict"] = verdicts[key]
            if position["status"] == "manufacturer_differs":
                position["note"] = f"your part is {part['manufacturer']!r}; the reference is {result['reference_manufacturer']!r}"
            if position["status"] == "unconfirmed":
                position["note"] = "manufacturer missing in your schematic or the reference BOM; confirm it is the same part"
            result["positions"].append(position)
        statuses = [p["status"] for p in result["positions"]]
        missing = slot["expected"] - len(statuses)
        for status in ("value_differs", "manufacturer_differs", "substituted", "unconfirmed", "vendor_alternate", "same_part"):
            if status in statuses:
                result["status"] = status
                break
        else:
            result["status"] = "not_in_design"
        if statuses and missing > 0 and result["status"] in AGAINST_OK:
            result["status"] = "incomplete"
        if missing > 0 and statuses:
            result["notes"].append(f"{len(statuses)} of {slot['expected']} positions found (renumbered beyond recognition, "
                                   "or not copied)")
        screened = [p["verdict"] for p in result["positions"] if p.get("verdict")]
        if screened:
            result["verdict"] = min(screened, key=lambda v: VERDICT_RANK.get(v["status"], 1))  # the worst member decides
            result["notes"].append(REVIEW_NOTE)
        if len({(p["mpn"], p["manufacturer"]) for p in result["positions"]}) > 1:
            result["notes"].append("positions use different parts: "
                                   + ", ".join(f"{p['ref']}={p['mpn'] or '?'}" for p in result["positions"]))
        results.append(result)
    summary = {}
    for result in results:
        summary[result["status"]] = summary.get(result["status"], 0) + 1
    return {"results": results, "summary": summary,
            "open": sum(1 for x in results if x["status"] not in AGAINST_OK),
            "design_parts_not_in_reference": len([p for p in parts if p not in used])}


def spec_package(r):
    product = r.get("_product")
    spec = spec_from_product(product) if product else None
    return spec["package"] if spec else None


def substitution_verdict(r, part, records):
    """match / possible / rejected for an R/C/L substitute, screened against the reference part's
    DigiKey ratings; 'review' for other parts (datasheet and pinout comparison is manual)."""
    if not part["mpn"]:
        return {"status": "unknown", "note": "your part has no MPN"}
    product = r.get("_product")
    spec = spec_from_product(product) if product else None
    if spec is None:
        return {"status": "review", "note": "not an R/C/L part, or the reference part has no readable DigiKey record: "
                                            "compare datasheets and pinout"}
    record = records.get(part["mpn"])
    if "products" not in record:
        return {"status": "unknown", "note": record["note"]}
    tolerance = spec["tolerance_percent"]
    request = _request(spec, r["need"], tolerance if tolerance is not None else 100)
    declared = {"tolerance": tolerance is not None, "power": spec["power"] is not None or spec["class"] != "resistor",
                "current": inductor_current_known(spec)}
    candidate = verify_candidate({"mpn": part["mpn"], "manufacturer": part["manufacturer"], "checks": []},
                                 record["products"], record["observed_at"], request, declared,
                                 (r.get("catalog") or {}).get("manufacturer"))
    dk = candidate.get("digikey") or {}
    if dk.get("status") in ("not_found", "ambiguous", "manufacturer_differs", "not_checked"):
        return {"status": "unknown", "note": dk.get("note"), "requirements": requirement_summary(spec)}
    return {"status": candidate.get("spec_status", "unknown"), "requirements": requirement_summary(spec), "digikey": dk}


def _verdict_text(verdict):
    issues = [i.get("note") or f"{i['parameter']} {i['status']} ({i.get('actual')})"
              for i in (verdict.get("digikey") or {}).get("issues", [])]
    text = f"**{verdict['status']}**"
    if issues:
        text += " (" + "; ".join(issues) + ")"
    elif verdict.get("note"):
        text += f" ({verdict['note']})"
    return text


def against_markdown(report):
    against = report["against"]
    lines = ["", f"## Your schematic vs the reference BOM (`{against['design']['path']}`)", "",
             "| Status | Rows |", "| --- | --- |"]
    lines += [f"| {AGAINST_TEXT.get(k, k)} | {v} |" for k, v in against["summary"].items()]
    lines += ["", "| Row | Reference refs | Reference part | Your schematic | Result |", "| --- | --- | --- | --- | --- |"]
    for res in against["results"]:
        mine = []
        for position in res["positions"]:
            text = f"{position['ref']}: {position['mpn'] or '?'}"
            if not position["located_by"] == "designator":
                text += f" (found by {position['located_by'].split(' (')[0]})"
            if position["status"] not in ("same_part",):
                text += f" — {AGAINST_TEXT[position['status']]}"
                if position.get("verdict"):
                    text += " · " + _verdict_text(position["verdict"])
                if position.get("note"):
                    text += f" ({position['note']})"
            mine.append(text)
        result = AGAINST_TEXT[res["status"]]
        if res.get("verdict"):
            result += " · " + f"**{res['verdict']['status']}**"
        notes = [n for n in res["notes"] if n != REVIEW_NOTE and not n.startswith("positions use different parts")]
        if notes:
            result += " · " + "; ".join(notes)
        if REVIEW_NOTE in res["notes"]:
            result += " ⚠"
        lines.append("| " + " | ".join(_cell(x) for x in (res["row"], _refs(", ".join(res["references"]), 4),
                                                          res.get("reference_mpn") or "", "<br>".join(mine), result)) + " |")
    lines += ["", f"⚠ {REVIEW_NOTE[0].upper() + REVIEW_NOTE[1:]}.",
              f"Open comparison items: {against['open']} (these keep the exit code at 2).",
              f"Parts in your schematic that are not in the reference BOM: {against['design_parts_not_in_reference']} "
              "(the rest of your design; not compared)."]
    return lines


def catalog_facts(product):
    lifecycle, flags = digikey.lifecycle(product)
    qualification = ", ".join(v for v in (digikey.parameter(product, x) for x in ("Ratings", "Features", "Qualification"))
                              if v and v.strip() != "-")
    description = product.get("Description") or {}
    return {"manufacturer": (product.get("Manufacturer") or {}).get("Name"), "lifecycle": lifecycle, "flags": flags,
            "normally_stocking": product.get("NormallyStocking"),
            "aec_q200": bool(re.search(r"AEC-?Q(100|101|200)", qualification, re.I)) or None,
            "qualification": qualification or None,
            "description": description.get("DetailedDescription") or description.get("ProductDescription"),
            "category": " / ".join(n.get("Name", "") for n in _nodes(product)),
            "datasheet": product.get("DatasheetUrl"), "url": product.get("ProductUrl"),
            "digikey_pns": [v.get("DigiKeyProductNumber") for v in product.get("ProductVariations") or []
                            if v.get("DigiKeyProductNumber") and v.get("MarketPlace") is False]}


def _nodes(product):
    from .requirements import category_nodes
    return list(category_nodes(product))


def row_status(match_status, facts, suppliers):
    """available / stock_issues / not_active, or the identity status when no product matched."""
    if match_status != "found":
        return match_status
    if str(facts["lifecycle"]).casefold() != "active" or facts["flags"]:
        return "not_active"
    return "available" if suppliers["digikey"]["status"] == "available" else "stock_issues"


# --- command -------------------------------------------------------------------------------

def run(args):
    from .cli import jlc_observations, lcsc_observations
    from .inventory_cli import DigiKeyRecords, _verdict, market_alternatives, parse_columns, parse_stock_args
    from .service import stock_report
    for name in ("boards", "quantity"):
        if getattr(args, name) < 1:
            raise ValueError(f"--{name} must be at least 1")
    if args.spare_percent < 0 or args.spare_min < 0 or not 1 <= args.max_pages <= 20:
        raise ValueError("Check --spare-percent/--spare-min (≥ 0) and --max-pages (1–20)")
    if not args.dry_run:
        if not args.output:
            raise ValueError("--output is required (or use --dry-run to check the columns first)")
        output = Path(args.output).resolve()
        if output.exists():
            raise ValueError(f"{output} already exists (reports are never overwritten); choose a new --output")
    args.cache = args.cache or str(CACHE_ROOT / "sourcing")
    args.offline, args.max_verify_lookups = not args.online, args.max_lookups
    overrides = parse_columns(args.column)
    parts = load_stock(args.partslist, overrides, "list", args.sheet, require_quantity=False)
    sources = [load_stock(path, overrides, label, args.sheet) for label, path in parse_stock_args(args.stock)]
    warnings = list(parts["warnings"]) + [w for s in sources for w in s["warnings"]]
    has_quantity = "quantity" in parts["columns"]
    if args.dry_run:
        print("\n".join(dry_run_lines(parts, sources, args, warnings)))
        return 0
    print(f"Parts list: {len(parts['entries'])} rows (header row {parts['header_row']}; "
          + ", ".join(f"{r}={h!r}" for r, h in parts["columns"].items()) + ")")

    records = DigiKeyRecords(args, args.online, warnings)
    rows = []
    for entry in parts["entries"]:
        per_board, quantity_note, quantity_known = per_board_quantity(entry, has_quantity, args.quantity)
        row = {"row": entry["row"], "references": entry["references"], "mpn": entry["mpn"],
               "manufacturer": entry["manufacturer"], "lcsc": entry["lcsc"], "description": entry["description"],
               "value": entry["value"], "package": entry["package"],
               "vendor_alternates": [dict(a) for a in entry["alternates"]],
               "need": required_quantity(per_board, args.boards, args.spare_percent, args.spare_min)}
        rows.append(row)
        skip, reason = fitting(entry, has_quantity)
        if skip:
            row.update(status=skip, note=reason)
            continue
        if quantity_note:
            row["quantity_note"] = quantity_note
        if not quantity_known:
            row["demand_unknown"] = True
        if not entry["mpn"]:
            row.update(status="no_mpn", note="row has only a C-code; exact identity needs an MPN")
            continue
        record = records.get(entry["mpn"])
        if "products" not in record:
            row.update(status="not_checked", note=record["note"])
            continue
        product, match, note = find_product(entry["mpn"], entry["manufacturer"], record["products"])
        row["_record"], row["note"] = record, note
        if product is not None:
            row["_product"], row["catalog"] = product, catalog_facts(product)
        row["match"] = match

    # Supplier availability: DigiKey from the records; LCSC/JLCPCB when keys are configured.
    found = [r for r in rows if r.get("_product")]
    observations = lcsc_observations([(r["catalog"]["manufacturer"], r["mpn"]) for r in found][:args.max_lookups],
                                     args.cache, args.online, warnings)
    for r in found:
        line = {"mpn": r["mpn"], "manufacturer": r["catalog"]["manufacturer"], "lcsc": [r["lcsc"]] if r["lcsc"] else [],
                "issues": []}
        if sources:
            r["stock"] = match_line(line, sources, r["need"], args.combine_sources)
        code = r["lcsc"] or next(iter((r.get("stock") or {}).get("stock_lcsc") or []), None) or next(
            (o["code"] for o in observations if o["supplier"] == "lcsc" and o.get("mpn") == r["mpn"]
             and re.fullmatch(r"C\d+", str(o.get("code", "")))), None)
        r["jlc_code"] = code
    mappings = [{"manufacturer": r["catalog"]["manufacturer"], "mpn": r["mpn"], "jlcpcb": r["jlc_code"]} for r in found if r["jlc_code"]]
    observations += jlc_observations(mappings[:args.max_lookups], args.cache, args.online, warnings)
    for r in rows:
        if sources and "stock" not in r and (r["mpn"] or r["lcsc"]) and r.get("status") not in ("not_fitted", "not_a_part"):
            line = {"mpn": r["mpn"], "manufacturer": r["manufacturer"], "lcsc": [r["lcsc"]] if r["lcsc"] else [], "issues": []}
            r["stock"] = match_line(line, sources, r["need"], args.combine_sources)
        if not r.get("_product"):
            r.setdefault("status", r.get("match", "not_checked"))
            continue
        policy = {"quantity": r["need"]["needed"], "min_stock": max(r["need"]["needed"], args.min_stock or 1),
                  "max_age_hours": args.max_age_hours}
        same = [{"product": p, "observed_at": r["_record"]["observed_at"]} for p in r["_record"]["products"]]
        report = stock_report(r["catalog"]["manufacturer"], r["mpn"], same, policy, imported_offers=observations)
        r["suppliers"] = {s: {"status": report["suppliers"][s]["status"], "stock": report["suppliers"][s]["max_reported_stock"],
                              "issues": sorted({i for o in report["suppliers"][s]["offers"] for i in o.get("issues", [])})}
                          for s in SUPPLIERS}
        r["status"] = row_status("found", r["catalog"], r["suppliers"])
        if r.get("demand_unknown") and r["status"] in ("available", "stock_issues"):
            r["status"] = "demand_unknown"  # stock cannot be judged against an unknown quantity

    # Vendor-listed alternates (the reference design's own second sources) come first.
    for r in rows:
        if not r["vendor_alternates"] or r["status"] in ("not_fitted", "not_a_part"):
            continue
        for alt in r["vendor_alternates"]:
            if sources:
                line = {"mpn": alt["mpn"], "manufacturer": alt["manufacturer"], "lcsc": [], "issues": []}
                stock = match_line(line, sources, r["need"], args.combine_sources)
                alt["stock"] = {"status": stock["status"], "quantity": stock["best_source_quantity"]}
            if r["status"] not in CHECK_ALTERNATES and not (r.get("stock") and r["stock"]["status"] != "in_stock"
                                                            and alt.get("stock", {}).get("status") == "in_stock"):
                continue
            record = records.get(alt["mpn"])
            if "products" not in record:
                alt.update(status="not_checked", note=record["note"])
                continue
            product, match, note = find_product(alt["mpn"], alt["manufacturer"], record["products"])
            if product is None:
                alt.update(status=match, note=note)
                continue
            facts = catalog_facts(product)
            policy = {"quantity": r["need"]["needed"], "min_stock": max(r["need"]["needed"], args.min_stock or 1),
                      "max_age_hours": args.max_age_hours}
            same = [{"product": p, "observed_at": record["observed_at"]} for p in record["products"]]
            report = stock_report(facts["manufacturer"], alt["mpn"], same, policy)
            digikey_summary = report["suppliers"]["digikey"]
            alt.update(manufacturer=facts["manufacturer"], lifecycle=facts["lifecycle"], aec_q200=facts["aec_q200"],
                       datasheet=facts["datasheet"], digikey={"status": digikey_summary["status"],
                                                              "stock": digikey_summary["max_reported_stock"]},
                       status=row_status("found", facts, {"digikey": digikey_summary}))
        usable = next((a for a in r["vendor_alternates"] if a.get("status") == "available"
                       or a.get("stock", {}).get("status") == "in_stock"), None)
        if usable and r["status"] in CHECK_ALTERNATES:
            r["resolution"] = {"by": "vendor_alternate", "mpn": usable["mpn"], "manufacturer": usable.get("manufacturer"),
                               "why": "in your stock" if usable.get("stock", {}).get("status") == "in_stock" else "available at DigiKey"}

    # Alternatives for obsolete / unstocked R/C/L rows: from stock first, then DigiKey.
    searches = 0
    for index, r in enumerate(rows, 1):
        if r["status"] not in PROBLEMS:
            continue
        if r.get("resolution"):
            r["alternatives_note"] = (f"vendor alternate {r['resolution']['mpn']} is {r['resolution']['why']}; "
                                      "own search skipped (the reference design's alternate wins)")
            continue
        spec = spec_from_product(r["_product"])
        if spec is None:
            r["alternatives_note"] = "automatic alternatives cover R/C/L with a readable value and package; review manually"
            continue
        r["requirements"] = requirement_summary(spec)
        tolerance = spec["tolerance_percent"]
        request = _request(spec, r["need"], tolerance if tolerance is not None else 100)
        declared = {"tolerance": tolerance is not None, "power": spec["power"] is not None or spec["class"] != "resistor",
                    "current": inductor_current_known(spec)}
        if sources:
            candidates = screen_stock_for_spec(spec, sources, r["need"], exclude=[r["mpn"]])
            for candidate in candidates:
                record = records.get(candidate["mpn"]) if candidate["mpn"] else {"note": "no MPN"}
                if "products" in record:
                    verify_candidate(candidate, record["products"], record["observed_at"], request, declared,
                                     r["catalog"]["manufacturer"])
                else:
                    candidate["digikey"] = {"status": "not_checked", "note": record["note"]}
            candidates.sort(key=lambda a: ({"match": 0, "possible": 1, "rejected": 2}.get(a["spec_status"], 1),
                                           not a["covers_need"], -(a["quantity"] or 0)))
            if candidates:
                r["stock_alternatives"] = candidates
        if not args.find_alternatives:
            continue
        if tolerance is None:
            r["market_alternatives"] = {"status": "skipped", "reason": "DigiKey states no tolerance for this part"}
            continue
        if not inductor_current_known(spec):
            r["market_alternatives"] = {"status": "skipped", "reason": "DigiKey lists no Isat/rated current for this inductor; "
                                                                       "a value/package-only search could suggest under-rated parts"}
            continue
        if searches >= args.max_alternative_searches:
            r["market_alternatives"] = {"status": "skipped", "reason": "search budget reached (--max-alternative-searches)"}
            continue
        searches += 1
        print(f"  alternatives: row {r['row']} {r['mpn']} ({request['kind']} {request['value']} {request['package']}) ...")
        line = {"class": spec["class"], "mpn": r["mpn"], "manufacturer": r["catalog"]["manufacturer"]}
        try:
            r["market_alternatives"] = market_alternatives(line, request, args, output / "alternatives" / f"row-{r['row']:04d}",
                                                           value=spec["value"])
        except (OSError, ValueError, KeyError, TypeError) as error:
            r["market_alternatives"] = {"status": "skipped", "reason": f"search failed ({type(error).__name__}: {error})"}
    against = None
    if args.against:
        from .inventory_cli import load_design
        circuit, design = load_design(args.against)
        against = {"design": design, **compare_design(rows, circuit, records)}
        print("Against your schematic: " + ", ".join(f"{AGAINST_TEXT.get(k, k)} {v}" for k, v in against["summary"].items())
              + (f" ({against['open']} open)" if against["open"] else ""))
    if records.lookups:
        print(f"  DigiKey exact lookups: {records.lookups}{'' if records.client else ' (cache only)'}")

    counts = {}
    for r in rows:
        counts[r["status"]] = counts.get(r["status"], 0) + 1
    report = {"schema_version": 1, "mode": "parts_list", "generated_at": utc_now(), "list": {k: v for k, v in parts.items() if k != "entries"},
              "boards": args.boards, "spare": {"percent": args.spare_percent, "minimum": args.spare_min},
              "stock_sources": [{k: v for k, v in s.items() if k != "entries"} | {"parts": len(s["entries"])} for s in sources],
              "online": args.online, "summary": counts, "resolved_by_vendor_alternate": sum(1 for r in rows if r.get("resolution")),
              "warnings": list(dict.fromkeys(warnings)),
              "rows": [{k: v for k, v in r.items() if not k.startswith("_")} for r in rows],
              "against": against,
              "scope": ("Exact manufacturer + MPN per row. DigiKey catalog data, timestamped stock snapshots; suppliers are "
                        "never summed. Alternatives are candidates for engineering review, not approvals.")}
    output.mkdir(parents=True, exist_ok=True)
    write_json(output / "parts-list.json", report)
    (output / "parts-list.md").write_text(markdown(report, _verdict), encoding="utf-8")
    write_csv(output / "parts-list.csv", report)
    print("Status: " + ", ".join(f"{STATUS_TEXT.get(k, k)} {v}" for k, v in counts.items()))
    print(f"Report: {output / 'parts-list.md'}")
    resolved = sum(1 for r in rows if r.get("resolution"))
    if resolved:
        print(f"Covered by the vendor's listed alternates: {resolved} row(s)")
    rows_ok = rows and all(r["status"] in ("available", "not_fitted", "not_a_part") for r in rows)
    return 0 if rows_ok and (against is None or against["open"] == 0) else 2


# --- reports -------------------------------------------------------------------------------

def _cell(value):
    return str(value if value is not None else "").replace("|", "\\|").replace("\n", " ")


def _stock_cell(r):
    stock = r.get("stock")
    if not stock:
        return ""
    best = max((s["quantity"] or 0 for s in stock["sources"] if s["counted_rows"]), default=0)
    return f"{best} ({stock['status'].replace('_', ' ')})"


def _supplier(r, name):
    s = (r.get("suppliers") or {}).get(name)
    if not s:
        return ""
    return f"{s['stock'] if s['stock'] is not None else '?'} ({s['status']})"


def markdown(report, verdict):
    lines = ["# Parts list check", "", f"List: `{report['list']['path']}` · boards {report['boards']} · spare "
             f"{report['spare']['percent']}% (min {report['spare']['minimum']})"
             + (f" · stock: {', '.join(s['source'] for s in report['stock_sources'])}" if report["stock_sources"] else ""), "",
             "| Status | Rows |", "| --- | --- |"]
    lines += [f"| {STATUS_TEXT.get(k, k)} | {v} |" for k, v in report["summary"].items()]
    if report["warnings"]:
        lines += ["", "## Warnings", ""] + [f"- {_cell(w)}" for w in report["warnings"]]
    with_stock = bool(report["stock_sources"])
    header = ["Row", "Refs", "MPN", "Manufacturer", "Status", "Lifecycle", "Need", "DigiKey", "LCSC", "JLCPCB"] + \
             (["Our stock"] if with_stock else []) + ["AEC-Q", "Notes"]
    lines += ["", "## Parts", "", "| " + " | ".join(header) + " |", "| --- " * len(header) + "|"]
    fitted_rows = [r for r in report["rows"] if r["status"] not in ("not_fitted", "not_a_part")]
    for r in fitted_rows:
        facts = r.get("catalog") or {}
        notes = [n for n in (r.get("note"), r.get("quantity_note")) if n]
        if r.get("resolution"):
            notes.insert(0, f"✔ vendor alternate {r['resolution']['mpn']} {r['resolution']['why']}")
        elif r.get("stock") and r["stock"]["status"] != "in_stock":
            stocked = [a["mpn"] for a in r.get("vendor_alternates", []) if a.get("stock", {}).get("status") == "in_stock"]
            if stocked:
                notes.insert(0, f"vendor alternate {', '.join(stocked)} is in your stock")
        issues = (r.get("suppliers") or {}).get("digikey", {}).get("issues") or []
        if r["status"] == "stock_issues" and issues:
            notes.append("; ".join(issues))
        if facts.get("datasheet"):
            notes.append(f"[datasheet]({facts['datasheet']})")
        cells = [r["row"], _refs(r.get("references")), r["mpn"] or "", facts.get("manufacturer") or r.get("manufacturer") or "",
                 STATUS_TEXT.get(r["status"], r["status"]), facts.get("lifecycle", ""), r["need"]["needed"],
                 _supplier(r, "digikey"), _supplier(r, "lcsc"), _supplier(r, "jlcpcb")]
        if with_stock:
            cells.append(_stock_cell(r))
        cells += ["yes" if facts.get("aec_q200") else "", "; ".join(notes)]
        lines.append("| " + " | ".join(_cell(c) for c in cells) + " |")
    other = [r for r in report["rows"] if r["status"] in ("not_found", "manufacturer_differs", "ambiguous")
             and r.get("vendor_alternates")]
    if other:
        lines += ["", "## Vendor alternates for parts that could not be identified", ""]
        for r in other:
            lines.append(f"**Row {r['row']} {r['mpn']}** ({STATUS_TEXT[r['status']]}):")
            lines += ["- " + _vendor_alternate(alt) for alt in r["vendor_alternates"]] + [""]
    problems = [r for r in report["rows"] if r["status"] in PROBLEMS]
    if problems:
        lines += ["", "## Problem parts and alternatives (engineering review required)", ""]
        for r in problems:
            facts = r.get("catalog") or {}
            lines.append(f"**Row {r['row']} {r['mpn']}** ({facts.get('manufacturer')}, {STATUS_TEXT[r['status']]}, "
                         f"{facts.get('lifecycle')})" + (f" — requires {r['requirements']}" if r.get("requirements") else "") + ":")
            for alt in r.get("vendor_alternates", []):
                lines.append("- " + _vendor_alternate(alt))
            if r.get("alternatives_note"):
                lines.append(f"- {r['alternatives_note']}")
            for alt in r.get("stock_alternatives", []):
                maker = f"{alt['manufacturer']} " if alt.get("manufacturer") else ""
                lines.append(f"- In stock: {maker}{alt['mpn'] or '?'} / {alt['lcsc'] or '?'} — {alt['source']}: {alt['quantity']} pcs"
                             f"{'' if alt['covers_need'] else ' (not enough alone)'} · **{alt['spec_status']}**" + verdict(alt))
            a = r.get("market_alternatives")
            if a and a.get("status") == "skipped":
                lines.append(f"- DigiKey search skipped: {a['reason']}")
            elif a:
                lines.append(f"- DigiKey: {a['status']}, {a['counted']}/{a['target']} counted, {a.get('other_manufacturers', '?')} "
                             f"other manufacturer(s) · [report]({a['report']})")
                lines += [f"  - {t['manufacturer']} {t['mpn']} — DigiKey {t['digikey_stock']}"
                          + (" · same manufacturer (not an independent source)" if t.get("same_manufacturer") else "")
                          for t in a["top"]]
            lines.append("")
    if report.get("against"):
        lines += against_markdown(report)
    skipped = [r for r in report["rows"] if r["status"] in ("not_fitted", "not_a_part")]
    if skipped:
        lines += ["", "## Not fitted / not a component (not looked up)", ""]
        lines += [f"- Row {r['row']} {_refs(r.get('references')) or ''} {r['mpn'] or ''}: {r['note']}".replace("  ", " ")
                  for r in skipped]
    lines += ["", report["scope"], ""]
    return "\n".join(lines)


def _refs(text, limit=6):
    tokens = [x for x in re.split(r"[,;\s]+", text or "") if x]
    return ", ".join(tokens[:limit]) + (f" +{len(tokens) - limit}" if len(tokens) > limit else "")


def _vendor_alternate(alt):
    maker = f"{alt['manufacturer']} " if alt.get("manufacturer") else ""
    text = f"Vendor alternate: {maker}{alt['mpn']}"
    if alt.get("status"):
        text += f" — {STATUS_TEXT.get(alt['status'], alt['status'])}"
        if alt.get("digikey"):
            text += f" (DigiKey {alt['digikey']['stock'] if alt['digikey']['stock'] is not None else '?'}, {alt.get('lifecycle')}"
            text += ", AEC-Q)" if alt.get("aec_q200") else ")"
        if alt.get("note"):
            text += f": {alt['note']}"
    if alt.get("stock"):
        text += f" · our stock: {alt['stock']['quantity']} ({alt['stock']['status'].replace('_', ' ')})"
    return text


def write_csv(path, report):
    with open(path, "w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(["Row", "References", "MPN", "Manufacturer (list)", "Manufacturer (DigiKey)", "Status", "Lifecycle",
                         "Needed", "DigiKey stock", "DigiKey PN", "LCSC", "JLCPCB", "Our stock", "AEC-Q", "Datasheet",
                         "Vendor alternates", "Best alternative"])
        for r in report["rows"]:
            facts = r.get("catalog") or {}
            alt = next(iter(a for a in r.get("stock_alternatives", []) if a["spec_status"] == "match"), None)
            top = ((r.get("market_alternatives") or {}).get("top") or [])
            best = (f"vendor alternate: {r['resolution']['mpn']}" if r.get("resolution") else
                    f"in stock: {alt['mpn']}" if alt else
                    next((f"DigiKey: {t['manufacturer']} {t['mpn']}" for t in top if not t.get("same_manufacturer")), ""))
            vendor = "; ".join(f"{a['mpn']} ({a.get('status', 'not checked')})" for a in r.get("vendor_alternates", []))
            writer.writerow([r["row"], r.get("references") or "", r["mpn"] or "", r.get("manufacturer") or "", facts.get("manufacturer") or "",
                             r["status"], facts.get("lifecycle", ""), r["need"]["needed"], _supplier(r, "digikey"),
                             " ".join(facts.get("digikey_pns") or []), _supplier(r, "lcsc"), _supplier(r, "jlcpcb"),
                             _stock_cell(r), "yes" if facts.get("aec_q200") else "", facts.get("datasheet") or "", vendor, best])
