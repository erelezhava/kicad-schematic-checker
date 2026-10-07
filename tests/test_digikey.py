"""DigiKey lookup tests. Offline: real API responses captured 2026-10-07 are replayed
from tests/fixtures/digikey; the HTTP client is exercised with a fake opener."""

import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from kicad_checker import digikey
from kicad_checker.core import read_json, write_json

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests/fixtures/digikey"
MPNS = {"GCM21BR71E225KA73L": "GCM21BR71E225KA73L", "AF0402FR-0710KL": "AF0402FR-0710KL",
        "LP591233MDRVREP": "LP591233MDRVREP", "LTC4331HUFD#PBF": "LTC4331HUFD_PBF",
        "5040500691": "5040500691", "MLX90640ESF-BAA-000-TU": "MLX90640ESF-BAA-000-TU"}
C_FP = "Capacitor_SMD:C_0805_2012Metric_Pad1.18x1.45mm_HandSolder"
R_FP = "Resistor_SMD:R_0402_1005Metric_Pad0.72x0.64mm_HandSolder"
C2_DESC = "2.2 µF ±10% 25V Ceramic Capacitor X7R 0805 (2012 Metric) AEC-Q200"


def fixture(mpn):
    return json.loads((FIXTURES / f"{MPNS[mpn]}.keyword.json").read_text(encoding="utf-8"))


def part(ref, value, footprint="", **fields):
    return {"reference": ref, "value": value, "footprint": footprint, "dnp": False, "sheet_path": "/",
            "fields": fields, "pins": {}}


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cache = Path(self.tmp.name) / "cache"
        for mpn in MPNS:
            data = fixture(mpn)
            digikey.store(self.cache, mpn, data["status"], data["body"])

    def tearDown(self):
        self.tmp.cleanup()

    def audit(self, *components):
        circuit = {"netlist_sha256": "x", "components": {c["reference"]: c for c in components}}
        return {r["reference"]: r for r in digikey.digikey_audit(circuit, self.cache)["results"]}

    def one(self, component):
        return self.audit(component)[component["reference"]]

    def check(self, item, name):
        return [c for c in item["checks"] if c["check"] == name]


