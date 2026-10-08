"""datasheet-fetch: project docs first, shared cache second, download last. Offline tests."""

import io
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from kicad_checker import datasheets, digikey

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests/fixtures/digikey"


def make_pdf(path, text):
    """Write a minimal one-page PDF containing `text` (readable by pdftotext)."""
    content = f"BT /F1 12 Tf 72 720 Td ({text}) Tj ET".encode()
    objects = [b"<< /Type /Catalog /Pages 2 0 R >>", b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
               b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R "
               b"/Resources << /Font << /F1 5 0 R >> >> >>",
               b"<< /Length %d >>\nstream\n" % len(content) + content + b"\nendstream",
               b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"]
    out, offsets = bytearray(b"%PDF-1.4\n"), []
    for number, body in enumerate(objects, 1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % number + body + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objects) + 1)
    out += b"".join(b"%010d 00000 n \n" % o for o in offsets)
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(objects) + 1, xref)
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_bytes(bytes(out))
    return Path(path)


def part(ref, mpn, dnp=False):
    return {"reference": ref, "value": mpn, "dnp": dnp, "fields": {"MPN": mpn}, "pins": {}}


class Response(io.BytesIO):
    def __init__(self, data, url):
        super().__init__(data)
        self.url = url


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        self.cache = self.base / "pdf-cache"
        self.dk = self.base / "dk-cache"
        data = json.loads((FIXTURES / "LP591233MDRVREP.keyword.json").read_text())
        digikey.store(self.dk, "LP591233MDRVREP", 200, data["body"])  # DatasheetUrl .../lp5912-ep.pdf
        self.docs = self.base / "project" / "Docs"
        self.docs.mkdir(parents=True)
        self.circuit = {"source": str(self.base / "project/board.kicad_sch"), "components": {"U2": part("U2", "LP591233MDRVREP")}}
        self.calls = []

    def tearDown(self):
        self.tmp.cleanup()

    def opener(self, payload=None, error=None):
        def open_url(request, timeout):
            self.calls.append(request.full_url)
            if error:
                raise error
            return Response(payload, request.full_url)
        return open_url

    def run_find(self, payload=None, error=None, **kwargs):
        return datasheets.find_datasheets(self.circuit, self.cache, self.dk, opener=self.opener(payload, error),
                                          log=lambda *_: None, **kwargs)["results"][0]


class LocalDocsTests(Base):
    def test_file_name_with_mpn_is_used_without_download(self):
        pdf = make_pdf(self.docs / "LDO" / "LP591233MDRVREP_rev2.pdf", "anything")
        item = self.run_find()
        self.assertEqual((item["status"], item["source"], item["path"]), ("found", "project_docs", str(pdf.resolve())))
        self.assertEqual(self.calls, [])

    def test_same_file_name_as_digikey_datasheet(self):
        make_pdf(self.docs / "lp5912-ep.pdf", "LP5912-EP 500mA LDO")  # family name only, like your Docs/LDO
        item = self.run_find()
        self.assertEqual(item["source"], "project_docs")
        self.assertIn("same file name", item["reason"])
        self.assertEqual(self.calls, [])

    @unittest.skipUnless(datasheets.pdftotext_available(), "pdftotext not installed")
    def test_text_listing_the_mpn(self):
        make_pdf(self.docs / "other.pdf", "Unrelated op amp")
        make_pdf(self.docs / "regulator_datasheet.pdf", "Orderable device LP591233MDRVREP WSON 6")
        item = self.run_find()
        self.assertEqual(Path(item["path"]).name, "regulator_datasheet.pdf")
        self.assertIn("text lists the MPN", item["reason"])

    @unittest.skipUnless(datasheets.pdftotext_available(), "pdftotext not installed")
    def test_two_matching_documents_need_a_choice(self):
        make_pdf(self.docs / "a.pdf", "LP591233MDRVREP revision A")
        make_pdf(self.docs / "b.pdf", "LP591233MDRVREP revision B")
        item = self.run_find()
        self.assertEqual(item["status"], "needs_review")
        self.assertIn("Several project PDFs", item["reason"])
        self.assertEqual(self.calls, [])

    def test_hidden_folders_and_non_doc_folders_are_ignored(self):
        make_pdf(self.docs / ".history" / "LP591233MDRVREP.pdf", "old")
        make_pdf(self.base / "project" / "gerbers" / "LP591233MDRVREP.pdf", "not a doc folder")
        item = self.run_find(offline=True)
        self.assertEqual(item["status"], "missing")


