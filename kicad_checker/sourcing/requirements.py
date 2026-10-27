"""Strict, deterministic catalog screening. These are selection criteria, not circuit approval."""

import re
from decimal import Decimal

from .. import digikey
from ..description import percent_values, power_values, size_codes
from ..units import convert, dielectric, parse_unit, schematic_quantity, to_decimal

UNITS = {"resistor": "Ohm", "capacitor": "F", "inductor": "H"}
FIELDS = {"resistor": "Resistance", "capacitor": "Capacitance", "inductor": "Inductance"}
# Deliberately narrow: resistor networks, beads and capacitor arrays are not single passives.
CATEGORIES = {"resistor": {"Chip Resistor - Surface Mount", "Through Hole Resistors"},
              "capacitor": {"Ceramic Capacitors", "Aluminum Electrolytic Capacitors", "Tantalum Capacitors", "Film Capacitors"},
              "inductor": {"Fixed Inductors"}}
REQUEST_KEYS = {"schema_version", "kind", "value", "package", "tolerance_percent", "automotive", "dielectric",
                "constraints", "quantity", "min_stock", "min_alternatives", "normally_stocking", "max_age_hours",
                "exclude_mpns", "keywords", "supplier_codes", "category", "qualification",
                "supplier_device_package", "min_manufacturers"}


def positive_int(value, name):
    if type(value) is not int or value < 1:
        raise ValueError(f"{name} must be a positive integer")
    return value


def stock_policy(data):
    allowed = {"quantity", "min_stock", "max_age_hours", "normally_stocking"}
    if not isinstance(data, dict) or set(data) - allowed:
        raise ValueError("Unknown stock policy field")
    req = {"quantity": 100, "min_stock": 1000, "max_age_hours": 24, "normally_stocking": True, **data}
    for key in ("quantity", "min_stock"):
        positive_int(req[key], key)
    age = to_decimal(req["max_age_hours"])
    if age is None or age <= 0 or type(req["normally_stocking"]) is not bool:
        raise ValueError("Stock policy needs positive max_age_hours and boolean normally_stocking")
    return req


