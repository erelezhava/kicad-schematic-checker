"""Deterministic checks. No model calls and no schematic modifications."""

import hashlib
import json
import math
import re
import xml.etree.ElementTree as ET
from pathlib import Path

from .units import (convert, dielectric, dielectric_in_text, is_dielectric_property, parse_unit,
                    schematic_quantity, text_token, to_decimal, unit_mismatch_reason)


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def write_json(path, value):
    Path(path).write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


IDENTITY_ALIASES = {
    "manufacturer": {"manufacturer", "manufacturername", "mfr", "mfrname", "mfg"},
    "part_number": {"mpn", "manufacturerpartnumber", "manufacturerpart", "mfrpartnumber", "mfrpart", "mfrpn", "prtnum"},
    "lcsc": {"lcsc", "lcscpart", "lcscpartnumber"},
    "jlcpcb": {"jlcpcb", "jlcpcbpart", "jlcpcbpartnumber"},
}
IDENTITY_PLACEHOLDERS = {"", "?", "-", "unknown", "tbd", "tbc", "todo", "n/a", "na", "none", "null", "not specified", "to be determined"}


def identity_text(value):
    if not isinstance(value, str) or value.strip().casefold() in IDENTITY_PLACEHOLDERS:
        return None
    return value.strip()


def identity_candidates(component, role):
    candidates = {}
    for name, raw in component.get("fields", {}).items():
        key = re.sub(r"[^a-z0-9]", "", name.casefold())
        value = identity_text(raw)
        if key in IDENTITY_ALIASES[role] and value:
            candidates[name] = value
    # Keep older circuit.json files usable and expose conflicting declarations.
    if role in ("manufacturer", "part_number"):
        value = identity_text(component.get(role))
        if value:
            candidates[f"circuit.{role}"] = value
    return candidates


def exemption_without_reason(component):
    fields = component.get("fields", {})
    flagged = str(fields.get("Checker_NonPurchasable")).casefold() in ("true", "yes", "1")
    return flagged and not identity_text(fields.get("Checker_ExemptionReason"))


def component_exemption(ref, component):
    """Shared by the identification and description audits: DNP parts, KiCad power
    symbols, and documented non-purchasable objects. Returns None when not exempt."""
    fields = component.get("fields", {})
    if component.get("dnp"):
        return {"category": "dnp", "reason": "DNP: not required for the reviewed default assembly"}
    if ref.startswith(("#PWR", "#FLG")) and component.get("symbol_library") == "power":
        return {"category": "schematic_object", "reason": "KiCad power symbol or power flag; no purchased component"}
    reason = identity_text(fields.get("Checker_ExemptionReason"))
    if str(fields.get("Checker_NonPurchasable")).casefold() in ("true", "yes", "1") and reason:
        return {"category": "declared_exemption", "reason": reason}
    return None


PART_NUMBER_LIKE = re.compile(r"[A-Za-z0-9][A-Za-z0-9\-#/.+]*")


def compact(text):
    """Compare part numbers ignoring case, spaces and - # / . separators."""
    return re.sub(r"[\s\-#/.]", "", text).upper()


def value_mpn_check(ref, component, mpn):
    """Does the Value name the same part as the MPN? R/C/L Values are electrical values
    (covered by the Description check), and generic library text is not compared.
    Returns None when not compared, else {"status", "reason", "value", "mpn"}."""
    value = (component.get("value") or "").strip()
    if not mpn or re.fullmatch(r"[CRL]\d+", ref) or len(value) < 5 or not PART_NUMBER_LIKE.fullmatch(value) \
            or not re.search(r"\d", value):
        return None
    v, m = compact(value), compact(mpn)
    result = {"value": value, "mpn": mpn}
    if v == m:
        return {**result, "status": "pass", "reason": "Value matches the MPN"}
    if m.startswith(v) and len(v) >= 4:
        return {**result, "status": "pass", "reason": "Value is the family/base name of the MPN"}
    if v.startswith(m):
        return {**result, "status": "needs_review",
                "reason": f"MPN {mpn!r} is shorter than Value {value!r}: the MPN may be only a family name; enter the full orderable MPN"}
    common = len(next((v[:i] for i in range(min(len(v), len(m)), 0, -1) if v[:i] == m[:i]), ""))
    if common >= 4:
        return {**result, "status": "needs_review",
                "reason": f"Value {value!r} names a different variant than MPN {mpn!r} (often only packaging); confirm which one is ordered"}
    return {**result, "status": "needs_review", "reason": f"Value {value!r} looks like a different part number than MPN {mpn!r}; confirm"}


