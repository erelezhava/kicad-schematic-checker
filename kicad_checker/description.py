"""Default check: component Description completeness and cross-check.

Every exported, populated, purchasable component needs a filled Description.
For resistors, capacitors and inductors the Description must also state the value
and (for sized SMD footprints) the package code, and is cross-checked against the
saved Value, footprint, and any rating fields the owner declared.

Statuses: pass, fail (a stated fact contradicts another declared field),
needs_review (empty, unreadable or missing information: an owner action),
not_applicable (DNP, power symbol, documented exemption).
The parser never turns an unreadable text into a fail.
"""

import re
from decimal import Decimal

from .core import component_exemption, identity_text
from .units import dielectric, dielectric_in_text, format_quantity, schematic_quantity

DESCRIPTION_KEYS = {"description", "desc"}
CLASSES = {"C": "capacitor", "R": "resistor", "L": "inductor"}
DIMENSION = {"capacitor": ("capacitance", "F"), "resistor": ("resistance", "Ohm"), "inductor": ("inductance", "H")}
TYPE_WORDS = {
    "capacitor": {"capacitor", "cap", "mlcc"},
    "resistor": {"resistor", "res", "jumper"},
    "inductor": {"inductor", "ind", "ferrite", "choke", "bead"},
}
FOOTPRINT_LIBRARIES = {"Capacitor_SMD": "capacitor", "Capacitor_THT": "capacitor",
                       "Resistor_SMD": "resistor", "Resistor_THT": "resistor",
                       "Inductor_SMD": "inductor", "Inductor_THT": "inductor"}
IMPERIAL_CODES = {"01005", "0201", "0402", "0603", "0805", "1008", "1206", "1210", "1806", "1812",
                  "2010", "2220", "2512", "2920"}
RATING_FIELDS = {
    "voltage": {"voltage", "ratedvoltage", "voltagerating", "vrating"},
    "tolerance": {"tolerance", "tol"},
    "power": {"power", "powerrating", "ratedpower", "wattage"},
    "dielectric": {"dielectric", "tempco", "temperaturecoefficient"},
}


def _key(name):
    return re.sub(r"[^a-z0-9]", "", str(name).casefold())


def field_by_keys(component, keys):
    """Return (field name, text) for the single non-empty field matching keys; (None, None) if absent.
    Raises ValueError when two matching fields disagree."""
    found = {name: str(value).strip() for name, value in component.get("fields", {}).items()
             if _key(name) in keys and identity_text(value if isinstance(value, str) else None)}
    if not found:
        return None, None
    if len(set(found.values())) > 1:
        raise ValueError(f"conflicting fields {sorted(found)}")
    name = sorted(found)[0]
    return name, found[name]


def component_class(ref):
    match = re.fullmatch(r"([CRL])\d+", ref)
    return CLASSES[match.group(1)] if match else None


def normalize_description(text):
    """Accept distributor short-form capitals: '2.2UF', '10UH', '1MH', '10K OHM'."""
    text = re.sub(r"(?<=[\d\s])([UNP])(?=[FH](?![A-Za-z]))", lambda m: m.group(1).lower(), text)
    text = re.sub(r"(?<=[\d\s])MH(?![A-Za-z])", "mH", text)  # megahenry is not a real part value
    return re.sub(r"(?i)ohms?(?![a-z])", "Ohm", text)


def description_quantity(text, unit):
    if "MOHM" in text and not re.search(r"[a-z]", text):
        # All-caps 'MOHM' means milliohm in distributor short forms but would read as megaohm.
        return None, "all-caps 'MOHM' is ambiguous (milliohm or megaohm)"
    return schematic_quantity(normalize_description(text), unit)


def size_codes(text):
    """Return (imperial codes, metric codes) stated in text."""
    metric = set(re.findall(r"(?<![\w.])(\d{4,5})\s*metric", text, re.IGNORECASE))
    stripped = re.sub(r"(?<![\w.])\d{4,5}\s*metric", " ", text, flags=re.IGNORECASE)
    imperial = {code for code in re.findall(r"(?<![\w.])(\d{4,5})(?![\w.])", stripped) if code in IMPERIAL_CODES}
    return imperial, metric


def footprint_size(footprint):
    match = re.search(r"_(\d{4,5})_(\d{4,5})Metric", footprint or "")
    return (match.group(1), match.group(2)) if match else None


def percent_values(text):
    return {Decimal(v) for v in re.findall(r"±?\s*(\d+(?:\.\d+)?)\s*%", text)}


FRACTION_WATTS = r"(?<![\w.])(\d+)\s*/\s*(\d+)\s*W(?![A-Za-z])"


