"""Offline sourcing tests. Mutated MPNs are synthetic test identities, never real alternatives."""

import hashlib
import io
import json
import tempfile
import unittest
import urllib.error
from contextlib import redirect_stdout, redirect_stderr
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from kicad_checker import digikey
from kicad_checker.cli import main
from kicad_checker.sourcing.cli import jlc_observations, lcsc_observations
from kicad_checker.sourcing.jlc_auth import JLCPCBAuthenticator
from kicad_checker.sourcing.lcsc import LCSCStock, auth_headers
from kicad_checker.sourcing.providers import DigiKeyCatalog, JLCPCBStock, digikey_offers, load_offers
from kicad_checker.sourcing.requirements import package_matches, validate_request
from kicad_checker.sourcing.service import fresh, markdown, search_report, stock_report

ROOT = Path(__file__).resolve().parents[1]
NOW = datetime(2026, 10, 24, 14, tzinfo=timezone.utc)
AT = NOW.isoformat()
REQ = {"kind": "resistor", "value": "10k", "package": "0402", "tolerance_percent": 1, "automotive": True}


def product(mpn=None):
    data = json.loads((ROOT / "tests/fixtures/digikey/AF0402FR-0710KL.keyword.json").read_text())
    p = data["body"]["ExactMatches"][0]
    if mpn:
        p["ManufacturerProductNumber"] = mpn
    return p


def maker(p, name):
    p["Manufacturer"] = {**(p.get("Manufacturer") or {}), "Name": name}
    return p


def record(p=None, at=AT):
    return {"product": p or product(), "observed_at": at}


def change(p, name, value):
    for item in p["Parameters"]:
        if item["ParameterText"] == name:
            item["ValueText"] = value
            return p
    p["Parameters"].append({"ParameterText": name, "ValueText": value})
    return p


class Response(io.BytesIO):
    def __init__(self, data):
        super().__init__(json.dumps(data).encode())
        self.status = 200