class AuditTests(Base):
    def test_real_capacitor_passes_when_manufacturer_declared(self):
        item = self.one(part("C2", "2.2 uF", C_FP, MPN="GCM21BR71E225KA73L", Manufacturer="Murata", Description=C2_DESC))
        self.assertEqual(item["status"], "pass", item.get("reason"))
        self.assertEqual(item["digikey"]["manufacturer"], "Murata Electronics")  # exact name wins over Murata Power Solutions
        for name in ("value", "package", "voltage", "tolerance", "dielectric", "lifecycle", "category"):
            self.assertEqual(self.check(item, name)[0]["status"], "pass", name)

    def test_missing_manufacturer_gives_owner_suggestion(self):
        item = self.one(part("R15", "10 K", R_FP, **{"PRT.NUM": "AF0402FR-0710KL"}))
        self.assertEqual(item["status"], "needs_review")
        self.assertIn("Set Manufacturer = 'YAGEO'", item["suggestions"][0])
        self.assertEqual(self.check(item, "value")[0]["status"], "pass")

    def test_duplicate_listings_with_identical_data_are_checked_once(self):
        item = self.one(part("C2", "2.2uF", C_FP, MPN="GCM21BR71E225KA73L"))
        self.assertEqual(self.check(item, "match")[0]["status"], "pass")
        self.assertIn("Murata Electronics", item["suggestions"][0])
        self.assertIn("Murata Power Solutions", item["suggestions"][0])

    def test_duplicate_listings_with_different_data_need_owner_choice(self):
        data = fixture("GCM21BR71E225KA73L")
        data["body"]["ExactMatches"][1]["Parameters"][0]["ValueText"] = "1 µF"
        digikey.store(self.cache, "GCM21BR71E225KA73L", 200, data["body"])
        item = self.one(part("C2", "2.2uF", C_FP, MPN="GCM21BR71E225KA73L"))
        self.assertEqual(item["status"], "needs_review")
        self.assertIn("different data", item["reason"])

    def test_contradictions_fail(self):
        cases = [
            (part("C2", "4.7uF", C_FP, MPN="GCM21BR71E225KA73L", Manufacturer="Murata"), "value"),
            (part("C2", "2.2uF", C_FP.replace("0805_2012", "0603_1608"), MPN="GCM21BR71E225KA73L", Manufacturer="Murata"), "package"),
            (part("C2", "2.2uF", C_FP, MPN="GCM21BR71E225KA73L", Manufacturer="Murata", Voltage="16V"), "voltage"),
            (part("C2", "2.2uF", C_FP, MPN="GCM21BR71E225KA73L", Manufacturer="Murata",
                  Description=C2_DESC.replace("X7R", "X5R")), "dielectric"),
            (part("R1", "10k", R_FP, MPN="GCM21BR71E225KA73L", Manufacturer="Murata"), "category"),
        ]
        for component, name in cases:
            with self.subTest(check=name):
                item = self.one(component)
                self.assertEqual(item["status"], "fail")
                self.assertTrue(any(c["status"] == "fail" for c in self.check(item, name)), item["checks"])

    def test_manufacturer_names_and_aliases(self):
        for declared in ("Texas Instruments", "TI", "texas instruments inc."):
            with self.subTest(declared=declared):
                item = self.one(part("U2", "LP5912", MPN="LP591233MDRVREP", Manufacturer=declared))
                self.assertEqual(self.check(item, "manufacturer")[0]["status"], "pass")
        for declared in ("Analog Devices", "Linear Technology", "ADI"):
            with self.subTest(declared=declared):
                item = self.one(part("U1", "LTC4331", MPN="LTC4331HUFD#PBF", Manufacturer=declared))
                self.assertEqual(self.check(item, "manufacturer")[0]["status"], "pass")
        item = self.one(part("U2", "LP5912", MPN="LP591233MDRVREP", Manufacturer="Vishay"))
        self.assertEqual(item["status"], "needs_review")  # naming differences are questions, not failures

    def test_lifecycle_warning(self):
        data = fixture("LP591233MDRVREP")
        data["body"]["ExactMatches"][0]["ProductStatus"]["Status"] = "Obsolete"
        digikey.store(self.cache, "LP591233MDRVREP", 200, data["body"])
        item = self.one(part("U2", "LP5912", MPN="LP591233MDRVREP", Manufacturer="TI"))
        self.assertEqual(item["status"], "needs_review")
        self.assertIn("Obsolete", item["reason"])

    def test_unresolved_and_not_applicable_cases(self):
        digikey.store(self.cache, "NOPE-123", 200, {"ProductsCount": 0, "ExactMatches": []})
        digikey.store(self.cache, "ERR-1", 500, {"title": "Server error", "status": 500})
        results = self.audit(part("U9", "X", MPN="NOPE-123"), part("U8", "X", MPN="ERR-1"),
                             part("U7", "X", MPN="NEVER-FETCHED"), part("U6", "X"),
                             {**part("C9", "1uF", MPN="GCM21BR71E225KA73L"), "dnp": True})
        self.assertIn("not found on DigiKey", results["U9"]["reason"])
        self.assertIn("HTTP 500", results["U8"]["reason"])
        self.assertIn("digikey-fetch", results["U7"]["reason"])
        self.assertEqual({r: results[r]["status"] for r in ("U9", "U8", "U7")}, dict.fromkeys(("U9", "U8", "U7"), "needs_review"))
        self.assertEqual(results["U6"]["status"], "not_checked")
        self.assertEqual(results["C9"]["status"], "not_applicable")

    def test_ic_package_is_reported_not_judged_and_pin_hint_is_a_lead(self):
        component = part("U2", "LP5912", "LP591233MDRVREP:WSON6_DRV_TEX", MPN="LP591233MDRVREP", Manufacturer="TI")
        component["symbol_pin_inventory"] = {"complete": True, "count": 6, "source": "component_units"}
        item = self.one(component)
        self.assertEqual(item["status"], "pass")
        self.assertEqual(self.check(item, "package")[0]["status"], "not_checked")
        hint = self.check(item, "pin_count_hint")[0]
        self.assertEqual(hint["status"], "not_checked")
        component["symbol_pin_inventory"]["count"] = 5
        item = self.one(component)
        self.assertEqual(item["status"], "pass")  # a hint never changes the verdict
        self.assertIn("suggests 6 + exposed pad", self.check(item, "pin_count_hint")[0]["reason"])


