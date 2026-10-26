"""Compare a design's BOM with stock inventories. Pure logic: no network, no prompts, no writes.

Identity rules (see SOURCING.md, "Inventory check"):
- MPN identifies a part (compared ignoring case, spaces and - # / . separators).
- An LCSC/JLCPCB C-code is a supplier listing. A C-code match whose stock row names a
  different MPN is a conflict and never counts.
- Rows of the same identity in one stock source are summed (one physical stock);
  different sources are never summed unless the caller explicitly combines them.
- R/C/L "alternatives" from stock are candidates for engineering review, never approvals.
"""

import math
import re
from decimal import Decimal
from fractions import Fraction

from ..core import compact, component_exemption, identity_candidates
from ..description import (IMPERIAL_CODES, RATING_FIELDS, component_class, description_quantity, field_by_keys,
                           footprint_size, normalize_description, percent_values, power_values, size_codes)
from ..units import dielectric, dielectric_in_text, format_quantity, schematic_quantity
from .providers import manufacturer_key

UNIT = {"resistor": "Ohm", "capacitor": "F", "inductor": "H"}
EXCLUDE_FIELDS = {"excludefrombom", "excludefromboard"}
NEEDS_PARTS = ("not_in_stock", "insufficient", "quantity_unknown", "unidentified")


# --- quantities ----------------------------------------------------------------------------

def required_quantity(per_board, boards, spare_percent, spare_min):
    """Net = per board x boards; spare = max(ceil(net x %), minimum), none when net is 0."""
    net = per_board * boards
    spare = 0 if net == 0 else max(math.ceil(Fraction(net) * Fraction(str(spare_percent)) / 100), spare_min)
    return {"per_board": per_board, "net": net, "spare": spare, "needed": net + spare}


# --- BOM lines -----------------------------------------------------------------------------

def _excluded(component):
    for name, value in component.get("fields", {}).items():
        if re.sub(r"[^a-z]", "", name.casefold()) in EXCLUDE_FIELDS and str(value).casefold() not in ("false", "no", "0"):
            return "Excluded from BOM/board in KiCad"
    return None


def _unique(component, role):
    values = list(dict.fromkeys(identity_candidates(component, role).values()))
    return values


def bom_lines(circuit):
    """Group populated components into purchasable lines. Returns (lines, excluded)."""
    lines, excluded = {}, []
    for ref, component in sorted(circuit["components"].items(), key=lambda item: _ref_key(item[0])):
        reason = component_exemption(ref, component)
        reason = reason["reason"] if reason else _excluded(component)
        if reason:
            excluded.append({"references": [ref], "reason": reason})
            continue
        mfr, mpn = _unique(component, "manufacturer"), _unique(component, "part_number")
        codes = [c.upper() for c in dict.fromkeys([*identity_candidates(component, "lcsc").values(),
                                                    *identity_candidates(component, "jlcpcb").values()])]
        valid_codes = [c for c in codes if re.fullmatch(r"C\d+", c)]
        cls = component_class(ref)
        issues = []
        if len(mfr) > 1 or len(mpn) > 1:
            issues.append("Conflicting manufacturer/MPN fields")
        if len(set(valid_codes)) > 1:
            issues.append(f"Several LCSC/JLCPCB codes: {', '.join(valid_codes)}")
        if mpn and re.fullmatch(r"C\d+", mpn[0], re.I):
            issues.append("MPN field holds a C-code; treated as an LCSC code")
            valid_codes = list(dict.fromkeys(valid_codes + [mpn[0].upper()]))
            mpn = []
        if len(mpn) == 1 and len(mfr) <= 1:
            key = ("mpn", compact(mpn[0]))
        elif not mpn and len(set(valid_codes)) == 1:
            key = ("lcsc", valid_codes[0])
        elif cls and not mpn and not valid_codes:
            # Generic R/C/L without identity: still useful for finding stock candidates.
            key = ("generic", cls, compact(component.get("value") or ""), (footprint_size(component.get("footprint")) or ("",))[0])
        else:
            key = ("ref", ref)
        line = lines.setdefault(key, {
            "key": "|".join(key), "references": [], "manufacturer": mfr[0] if len(mfr) == 1 else None,
            "mpn": mpn[0] if len(mpn) == 1 else None, "lcsc": [], "class": cls,
            "value": component.get("value") or "", "footprint": component.get("footprint") or "",
            "description": next(iter(identity_candidates_desc(component)), ""), "issues": [], "_component": component})
        line["references"].append(ref)
        for code in valid_codes:
            if code not in line["lcsc"]:
                line["lcsc"].append(code)
        if len(line["references"]) > 1 and (component.get("value") or "") != line["value"]:
            issues.append(f"{ref} Value {component.get('value')!r} differs from {line['references'][0]} ({line['value']!r}) for the same part")
        for issue in issues:
            if issue not in line["issues"]:
                line["issues"].append(issue)
        if mfr and line["manufacturer"] and mfr[0] != line["manufacturer"]:
            line["issues"].append(f"{ref} manufacturer {mfr[0]!r} differs from {line['manufacturer']!r}")
    for line in lines.values():
        if len(line["lcsc"]) > 1 and not any(i.startswith("Several LCSC") for i in line["issues"]):
            line["issues"].append(f"References use different C-codes: {', '.join(line['lcsc'])}")
        kind = line["key"].split("|")[0]
        line["identity"] = {"mpn": "manufacturer part number", "lcsc": "C-code only", "generic": "none (generic R/C/L)",
                            "ref": "none"}[kind]
    return list(lines.values()), excluded


