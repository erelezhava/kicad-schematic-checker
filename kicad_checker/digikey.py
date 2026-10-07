"""DigiKey lookup by exact MPN, cached, and checks against the saved schematic.

Two steps, kept apart so reviews are offline and reproducible:

* fetch: query DigiKey Product Information v4 keyword search for every MPN in a
  circuit and store the exact matches in a cache folder (needs credentials).
* audit: compare cached DigiKey data with the schematic (no network).

Statuses follow the rest of the checker: a deterministic contradiction between
DigiKey data and a declared field is `fail`; not found, ambiguous, lifecycle
warnings, manufacturer naming differences and unreadable values are
`needs_review`; parts without an MPN are `not_checked` (identification already
reports them); DNP and exempt parts are `not_applicable`.
DigiKey is catalog data from a distributor, not manufacturer-authored evidence.
"""

import hashlib
import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from . import credentials
from .core import component_exemption, identity_candidates
from .description import (DIMENSION, RATING_FIELDS, component_class, field_by_keys,
                          footprint_size, normalize_description, percent_values, power_values, same_power,
                          size_codes, DESCRIPTION_KEYS)
from .units import dielectric, dielectric_in_text, format_quantity, schematic_quantity

API = "https://api.digikey.com"
LOCALE = {"X-DIGIKEY-Locale-Site": "US", "X-DIGIKEY-Locale-Language": "en", "X-DIGIKEY-Locale-Currency": "USD"}
CACHE_SCHEMA = 1
DEFAULT_CACHE = Path.home() / ".cache" / "kicad_checker" / "digikey"
ACTIVE = {"active"}


# --- credentials and HTTP ---------------------------------------------------------

def load_credentials(files=None):
    """Your own DigiKey keys from the environment, .env, or ~/.config/kicad_checker/credentials.env."""
    return tuple(credentials.require(("DIGIKEY_CLIENT_ID", "DIGIKEY_CLIENT_SECRET"), "DigiKey", files))


class DigiKeyClient:
    """Minimal Product Information v4 client: OAuth2 client credentials + keyword search."""

    def __init__(self, client_id, secret, api=API, min_interval=0.6, opener=urllib.request.urlopen):
        self.client_id, self.secret, self.api = client_id, secret, api
        self.min_interval, self.opener = min_interval, opener
        self.token, self.last_call = None, 0.0

    def _request(self, request):
        wait = self.min_interval - (time.monotonic() - self.last_call)
        if wait > 0:
            time.sleep(wait)  # stay under ~120 requests/minute
        self.last_call = time.monotonic()
        try:
            with self.opener(request, timeout=30) as response:
                return response.status, json.load(response)
        except urllib.error.HTTPError as error:
            with error:
                text = error.read().decode(errors="replace")
            try:
                body = json.loads(text)
            except json.JSONDecodeError:
                body = {"raw": text[:500]}
            return error.code, body

    def authenticate(self):
        body = urllib.parse.urlencode({"client_id": self.client_id, "client_secret": self.secret,
                                       "grant_type": "client_credentials"}).encode()
        request = urllib.request.Request(f"{self.api}/v1/oauth2/token", data=body,
                                         headers={"Content-Type": "application/x-www-form-urlencoded"})
        status, data = self._request(request)
        if status != 200 or "access_token" not in data:
            raise ValueError(f"DigiKey authentication failed (HTTP {status}): {data.get('error_description') or data.get('detail') or data}")
        self.token = data["access_token"]

    def keyword(self, mpn, retried=False):
        if not self.token:
            self.authenticate()
        payload = json.dumps({"Keywords": mpn, "Limit": 10, "Offset": 0}).encode()
        request = urllib.request.Request(f"{self.api}/products/v4/search/keyword", data=payload, method="POST", headers={
            "X-DIGIKEY-Client-Id": self.client_id, "Authorization": f"Bearer {self.token}",
            "Content-Type": "application/json", "Accept": "application/json", **LOCALE})
        status, body = self._request(request)
        if status == 401 and not retried:
            self.token = None
            return self.keyword(mpn, retried=True)
        if status == 429 and not retried:
            time.sleep(60)
            return self.keyword(mpn, retried=True)
        return status, body


# --- cache ------------------------------------------------------------------------

def mpn_key(mpn):
    return " ".join(str(mpn).split()).casefold()