class CacheAndClientTests(unittest.TestCase):
    def test_cache_trims_and_never_keeps_account_ids(self):
        with tempfile.TemporaryDirectory() as folder:
            body = {"ProductsCount": 1, "AccountIdUsed": 42, "ExactMatches": [
                {**fixture("AF0402FR-0710KL")["body"]["ExactMatches"][0], "CustomerIdUsed": 7, "PhotoUrl": "x"}]}
            digikey.store(folder, "AF0402FR-0710KL", 200, body)
            text = digikey.cache_path(folder, "AF0402FR-0710KL").read_text()
            self.assertNotIn("CustomerIdUsed", text)
            self.assertNotIn("AccountIdUsed", text)
            self.assertNotIn("PhotoUrl", text)
            self.assertIsNotNone(digikey.load(folder, " af0402fr-0710kl "))  # key ignores case/spacing

    def test_client_auth_search_retry_and_fetch(self):
        calls = []

        class Response(io.BytesIO):
            def __init__(self, status, data):
                super().__init__(json.dumps(data).encode())
                self.status = status

        def opener(request, timeout):
            calls.append((request.get_method(), request.full_url))
            if request.full_url.endswith("/token"):
                return Response(200, {"access_token": f"tok{len(calls)}"})
            if len([c for c in calls if c[1].endswith("/keyword")]) == 1:
                raise digikey.urllib.error.HTTPError(request.full_url, 401, "expired", {}, io.BytesIO(b"{}"))
            self.assertIn("Bearer tok", request.headers["Authorization"])
            return Response(200, fixture("AF0402FR-0710KL")["body"])

        client = digikey.DigiKeyClient("id", "secret", min_interval=0, opener=opener)
        circuit = {"components": {"R1": part("R1", "10k", MPN="AF0402FR-0710KL"), "R2": part("R2", "10k", MPN="AF0402FR-0710KL")}}
        with tempfile.TemporaryDirectory() as folder:
            summary = digikey.fetch(circuit, folder, client, log=lambda *_: None)
            self.assertEqual(summary, {"fetched": 1, "cached": 0, "errors": 0})  # one call per unique MPN
            self.assertEqual(sum(url.endswith("/token") for _, url in calls), 2)  # re-authenticated after 401
            again = digikey.fetch(circuit, folder, client, log=lambda *_: None)
            self.assertEqual(again["cached"], 1)

    def test_credentials_from_env_file_and_environment(self):
        with tempfile.TemporaryDirectory() as folder:
            env_file = Path(folder) / ".env"
            env_file.write_text("# keys\nexport DIGIKEY_CLIENT_ID=abc\nDIGIKEY_CLIENT_SECRET='xyz'\n")
            saved = dict(os.environ)
            for key in ("DIGIKEY_CLIENT_ID", "DIGIKEY_CLIENT_SECRET"):
                os.environ.pop(key, None)
            try:
                self.assertEqual(digikey.load_credentials([env_file]), ("abc", "xyz"))
                os.environ["DIGIKEY_CLIENT_ID"] = "from-env"  # environment wins
                self.assertEqual(digikey.load_credentials([env_file])[0], "from-env")
                with self.assertRaises(ValueError) as error:
                    digikey.load_credentials([Path(folder) / "missing.env"])
                self.assertNotIn("from-env", str(error.exception))  # never echo secrets
                self.assertIn("DIGIKEY_CLIENT_SECRET", str(error.exception))
            finally:
                os.environ.clear()
                os.environ.update(saved)

    def test_secrets_file_is_git_ignored_and_template_has_no_values(self):
        ignored = (ROOT / ".gitignore").read_text().split()
        self.assertIn(".env", ignored)
        self.assertIn("!.env.example", ignored)
        values = digikey.credentials.read_env_file(ROOT / ".env.example")
        self.assertIn("DIGIKEY_CLIENT_ID", values)
        self.assertTrue(all(v == "" for v in values.values()), "template must stay empty")


