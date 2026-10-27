"""Read-only catalog/stock adapters. No supplier data is engineering evidence by itself.

DigiKey discovers parts. JLCPCB resolves explicit C-number mappings. LCSC exact-MPN
lookups are in lcsc.py. Missing access is always unknown.
"""

import re
import difflib
import hashlib
import json
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from .. import credentials, digikey
from .jlc_auth import JLCPCBAuthenticator
from .requirements import OPTIONAL_SEARCH_KEYS

SUPPLIERS = ("digikey", "lcsc", "jlcpcb")


def utc_now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# Trailing legal/descriptive words removed before comparing manufacturer names, so
# "Melexis" = "Melexis Technologies NV" and "Murata" = "Murata Electronics". Only trailing
# words are removed; the exact MPN must still match, so the merge cannot join two parts.
MANUFACTURER_SUFFIXES = {"inc", "incorporated", "corp", "corporation", "co", "company", "ltd", "limited", "llc", "lp",
                         "plc", "gmbh", "ag", "sa", "nv", "bv", "srl", "spa", "kk", "oy", "ab", "technologies",
                         "technology", "electronics", "electronic", "semiconductor", "semiconductors", "components",
                         "international", "group", "holdings", "industries", "usa", "america", "north", "europe", "the",
                         "elektronik", "corporation"}
# Reviewed brand/short-name aliases (key after suffix removal -> canonical key).
MANUFACTURER_ALIASES = {"ti": "texas instruments", "st": "stmicroelectronics", "stmicro": "stmicroelectronics",
                        "adi": "analog devices", "on": "onsemi", "on semiconductor": "onsemi", "on semi": "onsemi",
                        "vishay dale": "vishay", "vishay beyschlag": "vishay", "vishay siliconix": "vishay",
                        "vishay sfernice": "vishay", "vishay bccomponents": "vishay", "vishay bc components": "vishay",
                        "yageo group": "yageo", "nxp": "nxp", "infineon": "infineon",
                        # DigiKey's long names for makers usually written short in schematics
                        "samsung electro mechanics": "samsung", "samsung electromechanics": "samsung", "semco": "samsung",
                        "panasonic industry": "panasonic", "koa speer": "koa", "wurth": "wurth", "wuerth": "wurth",
                        "w rth": "wurth", "nexperia usa": "nexperia", "rohm": "rohm", "taiyo yuden": "taiyo yuden",
                        "maxim": "analog devices", "maxim integrated": "analog devices", "linear technology": "analog devices",
                        "analog devices maxim integrated": "analog devices", "analog devices inc maxim integrated": "analog devices", "te connectivity passive product": "te connectivity",
                        "stmicroelectronics": "stmicroelectronics", "micro commercial co": "mcc", "micro commercial": "mcc"}


def manufacturer_key(name):
    words = re.sub(r"[^a-z0-9]+", " ", str(name or "").casefold()).split()
    while len(words) > 1 and words[-1] in MANUFACTURER_SUFFIXES:
        words.pop()
    key = " ".join(words)
    return MANUFACTURER_ALIASES.get(key, key)


def identity(manufacturer, mpn):
    # Manufacturer (suffix-normalized) + exact MPN; never merge on family prefix, description or package.
    return manufacturer_key(manufacturer), digikey.mpn_key(mpn or "")


def integer(value):
    return value if type(value) is int and value >= 0 else None


def safe_product(product):
    if not isinstance(product, dict):
        raise ValueError("Unsupported DigiKey product shape")
    for key in ("Description", "Manufacturer", "Category", "ProductStatus"):
        if product.get(key) is not None and not isinstance(product[key], dict):
            raise ValueError("Unsupported DigiKey product field shape")
    for key in ("Parameters", "ProductVariations"):
        if product.get(key) is not None and (not isinstance(product[key], list) or any(not isinstance(p, dict) for p in product[key])):
            raise ValueError("Unsupported DigiKey product list shape")
    keys = ("Description", "Manufacturer", "ManufacturerProductNumber", "ProductUrl", "DatasheetUrl",
            "QuantityAvailable", "ProductStatus", "NormallyStocking", "Discontinued", "EndOfLife",
            "Parameters", "Category")
    result = {k: product[k] for k in keys if k in product}
    variation_keys = ("DigiKeyProductNumber", "PackageType", "StandardPricing", "MarketPlace",
                      "QuantityAvailableforPackageType", "MinimumOrderQuantity", "MaxQuantityForDistribution")
    result["ProductVariations"] = [{k: v[k] for k in variation_keys if k in v}
                                    for v in product.get("ProductVariations") or []]
    return result