def identity_candidates_desc(component):
    for name, value in component.get("fields", {}).items():
        if re.sub(r"[^a-z]", "", name.casefold()) in ("description", "desc") and isinstance(value, str) and value.strip():
            yield value.strip()


def _ref_key(ref):
    match = re.fullmatch(r"([A-Za-z#_]*)(\d*)(.*)", ref)
    return (match.group(1), int(match.group(2)) if match.group(2) else -1, match.group(3)) if match else (ref, 0, "")


def select_lines(lines, excluded, selectors):
    """Resolve user selectors (references such as R27, or MPNs; comma-separated, case-insensitive)
    to BOM lines in BOM order. Returns (lines, problems); a problem stops the run before any API call."""
    wanted = [s.strip() for item in selectors for s in str(item).split(",") if s.strip()]
    chosen, problems = set(), []
    excluded_refs = {r.casefold(): e["reason"] for e in excluded for r in e["references"]}
    for selector in wanted:
        key = selector.casefold()
        hits = [l for l in lines if key in (r.casefold() for r in l["references"])
                or (l["mpn"] and compact(l["mpn"]) == compact(selector))]
        if hits:
            chosen.update(id(l) for l in hits)
        elif key in excluded_refs:
            problems.append(f"{selector} is not in the BOM ({excluded_refs[key]})")
        else:
            problems.append(f"{selector} matches no reference or MPN in the design")
    if not wanted:
        problems.append("--alternatives-for needs at least one reference or MPN")
    return [l for l in lines if id(l) in chosen], problems


# --- stock matching ------------------------------------------------------------------------

def match_entry(line, entry):
    """How one stock row relates to a BOM line: None (unrelated) or {match, counts, notes}."""
    line_mpn = compact(line["mpn"]) if line["mpn"] else None
    row_mpn = compact(entry["mpn"]) if entry["mpn"] else None
    mpn_equal = bool(line_mpn and row_mpn and line_mpn == row_mpn)
    code_equal = bool(entry["lcsc"] and entry["lcsc"] in line["lcsc"])
    if not mpn_equal and not code_equal:
        return None
    notes = []
    if mpn_equal:
        if entry["lcsc"] and line["lcsc"] and not code_equal:
            match = "mpn_lcsc_differs"
            notes.append(f"Same MPN, but stock is C-code {entry['lcsc']} and the schematic says {', '.join(line['lcsc'])}. "
                         f"JLCPCB assembly consumes stock by C-code: set the LCSC field to {entry['lcsc']} to use it")
        else:
            match = "exact"
    elif row_mpn and line_mpn:
        return {"match": "conflict", "counts": False,
                "notes": [f"C-code {entry['lcsc']} in stock is MPN {entry['mpn']}, but the schematic MPN is {line['mpn']}; "
                          "not counted. Fix the schematic C-code or MPN"]}
    else:
        match = "lcsc_only"
        if not line_mpn:
            notes.append("Matched by C-code only (schematic has no MPN)")
        else:
            notes.append("Matched by C-code only (stock row has no MPN)")
    if line["manufacturer"] and entry["manufacturer"] and manufacturer_key(line["manufacturer"]) != manufacturer_key(entry["manufacturer"]):
        notes.append(f"Manufacturer differs: schematic {line['manufacturer']!r}, stock {entry['manufacturer']!r}; confirm")
    return {"match": match, "counts": True, "notes": notes}


