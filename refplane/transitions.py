"""Reference continuity through layer changes (signal vias and through pads).

When a selected net changes layer, its return current has to follow it from
the old reference plane to the new one. At each signal via the reference is
looked up on every layer the net leaves on, just beyond the via's antipad:

  shared plane         both layers reference the same (layer, net): OK
  same net, new layer  e.g. GND on In1 -> GND on In4: a via (or through pad)
                       of that net must be within the profile's distance,
                       span both reference layers and be tied into both fills.
                       Missing -> `return_via_missing` with the profile's
                       severity (high_speed: error, low_speed: warning)
  different net        e.g. GND -> +3V3: no conductive return path at all;
                       warning `reference_net_change`

Return vias may be shared. Signal vias are assigned to return vias so that
the load is spread (most constrained signal via first). When a return via
ends up serving more signal vias than a profile's max_share allows, a warning
`return_via_shared` lists the nets that share it.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

from .check import Config, Finding, LayerPlan
from .pcb import Board

SEARCH_MM = 5.0          # how far to look for a "nearest" return via for the message


@dataclass(eq=False)
class _Node:
    """A place where a net can change layer: a via or a through-hole pad."""
    net: str
    at: tuple[float, float]
    radius: float
    spans: object          # callable(layer) -> bool
    label: str

    def where(self) -> str:
        return f"({self.at[0]:.3f}, {self.at[1]:.3f})"


@dataclass(eq=False)
class _Req:
    node: _Node
    net: str
    la: str
    lb: str
    ref_net: str
    r1: str
    r2: str
    rule: object
    cands: list = field(default_factory=list)        # (dist, node) qualifying within distance
    nearest: tuple | None = None                     # nearest qualifying beyond distance
    nearest_bad: tuple | None = None                 # within distance but not spanning / not tied in
    assigned: _Node | None = None


def _nodes(board: Board) -> list[_Node]:
    out = [_Node(v.net, v.at, v.size / 2, (lambda l, v=v: board.via_spans(v, l)),
                 "via" if v.kind == "through" else f"{v.kind} via") for v in board.vias if v.net]
    out += [_Node(p.net, p.at, p.radius, (lambda l: True), f"pad {p.ref}.{p.number}")
            for p in board.pads if p.through and p.net]
    return out


def _walk(points, from_start: bool, dist: float):
    pts = points if from_start else points[::-1]
    acc = 0.0
    for p, q in zip(pts, pts[1:]):
        d = math.dist(p, q)
        if d > 0 and acc + d >= dist:
            f = (dist - acc) / d
            return (p[0] + (q[0] - p[0]) * f, p[1] + (q[1] - p[1]) * f)
        acc += d
    return pts[-1]


def _tied_in(ref_layer, net: str, at, radius: float) -> bool:
    """Is a via/pad of `net` at `at` connected to that net's fill (solid or spokes)?"""
    if ref_layer.net_at(*at) == net:
        return True
    r = radius + 0.02
    return any(ref_layer.net_at(at[0] + r * math.cos(2 * math.pi * i / 24),
                                at[1] + r * math.sin(2 * math.pi * i / 24)) == net for i in range(24))


def _fmt(refs) -> str:
    return ", ".join(f"{n} on {l}" for l, n in sorted(refs)) or "nothing"


