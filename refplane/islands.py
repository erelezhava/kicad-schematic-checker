"""Island check: is the reference copper under a trace really tied into its ground?

The plane check only looks at the copper directly under a trace. That copper can be an
island that joins the rest of its net only through a thin neck, one via, or nothing at all,
and then the return current has to detour or cannot flow at all. This module:

1. rasterises every fill polygon of every reference net (all copper layers, so outer-layer
   pours that carry ties count too);
2. splits a polygon into regions where it narrows below `neck_mm` (erosion by neck/2, then
   geodesic growth back to the full shape), so a peninsula on a thin neck is its own region;
3. links regions with *strong* ties (a via or through pad of the net, tied into the fill on
   two layers) and *weak* links (a neck inside one polygon, or a track of the net);
4. takes the strongly-tied group with the largest area as the main plane of that net;
5. for every region on a reference layer that selected traces run over (centre line), reports:

     island_floating   error    no path at all to the main plane
     island_neck       warning  reaches the main plane only through a neck or a track
     island_weak_tie   warning  tied to the main plane by fewer than `min_ties` vias/pads
     island_tie_far    warning  part of the trace over it is more than `max_tie_distance_mm`
                                from the island's nearest tie

The main region itself is never reported. Standard library only.
"""

from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass, field

from .check import REF_ROLES, Config, Finding, LayerPlan
from .geometry import Polygon
from .pcb import Board

DEFAULTS = {
    "enabled": True,
    "neck_mm": 1.0,               # copper narrower than this splits a polygon into regions
    "min_area_mm2": 4.0,          # a region must have at least this much core area to stand alone
    "min_ties": 2,                # fewer vias/pads than this tying an island to the plane: warning
    "max_tie_distance_mm": 5.0,   # trace over an island farther than this from its nearest tie
    "min_length_mm": 0.5,         # ignore islands a selected trace crosses for less than this
    "max_cells": 400000,          # raster size cap per polygon (resolution is coarsened to fit)
}


@dataclass(eq=False)
class Region:
    rid: int
    net: str
    layer: str
    area: float
    centre: tuple[float, float]               # deepest point of the region
    ties: list = field(default_factory=list)  # (x, y, label) strong ties of this region
    links: list = field(default_factory=list)  # (other rid, kind, width_mm, (x, y)) weak links