def validate_request(data):
    if not isinstance(data, dict) or set(data) - REQUEST_KEYS:
        raise ValueError("Unknown sourcing request fields (check spelling); see SOURCING.md")
    req = {"schema_version": 1, "quantity": 100, "min_stock": 1000, "min_alternatives": 3,
           "normally_stocking": True, "max_age_hours": 24, "automotive": False,
           "constraints": [], "exclude_mpns": [], "supplier_codes": [], **data}
    if type(req["schema_version"]) is not int or req["schema_version"] != 1:
        raise ValueError("Unsupported sourcing request schema_version")
    if not isinstance(req.get("kind"), str) or not req["kind"].strip():
        raise ValueError("kind must be a nonempty component label, e.g. resistor, mosfet, connector or component")
    req["kind"] = req["kind"].strip().casefold()
    if req["kind"] in (*UNITS, "diode") and "package" not in req:
        raise ValueError("An explicit package is required; numeric chip sizes use imperial codes")
    if "package" in req:
        if not isinstance(req["package"], str) or not req["package"].strip():
            raise ValueError("package must be nonempty text")
        req["package"] = req["package"].strip()
    if "category" in req:
        category = req["category"]
        if not ((type(category) is int and category > 0) or (isinstance(category, str) and category.strip())):
            raise ValueError("category must be an exact DigiKey category name or positive numeric CategoryId")
    if "qualification" in req and (not isinstance(req["qualification"], str) or not req["qualification"].strip()):
        raise ValueError("qualification must be nonempty text, e.g. AEC-Q100")
    for key in ("quantity", "min_stock", "min_alternatives"):
        positive_int(req[key], key)
    # Supply diversity: by default at least two manufacturers (or one if only one alternative is wanted).
    req.setdefault("min_manufacturers", min(2, req["min_alternatives"]))
    positive_int(req["min_manufacturers"], "min_manufacturers")
    if req["min_manufacturers"] > req["min_alternatives"]:
        raise ValueError("min_manufacturers cannot exceed min_alternatives")
    if "supplier_device_package" in req:
        if not isinstance(req["supplier_device_package"], str) or not req["supplier_device_package"].strip():
            raise ValueError("supplier_device_package must be nonempty text, e.g. 6-WSON (2x2)")
        req["supplier_device_package"] = req["supplier_device_package"].strip()
    age = to_decimal(req["max_age_hours"])
    if age is None or age <= 0:
        raise ValueError("max_age_hours must be positive")
    for key in ("automotive", "normally_stocking"):
        if type(req[key]) is not bool:
            raise ValueError(f"{key} must be true or false")
    if req["automotive"] and req["kind"] not in (*UNITS, "diode") and "qualification" not in req:
        raise ValueError("Specify qualification for automotive components outside RLC/diodes; no AEC standard is assumed")
    if req["kind"] in UNITS:
        val, error = schematic_quantity(req.get("value"), UNITS[req["kind"]])
        if error or val < 0 or (val == 0 and req["kind"] != "resistor"):
            raise ValueError("value must be an unambiguous positive R/C/L value (zero-ohm resistors are allowed)")
        tol = to_decimal(req.get("tolerance_percent"))
        if tol is None or not 0 <= tol <= 100:
            raise ValueError("R/C/L requests require tolerance_percent (maximum allowed, 0–100)")
    elif "value" in req or "tolerance_percent" in req:
        raise ValueError("value/tolerance_percent shorthand is RLC-only; use named constraints for other components")
    if "dielectric" in req and (req["kind"] != "capacitor" or not dielectric(req["dielectric"])):
        raise ValueError("dielectric must be a capacitor EIA code such as X7R or C0G")
    if "keywords" in req and (not isinstance(req["keywords"], str) or not req["keywords"].strip()):
        raise ValueError("keywords must be nonempty text")
    if not isinstance(req["exclude_mpns"], list) or any(not isinstance(x, str) or not x.strip() for x in req["exclude_mpns"]):
        raise ValueError("exclude_mpns must be a list of exact MPNs")
    if not isinstance(req["constraints"], list):
        raise ValueError("constraints must be a list")
    for c in req["constraints"]:
        validate_constraint(c)
    if not isinstance(req["supplier_codes"], list):
        raise ValueError("supplier_codes must be a list")
    for item in req["supplier_codes"]:
        if (not isinstance(item, dict) or set(item) != {"manufacturer", "mpn", "jlcpcb"}
                or any(not isinstance(v, str) or not v.strip() for v in item.values())
                or not re.fullmatch(r"C\d+", item["jlcpcb"])):
            raise ValueError("supplier_codes entries need manufacturer, mpn and jlcpcb (C-number)")
    return req


def _unit_ok(unit):
    return isinstance(unit, str) and (unit == "" or parse_unit(unit) is not None)


def validate_constraint(c):
    """parameter + op (eq/min/max/covers) + value [+ unit, at, qualifier]; see SOURCING.md."""
    if (not isinstance(c, dict) or set(c) - {"parameter", "op", "value", "unit", "at", "qualifier"}
            or not isinstance(c.get("parameter"), str) or not c["parameter"].strip()
            or c.get("op") not in ("eq", "min", "max", "covers", "in") or "value" not in c):
        raise ValueError("Each constraint needs parameter, op (eq/min/max/covers/in), value and optional unit/at/qualifier")
    if c["op"] == "in":
        # One of several catalog texts, e.g. Shielding in ["Shielded", "Semi-Shielded"].
        if ("unit" in c or {"at", "qualifier"} & set(c) or not isinstance(c["value"], list) or not c["value"]
                or any(not isinstance(v, str) or not v.strip() for v in c["value"])):
            raise ValueError("'in' needs a list of catalog texts, without unit/at/qualifier")
        return
    if "unit" not in c:
        if c["op"] != "eq" or not isinstance(c["value"], str) or not c["value"].strip() or {"at", "qualifier"} & set(c):
            raise ValueError("Text constraints support eq (or in) only, without at/qualifier")
        return
    if not _unit_ok(c["unit"]):
        raise ValueError(f"Unsupported unit in constraint {c['parameter']!r}")
    if c["op"] == "covers":
        value = c["value"]
        if (not isinstance(value, list) or len(value) != 2 or any(to_decimal(v) is None for v in value)
                or to_decimal(value[0]) > to_decimal(value[1])):
            raise ValueError("covers needs value [low, high], e.g. [-40, 125] with unit °C")
    elif to_decimal(c["value"]) is None:
        raise ValueError("Numeric constraints need a finite value")
    if "qualifier" in c and (not isinstance(c["qualifier"], str) or not re.fullmatch(r"\w+", c["qualifier"])):
        raise ValueError("qualifier is the catalog suffix without parentheses, e.g. Ta, Tc or TJ")
    if "at" in c:
        at = c["at"]
        if not isinstance(at, dict) or not at or any(
                not isinstance(k, str) or not k.strip() or not isinstance(v, dict) or set(v) != {"value", "unit"}
                or to_decimal(v["value"]) is None or not _unit_ok(v["unit"]) for k, v in at.items()):
            raise ValueError('at needs test conditions, e.g. {"Vgs": {"value": 4.5, "unit": "V"}}')