def check_transitions(board: Board, cfg: Config, plans: list[LayerPlan], ref_layers: dict,
                      clearance: float, net_stats: dict) -> list[Finding]:
    rules = cfg.stitch_rules()
    plan_by_layer = {p.layer: p for p in plans}
    ref_nets = cfg.gnd_nets | cfg.pwr_nets
    by_net: dict[str, list[_Node]] = {}
    for n in _nodes(board):
        by_net.setdefault(n.net, []).append(n)
    tracks_by_net: dict[str, list] = {}
    for t in board.tracks:
        if t.net in cfg.nets and t.net not in ref_nets and t.layer in plan_by_layer:
            tracks_by_net.setdefault(t.net, []).append(t)

    findings: list[Finding] = []
    reqs: list[_Req] = []
    transitions: list[tuple[str, _Node]] = []
    bad_nodes: set[int] = set()
    tied_cache: dict[tuple[int, str, str], bool] = {}

    def nc(net):
        return ",".join(cfg.netclass_of.get(net, ["Default"]))

    for net, tracks in sorted(tracks_by_net.items()):
        rule = rules.rule_for(net, cfg.netclass_of.get(net, ["Default"]))
        for node in by_net.get(net, []):
            refs = _refs_at(node, tracks, plan_by_layer, ref_layers, clearance)
            layers = sorted(refs, key=board.copper_layers.index)
            if len(layers) < 2:
                continue
            transitions.append((net, node))
            for i, la in enumerate(layers):
                for lb in layers[i + 1:]:
                    ra, rb = refs[la], refs[lb]
                    if not ra or not rb or ra & rb:
                        continue          # unreferenced side (reported elsewhere) or shared plane
                    same = [(a, b) for a in ra for b in rb if a[1] == b[1]]
                    if not same:
                        bad_nodes.add(id(node))
                        findings.append(Finding(
                            "warning", "reference_net_change",
                            f"{net} {node.label} at {node.where()} {la} -> {lb}: reference changes from {_fmt(ra)} "
                            f"to {_fmt(rb)}; no conductive return path between "
                            f"{'/'.join(sorted({n for _, n in ra}))} and {'/'.join(sorted({n for _, n in rb}))}",
                            net=net, netclass=nc(net), layer=la, ref_layers=sorted({l for l, _ in ra | rb}),
                            start=node.at, end=node.at, via_layers=[la, lb], radius=rule.distance_mm,
                            profile=rule.name))
                        continue
                    same.sort(key=lambda ab: (ab[0][1] not in cfg.gnd_nets, ab[0][1], ab[0][0], ab[1][0]))
                    req = None
                    for (r1, rnet), (r2, _) in same:
                        cand_req = _Req(node, net, la, lb, rnet, r1, r2, rule)
                        _candidates(cand_req, by_net.get(rnet, []), ref_layers, tied_cache)
                        if req is None or (cand_req.cands and not req.cands):
                            req = cand_req
                        if req.cands:
                            break
                    reqs.append(req)

    # Missing return vias
    for r in reqs:
        if r.cands:
            continue
        bad_nodes.add(id(r.node))
        msg = (f"{r.net} {r.node.label} at {r.node.where()} {r.la} -> {r.lb}: reference changes {r.ref_net} on {r.r1} "
               f"-> {r.ref_net} on {r.r2}; no {r.ref_net} via within {r.rule.distance_mm:g} mm ({r.rule.name})")
        if r.nearest:
            d, n = r.nearest
            msg += f"; nearest connected one {d:.2f} mm away at {n.where()}"
        else:
            msg += f"; none within {SEARCH_MM:g} mm"
        if r.nearest_bad:
            d, n = r.nearest_bad
            msg += f"; the {n.label} {d:.2f} mm away does not reach or is not tied into both {r.r1} and {r.r2}"
        findings.append(Finding(r.rule.severity, "return_via_missing", msg, net=r.net, netclass=nc(r.net),
                                layer=r.la, ref_layers=[r.r1, r.r2], start=r.node.at, end=r.node.at,
                                via_layers=[r.la, r.lb], radius=r.rule.distance_mm, profile=r.rule.name))

    # Shared return vias: spread the load, most constrained signal via first
    load: dict[int, list[_Req]] = {}
    users: dict[int, set[int]] = {}
    by_node: dict[int, _Node] = {}
    pending = sorted((r for r in reqs if r.cands), key=lambda r: (len(r.cands), r.node.at))
    for r in pending:
        already = next((c for _, c in r.cands if id(r.node) in users.get(id(c), set())), None)
        if already is None:
            free = [(d, c) for d, c in r.cands if len(users.get(id(c), ())) < r.rule.max_share]
            pick = min(free or r.cands, key=lambda dc: (0 if free else len(users.get(id(dc[1]), ())), dc[0]))[1]
        else:
            pick = already
        r.assigned = pick
        by_node[id(pick)] = pick
        load.setdefault(id(pick), []).append(r)
        users.setdefault(id(pick), set()).add(id(r.node))
    for key, rs in load.items():
        n_users = len(users[key])
        over = [r for r in rs if n_users > r.rule.max_share]
        if not over:
            continue
        rv = by_node[key]
        for r in over:
            bad_nodes.add(id(r.node))
        nets = sorted({r.net for r in rs})
        limit = min(r.rule.max_share for r in over)
        layers = sorted({l for r in rs for l in (r.la, r.lb)}, key=board.copper_layers.index)
        findings.append(Finding(
            "warning", "return_via_shared",
            f"{rv.net} {rv.label} at {rv.where()} is the return via for {n_users} signal vias "
            f"({', '.join(nets)}); {', '.join(sorted({r.rule.name for r in over}))} allows {limit}. "
            f"Add a {rv.net} via near the signal vias or spread them out",
            net=", ".join(nets), netclass=", ".join(sorted({nc(r.net) for r in rs})), layer=layers[0],
            ref_layers=sorted({l for r in rs for l in (r.r1, r.r2)}), start=rv.at, end=rv.at,
            via_layers=layers, radius=max(r.rule.distance_mm for r in over),
            profile=",".join(sorted({r.rule.name for r in over}))))

    for net, node in transitions:
        st = net_stats.get(net)
        if st is None:
            continue
        st["transitions"] = st.get("transitions", 0) + 1
        if id(node) not in bad_nodes:
            st["transitions_ok"] = st.get("transitions_ok", 0) + 1
    for net, st in net_stats.items():
        st["stitch_profile"] = rules.rule_for(net, cfg.netclass_of.get(net, ["Default"])).name
    return findings