def match_line(line, sources, need, combine=False):
    """Stock status for one BOM line across all sources."""
    per_source, notes, conflicts = [], [], []
    for source in sources:
        rows, total, unknown = [], 0, False
        for entry in source["entries"]:
            result = match_entry(line, entry)
            if result is None:
                continue
            rows.append({"row": entry["row"], "mpn": entry["mpn"], "lcsc": entry["lcsc"], "manufacturer": entry["manufacturer"],
                         "quantity": entry["quantity"], "pools": entry["pools"], **result})
            notes.extend(n for n in result["notes"] if n not in notes)
            if not result["counts"]:
                conflicts.append(entry)
                continue
            if entry["quantity"] is None:
                unknown = True
            else:
                total += entry["quantity"]
        if rows:
            per_source.append({"source": source["source"], "quantity": None if unknown else total, "rows": rows,
                               "counted_rows": sum(r["counts"] for r in rows)})
    counted = [s for s in per_source if s["counted_rows"]]
    known = [s["quantity"] for s in counted if s["quantity"] is not None]
    best = max(known, default=0)
    combined = sum(known)
    needed = need["needed"]
    if not counted:
        status = "not_in_stock"
    elif best >= needed or (combine and combined >= needed):
        status = "in_stock"
    elif any(s["quantity"] is None for s in counted):
        status = "quantity_unknown"
    elif combined >= needed:
        status = "split_across_sources"
        notes.append(f"No single source has {needed}; sources together have {combined}. Use --combine-sources if they can be pooled")
    else:
        status = "insufficient"
    available = combined if combine else best
    stock_codes = sorted({r["lcsc"] for s in counted for r in s["rows"] if r["counts"] and r["lcsc"]} - set(line["lcsc"]))
    return {"status": status, "stock_lcsc": stock_codes, "sources": per_source, "best_source_quantity": best, "all_sources_quantity": combined,
            "shortfall": max(needed - available, 0) if status != "in_stock" else 0, "notes": notes,
            "review": bool(notes) or bool(line["issues"])}


# --- R/C/L specifications ------------------------------------------------------------------

def _single(values):
    values = set(values)
    return values.pop() if len(values) == 1 else None


def _quantity(text, unit):
    value, error = schematic_quantity(normalize_description(text or ""), unit)
    return None if error else value


def schematic_spec(line):
    """What the schematic declares for an R/C/L line. Missing items stay None (unknown)."""
    cls, component = line["class"], line["_component"]
    if cls not in UNIT:
        return None
    text = line["description"]
    spec = {"class": cls, "value": None, "package": None, "tolerance_percent": None, "voltage": None, "power": None,
            "dielectric": None, "automotive": "aec-q200" in (text + " ".join(map(str, component.get("fields", {}).values()))).casefold()}
    value, error = schematic_quantity(line["value"], UNIT[cls])
    spec["value"] = None if error else value
    size = footprint_size(line["footprint"])
    if size:
        spec["package"] = size[0]
    else:
        imperial, _ = size_codes(text)
        spec["package"] = _single(imperial)

    def field(rating):
        try:
            return field_by_keys(component, RATING_FIELDS[rating])[1]
        except ValueError:
            return None

    tolerance = field("tolerance")
    spec["tolerance_percent"] = _single(percent_values(tolerance)) if tolerance else _single(percent_values(text))
    voltage = field("voltage")
    spec["voltage"] = _quantity(voltage, "V") if voltage else _quantity(text, "V")
    power = field("power")
    spec["power"] = _single(power_values(power)) if power else _single(power_values(text))
    if cls == "capacitor":
        declared = field("dielectric")
        spec["dielectric"] = dielectric(declared) if declared else dielectric_in_text(text)
    # What the schematic leaves out comes from the original part's DigiKey record (no downgrade
    # versus the part actually designed in). Declared schematic values always win.
    spec["provenance"] = {k: "schematic" for k in ("tolerance_percent", "voltage", "power", "dielectric")
                          if spec[k] is not None}
    if spec["automotive"]:
        spec["provenance"]["automotive"] = "schematic"
    baseline = line.get("_baseline")
    if baseline:
        for key in ("tolerance_percent", "voltage", "power", "dielectric"):
            if spec[key] is None and baseline.get(key) is not None and (key != "dielectric" or cls == "capacitor") \
                    and (key != "power" or cls == "resistor"):
                spec[key] = baseline[key]
                spec["provenance"][key] = "original part (DigiKey)"
        if not spec["automotive"] and baseline.get("automotive"):
            spec["automotive"] = True
            spec["provenance"]["automotive"] = "original part (DigiKey)"
    return spec