def identification(circuit):
    """Audit declared identity of every exported component, without catalog lookups."""
    results = []
    for ref, component in sorted(circuit["components"].items()):
        candidates = {role: identity_candidates(component, role) for role in IDENTITY_ALIASES}
        values = {role: list(dict.fromkeys(items.values())) for role, items in candidates.items()}
        missing = [role for role in ("manufacturer", "part_number") if not values[role]]
        conflicts = {role: items for role, items in candidates.items() if len(values[role]) > 1}
        supplier_only = any(re.fullmatch(r"C\d+", value, re.IGNORECASE) for value in values["part_number"])
        item = {
            "reference": ref, "sheet_path": component.get("sheet_path"),
            "observation_class": "schematic_metadata", "status": "needs_review",
            "manufacturer": values["manufacturer"][0] if len(values["manufacturer"]) == 1 else None,
            "part_number": values["part_number"][0] if len(values["part_number"]) == 1 else None,
            "supplier_part_numbers": {role: values[role] for role in ("lcsc", "jlcpcb")},
            "declared_fields": candidates, "missing_fields": missing, "conflicting_fields": conflicts,
        }
        exemption = component_exemption(ref, component)
        if exemption:
            item.update(status="not_applicable", **exemption)
        else:
            reasons = []
            if exemption_without_reason(component):
                reasons.append("Non-purchasable exemption needs Checker_ExemptionReason")
            if missing:
                reasons.append("Missing " + ", ".join(missing))
            if conflicts:
                reasons.append("Conflicting identity fields: " + ", ".join(conflicts))
            if supplier_only:
                reasons.append("Declared MPN resembles an LCSC catalog code (C followed by digits); confirm the manufacturer's exact MPN")
            check = value_mpn_check(ref, component, item["part_number"])
            if check:
                item["value_check"] = check
                if check["status"] != "pass":
                    reasons.append(check["reason"])
            if reasons:
                item["reason"] = "; ".join(reasons)
            else:
                item.update(status="pass", reason="Manufacturer and MPN are declared; catalog identity and component suitability require separate verification")
        results.append(item)
    return {
        "schema_version": 1,
        "scope": "Declared component identification in the exported netlist, for the default assembly. Symbols excluded from the netlist are outside this audit. A pass establishes metadata completeness only.",
        "circuit_netlist_sha256": circuit["netlist_sha256"],
        "coverage": {s: sum(r["status"] == s for r in results) for s in ("pass", "needs_review", "not_applicable")},
        "groups": identification_groups(results),
        "results": results,
    }


def identification_groups(results):
    """Group unresolved identification by what must be fixed. Readability only:
    every component keeps its own needs_review result."""
    groups = {"missing_manufacturer_mpn_present": {}, "missing_manufacturer_and_mpn": [],
              "missing_mpn_manufacturer_present": [], "conflicting_fields": [],
              "supplier_code_like_mpn": [], "exemption_without_reason": [], "value_mpn_mismatch": []}
    for item in results:
        if item["status"] != "needs_review":
            continue
        ref, missing, reason = item["reference"], item["missing_fields"], item.get("reason", "")
        if missing == ["manufacturer"] and item["part_number"]:
            groups["missing_manufacturer_mpn_present"].setdefault(item["part_number"], []).append(ref)
        elif "manufacturer" in missing and "part_number" in missing:
            groups["missing_manufacturer_and_mpn"].append(ref)
        elif missing == ["part_number"]:
            groups["missing_mpn_manufacturer_present"].append(ref)
        if item["conflicting_fields"]:
            groups["conflicting_fields"].append(ref)
        if "resembles an LCSC" in reason:
            groups["supplier_code_like_mpn"].append(ref)
        if "needs Checker_ExemptionReason" in reason:
            groups["exemption_without_reason"].append(ref)
        if (item.get("value_check") or {}).get("status") == "needs_review":
            groups["value_mpn_mismatch"].append(ref)
    return groups


IDENTIFICATION_GROUP_TITLES = {
    "missing_manufacturer_mpn_present": "MPN present, Manufacturer field missing (fill once per MPN)",
    "missing_manufacturer_and_mpn": "Manufacturer and MPN both missing",
    "missing_mpn_manufacturer_present": "Manufacturer present, MPN missing",
    "conflicting_fields": "Conflicting identity fields",
    "supplier_code_like_mpn": "MPN looks like an LCSC code (confirm the manufacturer MPN)",
    "exemption_without_reason": "Checker_NonPurchasable set without Checker_ExemptionReason",
    "value_mpn_mismatch": "Value and MPN name different parts or variants (confirm which one is ordered)",
}


