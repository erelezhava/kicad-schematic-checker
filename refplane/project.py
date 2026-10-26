"""Net classes from a KiCad .kicad_pro (KiCad 7-10 JSON project file).

A net gets its classes from, in order: explicit assignments
(net_settings.netclass_assignments, written by the schematic's net class
directives), then pattern rules (net_settings.netclass_patterns). KiCad 8+
allows a net to match several classes; all matches are kept. Nets with no
match are in "Default".
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class NetClasses:
    classes: list[str] = field(default_factory=lambda: ["Default"])
    assignments: dict[str, list[str]] = field(default_factory=dict)
    patterns: list[tuple[str, str]] = field(default_factory=list)   # (pattern, class)
    clearance: dict[str, float] = field(default_factory=dict)       # class -> clearance, mm
    source: Path | None = None

    def clearance_for(self, net: str) -> float | None:
        vals = [self.clearance[c] for c in self.classes_for(net) if c in self.clearance]
        return max(vals) if vals else None

    def classes_for(self, net: str) -> list[str]:
        if net in self.assignments:
            return list(self.assignments[net])
        found = [cls for pat, cls in self.patterns if wildcard_match(pat, net)]
        out = []
        for c in found:
            if c not in out:
                out.append(c)
        return out or ["Default"]


def wildcard_match(pattern: str, text: str) -> bool:
    """KiCad-style wildcard: '*' and '?' only; everything else is literal."""
    rx = "".join(".*" if ch == "*" else "." if ch == "?" else re.escape(ch) for ch in pattern)
    return re.fullmatch(rx, text) is not None


def load_netclasses(pro_path: str | Path | None) -> NetClasses:
    if pro_path is None:
        return NetClasses()
    pro_path = Path(pro_path)
    if not pro_path.exists():
        return NetClasses()
    data = json.loads(pro_path.read_text(encoding="utf-8"))
    ns = data.get("net_settings") or {}
    nc = NetClasses(source=pro_path)
    names = [c.get("name") for c in ns.get("classes") or [] if c.get("name")]
    for c in ns.get("classes") or []:
        if c.get("name") and isinstance(c.get("clearance"), (int, float)):
            nc.clearance[c["name"]] = float(c["clearance"])
    if names:
        nc.classes = names if "Default" in names else ["Default"] + names
    for net, cls in (ns.get("netclass_assignments") or {}).items():
        if isinstance(cls, str):
            cls = [c.strip() for c in cls.split(",") if c.strip()]
        nc.assignments[net] = [c for c in cls if c]
    for p in ns.get("netclass_patterns") or []:
        if p.get("pattern") and p.get("netclass"):
            nc.patterns.append((p["pattern"], p["netclass"]))
    for cls_list in nc.assignments.values():
        for c in cls_list:
            if c not in nc.classes:
                nc.classes.append(c)
    for _, c in nc.patterns:
        if c not in nc.classes:
            nc.classes.append(c)
    return nc
