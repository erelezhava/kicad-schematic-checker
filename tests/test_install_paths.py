"""Cache location: the checker folder in a source checkout, the user's cache folder when installed."""

import os
import tempfile
import unittest
from pathlib import Path

from kicad_checker import credentials, run
from kicad_checker.paths import CACHE_ROOT, CHECKER_ROOT, cache_root


class CacheRoot(unittest.TestCase):
    def test_source_checkout_keeps_its_cache_folder(self):
        if not os.environ.get("KICAD_CHECKER_CACHE"):
            self.assertEqual(CACHE_ROOT, CHECKER_ROOT / "cache")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "pyproject.toml").write_text("")
            self.assertEqual(cache_root(root, environ={}), root / "cache")

    def test_installed_copy_uses_the_user_cache_folder(self):
        with tempfile.TemporaryDirectory() as tmp:
            site = Path(tmp) / "site-packages"
            site.mkdir()
            found = cache_root(site, environ={}, home=Path(tmp) / "home")
            self.assertNotIn(site, found.parents)
            self.assertEqual(found.name, "kicad_checker")
            if os.name != "nt":
                self.assertEqual(found, Path(tmp) / "home" / ".cache" / "kicad_checker")
                self.assertEqual(cache_root(site, environ={"XDG_CACHE_HOME": "/x/cache"}), Path("/x/cache/kicad_checker"))

    def test_environment_variable_wins(self):
        self.assertEqual(cache_root(CHECKER_ROOT, environ={"KICAD_CHECKER_CACHE": "/data/kc"}), Path("/data/kc"))


class SecretNames(unittest.TestCase):
    def test_every_name_in_env_example_is_a_known_key(self):
        example = (CHECKER_ROOT / ".env.example").read_text(encoding="utf-8")
        names = {line.lstrip("# ").split("=")[0] for line in example.splitlines()
                 if "=" in line and line.lstrip("# ").split("=")[0].isupper()}
        self.assertTrue(names)
        self.assertLessEqual(names, set(credentials.KNOWN_KEYS))

    def test_secrets_are_masked_without_env_example(self):
        from unittest import mock
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.object(run, "CHECKER_ROOT", Path(tmp)), \
                mock.patch.dict(os.environ, {"LCSC_APP_SECRET": "lcsc-secret-9876"}):
            self.assertIn("lcsc-secret-9876", run._secrets())


if __name__ == "__main__":
    unittest.main()