def identification_markdown(report, heading=1):
    def cell(value):
        return str(value or "—").replace("|", "\\|").replace("\n", " ").replace("\r", " ")

    lines = ["#" * heading + " Component identification completeness", "", report["scope"], ""]
    lines.extend(f"- {status}: {count}" for status, count in report["coverage"].items())
    groups = report.get("groups") or {}
    if any(groups.values()):
        lines.extend(["", "#" * (heading + 1) + " What to fix (grouped)", "",
                      "Grouping is for readability; each component below stays needs_review until fixed.", ""])
        for key, title in IDENTIFICATION_GROUP_TITLES.items():
            members = groups.get(key)
            if not members:
                continue
            if isinstance(members, dict):
                count = sum(len(refs) for refs in members.values())
                lines.append(f"- {title}: {count}")
                lines.extend(f"  - `{cell(mpn)}`: {', '.join(refs)}" for mpn, refs in sorted(members.items()))
            else:
                lines.append(f"- {title}: {len(members)} — {', '.join(members)}")
    lines.extend(["", "| Component | Sheet | Manufacturer | MPN | LCSC/JLCPCB | Status | Reason |", "| --- | --- | --- | --- | --- | --- | --- |"])
    for item in report["results"]:
        supplier = "; ".join(f"{role}: {', '.join(values)}" for role, values in item["supplier_part_numbers"].items() if values)
        lines.append("| " + " | ".join(cell(value) for value in (item["reference"], item["sheet_path"], item["manufacturer"], item["part_number"], supplier, item["status"], item["reason"])) + " |")
    for item in report["results"]:
        if item["conflicting_fields"]:
            lines.extend(["", f"Conflicting fields for {cell(item['reference'])}:", "", "```json", json.dumps(item["conflicting_fields"], indent=2, ensure_ascii=False), "```", ""])
    return "\n".join(lines) + "\n"


def parse_netlist(path):
    data = Path(path).read_bytes()
    if b"<!DOCTYPE" in data.upper() or b"<!ENTITY" in data.upper():
        raise ValueError("XML declarations with external/internal entities are not supported")
    root = ET.fromstring(data)
    if root.tag != "export":
        raise ValueError("Expected a KiCad XML netlist")
    library_pins = {}
    for part in root.findall("./libparts/libpart"):
        key = (part.get("lib"), part.get("part"))
        if key in library_pins:
            raise ValueError(f"Duplicate symbol definition in netlist: {key}")
        pins = part.findall("./pins/pin")
        library_pins[key] = [dict(pin.attrib) for pin in pins]
    components = {}
    for element in root.findall("./components/comp"):
        ref = element.get("ref")
        if not ref or ref in components:
            raise ValueError(f"Missing or duplicate component reference: {ref}")
        props = {p.get("name"): p.get("value", True) for p in element.findall("property")}
        fields = {p.get("name"): p.text or "" for p in element.findall("./fields/field")}
        fields.update(props)
        declared = {"fields": fields}
        parts = list(dict.fromkeys(identity_candidates(declared, "part_number").values()))
        manufacturers = list(dict.fromkeys(identity_candidates(declared, "manufacturer").values()))
        sheet = element.find("sheetpath")
        symbol = element.find("libsource")
        symbol_part = symbol.get("part") if symbol is not None else None
        symbol_library = symbol.get("lib") if symbol is not None else None
        definition = library_pins.get((symbol_library, symbol_part))
        # KiCad 10 exports the pins of placed units, including unconnected pins.
        # A library definition alone cannot establish which units were placed.
        units = element.findall("./units/unit")
        symbol_pins = {}
        complete = bool(units)
        for index, unit in enumerate(units):
            unit_name = unit.get("name") or f"unit-{index + 1}"
            pin_elements = unit.find("pins")
            if pin_elements is None:
                complete = False
                continue
            for pin in pin_elements.findall("pin"):
                number = (pin.get("num") or "").strip()
                if not number:
                    complete = False
                    continue
                record = symbol_pins.setdefault(number, {"units": []})
                if unit_name not in record["units"]:
                    record["units"].append(unit_name)
        for pin in definition or []:
            if pin.get("num") in symbol_pins:
                symbol_pins[pin["num"]].setdefault("name", pin.get("name", ""))
                symbol_pins[pin["num"]].setdefault("type", pin.get("type", ""))
        components[ref] = {
            "reference": ref, "value": element.findtext("value", ""),
            "part_number": parts[0] if len(parts) == 1 else None,
            "manufacturer": manufacturers[0] if len(manufacturers) == 1 else None,
            "symbol_library": symbol_library, "symbol_part": symbol_part,
            "symbol_pins": symbol_pins,
            "symbol_pin_inventory": {"source": "component_units" if units else None,
                                     "complete": complete, "count": len(symbol_pins) if complete else None},
            "library_pins": definition,
            "footprint": element.findtext("footprint", ""),
            "datasheet": element.findtext("datasheet") or fields.get("Datasheet", ""),
            "dnp": "dnp" in props, "fields": fields,
            "sheet_path": sheet.get("names") if sheet is not None else None,
            "uuid_path": element.findtext("tstamps"), "pins": {},
        }
    nets = []
    for element in root.findall("./nets/net"):
        name = element.get("name")
        nodes = []
        for node in element.findall("node"):
            ref, pin = node.get("ref"), node.get("pin")
            if ref not in components or not pin:
                raise ValueError(f"Invalid net node: {ref}.{pin}")
            if pin in components[ref]["pins"]:
                raise ValueError(f"Pin appears more than once in netlist: {ref}.{pin}")
            components[ref]["pins"][pin] = {"net": name, "function": node.get("pinfunction", ""), "type": node.get("pintype", "")}
            if components[ref]["symbol_pin_inventory"]["complete"] and pin not in components[ref]["symbol_pins"]:
                components[ref]["symbol_pin_inventory"].update(complete=False, count=None,
                    reason="A net node uses a pin absent from the exported symbol unit inventory")
            nodes.append(dict(node.attrib))
        nets.append({"name": name, "nodes": nodes})
    if not components:
        raise ValueError("Netlist contains no components")
    return {"schema_version": 1, "kicad_tool": root.findtext("./design/tool"),
            "source": root.findtext("./design/source"), "netlist_sha256": digest(path),
            "components": components, "nets": nets}


