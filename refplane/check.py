"""Reference-plane continuity check.

For every routed segment of a selected net on a signal (or hybrid) layer, the
copper on the adjacent reference layer(s) is sampled along five lines parallel
to the trace: the centre line, both trace edges, and both edges of a margin
band (margin = margin_h x dielectric height H to that layer).

Per position along the trace:
  error    no reference copper under the trace body on any adjacent reference
           layer (antipad, cutout, split-plane gap, off the plane edge)
  warning  body is over PWR copper only (valid AC reference only if decoupled)
  warning  body is referenced, but a void or plane edge is inside the margin band

Only zone fills of reference nets count as reference copper. Their own-net
via / through-pad antipads where the trace connects are excluded.
"""

from __future__ import annotations

import math
import re
from dataclasses import asdict, dataclass, field

from .geometry import (Polygon, circle_interval, iv_complement, iv_intersect, iv_length,
                       iv_subtract, iv_union)
from .pcb import Board

ROLES = ("sig", "gnd", "pwr", "hybrid")
REF_ROLES = {"gnd", "pwr", "hybrid"}
TRACE_ROLES = {"sig", "hybrid"}
ROLE_ALIASES = {"signal": "sig", "s": "sig", "ground": "gnd", "g": "gnd", "plane": "gnd",
                "power": "pwr", "p": "pwr", "mixed": "hybrid", "h": "hybrid", "mix": "hybrid"}
GND_RX = re.compile(r"(GND|VSS|^0V$|EARTH|CHASSIS|^PE$)", re.I)
DEFAULT_H = 0.2           # mm, used only when the board has no stackup
SEVERITY_ORDER = {"error": 0, "warning": 1, "info": 2}


@dataclass
class Config:
    roles: dict[str, str]
    gnd_nets: set[str]
    pwr_nets: set[str]
    nets: set[str]                         # signal nets to check
    netclass_of: dict[str, list[str]] = field(default_factory=dict)
    margin_h: float = 3.0
    min_length: float = 0.02               # mm; shorter runs are numeric noise
    default_clearance: float = 0.25        # mm, zone clearance when the zone has none
    stitch: object = None                  # settings.StitchRules; None = built-in defaults.json
    net_clearance: dict[str, float] = field(default_factory=dict)  # netclass clearance per net (antipad size)

    def stitch_rules(self):
        if self.stitch is None:
            from .settings import load_settings, stitch_rules
            self.stitch = stitch_rules(load_settings()[0])
        return self.stitch


@dataclass
class Finding:
    severity: str
    kind: str
    message: str
    net: str = ""
    netclass: str = ""
    layer: str = ""
    ref_layers: list[str] = field(default_factory=list)
    start: tuple[float, float] | None = None
    end: tuple[float, float] | None = None
    length: float | None = None
    pieces: list[tuple[tuple[float, float], tuple[float, float]]] = field(default_factory=list)
    via_layers: list[str] = field(default_factory=list)    # layer change findings: from, to
    radius: float | None = None                            # stitch distance used
    profile: str = ""                                      # stitch profile used

    def to_dict(self) -> dict:
        d = asdict(self)
        d.pop("pieces")
        for k in ("start", "end"):
            if d[k] is not None:
                d[k] = [round(d[k][0], 4), round(d[k][1], 4)]
        if d["length"] is not None:
            d["length"] = round(d["length"], 4)
        return d


@dataclass
class LayerPlan:
    layer: str
    role: str
    refs: list[tuple[str, float, float]]     # (ref layer, H, margin)


@dataclass
class Result:
    findings: list[Finding]
    plans: list[LayerPlan]
    net_stats: dict[str, dict]
    notes: list[str]


# --------------------------------------------------------------------------- roles / nets

