"""Settings: built-in defaults.json, an optional <board>.refplane.json, then CLI flags.

Return-via ("stitch") rules are profiles: distance, how many signal vias may
share one return via, and the severity of a missing return via.
"""

from __future__ import annotations

import copy
import json
from dataclasses import dataclass, field
from pathlib import Path

from .project import wildcard_match

DEFAULTS_PATH = Path(__file__).with_name("defaults.json")
SEVERITIES = ("error", "warning", "info")


@dataclass(frozen=True)
class StitchRule:
    name: str
    distance_mm: float
    max_share: int
    severity: str

    def describe(self) -> str:
        return f"{self.name}: {self.distance_mm:g} mm, max {self.max_share} per return via, missing = {self.severity}"


@dataclass
class StitchRules:
    profiles: dict[str, StitchRule]
    default_profile: str = "high_speed"
    classes: dict[str, object] = field(default_factory=dict)     # class -> profile name or mm
    nets: list[tuple[str, object]] = field(default_factory=list)  # (pattern, profile name or mm)
    # Explicit mode: when this list is non-empty, the listed nets/classes are high speed and
    # every other net is low speed; name patterns are not used.
    high_speed: list[str] = field(default_factory=list)
    high_speed_profile: str = "high_speed"
    low_speed_profile: str = "low_speed"

    @property
    def explicit(self) -> bool:
        return bool(self.high_speed)

    def is_listed_high(self, net: str, classes: list[str]) -> bool:
        base = net.rsplit("/", 1)[-1].upper()
        for tok in self.high_speed:
            if tok in classes:
                return True
            t = tok.upper()
            if wildcard_match(t, base) or wildcard_match(t, net.upper()) or wildcard_match(t.lstrip("/"), net.upper().lstrip("/")):
                return True
        return False

    @property
    def default(self) -> StitchRule:
        return self.profiles[self.default_profile]

    def resolve(self, spec, base: StitchRule | None = None, label: str = "") -> StitchRule:
        base = base or self.default
        if isinstance(spec, StitchRule):
            return spec
        if isinstance(spec, (int, float)) or (isinstance(spec, str) and _is_float(spec)):
            return StitchRule(label or f"{base.name}@{float(spec):g}mm", float(spec), base.max_share, base.severity)
        if isinstance(spec, dict):
            return _rule(label or base.name, {**base.__dict__, **spec})
        if spec not in self.profiles:
            raise ValueError(f"unknown stitch profile {spec!r} (have {', '.join(self.profiles)})")
        return self.profiles[spec]

    def class_rule(self, cls: str) -> StitchRule | None:
        if cls in self.classes:
            return self.resolve(self.classes[cls], label=f"class {cls}")
        return None

    def rule_for(self, net: str, classes: list[str]) -> StitchRule:
        if self.explicit:
            listed = self.is_listed_high(net, classes)
            base = self.profiles[self.high_speed_profile if listed else self.low_speed_profile]
            own = [r for c in classes if (r := self.class_rule(c))]
            return strictest(own) if own else base
        specific = [r for c in classes if c != "Default" and (r := self.class_rule(c))]
        if specific:
            return strictest(specific)
        base = net.rsplit("/", 1)[-1]
        for pat, spec in self.nets:
            if wildcard_match(pat.upper(), base.upper()) or wildcard_match(pat.upper(), net.upper()):
                return self.resolve(spec, label=f"net {pat}")
        if "Default" in classes and (r := self.class_rule("Default")):
            return r
        return self.default

    def source_for(self, net: str, classes: list[str]) -> str:
        if self.explicit:
            if any(c in self.classes for c in classes):
                return "class rule"
            return "listed high speed" if self.is_listed_high(net, classes) else "not listed"
        if any(c != "Default" and c in self.classes for c in classes):
            return "class"
        base = net.rsplit("/", 1)[-1]
        if any(wildcard_match(p.upper(), base.upper()) or wildcard_match(p.upper(), net.upper()) for p, _ in self.nets):
            return "pattern"
        return "class" if "Default" in classes and "Default" in self.classes else "default"


def strictest(rules: list[StitchRule]) -> StitchRule:
    rank = {s: i for i, s in enumerate(SEVERITIES)}
    return min(rules, key=lambda r: (rank.get(r.severity, 9), r.distance_mm, r.max_share))


def _is_float(s) -> bool:
    try:
        float(s)
        return True
    except (TypeError, ValueError):
        return False