def erc_summary(report):
    counts = {}
    findings = []
    for sheet in report.get("sheets", []):
        for violation in sheet.get("violations", []):
            key = violation.get("severity", "unknown") + ":" + violation.get("type", "unknown")
            counts[key] = counts.get(key, 0) + 1
            findings.append({"sheet": sheet.get("path"), **violation})
    return {"counts": counts, "ignored_checks": report.get("ignored_checks", []), "findings": findings}


def require(condition, message):
    if not condition:
        raise ValueError(message)


def validate_rules(rules):
    require(rules.get("schema_version") == 1, "Rules need schema_version: 1")
    require(isinstance(rules.get("rules"), list), "Rules must contain a rules array")
    ids = set()
    for rule in rules["rules"]:
        rid = rule.get("id")
        require(isinstance(rid, str) and rid and rid not in ids, "Each rule needs a unique id")
        ids.add(rid)
        require(rule.get("status") in ("draft", "approved"), f"{rid}: status must be draft or approved")
        require(isinstance(rule.get("requirement"), str) and rule["requirement"], f"{rid}: missing requirement")
        require(isinstance(rule.get("source"), dict) and rule["source"].get("locator"), f"{rid}: missing source locator")
        require(rule.get("kind") in ("connected", "component_property", "symbol_pin_count", "manual", "heuristic"), f"{rid}: unsupported check kind")
        require(isinstance(rule.get("target"), str) and rule["target"], f"{rid}: missing target")
        if rule["kind"] == "heuristic":
            require(isinstance(rule.get("candidate"), str) and rule["candidate"], f"{rid}: missing heuristic candidate")
            require(isinstance(rule.get("method"), str) and rule["method"], f"{rid}: missing heuristic method")
        if rule["kind"] == "connected":
            for field in ("pin", "other_target", "other_pin"):
                require(isinstance(rule.get(field), str) and rule[field], f"{rid}: missing {field}")
        if rule["kind"] == "symbol_pin_count":
            for field in ("manufacturer", "part_number", "package", "pin_count_convention"):
                require(bool(identity_text(rule.get(field))), f"{rid}: missing {field}")
            count = rule.get("expected_count")
            require(isinstance(count, int) and not isinstance(count, bool) and count > 0, f"{rid}: expected_count must be a positive integer")
            numbers = rule.get("expected_pin_numbers")
            if numbers is not None:
                require(isinstance(numbers, list) and len(numbers) == count and all(isinstance(n, str) and n.strip() == n and n for n in numbers),
                        f"{rid}: expected_pin_numbers must contain expected_count non-empty pin numbers")
                require(len(set(numbers)) == count, f"{rid}: expected_pin_numbers must be unique")
        if rule["kind"] == "component_property":
            require(bool(rule.get("property")), f"{rid}: missing property")
            require(rule.get("evidence_basis") in ("schematic", "manufacturer"),
                    f"{rid}: evidence_basis must be schematic or manufacturer")
            require(rule.get("operator") in ("range", "one_of"), f"{rid}: unsupported operator")
            if rule["operator"] == "range":
                require(all(is_number(rule.get(x)) for x in ("min", "max")), f"{rid}: invalid numeric limits")
                require(rule["min"] <= rule["max"], f"{rid}: reversed range")
                require(bool(rule.get("unit")), f"{rid}: missing unit")
            else:
                require(isinstance(rule.get("allowed"), list) and bool(rule["allowed"]), f"{rid}: missing allowed values")
                if is_dielectric_property(rule):
                    unknown = [v for v in rule["allowed"] if dielectric(v) is None]
                    require(not unknown, f"{rid}: allowed dielectric values are not recognized EIA codes: {unknown}")
            field = rule.get("schematic_field")
            if field is not None:
                require(isinstance(field, str) and bool(field.strip()), f"{rid}: schematic_field must be a non-empty string")
                require(rule["evidence_basis"] == "schematic", f"{rid}: schematic_field is only valid with evidence_basis schematic")
                if rule["operator"] == "range":
                    require(parse_unit(rule["unit"]) is not None, f"{rid}: unit {rule['unit']!r} cannot be read from a schematic field")
        human = rule.get("approved_by_human")
        require(human is None or isinstance(human, str), f"{rid}: approved_by_human must be a string")