def _refs_at(node, tracks, plan_by_layer, ref_layers, clearance):
    """layer -> {(ref layer, ref net)} sampled on each track leaving the node, past the antipad."""
    refs: dict[str, set[tuple[str, str]]] = {}
    sample = node.radius + clearance + 0.25
    tol = node.radius + 1e-3
    for t in tracks:
        if not node.spans(t.layer):
            continue
        ends = []
        if math.dist(t.points[0], node.at) <= tol:
            ends.append(True)
        if math.dist(t.points[-1], node.at) <= tol:
            ends.append(False)
        for from_start in ends:
            p = _walk(t.points, from_start, sample)
            s = refs.setdefault(t.layer, set())
            for r, _, _ in plan_by_layer[t.layer].refs:
                rn = ref_layers[r].net_at(*p) if r in ref_layers else None
                if rn:
                    s.add((r, rn))
    return refs


def _candidates(req: _Req, pool: list[_Node], ref_layers: dict, cache: dict) -> None:
    d_max = req.rule.distance_mm
    reach = max(d_max, SEARCH_MM)
    for cand in pool:
        dist = math.dist(cand.at, req.node.at)
        if dist > reach:
            continue
        key = (id(cand), req.r1, req.r2)
        if key not in cache:
            cache[key] = (cand.spans(req.r1) and cand.spans(req.r2)
                          and _tied_in(ref_layers[req.r1], req.ref_net, cand.at, cand.radius)
                          and _tied_in(ref_layers[req.r2], req.ref_net, cand.at, cand.radius))
        if cache[key]:
            if dist <= d_max:
                req.cands.append((dist, cand))
            elif req.nearest is None or dist < req.nearest[0]:
                req.nearest = (dist, cand)
        elif dist <= d_max and (req.nearest_bad is None or dist < req.nearest_bad[0]):
            req.nearest_bad = (dist, cand)
    req.cands.sort(key=lambda dc: dc[0])