def parse_roles(spec: str, board: Board) -> dict[str, str]:
    """'F.Cu=sig,In1.Cu=gnd' or positional 'sig,gnd,pwr,sig' (top -> bottom)."""
    items = [s.strip() for s in spec.replace(";", ",").split(",") if s.strip()]
    roles: dict[str, str] = {}
    if items and all("=" not in s for s in items):
        if len(items) != len(board.copper_layers):
            raise ValueError(f"{len(items)} roles given for {len(board.copper_layers)} copper layers "
                             f"({', '.join(board.copper_layers)})")
        pairs = list(zip(board.copper_layers, items))
    else:
        pairs = [tuple(s.split("=", 1)) for s in items]
    lookup = {l.lower(): l for l in board.copper_layers}
    for i, l in enumerate(board.copper_layers, 1):
        lookup[f"l{i}"] = l
    for layer, role in pairs:
        layer, role = layer.strip(), role.strip().lower()
        role = ROLE_ALIASES.get(role, role)
        if layer.lower() not in lookup:
            raise ValueError(f"unknown copper layer {layer!r} (have {', '.join(board.copper_layers)})")
        if role not in ROLES:
            raise ValueError(f"unknown role {role!r} for {layer} (use {', '.join(ROLES)})")
        roles[lookup[layer.lower()]] = role
    return roles


def format_roles(roles: dict[str, str], board: Board) -> str:
    return ",".join(f"{l}={roles.get(l, 'sig')}" for l in board.copper_layers)


def is_gnd_name(net: str) -> bool:
    return bool(GND_RX.search(net.rsplit("/", 1)[-1]))


def zone_coverage(board: Board) -> dict[str, dict[str, float]]:
    """layer -> net -> filled area (mm^2)."""
    cov: dict[str, dict[str, float]] = {}
    for z in board.zones:
        if not z.net:
            continue
        for layer, polys in z.fills.items():
            a = sum(Polygon(p, cell=5.0).area for p in polys)
            cov.setdefault(layer, {})
            cov[layer][z.net] = cov[layer].get(z.net, 0.0) + a
    return cov


def board_area(board: Board) -> float | None:
    if not board.outline_bbox:
        return None
    x0, y0, x1, y1 = board.outline_bbox
    return max(0.0, (x1 - x0) * (y1 - y0)) or None


def guess_roles(board: Board) -> tuple[dict[str, str], dict[str, str]]:
    """Guess layer roles from KiCad layer type and zone fill coverage.

    Outer layers default to 'sig' (a top pour is not trusted as a plane).
    Returns (roles, reasons).
    """
    cov = zone_coverage(board)
    area = board_area(board)
    tracks_on = {}
    for t in board.tracks:
        tracks_on[t.layer] = tracks_on.get(t.layer, 0) + 1
    roles, why = {}, {}
    for l in board.copper_layers:
        ktype = board.layer_types.get(l, "signal")
        nets = cov.get(l, {})
        gnd_a = sum(a for n, a in nets.items() if is_gnd_name(n))
        oth_a = sum(a for n, a in nets.items() if not is_gnd_name(n))
        frac_g = gnd_a / area if area else 0.0
        frac_o = oth_a / area if area else 0.0
        outer = l in ("F.Cu", "B.Cu")
        if outer:
            roles[l], why[l] = "sig", "outer layer"
        elif ktype == "mixed":
            roles[l], why[l] = "hybrid", "KiCad layer type 'mixed'"
        elif frac_g >= 0.5 and tracks_on.get(l, 0) <= 20:
            roles[l], why[l] = "gnd", f"GND fill {frac_g:.0%} of board"
        elif frac_o >= 0.5 and tracks_on.get(l, 0) <= 20:
            roles[l], why[l] = "pwr", f"power fill {frac_o:.0%} of board"
        elif frac_g + frac_o >= 0.3:
            roles[l], why[l] = "hybrid", f"fills {frac_g + frac_o:.0%} of board plus {tracks_on.get(l, 0)} tracks"
        elif ktype == "power":
            roles[l], why[l] = ("gnd" if gnd_a >= oth_a else "pwr"), "KiCad layer type 'power'"
        else:
            roles[l], why[l] = "sig", f"{tracks_on.get(l, 0)} tracks, little fill"
    return roles, why


def guess_ref_nets(board: Board, roles: dict[str, str]) -> tuple[set[str], set[str]]:
    """GND nets by name; PWR = other nets that have a fill on a reference layer."""
    gnd, pwr = set(), set()
    for z in board.zones:
        if not z.net:
            continue
        if not any(roles.get(l) in REF_ROLES for l in z.fills):
            continue
        (gnd if is_gnd_name(z.net) else pwr).add(z.net)
    gnd.update(n for n in board.nets if is_gnd_name(n))
    return gnd, pwr - gnd