def is_number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def resolve_source(path, base=None):
    """Absolute paths are used as-is. Relative paths resolve against `base` (the folder
    of the rules/evidence file, or --docs-root). Returns None when no base is known."""
    path = Path(path)
    if path.is_absolute():
        return path
    if base is None:
        return None
    return (Path(base) / path).resolve()


def source_problem(source, base=None):
    """Return None while a pinned local source matches its SHA-256, else a short reason."""
    path = source.get("path")
    if not path:
        return None  # Older and synthetic evidence may have only a text locator.
    expected = source.get("sha256")
    if not isinstance(expected, str) or len(expected) != 64:
        return "no valid sha256 recorded for the local source"
    file = resolve_source(path, base)
    if file is None:
        return f"relative source path {path!r} has no base directory"
    if not file.is_file():
        return f"source file not found at {file}"
    if digest(file) != expected:
        return f"source file {file} has changed (SHA-256 differs)"
    return None


def source_is_current(source, base=None):
    """A local source is usable only while it matches its recorded contents."""
    return source_problem(source, base) is None


def check_symbol_pin_count(result, component, rule, binding):
    result["evidence_needed"] = "Package-specific manufacturer pin count and full placed-symbol pin inventory"
    parts = list(identity_candidates(component, "part_number").values())
    if component.get("part_number") != rule["part_number"] or not parts or any(part != rule["part_number"] for part in parts):
        result["reason"] = "Datasheet pin-count requirement is not bound to the exact schematic MPN"
        return
    names = list(identity_candidates(component, "manufacturer").values())
    if not names or any(name.casefold() != rule["manufacturer"].strip().casefold() for name in names):
        result["reason"] = "Datasheet pin-count requirement is not bound to the declared manufacturer"
        return
    if text_token(binding.get("package")) != text_token(rule["package"]):
        result["reason"] = "Confirm the selected part's package in the target binding before comparing pins"
        return
    inventory = component.get("symbol_pin_inventory", {})
    pins = component.get("symbol_pins")
    if inventory.get("complete") is not True or inventory.get("source") != "component_units" or not isinstance(pins, dict):
        result["reason"] = "Full placed-symbol pin inventory is unavailable or incomplete; re-export with KiCad 10. Connected pins or library pins alone cannot establish this count"
        return
    if not pins or any(not isinstance(number, str) or not number.strip() for number in pins):
        result["reason"] = "Symbol pin inventory is empty or has invalid pin numbers"
        return
    if any(pin not in pins for pin in component.get("pins", {})):
        result["reason"] = "Connected pins disagree with the symbol pin inventory; re-extract the design"
        return
    numbers = sorted(pins)
    passed = len(numbers) == rule["expected_count"]
    observation = {"symbol_pin_count": len(numbers), "expected_pin_count": rule["expected_count"],
                   "symbol_pin_numbers": numbers, "package": rule["package"],
                   "pin_count_convention": rule["pin_count_convention"]}
    expected_numbers = rule.get("expected_pin_numbers")
    if expected_numbers is not None:
        missing = sorted(set(expected_numbers) - set(numbers))
        unexpected = sorted(set(numbers) - set(expected_numbers))
        observation.update(missing_pin_numbers=missing, unexpected_pin_numbers=unexpected)
        passed = passed and not missing and not unexpected
    result.update(status="pass" if passed else "fail", observation=observation,
                  reason="Symbol pin count matches the reviewed datasheet requirement; pin names, functions, and footprint mapping need separate checks" if passed
                  else "Symbol pins differ from the reviewed package-specific datasheet requirement")