def baseline_from_digikey(line, products, observed_at):
    """Ratings of the line's own MPN from DigiKey: (private Decimal dict, public report dict).
    Not used when the record's value contradicts the schematic Value (the MPN may be wrong)."""
    from .. import digikey
    from .requirements import FIELDS
    if line["class"] not in UNIT or not line["mpn"]:
        return None, None
    key = digikey.mpn_key(line["mpn"])
    same = [p for p in products if digikey.mpn_key(p.get("ManufacturerProductNumber") or "") == key]
    if line["manufacturer"]:
        same = [p for p in same if manufacturer_key((p.get("Manufacturer") or {}).get("Name")) == manufacturer_key(line["manufacturer"])]
    if len({manufacturer_key((p.get("Manufacturer") or {}).get("Name")) for p in same}) != 1:
        note = "original MPN not found on DigiKey" if not same else "original MPN listed under several manufacturers"
        return None, {"status": "unavailable", "note": note}
    product = same[0]
    get = lambda name: digikey.parameter(product, name)
    value, _ = schematic_quantity(normalize_description(get(FIELDS[line["class"]]) or ""), UNIT[line["class"]])
    schematic_value, _ = schematic_quantity(line["value"], UNIT[line["class"]])
    if value is not None and schematic_value is not None and value != schematic_value:
        return None, {"status": "conflict", "note": f"DigiKey lists {line['mpn']} as {get(FIELDS[line['class']])}, "
                                                    f"schematic Value is {line['value']!r}; baseline not used"}
    tolerances = percent_values(get("Tolerance") or "")
    powers = power_values(get("Power (Watts)") or "")
    qualification = " ".join(v for v in (get("Ratings"), get("Features"), get("Qualification")) if v)
    private = {"tolerance_percent": max(tolerances) if tolerances else None,
               "voltage": _quantity(get("Voltage - Rated"), "V"),
               "power": min(powers) if powers else None,
               "dielectric": dielectric(get("Temperature Coefficient") or ""),
               "automotive": bool(re.search(r"AEC-?Q200", qualification, re.I))}
    public = {"status": "ok", "source": "DigiKey", "manufacturer": (product.get("Manufacturer") or {}).get("Name"),
              "url": product.get("ProductUrl"), "observed_at": observed_at,
              "ratings": {k: (_plain(v) if isinstance(v, Decimal) else v) for k, v in private.items()}}
    return private, public


def _plain(number):
    """Decimal without exponent: 10 -> '10', 0.0625 -> '0.0625' (str(normalize()) gives '1E+1')."""
    text = format(number.normalize(), "f")
    return text.rstrip("0").rstrip(".") if "." in text else text


def requirement_summary(spec):
    """'1% · ≥63 mW · AEC-Q200 (from original part: power, AEC-Q200)' for reports."""
    if not spec:
        return ""
    parts = []
    if spec["tolerance_percent"] is not None:
        parts.append(f"≤{_plain(spec['tolerance_percent'])}%")
    if spec["voltage"] is not None:
        parts.append(f"≥{format_quantity(spec['voltage'], 'V')}")
    if spec["power"] is not None:
        parts.append(f"≥{format_quantity(spec['power'], 'W')}")
    if spec["dielectric"]:
        parts.append(spec["dielectric"])
    if spec["automotive"]:
        parts.append("AEC-Q200")
    inherited = [k.replace("_percent", "").replace("automotive", "AEC-Q200") for k, v in spec["provenance"].items()
                 if v != "schematic"]
    return " · ".join(parts) + (f" (from original part on DigiKey: {', '.join(inherited)})" if inherited else "")