def layer_plans(board: Board, roles: dict[str, str], margin_h: float, notes: list[str]) -> list[LayerPlan]:
    plans = []
    order = board.copper_layers
    for i, l in enumerate(order):
        role = roles.get(l, "sig")
        if role not in TRACE_ROLES:
            continue
        refs = []
        for j in (i - 1, i + 1):
            if 0 <= j < len(order) and roles.get(order[j], "sig") in REF_ROLES:
                h = board.h_between(l, order[j])
                if h is None:
                    h = DEFAULT_H
                    msg = f"no stackup dielectric between {l} and {order[j]}; using H = {DEFAULT_H} mm"
                    if msg not in notes:
                        notes.append(msg)
                refs.append((order[j], h, margin_h * h))
        plans.append(LayerPlan(l, role, refs))
    return plans


# --------------------------------------------------------------------------- main check

class _RefLayer:
    def __init__(self, layer: str, polys: list[tuple[str, str, Polygon]]):
        self.layer = layer
        self.polys = polys                      # (net, 'gnd'|'pwr', polygon)

    def intervals(self, ax, ay, bx, by):
        gnd, anyc, per_net = [], [], {}
        for net, cat, poly in self.polys:
            iv = poly.inside_intervals(ax, ay, bx, by)
            if iv:
                anyc += iv
                per_net.setdefault(net, []).extend(iv)
                if cat == "gnd":
                    gnd += iv
        return iv_union(gnd), iv_union(anyc), per_net

    def ring_is_gnd(self, x, y, r) -> bool:
        """Is the copper around an antipad (ring of radius r) a GND-category net?"""
        for i in range(12):
            ang = 2 * math.pi * i / 12
            px, py = x + r * math.cos(ang), y + r * math.sin(ang)
            for net, cat, poly in self.polys:
                if poly.contains(px, py):
                    if cat == "gnd":
                        return True
                    break
        return False

    def net_at(self, x, y) -> str | None:
        for net, _, poly in self.polys:
            if poly.contains(x, y):
                return net
        return None