def review(circuit, rules, evidence, rules_base=None, evidence_base=None):
    """Evaluate rules. Relative source paths resolve against rules_base / evidence_base."""
    validate_rules(rules)
    require(evidence.get("schema_version") == 1, "Evidence needs schema_version: 1")
    require(isinstance(evidence.get("bindings"), dict), "Evidence needs bindings")
    bindings = evidence["bindings"]
    results = []
    for rule in rules["rules"]:
        kind = rule["kind"]
        observation_class = {
            "connected": "deterministic_connectivity",
            "symbol_pin_count": "manufacturer_specification",
            "component_property": "schematic_value" if rule.get("evidence_basis") == "schematic" else "manufacturer_specification",
            "manual": "manual_review",
            "heuristic": "heuristic_candidate",
        }[kind]
        result = {"id": rule["id"], "requirement": rule["requirement"],
                  "source": rule["source"], "target": rule["target"],
                  "observation_class": observation_class, "status": "needs_review"}
        results.append(result)
        if rule["status"] != "approved":
            result.update(status="not_checked", reason="Draft rule: applicability and interpretation need review")
            continue
        problem = source_problem(rule["source"], rules_base)
        if problem:
            result["reason"] = f"Requirement source file is missing or changed ({problem}); review the current document and revise this rule"
            continue
        binding = bindings.get(rule["target"], {})
        ref = binding.get("reference")
        result["reference"] = ref
        component = circuit["components"].get(ref)
        if not component:
            result["reason"] = "Target has not been bound to an existing component"
            continue
        if binding.get("applicability") == "not_applicable" and binding.get("rationale") and binding.get("reviewed_by"):
            result.update(status="not_applicable", reason=binding["rationale"], reviewed_by=binding["reviewed_by"])
            continue
        if binding.get("applicability") != "applicable" or not binding.get("reviewed_by") or not binding.get("rationale"):
            result["reason"] = "Applicability requires a reviewed rationale for this circuit and operating conditions"
            continue
        if kind == "heuristic":
            result.update(reason="Heuristic candidate only; confirm with a deterministic check or reviewed engineering evidence before assigning pass/fail",
                          candidate=rule["candidate"], method=rule["method"])
            continue
        if component["dnp"]:
            result.update(status="fail", reason="Required component is marked do not populate")
            continue
        if rule["kind"] == "manual":
            result["reason"] = "Manual engineering review required; this version does not auto-pass manual checks"
            continue
        if rule["kind"] == "symbol_pin_count":
            check_symbol_pin_count(result, component, rule, binding)
            continue
        if rule["kind"] == "connected":
            other_binding = bindings.get(rule["other_target"], {})
            other = circuit["components"].get(other_binding.get("reference"))
            if not other:
                result["reason"] = "Other endpoint is not bound to an existing component"
                continue
            if other["dnp"]:
                result.update(status="fail", reason="Other required endpoint is marked do not populate")
                continue
            a = component["pins"].get(rule["pin"])
            b = other["pins"].get(rule["other_pin"])
            if not a or not b:
                result["reason"] = "A requested pin is missing from the netlist; verify pin mapping"
                continue
            same = a["net"] == b["net"] and bool(a["net"])
            result.update(status="pass" if same else "fail", reason="Same electrical net" if same else "Different electrical nets",
                          observation={"from": f"{ref}.{rule['pin']}", "from_net": a["net"],
                                       "to": f"{other['reference']}.{rule['other_pin']}", "to_net": b["net"]})
            continue
        fact = evidence.get("components", {}).get(ref, {}).get(rule["property"], {})
        if not isinstance(fact, dict):
            raise ValueError(f"{rule['id']}: evidence fact must be an object")
        check_property(result, rule, component, fact, evidence_base)
    for rule, result in zip(rules["rules"], results):
        result["approval"] = approval(rule, result, bindings)
    coverage = {s: sum(r["status"] == s for r in results) for s in ("pass", "fail", "needs_review", "not_checked", "not_applicable")}
    passes = [r for r in results if r["status"] == "pass"]
    return {"schema_version": 1, "scope": "Only listed requirements; not a complete design approval. Passes rely on supplied reviewed evidence.",
            "circuit_netlist_sha256": circuit["netlist_sha256"], "coverage": coverage, "results": results,
            "approval_summary": {"pass_human_approved": sum(r["approval"] == "human" for r in passes),
                                 "pass_agent_only": sum(r["approval"] == "agent_only" for r in passes)},
            "identification": identification(circuit),
            "description": description_report(circuit)}