def cache_path(cache_dir, mpn):
    digest = hashlib.sha256(mpn_key(mpn).encode()).hexdigest()[:16]
    safe = re.sub(r"[^A-Za-z0-9._-]", "_", mpn_key(mpn).upper())[:60]
    return Path(cache_dir) / f"{safe}.{digest}.json"


def trim_product(product):
    """Keep what the checks and the reader need; drop images and volatile clutter."""
    drop = {"PhotoUrl", "PrimaryVideoUrl", "OtherNames"}
    return {k: v for k, v in product.items() if k not in drop and "accountid" not in k.casefold() and "customerid" not in k.casefold()}


def store(cache_dir, mpn, status, body):
    entry = {"schema_version": CACHE_SCHEMA, "source": "DigiKey Product Information v4 keyword search",
             "mpn": mpn, "fetched_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
             "http_status": status}
    if status == 200:
        entry["products_count"] = body.get("ProductsCount")
        entry["exact_matches"] = [trim_product(p) for p in body.get("ExactMatches") or []]
    else:
        entry["error"] = {k: body.get(k) for k in ("title", "detail", "status")} if isinstance(body, dict) else str(body)
    path = cache_path(cache_dir, mpn)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(entry, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    return entry


def load(cache_dir, mpn):
    path = cache_path(cache_dir, mpn)
    if not path.is_file():
        return None
    entry = json.loads(path.read_text(encoding="utf-8"))
    return entry if entry.get("schema_version") == CACHE_SCHEMA and mpn_key(entry.get("mpn", "")) == mpn_key(mpn) else None


def circuit_mpns(circuit):
    """Unique declared MPNs of populated, non-exempt parts (conflicting MPNs are skipped)."""
    mpns = {}
    for ref, component in sorted(circuit["components"].items()):
        if component_exemption(ref, component):
            continue
        values = list(dict.fromkeys(identity_candidates(component, "part_number").values()))
        if len(values) == 1:
            mpns.setdefault(values[0], []).append(ref)
    return mpns


def fetch(circuit, cache_dir, client, refresh=False, log=print):
    summary = {"fetched": 0, "cached": 0, "errors": 0}
    for mpn in circuit_mpns(circuit):
        if not refresh and load(cache_dir, mpn) is not None:
            summary["cached"] += 1
            continue
        status, body = client.keyword(mpn)
        entry = store(cache_dir, mpn, status, body)
        if status == 200:
            summary["fetched"] += 1
            log(f"{mpn}: {len(entry['exact_matches'])} exact match(es)")
        else:
            summary["errors"] += 1
            log(f"{mpn}: HTTP {status} {entry.get('error')}")
    return summary


# --- manufacturer names -----------------------------------------------------------

MANUFACTURER_STOPWORDS = {"inc", "incorporated", "corp", "corporation", "co", "company", "ltd", "limited", "llc",
                          "gmbh", "nv", "bv", "ag", "sa", "plc", "kk", "usa", "the", "group", "international",
                          "technologies", "technology", "electronics", "electronic", "components", "semiconductor",
                          "semiconductors", "devices"}
MANUFACTURER_ALIASES = {
    "ti": "texas instruments", "adi": "analog", "analog": "analog", "linear": "analog", "ltc": "analog",
    "maxim integrated": "analog", "maxim": "analog", "st": "stmicroelectronics", "stm": "stmicroelectronics",
    "atmel": "microchip", "microsemi": "microchip", "on": "onsemi", "fairchild": "onsemi",
    "international rectifier": "infineon", "ir": "infineon", "cypress": "infineon",
    "intersil": "renesas", "idt": "renesas", "avx": "kyocera avx", "kyocera": "kyocera avx",
    "wurth": "würth elektronik", "würth": "würth elektronik", "wurth elektronik": "würth elektronik", "we": "würth elektronik",
    "panasonic": "panasonic", "samsung": "samsung electro mechanics", "sem": "samsung electro mechanics",
}


def manufacturer_key(name):
    words = [w for w in re.sub(r"[^\w]+", " ", str(name).casefold()).split() if w not in MANUFACTURER_STOPWORDS]
    key = " ".join(words)
    return MANUFACTURER_ALIASES.get(key, key)


def manufacturer_matches(declared, products):
    """Products whose manufacturer equals the declared one; fall back to word-prefix matches."""
    wanted = manufacturer_key(declared)
    if not wanted:
        return []
    exact = [p for p in products if manufacturer_key(p["Manufacturer"]["Name"]) == wanted]
    if exact:
        return exact
    return [p for p in products if manufacturer_key(p["Manufacturer"]["Name"]).split()[:len(wanted.split())] == wanted.split()]


# --- product helpers --------------------------------------------------------------

def parameter(product, *names):
    wanted = {n.casefold() for n in names}
    for item in product.get("Parameters", []):
        if str(item.get("ParameterText", "")).casefold() in wanted:
            value = str(item.get("ValueText", "")).strip()
            return None if value in ("", "-") else value
    return None


def category_path(product):
    names, node = [], product.get("Category") or {}
    while node:
        names.append(node.get("Name", ""))
        children = node.get("ChildCategories") or []
        node = children[0] if children else None
    return names


def lifecycle(product):
    status = (product.get("ProductStatus") or {}).get("Status") or "unknown"
    flags = [name for name, key in (("discontinued", "Discontinued"), ("end of life", "EndOfLife")) if product.get(key)]
    return status, flags


def same_parameters(products):
    keys = lambda p: sorted((x.get("ParameterText"), x.get("ValueText")) for x in p.get("Parameters", []))
    return all(keys(p) == keys(products[0]) for p in products[1:])


# --- audit ------------------------------------------------------------------------

class Checks:
    def __init__(self):
        self.items = []

    def add(self, check, status, reason, **details):
        self.items.append({"check": check, "status": status, "reason": reason, **details})


def compare_class(checks, cls, product):
    path = " / ".join(category_path(product)).casefold()
    stated = {c for c, word in (("capacitor", "capacitor"), ("resistor", "resistor"), ("inductor", "inductor")) if word in path}
    if stated and cls not in stated and not (cls == "inductor" and "filter" in path):
        checks.add("category", "fail", f"DigiKey lists this MPN under {' / '.join(category_path(product))}, but the reference is a {cls}")
    elif stated or (cls == "inductor" and "filter" in path):
        checks.add("category", "pass", f"DigiKey category matches a {cls}")


def compare_package(checks, component, product):
    case = parameter(product, "Package / Case")
    footprint = footprint_size(component.get("footprint"))
    details = {"digikey_package": case, "footprint": component.get("footprint")}
    if not case:
        checks.add("package", "not_checked", "DigiKey lists no Package / Case", **details)
        return
    imperial, metric = size_codes(case)
    if not footprint or not (imperial or metric):
        checks.add("package", "not_checked", f"DigiKey package {case!r}; footprint has no standard size code to compare. Verify the footprint against the datasheet", **details)
        return
    wrong = (imperial - {footprint[0]}) | {f"{m} metric" for m in metric - {footprint[1]}}
    if wrong:
        checks.add("package", "fail", f"DigiKey package is {case}, but the footprint is {footprint[0]} ({footprint[1]} metric)", **details)
    else:
        checks.add("package", "pass", "DigiKey package matches the footprint size", **details)


def pin_count_hint(checks, component, product):
    """Report-only lead: DigiKey package names like '6-WDFN Exposed Pad' give a terminal count.
    Never a verdict: names such as 'TO-39-3' can differ from the real lead count; use a
    datasheet-backed symbol_pin_count rule for a pass/fail."""
    case = parameter(product, "Package / Case") or ""
    match = re.match(r"\s*(\d+)-", case)
    inventory = component.get("symbol_pin_inventory") or {}
    if not match or inventory.get("complete") is not True:
        return
    listed, pad = int(match.group(1)), "exposed pad" in case.casefold()
    placed = inventory.get("count")
    expected = f"{listed}{' + exposed pad' if pad else ''}"
    note = (f"DigiKey package {case!r} suggests {expected} terminals; symbol has {placed}. "
            "Lead only: confirm with the datasheet pin table and a symbol_pin_count rule")
    agrees = placed == listed or (pad and placed == listed + 1)
    checks.add("pin_count_hint", "not_checked", note if not agrees else f"Symbol pin count {placed} is consistent with DigiKey package {case!r} (lead only)",
               digikey_package=case, symbol_pin_count=placed)


def compare_value(checks, cls, component, product):
    dimension, unit = DIMENSION[cls]
    name = {"capacitor": "Capacitance", "resistor": "Resistance", "inductor": "Inductance"}[cls]
    text = parameter(product, name)
    if cls == "inductor" and not text:
        text, dimension, unit = parameter(product, "Impedance @ Frequency"), "impedance", "Ohm"
    if not text:
        checks.add("value", "not_checked", f"DigiKey lists no {name}")
        return
    listed, reason = schematic_quantity(normalize_description(text), unit)
    if reason:
        checks.add("value", "needs_review", f"DigiKey {name} {text!r} could not be read ({reason})")
        return
    saved, reason = schematic_quantity(component.get("value") or "", unit)
    if reason:
        checks.add("value", "needs_review", f"Value field cannot be read as a {dimension} ({reason})")
        return
    details = {"digikey_value": format_quantity(listed, unit), "schematic_value": format_quantity(saved, unit)}
    if listed == saved:
        checks.add("value", "pass", "DigiKey value matches the Value field", **details)
    else:
        checks.add("value", "fail", f"DigiKey lists {details['digikey_value']} for this MPN, but Value is {component.get('value')!r}", **details)


RATINGS = {  # rating -> (DigiKey parameter names, parser, equality)
    "voltage": (("Voltage - Rated", "Voltage Rating"), lambda t: {schematic_quantity(normalize_description(t), "V")[0]} - {None}, None),
    "tolerance": (("Tolerance",), percent_values, None),
    "power": (("Power (Watts)", "Power - Max"), power_values, same_power),
    "dielectric": (("Temperature Coefficient",), lambda t: {dielectric(t)} - {None}, None),
}


def compare_ratings(checks, cls, component, product, description):
    for rating, (names, parse, same) in RATINGS.items():
        if rating == "dielectric" and cls != "capacitor":
            continue
        listed_text = parameter(product, *names)
        listed = parse(listed_text) if listed_text else set()
        if not listed:
            continue
        same = same or (lambda a, b: a == b)
        sources = []
        try:
            field, declared = field_by_keys(component, RATING_FIELDS[rating])
        except ValueError:
            field, declared = None, None
        if declared:
            sources.append((f"field {field}", declared, parse(declared) if rating != "dielectric" else {dielectric(declared)} - {None}))
        if description:
            text = description if rating != "voltage" else normalize_description(description)
            stated = ({dielectric_in_text(description)} - {None}) if rating == "dielectric" else parse(text)
            if stated:
                sources.append(("Description", description, stated))
        for label, raw, values in sources:
            if not values:
                checks.add(rating, "needs_review", f"{label} {raw!r} cannot be read")
            elif all(any(same(v, l) for l in listed) for v in values):
                checks.add(rating, "pass", f"{label} {rating} matches DigiKey ({listed_text})", digikey_value=listed_text)
            else:
                checks.add(rating, "fail", f"{label} states {rating} {raw!r}, DigiKey lists {listed_text} for this MPN", digikey_value=listed_text)


def audit_component(ref, component, cache_dir):
    item = {"reference": ref, "sheet_path": component.get("sheet_path"), "observation_class": "distributor_catalog",
            "mpn": None, "digikey": None, "checks": [], "suggestions": [], "suggested_fields": {}}
    exemption = component_exemption(ref, component)
    if exemption:
        item.update(status="not_applicable", **exemption)
        return item
    mpns = list(dict.fromkeys(identity_candidates(component, "part_number").values()))
    if len(mpns) != 1:
        item.update(status="not_checked", reason="No single declared MPN to look up (see identification)")
        return item
    mpn = item["mpn"] = mpns[0]
    entry = load(cache_dir, mpn)
    if entry is None:
        item.update(status="needs_review", reason="No cached DigiKey data; run digikey-fetch")
        return item
    item["fetched_at"] = entry.get("fetched_at")
    if entry.get("http_status") != 200:
        item.update(status="needs_review", reason=f"DigiKey lookup failed (HTTP {entry.get('http_status')}): {entry.get('error')}")
        return item
    products = [p for p in entry.get("exact_matches", []) if mpn_key(p.get("ManufacturerProductNumber", "")) == mpn_key(mpn)]
    if not products:
        item.update(status="needs_review", reason="MPN not found on DigiKey (not proof of an error: DigiKey does not list every part). Confirm the MPN with the manufacturer")
        return item
    checks = Checks()
    declared = list(dict.fromkeys(identity_candidates(component, "manufacturer").values()))
    names = sorted({p["Manufacturer"]["Name"] for p in products})
    if len(declared) == 1:
        chosen = manufacturer_matches(declared[0], products)
        if chosen:
            checks.add("manufacturer", "pass", f"Declared manufacturer {declared[0]!r} matches DigiKey {chosen[0]['Manufacturer']['Name']!r}")
            products = chosen
        else:
            checks.add("manufacturer", "needs_review", f"Declared manufacturer {declared[0]!r} differs from DigiKey ({', '.join(names)}). Confirm, or correct the field")
    elif not declared:
        checks.add("manufacturer", "needs_review", f"Manufacturer field is empty; DigiKey lists {' or '.join(names)}")
        item["suggestions"].append(f"Set Manufacturer = {' or '.join(repr(n) for n in names)} (from DigiKey)")
        item["suggested_fields"]["Manufacturer"] = names  # several names = owner must choose
    if len(products) > 1:
        if not same_parameters(products):
            checks.add("match", "needs_review", f"{len(products)} DigiKey listings with different data ({', '.join(names)}); set the Manufacturer field to choose one")
            item.update(checks=checks.items, status="needs_review", reason=checks.items[-1]["reason"])
            return item
        checks.add("match", "pass", f"{len(products)} DigiKey listings ({', '.join(names)}) carry identical parameters; checked once")
    product = products[0]
    status_text, flags = lifecycle(product)
    item["digikey"] = {"manufacturer": product["Manufacturer"]["Name"], "mpn": product.get("ManufacturerProductNumber"),
                       "description": (product.get("Description") or {}).get("DetailedDescription"),
                       "status": status_text, "category": " / ".join(category_path(product)),
                       "package": parameter(product, "Package / Case"),
                       "supplier_device_package": parameter(product, "Supplier Device Package"),
                       "operating_temperature": parameter(product, "Operating Temperature"),
                       "ratings": parameter(product, "Ratings"),
                       "datasheet_url": product.get("DatasheetUrl"), "product_url": product.get("ProductUrl")}
    if status_text.casefold() in ACTIVE and not flags:
        checks.add("lifecycle", "pass", "Active on DigiKey")
    else:
        checks.add("lifecycle", "needs_review", f"DigiKey status: {status_text}{' (' + ', '.join(flags) + ')' if flags else ''}. Check availability or choose an alternative")
        item["suggestions"].append(f"Part is {status_text} on DigiKey; confirm lifecycle or pick a replacement")
    cls = component_class(ref)
    if cls:
        compare_class(checks, cls, product)
        compare_value(checks, cls, component, product)
        try:
            _, description = field_by_keys(component, DESCRIPTION_KEYS)
        except ValueError:
            description = None
        compare_ratings(checks, cls, component, product, description)
    compare_package(checks, component, product)
    pin_count_hint(checks, component, product)
    item["checks"] = checks.items
    statuses = {c["status"] for c in checks.items}
    problems = [c["reason"] for c in checks.items if c["status"] in ("fail", "needs_review")]
    if "fail" in statuses:
        item.update(status="fail", reason="; ".join(problems))
    elif "needs_review" in statuses:
        item.update(status="needs_review", reason="; ".join(problems))
    else:
        item.update(status="pass", reason="DigiKey data agrees with the declared fields")
    return item


def digikey_audit(circuit, cache_dir):
    results = [audit_component(ref, component, cache_dir) for ref, component in sorted(circuit["components"].items())]
    return {
        "schema_version": 1,
        "scope": ("Exact-MPN DigiKey catalog data (cached) compared with the saved schematic: identity, lifecycle, "
                  "category, package vs footprint size, and for R/C/L value and ratings vs Value, Description and "
                  "rating fields. Distributor catalog data, not manufacturer evidence; a pass does not establish suitability."),
        "cache_dir": str(cache_dir),
        "circuit_netlist_sha256": circuit.get("netlist_sha256"),
        "coverage": {s: sum(r["status"] == s for r in results) for s in ("pass", "fail", "needs_review", "not_checked", "not_applicable")},
        "results": results,
    }


def digikey_markdown(report, heading=1):
    def cell(value):
        return str(value or "—").replace("|", "\\|").replace("\n", " ")

    lines = ["#" * heading + " DigiKey cross-check", "", report["scope"], ""]
    lines.extend(f"- {status}: {count}" for status, count in report["coverage"].items())
    lines.extend(["", "| Component | MPN | DigiKey | Status | Reason |", "| --- | --- | --- | --- | --- |"])
    for item in report["results"]:
        dk = item.get("digikey") or {}
        summary = f"{dk.get('manufacturer')}; {dk.get('status')}; {dk.get('package')}" if dk else None
        lines.append("| " + " | ".join(cell(v) for v in (item["reference"], item["mpn"], summary, item["status"], item.get("reason"))) + " |")
    return "\n".join(lines) + "\n"
