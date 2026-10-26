"""Planar geometry for the reference-plane check (standard library only).

Polygons are KiCad filled polygons. KiCad stores each fill as one "fractured"
outline: holes are joined to the outer edge by zero-width slits. The even-odd
rule handles that representation directly, so no polygon boolean library is
needed. All work is done along lines: for a line from A to B we compute the
parameter intervals t in [0, 1] that lie inside copper.
"""

from __future__ import annotations

import math

Interval = tuple[float, float]


# --------------------------------------------------------------------------- intervals

def iv_union(ivs: list[Interval]) -> list[Interval]:
    out: list[Interval] = []
    for s, e in sorted(i for i in ivs if i[1] > i[0]):
        if out and s <= out[-1][1] + 1e-12:
            if e > out[-1][1]:
                out[-1] = (out[-1][0], e)
        else:
            out.append((s, e))
    return out


def iv_intersect(a: list[Interval], b: list[Interval]) -> list[Interval]:
    a, b = iv_union(a), iv_union(b)
    out, i, j = [], 0, 0
    while i < len(a) and j < len(b):
        s, e = max(a[i][0], b[j][0]), min(a[i][1], b[j][1])
        if e > s:
            out.append((s, e))
        if a[i][1] < b[j][1]:
            i += 1
        else:
            j += 1
    return out


def iv_complement(a: list[Interval], lo: float = 0.0, hi: float = 1.0) -> list[Interval]:
    out, cur = [], lo
    for s, e in iv_union(a):
        if s > cur:
            out.append((cur, min(s, hi)))
        cur = max(cur, e)
    if cur < hi:
        out.append((cur, hi))
    return [i for i in out if i[1] > i[0]]


def iv_subtract(a: list[Interval], b: list[Interval]) -> list[Interval]:
    return iv_intersect(a, iv_complement(b))


def iv_length(a: list[Interval]) -> float:
    return sum(e - s for s, e in iv_union(a))


# --------------------------------------------------------------------------- polygon

class Polygon:
    """A closed polygon (possibly fractured) with a grid index over its edges."""

    def __init__(self, pts: list[tuple[float, float]], cell: float = 1.0):
        if len(pts) > 1 and pts[0] == pts[-1]:
            pts = pts[:-1]
        self.pts = pts
        self.cell = cell
        n = len(pts)
        self.edges = [(pts[i][0], pts[i][1], pts[(i + 1) % n][0], pts[(i + 1) % n][1])
                      for i in range(n)] if n >= 3 else []
        xs = [p[0] for p in pts] or [0.0]
        ys = [p[1] for p in pts] or [0.0]
        self.bbox = (min(xs), min(ys), max(xs), max(ys))
        self._grid: dict[tuple[int, int], list[int]] = {}
        self._rows: dict[int, list[int]] = {}
        self.row_h = cell / 8                      # point-in-polygon rows: thin, so few edges each
        for idx, (x1, y1, x2, y2) in enumerate(self.edges):
            for key in cells_along(x1, y1, x2, y2, cell):
                self._grid.setdefault(key, []).append(idx)
            for r in range(math.floor(min(y1, y2) / self.row_h), math.floor(max(y1, y2) / self.row_h) + 1):
                self._rows.setdefault(r, []).append(idx)

    @property
    def area(self) -> float:
        s = 0.0
        for x1, y1, x2, y2 in self.edges:
            s += x1 * y2 - x2 * y1
        return abs(s) / 2.0

    def contains(self, x: float, y: float) -> bool:
        x0, y0, x1, y1 = self.bbox
        if not (x0 <= x <= x1 and y0 <= y <= y1):
            return False
        inside = False
        for idx in self._rows.get(math.floor(y / self.row_h), ()):
            ax, ay, bx, by = self.edges[idx]
            if (ay > y) != (by > y):
                xi = ax + (y - ay) * (bx - ax) / (by - ay)
                if x < xi:
                    inside = not inside
        return inside

    def inside_intervals(self, ax: float, ay: float, bx: float, by: float) -> list[Interval]:
        """Parameter intervals of segment A->B (t in [0,1]) that are inside.

        Every edge crossing (including ones exactly at t = 0 or t = 1) splits the segment;
        each open piece between two splits is then classified by testing its midpoint.
        Nothing depends on how a boundary point itself is classified, so the result is the
        same whichever way the segment is traversed.
        """
        x0, y0, x1, y1 = self.bbox
        if max(ax, bx) < x0 or min(ax, bx) > x1 or max(ay, by) < y0 or min(ay, by) > y1:
            return []
        dx, dy = bx - ax, by - ay
        cand: set[int] = set()
        for key in cells_along(ax, ay, bx, by, self.cell):
            cand.update(self._grid.get(key, ()))
        cuts = {0.0, 1.0}
        for idx in cand:
            px, py, qx, qy = self.edges[idx]
            sp = dx * (py - ay) - dy * (px - ax)
            sq = dx * (qy - ay) - dy * (qx - ax)
            if (sp > 0) == (sq > 0) and sp != 0 and sq != 0:
                continue
            ex, ey = qx - px, qy - py
            den = dx * ey - dy * ex
            if den == 0:
                continue                      # parallel / collinear: no single crossing point
            t = ((px - ax) * ey - (py - ay) * ex) / den
            u = ((px - ax) * dy - (py - ay) * dx) / den
            if -1e-12 <= t <= 1.0 + 1e-12 and -1e-9 <= u <= 1.0 + 1e-9:
                cuts.add(min(1.0, max(0.0, t)))
        ts = sorted(cuts)
        out = []
        for s0, s1 in zip(ts, ts[1:]):
            if s1 - s0 <= 1e-12:
                continue
            m = (s0 + s1) / 2
            if self.contains(ax + dx * m, ay + dy * m):
                out.append((s0, s1))
        return iv_union(out)          # merges the touching pieces either side of a fracture slit