def power_values(text):
    """Watts stated as '0.063W', '63mW' or '1/16W'."""
    values = {Decimal(a) / Decimal(b) for a, b in re.findall(FRACTION_WATTS, text) if int(b)}
    rest = re.sub(FRACTION_WATTS, " ", text)
    for number, prefix in re.findall(r"(?<![\w.])(\d+(?:\.\d+)?)\s*([mk]?)W(?![A-Za-z])", rest):
        values.add(Decimal(number).scaleb({"m": -3, "k": 3, "": 0}[prefix]))
    return values


def same_power(a, b):
    return abs(a - b) <= Decimal("0.02") * max(abs(a), abs(b))


class Audit:
    def __init__(self, ref, component, cls, text):
        self.ref, self.component, self.cls, self.text = ref, component, cls, text
        self.checks = []

    def add(self, check, status, reason, **details):
        self.checks.append({"check": check, "status": status, "reason": reason, **details})

    def run(self):
        if self.cls:
            self.type_words()
            self.footprint_library()
            self.value()
            self.package()
        self.ratings()
        return self.checks

    def type_words(self):
        words = set(re.findall(r"[a-z]+", self.text.casefold()))
        stated = {cls for cls, vocabulary in TYPE_WORDS.items() if words & vocabulary}
        others = stated - {self.cls}
        if others:
            self.add("description_type", "fail",
                     f"Description names a {', '.join(sorted(others))}, but reference {self.ref} is a {self.cls}")
        elif stated:
            self.add("description_type", "pass", f"Description names a {self.cls}")

    def footprint_library(self):
        library = (self.component.get("footprint") or "").split(":")[0]
        expected = FOOTPRINT_LIBRARIES.get(library)
        if expected and expected != self.cls:
            self.add("footprint_type", "fail", f"Footprint library {library} is for a {expected}, but {self.ref} is a {self.cls}")

    def value(self):
        dimension, unit = DIMENSION[self.cls]
        if self.cls == "inductor" and set(re.findall(r"[a-z]+", self.text.casefold())) & {"ferrite", "bead"}:
            dimension, unit = "impedance", "Ohm"  # ferrite beads are specified by impedance
        stated, reason = description_quantity(self.text, unit)
        if reason:
            self.add("value", "needs_review",
                     f"Description does not state a readable {dimension} ({reason}). Owner: write the value into the Description, e.g. a distributor description")
            return
        saved, reason = schematic_quantity(self.component.get("value") or "", unit)
        if reason:
            self.add("value", "needs_review", f"Value field cannot be read as a {dimension} ({reason})")
            return
        details = {"description_value": format_quantity(stated, unit), "schematic_value": format_quantity(saved, unit)}
        if stated == saved:
            self.add("value", "pass", "Description value matches the Value field", **details)
        else:
            self.add("value", "fail", f"Description states {details['description_value']} but Value is {self.component.get('value')!r}", **details)

    def package(self):
        footprint = footprint_size(self.component.get("footprint"))
        if not footprint:
            self.add("package", "not_checked", "Footprint name has no standard size code; package not compared")
            return
        imperial, metric = size_codes(self.text)
        details = {"footprint_size": f"{footprint[0]} ({footprint[1]} metric)",
                   "description_size": sorted(imperial) + [f"{m} metric" for m in sorted(metric)]}
        if not imperial and not metric:
            self.add("package", "needs_review",
                     f"Description does not state the package size; footprint is {footprint[0]} ({footprint[1]} metric). Owner: add it", **details)
            return
        wrong = (imperial - {footprint[0]}) | {f"{m} metric" for m in metric - {footprint[1]}}
        if wrong:
            self.add("package", "fail", f"Description states {', '.join(sorted(wrong))}, but footprint is {footprint[0]} ({footprint[1]} metric)", **details)
        else:
            self.add("package", "pass", "Description package matches the footprint", **details)

    def ratings(self):
        for rating, keys in RATING_FIELDS.items():
            try:
                name, declared = field_by_keys(self.component, keys)
            except ValueError as error:
                self.add(rating, "needs_review", f"Rating fields disagree: {error}")
                continue
            if not declared:
                continue
            getattr(self, f"rating_{rating}")(name, declared)

    def compare(self, rating, name, declared, field_values, text_values, same=lambda a, b: a == b, show=str):
        if len(field_values) != 1:
            self.add(rating, "needs_review", f"Field {name}={declared!r} cannot be read")
            return
        field_value = next(iter(field_values))
        if not text_values:
            self.add(rating, "not_checked", f"Description does not state {rating}; field {name}={declared!r} not cross-checked")
            return
        details = {"field": name, "field_value": declared, "description_values": sorted(show(v) for v in text_values)}
        if all(same(v, field_value) for v in text_values):
            self.add(rating, "pass", f"Description {rating} matches field {name}", **details)
        elif any(same(v, field_value) for v in text_values):
            self.add(rating, "needs_review", f"Description states several {rating} values; only some match {name}", **details)
        else:
            self.add(rating, "fail", f"Description {rating} differs from field {name}={declared!r}", **details)

    def rating_voltage(self, name, declared):
        field, _ = schematic_quantity(declared, "V")
        text, _ = schematic_quantity(normalize_description(self.text), "V")
        self.compare("voltage", name, declared, {field} - {None}, {text} - {None})

    def rating_tolerance(self, name, declared):
        self.compare("tolerance", name, declared, percent_values(declared), percent_values(self.text))

    def rating_power(self, name, declared):
        self.compare("power", name, declared, power_values(declared), power_values(self.text), same_power)

    def rating_dielectric(self, name, declared):
        self.compare("dielectric", name, declared, {dielectric(declared)} - {None}, {dielectric_in_text(self.text)} - {None})