def run(board: Board, cfg: Config) -> Result:
    notes: list[str] = []
    findings: list[Finding] = []
    roles = cfg.roles
    ref_nets = cfg.gnd_nets | cfg.pwr_nets
    plans = layer_plans(board, roles, cfg.margin_h, notes)

    # Reference copper per layer
    ref_layers: dict[str, _RefLayer] = {}
    for l in board.copper_layers:
        if roles.get(l) not in REF_ROLES:
            continue
        polys = []
        for z in board.zones:
            if z.net not in ref_nets or l not in z.layers:
                continue
            fills = z.fills.get(l, [])
            if not fills:
                findings.append(Finding("warning", "unfilled_zone",
                                        f"zone '{z.name or z.net}' on {l} has no fill; refill zones", net=z.net, layer=l))
            if z.hatched:
                findings.append(Finding("warning", "hatched_reference",
                                        f"{z.net} zone on {l} is hatched: not a solid reference plane",
                                        net=z.net, layer=l))
            cat = "gnd" if z.net in cfg.gnd_nets else "pwr"
            polys += [(z.net, cat, Polygon(p)) for p in fills]
        ref_layers[l] = _RefLayer(l, polys)
        if not polys and roles.get(l) in ("gnd", "pwr"):
            findings.append(Finding("error", "empty_plane",
                                    f"{l} is marked '{roles[l]}' but has no filled reference-net copper", layer=l))

    findings += _stale_fill_check(board, ref_layers)
    findings += _tracks_on_planes(board, roles, ref_nets)

    clearance = max([z.clearance for z in board.zones if z.net in ref_nets and z.clearance] or
                    [cfg.default_clearance])
    own = _own_net_holes(board, clearance)
    landed = _landed_holes(board, own)
    # Antipad size per reference layer: KiCad clears a via by the larger of the zone clearance
    # and the netclass clearance. +0.05 mm absorbs fill rounding.
    layer_clr = {l: max([z.clearance for z in board.zones if z.net in ref_nets and l in z.layers and z.clearance]
                        or [cfg.default_clearance]) for l in ref_layers}
    # Netclass clearance only of the reference nets actually filled on that layer: a large
    # clearance on another plane (e.g. +HV on In2) must not widen the GND exemption on In1.
    ref_net_clr = {l: max([cfg.net_clearance.get(n, 0.0) for n, _, _ in rl.polys] or [0.0])
                   for l, rl in ref_layers.items()}

    net_stats: dict[str, dict] = {}
    pieces: dict[tuple[str, str, str], list] = {}
    plan_by_layer = {p.layer: p for p in plans}
    no_ref_layers: dict[str, set[str]] = {}

    for t in board.tracks:
        if t.net not in cfg.nets or t.net in ref_nets:
            continue
        plan = plan_by_layer.get(t.layer)
        if plan is None:
            continue
        st = net_stats.setdefault(t.net, {"netclass": ",".join(cfg.netclass_of.get(t.net, ["Default"])),
                                          "layers": set(), "length": 0.0, "unreferenced": 0.0,
                                          "pwr_only": 0.0, "margin": 0.0, "edge_only": 0.0, "references": {},
                                          "transitions": 0, "transitions_ok": 0})
        st["layers"].add(t.layer)
        if not plan.refs:
            no_ref_layers.setdefault(t.layer, set()).add(t.net)
            for a, b in zip(t.points, t.points[1:]):
                st["length"] += math.dist(a, b)
                st["unreferenced"] += math.dist(a, b)
            continue
        connected = landed.get((t.net, t.layer), [])
        for a, b in zip(t.points, t.points[1:]):
            clr = {l: max(c, ref_net_clr[l], cfg.net_clearance.get(t.net, 0.0)) + 0.05 for l, c in layer_clr.items()}
            seg = _check_segment(a, b, t, plan, ref_layers, connected, clr)
            if seg is None:
                continue
            length, err, edge, pwr, marg, ref_len = seg
            st["length"] += length
            for key, L in ref_len.items():
                st["references"][key] = st["references"].get(key, 0.0) + L
            for kind, ivs in (("no_reference", err), ("reference_edge", edge), ("pwr_reference", pwr),
                              ("reference_margin", marg)):
                kept = [iv for iv in ivs if (iv[1] - iv[0]) * length >= cfg.min_length]
                if not kept:
                    continue
                st[_STAT_KEY[kind]] += iv_length(kept) * length
                for s, e in kept:
                    pa = (a[0] + (b[0] - a[0]) * s, a[1] + (b[1] - a[1]) * s)
                    pb = (a[0] + (b[0] - a[0]) * e, a[1] + (b[1] - a[1]) * e)
                    pieces.setdefault((kind, t.net, t.layer), []).append((pa, pb, t.width))

    for layer, nets in sorted(no_ref_layers.items()):
        findings.append(Finding("error", "no_adjacent_plane",
                                f"{layer} has no adjacent reference layer; {len(nets)} selected net(s) routed there "
                                f"have no defined return path: {', '.join(sorted(nets)[:8])}"
                                + (" ..." if len(nets) > 8 else ""), layer=layer))

    from .transitions import check_transitions
    findings += check_transitions(board, cfg, plans, ref_layers, clearance, net_stats)

    for (kind, net, layer), plist in pieces.items():
        plan = plan_by_layer[layer]
        for run_pieces in _chain(plist):
            findings.append(_make_finding(kind, net, layer, plan, run_pieces, ref_layers, cfg))

    for st in net_stats.values():
        st["layers"] = sorted(st["layers"], key=board.copper_layers.index)
        for k in ("length", "unreferenced", "edge_only", "pwr_only", "margin"):
            st[k] = round(st[k], 3)
        st["references"] = {k: round(v, 3) for k, v in sorted(st["references"].items(), key=lambda kv: -kv[1])}
    findings.sort(key=lambda f: (SEVERITY_ORDER.get(f.severity, 9), f.kind, f.layer, f.net,
                                 -(f.length or 0)))
    return Result(findings, plans, net_stats, notes)


_STAT_KEY = {"no_reference": "unreferenced", "reference_edge": "edge_only", "pwr_reference": "pwr_only",
             "reference_margin": "margin"}