def parametric_filters(body):
    """Parametric filter options offered by a keyword response (FilterOptions.ParametricFilters):
    [{"parameter_id", "parameter", "values": [{"id", "name", "count"}]}]. Read leniently because
    the v4 field names were taken from the API reference, not yet from a recorded response."""
    options = (body.get("FilterOptions") or {}) if isinstance(body, dict) else {}
    result = []
    for item in options.get("ParametricFilters") or []:
        if not isinstance(item, dict):
            continue
        pid = item.get("ParameterId", item.get("Id"))
        name = item.get("ParameterName") or item.get("Parameter") or item.get("ParameterText")
        values = []
        for value in item.get("FilterValues") or []:
            if isinstance(value, dict):
                vid = value.get("ValueId", value.get("Id"))
                vname = value.get("ValueName") or value.get("Value") or value.get("Name") or vid
                if vid is not None:
                    values.append({"id": str(vid), "name": str(vname), "count": integer(value.get("ProductCount"))})
        if pid is not None and name and values:
            result.append({"parameter_id": pid, "parameter": str(name), "values": values})
    return result


class DigiKeyCatalog:
    """Bounded, paginated search; separate cache from the checker's exact-MPN evidence."""

    def __init__(self, cache, client=None):
        self.cache, self.client = Path(cache), client

    def page(self, query, offset, limit=50, *, refresh=False, exact=False, filters=None, sort=None):
        key = {"query": query, "offset": offset, "limit": limit, "exact": exact, "locale": digikey.LOCALE}
        if filters is not None:
            key["filters"] = filters
        if sort is not None:
            key["sort"] = sort
        digest = hashlib.sha256(json.dumps(key, sort_keys=True).encode()).hexdigest()
        path = self.cache / f"{digest}.json"
        if not refresh and path.is_file():
            entry = json.loads(path.read_text(encoding="utf-8"))
            if entry.get("schema_version") != 1 or entry.get("request") != key:
                raise ValueError("Unsupported sourcing cache entry")
            return entry
        if self.client is None:
            raise LookupError("No sourcing search cached; rerun with --online, or import a recorded snapshot")
        options = {"limit": limit, "offset": offset}
        if filters is not None:
            options["filters"] = filters
        if sort is not None:
            options["sort"] = sort
        status, body = self.client.keyword(query, **options)
        dropped = []
        if status == 400 and (sort is not None or any(k in (filters or {}) for k in OPTIONAL_SEARCH_KEYS)):
            # Enum spellings for these options are not yet confirmed live; keep the search alive without them.
            dropped = [k for k in OPTIONAL_SEARCH_KEYS if k in (filters or {})] + (["SortOptions"] if sort is not None else [])
            options["filters"] = {k: v for k, v in (filters or {}).items() if k not in OPTIONAL_SEARCH_KEYS} or None
            options.pop("sort", None)
            if options["filters"] is None:
                options.pop("filters")
            status, body = self.client.keyword(query, **options)
        if status != 200 or not isinstance(body, dict):
            raise ValueError(f"DigiKey search failed (HTTP {status}); previous cache preserved")
        products = body.get("ExactMatches") or []
        if not isinstance(products, list) or not isinstance(body.get("Products") or [], list):
            raise ValueError("Unsupported DigiKey search response shape")
        # Even an exact query may return its exact identity under Products.
        # stock_report applies manufacturer+MPN equality to both response lists.
        products = products + (body.get("Products") or [])
        entry = {"schema_version": 1, "request": key, "observed_at": utc_now(), "dropped_options": dropped,
                 "products_count": body.get("ProductsCount"), "products": [safe_product(p) for p in products],
                 "parametric_filters": parametric_filters(body)}
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(entry, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        return entry

    def search(self, query, *, max_pages=3, refresh=False, filters=None, sort=None):
        if type(max_pages) is not int or not 1 <= max_pages <= 20:
            raise ValueError("DigiKey max_pages must be 1–20")
        products, warnings = [], []
        for page in range(max_pages):
            try:
                entry = self.page(query, page * 50, refresh=refresh, filters=filters, sort=sort)
            except (LookupError, OSError, ValueError):
                warnings.append(f"DigiKey search page {page + 1} unavailable; search coverage is incomplete")
                break
            if entry.get("dropped_options"):
                message = ("DigiKey rejected search options " + ", ".join(entry["dropped_options"]) +
                           " (HTTP 400); retried without them, local screening unchanged")
                if message not in warnings:
                    warnings.append(message)
            products.extend({"product": p, "observed_at": entry["observed_at"]} for p in entry["products"])
            total = integer(entry.get("products_count"))
            if not entry["products"] or (total is not None and (page + 1) * 50 >= total):
                break
        else:
            warnings.append(f"Search stopped at the {max_pages}-page budget; more candidates may exist")
        return products, warnings


class CategoryIndex:
    """DigiKey category name -> CategoryId, from a cached copy of the category tree.

    The tree changes rarely: it is fetched only with --online and when the cache is
    missing or older than max_age_days, so offline reruns reuse the same IDs.
    """
    def __init__(self, cache, client=None, max_age_days=30):
        self.path, self.client, self.max_age_days = Path(cache) / "categories.json", client, max_age_days

    def _load(self, refresh):
        entry = json.loads(self.path.read_text(encoding="utf-8")) if self.path.is_file() else None
        stale = True
        if entry:
            try:
                observed = datetime.fromisoformat(entry["observed_at"])
                stale = (datetime.now(timezone.utc) - observed).days >= self.max_age_days
            except (KeyError, TypeError, ValueError):
                stale = True
        if refresh and self.client is not None and (entry is None or stale):
            status, body = self.client.categories()
            if status == 200 and isinstance(body, dict) and isinstance(body.get("Categories"), list):
                entry = {"schema_version": 1, "observed_at": utc_now(), "categories": body["Categories"]}
                self.path.parent.mkdir(parents=True, exist_ok=True)
                self.path.write_text(json.dumps(entry, ensure_ascii=False) + "\n", encoding="utf-8")
            elif entry is None:
                raise ValueError(f"DigiKey category list unavailable (HTTP {status})")
        if entry is None:
            raise LookupError("No cached DigiKey category list; run once with --online")
        return entry["categories"]

    def resolve(self, name, *, refresh=False):
        """Return (category_id or None, warning or None). Ambiguous or unknown names are not guessed."""
        try:
            tree = self._load(refresh)
        except (LookupError, OSError, ValueError, KeyError, TypeError) as error:
            return None, f"Category '{name}' not sent to DigiKey ({error}); screened locally only"
        wanted, ids, stack, known = " ".join(name.split()).casefold(), set(), list(tree), {}
        while stack:
            node = stack.pop()
            if not isinstance(node, dict):
                continue
            node_id = node.get("CategoryId", node.get("Id"))
            label = " ".join(str(node.get("Name", "")).split())
            if type(node_id) is int:
                known.setdefault(label, node_id)
            if label.casefold() == wanted and type(node_id) is int:
                ids.add(node_id)
            # Live v4 /search/categories uses "Children"; product records use "ChildCategories".
            stack.extend(node.get("Children") or node.get("ChildCategories") or [])
        if len(ids) == 1:
            return ids.pop(), None
        reason = "is ambiguous in" if ids else "was not found in"
        hint = ""
        if not ids:
            close = difflib.get_close_matches(name, list(known), n=3, cutoff=0.6)
            close += [label for label in known if wanted in label.casefold() and label not in close][:3 - len(close)]
            if close:
                hint = " Did you mean: " + "; ".join(f"{label} ({known[label]})" for label in close) + "?"
        return None, f"Category '{name}' {reason} the DigiKey category list; use a numeric CategoryId. Screened locally only.{hint}"


def digikey_offers(product, observed_at):
    manufacturer = (product.get("Manufacturer") or {}).get("Name")
    status, flags = digikey.lifecycle(product)
    base = {"supplier": "digikey", "manufacturer": manufacturer, "mpn": product.get("ManufacturerProductNumber"),
            "url": product.get("ProductUrl"), "observed_at": observed_at,
            "normally_stocking": product.get("NormallyStocking"), "lifecycle": status,
            "discontinued": product.get("Discontinued"), "end_of_life": product.get("EndOfLife"),
            "origin": "DigiKey Product Information v4", "identity_review": False}
    offers = []
    for v in product.get("ProductVariations") or []:
        # Marketplace inventory belongs to a different seller and is not counted as DigiKey stock.
        offers.append({**base, "code": v.get("DigiKeyProductNumber"),
                       "packaging": (v.get("PackageType") or {}).get("Name"),
                       "stock": integer(v.get("QuantityAvailableforPackageType")),
                       "moq": integer(v.get("MinimumOrderQuantity")),
                       "max_order": integer(v.get("MaxQuantityForDistribution")),
                       "marketplace": v.get("MarketPlace"),
                       "price_breaks": v.get("StandardPricing") or [], "currency": "USD"})
    if not offers:
        offers.append({**base, "code": None, "stock": integer(product.get("QuantityAvailable")),
                       "moq": None, "max_order": None, "marketplace": None,
                       "price_breaks": [], "currency": "USD"})
    return offers


def load_offers(path):
    """Interchange for manually recorded/official-export stock; never reused as electrical specs."""
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(data, dict) or data.get("schema_version") != 1 or not isinstance(data.get("offers"), list):
        raise ValueError("Stock import needs schema_version=1 and offers list")
    result = []
    allowed = {"supplier", "manufacturer", "mpn", "code", "stock", "moq", "observed_at", "url", "normally_stocking",
               "lifecycle", "discontinued", "end_of_life", "origin", "identity_review", "max_order", "marketplace", "library_type", "order_multiple"}
    for item in data["offers"]:
        if not isinstance(item, dict) or set(item) - allowed:
            raise ValueError("Unknown fields in stock observation")
        if item.get("supplier") not in SUPPLIERS or any(not isinstance(item.get(k), str) or not item[k].strip()
                                                       for k in ("manufacturer", "mpn", "url", "observed_at")):
            raise ValueError("Stock observations need supplier, manufacturer, exact mpn, url and observed_at")
        for key in ("stock", "moq", "max_order", "order_multiple"):
            if item.get(key) is not None and integer(item[key]) is None:
                raise ValueError(f"Stock observation {key} must be a nonnegative integer or null")
        for key in ("normally_stocking", "discontinued", "end_of_life", "identity_review", "marketplace"):
            if item.get(key) is not None and type(item[key]) is not bool:
                raise ValueError(f"Stock observation {key} must be boolean or null")
        result.append({**item, "origin": item.get("origin") or "Imported observation (user supplied)"})
    return result


class JLCPCBStock:
    """Thin stdlib adapter using the old project's unchanged, vector-tested signer.

    Only getComponentDetailByCode is exposed. Endpoint/field contract inherited
    from the supplied older agent; live compatibility is not yet tested here.
    """
    def __init__(self, app_id, access_key, secret_key, opener=urllib.request.urlopen):
        self.auth = JLCPCBAuthenticator(app_id, access_key, secret_key)
        self.opener = opener
        self.last_call = 0.0

    def lookup(self, mapping):
        path = "/overseas/openapi/component/getComponentDetailByCode"
        body = json.dumps({"componentCodes": [mapping["jlcpcb"]]}, separators=(",", ":"))
        for attempt in range(2):
            wait = 1.0 - (time.monotonic() - self.last_call)
            if wait > 0:
                time.sleep(wait)
            header = self.auth.authorize("POST", path, body).header
            request = urllib.request.Request("https://open.jlcpcb.com" + path, data=body.encode(), headers={
                "Authorization": header, "Content-Type": "application/json", "Accept": "application/json"})
            self.last_call = time.monotonic()
            try:
                with self.opener(request, timeout=30) as response:
                    raw = json.load(response)
                break
            except urllib.error.HTTPError as error:
                status = error.code
                error.close()
                if attempt == 0 and (status == 429 or status >= 500):
                    continue
                raise ValueError(f"JLCPCB stock lookup failed (HTTP {status})") from None
            except (OSError, ValueError):
                if attempt == 0:
                    continue
                raise ValueError("JLCPCB stock lookup failed (transport or invalid JSON)") from None
        if not isinstance(raw, dict) or raw.get("success") is False:
            raise ValueError("JLCPCB returned an unsuccessful or unsupported response")
        data = raw.get("data", raw)
        if isinstance(data, dict):
            data = next((data[k] for k in ("componentDetails", "list", "items") if isinstance(data.get(k), list)), None)
        if not isinstance(data, list):
            raise ValueError("JLCPCB returned an unsupported detail shape")
        if any(not isinstance(d, dict) for d in data):
            raise ValueError("JLCPCB returned invalid detail records")
        matches = [d for d in data if d.get("componentCode") == mapping["jlcpcb"]
                   and digikey.mpn_key(d.get("componentModel", "")) == digikey.mpn_key(mapping["mpn"])]
        if len(matches) != 1:
            raise ValueError("JLCPCB code/MPN mapping not uniquely confirmed; stock stays unknown")
        d = matches[0]
        return {"supplier": "jlcpcb", "manufacturer": mapping["manufacturer"], "mpn": mapping["mpn"],
                "code": mapping["jlcpcb"], "stock": integer(d.get("stockCount")), "moq": None,
                "observed_at": utc_now(), "normally_stocking": None, "lifecycle": None,
                "url": "https://jlcpcb.com/partdetail/" + mapping["jlcpcb"], "library_type": d.get("libraryType"),
                "origin": "JLCPCB Open API detail", "identity_review": True}


def configured_jlc():
    keys = ("JLCPCB_APP_ID", "JLCPCB_ACCESS_KEY", "JLCPCB_SECRET_KEY")
    values = [credentials.get(k) for k in keys]
    return JLCPCBStock(*values) if all(values) else None
