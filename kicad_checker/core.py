"""Deterministic checks. No model calls and no schematic modifications."""

import hashlib
import json
import math
import re
import xml.etree.ElementTree as ET
from pathlib import Path


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
        fields = component.get("fields", {})
        exempt = fields.get("Checker_NonPurchasable")
        exemption_reason = identity_text(fields.get("Checker_ExemptionReason"))
        power_symbol = (ref.startswith(("#PWR", "#FLG")) and component.get("symbol_library") == "power")
        if component.get("dnp"):
            item.update(status="not_applicable", category="dnp", reason="DNP: identification is not required for the reviewed default assembly")
        elif power_symbol:
            item.update(status="not_applicable", category="schematic_object", reason="KiCad power symbol or power flag; no purchased component")
        elif str(exempt).casefold() in ("true", "yes", "1") and exemption_reason:
            item.update(status="not_applicable", category="declared_exemption", reason=exemption_reason)
        else:
            reasons = []
            if str(exempt).casefold() in ("true", "yes", "1") and not exemption_reason:
                reasons.append("Non-purchasable exemption needs Checker_ExemptionReason")
            if missing:
                reasons.append("Missing " + ", ".join(missing))
            if conflicts:
                reasons.append("Conflicting identity fields: " + ", ".join(conflicts))
            if supplier_only:
                reasons.append("Declared MPN resembles an LCSC catalog code (C followed by digits); confirm the manufacturer's exact MPN")
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
        "results": results,
    }


def identification_markdown(report, heading=1):
    def cell(value):
        return str(value or "—").replace("|", "\\|").replace("\n", " ").replace("\r", " ")

    lines = ["#" * heading + " Component identification completeness", "", report["scope"], ""]
    lines.extend(f"- {status}: {count}" for status, count in report["coverage"].items())
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
        components[ref] = {
            "reference": ref, "value": element.findtext("value", ""),
            "part_number": parts[0] if len(parts) == 1 else None,
            "manufacturer": manufacturers[0] if len(manufacturers) == 1 else None,
            "symbol_library": symbol.get("lib") if symbol is not None else None,
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
        require(rule.get("kind") in ("connected", "component_property", "manual", "heuristic"), f"{rid}: unsupported check kind")
        require(isinstance(rule.get("target"), str) and rule["target"], f"{rid}: missing target")
        if rule["kind"] == "heuristic":
            require(isinstance(rule.get("candidate"), str) and rule["candidate"], f"{rid}: missing heuristic candidate")
            require(isinstance(rule.get("method"), str) and rule["method"], f"{rid}: missing heuristic method")
        if rule["kind"] == "connected":
            for field in ("pin", "other_target", "other_pin"):
                require(isinstance(rule.get(field), str) and rule[field], f"{rid}: missing {field}")
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


def is_number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def source_is_current(source):
    """A local source is usable only while it matches its recorded contents."""
    path = source.get("path")
    if not path:
        return True  # Older and synthetic evidence may have only a text locator.
    expected = source.get("sha256")
    if not isinstance(expected, str) or len(expected) != 64:
        return False
    file = Path(path)
    return file.is_file() and digest(file) == expected


def review(circuit, rules, evidence):
    validate_rules(rules)
    require(evidence.get("schema_version") == 1, "Evidence needs schema_version: 1")
    require(isinstance(evidence.get("bindings"), dict), "Evidence needs bindings")
    bindings = evidence["bindings"]
    results = []
    for rule in rules["rules"]:
        kind = rule["kind"]
        observation_class = {
            "connected": "deterministic_connectivity",
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
        if not source_is_current(rule["source"]):
            result["reason"] = "Requirement source file is missing or changed; review the current document and revise this rule"
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
        result["evidence"] = fact
        if not isinstance(fact, dict):
            raise ValueError(f"{rule['id']}: evidence fact must be an object")
        basis = rule["evidence_basis"]
        result["evidence_needed"] = basis
        if not fact:
            result["reason"] = ("Manufacturer specification is missing for this component and property" if basis == "manufacturer"
                                else "Schematic-derived evidence is missing for this property")
            continue
        if fact.get("basis") != basis:
            result["reason"] = f"This check requires {basis} evidence; the supplied fact has a different or missing basis"
            continue
        if basis == "manufacturer" and (not component["part_number"] or fact.get("part_number") != component["part_number"]):
            result["reason"] = "Manufacturer evidence is not bound to the exact schematic part number"
            continue
        if fact.get("status") != "verified" or not fact.get("reviewed_by") or not fact.get("source", {}).get("locator"):
            result["reason"] = "Evidence requires verified status, reviewer, and a traceable source locator"
            continue
        if not source_is_current(fact["source"]):
            result["reason"] = "Evidence source file is missing or changed; verify this fact against the current document"
            continue
        if not fact.get("conditions") or fact.get("conditions_match") is not True:
            result["reason"] = "Evidence has not been confirmed for this circuit's operating conditions"
            continue
        if rule["operator"] == "one_of":
            if "value" not in fact:
                result["reason"] = "Missing evidence value"
                continue
            passed = fact["value"] in rule["allowed"]
        else:
            if fact.get("unit") != rule["unit"]:
                result["reason"] = "Evidence units must exactly match rule units; no implicit conversion"
                continue
            lo, hi = fact.get("min"), fact.get("max")
            if not is_number(lo) or not is_number(hi) or lo > hi:
                result["reason"] = "Evidence requires finite, ordered minimum and maximum bounds"
                continue
            passed = lo >= rule["min"] and hi <= rule["max"]
        result.update(status="pass" if passed else "fail", reason="Reviewed evidence satisfies the requirement" if passed else "Reviewed evidence falls outside the requirement")
    coverage = {s: sum(r["status"] == s for r in results) for s in ("pass", "fail", "needs_review", "not_checked", "not_applicable")}
    return {"schema_version": 1, "scope": "Only listed requirements; not a complete design approval. Passes rely on supplied reviewed evidence.",
            "circuit_netlist_sha256": circuit["netlist_sha256"], "coverage": coverage, "results": results,
            "identification": identification(circuit)}


def markdown(report):
    lines = ["# Schematic review", "", report["scope"], "", "## Coverage", ""]
    lines.extend(f"- {status}: {count}" for status, count in report["coverage"].items())
    if not report["results"]:
        lines.extend(["", "No engineering requirements were supplied. Engineering verification is incomplete."])
    if report.get("identification"):
        lines.extend(["", identification_markdown(report["identification"], heading=2)])
    for item in report["results"]:
        lines.extend(["", f"## {item['id']} — {item['status']}", "", item["requirement"], "",
                      f"Target: {item.get('reference') or item['target']}", "",
                      f"Observation class: {item['observation_class']}", "", item["reason"], "",
                      f"Requirement source: {item['source']['locator']}"])
        if item.get("candidate"):
            lines.extend(["", f"Candidate: {item['candidate']}", "", f"Method: {item['method']}"])
        if item.get("observation"):
            lines.extend(["", "Observed connectivity: " + json.dumps(item["observation"], ensure_ascii=False)])
        if item.get("evidence"):
            lines.extend(["", "Evidence:", "", "```json", json.dumps(item["evidence"], indent=2, ensure_ascii=False), "```"])
    return "\n".join(lines) + "\n"