def stock_spec(entry):
    """Parse a stock row's Value/Footprint/Description into the same shape (R/C/L only)."""
    words = re.findall(r"[a-z]+", f"{entry['category']} {entry['description']}".casefold())
    if {"array", "network", "bead", "ferrite", "thermistor", "thermistors", "varistor", "potentiometer"} & set(words):
        return None
    kinds = {cls for cls in UNIT for w in words if w.startswith(cls)}
    if len(kinds) != 1:
        return None
    cls = kinds.pop()
    text = entry["description"]
    value = None
    if entry["value"]:
        value, error = schematic_quantity(normalize_description(entry["value"]), UNIT[cls])
        value = None if error else value
    if value is None:
        value, error = description_quantity(text, UNIT[cls])
        value = None if error else value
    package = entry["package"].strip() if entry["package"].strip() in IMPERIAL_CODES else _single(size_codes(text)[0])
    return {"class": cls, "value": value, "package": package, "tolerance_percent": _single(percent_values(text)),
            "voltage": _quantity(text, "V"), "power": _single(power_values(text)),
            "dielectric": dielectric_in_text(text) if cls == "capacitor" else None,
            "automotive": "aec-q200" in text.casefold()}


LOW_TEMP = {"X": -55, "Y": -30, "Z": 10}
HIGH_TEMP = {"2": 45, "4": 65, "5": 85, "6": 105, "7": 125, "8": 150, "9": 200}
DELTA = {"A": 1.0, "B": 1.5, "C": 2.2, "D": 3.3, "E": 4.7, "F": 7.5, "P": 10, "R": 15, "S": 22, "L": 40, "T": 33,
         "U": 56, "V": 82}


def dielectric_ok(required, candidate):
    """True/False/None. C0G replaces any class II code; class II needs an equal-or-wider
    temperature range and equal-or-smaller capacitance change."""
    if required is None or candidate is None:
        return None
    if required == candidate or candidate == "C0G":
        return True
    if required == "C0G":
        return False
    try:
        r = (LOW_TEMP[required[0]], HIGH_TEMP[required[1]], DELTA[required[2]])
        c = (LOW_TEMP[candidate[0]], HIGH_TEMP[candidate[1]], DELTA[candidate[2]])
    except (KeyError, IndexError):
        return None
    return c[0] <= r[0] and c[1] >= r[1] and c[2] <= r[2]


def _show(value, unit):
    return format_quantity(value, unit) if isinstance(value, Decimal) else str(value)


def compare_specs(required, candidate):
    """Returns (status, checks): status 'match', 'possible' (some item unknown) or 'mismatch'."""
    checks = []

    def check(name, ok, required_text, candidate_text):
        checks.append({"check": name, "status": "ok" if ok else "unknown" if ok is None else "mismatch",
                       "required": required_text, "candidate": candidate_text})

    unit = UNIT[required["class"]]
    check("type", required["class"] == candidate["class"], required["class"], candidate["class"])
    for name in ("value", "package"):
        r, c = required[name], candidate[name]
        check(name, None if r is None or c is None else r == c,
              _show(r, unit) if name == "value" else r, _show(c, unit) if name == "value" else c)
    r, c = required["tolerance_percent"], candidate["tolerance_percent"]
    check("tolerance", None if r is None or c is None else c <= r, f"≤{r}%" if r is not None else "not declared",
          f"{c}%" if c is not None else "unknown")
    for name, unit_text in (("voltage", "V"), ("power", "W")):
        r, c = required[name], candidate[name]
        if r is None and name == "power" and required["class"] != "resistor":
            continue
        if r is None and name == "voltage" and required["class"] == "resistor":
            continue
        check(name, None if r is None or c is None else c >= r, f"≥{_show(r, unit_text)}" if r is not None else "not declared",
              _show(c, unit_text) if c is not None else "unknown")
    if required["class"] == "capacitor":
        check("dielectric", dielectric_ok(required["dielectric"], candidate["dielectric"]),
              required["dielectric"] or "not declared", candidate["dielectric"] or "unknown")
    if required["automotive"]:
        check("AEC-Q200", True if candidate["automotive"] else None, "required", "stated" if candidate["automotive"] else "not stated")
    statuses = {c["status"] for c in checks}
    status = "mismatch" if "mismatch" in statuses else "possible" if "unknown" in statuses else "match"
    return status, checks


