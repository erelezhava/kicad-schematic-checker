"""Local secrets for external sources (DigiKey now; Mouser, Nexar etc. later).

Each user keeps their own keys; nothing here is ever committed or printed.
Lookup order for every key, first non-empty value wins:

1. environment variable (CI, one-off shell use)
2. `.env` in the checker folder (git-ignored; copy `.env.example`)
3. `~/.config/kicad_checker/credentials.env` (shared by all checkouts on this machine)
"""

import os
from pathlib import Path

REPO_ENV = Path(__file__).resolve().parents[1] / ".env"
USER_ENV = Path.home() / ".config" / "kicad_checker" / "credentials.env"


def read_env_file(path):
    """Parse KEY=VALUE lines; ignores blanks, comments and 'export ' prefixes."""
    values = {}
    path = Path(path)
    if not path.is_file():
        return values
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip().removeprefix("export ").strip()
        values[key] = value.strip().strip('"').strip("'")
    return values


def get(name, files=None):
    value = os.environ.get(name)
    if value:
        return value
    for path in files if files is not None else (REPO_ENV, USER_ENV):
        value = read_env_file(path).get(name)
        if value:
            return value
    return None


def require(names, service, files=None):
    """Return values for all names, or raise a message that names files, never values."""
    values = [get(name, files) for name in names]
    missing = [name for name, value in zip(names, values) if not value]
    if missing:
        where = files if files is not None else (REPO_ENV, USER_ENV)
        raise ValueError(f"{service} credentials missing ({', '.join(missing)}). Copy .env.example to .env and fill in "
                         f"your own keys, or set environment variables. Looked in: {', '.join(str(p) for p in where)}")
    return values
