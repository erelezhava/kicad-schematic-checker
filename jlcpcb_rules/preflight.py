"""Board-level JLCPCB checks that KiCad custom rules cannot express."""

from __future__ import annotations

import math
from collections import Counter
from dataclasses import asdict, dataclass
from pathlib import Path

from .board import BoardInfo
from .generator import _Builder, read_profile_line
from .profile import Profile, load_capabilities

COMPARED = ("layers", "outer_oz", "inner_oz", "thickness", "finish", "mask_color")


@dataclass
class Finding:
    severity: str  # error | warning | info
    code: str
    message: str


def run(info: BoardInfo, profile: Profile, caps: dict | None = None,
        rules_path: Path | None = None) -> list[Finding]:
    c = caps or load_capabilities()
    out: list[Finding] = []

    def add(sev, code, msg):
        out.append(Finding(sev, code, msg))

    p = profile
    # Layers
    if p.layers not in c["layers"]["allowed"]:
        add("error", "layers", f"{p.layers} copper layers: JLC builds {c['layers']['allowed']}")
    elif p.layers > c["layers"]["review_above"]:
        add("info", "layers", f"{p.layers} layers: JLC recommends sending the stackup for review")

    # Thickness
    t, th = p.thickness, c["thickness"]
    if not th["min"] <= t <= th["max"]:
        add("error", "thickness", f"{t:g} mm board: JLC range is {th['min']:g}-{th['max']:g} mm")
    else:
        if t > th["max_conflict"]:
            add("warning", "thickness", f"{t:g} mm: JLC FAQ says max {th['max_conflict']:g} mm, "
                "capability table says 4.5 mm; confirm before ordering")
        if t >= th["thick_from"] and p.layers < th["thick_min_layers"]:
            add("error", "thickness", f"{t:g} mm needs {th['thick_min_layers']}+ layers at JLC")
        elif not any(abs(t - s) < 1e-6 for s in th["standard"]) and t < th["thick_from"]:
            add("warning", "thickness", f"{t:g} mm is not a standard JLC FR-4 thickness "
                f"({', '.join(f'{s:g}' for s in th['standard'])})")

    # Copper weights
    cu = c["copper_oz"]
    if p.multilayer:
        if p.outer_oz not in cu["outer_multilayer"]:
            add("error", "copper", f"outer {p.outer_oz:g} oz: multilayer boards offer {cu['outer_multilayer']} oz")
        if p.inner_oz not in cu["inner"]:
            add("error", "copper", f"inner {p.inner_oz:g} oz: JLC offers {cu['inner']} oz")
        elif p.inner_oz in cu["inner_paid"]:
            add("warning", "paid", f"inner {p.inner_oz:g} oz copper is a paid option")
    else:
        if p.outer_oz not in cu["outer_2L"]:
            add("error", "copper", f"outer {p.outer_oz:g} oz: 1-2 layer boards offer {cu['outer_2L']} oz")
        elif p.outer_oz in cu["outer_2L_paid"]:
            add("warning", "paid", f"{p.outer_oz:g} oz heavy copper is a paid option"
                + (f" (JLC FAQ lists max {cu['outer_2L_conflict_max']} oz; confirm)"
                   if p.outer_oz > cu["outer_2L_conflict_max"] else ""))

    # Finish and mask
    fr = c["finish_restrictions"]
    if p.finish is None:
        add("warning", "finish", "copper finish not set (Board Setup > Physical Stackup > Copper "
            "finish). Rules were generated for an unknown finish.")
    if p.finish in ("HASL", "HASL_LF") and t <= fr["hasl_not_below_thickness"] + 1e-9:
        add("error", "finish", f"HASL is not offered on boards <= {fr['hasl_not_below_thickness']:g} mm")
    if p.finish == "OSP" and p.layers == 1:
        add("error", "finish", "OSP is not offered on single-sided FR-4")

    # Outline
    size = info.size
    if not size:
        add("error", "outline", "no board outline found on Edge.Cuts")
    else:
        w, h = sorted(size, reverse=True)
        mn = sorted(c["board_size"]["min_fr4"], reverse=True)
        if w < mn[0] or h < mn[1]:
            add("error", "size", f"board {w:g} x {h:g} mm is below the {mn[0]} x {mn[1]} mm minimum")
        key = str(min(p.layers, 6)) if p.layers in (1, 2, 4) or p.layers >= 6 else "6"
        mx = sorted(c["board_size"]["max"][key], reverse=True)
        note = c["board_size"]["max_note"].get(key)
        if w > mx[0] or h > mx[1]:
            if note and w <= max(note) and h <= min(note):
                add("warning", "size", f"board {w:g} x {h:g} mm exceeds the standard {mx[0]} x {mx[1]} mm "
                    f"but is within the {note[0]} x {note[1]} mm noted on the JLC page; confirm")
            else:
                add("error", "size", f"board {w:g} x {h:g} mm exceeds the {mx[0]} x {mx[1]} mm maximum "
                    f"for {p.layers} layers")
        add("info", "size", f"board outline {w:g} x {h:g} mm")

    # Vias
    kinds = Counter(v["type"] for v in info.vias)
    hdi = sum(n for k, n in kinds.items() if k != "through")
    if hdi:
        if p.layers < c["hdi"]["min_layers"]:
            add("error", "hdi", f"{hdi} blind/buried/micro vias on a {p.layers}-layer board")
        else:
            add("warning", "paid", f"HDI: {dict((k, n) for k, n in kinds.items() if k != 'through')} "
                "blind/buried/micro vias (paid HDI process)")
            if p.finish in ("HASL", "HASL_LF") and p.layers >= fr["hasl_not_hdi_from_layers"]:
                add("error", "finish", f"HASL is not offered on {fr['hasl_not_hdi_from_layers']}+ layer HDI boards")
            mx = sorted(c["board_size"]["hdi_max"], reverse=True)
            if size and (max(size) > mx[0] or min(size) > mx[1]):
                add("error", "size", f"HDI boards max {mx[0]} x {mx[1]} mm")
    through = [v for v in info.vias if v["type"] == "through" and v["hole"] and v["diameter"]]
    if through:
        v = c["via"]
        hole = min(x["hole"] for x in through)
        small = [x for x in through if x["hole"] < v["free_hole_min"] - 1e-9 or
                 (x["hole"] < v["free_small_hole_below"] - 1e-9 and
                  x["diameter"] < v["free_small_hole_diameter_min"] - 1e-9)]
        abs_min = v["micro_hole_min"] if p.micro_drill_ok else v["hole_min"]
        too_small = [x for x in through if x["hole"] < abs_min - 1e-9]
        if too_small:
            add("error", "vias", f"{len(too_small)} vias below the {abs_min:g} mm minimum hole for this "
                "board" + ("" if p.micro_drill_ok else
                           f" ({v['micro_hole_min']:g} mm needs <= 1.0 mm board and ENIG/OSP)"))
        if small:
            worst = min(small, key=lambda x: (x["hole"], x["diameter"]))
            add("warning", "paid", f"{len(small)} vias need the paid via-size option "
                f"(smallest {worst['hole']:g}/{worst['diameter']:g} mm hole/diameter)")
        add("info", "vias", f"{len(through)} through vias, smallest hole {hole:g} mm")
    bd = sum(1 for x in info.vias + info.pads if x.get("backdrill"))
    if bd:
        add("warning", "paid", f"{bd} backdrilled holes (paid; {c['backdrill']['min_layers']}+ layers, "
            f">= {c['backdrill']['board_thickness_min']:g} mm)")
    pm = sum(1 for x in info.vias + info.pads if x.get("post_machining"))
    if pm:
        add("warning", "paid", f"{pm} countersunk/counterbored holes (paid process)")
    if info.default_via_filled and info.default_via_capped and p.layers < c["via"]["via_in_pad_free_from_layers"]:
        add("warning", "paid", "board default fills and caps all vias: JLC via-in-pad process is "
            f"paid below {c['via']['via_in_pad_free_from_layers']} layers")

    # Pads
    props = Counter(pd["fab_property"] for pd in info.pads if pd["fab_property"])
    if props.get("pad_prop_castellated"):
        ca = c["castellated"]
        add("warning", "paid", f"{props['pad_prop_castellated']} castellated pads: castellated holes "
            "are an order option (check the price)")
        if size and (min(size) < min(ca["board_min_mm"]) or max(size) < max(ca["board_min_mm"])):
            add("error", "castellated", f"castellated boards must be >= {ca['board_min_mm'][0]} x "
                f"{ca['board_min_mm'][1]} mm")
        if t < ca["board_thickness_min"]:
            add("error", "castellated", f"castellated boards must be >= {ca['board_thickness_min']:g} mm thick")
    if props.get("pad_prop_pressfit"):
        add("info", "pressfit", f"{props['pad_prop_pressfit']} press-fit pads: hole tolerance +-0.05 mm, "
            "multilayer ENIG only")
    edge = sum(1 for pd in info.pads if pd["type"] == "connect")
    if edge:
        add("warning", "paid", f"{edge} edge-connector pads: gold fingers are an order option; "
            "their specs are not in this capability snapshot")

    try:
        _drill_steps(info, p, c, add)
        _bga(info, p, c, add)
    except ValueError as exc:  # unsupported stackup, already reported above
        add("info", "skipped", f"drill-step and BGA checks skipped: {exc}")

    # Rules file
    if rules_path is not None:
        if not rules_path.exists():
            add("warning", "rules", f"{rules_path.name} not found: install the JLC rules "
                "('python3 -m jlcpcb_rules install')")
        else:
            installed = read_profile_line(rules_path.read_text(encoding="utf-8"))
            if installed is None:
                add("warning", "rules", f"{rules_path.name} has no JLC block")
            else:
                diff = [f"{k}: rules {getattr(installed, k)} vs board {getattr(p, k)}"
                        for k in COMPARED
                        if getattr(p, k) is not None and getattr(installed, k) != getattr(p, k)]
                if p.finish is None and installed.finish is not None:
                    add("warning", "rules", f"board has no copper finish set; installed rules assume "
                        f"{installed.finish}. Set it in the stackup so both agree.")
                if diff:
                    add("error", "rules", "installed JLC rules do not match the board stackup "
                        "(regenerate): " + "; ".join(diff))
                else:
                    add("info", "rules", f"{rules_path.name} matches the board stackup "
                        f"(tier {installed.tier})")
    return out