def stock_alternatives(line, sources, need, limit=5):
    """Other stock rows that may replace an R/C/L line. Never includes the line's own identity."""
    required = schematic_spec(line)
    if not required:
        return None, "Automatic alternatives cover R/C/L only"
    if required["value"] is None or required["package"] is None:
        return None, "Value or package not readable from the schematic; cannot screen stock"
    found = []
    for source in sources:
        for entry in source["entries"]:
            if match_entry(line, entry):
                continue
            spec = stock_spec(entry)
            if not spec or spec["class"] != required["class"]:
                continue
            status, checks = compare_specs(required, spec)
            if status == "mismatch":
                continue
            quantity = entry["quantity"]
            found.append({"source": source["source"], "row": entry["row"], "mpn": entry["mpn"], "lcsc": entry["lcsc"],
                          "manufacturer": entry["manufacturer"], "description": entry["description"],
                          "quantity": quantity, "covers_need": quantity is not None and quantity >= need["needed"],
                          "spec_status": status, "checks": checks, "engineering_status": "requires_review"})
    found.sort(key=lambda r: (r["spec_status"] != "match", not r["covers_need"], -(r["quantity"] or 0)))
    return found[:limit], None


def catalog_values(cls, value):
    """Value spellings that match DigiKey's keyword index, best first. DigiKey writes
    "10k"/"620" for resistors (not "10kOhm"), "0.1 µF"/"2200 pF" for capacitors (not "100nF"),
    "10 µH" for inductors. Every spelling parses to the same exact value for screening."""
    def plain(number):
        text = format(number.normalize(), "f")
        return text.rstrip("0").rstrip(".") if "." in text else text

    if cls == "resistor":
        for exponent, prefix in ((6, "M"), (3, "k"), (0, ""), (-3, "m")):
            if value >= Decimal(1).scaleb(exponent) or exponent == -3:
                primary = plain(value.scaleb(-exponent)) + prefix
                break
        return [primary, primary + " Ohm"]
    if cls == "capacitor":
        pf, nf, uf = (plain(value.scaleb(e)) for e in (12, 9, 6))
        # DigiKey: below 10 nF in pF ("2200 pF"), from 10 nF up in µF ("0.01 µF", "0.1 µF");
        # 10 nF–1 µF also appear in pF ("10000PF") in some listings.
        if value < Decimal("1E-9"):
            return [pf + "pF"]
        if value < Decimal("1E-8"):
            return [pf + "pF", nf + "nF"]
        if value < Decimal("1E-6"):
            return [uf + "uF", pf + "pF"]
        return [uf + "uF"]
    if cls == "inductor":
        if value < Decimal("1E-6"):
            return [plain(value.scaleb(9)) + "nH", plain(value.scaleb(6)) + "uH"]
        if value < Decimal("1E-3"):
            return [plain(value.scaleb(6)) + "uH"]
        return [plain(value.scaleb(3)) + "mH", plain(value.scaleb(6)) + "uH"]
    return [format_quantity(value, UNIT[cls]).replace(" ", "")]


def _request(spec, need, tolerance):
    request = {"schema_version": 1, "kind": spec["class"], "value": catalog_values(spec["class"], spec["value"])[0],
               "package": spec["package"], "tolerance_percent": float(tolerance),
               "automotive": spec["automotive"], "quantity": need["needed"], "min_stock": max(need["needed"], 1),
               "min_alternatives": 3, "constraints": []}
    if request["tolerance_percent"].is_integer():
        request["tolerance_percent"] = int(request["tolerance_percent"])
    if spec["class"] == "capacitor" and spec["dielectric"]:
        request["dielectric"] = spec["dielectric"]
    if spec["voltage"] is not None:
        request["constraints"].append({"parameter": "Voltage - Rated", "op": "min", "value": _plain(spec["voltage"]), "unit": "V"})
    if spec["power"] is not None and spec["class"] == "resistor":
        request["constraints"].append({"parameter": "Power (Watts)", "op": "min", "value": _plain(spec["power"]), "unit": "W"})
    return request


def draft_request(line, need):
    """parts-search request for DigiKey alternatives, from declared schematic data only.
    Returns (request, None) or (None, reason). Never invents a tolerance or rating."""
    spec = schematic_spec(line)
    if not spec:
        return None, "Automatic market alternatives cover R/C/L only (scope); write a request JSON for other parts (SOURCING.md)"
    if spec["class"] == "resistor" and spec["value"] == 0:
        return None, ("0 Ω jumper: tolerance does not apply and catalog jumpers state a maximum resistance/current instead; "
                      "write a request JSON with those constraints")
    missing = [n for n in ("value", "package", "tolerance_percent") if spec[n] is None]
    if missing:
        return None, f"Schematic does not declare {', '.join(missing)}; add the field(s) or write a request JSON"
    return _request(spec, need, spec["tolerance_percent"]), None


