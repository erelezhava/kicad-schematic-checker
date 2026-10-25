"""Manufacturing profile: the board facts that decide which JLCPCB limits apply."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path

CAPABILITIES_PATH = Path(__file__).with_name("capabilities.json")

TIERS = ("tiered", "standard", "advanced")
FINISHES = ("HASL", "HASL_LF", "ENIG", "OSP")
MASK_COLORS = ("green", "purple", "red", "yellow", "blue", "white", "black")
# Paid options whose warnings can be silenced once you have decided to pay for them.
ACCEPT_KEYS = ("small_via", "hdi", "via_in_pad", "backdrill")


def load_capabilities(path: str | Path | None = None) -> dict:
    with open(path or CAPABILITIES_PATH, encoding="utf-8") as fh:
        return json.load(fh)


@dataclass
class Profile:
    layers: int = 2
    outer_oz: float = 1.0
    inner_oz: float = 0.5
    thickness: float = 1.6
    finish: str | None = None          # None = unknown (treated conservatively)
    mask_color: str = "green"
    tier: str = "tiered"
    accept: list[str] = field(default_factory=list)
    via_fill_default: bool = False     # board setup: vias filled by default
    via_cap_default: bool = False      # board setup: vias capped by default
    notes: list[str] = field(default_factory=list)  # how values were detected

    def validate(self) -> list[str]:
        problems = []
        if self.tier not in TIERS:
            problems.append(f"tier must be one of {TIERS}")
        if self.finish is not None and self.finish not in FINISHES:
            problems.append(f"finish must be one of {FINISHES}")
        if self.mask_color not in MASK_COLORS:
            problems.append(f"mask color must be one of {MASK_COLORS}")
        for key in self.accept:
            if key not in ACCEPT_KEYS:
                problems.append(f"unknown --accept key '{key}', use {ACCEPT_KEYS}")
        if self.layers < 1:
            problems.append("layers must be >= 1")
        return problems

    @property
    def multilayer(self) -> bool:
        return self.layers >= 4

    @property
    def micro_drill_ok(self) -> bool:
        """0.10 mm drills: board <= 1.0 mm and ENIG/OSP finish."""
        return (self.layers >= 2 and self.thickness <= 1.0 + 1e-9
                and self.finish in ("ENIG", "OSP"))

    def key(self) -> str:
        """Short name used for preset files."""
        cu = f"{_fmt_oz(self.outer_oz)}oz"
        if self.multilayer:
            cu += f"-{_fmt_oz(self.inner_oz)}oz"
        finish = self.finish or "anyfinish"
        return f"jlc_{self.layers}L_{cu}_{self.thickness:g}mm_{finish}_{self.mask_color}"

    def to_json(self) -> str:
        data = asdict(self)
        data.pop("notes")
        return json.dumps(data, sort_keys=True, separators=(",", ":"))

    @classmethod
    def from_json(cls, text: str) -> "Profile":
        return cls(**json.loads(text))


def _fmt_oz(oz: float) -> str:
    return f"{oz:g}"


def profile_from_board(info, **overrides) -> Profile:
    """Build a Profile from BoardInfo; explicit overrides win over detection."""
    p = Profile()
    notes = []
    if info.layer_count:
        p.layers = info.layer_count
        notes.append(f"layers={p.layers} from board layer list")
    if info.thickness:
        p.thickness = info.thickness
        notes.append(f"thickness={p.thickness:g} mm from board setup")
    if info.outer_oz:
        p.outer_oz = info.outer_oz
        notes.append(f"outer copper={p.outer_oz:g} oz from stackup")
    elif info.has_stackup:
        notes.append("outer copper not recognised in stackup, assuming 1 oz")
    else:
        notes.append("no stackup in board file, assuming 1 oz outer / 0.5 oz inner")
    if info.inner_oz:
        p.inner_oz = info.inner_oz
        notes.append(f"inner copper={p.inner_oz:g} oz from stackup")
    if info.finish:
        p.finish = info.finish
        notes.append(f"finish={p.finish} from stackup ('{info.finish_raw}')")
    else:
        notes.append(f"finish not set or not offered by JLC ('{info.finish_raw}'): "
                     "treated as unknown (no 0.1 mm drills, BGA pads >= 0.25 mm)")
    if info.mask_color:
        p.mask_color = info.mask_color
        notes.append(f"mask color={p.mask_color} from stackup")
    p.via_fill_default = info.default_via_filled
    p.via_cap_default = info.default_via_capped

    for name, value in overrides.items():
        if value is not None:
            setattr(p, name, value)
            notes.append(f"{name}={value} set on the command line")
    p.notes = notes
    return p