def keywords(req):
    return req.get("keywords") or " ".join(str(x) for x in
        (req["kind"], req.get("value", ""), req.get("package", ""),
         f"{req['tolerance_percent']}%" if "tolerance_percent" in req else "",
         req.get("qualification") or ("AEC-Q200" if req["automotive"] and req["kind"] in UNITS else
                                      "AEC-Q101" if req["automotive"] and req["kind"] == "diode" else ""),
         req.get("dielectric", "")) if x != "")


# DigiKey-side narrowing. These only shrink the result set; screen() still checks every
# returned part locally, so a wrong or ignored filter cannot create a match.
OPTIONAL_SEARCH_KEYS = ("SearchOptions", "MarketPlaceFilter")  # dropped on HTTP 400 (enum names unverified live)
SORT = {"Field": "QuantityAvailable", "SortOrder": "Descending"}


def api_category_name(req):
    """Category name to resolve into a DigiKey CategoryId, or None.

    Explicit names win. Built-in profiles only infer a category when it is unambiguous:
    chip-size resistors, fixed inductors, and capacitors with an EIA dielectric (ceramic).
    """
    category = req.get("category")
    if isinstance(category, str):
        return category
    if category is not None:
        return None
    kind, package = req.get("kind"), req.get("package", "")
    if kind == "resistor" and re.fullmatch(r"\d{4,5}", package):
        return "Chip Resistor - Surface Mount"
    if kind == "inductor":
        return "Fixed Inductors"
    if kind == "capacitor" and "dielectric" in req:
        return "Ceramic Capacitors"
    return None


def search_filters(req, category_id=None):
    """FilterOptionsRequest for a validated request; None when nothing narrows the search."""
    filters = {}
    category = req.get("category") if type(req.get("category")) is int else category_id
    if category is not None:
        filters["CategoryFilter"] = [{"Id": str(category)}]
    if "min_stock" in req:
        filters["MinimumQuantityAvailable"] = max(req.get("quantity", 1), req["min_stock"])
        filters["MarketPlaceFilter"] = "ExcludeMarketPlace"
        filters["SearchOptions"] = ["InStock"] + (["NormallyStocking"] if req.get("normally_stocking") else [])
    return filters or None


def parametric_request(req, category_id, options):
    """ParameterFilterRequest pinning the exact R/C/L value (and package) using filter values
    DigiKey offered in a previous response, or (None, reason). Local screening still checks
    every returned part, so a wrong filter can only lose candidates, never create a match."""
    if req["kind"] not in UNITS or category_id is None:
        return None, "parametric filters apply to R/C/L with a resolved category"
    wanted, _ = schematic_quantity(req["value"], UNITS[req["kind"]])
    pinned = []
    for option in options:
        name = option["parameter"].casefold()
        if name == FIELDS[req["kind"]].casefold():
            ids = [v["id"] for v in option["values"] if schematic_quantity(v["name"], UNITS[req["kind"]])[0] == wanted]
        elif name == "package / case" and re.fullmatch(r"\d{4,5}", req.get("package", "")):
            ids = [v["id"] for v in option["values"] if package_matches(v["name"], req["package"]) is True]
        else:
            continue
        if ids:
            pinned.append({"ParameterId": option["parameter_id"], "FilterValues": [{"Id": i} for i in ids]})
    names = {p["ParameterId"] for p in pinned}
    value_ids = {o["parameter_id"] for o in options if o["parameter"].casefold() == FIELDS[req["kind"]].casefold()}
    if not names & value_ids:
        return None, f"DigiKey did not offer {req['value']} as a {FIELDS[req['kind']]} filter value in the first results"
    return {"CategoryFilter": {"Id": str(category_id)}, "ParameterFilters": pinned}, None


