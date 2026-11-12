"""Read the parts of a .kicad_pcb file that the JLCPCB rules depend on.

Standard library only. The parser is a small S-expression reader, enough for
KiCad 10 board files; it does not try to understand every token.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from pathlib import Path

_TOKEN = re.compile(r'\(|\)|"(?:[^"\\]|\\.)*"|[^\s()"]+')


_ESCAPES = {"n": "\n", "t": "\t", "r": "\r"}


def _unescape(body: str) -> str:
    """KiCad string escapes (\\" \\\\ \\n). Non-ASCII text is left exactly as written."""
    return re.sub(r"\\(.)", lambda m: _ESCAPES.get(m.group(1), m.group(1)), body, flags=re.S)


def parse_sexpr(text: str):
    """Parse S-expression text into nested lists. Strings lose their quotes."""
    stack: list[list] = [[]]
    for match in _TOKEN.finditer(text):
        tok = match.group(0)
        if tok == "(":
            stack.append([])
        elif tok == ")":
            if len(stack) == 1:
                raise ValueError("unbalanced ')' in S-expression")
            node = stack.pop()
            stack[-1].append(node)
        elif tok.startswith('"'):
            stack[-1].append(_unescape(tok[1:-1]) if "\\" in tok else tok[1:-1])
        else:
            stack[-1].append(tok)
    if len(stack) != 1:
        raise ValueError("unbalanced '(' in S-expression")
    return stack[0]


def _children(node, name):
    return [c for c in node if isinstance(c, list) and c and c[0] == name]


def _child(node, name):
    found = _children(node, name)
    return found[0] if found else None


def _value(node, name, default=None):
    c = _child(node, name)
    return c[1] if c is not None and len(c) > 1 else default


# KiCad copper_finish strings -> JLC finish names
_FINISH_MAP = {
    "hal snpb": "HASL",
    "hal lead-free": "HASL_LF",
    "enig": "ENIG",
    "immersion gold": "ENIG",
    "osp": "OSP",
    "ht_osp": "OSP",
}

# copper thickness in mm -> oz
_OZ_TABLE = [(0.0175, 0.5), (0.035, 1.0), (0.07, 2.0), (0.0875, 2.5),
             (0.1225, 3.5), (0.1575, 4.5)]


def thickness_to_oz(mm: float) -> float | None:
    for ref, oz in _OZ_TABLE:
        if abs(mm - ref) <= ref * 0.15:
            return oz
    return None


@dataclass
class BoardInfo:
    path: Path
    copper_layers: list[str] = field(default_factory=list)
    thickness: float | None = None
    copper_mm: dict[str, float] = field(default_factory=dict)
    finish_raw: str | None = None
    mask_color_raw: str | None = None
    outline_bbox: tuple[float, float, float, float] | None = None
    has_stackup: bool = False
    default_via_filled: bool = False
    default_via_capped: bool = False
    default_tenting: dict = field(default_factory=lambda: {"front": True, "back": True})
    mask_expansion: float = 0.0          # board setup: solder mask expansion (pad_to_mask_clearance)
    file_version: int | None = None      # (version 20260206): KiCad 10; 2024xxxx: KiCad 8/9
    vias: list[dict] = field(default_factory=list)
    pads: list[dict] = field(default_factory=list)

    @property
    def layer_count(self) -> int:
        return len(self.copper_layers)

    @property
    def finish(self) -> str | None:
        if not self.finish_raw:
            return None
        return _FINISH_MAP.get(self.finish_raw.strip().lower())

    @property
    def mask_color(self) -> str | None:
        if not self.mask_color_raw:
            return None
        raw = self.mask_color_raw.lower()
        for color in ("green", "purple", "red", "yellow", "blue", "white", "black"):
            if color in raw:
                return color
        return None

    def oz(self, layer: str) -> float | None:
        mm = self.copper_mm.get(layer)
        return thickness_to_oz(mm) if mm is not None else None

    @property
    def outer_oz(self) -> float | None:
        vals = [self.oz(l) for l in ("F.Cu", "B.Cu") if l in self.copper_mm]
        vals = [v for v in vals if v is not None]
        return max(vals) if vals else None

    @property
    def inner_oz(self) -> float | None:
        vals = [self.oz(l) for l in self.copper_layers if l not in ("F.Cu", "B.Cu")]
        vals = [v for v in vals if v is not None]
        return max(vals) if vals else None

    @property
    def size(self) -> tuple[float, float] | None:
        if not self.outline_bbox:
            return None
        x0, y0, x1, y1 = self.outline_bbox
        return (round(x1 - x0, 3), round(y1 - y0, 3))


def _xy(node):
    return float(node[1]), float(node[2])


def _shape_points(item):
    """Points that bound an Edge.Cuts graphic (arcs/circles sampled)."""
    kind = item[0]
    pts = []
    if kind in ("gr_line", "fp_line", "gr_rect", "fp_rect"):
        for key in ("start", "end"):
            c = _child(item, key)
            if c:
                pts.append(_xy(c))
    elif kind in ("gr_arc", "fp_arc"):
        s, m, e = _child(item, "start"), _child(item, "mid"), _child(item, "end")
        if s and m and e:
            pts += _arc_points(_xy(s), _xy(m), _xy(e))
    elif kind in ("gr_circle", "fp_circle"):
        c, e = _child(item, "center"), _child(item, "end")
        if c and e:
            cx, cy = _xy(c)
            r = math.dist((cx, cy), _xy(e))
            pts += [(cx - r, cy - r), (cx + r, cy + r)]
    elif kind in ("gr_poly", "fp_poly"):
        p = _child(item, "pts")
        if p:
            pts += [_xy(xy) for xy in _children(p, "xy")]
    return pts


def _arc_points(s, m, e, steps=32):
    (x1, y1), (x2, y2), (x3, y3) = s, m, e
    d = 2 * (x1 * (y2 - y3) + x2 * (y3 - y1) + x3 * (y1 - y2))
    if abs(d) < 1e-12:
        return [s, m, e]
    ux = ((x1**2 + y1**2) * (y2 - y3) + (x2**2 + y2**2) * (y3 - y1) + (x3**2 + y3**2) * (y1 - y2)) / d
    uy = ((x1**2 + y1**2) * (x3 - x2) + (x2**2 + y2**2) * (x1 - x3) + (x3**2 + y3**2) * (x2 - x1)) / d
    r = math.dist((ux, uy), s)
    a1 = math.atan2(y1 - uy, x1 - ux)
    a2 = math.atan2(y2 - uy, x2 - ux)
    a3 = math.atan2(y3 - uy, x3 - ux)

    def norm(a):
        return a % (2 * math.pi)

    sweep = norm(a3 - a1)
    if norm(a2 - a1) > sweep:  # mid not on CCW path -> go the other way
        sweep -= 2 * math.pi
    return [(ux + r * math.cos(a1 + sweep * i / steps),
             uy + r * math.sin(a1 + sweep * i / steps)) for i in range(steps + 1)]


def _footprint_transform(fp):
    at = _child(fp, "at")
    if not at:
        return 0.0, 0.0, 0.0
    x, y = float(at[1]), float(at[2])
    rot = float(at[3]) if len(at) > 3 and _is_number(at[3]) else 0.0
    return x, y, rot


def _is_number(s) -> bool:
    try:
        float(s)
        return True
    except (TypeError, ValueError):
        return False


def _to_board(fx, fy, rot, x, y):
    a = math.radians(-rot)  # KiCad Y points down; footprint rotation is CCW on screen
    return (fx + x * math.cos(a) - y * math.sin(a), fy + x * math.sin(a) + y * math.cos(a))


def read_board(path: str | Path) -> BoardInfo:
    path = Path(path)
    root = parse_sexpr(path.read_text(encoding="utf-8"))
    if not root or not isinstance(root[0], list) or root[0][0] != "kicad_pcb":
        raise ValueError(f"{path} is not a KiCad board file")
    pcb = root[0]
    info = BoardInfo(path=path)

    version = _value(pcb, "version")
    info.file_version = int(version) if str(version).isdigit() else None
    # Net table: KiCad 9 and older write (net 12) on vias and tracks and name the net only here.
    netmap = {str(n[1]): n[2] for n in _children(pcb, "net") if len(n) >= 3}

    layers = _child(pcb, "layers")
    if layers:
        for entry in layers[1:]:
            if isinstance(entry, list) and len(entry) >= 2 and str(entry[1]).endswith(".Cu"):
                info.copper_layers.append(entry[1])

    general = _child(pcb, "general")
    if general is not None:
        t = _value(general, "thickness")
        info.thickness = float(t) if t is not None else None

    setup = _child(pcb, "setup")
    if setup is not None:
        info.default_via_filled = _value(setup, "filling") == "yes"
        info.default_via_capped = _value(setup, "capping") == "yes"
        tent = _tenting(_child(setup, "tenting"))
        if tent is not None:
            info.default_tenting = {k: (True if v is None else v) for k, v in tent.items()}
        m = _value(setup, "pad_to_mask_clearance")
        info.mask_expansion = float(m) if _is_number(m) else 0.0
    stackup = _child(setup, "stackup") if setup else None
    if stackup:
        info.has_stackup = True
        info.finish_raw = _value(stackup, "copper_finish")
        for layer in _children(stackup, "layer"):
            name = layer[1]
            if name.endswith(".Cu"):
                t = _value(layer, "thickness")
                if t is not None:
                    info.copper_mm[name] = float(t)
            elif name in ("F.Mask", "B.Mask"):
                color = _value(layer, "color")
                if color and not info.mask_color_raw:
                    info.mask_color_raw = color

    # Board outline bounding box (board-level and footprint Edge.Cuts graphics)
    pts = []
    for item in pcb[1:]:
        if isinstance(item, list) and item and item[0].startswith("gr_") and _value(item, "layer") == "Edge.Cuts":
            pts += _shape_points(item)
    for fp in _children(pcb, "footprint"):
        fx, fy, rot = _footprint_transform(fp)
        for item in fp[1:]:
            if isinstance(item, list) and item and item[0].startswith("fp_") and _value(item, "layer") == "Edge.Cuts":
                pts += [_to_board(fx, fy, rot, x, y) for x, y in _shape_points(item)]
        for pad in _children(fp, "pad"):
            info.pads.append(_pad_summary(fp, pad, fx, fy, rot, netmap))
    if pts:
        xs, ys = [p[0] for p in pts], [p[1] for p in pts]
        info.outline_bbox = (min(xs), min(ys), max(xs), max(ys))

    for via in _children(pcb, "via"):
        info.vias.append(_via_summary(via, netmap))
    return info


def _via_summary(via, netmap=None) -> dict:
    kind = "through"
    if len(via) > 1 and via[1] in ("blind", "buried", "micro"):
        kind = via[1]
    size = _value(via, "size")
    drill = _value(via, "drill")
    at = _child(via, "at")
    layers = _child(via, "layers")
    return {
        "type": kind,
        "at": _xy(at) if at else None,
        "diameter": float(size) if _is_number(size) else None,
        "hole": float(drill) if _is_number(drill) else None,
        "net": _net(via, netmap),
        "layers": [l for l in layers[1:] if isinstance(l, str)] if layers else [],
        "tenting": _tenting(_child(via, "tenting")),     # None or {"front": bool|None, "back": ...}
        "backdrill": _child(via, "backdrill") is not None,
        "post_machining": _has_post_machining(via),
    }


def _has_post_machining(node) -> bool:
    return any(_child(node, key) is not None
               for key in ("front_post_machining", "back_post_machining"))


def _net(node, netmap=None) -> str | None:
    """Net name. KiCad 10 writes (net "GND"); older files (net 3 "GND") on pads and (net 3) on
    vias and tracks, where the name comes from the board's net table."""
    n = _child(node, "net")
    if n is None or len(n) < 2:
        return None
    if len(n) >= 3:
        return n[2] or None
    if _is_number(n[1]):
        return (netmap or {}).get(str(n[1])) or None
    return n[1] or None


def _tenting(node) -> dict | None:
    """Tenting per side: True/False, None = follow the board setup. Handles
    (tenting (front yes) (back no)), (tenting front back) and (tenting none)."""
    if node is None:
        return None
    out: dict = {"front": None, "back": None}
    for item in node[1:]:
        if isinstance(item, list) and len(item) > 1 and item[0] in out:
            out[item[0]] = {"yes": True, "no": False}.get(item[1])
        elif item in out:
            out[item] = True
        elif item == "none":
            out = {"front": False, "back": False}
    if all(isinstance(i, str) for i in node[1:]) and node[1:] and "none" not in node[1:]:
        out = {k: (v is True) for k, v in out.items()}  # atom form lists only the tented sides
    return out


def _pad_summary(fp, pad, fx=0.0, fy=0.0, rot=0.0, netmap=None) -> dict:
    ref = None
    for prop in _children(fp, "property"):
        if len(prop) > 2 and prop[1] == "Reference":
            ref = prop[2]
    ref = ref or _value(fp, "reference")
    drill = _child(pad, "drill")
    hole, slot = None, False
    if drill is not None:
        nums = [float(x) for x in drill[1:] if _is_number(x)]
        hole = min(nums) if nums else None
        slot = "oval" in drill[1:]
    at, size = _child(pad, "at"), _child(pad, "size")
    layers = _child(pad, "layers")
    local = _xy(at) if at else (0.0, 0.0)
    angle = float(at[3]) if at is not None and len(at) > 3 and _is_number(at[3]) else 0.0
    pad_m, fp_m = _value(pad, "solder_mask_margin"), _value(fp, "solder_mask_margin")
    rr = _value(pad, "roundrect_rratio")
    return {
        "ref": ref,
        "footprint": fp[1] if len(fp) > 1 and isinstance(fp[1], str) else "",
        "number": pad[1] if len(pad) > 1 else None,
        "type": pad[2] if len(pad) > 2 else None,
        "shape": pad[3] if len(pad) > 3 and isinstance(pad[3], str) else None,
        "at": _xy(at) if at else None,                      # footprint-relative
        "xy": _to_board(fx, fy, rot, *local),               # board coordinates
        "angle": angle,                                     # absolute pad orientation, degrees
        "size": _xy(size) if size else None,
        "net": _net(pad, netmap),
        "layers": [l for l in layers[1:] if isinstance(l, str)] if layers else [],
        "rratio": float(rr) if _is_number(rr) else 0.25,
        "mask_margin": float(pad_m) if _is_number(pad_m) else float(fp_m) if _is_number(fp_m) else None,
        "fab_property": _value(pad, "property"),
        "hole": hole,
        "slot": slot,
        "backdrill": _child(pad, "backdrill") is not None,
        "post_machining": _has_post_machining(pad),
    }
