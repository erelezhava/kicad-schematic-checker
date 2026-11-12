"""Locations inside the checker folder. Everything the tool creates stays in this one repo."""

import os
from pathlib import Path

CHECKER_ROOT = Path(__file__).resolve().parents[1]


def cache_root(root=CHECKER_ROOT, environ=None, home=None):
    """Where DigiKey answers, datasheet PDFs and PDF text are kept.

    1. KICAD_CHECKER_CACHE, when set.
    2. `cache/` in the checker folder when this is a source checkout (git-ignored), as always.
    3. Installed with pip/pipx (no checkout around the package): the user's cache folder,
       never site-packages.
    """
    environ = os.environ if environ is None else environ
    if environ.get("KICAD_CHECKER_CACHE"):
        return Path(environ["KICAD_CHECKER_CACHE"]).expanduser()
    root = Path(root)
    if (root / ".git").exists() or (root / "pyproject.toml").is_file() or (root / "cache").is_dir():
        return root / "cache"
    base = environ.get("LOCALAPPDATA") if os.name == "nt" else environ.get("XDG_CACHE_HOME")
    return Path(base or (Path(home) if home else Path.home()) / ".cache") / "kicad_checker"


CACHE_ROOT = cache_root()


def search_folders(project_dir, levels=3):
    """Folders that may hold material belonging to a KiCad project (docs/, CubeMX .ioc ...).

    The project folder, then its parents up to the repository root (the folder holding `.git`),
    at most `levels` up. Without a repository only the project folder and its parent are used.
    The search never reaches the user's home folder or anything above it, so unrelated files
    (~/Documents, other projects) are never read.
    """
    start = Path(project_dir).resolve()
    home = Path.home().resolve()
    off_limits = {home, *home.parents}
    chain = [start]
    while len(chain) <= levels and chain[-1].parent != chain[-1]:
        chain.append(chain[-1].parent)
    root = next((i for i, folder in enumerate(chain) if (folder / ".git").exists()), None)
    chain = chain[:root + 1] if root is not None else chain[:2]
    kept = [start]
    for folder in chain[1:]:
        if folder in off_limits:
            break
        kept.append(folder)
    return kept
