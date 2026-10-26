"""Read the copper geometry of a KiCad .kicad_pcb (KiCad 10; tolerant of 7-9).

Only what the reference-plane check needs: copper layer order and stackup,
tracks, vias, pads, and the *filled* polygons of zones. Antipads are not
objects in KiCad: they exist only as holes in zone fills, so the fills are the
truth. If the fills are stale, so is any answer computed from them.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from pathlib import Path

from .geometry import arc_points
from .sexpr import child, children, is_number, parse_sexpr, value, xy

Point = tuple[float, float]


@dataclass
class Track:
    net: str
    layer: str
    width: float
    points: list[Point]          # polyline: 2 points for a segment, sampled for arcs


@dataclass
class Via:
    net: str
    at: Point
    size: float
    layers: tuple[str, str]
    kind: str = "through"


@dataclass
class Pad:
    net: str
    at: Point
    radius: float                # half-diagonal of the pad: conservative
    through: bool
    layers: list[str]
    ref: str = ""
    number: str = ""
    size: tuple[float, float] = (0.0, 0.0)
    angle: float = 0.0           # absolute pad orientation, degrees (KiCad stores it absolute)
    shape: str = ""

    def on_layer(self, layer: str) -> bool:
        return self.through or layer in self.layers or ("*.Cu" in self.layers)


@dataclass
class Zone:
    net: str
    layers: list[str]
    fills: dict[str, list[list[Point]]] = field(default_factory=dict)  # layer -> polygons
    hatched: bool = False
    clearance: float | None = None
    name: str = ""


@dataclass
class Board:
    path: Path
    copper_layers: list[str] = field(default_factory=list)       # top -> bottom
    layer_types: dict[str, str] = field(default_factory=dict)    # signal / power / mixed / jumper
    dielectric: dict[tuple[str, str], float] = field(default_factory=dict)
    has_stackup: bool = False
    thickness: float | None = None
    nets: list[str] = field(default_factory=list)
    tracks: list[Track] = field(default_factory=list)
    vias: list[Via] = field(default_factory=list)
    pads: list[Pad] = field(default_factory=list)
    zones: list[Zone] = field(default_factory=list)
    outline_bbox: tuple[float, float, float, float] | None = None

    def h_between(self, a: str, b: str) -> float | None:
        return self.dielectric.get((a, b)) or self.dielectric.get((b, a))

    def index(self, layer: str) -> int:
        return self.copper_layers.index(layer)

    def via_spans(self, via: Via, layer: str) -> bool:
        if via.kind == "through":
            return layer in self.copper_layers
        try:
            i0, i1 = sorted(self.index(l) for l in via.layers)
            return i0 <= self.index(layer) <= i1
        except ValueError:
            return False


def copper_sort_key(name: str):
    if name == "F.Cu":
        return (0, 0)
    if name == "B.Cu":
        return (2, 0)
    m = re.match(r"In(\d+)\.Cu$", name)
    return (1, int(m.group(1))) if m else (1, 999)


def _net_of(node, netmap: dict[str, str]) -> str:
    nn = value(node, "net_name")
    if nn is not None:
        return str(nn)
    n = child(node, "net")
    if n is None or len(n) < 2:
        return ""
    if len(n) >= 3 and isinstance(n[2], str):
        return n[2]
    v = str(n[1])
    if v in netmap:
        return netmap[v]
    return "" if v == "0" else v


def _pts(node) -> list[Point]:
    pts: list[Point] = []
    p = child(node, "pts")
    if p is None:
        return pts
    for item in p[1:]:
        if not isinstance(item, list) or not item:
            continue
        if item[0] == "xy":
            pts.append(xy(item))
        elif item[0] == "arc":
            s, m, e = child(item, "start"), child(item, "mid"), child(item, "end")
            if s and m and e:
                pts.extend(arc_points(xy(s), xy(m), xy(e))[:-1] if pts else arc_points(xy(s), xy(m), xy(e)))
    return pts


def _rotate(fx, fy, rot, x, y) -> Point:
    a = math.radians(-rot)  # KiCad Y points down; rotation is CCW on screen
    return (fx + x * math.cos(a) - y * math.sin(a), fy + x * math.sin(a) + y * math.cos(a))


def _outline_points(item) -> list[Point]:
    kind = item[0]
    pts: list[Point] = []
    if kind in ("gr_line", "gr_rect"):
        for key in ("start", "end"):
            c = child(item, key)
            if c:
                pts.append(xy(c))
    elif kind == "gr_arc":
        s, m, e = child(item, "start"), child(item, "mid"), child(item, "end")
        if s and m and e:
            pts += arc_points(xy(s), xy(m), xy(e))
    elif kind == "gr_circle":
        c, e = child(item, "center"), child(item, "end")
        if c and e:
            cx, cy = xy(c)
            r = math.dist((cx, cy), xy(e))
            pts += [(cx - r, cy - r), (cx + r, cy + r)]
    elif kind == "gr_poly":
        pts += _pts(item)
    return pts


def read_board(path: str | Path) -> Board:
    path = Path(path)
    root = parse_sexpr(path.read_text(encoding="utf-8"))
    if not root or not isinstance(root[0], list) or root[0][0] != "kicad_pcb":
        raise ValueError(f"{path} is not a KiCad board file")
    pcb = root[0]
    b = Board(path=path)

    # Layers and their KiCad types
    layers = child(pcb, "layers")
    copper = []
    if layers:
        for entry in layers[1:]:
            if isinstance(entry, list) and len(entry) >= 3 and str(entry[1]).endswith(".Cu"):
                copper.append(entry[1])
                b.layer_types[entry[1]] = str(entry[2])

    general = child(pcb, "general")
    if general is not None and is_number(value(general, "thickness")):
        b.thickness = float(value(general, "thickness"))

    # Stackup: order and dielectric thickness between copper layers
    setup = child(pcb, "setup")
    stackup = child(setup, "stackup") if setup else None
    order: list[str] = []
    if stackup:
        b.has_stackup = True
        last_cu, acc = None, 0.0
        for layer in children(stackup, "layer"):
            name = layer[1]
            if name.endswith(".Cu"):
                order.append(name)
                if last_cu is not None and acc > 0:
                    b.dielectric[(last_cu, name)] = round(acc, 6)
                last_cu, acc = name, 0.0
            elif value(layer, "type") in ("core", "prepreg") or str(name).startswith("dielectric"):
                # all (thickness ...) children, including sub-layers
                acc += sum(float(t[1]) for t in children(layer, "thickness") if is_number(t[1]))
    if order and set(order) == set(copper):
        b.copper_layers = order
    else:
        b.copper_layers = sorted(copper, key=copper_sort_key)

    # Nets
    netmap: dict[str, str] = {}
    for n in children(pcb, "net"):
        if len(n) >= 3:
            netmap[str(n[1])] = n[2]
        elif len(n) == 2 and not is_number(n[1]):
            netmap[n[1]] = n[1]
    names = set(v for v in netmap.values() if v)

    # Tracks
    for seg in children(pcb, "segment"):
        s, e = child(seg, "start"), child(seg, "end")
        if not (s and e):
            continue
        t = Track(_net_of(seg, netmap), value(seg, "layer", ""), float(value(seg, "width", 0) or 0),
                  [xy(s), xy(e)])
        b.tracks.append(t)
    for arc in children(pcb, "arc"):
        s, m, e = child(arc, "start"), child(arc, "mid"), child(arc, "end")
        if not (s and m and e):
            continue
        b.tracks.append(Track(_net_of(arc, netmap), value(arc, "layer", ""),
                              float(value(arc, "width", 0) or 0), arc_points(xy(s), xy(m), xy(e))))

    # Vias
    for via in children(pcb, "via"):
        at = child(via, "at")
        if not at:
            continue
        kind = via[1] if len(via) > 1 and via[1] in ("blind", "buried", "micro") else "through"
        lay = child(via, "layers")
        pair = (lay[1], lay[2]) if lay and len(lay) >= 3 else ("F.Cu", "B.Cu")
        size = value(via, "size")
        b.vias.append(Via(_net_of(via, netmap), xy(at), float(size) if is_number(size) else 0.0, pair, kind))

    # Pads (absolute centre)
    for fp in children(pcb, "footprint"):
        at = child(fp, "at")
        fx, fy = (float(at[1]), float(at[2])) if at else (0.0, 0.0)
        rot = float(at[3]) if at and len(at) > 3 and is_number(at[3]) else 0.0
        ref = ""
        for prop in children(fp, "property"):
            if len(prop) > 2 and prop[1] == "Reference":
                ref = prop[2]
        for pad in children(fp, "pad"):
            pat, psize = child(pad, "at"), child(pad, "size")
            if not pat:
                continue
            w, h = xy(psize) if psize and len(psize) >= 3 else (0.0, 0.0)
            lay = child(pad, "layers")
            b.pads.append(Pad(
                net=_net_of(pad, netmap),
                at=_rotate(fx, fy, rot, float(pat[1]), float(pat[2])),
                radius=math.hypot(w, h) / 2,
                through=len(pad) > 2 and pad[2] == "thru_hole",
                layers=[l for l in (lay[1:] if lay else []) if isinstance(l, str)],
                ref=ref, number=str(pad[1]) if len(pad) > 1 else "",
                size=(w, h), angle=float(pat[3]) if len(pat) > 3 and is_number(pat[3]) else 0.0,
                shape=str(pad[3]) if len(pad) > 3 and isinstance(pad[3], str) else ""))

    # Zones and their fills
    for z in children(pcb, "zone"):
        if child(z, "keepout") is not None:
            continue
        zl = value(z, "layer")
        zlayers = [zl] if zl else [l for l in (child(z, "layers") or [None])[1:] if isinstance(l, str)]
        fill = child(z, "fill")
        hatched = bool(fill and value(fill, "mode") == "hatch")
        cp = child(z, "connect_pads")
        clr = value(cp, "clearance") if cp else None
        zone = Zone(net=_net_of(z, netmap), layers=zlayers, hatched=hatched,
                    clearance=float(clr) if is_number(clr) else None, name=str(value(z, "name", "") or ""))
        for fp in children(z, "filled_polygon"):
            layer = value(fp, "layer") or (zlayers[0] if zlayers else "")
            pts = _pts(fp)
            if len(pts) >= 3:
                zone.fills.setdefault(layer, []).append(pts)
        b.zones.append(zone)

    names.update(t.net for t in b.tracks)
    names.update(z.net for z in b.zones)
    b.nets = sorted(n for n in names if n)

    # Board outline bounding box
    pts: list[Point] = []
    for item in pcb[1:]:
        if isinstance(item, list) and item and str(item[0]).startswith("gr_") \
                and value(item, "layer") == "Edge.Cuts":
            pts += _outline_points(item)
    if pts:
        b.outline_bbox = (min(p[0] for p in pts), min(p[1] for p in pts),
                          max(p[0] for p in pts), max(p[1] for p in pts))
    return b
