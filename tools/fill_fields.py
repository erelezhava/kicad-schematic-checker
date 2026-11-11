#!/usr/bin/env python3
"""Fill EMPTY schematic Manufacturer fields from DigiKey (opt-in).

Kept for existing scripts; the same tool is now
    python3 -m kicad_checker fill-manufacturer PROJECT [--report R] [--apply]
which finds the newest <project>_report_R<n> on its own. See kicad_checker/fill.py.

Usage:
    python3 tools/fill_fields.py runs/board-dk/digikey.json /path/to/project            # preview
    python3 tools/fill_fields.py runs/board-dk/digikey.json /path/to/project --apply    # write
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from kicad_checker.fill import legacy_main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(legacy_main())