def _rule(name: str, d: dict) -> StitchRule:
    sev = str(d.get("severity", "error")).lower()
    if sev not in SEVERITIES:
        raise ValueError(f"stitch profile {name}: severity must be one of {', '.join(SEVERITIES)}")
    dist = float(d.get("distance_mm", 1.0))
    share = int(d.get("max_share", 2))
    if dist <= 0 or share < 1:
        raise ValueError(f"stitch profile {name}: distance_mm must be > 0 and max_share >= 1")
    return StitchRule(name, dist, share, sev)


def deep_merge(base: dict, over: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in (over or {}).items():
        if k.startswith("_"):
            continue
        if isinstance(v, dict) and isinstance(out.get(k), dict) and k != "nets":
            out[k] = deep_merge(out[k], v)
        elif k == "nets" and isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = {**v, **{p: s for p, s in out[k].items() if p not in v}}   # user patterns first
        else:
            out[k] = copy.deepcopy(v)
    return out


def board_config_path(board_path: Path) -> Path:
    return board_path.with_name(board_path.stem + ".refplane.json")


def load_settings(board_path: Path | None = None, extra: Path | None = None) -> tuple[dict, list[str]]:
    """Merged settings dict and the list of files it came from."""
    data = json.loads(DEFAULTS_PATH.read_text(encoding="utf-8"))
    sources = [str(DEFAULTS_PATH)]
    for p in ([board_config_path(board_path)] if board_path else []) + ([extra] if extra else []):
        if p and Path(p).exists():
            try:
                data = deep_merge(data, json.loads(Path(p).read_text(encoding="utf-8")))
            except json.JSONDecodeError as exc:
                raise ValueError(f"{p}: invalid JSON ({exc})")
            sources.append(str(p))
        elif p == extra and extra:
            raise ValueError(f"config file {extra} not found")
    return data, sources


def stitch_rules(settings: dict) -> StitchRules:
    st = settings.get("stitch") or {}
    profiles = {name: _rule(name, d) for name, d in (st.get("profiles") or {}).items() if not name.startswith("_")}
    if not profiles:
        profiles = {"high_speed": StitchRule("high_speed", 1.0, 2, "error")}
    default = st.get("default_profile", "high_speed")
    if default not in profiles:
        raise ValueError(f"stitch.default_profile {default!r} is not a profile ({', '.join(profiles)})")
    rules = StitchRules(profiles=profiles, default_profile=default,
                        classes={k: v for k, v in (st.get("classes") or {}).items() if not k.startswith("_")},
                        nets=[(p, s) for p, s in (st.get("nets") or {}).items() if not p.startswith("_")],
                        high_speed=[str(x).strip() for x in (st.get("high_speed") or []) if str(x).strip()],
                        high_speed_profile=st.get("high_speed_profile", "high_speed"),
                        low_speed_profile=st.get("low_speed_profile", "low_speed"))
    if rules.explicit:
        for key in ("high_speed_profile", "low_speed_profile"):
            if getattr(rules, key) not in profiles:
                raise ValueError(f"stitch.{key} {getattr(rules, key)!r} is not a profile ({', '.join(profiles)})")
    for c, spec in rules.classes.items():          # validate early
        rules.resolve(spec, label=f"class {c}")
    for p, spec in rules.nets:
        rules.resolve(spec, label=f"net {p}")
    return rules


def parse_list(spec: str | None) -> list[str]:
    return [s.strip() for s in (spec or "").split(",") if s.strip()]


def parse_class_overrides(spec: str | None) -> dict[str, object]:
    """'USB=0.6,Default=low_speed' -> {'USB': 0.6, 'Default': 'low_speed'}"""
    out: dict[str, object] = {}
    for item in [s.strip() for s in (spec or "").split(",") if s.strip()]:
        if "=" not in item:
            raise ValueError(f"expected CLASS=MM or CLASS=PROFILE, got {item!r}")
        cls, val = (x.strip() for x in item.split("=", 1))
        out[cls] = float(val) if _is_float(val) else val
    return out


def power_nets(settings: dict, board, exclude: set[str] | None = None) -> set[str]:
    """Nets that look like power rails: name patterns, or (option) a non-GND net with a zone fill.

    These are skipped by the trace and layer-change checks unless named with --nets or
    --include-power, because routed supply traces are not signals.
    """
    cfg = settings.get("power_nets") or {}
    pats = [p for p in (cfg.get("patterns") or []) if isinstance(p, str) and not p.startswith("_")]
    out = set()
    for n in board.nets:
        base = n.rsplit("/", 1)[-1].upper()
        if any(wildcard_match(pt.upper(), base) or wildcard_match(pt.upper(), n.upper()) for pt in pats):
            out.add(n)
    if cfg.get("nets_with_zones", True):
        for z in board.zones:
            if z.net and z.fills:
                out.add(z.net)
    return out - (exclude or set())