def search_sort(req):
    """Best-stocked first instead of text relevance, so the page budget sees the most available parts."""
    return dict(SORT) if "min_stock" in req else None


def category_nodes(product):
    node = product.get("Category") or {}
    while node:
        yield node
        children = node.get("ChildCategories") or []
        node = children[0] if children else None


def category_matches(nodes, wanted):
    if not nodes:
        return None
    if type(wanted) is int:
        ids = [n.get("CategoryId") for n in nodes if type(n.get("CategoryId")) is int]
        return wanted in ids if ids else None
    names = [" ".join(n.get("Name", "").split()).casefold() for n in nodes if n.get("Name")]
    return " ".join(wanted.split()).casefold() in names if names else None


def catalog_number(text, unit):
    """Read a whole scalar, preserving signs and refusing ranges/test conditions.

    Empty unit explicitly means a dimensionless count, such as connector positions.
    This parser does not scan a longer description and discard its qualifiers.
    """
    if not isinstance(text, str):
        return None
    match = re.fullmatch(r"\s*([+-]?(?:\d+(?:\.\d+)?|\.\d+)(?:[eE][+-]?\d+)?)\s*(.*?)\s*", text)
    if not match:
        return None
    number, actual_unit = match.groups()
    if unit == "":
        return to_decimal(number) if actual_unit == "" else None
    return convert(number, actual_unit, unit)


NUMBER = r"[+-]?(?:\d+(?:\.\d+)?|\.\d+)(?:[eE][+-]?\d+)?"


def _qualified(item):
    """'5.8A (Ta)' -> ('5.8A', 'Ta'); '45mOhm' -> ('45mOhm', None)."""
    match = re.fullmatch(r"\s*(.*?)\s*(?:\((\w+)\))?\s*", item)
    return match.group(1), match.group(2)


def _condition_names(parameter):
    """'Rds On (Max) @ Id, Vgs' -> ['Id', 'Vgs']: DigiKey lists the test conditions in label order."""
    return [n.strip() for n in parameter.split("@", 1)[1].split(",")] if "@" in parameter else []


