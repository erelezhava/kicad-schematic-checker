#!/usr/bin/env python3
"""Capture raw DigiKey Product Information v4 responses for a few MPNs.

One-off helper: the saved JSON becomes test fixtures for the DigiKey lookup.
Standard library only. Uses your own keys from .env / the environment
(see .env.example); they are never written to the output.

    python3 tools/digikey_capture.py --output runs/digikey-samples \
        GCM21BR71E225KA73L AF0402FR-0710KL LP591233MDRVREP "LTC4331HUFD#PBF" 5040500691
"""

import argparse
import json
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from kicad_checker import credentials  # noqa: E402

API = "https://api.digikey.com"
LOCALE = {"X-DIGIKEY-Locale-Site": "US", "X-DIGIKEY-Locale-Language": "en", "X-DIGIKEY-Locale-Currency": "USD"}


def token(client_id, secret):
    body = urllib.parse.urlencode({"client_id": client_id, "client_secret": secret,
                                   "grant_type": "client_credentials"}).encode()
    request = urllib.request.Request(f"{API}/v1/oauth2/token", data=body,
                                     headers={"Content-Type": "application/x-www-form-urlencoded"})
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.load(response)["access_token"]


def call(method, path, headers, payload=None):
    data = json.dumps(payload).encode() if payload is not None else None
    request = urllib.request.Request(API + path, data=data, method=method,
                                     headers={**headers, "Content-Type": "application/json", "Accept": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return response.status, json.load(response)
    except urllib.error.HTTPError as error:
        text = error.read().decode(errors="replace")
        try:
            return error.code, json.loads(text)
        except json.JSONDecodeError:
            return error.code, {"raw": text}


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("mpns", nargs="+")
    parser.add_argument("--output", required=True, help="New or existing folder for the JSON files (keep it out of git)")
    args = parser.parse_args()
    try:
        client_id, secret = credentials.require(("DIGIKEY_CLIENT_ID", "DIGIKEY_CLIENT_SECRET"), "DigiKey")
    except ValueError as error:
        sys.exit(str(error))
    out = Path(args.output)
    out.mkdir(parents=True, exist_ok=True)
    headers = {"X-DIGIKEY-Client-Id": client_id, "Authorization": f"Bearer {token(client_id, secret)}", **LOCALE}
    for mpn in args.mpns:
        safe = "".join(c if c.isalnum() or c in "-_." else "_" for c in mpn)
        quoted = urllib.parse.quote(mpn, safe="")
        status, details = call("GET", f"/products/v4/search/{quoted}/productdetails", headers)
        (out / f"{safe}.productdetails.json").write_text(json.dumps({"status": status, "body": details}, indent=2))
        status_k, keyword = call("POST", "/products/v4/search/keyword", headers, {"Keywords": mpn, "Limit": 10, "Offset": 0})
        (out / f"{safe}.keyword.json").write_text(json.dumps({"status": status_k, "body": keyword}, indent=2))
        print(f"{mpn}: productdetails HTTP {status}, keyword HTTP {status_k}")
    print(f"Saved to {out.resolve()}")


if __name__ == "__main__":
    main()
