"""Where `run` looks for project documents and CubeMX files: never the home folder."""

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from kicad_checker import cubemx, datasheets
from kicad_checker.paths import search_folders


class SearchFolders(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name).resolve() / "home" / "user"
        self.home.mkdir(parents=True)
        patcher = mock.patch("kicad_checker.paths.Path.home", return_value=self.home)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self.tmp.cleanup)

    def project(self, *parts, git_at=None):
        folder = self.home.joinpath(*parts)
        folder.mkdir(parents=True)
        (folder / "board.kicad_pro").write_text("{}")
        if git_at is not None:
            (self.home.joinpath(*parts[:git_at]) / ".git").mkdir()
        return folder

    def rel(self, folders):
        return [str(f.relative_to(self.home)) for f in folders]

    def test_repository_is_searched_up_to_its_root(self):
        proj = self.project("work", "sat", "hw", "board", git_at=2)
        self.assertEqual(self.rel(search_folders(proj)), ["work/sat/hw/board", "work/sat/hw", "work/sat"])

    def test_without_repository_only_project_and_parent(self):
        proj = self.project("a", "b", "c", "board")
        self.assertEqual(self.rel(search_folders(proj)), ["a/b/c/board", "a/b/c"])

    def test_home_folder_is_never_searched(self):
        proj = self.project("board")                      # ~/board: the parent is the home folder
        self.assertEqual(search_folders(proj), [proj])
        proj2 = self.project("kicad", "board2")            # ~/kicad/board2
        self.assertEqual(self.rel(search_folders(proj2)), ["kicad/board2", "kicad"])

    def test_repository_at_home_does_not_open_the_home_folder(self):
        (self.home / ".git").mkdir()                       # dotfiles repository
        proj = self.project("kicad", "board")
        self.assertEqual(self.rel(search_folders(proj)), ["kicad/board", "kicad"])

    def test_private_documents_and_foreign_ioc_files_are_not_found(self):
        proj = self.project("kicad", "board")
        (self.home / "Documents" / "tax").mkdir(parents=True)
        (self.home / "Documents" / "tax" / "private.pdf").write_bytes(b"%PDF-1.4")
        (self.home / "stm32" / "other").mkdir(parents=True)
        (self.home / "stm32" / "other" / "other.ioc").write_text("Mcu.CPN=STM32F103C8T6\n")
        (proj / "docs").mkdir()
        (proj / "docs" / "lp5912.pdf").write_bytes(b"%PDF-1.4")
        (proj / "fw").mkdir()
        (proj / "fw" / "board.ioc").write_text("Mcu.CPN=STM32H743VIH6\n")
        folders = [d for f in search_folders(proj) for d in datasheets.project_doc_folders(f)]
        self.assertEqual([p.name for p in datasheets.local_pdfs(folders)], ["lp5912.pdf"])
        self.assertEqual([p.name for p in cubemx.find_ioc(proj / "board.kicad_pro")], ["board.ioc"])


class WeakDatasheetMatch(unittest.TestCase):
    def test_family_match_is_found_but_marked_for_verification(self):
        with tempfile.TemporaryDirectory() as tmp:
            docs = Path(tmp) / "docs"
            docs.mkdir()
            (docs / "DS_stm32h743vi.pdf").write_bytes(b"%PDF-1.4")
            (docs / "LP591233MDRVREP.pdf").write_bytes(b"%PDF-1.4")
            circuit = {"components": {
                "U1": {"reference": "U1", "value": "x", "fields": {"MPN": "STM32H743VIH6"}, "dnp": False},
                "U2": {"reference": "U2", "value": "x", "fields": {"MPN": "LP591233MDRVREP"}, "dnp": False}}}
            with mock.patch.object(datasheets, "pdftotext_available", return_value=False):
                report = datasheets.find_datasheets(circuit, cache_dir=Path(tmp) / "cache", digikey_cache=Path(tmp) / "dk",
                                                    doc_folders=[docs], offline=True, log=lambda *_: None)
            by_mpn = {r["mpn"]: r for r in report["results"]}
            self.assertEqual(by_mpn["STM32H743VIH6"]["status"], "found")
            self.assertTrue(by_mpn["STM32H743VIH6"].get("verify"))
            self.assertFalse(by_mpn["LP591233MDRVREP"].get("verify"))      # file name holds the full MPN
            self.assertEqual(report["found_to_verify"], 1)
            self.assertIn("found (verify)", datasheets.datasheets_markdown(report))

    def test_only_web_addresses_are_downloaded(self):
        calls = []
        with tempfile.TemporaryDirectory() as tmp:
            for url in ("file:///etc/passwd", "ftp://host/x.pdf", "javascript:alert(1)"):
                entry, reason = datasheets.download(url, tmp, "X1", opener=lambda *a, **k: calls.append(a))
                self.assertIsNone(entry)
                self.assertIn("http", reason)
        self.assertEqual(calls, [])


if __name__ == "__main__":
    unittest.main()