def round_up_to_step(hole: float, step: float) -> float:
    return round(math.ceil(hole / step - 1e-6) * step, 4)


def _drill_steps(info: BoardInfo, p: Profile, c: dict, add) -> None:
    """Holes off the tool step are drilled at the next step up: re-check the rings."""
    step = c["drill_step"]["step"]
    via_ring = c["via"]["diameter_over_hole_min"] / 2
    pth_ring = _Builder(p, c).pth_ring_min()[0]
    off, broken = 0, []
    for v in info.vias:
        if not v["hole"] or not v["diameter"]:
            continue
        eff = round_up_to_step(v["hole"], step)
        if eff - v["hole"] > 1e-6:
            off += 1
            ring = (v["diameter"] - eff) / 2
            if ring < via_ring - 1e-6:
                at = v.get("at")
                broken.append(f"via {v['hole']:g}/{v['diameter']:g} at "
                              f"({at[0]:g}, {at[1]:g}) -> drilled {eff:g}, ring {ring:.3f}" if at else
                              f"via {v['hole']:g}/{v['diameter']:g} -> drilled {eff:g}, ring {ring:.3f}")
    for pd in info.pads:
        if pd["type"] != "thru_hole" or not pd["hole"] or pd["slot"] or not pd["size"]:
            continue
        eff = round_up_to_step(pd["hole"], step)
        if eff - pd["hole"] > 1e-6:
            off += 1
            ring = (min(pd["size"]) - eff) / 2
            if ring < pth_ring - 1e-6:
                broken.append(f"{pd['ref']} pad {pd['number']} hole {pd['hole']:g} -> drilled {eff:g}, "
                              f"ring {ring:.3f} < {pth_ring:g}")
    if broken:
        add("error", "drill_step", f"{len(broken)} holes lose their annular ring when rounded up to the "
            f"{step:g} mm drill step: " + "; ".join(broken[:6]) + (" ..." if len(broken) > 6 else ""))
    if off:
        add("warning", "drill_step", f"{off} holes are not on the {step:g} mm drill step and will be "
            "drilled at the next step up (put hole sizes on the step)")