def cells_along(x1: float, y1: float, x2: float, y2: float, cell: float):
    """Grid cells touched by a segment, padded by one cell on every side."""
    length = math.hypot(x2 - x1, y2 - y1)
    steps = max(1, int(length / (cell * 0.5)) + 1)
    seen = set()
    for i in range(steps + 1):
        t = i / steps
        cx = math.floor((x1 + (x2 - x1) * t) / cell)
        cy = math.floor((y1 + (y2 - y1) * t) / cell)
        if (cx, cy) in seen:
            continue
        seen.add((cx, cy))
    out = set()
    for cx, cy in seen:
        for ox in (-1, 0, 1):
            for oy in (-1, 0, 1):
                out.add((cx + ox, cy + oy))
    return out


def circle_interval(ax, ay, bx, by, cx, cy, r) -> list[Interval]:
    """Part of segment A->B (t in [0,1]) within radius r of C."""
    dx, dy = bx - ax, by - ay
    fx, fy = ax - cx, ay - cy
    a = dx * dx + dy * dy
    if a == 0:
        return [(0.0, 1.0)] if fx * fx + fy * fy <= r * r else []
    b = 2 * (fx * dx + fy * dy)
    c = fx * fx + fy * fy - r * r
    disc = b * b - 4 * a * c
    if disc <= 0:
        return []
    sq = math.sqrt(disc)
    t0, t1 = (-b - sq) / (2 * a), (-b + sq) / (2 * a)
    s, e = max(0.0, t0), min(1.0, t1)
    return [(s, e)] if e > s else []


def arc_points(s, m, e, max_seg_deg: float = 10.0):
    """Sample a 3-point arc into a polyline."""
    (x1, y1), (x2, y2), (x3, y3) = s, m, e
    d = 2 * (x1 * (y2 - y3) + x2 * (y3 - y1) + x3 * (y1 - y2))
    if abs(d) < 1e-12:
        return [s, e]
    ux = ((x1**2 + y1**2) * (y2 - y3) + (x2**2 + y2**2) * (y3 - y1) + (x3**2 + y3**2) * (y1 - y2)) / d
    uy = ((x1**2 + y1**2) * (x3 - x2) + (x2**2 + y2**2) * (x1 - x3) + (x3**2 + y3**2) * (x2 - x1)) / d
    r = math.dist((ux, uy), s)
    a1 = math.atan2(y1 - uy, x1 - ux)
    a2 = math.atan2(y2 - uy, x2 - ux)
    a3 = math.atan2(y3 - uy, x3 - ux)
    tau = 2 * math.pi
    sweep = (a3 - a1) % tau
    if (a2 - a1) % tau > sweep:
        sweep -= tau
    steps = max(2, int(abs(math.degrees(sweep)) / max_seg_deg) + 1)
    pts = [(ux + r * math.cos(a1 + sweep * i / steps), uy + r * math.sin(a1 + sweep * i / steps))
           for i in range(steps + 1)]
    pts[0], pts[-1] = s, e
    return pts
