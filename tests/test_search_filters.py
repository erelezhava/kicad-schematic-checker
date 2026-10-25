"""DigiKey-side search narrowing (phase A): category IDs, stock/marketplace options, sort, 400 fallback."""

import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from kicad_checker import digikey
from kicad_checker.cli import main
from kicad_checker.sourcing.providers import CategoryIndex, DigiKeyCatalog
from kicad_checker.sourcing.requirements import api_category_name, search_filters, search_sort, validate_request

ROOT = Path(__file__).resolve().parents[1]
RESISTOR = {"kind": "resistor", "value": "10k", "package": "0402", "tolerance_percent": 1}
# Shape of the live v4 /search/categories response (2026-10-25): nested under "Children".
TREE = [{"CategoryId": 2, "Name": "Resistors", "ProductCount": 1, "Children": [
            {"CategoryId": 52, "ParentId": 2, "Name": "Chip Resistor - Surface Mount", "ProductCount": 1, "Children": []},
            {"CategoryId": 53, "ParentId": 2, "Name": "Through Hole Resistors", "ProductCount": 1, "Children": []},
            {"CategoryId": 54, "ParentId": 2, "Name": "Accessories", "ProductCount": 1, "Children": []}]},
        {"CategoryId": 3, "Name": "Capacitors", "ProductCount": 1, "Children": [
            {"CategoryId": 60, "ParentId": 3, "Name": "Ceramic Capacitors", "ProductCount": 1, "Children": []},
            {"CategoryId": 61, "ParentId": 3, "Name": "Accessories", "ProductCount": 1, "Children": []}]}]


def product():
    data = json.loads((ROOT / "tests/fixtures/digikey/AF0402FR-0710KL.keyword.json").read_text())
    return data["body"]["ExactMatches"][0]


class FakeClient:
    def __init__(self, reject_options=False):
        self.reject_options, self.keyword_calls, self.category_calls = reject_options, [], 0

    def categories(self):
        self.category_calls += 1
        return 200, {"Categories": TREE}

    def keyword(self, query, **options):
        self.keyword_calls.append(options)
        if self.reject_options and ("sort" in options or "SearchOptions" in (options.get("filters") or {})):
            return 400, {"detail": "SENSITIVE"}
        return 200, {"ProductsCount": 1, "Products": [product()]}


class Response(io.BytesIO):
    def __init__(self, data):
        super().__init__(json.dumps(data).encode())
        self.status = 200