class ScreeningTests(unittest.TestCase):
    def report(self, records=None, req=None, **kw):
        return search_report(req or REQ, records or [record()], now=NOW, **kw)

    def test_three_packaging_variants_are_one_alternative(self):
        report = self.report()
        self.assertEqual(report["qualified_candidates"], 1)
        self.assertEqual(report["shortfall"], 2)
        self.assertEqual(len(report["results"][0]["suppliers"]["digikey"]["offers"]), 3)
        self.assertEqual(report["results"][0]["suppliers"]["digikey"]["max_reported_stock"], 60564)

    def test_three_distinct_mpns_count_and_original_is_excluded(self):
        records = [record(maker(product(f"SYNTHETIC-{i}"), f"Maker {i}")) for i in range(4)]
        report = self.report(records, {**REQ, "exclude_mpns": ["SYNTHETIC-0"]})
        self.assertEqual(report["status"], "target_met")
        self.assertEqual(report["qualified_candidates"], 3)
        self.assertEqual(report["distinct_manufacturers"], 3)
        self.assertTrue(all(r["engineering_status"] == "requires_review" for r in report["results"]))

    def test_wrong_value_package_and_tolerance_rejected(self):
        for parameter, value in (("Resistance", "100 kOhms"), ("Package / Case", "0603 (1608 Metric)"), ("Tolerance", "±5%")):
            with self.subTest(parameter=parameter):
                row = self.report([record(change(product(), parameter, value))])["results"][0]
                self.assertEqual(row["catalog_status"], "rejected")
                self.assertFalse(row["counts_toward_target"])

    def test_no_qualification_or_normally_stocked_is_not_a_pass(self):
        p = change(product(), "Features", "Automotive")  # marketing word alone is insufficient
        report = self.report([record(p)])
        self.assertEqual(report["results"][0]["catalog_status"], "needs_review")
        p = product()
        p.pop("NormallyStocking")
        self.assertEqual(self.report([record(p)])["qualified_candidates"], 0)

    def test_discontinued_obsolete_and_nrnd_never_count(self):
        for field, value in (("Discontinued", True), ("EndOfLife", True), ("ProductStatus", {"Status": "Obsolete"}), ("ProductStatus", {"Status": "Not For New Designs"})):
            p = product()
            p[field] = value
            self.assertEqual(self.report([record(p)])["qualified_candidates"], 0)

    def test_freshness_missing_future_naive_and_expired(self):
        for at in (None, "broken", "2026-10-24T14:00:00", (NOW + timedelta(seconds=1)).isoformat(), (NOW - timedelta(hours=25)).isoformat()):
            with self.subTest(at=at):
                self.assertFalse(fresh(at, 24, NOW))
                self.assertEqual(self.report([record(at=at)])["qualified_candidates"], 0)
        self.assertTrue(fresh((NOW - timedelta(hours=24)).isoformat(), 24, NOW))

    def test_moq_stock_distribution_and_marketplace(self):
        for overrides in ({"MinimumOrderQuantity": 10000}, {"QuantityAvailableforPackageType": 99},
                          {"MaxQuantityForDistribution": 99}, {"MarketPlace": True}, {"MarketPlace": None}):
            p = product()
            p["ProductVariations"] = [{**v, **overrides} for v in p["ProductVariations"]]
            self.assertEqual(self.report([record(p)])["qualified_candidates"], 0)

    def test_missing_variation_stock_not_filled_from_total(self):
        p = product()
        for v in p["ProductVariations"]:
            v.pop("QuantityAvailableforPackageType")
        self.assertEqual(self.report([record(p)])["qualified_candidates"], 0)

    def test_conflicting_duplicate_specifications_are_visible(self):
        report = self.report([record(), record(change(product(), "Resistance", "11 kOhms"))])
        self.assertEqual(report["qualified_candidates"], 0)
        self.assertEqual(report["results"][0]["catalog_status"], "needs_review")
        self.assertIn("Conflicting", report["results"][0]["issues"][0])

    def test_duplicate_listings_do_not_increase_count(self):
        self.assertEqual(self.report([record(), record()])["qualified_candidates"], 1)

    def test_newer_zero_stock_overrides_older_positive_observation(self):
        newer = product()
        for v in newer["ProductVariations"]:
            v["QuantityAvailableforPackageType"] = 0
        records = [record(at=(NOW - timedelta(hours=1)).isoformat()), record(newer)]
        row = self.report(records)["results"][0]
        self.assertFalse(row["counts_toward_target"])
        self.assertEqual(row["suppliers"]["digikey"]["max_reported_stock"], 0)

    def test_conflicting_same_time_stock_observations_cannot_pass(self):
        p = product()
        for v in p["ProductVariations"]:
            v["QuantityAvailableforPackageType"] = 0
        report = self.report([record(), record(p)])
        self.assertEqual(report["qualified_candidates"], 0)
        issues = report["results"][0]["suppliers"]["digikey"]["offers"][0]["issues"]
        self.assertIn("conflicting", " ".join(issues))

    def test_identity_is_exact_and_supplier_inventories_separate(self):
        offer = {"supplier": "lcsc", "manufacturer": "YAGEO", "mpn": "AF0402FR-0710KL", "stock": 10,
                 "moq": 1, "observed_at": AT, "url": "https://www.lcsc.com/"}
        other = {**offer, "mpn": "AF0402FR-0710KL-OTHER", "stock": 999999}
        row = self.report(imported_offers=[offer, other])["results"][0]
        self.assertEqual(row["suppliers"]["lcsc"]["max_reported_stock"], 10)
        self.assertEqual(row["suppliers"]["digikey"]["max_reported_stock"], 60564)
        self.assertEqual(row["suppliers"]["jlcpcb"]["status"], "unknown")
        self.assertIn("unknown", markdown(self.report(imported_offers=[offer])))

    def test_catalog_kind_does_not_accept_resistor_arrays(self):
        p = product()
        p["Category"]["ChildCategories"][0]["Name"] = "Resistor Networks, Arrays"
        self.assertEqual(self.report([record(p)])["results"][0]["catalog_status"], "rejected")

    def test_numeric_power_constraints_use_conservative_fraction(self):
        constraint = {"parameter": "Power (Watts)", "op": "min", "value": "0.063", "unit": "W"}
        report = self.report(req={**REQ, "constraints": [constraint]})
        self.assertEqual(report["qualified_candidates"], 0)  # 1/16W = 0.0625W
        constraint["value"] = "0.0625"
        self.assertEqual(self.report(req={**REQ, "constraints": [constraint]})["qualified_candidates"], 1)

    def test_capacitor_and_inductor_constraints(self):
        data = json.loads((ROOT / "tests/fixtures/digikey/GCM21BR71E225KA73L.keyword.json").read_text())
        p = data["body"]["ExactMatches"][0]
        req = {"kind": "capacitor", "value": "2200nF", "package": "0805", "tolerance_percent": 10, "dielectric": "X7R",
               "automotive": True, "constraints": [{"parameter": "Voltage - Rated", "op": "min", "value": 25, "unit": "V"}]}
        self.assertEqual(self.report([record(p)], req)["qualified_candidates"], 1)
        req["dielectric"] = "X5R"
        self.assertEqual(self.report([record(p)], req)["qualified_candidates"], 0)
        p = product()
        p["Category"] = {"Name": "Fixed Inductors"}
        p["Parameters"] = [{"ParameterText": k, "ValueText": v} for k, v in
                           [("Inductance", "10 µH"), ("Tolerance", "±20%"), ("Package / Case", "0805 (2012 Metric)"), ("Current Rating (Amps)", "2A")]]
        req = {"kind": "inductor", "value": "10uH", "package": "0805", "tolerance_percent": 20,
               "constraints": [{"parameter": "Current Rating (Amps)", "op": "min", "value": 2, "unit": "A"}]}
        self.assertEqual(self.report([record(p)], req)["qualified_candidates"], 1)

    def test_conditional_numeric_spec_is_unresolved_and_diode_catalog_match_needs_engineering_review(self):
        p = change(product(), "Voltage", "50V @ 1mA")
        req = {**REQ, "constraints": [{"parameter": "Voltage", "op": "min", "value": 50, "unit": "V"}]}
        self.assertEqual(self.report([record(p)], req)["results"][0]["catalog_status"], "needs_review")
        p["Category"] = {"Name": "Diodes - Rectifiers - Single"}
        p = change(change(p, "Package / Case", "SOD-123"), "Supplier Device Package", "SOD-123")
        req = {"kind": "diode", "package": "SOD-123"}
        diode = self.report([record(p)], req)
        self.assertEqual(diode["qualified_candidates"], 1)
        self.assertEqual(diode["results"][0]["engineering_status"], "requires_review")

    def test_same_manufacturer_identical_spec_mpns_are_one_alternative(self):
        # Reel-size / tape suffixes are separate MPNs with identical catalog specifications.
        records = [record(product(mpn)) for mpn in ("RC-07", "RC-13", "RC-7D")]
        report = self.report(records)
        self.assertEqual(report["qualified_candidates"], 1)
        self.assertEqual(report["status"], "shortfall")
        variants = [r for r in report["results"] if r.get("variant_of")]
        self.assertEqual(len(variants), 2)
        self.assertTrue(all("ordering/packaging variant" in r["issues"][-1] for r in variants))

    def test_variants_of_the_replaced_part_are_excluded(self):
        records = [record(product(mpn)) for mpn in ("RC-07", "RC-13")] + [record(maker(product("OTHER-1"), "Other"))]
        report = self.report(records, {**REQ, "exclude_mpns": ["RC-07"]})
        statuses = {r["mpn"]: r["catalog_status"] for r in report["results"]}
        self.assertEqual(statuses, {"RC-07": "excluded", "RC-13": "excluded_variant", "OTHER-1": "catalog_match"})
        self.assertEqual(report["qualified_candidates"], 1)

    def test_different_spec_same_manufacturer_counts_but_diversity_is_required(self):
        records = [record(change(product(f"SAME-{i}"), "Power (Watts)", w)) for i, w in enumerate(("0.0625W, 1/16W", "0.1W, 1/10W", "0.125W, 1/8W"))]
        report = self.report(records)
        self.assertEqual(report["qualified_candidates"], 3)
        self.assertEqual(report["distinct_manufacturers"], 1)
        self.assertEqual(report["manufacturer_shortfall"], 1)
        self.assertEqual(report["status"], "shortfall")
        self.assertEqual(self.report(records, {**REQ, "min_manufacturers": 1})["status"], "target_met")
        with self.assertRaises(ValueError):
            validate_request({**REQ, "min_alternatives": 1, "min_manufacturers": 2})

    def test_supplier_device_package_guards_body_size(self):
        p = change(change(product("DFN-1"), "Package / Case", "6-WDFN Exposed Pad"), "Supplier Device Package", "6-WSON (2x2)")
        p["Category"] = {"Name": "Diodes - Rectifiers - Single"}
        req = {"kind": "diode", "package": "6-WDFN Exposed Pad"}
        check = lambda r: next(c for c in self.report([record(p)], r)["results"][0]["checks"] if c["parameter"] == "supplier_device_package")
        self.assertEqual(check(req)["status"], "unknown")  # Family name alone does not fix the footprint.
        self.assertEqual(check({**req, "supplier_device_package": "6-WSON (2x2)"})["status"], "match")
        self.assertEqual(check({**req, "supplier_device_package": "6-DFN (3x3)"})["status"], "mismatch")
        self.assertEqual(check({**req, "supplier_device_package": "6-DFN (2x2)"})["status"], "unknown")
        self.assertEqual(check({**req, "supplier_device_package": "8-WSON (2x2)"})["status"], "mismatch")
        # Chip-size codes are already specific: no extra check.
        self.assertFalse(any(c["parameter"] == "supplier_device_package" for c in self.report()["results"][0]["checks"]))

    def test_compact_markdown_summarizes_and_full_report_keeps_everything(self):
        wrong = change(maker(product("WRONG-1"), "Other"), "Resistance", "11 kOhms")
        records = [record(product(m)) for m in ("RC-07", "RC-13")] + [record(maker(product(f"ALT-{i}"), f"M{i}")) for i in range(12)]
        report = self.report(records + [record(wrong)])
        compact, full = markdown(report), markdown(report, full=True)
        for heading in ("## Counted alternatives (13)", "## Details: top 10 of 13 counted",
                        "## Ordering/packaging variants (1)", "- RC-13 → RC-07", "## Rejected (1)", "- Resistance (1): WRONG-1"):
            self.assertIn(heading, compact)
        self.assertNotIn("### Catalog parameters", compact)
        self.assertIn("### Catalog parameters", full)
        self.assertLess(len(compact), len(full) / 2)
        self.assertIn("automotive_qualification match (Anti-Sulfur, Automotive AEC-Q200)", compact)

    def test_package_metric_ambiguity_and_suffixes(self):
        self.assertFalse(package_matches("0402 Metric", "0402"))
        self.assertTrue(package_matches("0402 (1005 Metric)", "0402"))
        self.assertFalse(package_matches("SOD-123F", "SOD-123"))

    def test_invalid_requests_reject_typos_and_wrong_types(self):
        for delta in ({"tolerence": 1}, {"quantity": True}, {"min_alternatives": 0}, {"value": "?"},
                      {"normally_stocking": "yes"}, {"automotive": "true"}, {"tolerance_percent": -1},
                      {"max_age_hours": "NaN"}, {"constraints": [{"parameter": "x", "op": "min", "value": 1}]},
                      {"constraints": [{"parameter": "x", "op": "eq", "value": 1, "unit": "bananas"}]}):
            with self.subTest(delta=delta), self.assertRaises(ValueError):
                validate_request({**REQ, **delta})

    def test_stock_lookup_does_not_substitute_an_mpn_prefix(self):
        report = stock_report("YAGEO", "AF0402", [record()], {}, now=NOW)
        self.assertEqual(report["status"], "unknown")
        report = stock_report("YAGEO", "AF0402FR-0710KL", [record()], {}, now=NOW)
        self.assertEqual(report["status"], "available")