class _Raster:
    """Inside mask, chamfer distance and region labels of one fill polygon."""

    def __init__(self, poly: Polygon, res: float, neck: float, min_area: float):
        x0, y0, x1, y1 = poly.bbox
        self.res = res
        self.x0, self.y0 = x0 - res, y0 - res
        self.nx = int((x1 - x0) / res) + 3
        self.ny = int((y1 - y0) / res) + 3
        nx, ny = self.nx, self.ny
        inside = bytearray(nx * ny)
        for j in range(ny):
            y = self.y0 + (j + 0.5) * res
            xs = []
            for idx in poly._rows.get(math.floor(y / poly.row_h), ()):
                ax, ay, bx, by = poly.edges[idx]
                if (ay > y) != (by > y):
                    xs.append(ax + (y - ay) * (bx - ax) / (by - ay))
            xs.sort()
            row = j * nx
            for k in range(0, len(xs) - 1, 2):
                i0 = max(0, math.ceil((xs[k] - self.x0) / res - 0.5))
                i1 = min(nx - 1, math.floor((xs[k + 1] - self.x0) / res - 0.5))
                for i in range(i0, i1 + 1):
                    inside[row + i] = 1
        self.inside = inside
        # chamfer 3-4 distance to the nearest outside cell (in units of res * 3)
        big = 1 << 30
        d = [0 if not v else big for v in inside]
        for j in range(ny):
            for i in range(nx):
                k = j * nx + i
                if not d[k]:
                    continue
                v = d[k]
                if i:
                    v = min(v, d[k - 1] + 3)
                if j:
                    v = min(v, d[k - nx] + 3)
                    if i:
                        v = min(v, d[k - nx - 1] + 4)
                    if i < nx - 1:
                        v = min(v, d[k - nx + 1] + 4)
                d[k] = v
        for j in range(ny - 1, -1, -1):
            for i in range(nx - 1, -1, -1):
                k = j * nx + i
                if not d[k]:
                    continue
                v = d[k]
                if i < nx - 1:
                    v = min(v, d[k + 1] + 3)
                if j < ny - 1:
                    v = min(v, d[k + nx] + 3)
                    if i < nx - 1:
                        v = min(v, d[k + nx + 1] + 4)
                    if i:
                        v = min(v, d[k + nx - 1] + 4)
                d[k] = v
        self.dist = d                                    # mm = d / 3 * res
        # core = cells deeper than neck / 2; seeds = core components with enough area
        thr = (neck / 2) / res * 3
        label = [-1] * (nx * ny)
        seeds = []
        core_min = max(1, int(0.25 * min_area / (res * res)))
        for start in range(nx * ny):
            if d[start] <= thr or label[start] != -1:
                continue
            comp, q = [start], deque([start])
            label[start] = -2
            while q:
                k = q.popleft()
                i, j = k % nx, k // nx
                for n in (k - 1 if i else -1, k + 1 if i < nx - 1 else -1,
                          k - nx if j else -1, k + nx if j < ny - 1 else -1):
                    if n >= 0 and label[n] == -1 and d[n] > thr:
                        label[n] = -2
                        q.append(n)
                        comp.append(n)
            if len(comp) >= core_min:
                seeds.append(comp)
        for k in range(nx * ny):
            if label[k] == -2:
                label[k] = -1
        # geodesic growth from the seeds over the whole inside mask
        q = deque()
        for s, comp in enumerate(seeds):
            for k in comp:
                label[k] = s
                q.append(k)
        if not seeds:                                     # thin copper everywhere: one region
            for k in range(nx * ny):
                if inside[k]:
                    label[k] = 0
            seeds = [[k for k in range(nx * ny) if inside[k]]]
        else:
            while q:
                k = q.popleft()
                i, j = k % nx, k // nx
                lk = label[k]
                for n in (k - 1 if i else -1, k + 1 if i < nx - 1 else -1,
                          k - nx if j else -1, k + nx if j < ny - 1 else -1):
                    if n >= 0 and inside[n] and label[n] == -1:
                        label[n] = lk
                        q.append(n)
            # copper the growth could not reach (a neck thinner than one cell): own label
            extra = len(seeds)
            for k in range(nx * ny):
                if inside[k] and label[k] == -1:
                    comp, q2 = [], deque([k])
                    label[k] = extra
                    while q2:
                        c = q2.popleft()
                        comp.append(c)
                        i, j = c % nx, c // nx
                        for n in (c - 1 if i else -1, c + 1 if i < nx - 1 else -1,
                                  c - nx if j else -1, c + nx if j < ny - 1 else -1):
                            if n >= 0 and inside[n] and label[n] == -1:
                                label[n] = extra
                                q2.append(n)
                    seeds.append(comp)
                    extra += 1
        self.label = label
        self.n_labels = len(seeds)

    def cell(self, x, y) -> int:
        i = int((x - self.x0) / self.res)
        j = int((y - self.y0) / self.res)
        if 0 <= i < self.nx and 0 <= j < self.ny:
            return j * self.nx + i
        return -1

    def label_at(self, x, y) -> int:
        k = self.cell(x, y)
        return self.label[k] if k >= 0 and self.inside[k] else -1

    def label_near(self, x, y, r) -> int:
        """Label at a point, or on a ring around it (a via in a thermal-relief hole)."""
        lab = self.label_at(x, y)
        if lab >= 0:
            return lab
        for rr in (r + self.res, r + 2 * self.res, r + 4 * self.res):
            for a in range(24):
                ang = 2 * math.pi * a / 24
                lab = self.label_at(x + rr * math.cos(ang), y + rr * math.sin(ang))
                if lab >= 0:
                    return lab
        return -1

    def stats(self):
        """Per label: cell count, deepest cell, and the interfaces between labels."""
        nx = self.nx
        count, deep, sx, sy = {}, {}, {}, {}
        inter: dict[tuple[int, int], tuple[float, int]] = {}
        for k, lab in enumerate(self.label):
            if lab < 0:
                continue
            count[lab] = count.get(lab, 0) + 1
            sx[lab] = sx.get(lab, 0) + k % nx
            sy[lab] = sy.get(lab, 0) + k // nx
            if self.dist[k] > deep.get(lab, (-1, 0))[0]:
                deep[lab] = (self.dist[k], k)
            i = k % nx
            for n in ((k + 1) if i < nx - 1 else -1, k + nx if k + nx < len(self.label) else -1):
                if n >= 0 and self.label[n] >= 0 and self.label[n] != lab:
                    key = (min(lab, self.label[n]), max(lab, self.label[n]))
                    w = min(self.dist[k], self.dist[n])
                    if w > inter.get(key, (-1, 0))[0]:
                        inter[key] = (w, k)
        for lab in count:                         # marker: the centroid if it is in the region
            ci, cj = round(sx[lab] / count[lab]), round(sy[lab] / count[lab])
            k = cj * nx + ci
            if 0 <= k < len(self.label) and self.label[k] == lab:
                deep[lab] = (deep[lab][0], k)
        return count, deep, inter

    def xy(self, k):
        return (self.x0 + (k % self.nx + 0.5) * self.res, self.y0 + (k // self.nx + 0.5) * self.res)


class _Whole:
    """A fill polygon kept as one region (no selected trace runs over it, so no neck split needed)."""

    def __init__(self, poly: Polygon, res: float = 0.1):
        self.poly, self.res, self.n_labels = poly, res, 1

    def label_at(self, x, y) -> int:
        return 0 if self.poly.contains(x, y) else -1

    def label_near(self, x, y, r) -> int:
        if self.poly.contains(x, y):
            return 0
        for rr in (r + 0.05, r + 0.1, r + 0.2):
            for a in range(24):
                ang = 2 * math.pi * a / 24
                if self.poly.contains(x + rr * math.cos(ang), y + rr * math.sin(ang)):
                    return 0
        return -1


def _trace_samples(board, cfg, users, ref_nets, step=0.1):
    """(signal layer, x, y, length, net) points along selected traces' centre lines."""
    out = []
    for t in board.tracks:
        if t.net not in cfg.nets or t.net in ref_nets:
            continue
        if not any(t.layer in ls for ls in users.values()):
            continue
        for a, b in zip(t.points, t.points[1:]):
            L = math.dist(a, b)
            n = max(1, int(L / step))
            for s in range(n):
                f = (s + 0.5) / n
                out.append((t.layer, a[0] + (b[0] - a[0]) * f, a[1] + (b[1] - a[1]) * f, L / n, t.net))
    return out


def _uf(n):
    parent = list(range(n))

    def find(a):
        while parent[a] != a:
            parent[a] = parent[parent[a]]
            a = parent[a]
        return a

    def union(a, b):
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[ra] = rb
    return find, union


def check_islands(board: Board, cfg: Config, plans: list[LayerPlan], settings: dict | None = None):
    opt = {**DEFAULTS, **(settings or {})}
    if not opt.get("enabled", True):
        return [], {}
    ref_nets = cfg.gnd_nets | cfg.pwr_nets
    roles = cfg.roles
    findings: list[Finding] = []
    summary: dict[str, dict] = {}

    # signal layers that use each reference layer
    users: dict[str, list[str]] = {}
    for p in plans:
        for r, _, _ in p.refs:
            users.setdefault(r, []).append(p.layer)

    samples = _trace_samples(board, cfg, users, ref_nets)
    for net in sorted(ref_nets):
        regions: list[Region] = []
        rasters: dict[str, list[tuple[object, list[int]]]] = {}   # layer -> [(raster, label->rid)]
        for z in board.zones:
            if z.net != net:
                continue
            for layer, polys in z.fills.items():
                for pts in polys:
                    poly = Polygon(pts)
                    x0, y0, x1, y1 = poly.bbox
                    used = roles.get(layer) in REF_ROLES and any(
                        sl in users.get(layer, ()) and x0 <= x <= x1 and y0 <= y <= y1 and poly.contains(x, y)
                        for sl, x, y, _, _ in samples)
                    if not used:                       # nothing runs over it: one region, no raster
                        rg = Region(len(regions), net, layer, poly.area, ((x0 + x1) / 2, (y0 + y1) / 2))
                        regions.append(rg)
                        rasters.setdefault(layer, []).append((_Whole(poly), [rg.rid]))
                        continue
                    res = max(0.05, opt["neck_mm"] / 6)
                    while ((x1 - x0) / res + 3) * ((y1 - y0) / res + 3) > opt["max_cells"]:
                        res *= 1.25
                    ras = _Raster(poly, res, opt["neck_mm"], opt["min_area_mm2"])
                    count, deep, inter = ras.stats()
                    lab2rid = [-1] * ras.n_labels
                    for lab in range(ras.n_labels):
                        if lab not in count:
                            continue
                        dk = deep[lab][1]
                        rg = Region(len(regions), net, layer, count[lab] * res * res, ras.xy(dk))
                        lab2rid[lab] = rg.rid
                        regions.append(rg)
                    for (la, lb), (w, k) in inter.items():
                        a, b = lab2rid[la], lab2rid[lb]
                        if a >= 0 and b >= 0:
                            width = 2 * w / 3 * res
                            regions[a].links.append((b, "neck", width, ras.xy(k)))
                            regions[b].links.append((a, "neck", width, ras.xy(k)))
                    rasters.setdefault(layer, []).append((ras, lab2rid))
        if not regions:
            continue

        def rid_at(layer, x, y, r=0.0):
            for ras, l2r in rasters.get(layer, ()):
                lab = ras.label_near(x, y, r) if r else ras.label_at(x, y)
                if lab >= 0:
                    return l2r[lab]
            return -1

        find_s, union_s = _uf(len(regions))
        find_a, union_a = _uf(len(regions))
        # strong ties: vias and through pads of the net
        nodes = [(v.at, v.size / 2, (lambda l, v=v: board.via_spans(v, l))) for v in board.vias if v.net == net]
        nodes += [(p.at, p.radius, (lambda l: True)) for p in board.pads if p.net == net and p.through]
        for at, rad, spans in nodes:
            hit = []
            for layer in rasters:
                if spans(layer):
                    rid = rid_at(layer, at[0], at[1], rad)
                    if rid >= 0 and rid not in hit:
                        hit.append(rid)
            if len(hit) > 1:                      # a tie only if it joins two regions
                for rid in hit:
                    regions[rid].ties.append(at)
            for a, b in zip(hit, hit[1:]):
                union_s(a, b)
                union_a(a, b)
        # weak links: necks inside a polygon, and tracks of the net
        for rg in regions:
            for other, kind, w, at in rg.links:
                union_a(rg.rid, other)
        for t in board.tracks:
            if t.net != net or t.layer not in rasters:
                continue
            a = rid_at(t.layer, *t.points[0])
            b = rid_at(t.layer, *t.points[-1])
            if a >= 0 and b >= 0 and a != b:
                regions[a].links.append((b, "track", t.width, t.points[0]))
                regions[b].links.append((a, "track", t.width, t.points[-1]))
                union_a(a, b)

        # main plane: the strongly tied group with the largest area
        area_s: dict[int, float] = {}
        for rg in regions:
            area_s[find_s(rg.rid)] = area_s.get(find_s(rg.rid), 0.0) + rg.area
        main = max(area_s, key=area_s.get)
        main_region = max((rg for rg in regions if find_s(rg.rid) == main), key=lambda r: r.area)
        main_any = find_a(main_region.rid)
        # the plane of each layer: its largest region inside the main group. Everything else is an island.
        principal: dict[str, Region] = {}
        for rg in regions:
            if find_s(rg.rid) == main and (rg.layer not in principal or rg.area > principal[rg.layer].area):
                principal[rg.layer] = rg
        planes = {rg.rid for rg in principal.values()}
        summary[net] = {"regions": len(regions), "islands": len(regions) - len(planes),
                        "main_area_mm2": round(area_s[main], 1),
                        "planes": {l: round(rg.area, 1) for l, rg in sorted(principal.items())}}

        # which regions do selected traces run over (centre line), and how far from ties
        over: dict[int, dict[str, list[tuple[float, float]]]] = {}
        refs_of = {sl: [r for r in rasters if sl in users.get(r, ()) and roles.get(r) in REF_ROLES]
                   for sl in {s[0] for s in samples}}
        for sl, x, y, dl, snet in samples:
            for ref in refs_of.get(sl, ()):
                rid = rid_at(ref, x, y)
                if rid >= 0 and rid not in planes:
                    over.setdefault(rid, {}).setdefault(snet, []).append((x, y, dl, sl))

        for rid, by_net in sorted(over.items()):
            rg = regions[rid]
            lengths = {n: sum(p[2] for p in pts) for n, pts in by_net.items()}
            if sum(lengths.values()) < opt["min_length_mm"]:
                continue
            sig_layers = sorted({p[3] for pts in by_net.values() for p in pts}, key=board.copper_layers.index)
            nets_txt = ", ".join(f"{n} {L:.1f} mm" for n, L in sorted(lengths.items(), key=lambda kv: -kv[1]))
            where = f"around ({rg.centre[0]:.2f}, {rg.centre[1]:.2f}), {rg.area:.1f} mm2"
            classes = ",".join(sorted({c for n in lengths for c in cfg.netclass_of.get(n, ["Default"])}))
            common = dict(net=", ".join(sorted(lengths)), netclass=classes, layer=rg.layer, ref_layers=[rg.layer],
                          start=rg.centre, end=rg.centre, via_layers=sig_layers,
                          radius=min(5.0, max(0.5, math.sqrt(rg.area / math.pi))))
            if find_s(rid) == main:
                n_ties = len({(round(x, 3), round(y, 3)) for x, y in rg.ties})
                if n_ties < opt["min_ties"]:
                    findings.append(Finding(
                        "warning", "island_weak_tie",
                        f"{nets_txt} over a {net} island on {rg.layer} {where}: tied to the main {net} by only {n_ties} via/pad(s); "
                        f"the return current has to funnel through it. Add {net} vias in the island", **common))
                elif rg.ties:
                    far = max(min(math.dist((x, y), tie) for tie in rg.ties)
                              for pts in by_net.values() for x, y, *_ in pts)
                    if far > opt["max_tie_distance_mm"]:
                        findings.append(Finding(
                            "warning", "island_tie_far",
                            f"{nets_txt} over a {net} island on {rg.layer} {where}: part of the trace is {far:.1f} mm from the island's "
                            f"nearest tie via (limit {opt['max_tie_distance_mm']:g} mm)", **common))
            elif find_a(rid) == main_any:
                weak = sorted(rg.links, key=lambda l: l[2])
                desc = "; ".join(f"{'a neck' if k == 'neck' else 'a track'} about {w:.2f} mm wide at "
                                 f"({at[0]:.2f}, {at[1]:.2f})" for _, k, w, at in weak[:2]) or "a weak link"
                findings.append(Finding(
                    "warning", "island_neck",
                    f"{nets_txt} over a {net} island on {rg.layer} {where}: no via ties it to the main {net}; it reaches the plane only "
                    f"through {desc}", **common))
            else:
                findings.append(Finding(
                    "error", "island_floating",
                    f"{nets_txt} over {net} copper on {rg.layer} {where}: it is not connected to the main {net} at all "
                    f"(no via, pad, neck or track), so it carries no return current", **common))
    return findings, summary