def description_audit(circuit):
    results = []
    for ref, component in sorted(circuit["components"].items()):
        cls = component_class(ref)
        item = {"reference": ref, "sheet_path": component.get("sheet_path"), "component_class": cls or "other",
                "classification_basis": "reference prefix" if cls else None,
                "observation_class": "schematic_metadata", "description": None, "checks": []}
        results.append(item)
        exemption = component_exemption(ref, component)
        if exemption:
            item.update(status="not_applicable", **exemption)
            continue
        try:
            name, text = field_by_keys(component, DESCRIPTION_KEYS)
        except ValueError as error:
            item.update(status="needs_review", reason=f"Description fields disagree ({error}). Owner: keep one Description")
            continue
        if not text:
            item.update(status="needs_review", reason="Description is empty. Owner: fill the Description field")
            continue
        item.update(description=text, description_field=name)
        checks = Audit(ref, component, cls, text).run()
        item["checks"] = checks
        statuses = {c["status"] for c in checks}
        problems = [c["reason"] for c in checks if c["status"] in ("fail", "needs_review")]
        if "fail" in statuses:
            item.update(status="fail", reason="; ".join(problems))
        elif "needs_review" in statuses:
            item.update(status="needs_review", reason="; ".join(problems))
        else:
            item.update(status="pass", reason=("Description is filled and agrees with the declared fields" if cls
                                               else "Description is filled; no automatic cross-check for this component class"))
    return {
        "schema_version": 1,
        "scope": ("Description field of each exported component in the default assembly. R/C/L parts (by reference "
                  "prefix) must state value and package; both are cross-checked against Value and footprint, and "
                  "stated ratings against any declared rating fields. A pass checks declared metadata consistency only."),
        "circuit_netlist_sha256": circuit["netlist_sha256"],
        "coverage": {s: sum(r["status"] == s for r in results) for s in ("pass", "fail", "needs_review", "not_applicable")},
        "results": results,
    }


def description_markdown(report, heading=1):
    def cell(value):
        return str(value or "—").replace("|", "\\|").replace("\n", " ").replace("\r", " ")

    lines = ["#" * heading + " Component description", "", report["scope"], ""]
    lines.extend(f"- {status}: {count}" for status, count in report["coverage"].items())
    lines.extend(["", "| Component | Class | Description | Status | Reason |", "| --- | --- | --- | --- | --- |"])
    for item in report["results"]:
        lines.append("| " + " | ".join(cell(v) for v in (item["reference"], item["component_class"], item["description"],
                                                         item["status"], item.get("reason"))) + " |")
    return "\n".join(lines) + "\n"


def owner_actions(identification_report, description_report):
    """One list, per component, of what the design owner must fill or correct."""
    actions = {}
    for item in identification_report["results"]:
        if item["status"] == "needs_review":
            actions.setdefault(item["reference"], []).append("Identification: " + item.get("reason", "incomplete"))
    for item in description_report["results"]:
        if item["status"] in ("fail", "needs_review"):
            label = "Description conflict" if item["status"] == "fail" else "Description"
            actions.setdefault(item["reference"], []).append(f"{label}: {item.get('reason', '')}")
    return actions


def owner_actions_markdown(actions):
    lines = ["# Owner actions", "",
             "Fill or correct these schematic fields in KiCad, save, then re-run extraction. The checker never edits the design.", ""]
    if not actions:
        lines.append("Nothing to fill: identification and descriptions are complete.")
    for ref in sorted(actions, key=lambda r: (re.sub(r"\d+", "", r), int(re.sub(r"\D", "", r) or 0))):
        lines.append(f"- **{ref}**")
        lines.extend(f"  - {text}" for text in actions[ref])
    return "\n".join(lines) + "\n"