class AdapterTests(unittest.TestCase):
    def test_old_jlc_authentication_vector_preserved(self):
        a = JLCPCBAuthenticator("app-demo", "ak-demo", "sk-demo")
        header = a.authorize("POST", "/overseas/openapi/component/getComponentDetailByCode", '{"componentCodes":["C8734"]}',
                             timestamp=1700000000, nonce="0123456789abcdefghijklmnopqrstuv").header
        self.assertIn('signature="IEDkj4lvZ0g1Hei6kToZRDrJmE2LqNA5WPEzuYlWi2E="', header)

    def test_search_pagination_caching_and_partial_provider_failure(self):
        class Client:
            def __init__(self):
                self.calls = []
            def keyword(self, query, **kw):
                self.calls.append(kw)
                if kw["offset"]:
                    return 429, {"detail": "SENSITIVE SERVER TEXT"}
                return 200, {"ProductsCount": 100, "Products": [product()], "ExactMatches": [product()]}
        with tempfile.TemporaryDirectory() as folder:
            client = Client()
            catalog = DigiKeyCatalog(folder, client)
            records, warnings = catalog.search("resistor")
            self.assertEqual(len(records), 2)
            self.assertEqual([c["offset"] for c in client.calls], [0, 50])
            self.assertTrue(warnings)
            self.assertNotIn("SENSITIVE", str(warnings))
            page = DigiKeyCatalog(folder).page("resistor", 0)
            self.assertEqual(page["observed_at"], records[0]["observed_at"])
            with self.assertRaises(LookupError):
                DigiKeyCatalog(folder).page("resistor", 0, refresh=True)

    def test_digikey_client_sends_pagination_and_preserves_it_on_token_retry(self):
        calls = []
        def opener(request, timeout):
            if request.full_url.endswith("/token"):
                return Response({"access_token": "FAKE-TOKEN"})
            calls.append(json.loads(request.data))
            if len(calls) == 1:
                raise urllib.error.HTTPError(request.full_url, 401, "Unauthorized", {}, io.BytesIO())
            return Response({"Products": []})
        client = digikey.DigiKeyClient("FAKE-ID", "FAKE-SECRET", min_interval=0, opener=opener)
        client.keyword("10k", limit=50, offset=50, filters={"MinimumQuantityAvailable": 1000})
        self.assertEqual(calls, [calls[0], calls[0]])
        self.assertEqual(calls[0]["Offset"], 50)
        self.assertEqual(calls[0]["Limit"], 50)
        self.assertEqual(calls[0]["FilterOptionsRequest"]["MinimumQuantityAvailable"], 1000)

    def test_jlc_request_and_exact_mapping_validation(self):
        seen = []
        def opener(request, timeout):
            seen.append(request.data.decode())
            return Response({"data": {"componentDetails": [{"componentCode": "C8734", "componentModel": "EXACT", "stockCount": 123}]}})
        client = JLCPCBStock("fake", "fake", "fake", opener)
        mapping = {"manufacturer": "Demo", "mpn": "EXACT", "jlcpcb": "C8734"}
        offer = client.lookup(mapping)
        self.assertEqual(seen, ['{"componentCodes":["C8734"]}'])
        self.assertEqual(offer["stock"], 123)
        self.assertTrue(offer["identity_review"])
        client.last_call = 0
        with self.assertRaises(ValueError):
            client.lookup({**mapping, "mpn": "EXACT-SUFFIX"})

    def test_lcsc_signing_simple_official_algorithm(self):
        payload = {"keyword": "RC 10k", "limit": "100", "unused": None}
        h = auth_headers("demo-key", "demo-secret", payload, nonce="abcdefghijklmnop", timestamp=1700000000)
        base = "key=demo-key&nonce=abcdefghijklmnop&secret=demo-secret&timestamp=1700000000&keyword=RC+10k&limit=100"
        self.assertEqual(h["signature"], hashlib.sha256(base.encode()).hexdigest())
        self.assertNotIn("demo-secret", json.dumps(h))

    def test_lcsc_uses_official_endpoint_and_filters_exact_identity(self):
        seen = []
        def opener(request, timeout):
            seen.append(request)
            return Response({"success": True, "code": 200, "result": {"nextOffset": 0, "productCount": 2, "products": [
                {"manufacturer": {"name": "YAGEO"}, "manufacturerProductNumber": mpn,
                 "lcscProductNumber": "C123", "minimumOrderQuantity": 100, "incrementQuantity": 100,
                 "productPrice": {"quantityAvailable": 5000}, "productDetailURL": "https://www.lcsc.com/demo"}
                for mpn in ("EXACT", "EXACT-OTHER")]}})
        offers, incomplete = LCSCStock("fake-key", "fake-secret", opener).lookup("YAGEO", "EXACT")
        self.assertEqual(len(offers), 1)
        self.assertEqual(offers[0]["stock"], 5000)
        self.assertFalse(incomplete)
        self.assertTrue(seen[0].full_url.startswith("https://api.lcsc.com/rest/api/agent/product/v1/keywordsearch?"))
        self.assertIn("offset=1", seen[0].full_url)
        self.assertNotIn("fake-secret", seen[0].full_url)

    def test_jlc_and_lcsc_observations_round_trip_through_cache(self):
        base = {"manufacturer": "YAGEO", "mpn": "EXACT", "code": "C123", "stock": 500,
                "observed_at": AT, "url": "https://example.test/part", "moq": 1}
        with tempfile.TemporaryDirectory() as folder:
            lcsc = {**base, "supplier": "lcsc", "order_multiple": 100}
            jlc = {**base, "supplier": "jlcpcb", "library_type": "Basic", "identity_review": True}
            with patch("kicad_checker.sourcing.cli.configured_lcsc") as config:
                config.return_value.lookup.return_value = ([lcsc], False)
                self.assertEqual(lcsc_observations([("YAGEO", "EXACT")], folder, True, []), [lcsc])
            self.assertEqual(lcsc_observations([("YAGEO", "EXACT")], folder, False, [])[0]["stock"], 500)
            mapping = {"manufacturer": "YAGEO", "mpn": "EXACT", "jlcpcb": "C123"}
            with patch("kicad_checker.sourcing.cli.configured_jlc") as config:
                config.return_value.lookup.return_value = jlc
                jlc_observations([mapping], folder, True, [])
            self.assertEqual(jlc_observations([mapping], folder, False, [])[0]["library_type"], "Basic")

    def test_http_errors_do_not_leak_server_body(self):
        def opener(request, timeout):
            raise urllib.error.HTTPError(request.full_url, 403, "Forbidden", {}, io.BytesIO(b"SECRET SERVER BODY"))
        for client, args in ((LCSCStock("fake", "fake", opener), ("YAGEO", "EXACT")),
                             (JLCPCBStock("fake", "fake", "fake", opener), ({"manufacturer": "x", "mpn": "x", "jlcpcb": "C1"},))):
            with self.subTest(client=type(client).__name__), self.assertRaises(ValueError) as ctx:
                client.lookup(*args)
            self.assertNotIn("SECRET", str(ctx.exception))