def evaluate_numeric(parameter, actual, c):
    """Return (matched True/False/None, note). Conditions, ranges and suffixes are kept, never discarded.

    - '45mOhm @ 4.5A, 10V': compared only when every condition in c['at'] equals the tested one.
    - '-55°C ~ 150°C (TJ)': only op 'covers' with [low, high]; the catalog range must contain it.
    - '5.8A (Ta), 30A (Tc)': c['qualifier'] picks one; without it, the worst case is used
      (smallest for min, largest for max).
    """
    if actual is None or actual.strip() in ("", "-"):
        return None, "not listed"
    unit = c["unit"]
    main, _, conditions = actual.partition("@")
    names = _condition_names(parameter)
    if conditions:
        tested = [x.strip() for x in conditions.split(",")]
        if "at" not in c:
            return None, f"tested at {conditions.strip()}; add 'at' ({', '.join(names) or 'conditions'}) to compare"
        if len(tested) != len(names):
            return None, "cannot pair test conditions with the parameter label"
        for name, wanted in c["at"].items():
            index = next((i for i, n in enumerate(names) if n.casefold() == name.strip().casefold()), None)
            if index is None:
                return None, f"condition {name} is not part of this parameter ({', '.join(names)})"
            observed = catalog_number(tested[index], wanted["unit"])
            if observed is None or observed != Decimal(str(wanted["value"])):
                return None, f"tested at {name}={tested[index]}, required at {wanted['value']}{wanted['unit']}"
    elif "at" in c:
        return None, "catalog value has no test condition to match 'at'"
    span = re.fullmatch(r"\s*(" + NUMBER + r")\s*([^~]*?)\s*~\s*(" + NUMBER + r")\s*([^~(]*?)\s*(?:\((\w+)\))?\s*", main)
    if c["op"] == "covers":
        if not span:
            return None, "catalog value is not a range"
        low, high = convert(span.group(1), span.group(2), unit), convert(span.group(3), span.group(4), unit)
        if low is None or high is None:
            return None, "unreadable range"
        if "qualifier" in c and (span.group(5) or "").casefold() != c["qualifier"].casefold():
            return None, f"range is ({span.group(5) or 'unqualified'}), required ({c['qualifier']})"
        want = [Decimal(str(v)) for v in c["value"]]
        return low <= want[0] and high >= want[1], None
    if span:
        return None, "catalog value is a range; use op 'covers'"
    # '110mOhm Max' states a guaranteed limit; 'Typ'/'Nom' does not guarantee a max/min requirement.
    if re.search(r"\b(typ|typical|nom|nominal)\.?\s*$", main.strip(), re.I) and c["op"] in ("min", "max"):
        return None, "only a typical value is listed; no guaranteed limit"
    main = re.sub(r"\s*\b(max|maximum)\.?\s*$", "", main.strip(), flags=re.I)
    values = [_qualified(item) for item in main.split(",")]
    if "qualifier" in c:
        values = [v for v in values if (v[1] or "").casefold() == c["qualifier"].casefold()]
        if not values:
            return None, f"no ({c['qualifier']}) value listed"
    numbers = [catalog_number(text, unit) for text, _ in values]
    if not numbers or any(n is None for n in numbers):
        return None, "unreadable value"
    expected = Decimal(str(c["value"]))
    if c["op"] == "eq":
        return (all(n == expected for n in numbers) if len(set(numbers)) == 1 else None), (
            None if len(set(numbers)) == 1 else "several values listed")
    worst = min(numbers) if c["op"] == "min" else max(numbers)
    return (worst >= expected if c["op"] == "min" else worst <= expected), None


def package_matches(actual, wanted):
    if not actual:
        return None
    codes, metric = size_codes(actual)
    if re.fullmatch(r"\d{4,5}", wanted):
        # '0402 Metric' must never satisfy an imperial 0402 request.
        return wanted in codes if codes or metric else None
    names = [_compact(actual)] + [_compact(n) for n in actual.split(",")]
    if _compact(wanted) in names:
        return True  # 'TO-236-3, SC-59, SOT-23-3' lists equivalent names for one outline.
    if any(re.fullmatch(re.escape(_compact(wanted)) + r"-\d+", n) for n in names):
        return None  # 'SOT-23' vs 'SOT-23-3': pin count not stated in the request.
    return False


def _compact(text):
    return re.sub(r"\s+", "", text or "").casefold()


def device_outline(text):
    """Pin count prefix and body size, e.g. '6-WSON (2x2)' -> (6, (2, 2)). Missing parts are None."""
    pins = re.match(r"\s*(\d+)-", text or "")
    body = re.search(r"\(\s*(\d+(?:\.\d+)?)\s*[x\u00d7]\s*(\d+(?:\.\d+)?)(?:\s*[x\u00d7]\s*\d+(?:\.\d+)?)?\s*(?:mm)?\s*\)", text or "", re.I)
    return (int(pins.group(1)) if pins else None,
            tuple(sorted(Decimal(x) for x in body.groups())) if body else None)


def device_package_matches(actual, wanted):
    """Exact name -> match. Different pin count or body size -> mismatch. Same outline, other name -> unknown."""
    if not actual:
        return None
    if _compact(actual) == _compact(wanted):
        return True
    (pa, ba), (pw, bw) = device_outline(actual), device_outline(wanted)
    if (pa is not None and pw is not None and pa != pw) or (ba and bw and ba != bw):
        return False
    return None  # Same or unparsed outline under another name: land pattern needs review.