def description_report(circuit):
    from .description import description_audit  # local import: description.py imports core
    return description_audit(circuit)


def approval(rule, result, bindings):
    """Report 'human' only when every record a result relied on has approved_by_human.

    Reporting only, never a gate: agent-reviewed results keep their status. An agent
    must never write approved_by_human on the user's behalf.
    """
    records = [rule, bindings.get(rule["target"], {})]
    if rule["kind"] == "connected":
        records.append(bindings.get(rule["other_target"], {}))
    if result.get("evidence"):
        records.append(result["evidence"])
    return "human" if all(identity_text(r.get("approved_by_human")) for r in records) else "agent_only"


def schematic_field(component, name):
    """Read a saved schematic field. 'Value' maps to the symbol value."""
    if name.strip().casefold() == "value":
        return component.get("value")
    fields = component.get("fields", {})
    if name in fields:
        return fields[name]
    matches = [v for k, v in fields.items() if k.casefold() == name.strip().casefold()]
    return matches[0] if len(matches) == 1 else None


def one_of_match(rule, value, from_text=False):
    """Return (matched, None), or (None, reason) when the value is not recognized."""
    if is_dielectric_property(rule):
        canonical = dielectric_in_text(value) if from_text else dielectric(value)
        if canonical is None:
            return None, f"Dielectric {value!r} is not a recognized EIA code; no comparison attempted"
        return canonical in {dielectric(v) for v in rule["allowed"]}, None
    if value is None or (isinstance(value, str) and not value.strip()):
        return None, "Evidence value is empty"
    return text_token(value) in {text_token(v) for v in rule["allowed"]}, None


def fact_problem(fact, evidence_base):
    if fact.get("status") != "verified" or not fact.get("reviewed_by") or not fact.get("source", {}).get("locator"):
        return "Evidence requires verified status, reviewer, and a traceable source locator"
    problem = source_problem(fact["source"], evidence_base)
    if problem:
        return f"Evidence source file is missing or changed ({problem}); verify this fact against the current document"
    if not fact.get("conditions") or fact.get("conditions_match") is not True:
        return "Evidence has not been confirmed for this circuit's operating conditions"
    return None


def fact_interval(fact, unit):
    """Convert a fact's min/max to the rule unit. Returns (lo, hi, None) or (None, None, reason)."""
    lo, hi = fact.get("min"), fact.get("max")
    if not is_number(lo) or not is_number(hi) or lo > hi:
        return None, None, "Evidence requires finite, ordered minimum and maximum bounds"
    lo, hi = convert(lo, fact.get("unit"), unit), convert(hi, fact.get("unit"), unit)
    if lo is None or hi is None:
        return None, None, unit_mismatch_reason(fact.get("unit"), unit)
    return lo, hi, None


def check_property(result, rule, component, fact, evidence_base):
    basis = rule["evidence_basis"]
    result["evidence_needed"] = basis
    if fact:
        result["evidence"] = fact
    if basis == "schematic":
        check_schematic_property(result, rule, component, fact, evidence_base)
        return
    if not fact:
        result["reason"] = "Manufacturer specification is missing for this component and property"
        return
    if fact.get("basis") != basis:
        result["reason"] = f"This check requires {basis} evidence; the supplied fact has a different or missing basis"
        return
    if not component["part_number"] or fact.get("part_number") != component["part_number"]:
        result["reason"] = "Manufacturer evidence is not bound to the exact schematic part number"
        return
    problem = fact_problem(fact, evidence_base)
    if problem:
        result["reason"] = problem
        return
    if rule["operator"] == "one_of":
        if "value" not in fact:
            result["reason"] = "Missing evidence value"
            return
        passed, reason = one_of_match(rule, fact["value"])
        if reason:
            result["reason"] = reason
            return
    else:
        lo, hi, reason = fact_interval(fact, rule["unit"])
        if reason:
            result["reason"] = reason
            return
        if fact.get("unit") != rule["unit"]:
            result["observation"] = {"converted_min": str(lo), "converted_max": str(hi), "unit": rule["unit"]}
        passed = lo >= to_decimal(rule["min"]) and hi <= to_decimal(rule["max"])
    result.update(status="pass" if passed else "fail",
                  reason="Reviewed evidence satisfies the requirement" if passed else "Reviewed evidence falls outside the requirement")