def _check_segment(a, b, t, plan: LayerPlan, ref_layers, own_holes, clr=None):
    """Coverage of one straight piece of track.

    `own_holes` are the own-net vias / through pads the net's routing on this layer lands on.
    Their antipad is expected, but only on a reference layer the via or pad passes through:
    there the antipad disc (copper radius + clearance) counts as referenced, line by line.
    Any other void, even right next to the via, is still reported. A via that does not reach a
    reference layer waives nothing on it.
    """
    clr = clr or {}
    length = math.dist(a, b)
    if length < 1e-6:
        return None
    ux, uy = (b[0] - a[0]) / length, (b[1] - a[1]) / length
    nx, ny = -uy, ux
    hw = t.width / 2
    body_any, body_gnd, margin_ok, centre_any = [], [], [], []
    ref_len: dict[str, float] = {}
    for ref, _h, margin in plan.refs:
        rl = ref_layers.get(ref)
        if rl is None:
            continue
        discs = []                     # (x, y, antipad radius, surrounded by GND?)
        for h in own_holes:
            if not h.spans(ref):
                continue
            R = h.size_r + clr.get(ref, 0.3)
            if _near_segment(a, b, h.x, h.y, R + margin + t.width):
                discs.append((h.x, h.y, R, rl.ring_is_gnd(h.x, h.y, R + 0.05)))

        def line(off):
            ax_, ay_, bx_, by_ = a[0] + nx * off, a[1] + ny * off, b[0] + nx * off, b[1] + ny * off
            g, an, per_net = rl.intervals(ax_, ay_, bx_, by_)
            if discs:
                ex = [circle_interval(ax_, ay_, bx_, by_, x, y, R) for x, y, R, _ in discs]
                exg = [iv for (x, y, R, isg), iv in zip(discs, ex) if isg]
                an = iv_union(an + [i for e in ex for i in e])
                g = iv_union(g + [i for e in exg for i in e])
            return g, an, per_net

        g_lines, a_lines = [], []
        for off in (0.0, hw, -hw):
            g, an, per_net = line(off)
            if off == 0.0:
                for rnet, ivs in per_net.items():
                    key = f"{ref}/{rnet}"
                    ref_len[key] = ref_len.get(key, 0.0) + iv_length(ivs) * length
            g_lines.append(g)
            a_lines.append(an)
        bg = g_lines[0]
        ba = a_lines[0]
        for g, an in zip(g_lines[1:], a_lines[1:]):
            bg, ba = iv_intersect(bg, g), iv_intersect(ba, an)
        mo = ba
        for off in (hw + margin, -(hw + margin)):
            _, an, _ = line(off)
            mo = iv_intersect(mo, an)
        body_any += ba
        body_gnd += bg
        margin_ok += mo
        centre_any += a_lines[0]
    body_any, body_gnd, margin_ok = iv_union(body_any), iv_union(body_gnd), iv_union(margin_ok)
    # Centre line over a void: error. Only a trace edge over a void: warning (graded by depth).
    err = iv_complement(iv_union(centre_any))
    edge = iv_subtract(iv_complement(body_any), err)
    pwr = iv_subtract(body_any, body_gnd)
    marg = iv_subtract(body_any, margin_ok)
    return length, err, edge, pwr, marg, ref_len


def _near_segment(a, b, cx, cy, r) -> bool:
    return (min(a[0], b[0]) - r <= cx <= max(a[0], b[0]) + r and
            min(a[1], b[1]) - r <= cy <= max(a[1], b[1]) + r)


@dataclass
class _Hole:
    """A via or through pad of the trace's own net; its antipad on a plane is expected."""
    x: float
    y: float
    size_r: float          # copper radius of the via / pad
    r: float               # antipad radius estimate for the transition check (largest zone clearance)
    spans: object          # callable(layer) -> bool


def _own_net_holes(board: Board, clearance: float) -> dict[str, list[_Hole]]:
    out: dict[str, list] = {}
    for v in board.vias:
        if v.net:
            out.setdefault(v.net, []).append(_Hole(v.at[0], v.at[1], v.size / 2, v.size / 2 + clearance + 0.05,
                                                   lambda l, v=v: board.via_spans(v, l)))
    for p in board.pads:
        if p.net and p.through:
            out.setdefault(p.net, []).append(_Hole(p.at[0], p.at[1], p.radius, p.radius + clearance + 0.05,
                                                   lambda l: True))
    return out


