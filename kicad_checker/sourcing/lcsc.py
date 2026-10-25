"""Official LCSC API, exact-MPN procurement lookup only.

Contract checked 2026-10-24:
https://www.lcsc.com/agent/authentication?urlKey=authentication
https://www.lcsc.com/agent/docs?id=keywordSearch
No internal webshop endpoint or scraping fallback. Response examples are not live validation.
"""

import hashlib
import json
import secrets
import time
import urllib.error
import urllib.parse
import urllib.request

from .. import credentials
from .providers import identity, integer, utc_now


def auth_headers(key, secret, payload, *, nonce=None, timestamp=None):
    """Match the vendor's recursive scalar/string JSON signing algorithm."""
    def convert(value):
        if value is None:
            return ""
        if isinstance(value, dict):
            return json.dumps({k: convert(value[k]) for k in sorted(value)}, separators=(",", ":"), ensure_ascii=False)
        if isinstance(value, list):
            return json.dumps([convert(v) for v in value], separators=(",", ":"), ensure_ascii=False)
        return str(value)

    nonce = nonce or secrets.token_hex(8)
    timestamp = str(int(time.time()) if timestamp is None else timestamp)
    base = f"key={key}&nonce={nonce}&secret={secret}&timestamp={timestamp}"
    params = [(k, convert(v)) for k, v in sorted(payload.items()) if v is not None]
    if params:
        base += "&" + urllib.parse.urlencode(params)
    return {"key": key, "nonce": nonce, "timestamp": timestamp,
            "signature": hashlib.sha256(base.encode()).hexdigest()}


class LCSCStock:
    def __init__(self, key, secret, opener=urllib.request.urlopen):
        self.key, self.secret, self.opener = key, secret, opener
        self.last_call = 0.0

    def lookup(self, manufacturer, mpn):
        # The API uses 1-based offsets. Exact matching is done locally, not assumed from keyword search.
        payload = {"keyword": mpn, "offset": "1", "limit": "100", "returnInformation": "All", "currency": "USD", "inStockOnly": "false"}
        url = "https://api.lcsc.com/rest/api/agent/product/v1/keywordsearch?" + urllib.parse.urlencode(payload)
        for attempt in range(2):
            wait = 1.05 - (time.monotonic() - self.last_call)
            if wait > 0:
                time.sleep(wait)
            request = urllib.request.Request(url, headers={**auth_headers(self.key, self.secret, payload),
                                                          "Accept": "application/json", "Content-Type": "application/json"})
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
                raise ValueError(f"LCSC stock lookup failed (HTTP {status})") from None
            except (OSError, ValueError):
                if attempt == 0:
                    continue
                raise ValueError("LCSC stock lookup failed (transport or invalid JSON)") from None
        if not isinstance(raw, dict) or raw.get("success") is not True or raw.get("code") != 200:
            raise ValueError("LCSC returned an unsuccessful response")
        result = raw.get("result")
        if not isinstance(result, dict) or not isinstance(result.get("products"), list):
            raise ValueError("LCSC returned an unsupported keyword response shape")
        offers = []
        key = identity(manufacturer, mpn)
        observed = utc_now()
        for p in result["products"]:
            if not isinstance(p, dict) or not isinstance(p.get("manufacturer") or {}, dict):
                raise ValueError("LCSC returned invalid product records")
            if identity((p.get("manufacturer") or {}).get("name"), p.get("manufacturerProductNumber")) != key:
                continue
            pricing = p.get("productPrice") or {}
            if not isinstance(pricing, dict):
                raise ValueError("LCSC returned invalid stock records")
            offers.append({"supplier": "lcsc", "manufacturer": p["manufacturer"]["name"],
                           "mpn": p["manufacturerProductNumber"], "code": p.get("lcscProductNumber"),
                           "stock": integer(pricing.get("quantityAvailable")), "moq": integer(p.get("minimumOrderQuantity")),
                           "order_multiple": integer(p.get("incrementQuantity")),
                           "observed_at": observed, "url": p.get("productDetailURL"), "normally_stocking": None,
                           "lifecycle": None, "identity_review": False, "origin": "LCSC official API keywordsearch"})
        # A bounded page without a match cannot establish that LCSC does not sell the MPN.
        incomplete = bool(result.get("nextOffset")) or (integer(result.get("productCount")) or 0) > 100
        return offers, incomplete


def configured_lcsc():
    values = [credentials.get(k) for k in ("LCSC_APP_KEY", "LCSC_APP_SECRET")]
    return LCSCStock(*values) if all(values) else None
