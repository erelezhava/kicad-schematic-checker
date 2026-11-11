"""BGA areas: the ball field of each BGA footprint, where no extra via fits.

Between balls there is room for one dog-bone via per ball at most, so a dedicated return
via next to a signal via cannot be added. Return-via findings for signal vias inside an
area are reported as warnings and say so; the nearest GND ball's fan-out via is the return.

A footprint is a BGA when a pad has Fabrication Property "BGA pad" or the footprint's library
name contains "BGA", and it has at least 4 SMD pads. The area is the box around the ball
centres grown by one pitch, which includes the dog-bone vias of the outer row.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from .pcb import Board


@dataclass
class BgaArea:
    ref: str
    box: tuple[float, float, float, float]   # x0, y0, x1, y1
    pitch: float
    balls: int

    def contains(self, at) -> bool:
        x0, y0, x1, y1 = self.box
        return x0 <= at[0] <= x1 and y0 <= at[1] <= y1


def bga_areas(board: Board) -> list[BgaArea]:
    groups: dict[str, list] = {}
    for p in board.pads:
        if p.through:
            continue
        if p.fab == "pad_prop_bga" or "bga" in p.footprint.lower():
            groups.setdefault(p.ref or p.footprint, []).append(p)
    areas = []
    for ref, pads in sorted(groups.items()):
        if len(pads) < 4:
            continue
        pts = [p.at for p in pads]
        nearest = [min(math.dist(a, q) for j, q in enumerate(pts) if j != i) for i, a in enumerate(pts)]
        pitch = sorted(nearest)[len(nearest) // 2]
        xs, ys = [a[0] for a in pts], [a[1] for a in pts]
        areas.append(BgaArea(ref, (min(xs) - pitch, min(ys) - pitch, max(xs) + pitch, max(ys) + pitch),
                             round(pitch, 3), len(pads)))
    return areas


def area_at(areas: list[BgaArea], at) -> BgaArea | None:
    return next((a for a in areas if a.contains(at)), None)