class CliTests(Base):
    def test_digikey_check_and_review_integration(self):
        base = Path(self.tmp.name)
        circuit = {"schema_version": 1, "netlist_sha256": "x", "components": {
            "C2": part("C2", "2.2 uF", C_FP, MPN="GCM21BR71E225KA73L", Description=C2_DESC),
            "U2": part("U2", "LP5912", MPN="LP591233MDRVREP", Manufacturer="Texas Instruments", Description="LDO 3.3 V")}}
        write_json(base / "circuit.json", circuit)
        run = lambda *args: subprocess.run([sys.executable, "-m", "kicad_checker", *args], cwd=ROOT, capture_output=True, text=True)
        proc = run("digikey-check", str(base / "circuit.json"), "--cache", str(self.cache), "--output", str(base / "dk"))
        self.assertEqual(proc.returncode, 2, proc.stderr)  # C2 manufacturer empty -> needs_review
        actions = (base / "dk/owner-actions.md").read_text()
        self.assertIn("DigiKey suggestion: Set Manufacturer = 'Murata Electronics'", actions)
        self.assertTrue((base / "dk/digikey.md").is_file())

        circuit["components"]["C2"]["value"] = "4.7uF"  # contradicts DigiKey and the Description
        write_json(base / "circuit.json", circuit)
        rules = {"schema_version": 1, "rules": []}
        evidence = {"schema_version": 1, "bindings": {}}
        write_json(base / "rules.json", rules)
        write_json(base / "evidence.json", evidence)
        proc = run("review", str(base / "circuit.json"), "--snapshot", "--rules", str(base / "rules.json"),
                   "--evidence", str(base / "evidence.json"), "--digikey-cache", str(self.cache), "--output", str(base / "review"))
        self.assertEqual(proc.returncode, 1, proc.stderr)
        report = read_json(base / "review/report.json")
        self.assertEqual(report["digikey"]["coverage"]["fail"], 1)
        self.assertIn("DigiKey cross-check", (base / "review/report.md").read_text())
        self.assertIn("DigiKey conflict", (base / "review/owner-actions.md").read_text())

    def test_fetch_without_credentials_is_a_clear_error(self):
        if digikey.credentials.REPO_ENV.is_file():
            self.skipTest("a local .env with real keys exists in this checkout")
        env = {k: v for k, v in os.environ.items() if not k.startswith("DIGIKEY_")}
        env["HOME"] = self.tmp.name  # no user credentials file there
        with tempfile.TemporaryDirectory() as folder:
            write_json(Path(folder) / "c.json", {"components": {}})
            proc = subprocess.run([sys.executable, "-m", "kicad_checker", "digikey-fetch", str(Path(folder) / "c.json"),
                                   "--cache", folder], cwd=ROOT, capture_output=True, text=True, env=env)
        self.assertEqual(proc.returncode, 3)
        self.assertIn("credentials missing", proc.stderr)


if __name__ == "__main__":
    unittest.main()