class CLITests(unittest.TestCase):
    def test_bom_groups_parts_counts_boards_and_keeps_unknown_dnp_and_budget(self):
        def part(mpn="EXACT", mfr="YAGEO", dnp=False):
            return {"fields": {"MPN": mpn, "Manufacturer": mfr}, "dnp": dnp}
        with tempfile.TemporaryDirectory() as folder:
            folder = Path(folder)
            source = folder / "circuit.json"
            source.write_text(json.dumps({"components": {"R1": part(), "R2": part(), "R3": part("OTHER"),
                                                         "R4": part("", ""), "R5": part(dnp=True)}}))
            with patch("kicad_checker.credentials.get", side_effect=AssertionError("credentials accessed")), redirect_stdout(io.StringIO()):
                status = main(["parts-bom", str(source), "--boards", "10", "--max-parts", "1",
                               "--cache", str(folder / "cache"), "--output", str(folder / "report")])
            report = json.loads((folder / "report/bom-stock.json").read_text())
            self.assertEqual(status, 2)
            rows = {r["references"][0]: r for r in report["results"]}
            self.assertEqual(rows["R1"]["references"], ["R1", "R2"])
            self.assertEqual(rows["R1"]["quantity"], 20)
            self.assertIn("budget", rows["R3"]["reason"])
            self.assertEqual(rows["R4"]["status"], "unknown")
            self.assertEqual(rows["R5"]["status"], "not_applicable")

    def test_documented_request_examples_validate(self):
        for name in ("resistor", "capacitor", "inductor", "diode"):
            with self.subTest(name=name):
                validate_request(json.loads((ROOT / f"examples/sourcing/{name}.json").read_text()))

    def test_dry_run_never_reads_credentials_or_opens_network(self):
        with tempfile.TemporaryDirectory() as folder:
            request = Path(folder) / "request.json"
            request.write_text(json.dumps(REQ))
            with patch("kicad_checker.credentials.get", side_effect=AssertionError("credentials accessed")), patch("urllib.request.urlopen", side_effect=AssertionError("network")), redirect_stdout(io.StringIO()) as output:
                result = main(["parts-search", str(request), "--online", "--dry-run"])
            self.assertEqual(result, 0)
            self.assertEqual(json.loads(output.getvalue())["network_requests_sent"], 0)

    def test_offline_snapshot_cli_reports_stale_stock_and_preserves_report(self):
        with tempfile.TemporaryDirectory() as folder:
            folder = Path(folder)
            request, snapshot, out = folder / "request.json", folder / "snapshot.json", folder / "report"
            request.write_text(json.dumps(REQ))
            snapshot.write_text(json.dumps({"schema_version": 1, "records": [record(at="2020-01-01T00:00:00+00:00")]}))
            args = ["parts-search", str(request), "--digikey-snapshot", str(snapshot), "--output", str(out)]
            with patch("kicad_checker.credentials.get", side_effect=AssertionError("credentials accessed")), redirect_stdout(io.StringIO()):
                self.assertEqual(main(args), 2)
            data = json.loads((out / "sourcing.json").read_text())
            self.assertEqual(data["shortfall"], 3)
            self.assertIsNone(data["results"][0]["suppliers"]["lcsc"]["max_reported_stock"])
            with redirect_stderr(io.StringIO()):
                self.assertEqual(main(args), 3)

    def test_offline_without_cache_is_visible_shortfall(self):
        with tempfile.TemporaryDirectory() as folder:
            folder = Path(folder)
            req = folder / "request.json"
            req.write_text(json.dumps(REQ))
            with redirect_stdout(io.StringIO()):
                result = main(["parts-search", str(req), "--cache", str(folder / "cache"), "--output", str(folder / "out")])
            self.assertEqual(result, 2)
            report = json.loads((folder / "out/sourcing.json").read_text())
            self.assertFalse(report["results"])
            self.assertTrue(report["warnings"])

    def test_negative_import_quantity_rejected(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "offers.json"
            path.write_text(json.dumps({"schema_version": 1, "offers": [{"supplier": "lcsc", "manufacturer": "x", "mpn": "x",
                                                                        "url": "https://example.test", "observed_at": AT, "stock": -1}]}))
            with self.assertRaises(ValueError):
                load_offers(path)


if __name__ == "__main__":
    unittest.main()