class DownloadAndCacheTests(Base):
    def test_download_once_then_cache(self):
        pdf_bytes = make_pdf(self.base / "remote.pdf", "LP5912").read_bytes()
        item = self.run_find(payload=pdf_bytes)
        self.assertEqual((item["status"], item["source"]), ("found", "downloaded"))
        self.assertEqual(self.calls, ["https://www.ti.com/lit/ds/symlink/lp5912-ep.pdf"])
        self.assertTrue(Path(item["path"]).is_file())
        again = self.run_find(payload=b"should not be fetched")
        self.assertEqual(again["source"], "cache")
        self.assertEqual(len(self.calls), 1)

    def test_web_page_instead_of_pdf_is_rejected(self):
        item = self.run_find(payload=b"<!DOCTYPE html><html>Please log in</html>")
        self.assertEqual(item["status"], "missing")
        self.assertIn("web page, not a PDF", item["reason"])
        self.assertIn("lp5912-ep.pdf", item["reason"])  # URL given for manual download
        self.assertEqual(list(self.cache.glob("*.pdf")), [])

    def test_http_error_and_offline(self):
        error = datasheets.urllib.error.HTTPError("u", 403, "Forbidden", {}, io.BytesIO(b""))
        self.assertIn("download failed", self.run_find(error=error)["reason"])
        self.assertEqual(self.run_find(offline=True)["status"], "missing")

    def test_cache_entry_with_changed_file_is_not_trusted(self):
        pdf_bytes = make_pdf(self.base / "remote.pdf", "LP5912").read_bytes()
        item = self.run_find(payload=pdf_bytes)
        Path(item["path"]).write_bytes(pdf_bytes + b"tampered")
        self.assertEqual(self.run_find(offline=True)["status"], "missing")

    def test_no_digikey_data_means_no_url(self):
        self.circuit["components"] = {"U9": part("U9", "UNKNOWN-123")}
        item = self.run_find()
        self.assertEqual(item["status"], "missing")
        self.assertIn("no datasheet URL", item["reason"])


class SelectionTests(unittest.TestCase):
    def test_passives_and_dnp_skipped_unless_all(self):
        circuit = {"components": {"U2": part("U2", "LP591233MDRVREP"), "R1": part("R1", "AF0402FR-0710KL"),
                                  "C2": part("C2", "GCM21BR71E225KA73L"), "U3": part("U3", "X1", dnp=True),
                                  "U4": part("U4", "LP591233MDRVREP")}}
        self.assertEqual(datasheets.wanted_parts(circuit), {"LP591233MDRVREP": ["U2", "U4"]})
        self.assertEqual(set(datasheets.wanted_parts(circuit, include_passives=True)),
                         {"LP591233MDRVREP", "AF0402FR-0710KL", "GCM21BR71E225KA73L"})


class CliTests(Base):
    def test_cli_offline_with_docs_folder(self):
        make_pdf(self.docs / "lp5912-ep.pdf", "LP5912")
        circuit_path = self.base / "circuit.json"
        circuit_path.write_text(json.dumps({**self.circuit, "components": {
            "U2": part("U2", "LP591233MDRVREP"), "U9": part("U9", "MISSING-1")}}))
        proc = subprocess.run([sys.executable, "-m", "kicad_checker", "datasheet-fetch", str(circuit_path),
                               "--output", str(self.base / "out"), "--cache", str(self.cache),
                               "--digikey-cache", str(self.dk), "--offline"], cwd=ROOT, capture_output=True, text=True)
        self.assertEqual(proc.returncode, 2, proc.stderr)  # one missing
        report = json.loads((self.base / "out/datasheets.json").read_text())
        self.assertEqual(report["coverage"], {"found": 1, "needs_review": 0, "missing": 1})
        self.assertIn("lp5912-ep.pdf", (self.base / "out/datasheets.md").read_text())


if __name__ == "__main__":
    unittest.main()