# --- DigiKey verification of in-stock candidates --------------------------------------------
# Stock files (JLC/LCSC descriptions) rarely state qualification or full ratings. The exact
# DigiKey record of a candidate's MPN is screened with the same rules as parts-search.

def verification_request(line, need):
    """(request, tolerance_declared) for screening a candidate's DigiKey record, or (None, reason).
    An undeclared tolerance is a placeholder limit of 100 % and is reported as not compared."""
    spec = schematic_spec(line)
    if not spec or spec["value"] is None or spec["package"] is None:
        return None, "value or package not readable"
    declared = {"tolerance": spec["tolerance_percent"] is not None,
                "power": spec["power"] is not None or spec["class"] != "resistor"}
    return _request(spec, need, spec["tolerance_percent"] if declared["tolerance"] else 100), declared


def verify_candidate(candidate, products, observed_at, request, tolerance_declared, original_manufacturer=None):
    """Attach a DigiKey verdict to an in-stock candidate (mutates and returns it)."""
    from .. import digikey
    from .requirements import screen
    mpn = digikey.mpn_key(candidate["mpn"] or "")
    same = [p for p in products if digikey.mpn_key(p.get("ManufacturerProductNumber") or "") == mpn]
    if candidate.get("manufacturer"):
        named = [p for p in same if manufacturer_key((p.get("Manufacturer") or {}).get("Name")) == manufacturer_key(candidate["manufacturer"])]
        same = named or same
    makers = {manufacturer_key((p.get("Manufacturer") or {}).get("Name")) for p in same}
    if not same:
        candidate["digikey"] = {"status": "not_found", "note": "exact MPN not listed by DigiKey"}
        return candidate
    if len(makers) > 1:
        candidate["digikey"] = {"status": "ambiguous", "note": "MPN listed under several manufacturers; add a manufacturer column"}
        return candidate
    product = same[0]
    from .. import digikey as dk
    if isinstance(tolerance_declared, bool):
        tolerance_declared = {"tolerance": tolerance_declared, "power": True}
    status, checks = screen(product, request)
    for check in checks:
        if check["parameter"] == "tolerance_percent" and not tolerance_declared["tolerance"]:
            check["status"], check["note"] = "unknown", "tolerance not declared in the schematic; not compared"
        if check["parameter"] == "automotive_qualification" and check["status"] == "unknown":
            check["note"] = "AEC-Q200 not listed by DigiKey"
    if not tolerance_declared["power"]:
        checks.append({"parameter": "Power (Watts)", "status": "unknown", "actual": dk.parameter(product, "Power (Watts)"),
                       "expected": None, "note": "power not declared in the schematic; not compared"})
    if any(c["status"] == "unknown" for c in checks) and status == "catalog_match":
        status = "needs_review"
    lifecycle, flags = digikey.lifecycle(product)
    manufacturer = (product.get("Manufacturer") or {}).get("Name")
    qualification = ", ".join(v for v in (digikey.parameter(product, x) for x in ("Ratings", "Features", "Qualification"))
                              if v and v.strip() != "-")
    candidate["manufacturer"] = candidate.get("manufacturer") or manufacturer
    candidate["digikey"] = {"status": status, "manufacturer": manufacturer, "lifecycle": lifecycle, "flags": flags,
                            "qualification": qualification or None, "url": product.get("ProductUrl"),
                            "observed_at": observed_at,
                            "issues": [{"parameter": c["parameter"], "status": c["status"], "actual": c["actual"],
                                        "note": c.get("note")} for c in checks if c["status"] != "match"]}
    if original_manufacturer:
        candidate["same_manufacturer"] = manufacturer_key(manufacturer) == manufacturer_key(original_manufacturer)
    active = str(lifecycle).casefold() == "active" and not flags
    candidate["spec_status"] = ("rejected" if status == "rejected" else
                                "match" if status == "catalog_match" and active else "possible")
    candidate["spec_source"] = "DigiKey catalog"
    return candidate