def check_schematic_property(result, rule, component, fact, evidence_base):
    """The saved schematic is the source of truth; a supplied fact may only agree with it."""
    field = rule.get("schematic_field") or fact.get("field")
    if fact:
        if fact.get("basis") != "schematic":
            result["reason"] = "This check requires schematic evidence; the supplied fact has a different or missing basis"
            return
        if rule.get("schematic_field") and fact.get("field") and fact["field"] != rule["schematic_field"]:
            result["reason"] = "Evidence names a different schematic field than the rule"
            return
        problem = fact_problem(fact, evidence_base)
        if problem:
            result["reason"] = problem
            return
    elif not field:
        result["reason"] = "Schematic-derived evidence is missing for this property"
        return
    if not isinstance(field, str) or not field.strip():
        result["reason"] = "Schematic evidence must name the KiCad field it was read from (rule schematic_field or fact field)"
        return
    text = schematic_field(component, field)
    observation = {"schematic_field": field, "schematic_text": text}
    result["observation"] = observation
    if not isinstance(text, str) or not text.strip():
        result["reason"] = f"Component has no saved schematic field {field!r}"
        return
    if rule["operator"] == "one_of":
        passed, reason = one_of_match(rule, text, from_text=True)
        if reason:
            result["reason"] = reason
            return
        if fact and "value" in fact:
            saved = dielectric_in_text(text) if is_dielectric_property(rule) else text
            agrees, _ = one_of_match({**rule, "allowed": [saved]}, fact["value"])
            if not agrees:
                result["reason"] = f"Schematic evidence {fact['value']!r} disagrees with the saved schematic field {field!r} = {text!r}"
                return
    else:
        value, reason = schematic_quantity(text, rule["unit"])
        if reason:
            result["reason"] = reason
            return
        observation["schematic_value"] = f"{value} {rule['unit']}"
        if fact and ("min" in fact or "max" in fact):
            lo, hi, reason = fact_interval(fact, rule["unit"])
            if reason:
                result["reason"] = reason
                return
            if not lo == value == hi:
                result["reason"] = (f"Schematic evidence ({fact.get('min')}–{fact.get('max')} {fact.get('unit')}) disagrees with "
                                    f"the saved schematic field {field!r} = {text!r}")
                return
        passed = to_decimal(rule["min"]) <= value <= to_decimal(rule["max"])
    result.update(status="pass" if passed else "fail",
                  reason="Saved schematic value satisfies the requirement" if passed
                  else "Saved schematic value falls outside the requirement")


def markdown(report):
    lines = ["# Schematic review", "", report["scope"], "", "## Coverage", ""]
    lines.extend(f"- {status}: {count}" for status, count in report["coverage"].items())
    summary = report.get("approval_summary")
    if summary:
        lines.extend(["", f"Passes approved by a human: {summary['pass_human_approved']}. "
                          f"Passes resting on agent review only: {summary['pass_agent_only']}."])
    if not report["results"]:
        lines.extend(["", "No engineering requirements were supplied. Engineering verification is incomplete."])
    if report.get("identification"):
        lines.extend(["", identification_markdown(report["identification"], heading=2)])
    if report.get("description"):
        from .description import description_markdown
        lines.extend(["", description_markdown(report["description"], heading=2)])
    for item in report["results"]:
        lines.extend(["", f"## {item['id']} — {item['status']}", "", item["requirement"], "",
                      f"Target: {item.get('reference') or item['target']}", "",
                      f"Observation class: {item['observation_class']}", "",
                      "Approval: " + ("human" if item.get("approval") == "human" else "agent review only"), "",
                      item["reason"], "",
                      f"Requirement source: {item['source']['locator']}"])
        if item.get("candidate"):
            lines.extend(["", f"Candidate: {item['candidate']}", "", f"Method: {item['method']}"])
        if item.get("observation"):
            lines.extend(["", "Observation: " + json.dumps(item["observation"], ensure_ascii=False)])
        if item.get("evidence"):
            lines.extend(["", "Evidence:", "", "```json", json.dumps(item["evidence"], indent=2, ensure_ascii=False), "```"])
    return "\n".join(lines) + "\n"