def _bga(info: BoardInfo, p: Profile, c: dict, add) -> None:
    """Pitch-based BGA feasibility with this stackup's JLC limits."""
    groups: dict[str, list[dict]] = {}
    for pd in info.pads:
        if pd["type"] != "smd" or not pd["at"] or not pd["size"]:
            continue
        if pd["fab_property"] == "pad_prop_bga" or "bga" in (pd["footprint"] or "").lower():
            groups.setdefault(pd["ref"] or pd["footprint"], []).append(pd)
    b = _Builder(p, c)
    w = cl = b.outer_trace()
    smd_gap = max(c["clearance"]["smd_pad_to_pad_diff_net"], cl)
    dam = b.mask_dam()
    hole_cu = c["clearance"]["via_hole_to_track"]
    v = c["via"]
    vias = [("free 0.3/0.4", 0.3, 0.4), ("free 0.2/0.45", 0.2, 0.45), ("paid 0.15/0.25", 0.15, 0.25)]
    if p.micro_drill_ok:
        vias.append(("paid 0.1/0.2", 0.1, 0.2))
    for ref, pads in sorted(groups.items()):
        if len(pads) < 4:
            continue
        pts = [pd["at"] for pd in pads]
        pitch = min(math.dist(a, q) for i, a in enumerate(pts) for q in pts[i + 1:])
        pitch = round(pitch, 3)
        d = sorted(min(pd["size"]) for pd in pads)[len(pads) // 2]
        gap = pitch - d
        notes = [f"{ref}: {len(pads)} balls, pitch {pitch:g} mm, pad {d:g} mm, pad gap {gap:.3f} mm"]
        sev = "info"
        if gap < smd_gap - 1e-6:
            sev = "error"
            notes.append(f"pad gap below JLC {smd_gap:g} mm SMD spacing")
        if d < c["bga"]["pad_min"] - 1e-6:
            sev = "error"
            notes.append(f"pad below JLC {c['bga']['pad_min']:g} mm minimum")
        elif d < c["bga"]["pad_enig_below"] - 1e-6 and p.finish != "ENIG":
            sev = "error"
            notes.append("pads under 0.25 mm need ENIG")
        if gap < dam - 1e-6:
            sev = "warning" if sev == "info" else sev
            notes.append(f"no mask dam between balls (gap < {dam:g} mm)")
        tracks = max(0, math.floor((gap - cl) / (w + cl) + 1e-9))
        msg = f"{tracks} track(s) of {w:g}/{cl:g} mm fit between balls"
        if p.multilayer:  # JLC allows 3 mil traces inside BGA fanouts
            fan = max(0, math.floor((gap - cl) / (0.0762 + cl) + 1e-9))
            if fan > tracks:
                msg += f" ({fan} with JLC's 3 mil BGA-fanout trace)"
        notes.append(msg)
        centre = pitch * math.sqrt(2) / 2 - d / 2   # via centre (between 4 balls) to ball edge
        fits = [name for name, h, dia in vias if dia / 2 + cl <= centre + 1e-6 and h / 2 + hole_cu <= centre + 1e-6]
        vip = [name for name, h, dia in vias if dia <= d + 1e-6 and v["via_in_pad_hole_min"] - 1e-6 <= h]
        if fits:
            best = fits[0]
            notes.append(f"dog-bone fanout OK with {best} vias")
            if best.startswith("paid"):
                sev = "warning" if sev == "info" else sev
        elif vip:
            sev = "warning" if sev == "info" else sev
            vip_paid = p.layers < v["via_in_pad_free_from_layers"]
            notes.append(f"no dog-bone via fits: needs via-in-pad ({vip[0]}, filled + capped"
                         + (", paid below 6 layers)" if vip_paid else ", free at 6+ layers)"))
        else:
            sev = "warning" if sev == "info" else sev
            notes.append("neither dog-bone nor via-in-pad fits: needs HDI microvias (paid, 4+ layers)")
        guide = c["bga"]["pitch_guide"]
        near = min(guide, key=lambda k: abs(float(k) - pitch))
        notes.append(f"JLC guide for {near} mm pitch: {guide[near]}")
        add(sev, "bga", "; ".join(notes))


def format_text(findings: list[Finding], profile: Profile) -> str:
    order = {"error": 0, "warning": 1, "info": 2}
    lines = ["JLCPCB preflight", "Detected: " + "; ".join(profile.notes), ""]
    for f in sorted(findings, key=lambda f: order[f.severity]):
        lines.append(f"[{f.severity.upper():7}] {f.code:<12} {f.message}")
    n = Counter(f.severity for f in findings)
    lines += ["", f"{n.get('error', 0)} errors, {n.get('warning', 0)} warnings"]
    return "\n".join(lines)


def to_dicts(findings: list[Finding]) -> list[dict]:
    return [asdict(f) for f in findings]
