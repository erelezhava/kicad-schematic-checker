"""Via next to an SMD pad: solder wicking and bridging checks (preflight).

KiCad DRC already enforces the JLC fab limits for different nets (via hole to copper,
copper to copper). These checks cover assembly, which needs each via's tenting and the real
mask openings, so they run in Python on the board file:

  same net, tented via      copper gap via ring -> pad edge >= assembly.via_to_smd_pad_same_net_tented
  same net, untented via    mask strip between the two openings >= JLC mask dam
                            (gap >= dam + via mask expansion + pad mask expansion)
  different net, untented   copper gap >= clearance.smd_pad_to_pad_diff_net (the via is an exposed pad)

A via whose centre lies inside the pad is via-in-pad and left to the DRC via-in-pad rule.
Gaps are copper edge to copper edge, in mm.
"""

from __future__ import annotations

import math

from .board import BoardInfo

SIDES = {"F.Cu": ("front", "F.Mask"), "B.Cu": ("back", "B.Mask")}


def pad_signed_distance(x: float, y: float, pad: dict) -> float | None:
    """Distance from a board point to the pad's copper outline; negative inside.
    rect, roundrect, oval and circle are exact; trapezoid/custom use the pad's size box."""
    if not pad.get("size") or not pad.get("xy"):
        return None
    w, h = pad["size"]
    shape = pad.get("shape") or "rect"
    a = math.radians(pad.get("angle") or 0.0)
    dx, dy = x - pad["xy"][0], y - pad["xy"][1]
    lx = dx * math.cos(a) - dy * math.sin(a)  # inverse of the board's R(-angle)
    ly = dx * math.sin(a) + dy * math.cos(a)
    if shape == "circle":
        r = w / 2
    elif shape == "oval":
        r = min(w, h) / 2
    elif shape == "roundrect":
        r = min(max(pad.get("rratio", 0.25), 0.0), 0.5) * min(w, h)
    else:
        r = 0.0
    qx, qy = abs(lx) - (w / 2 - r), abs(ly) - (h / 2 - r)
    outside = math.hypot(max(qx, 0.0), max(qy, 0.0))
    inside = min(max(qx, qy), 0.0)
    return outside + inside - r


def _tented(via: dict, side: str, info: BoardInfo) -> bool:
    own = (via.get("tenting") or {}).get(side)
    return info.default_tenting.get(side, True) if own is None else own


def _via_on(via: dict, layer: str) -> bool:
    if via.get("type") == "through":
        return True
    return layer in (via.get("layers") or [])


def check(info: BoardInfo, caps: dict, dam: float) -> dict[str, list[dict]]:
    """Return {"same_tented": [...], "same_untented": [...], "diff_untented": [...]}, worst first."""
    same_min = caps["assembly"]["via_to_smd_pad_same_net_tented"]
    diff_min = caps["clearance"]["smd_pad_to_pad_diff_net"]
    out: dict[str, list[dict]] = {"same_tented": [], "same_untented": [], "diff_untented": []}
    pads = [p for p in info.pads if p.get("type") == "smd" and p.get("size") and p.get("xy")]
    for via in info.vias:
        if not via.get("at") or not via.get("diameter"):
            continue
        vx, vy = via["at"]
        vr = via["diameter"] / 2
        for pad in pads:
            reach = math.hypot(*pad["size"]) / 2 + vr + max(diff_min, dam) + 0.5
            if abs(pad["xy"][0] - vx) > reach or abs(pad["xy"][1] - vy) > reach:
                continue
            for layer in pad.get("layers", []):
                if layer not in SIDES or not _via_on(via, layer):
                    continue
                side, mask_layer = SIDES[layer]
                sd = pad_signed_distance(vx, vy, pad)
                if sd is None or sd < 0:  # via-in-pad: DRC rule
                    continue
                gap = sd - vr
                same = via.get("net") is not None and via.get("net") == pad.get("net")
                tented = _tented(via, side, info)
                pad_open = mask_layer in pad.get("layers", [])
                pad_exp = pad["mask_margin"] if pad.get("mask_margin") is not None else info.mask_expansion
                item = {"pad": f"{pad.get('ref')}.{pad.get('number')}", "pad_net": pad.get("net"),
                        "via_net": via.get("net"), "via": f"{via.get('hole') or 0:g}/{via['diameter']:g}",
                        "at": [round(vx, 3), round(vy, 3)], "layer": layer, "gap_mm": round(gap, 3),
                        "tented": tented}
                if same and tented:
                    if gap < same_min - 1e-9:
                        out["same_tented"].append({**item, "min_mm": same_min})
                elif same and pad_open:
                    web = gap - info.mask_expansion - pad_exp
                    if web < dam - 1e-9:
                        out["same_untented"].append({**item, "mask_web_mm": round(web, 3), "min_mm": dam,
                                                     "need_gap_mm": round(dam + info.mask_expansion + pad_exp, 3)})
                elif not same and not tented and pad_open:
                    if gap < diff_min - 1e-9:
                        out["diff_untented"].append({**item, "min_mm": diff_min})
    for rows in out.values():
        rows.sort(key=lambda r: r["gap_mm"])
    return out


def describe(item: dict) -> str:
    extra = f", mask strip {item['mask_web_mm']:.3f} mm" if "mask_web_mm" in item else ""
    nets = item["pad_net"] if item["pad_net"] == item["via_net"] else f"{item['via_net']} via / {item['pad_net']} pad"
    return (f"{item['pad']} ({nets}) {item['layer']}: gap {item['gap_mm']:.3f} mm{extra}, "
            f"via {item['via']} (hole/dia) at ({item['at'][0]:g}, {item['at'][1]:g})")