class FilterTests(unittest.TestCase):
    def test_validated_request_builds_stock_marketplace_and_sort_options(self):
        req = validate_request({**RESISTOR, "quantity": 100, "min_stock": 10000})
        self.assertEqual(search_filters(req, 52), {
            "CategoryFilter": [{"Id": "52"}], "MinimumQuantityAvailable": 10000,
            "MarketPlaceFilter": "ExcludeMarketPlace", "SearchOptions": ["InStock", "NormallyStocking"]})
        self.assertEqual(search_sort(req), {"Field": "QuantityAvailable", "SortOrder": "Descending"})
        relaxed = validate_request({**RESISTOR, "normally_stocking": False, "quantity": 20000})
        self.assertEqual(search_filters(relaxed)["SearchOptions"], ["InStock"])
        self.assertEqual(search_filters(relaxed)["MinimumQuantityAvailable"], 20000)
        # An explicit numeric category always wins over a resolved name.
        self.assertEqual(search_filters(validate_request({**RESISTOR, "category": 7}), 52)["CategoryFilter"], [{"Id": "7"}])

    def test_category_inference_only_when_unambiguous(self):
        self.assertEqual(api_category_name(validate_request(RESISTOR)), "Chip Resistor - Surface Mount")
        self.assertIsNone(api_category_name(validate_request({**RESISTOR, "package": "Axial"})))
        cap = {"kind": "capacitor", "value": "100n", "package": "0402", "tolerance_percent": 10}
        self.assertIsNone(api_category_name(validate_request(cap)))
        self.assertEqual(api_category_name(validate_request({**cap, "dielectric": "X7R"})), "Ceramic Capacitors")
        self.assertIsNone(api_category_name(validate_request({"kind": "diode", "package": "SOD-123"})))
        self.assertEqual(api_category_name({"kind": "mosfet", "category": "FETs, MOSFETs - Single"}), "FETs, MOSFETs - Single")
        self.assertIsNone(api_category_name({"kind": "mosfet", "category": 278}))

    def test_category_index_resolves_caches_and_never_guesses(self):
        with tempfile.TemporaryDirectory() as folder:
            self.assertIsNone(CategoryIndex(folder).resolve("Ceramic Capacitors")[0])  # offline, no cache
            client = FakeClient()
            index = CategoryIndex(folder, client)
            self.assertEqual(index.resolve("ceramic  capacitors", refresh=True), (60, None))
            self.assertEqual(index.resolve("Chip Resistor - Surface Mount", refresh=True), (52, None))
            self.assertEqual(client.category_calls, 1)  # fresh cache is reused even online
            self.assertEqual(CategoryIndex(folder).resolve("Ceramic Capacitors"), (60, None))  # offline rerun
            category, warning = index.resolve("Accessories")
            self.assertIsNone(category)
            self.assertIn("ambiguous", warning)
            self.assertIn("Did you mean: Ceramic Capacitors (60)", index.resolve("Ceramic Capacitor")[1])
            category, warning = index.resolve("No Such Category")
            self.assertIsNone(category)
            self.assertIn("not found", warning)

    def test_rejected_options_are_dropped_once_and_reported_without_server_text(self):
        req = validate_request(RESISTOR)
        filters, sort = search_filters(req, 52), search_sort(req)
        with tempfile.TemporaryDirectory() as folder:
            client = FakeClient(reject_options=True)
            records, warnings = DigiKeyCatalog(folder, client).search("resistor", max_pages=1, filters=filters, sort=sort)
            self.assertEqual(len(records), 1)
            retry = client.keyword_calls[1]
            self.assertNotIn("sort", retry)
            self.assertEqual(retry["filters"], {"CategoryFilter": [{"Id": "52"}], "MinimumQuantityAvailable": 1000})
            self.assertTrue(any("SearchOptions, MarketPlaceFilter, SortOptions" in w for w in warnings))
            self.assertNotIn("SENSITIVE", str(warnings))
            # Cached under the requested options, so the offline rerun matches.
            again, warnings = DigiKeyCatalog(folder).search("resistor", max_pages=1, filters=filters, sort=sort)
            self.assertEqual(len(again), 1)
            self.assertTrue(warnings)

    def test_client_sends_sort_and_reads_category_tree(self):
        sent = []
        def opener(request, timeout):
            if request.full_url.endswith("/token"):
                return Response({"access_token": "FAKE-TOKEN"})
            sent.append((request.get_method(), request.full_url, request.data and json.loads(request.data)))
            return Response({"Categories": TREE} if request.full_url.endswith("/categories") else {"Products": []})
        client = digikey.DigiKeyClient("FAKE-ID", "FAKE-SECRET", min_interval=0, opener=opener)
        client.keyword("10k", limit=50, sort={"Field": "QuantityAvailable", "SortOrder": "Descending"})
        self.assertEqual(sent[0][2]["SortOptions"], {"Field": "QuantityAvailable", "SortOrder": "Descending"})
        status, body = client.categories()
        self.assertEqual((status, sent[1][0]), (200, "GET"))
        self.assertTrue(sent[1][1].endswith("/products/v4/search/categories"))

    def test_online_cli_resolves_category_and_sends_filters(self):
        client = FakeClient()
        with tempfile.TemporaryDirectory() as folder, \
                patch("kicad_checker.digikey.load_credentials", return_value=("ID", "SECRET")), \
                patch("kicad_checker.digikey.DigiKeyClient", return_value=client), \
                patch("kicad_checker.sourcing.cli.configured_lcsc", return_value=None), \
                patch("kicad_checker.sourcing.cli.configured_jlc", return_value=None), \
                redirect_stdout(io.StringIO()):
            main(["parts-search", str(ROOT / "examples/sourcing/resistor.json"), "--online", "--max-pages", "1",
                  "--cache", str(Path(folder) / "cache"), "--output", str(Path(folder) / "out")])
            sent = client.keyword_calls[0]
            self.assertEqual(sent["filters"]["CategoryFilter"], [{"Id": "52"}])
            self.assertEqual(sent["filters"]["MinimumQuantityAvailable"], 10000)
            self.assertEqual(sent["sort"]["Field"], "QuantityAvailable")
            report = json.loads((Path(folder) / "out/sourcing.json").read_text())
            self.assertEqual(report["search"]["category_id"], 52)


if __name__ == "__main__":
    unittest.main()