def _landed_holes(board: Board, own: dict) -> dict[tuple[str, str], list]:
    """(net, layer) -> own-net vias / through pads that the net's routing on that layer
    actually lands on (some track of the net on that layer ends inside the via/pad copper).

    KiCad stores every straight piece as its own segment, so connection is judged per net
    and layer, not per segment: the piece just before the one that ends on the via also runs
    over that via's antipad."""
    out: dict[tuple[str, str], list] = {}
    for t in board.tracks:
        for h in own.get(t.net, ()):
            if not h.spans(t.layer):
                continue
            if any(math.dist(e, (h.x, h.y)) <= h.size_r + 1e-3 for e in (t.points[0], t.points[-1])):
                lst = out.setdefault((t.net, t.layer), [])
                if h not in lst:
                    lst.append(h)
    return out


def _stale_fill_check(board: Board, ref_layers: dict[str, _RefLayer]) -> list[Finding]:
    """A via or through pad sitting inside another net's fill means the fill is stale."""
    bad: dict[str, list] = {}
    items = [(v.net, v.at, (lambda l, v=v: board.via_spans(v, l)), "via") for v in board.vias]
    items += [(p.net, p.at, (lambda l, p=p: True), f"pad {p.ref}.{p.number}") for p in board.pads if p.through]
    for net, at, spans, what in items:
        for l, rl in ref_layers.items():
            if not spans(l):
                continue
            hit = rl.net_at(*at)
            if hit is not None and hit != net:
                bad.setdefault(l, []).append((what, net or "<no net>", hit, at))
    out = []
    for l, hits in bad.items():
        what, net, hit, at = hits[0]
        out.append(Finding("error", "stale_fill",
                           f"{l} fill looks stale: {len(hits)} via/pad(s) of other nets sit inside reference copper "
                           f"(first: {what} {net} inside {hit} at {at[0]:.3f}, {at[1]:.3f}). Refill zones and re-run.",
                           layer=l, start=at))
    return out


def _tracks_on_planes(board: Board, roles, ref_nets) -> list[Finding]:
    counts: dict[str, set] = {}
    for t in board.tracks:
        if roles.get(t.layer) in ("gnd", "pwr") and t.net not in ref_nets:
            counts.setdefault(t.layer, set()).add(t.net)
    return [Finding("info", "tracks_on_plane",
                    f"{len(nets)} signal net(s) routed on plane layer {l}; they cut the plane: "
                    f"{', '.join(sorted(nets)[:8])}" + (" ..." if len(nets) > 8 else ""), layer=l)
            for l, nets in sorted(counts.items())]


def _key(p):
    return (round(p[0], 3), round(p[1], 3))


def _chain(plist):
    """Join pieces whose ends touch into runs."""
    by_start: dict = {}
    ends = set()
    for pc in plist:
        by_start.setdefault(_key(pc[0]), []).append(pc)
        ends.add(_key(pc[1]))
    used = set()
    runs = []
    starts = [pc for pc in plist if _key(pc[0]) not in ends] + plist
    for pc in starts:
        if id(pc) in used:
            continue
        run = [pc]
        used.add(id(pc))
        cur = pc
        while True:
            nxt = [q for q in by_start.get(_key(cur[1]), []) if id(q) not in used]
            if not nxt:
                break
            cur = nxt[0]
            used.add(id(cur))
            run.append(cur)
        runs.append(run)
    return runs