def footprint_check(product, req):
    """Package / Case names a family ('6-WDFN Exposed Pad') that spans many body sizes.

    Supplier Device Package usually carries the body size. Chip-size codes are already
    specific, so they are skipped. Returns (actual, expected, matched) or None if not applicable.
    """
    wanted = req.get("supplier_device_package")
    package = req.get("package")
    if wanted is None and (package is None or re.fullmatch(r"\d{4,5}", package)):
        return None
    actual = digikey.parameter(product, "Supplier Device Package")
    if wanted is not None:
        return actual, wanted, device_package_matches(actual, wanted)
    case = digikey.parameter(product, "Package / Case")
    if not actual or _compact(actual) in (_compact(case), _compact(package)):
        return None  # The listing gives no extra outline information to disagree with.
    return actual, "Specify supplier_device_package to confirm body size / land pattern", None


def screen(product, req):
    """Every supplied constraint is retained with its observed catalog value."""
    checks = []

    def add(name, actual, expected, matched, note=None):
        checks.append({"parameter": name, "actual": actual, "expected": expected,
                       "status": "unknown" if matched is None else "match" if matched else "mismatch"})
        if note:
            checks[-1]["note"] = note

    nodes = list(category_nodes(product))
    path = [n.get("Name", "") for n in nodes]
    leaf = path[-1] if path else None
    kind = req["kind"]
    if kind in CATEGORIES:
        add("category", leaf, kind, leaf in CATEGORIES[kind] if leaf else None)
    elif kind == "diode":
        add("category", leaf, kind, "diode" in leaf.casefold() if leaf else None)
    elif "category" not in req:
        add("category", leaf, "Specify category to screen this discovery query", None)
    if "category" in req:
        add("requested_category", path, req["category"], category_matches(nodes, req["category"]))
    if "package" in req:
        actual = digikey.parameter(product, "Package / Case")
        add("package", actual, req["package"], package_matches(actual, req["package"]))
    footprint = footprint_check(product, req)
    if footprint:
        add("supplier_device_package", *footprint)
    if kind in UNITS:
        actual = digikey.parameter(product, FIELDS[kind])
        number, _ = schematic_quantity(actual, UNITS[kind])
        expected, _ = schematic_quantity(req["value"], UNITS[kind])
        add(FIELDS[kind], actual, req["value"], None if number is None else number == expected)
        actual = digikey.parameter(product, "Tolerance")
        values = percent_values(actual or "")
        add("tolerance_percent", actual, f"<= {req['tolerance_percent']}%",
            None if not values else max(values) <= Decimal(str(req["tolerance_percent"])))
    if req["automotive"] or "qualification" in req:
        actual = ", ".join(v for v in (digikey.parameter(product, x) for x in ("Ratings", "Features", "Qualification")) if v and v.strip() != "-")
        qualification = req.get("qualification") or ("AEC-Q101" if kind == "diode" else "AEC-Q200")
        add("automotive_qualification" if req["automotive"] else "qualification", actual, qualification,
            True if re.search(r"(?<!\w)" + re.escape(qualification.strip()) + r"(?!\w)", actual, re.I) else None)
    if "dielectric" in req:
        actual = digikey.parameter(product, "Temperature Coefficient")
        code = dielectric(actual)
        add("dielectric", actual, req["dielectric"], None if not code else code == dielectric(req["dielectric"]))
    for c in req["constraints"]:
        actual = digikey.parameter(product, c["parameter"])
        if c["op"] == "in":
            allowed = {v.casefold().strip() for v in c["value"]}
            matched = None if actual is None else actual.casefold().strip() in allowed
        elif "unit" not in c:
            matched = None if actual is None else actual.casefold().strip() == c["value"].casefold().strip()
        elif c["parameter"] == "Power (Watts)" and c["unit"] == "W" and c["op"] != "covers" and "@" not in (actual or ""):
            # '0.063W, 1/16W' states one rating twice; use the smaller for minimum comparisons.
            values = power_values(actual or "")
            number = min(values) if values else None
            expected = Decimal(str(c["value"]))
            matched = None if number is None else {"eq": number == expected, "min": number >= expected,
                                                   "max": number <= expected}[c["op"]]
        else:
            matched, note = evaluate_numeric(c["parameter"], actual, c)
            add(c["parameter"], actual, c, matched, note)
            continue
        add(c["parameter"], actual, c, matched)
    status = "rejected" if any(c["status"] == "mismatch" for c in checks) else (
        "needs_review" if any(c["status"] == "unknown" for c in checks) else "catalog_match")
    # All categories count catalog candidates only; manufacturer/pinout approval is separate.
    return status, checks