def _make_finding(kind, net, layer, plan, run, ref_layers, cfg) -> Finding:
    start, end = run[0][0], run[-1][1]
    length = sum(math.dist(pc[0], pc[1]) for pc in run)
    refs = [r for r, _, _ in plan.refs]
    nc = ",".join(cfg.netclass_of.get(net, ["Default"]))
    where = f"({start[0]:.3f}, {start[1]:.3f}) -> ({end[0]:.3f}, {end[1]:.3f})"
    if kind == "reference_edge":
        depth, width = _edge_depth(run, plan, ref_layers)
        msg = (f"{net} on {layer}: {length:.3f} mm where the trace edge is over a void on {'/'.join(refs)} "
               f"(centre line still referenced); the void reaches about {depth * 1000:.0f} um into the "
               f"{width:.2f} mm trace - {_describe_gap(run, plan, ref_layers)}; {where}")
        sev = "warning"
    elif kind == "no_reference":
        detail = _describe_gap(run, plan, ref_layers)
        msg = f"{net} on {layer}: {length:.3f} mm with no reference copper on {'/'.join(refs)} - {detail}; {where}"
        sev = "error"
    elif kind == "pwr_reference":
        mid = _midpoint(run)
        nets = sorted({n for r in refs if (n := ref_layers[r].net_at(*mid))})
        msg = (f"{net} on {layer}: {length:.3f} mm referenced to power ({', '.join(nets) or '?'}) only; "
               f"the return current flows on a power plane; {where}")
        sev = "warning"
    else:
        margins = "/".join(f"{m:.2f}" for _, _, m in plan.refs)
        msg = (f"{net} on {layer}: {length:.3f} mm within {margins} mm ({cfg.margin_h:g}H) of a void or plane edge "
               f"on {'/'.join(refs)}; {where}")
        sev = "warning"
    return Finding(sev, kind, msg, net=net, netclass=nc, layer=layer, ref_layers=refs,
                   start=start, end=end, length=length, pieces=run)


def _edge_depth(run, plan, ref_layers):
    """How far a void reaches into the trace from its edge, sampled across the trace at the
    worst point along the run (mm), and the trace width."""
    width = max(pc[2] if len(pc) > 2 else 0.0 for pc in run)
    hw = width / 2
    worst = 0.0
    for pc in run:
        p, q = pc[0], pc[1]
        d = math.dist(p, q)
        if d == 0:
            continue
        nx, ny = -(q[1] - p[1]) / d, (q[0] - p[0]) / d
        for f in (0.1, 0.5, 0.9):
            cx, cy = p[0] + (q[0] - p[0]) * f, p[1] + (q[1] - p[1]) * f
            for side in (1, -1):
                def covered(k):                       # k = 0 (centre) .. 20 (edge), 5 % steps
                    off = hw * k / 20
                    x, y = cx + nx * off * side, cy + ny * off * side
                    return any(ref_layers[r].net_at(x, y) for r, _, _ in plan.refs if r in ref_layers)
                if covered(20):
                    continue
                lo, hi = 0, 20                        # covered(lo) (centre is referenced), not covered(hi)
                if not covered(0):
                    worst = max(worst, hw)
                    continue
                while hi - lo > 1:                    # void reaches in from the edge: bisect its depth
                    mid = (lo + hi) // 2
                    if covered(mid):
                        lo = mid
                    else:
                        hi = mid
                worst = max(worst, hw * (20 - lo) / 20)
    return worst, width


def _midpoint(run):
    total = sum(math.dist(pc[0], pc[1]) for pc in run)
    acc = 0.0
    for pc in run:
        p, q = pc[0], pc[1]
        d = math.dist(p, q)
        if acc + d >= total / 2 and d > 0:
            f = (total / 2 - acc) / d
            return (p[0] + (q[0] - p[0]) * f, p[1] + (q[1] - p[1]) * f)
        acc += d
    return run[0][0]


def _describe_gap(run, plan, ref_layers) -> str:
    def step(p, q, dist):
        d = math.dist(p, q) or 1.0
        return (p[0] + (q[0] - p[0]) / d * dist, p[1] + (q[1] - p[1]) / d * dist)

    before = step(run[0][0], run[0][1], -0.15)
    after = step(run[-1][1], run[-1][0], -0.15)
    parts = []
    for r, _, _ in plan.refs:
        rl = ref_layers[r]
        nb, na = rl.net_at(*before), rl.net_at(*after)
        if nb and na and nb == na:
            parts.append(f"void in {nb} on {r} (antipad, cutout or slot)")
        elif nb and na:
            parts.append(f"crosses split between {nb} and {na} on {r}")
        elif nb or na:
            parts.append(f"runs off the edge of {nb or na} on {r}")
        else:
            parts.append(f"no reference copper on {r}")
    return "; ".join(parts)
