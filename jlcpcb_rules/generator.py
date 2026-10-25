"""Generate KiCad 10 custom design rules (.kicad_dru) from JLCPCB capabilities.

Severity model
  error    below JLCPCB's absolute process limit (or a hard process restriction)
  warning  buildable, but a paid option, a recommendation, or legibility
           (tier 'standard' turns paid items into errors, tier 'advanced' drops them)

KiCad rule precedence: for one constraint type, the LAST matching rule wins and
no further rules are checked. Rules below are therefore ordered generic ->
specific inside each constraint type. Assertions are the exception: every
matching assertion is evaluated, which is why cost/recommendation checks are
written as assertions next to the hard-limit constraints.
"""

from __future__ import annotations

import datetime as _dt
from dataclasses import dataclass, field

from .profile import Profile, load_capabilities

BEGIN_MARK = "# >>> JLCPCB RULES BEGIN (generated, do not edit inside this block)"
END_MARK = "# <<< JLCPCB RULES END"
PROFILE_PREFIX = "# jlc-profile: "
OMIT = "omit"
USER_HEADER = "# Your own rules go below this line. They are kept when the JLC block is regenerated\n# and, being later in the file, they override the JLC rules for the items they match.\n"


@dataclass
class Rule:
    name: str
    constraints: list[str]
    condition: str | None = None
    layer: str | None = None
    severity: str | None = None
    comment: str | None = None

    def render(self) -> str:
        lines = []
        if self.comment:
            lines += [f"# {c}" for c in self.comment.splitlines()]
        lines.append(f'(rule "{self.name}"')
        if self.severity:
            lines.append(f"    (severity {self.severity})")
        if self.layer:
            lines.append(f'    (layer "{self.layer}")' if any(ch in self.layer for ch in "*?")
                         else f"    (layer {self.layer})")
        if self.condition:
            lines.append(f'    (condition "{self.condition}")')
        for c in self.constraints:
            lines.append(f"    (constraint {c})")
        lines[-1] += ")"
        return "\n".join(lines)


@dataclass
class Section:
    title: str
    rules: list[Rule] = field(default_factory=list)


def mm(v: float) -> str:
    return f"{round(v, 4):g}mm"


def _oz_key(oz: float) -> str:
    return f"{oz:g}"


class _Builder:
    def __init__(self, profile: Profile, caps: dict):
        self.p = profile
        self.c = caps
        self.sections: list[Section] = []
        self.board_setup: dict[str, float] = {}

    # --- severity helpers -------------------------------------------------
    def paid(self, key: str | None = None) -> str:
        """Severity for a buildable-but-paid item; OMIT = do not emit the rule."""
        if key and key in self.p.accept:
            return OMIT
        return {"tiered": "warning", "standard": "error", "advanced": OMIT}[self.p.tier]

    def advisory(self) -> str:
        """Severity for JLC 'recommended/preferred' values."""
        return OMIT if self.p.tier == "advanced" else "warning"

    def section(self, title: str) -> Section:
        s = Section(title)
        self.sections.append(s)
        return s

    @staticmethod
    def add(sec: Section, rule: Rule) -> None:
        if rule.severity == OMIT:
            return
        sec.rules.append(rule)

    # --- derived values ---------------------------------------------------
    def outer_trace(self) -> float:
        ts = self.c["trace_space"]
        table = ts["multilayer_outer"] if self.p.multilayer else ts["two_layer_outer"]
        key = _oz_key(self.p.outer_oz)
        if key not in table:
            raise ValueError(f"JLC does not offer {self.p.outer_oz:g} oz outer copper on a "
                             f"{self.p.layers}-layer board (offered: {', '.join(table)} oz)")
        return table[key]

    def inner_trace(self) -> float:
        table = self.c["trace_space"]["multilayer_inner"]
        key = _oz_key(self.p.inner_oz)
        if key not in table:
            raise ValueError(f"JLC does not offer {self.p.inner_oz:g} oz inner copper "
                             f"(offered: {', '.join(table)} oz)")
        return table[key]

    def via_hole_abs(self) -> float:
        v = self.c["via"]
        return v["micro_hole_min"] if self.p.micro_drill_ok else v["hole_min"]

    def via_dia_abs(self) -> float:
        v = self.c["via"]
        return v["micro_diameter_min"] if self.p.micro_drill_ok else v["diameter_min"]

    def pth_ring_min(self) -> tuple[float, float]:
        a = self.c["annular_ring_pth"]
        if self.p.outer_oz >= 2 or (self.p.multilayer and self.p.inner_oz >= 2):
            return a["two_oz_min"], a["two_oz_min"]
        if self.p.multilayer:
            return a["multilayer_1oz_min"], a["multilayer_1oz_recommended"]
        return a["two_layer_1oz_min"], a["two_layer_1oz_recommended"]

    def mask_dam(self) -> float:
        sm = self.c["soldermask"]
        if self.p.outer_oz >= 2:
            return sm["dam_2oz"]
        if self.p.mask_color in ("black", "white"):
            return sm["dam_1oz_black_white"]
        return sm["dam_1oz"]

    def not_filled(self, x: str) -> str:
        fill = f"{x}.Filling == 'Not filled'" + ("" if self.p.via_fill_default
                                                 else f" || {x}.Filling == 'From board stackup'")
        cap = f"{x}.Capping == 'Not capped'" + ("" if self.p.via_cap_default
                                               else f" || {x}.Capping == 'From board stackup'")
        return f"({fill} || {cap})"

    def is_filled(self, x: str) -> str:
        if self.p.via_fill_default:
            return f"{x}.Filling != 'Not filled'"
        return f"{x}.Filling == 'Filled'"

    # --- rule sections ----------------------------------------------------
    def build(self) -> list[Section]:
        if self.p.layers == 1:
            self._single_layer()
        self._tracks_and_clearance()
        self._holes()
        self._vias()
        self._annular()
        self._hole_spacing()
        self._edges()
        self._pads()
        self._soldermask()
        self._silkscreen()
        return self.sections

    def _single_layer(self):
        s = self.section("1-layer board: no plated holes")
        self.add(s, Rule("JLC 1L: no vias on a 1-layer board", ["disallow via"],
                         severity="error"))
        self.add(s, Rule("JLC 1L: holes are not plated on a 1-layer board",
                         ["assertion \"!A.isPlated()\""],
                         condition="A.Type == 'Pad'", severity="error",
                         comment="Set these pads to NPTH (or use a 2-layer board)."))

    def _tracks_and_clearance(self):
        s = self.section("Copper: track width and clearance (different nets)")
        ow = self.outer_trace()
        cu = f"{self.p.outer_oz:g}oz {'multilayer' if self.p.multilayer else '1-2 layer'}"
        self.add(s, Rule(f"JLC track: outer min width {mm(ow)} ({cu})",
                         [f"track_width (min {mm(ow)})"], layer="outer", severity="error"))
        if self.p.multilayer:
            iw = self.inner_trace()
            self.add(s, Rule(f"JLC track: inner min width {mm(iw)} ({self.p.inner_oz:g}oz)",
                             [f"track_width (min {mm(iw)})"], layer="inner", severity="error"))
        diff = "A.NetName != B.NetName"
        self.add(s, Rule(f"JLC clearance: outer copper min {mm(ow)} ({cu})",
                         [f"physical_clearance (min {mm(ow)})"], layer="outer",
                         condition=diff, severity="error",
                         comment="physical_clearance (not clearance) so your netclass clearances still apply."))
        if self.p.multilayer:
            self.add(s, Rule(f"JLC clearance: inner copper min {mm(iw)} ({self.p.inner_oz:g}oz)",
                             [f"physical_clearance (min {mm(iw)})"], layer="inner",
                             condition=diff, severity="error"))
        smd = max(self.c["clearance"]["smd_pad_to_pad_diff_net"], ow)
        self.add(s, Rule(f"JLC clearance: SMD pad to SMD pad min {mm(smd)}",
                         [f"physical_clearance (min {mm(smd)})"], layer="outer",
                         condition=f"{diff} && A.Pad_Type == 'SMD' && B.Pad_Type == 'SMD'",
                         severity="error"))
        self.board_setup["Minimum clearance"] = min(ow, iw) if self.p.multilayer else ow
        self.board_setup["Minimum track width"] = min(ow, iw) if self.p.multilayer else ow

    def _holes(self):
        d = self.c["drill"]
        s = self.section("Drilling: hole and slot sizes")
        drill_min = d["min_1L"] if self.p.layers == 1 else self.via_hole_abs()
        if self.p.layers >= 2:
            self.add(s, Rule(f"JLC drill: plated hole min {mm(drill_min)}",
                             [f"hole_size (min {mm(drill_min)})"],
                             condition="A.Type == 'Pad' && A.isPlated() && A.Hole_Size_X == A.Hole_Size_Y",
                             severity="error"))
            slot = d["plated_slot_min_multilayer"] if self.p.multilayer else d["plated_slot_min_2L"]
            self.add(s, Rule(f"JLC drill: plated slot min width {mm(slot)}",
                             [f"hole_size (min {mm(slot)})"],
                             condition="A.Type == 'Pad' && A.isPlated() && A.Hole_Size_X != A.Hole_Size_Y",
                             severity="error"))
            if slot < d["plated_slot_min_conflict"]:
                self.add(s, Rule(
                    f"JLC check: plated slot under {mm(d['plated_slot_min_conflict'])} "
                    "(JLC page gives 0.35 and 0.5 mm, confirm before ordering)",
                    [f"assertion \"A.Hole_Size_X >= {mm(d['plated_slot_min_conflict'])} && "
                     f"A.Hole_Size_Y >= {mm(d['plated_slot_min_conflict'])}\""],
                    condition="A.Type == 'Pad' && A.isPlated() && A.Hole_Size_X != A.Hole_Size_Y",
                    severity=self.advisory()))
        npth_min = d["min_1L"] if self.p.layers == 1 else d["npth_min"]
        self.add(s, Rule(f"JLC drill: NPTH hole min {mm(npth_min)}",
                         [f"hole_size (min {mm(npth_min)})"],
                         condition="A.Pad_Type == 'NPTH, mechanical' && A.Hole_Size_X == A.Hole_Size_Y",
                         severity="error"))
        self.add(s, Rule(f"JLC drill: NPTH slot min width {mm(d['npth_slot_min'])}",
                         [f"hole_size (min {mm(d['npth_slot_min'])})"],
                         condition="A.Pad_Type == 'NPTH, mechanical' && A.Hole_Size_X != A.Hole_Size_Y",
                         severity="error"))
        ca = self.c["castellated"]
        self.add(s, Rule(f"JLC drill: castellated hole min {mm(ca['hole_min'])}",
                         [f"hole_size (min {mm(ca['hole_min'])})"],
                         condition="A.Fabrication_Property == 'Castellated pad'",
                         severity="error"))
        self.add(s, Rule(f"JLC drill: press-fit hole {mm(d['press_fit_min'])} to {mm(d['press_fit_max'])}",
                         [f"hole_size (min {mm(d['press_fit_min'])}) (max {mm(d['press_fit_max'])})"],
                         condition="A.Fabrication_Property == 'Press-fit pad'",
                         severity="error"))
        if not (self.p.multilayer and self.p.finish == "ENIG"):
            self.add(s, Rule("JLC press-fit: needs a multilayer ENIG board",
                             ["assertion \"A.Fabrication_Property != 'Press-fit pad'\""],
                             condition="A.Type == 'Pad'", severity="error"))
        ratio = d["slot_length_to_width"]
        self.add(s, Rule(f"JLC drill: slot length at least {ratio:g}x its width",
                         [f"assertion \"A.Hole_Size_X >= {ratio:g} * A.Hole_Size_Y || "
                          f"A.Hole_Size_Y >= {ratio:g} * A.Hole_Size_X\""],
                         condition="A.Type == 'Pad' && A.Hole_Size_X != A.Hole_Size_Y",
                         severity="error"))
        self.add(s, Rule(f"JLC note: hole above {mm(d['max_drilled'])} is CNC-routed (+-0.2 mm)",
                         [f"assertion \"A.Hole_Size_X <= {mm(d['max_drilled'])}\""],
                         condition="A.Type == 'Pad' && A.Hole_Size_X == A.Hole_Size_Y",
                         severity="warning"))
        if self.p.layers >= 2:
            free = self.c["via"]["free_hole_min"]
            self.add(s, Rule(f"JLC paid: plated hole under {mm(free)}",
                             [f"assertion \"A.Hole_Size_X >= {mm(free)} && A.Hole_Size_Y >= {mm(free)}\""],
                             condition="A.Type == 'Pad' && A.isPlated()",
                             severity=self.paid("small_via")))
        self.board_setup["Minimum through hole"] = drill_min

    def _vias(self):
        if self.p.layers == 1:
            return
        v, h, bd = self.c["via"], self.c["hdi"], self.c["backdrill"]
        s = self.section("Vias: size, cost tiers, HDI, via-in-pad, backdrill")
        hole, dia = self.via_hole_abs(), self.via_dia_abs()
        why = " (board <= 1.0 mm, ENIG/OSP)" if self.p.micro_drill_ok else ""
        self.add(s, Rule(f"JLC via: hole min {mm(hole)}{why}",
                         [f"hole_size (min {mm(hole)})"],
                         condition="A.Type == 'Via'", severity="error",
                         comment=None if self.p.micro_drill_ok else
                         "0.10 mm via holes need a board <= 1.0 mm thick with ENIG or OSP."))
        self.add(s, Rule(f"JLC via: diameter min {mm(dia)}",
                         [f"via_diameter (min {mm(dia)})"],
                         condition="!A.isMicroVia() && !A.isBlindVia()", severity="error"))

        hdi_ok = self.p.layers >= h["min_layers"]
        if hdi_ok:
            self.add(s, Rule(f"JLC HDI: laser blind/micro via hole {mm(h['laser_blind_hole_min'])} "
                             f"to {mm(h['laser_blind_hole_max'])}",
                             [f"hole_size (min {mm(h['laser_blind_hole_min'])}) "
                              f"(max {mm(h['laser_blind_hole_max'])})"],
                             condition="A.isMicroVia() || A.isBlindVia()", severity="error"))
            self.add(s, Rule(f"JLC HDI: buried via hole {mm(h['buried_hole_extreme_min'])} "
                             f"to {mm(h['buried_hole_max'])}",
                             [f"hole_size (min {mm(h['buried_hole_extreme_min'])}) "
                              f"(max {mm(h['buried_hole_max'])})"],
                             condition="A.isBuriedVia()", severity="error"))
            self.add(s, Rule(f"JLC paid: buried via hole under {mm(h['buried_hole_min'])} "
                             "(extreme, dielectric <= 1.0 mm)",
                             [f"assertion \"A.Hole >= {mm(h['buried_hole_min'])}\""],
                             condition="A.Type == 'Via' && A.isBuriedVia()",
                             severity=self.paid("hdi")))
            self.add(s, Rule("JLC paid: HDI blind/buried/micro via",
                             ["disallow blind_via buried_via micro_via"],
                             severity=self.paid("hdi")))
        else:
            self.add(s, Rule(f"JLC HDI: blind/buried/micro vias need {h['min_layers']}+ layers",
                             ["disallow blind_via buried_via micro_via"], severity="error"))

        through = "A.Type == 'Via' && A.Via_Type == 'Through'"
        self.add(s, Rule(f"JLC paid: via hole under {mm(v['free_hole_min'])}",
                         [f"assertion \"A.Hole >= {mm(v['free_hole_min'])}\""],
                         condition=through, severity=self.paid("small_via")))
        self.add(s, Rule(f"JLC paid: via hole under {mm(v['free_small_hole_below'])} needs "
                         f"diameter >= {mm(v['free_small_hole_diameter_min'])}",
                         [f"assertion \"A.Hole >= {mm(v['free_small_hole_below'])} || "
                          f"A.Diameter >= {mm(v['free_small_hole_diameter_min'])}\""],
                         condition=through, severity=self.paid("small_via")))
        self.add(s, Rule(f"JLC recommended: via diameter >= hole + {mm(v['diameter_over_hole_preferred'])}",
                         [f"assertion \"A.Diameter - A.Hole >= {mm(v['diameter_over_hole_preferred'])}\""],
                         condition=through, severity=self.advisory()))

        # Drill tools come in fixed steps; an off-step hole is drilled at the next step up,
        # which eats into the annular ring.
        ds = self.c["drill_step"]
        step, top, eps = ds["step"], ds["via_grid_check_max"], 0.0005
        steps = [round(v["micro_hole_min"] + i * step, 4)
                 for i in range(int(round((top - v["micro_hole_min"]) / step)) + 1)]
        on_grid = " || ".join(f"(A.Hole > {mm(x - eps)} && A.Hole < {mm(x + eps)})" for x in steps)
        ring2 = v["diameter_over_hole_min"]
        shrunk = " || ".join(
            f"(A.Hole > {mm(a + eps)} && A.Hole < {mm(b - eps)} && A.Diameter - {mm(b)} < {mm(ring2)})"
            for a, b in zip(steps, steps[1:]))
        grid_cond = f"A.Type == 'Via' && A.Hole <= {mm(top)}"
        self.add(s, Rule(f"JLC drill: via hole off the {mm(step)} drill step (drilled at the next step up)",
                         [f"assertion \"{on_grid}\""], condition=grid_cond, severity="warning",
                         comment=f"Drill tools come in {mm(step)} steps; e.g. 0.16 mm is drilled at 0.2 mm."))
        self.add(s, Rule(f"JLC drill: via ring below {mm(ring2 / 2)} once the hole is rounded up to the next step",
                         [f"assertion \"!({shrunk})\""], condition=grid_cond, severity="error"))

        # Via-in-pad: JLC fills with epoxy/copper and caps it (paid below 6 layers).
        vip_paid = self.p.layers < v["via_in_pad_free_from_layers"]
        vip_sev = "error" if (self.p.tier == "standard" and vip_paid) else "warning"
        cond = (f"A.NetName == B.NetName && ("
                f"(A.Type == 'Via' && B.Pad_Type == 'SMD' && {self.not_filled('A')}) || "
                f"(B.Type == 'Via' && A.Pad_Type == 'SMD' && {self.not_filled('B')}))")
        self.add(s, Rule("JLC via-in-pad: via in SMD pad must be Filled + Capped "
                         "(JLC VIP process" + (", paid below 6 layers)" if vip_paid else ", default 6+ layers)"),
                         ["physical_hole_clearance (min 0.05mm)"],
                         layer="*.Cu", condition=cond, severity=vip_sev,
                         comment="Set the via's Filling = Filled and Capping = Capped (via properties),\n"
                                 "or move it out of the pad. Unfilled vias in pads wick solder."))
        self.add(s, Rule(f"JLC via-in-pad: filled via hole {mm(v['via_in_pad_hole_min'])} "
                         f"to {mm(v['via_in_pad_hole_max'])}",
                         [f"assertion \"A.Hole >= {mm(v['via_in_pad_hole_min'])} && "
                          f"A.Hole <= {mm(v['via_in_pad_hole_max'])}\""],
                         condition=f"A.Type == 'Via' && A.Via_Type == 'Through' && {self.is_filled('A')}",
                         severity="error"))
        self.add(s, Rule(f"JLC mask plug: plugged via hole max {mm(v['plugged_hole_max'])}",
                         [f"assertion \"A.Hole <= {mm(v['plugged_hole_max'])}\""],
                         condition="A.Type == 'Via' && (A.Front_plugging == 'Plugged' || "
                                   "A.Back_plugging == 'Plugged')",
                         severity="error"))

        # Backdrill
        bd_cond = "(A.Type == 'Via' || A.Type == 'Pad') && A.Backdrill_Mode != 'No backdrill'"
        if self.p.layers < bd["min_layers"] or self.p.thickness < bd["board_thickness_min"]:
            self.add(s, Rule(f"JLC backdrill: needs {bd['min_layers']}+ layers and "
                             f">= {mm(bd['board_thickness_min'])} board",
                             ["assertion \"A.Backdrill_Mode == 'No backdrill'\""],
                             condition="A.Type == 'Via' || A.Type == 'Pad'", severity="error"))
        else:
            self.add(s, Rule(f"JLC backdrill: through hole {mm(bd['hole_min'])} to {mm(bd['hole_max'])}",
                             [f"assertion \"A.Hole >= {mm(bd['hole_min'])} && "
                              f"A.Hole <= {mm(bd['hole_max'])}\""],
                             condition=bd_cond.replace("(A.Type == 'Via' || A.Type == 'Pad')",
                                                       "A.Type == 'Via'"),
                             severity="error"))
            self.add(s, Rule("JLC paid: backdrilled hole",
                             ["assertion \"A.Backdrill_Mode == 'No backdrill'\""],
                             condition="A.Type == 'Via' || A.Type == 'Pad'",
                             severity=self.paid("backdrill")))
        self.board_setup["Minimum via diameter"] = dia
        self.board_setup["Minimum via hole"] = hole
        if hdi_ok:
            self.board_setup["Minimum uVia diameter"] = 0.2
            self.board_setup["Minimum uVia hole"] = h["laser_blind_hole_min"]

    def _annular(self):
        s = self.section("Annular rings")
        if self.p.layers >= 2:
            ring = self.c["via"]["diameter_over_hole_min"] / 2
            self.add(s, Rule(f"JLC via: annular ring min {mm(ring)} (diameter >= hole + "
                             f"{mm(self.c['via']['diameter_over_hole_min'])})",
                             [f"annular_width (min {mm(ring)})"],
                             condition="A.Type == 'Via'", severity="error"))
            self.board_setup["Minimum annular width"] = ring
            lo, rec = self.pth_ring_min()
            self.add(s, Rule(f"JLC PTH: annular ring min {mm(lo)}",
                             [f"annular_width (min {mm(lo)})"],
                             condition="A.Type == 'Pad' && A.isPlated()", severity="error"))
            if rec > lo:
                self.add(s, Rule(f"JLC recommended: PTH annular ring >= {mm(rec)}",
                                 [f"assertion \"A.Size_X - A.Hole_Size_X >= {mm(2 * rec)} && "
                                  f"A.Size_Y - A.Hole_Size_Y >= {mm(2 * rec)}\""],
                                 condition="A.Type == 'Pad' && A.isPlated() && "
                                           "A.Pad_Shape != 'Custom' && A.Hole_Size_X == A.Hole_Size_Y",
                                 severity=self.advisory(),
                                 comment="Approximation: assumes the hole is centred in the pad."))

    def _hole_spacing(self):
        d, cl = self.c["drill"], self.c["clearance"]
        s = self.section("Hole spacing and hole-to-copper clearance")
        self.add(s, Rule(f"JLC drill: hole to hole min {mm(d['via_hole_to_hole'])}",
                         [f"hole_to_hole (min {mm(d['via_hole_to_hole'])})"], severity="error"))
        self.add(s, Rule(f"JLC drill: pad hole to pad hole min {mm(d['pad_hole_to_hole'])}",
                         [f"hole_to_hole (min {mm(d['pad_hole_to_hole'])})"],
                         condition="A.Type == 'Pad' && B.Type == 'Pad'", severity="error"))
        ca = self.c["castellated"]
        self.add(s, Rule(f"JLC castellated: hole to hole min {mm(ca['hole_to_hole_min'])}",
                         [f"hole_to_hole (min {mm(ca['hole_to_hole_min'])})"],
                         condition="A.Fabrication_Property == 'Castellated pad' && "
                                   "B.Fabrication_Property == 'Castellated pad'",
                         severity="error"))
        self.board_setup["Minimum hole to hole"] = d["via_hole_to_hole"]

        if self.p.layers >= 2:
            self.add(s, Rule(f"JLC clearance: via hole to copper min {mm(cl['via_hole_to_track'])}",
                             [f"hole_clearance (min {mm(cl['via_hole_to_track'])})"],
                             condition="A.Type == 'Via' || B.Type == 'Via'", severity="error"))
            pth = "(A.Type == 'Pad' && A.isPlated()) || (B.Type == 'Pad' && B.isPlated())"
            self.add(s, Rule(f"JLC clearance: PTH hole to copper min {mm(cl['pth_to_track'])} (outer)",
                             [f"hole_clearance (min {mm(cl['pth_to_track'])})"],
                             layer="outer", condition=pth, severity="error"))
            if self.p.multilayer:
                self.add(s, Rule(f"JLC clearance: inner via hole to copper min "
                                 f"{mm(cl['inner_via_hole_to_copper'])}",
                                 [f"hole_clearance (min {mm(cl['inner_via_hole_to_copper'])})"],
                                 layer="inner", condition="A.Type == 'Via' || B.Type == 'Via'",
                                 severity="error"))
                self.add(s, Rule(f"JLC clearance: inner PTH hole to copper min "
                                 f"{mm(cl['inner_pth_hole_to_copper'])}",
                                 [f"hole_clearance (min {mm(cl['inner_pth_hole_to_copper'])})"],
                                 layer="inner", condition=pth, severity="error"))
            self.board_setup["Copper to hole clearance"] = cl["via_hole_to_track"]
        self.add(s, Rule(f"JLC clearance: NPTH to copper min {mm(cl['npth_to_track'])}",
                         [f"physical_hole_clearance (min {mm(cl['npth_to_track'])})"],
                         layer="*.Cu",
                         condition="A.Pad_Type == 'NPTH, mechanical' || B.Pad_Type == 'NPTH, mechanical'",
                         severity="error"))

    def _edges(self):
        cl = self.c["clearance"]
        s = self.section("Board edge")
        self.add(s, Rule(f"JLC edge: copper to routed edge min {mm(cl['copper_to_routed_edge'])}",
                         [f"edge_clearance (min {mm(cl['copper_to_routed_edge'])})"],
                         layer="*.Cu", severity="error",
                         comment=f"V-cut edges need {mm(cl['copper_to_vcut'])}: KiCad cannot tell a V-cut\n"
                                 "edge from a routed one, so check panel V-cut lines by hand."))
        edge_ok = ("A.Fabrication_Property == 'Castellated pad' || A.Pad_Type == 'Edge connector' || "
                   "B.Fabrication_Property == 'Castellated pad' || B.Pad_Type == 'Edge connector'")
        self.add(s, Rule("JLC edge: castellated and edge-connector pads may reach the edge",
                         ["edge_clearance (min -1mm)"], condition=edge_ok))
        self.board_setup["Copper to edge clearance"] = cl["copper_to_routed_edge"]

    def _pads(self):
        b = self.c["bga"]
        s = self.section("Pads: BGA and small SMD pads")
        shaped = "A.Pad_Shape != 'Custom'"
        self.add(s, Rule(f"JLC BGA: pad min {mm(b['pad_min'])}",
                         [f"assertion \"A.Size_X >= {mm(b['pad_min'])} && A.Size_Y >= {mm(b['pad_min'])}\""],
                         condition=f"A.Type == 'Pad' && A.Fabrication_Property == 'BGA pad' && {shaped}",
                         severity="error"))
        if self.p.finish != "ENIG":
            self.add(s, Rule(f"JLC BGA: pads under {mm(b['pad_enig_below'])} need ENIG",
                             [f"assertion \"A.Size_X >= {mm(b['pad_enig_below'])} && "
                              f"A.Size_Y >= {mm(b['pad_enig_below'])}\""],
                             condition=f"A.Type == 'Pad' && A.Fabrication_Property == 'BGA pad' && {shaped}",
                             severity="error",
                             comment=f"Finish is {self.p.finish or 'unknown'}; regenerate with ENIG to allow "
                                     f"{mm(b['pad_min'])}-{mm(b['pad_enig_below'])} BGA pads."))
        smd = self.c["smd_pad_min"]
        self.add(s, Rule(f"JLC pad: SMD pad smaller than {mm(smd)} x {mm(smd)}",
                         [f"assertion \"A.Size_X >= {mm(smd)} || A.Size_Y >= {mm(smd)}\""],
                         condition=f"A.Type == 'Pad' && A.Pad_Type == 'SMD' && "
                                   f"A.Fabrication_Property != 'BGA pad' && {shaped}",
                         severity="warning",
                         comment="Flags pads where BOTH sides are under the JLC minimum SMD pad."))

    def _soldermask(self):
        s = self.section("Solder mask")
        dam = self.mask_dam()
        what = (f"{self.p.outer_oz:g}oz" if self.p.outer_oz >= 2
                else f"{self.p.outer_oz:g}oz {self.p.mask_color}")
        self.add(s, Rule(f"JLC mask: dam (web) min {mm(dam)} ({what}), narrower dams are removed",
                         [f"solder_mask_sliver (min {mm(dam)})"], severity="warning",
                         comment="Below this JLC opens the mask between the pads (no dam = bridge risk)."))
        self.board_setup["Solder mask minimum web width"] = dam

    def _silkscreen(self):
        sk = self.c["silkscreen"]
        s = self.section("Silkscreen (legibility; JLC prints but may lose detail)")
        silk = "?.Silkscreen"
        self.add(s, Rule(f"JLC silk: text height min {mm(sk['text_height_min'])}",
                         [f"text_height (min {mm(sk['text_height_min'])})"],
                         layer=silk, severity="warning"))
        self.add(s, Rule(f"JLC silk: text stroke min {mm(sk['line_width_min'])}",
                         [f"text_thickness (min {mm(sk['line_width_min'])})"],
                         layer=silk, severity="warning"))
        r = sk["text_width_to_height"]
        # Assertions ignore the (layer) clause in KiCad 10, so the layer test is in the condition.
        on_silk = "(A.Layer == 'F.Silkscreen' || A.Layer == 'B.Silkscreen')"
        self.add(s, Rule(f"JLC silk: text stroke at least 1/{r} of text height",
                         [f"assertion \"A.Auto_Thickness || A.Thickness * {r} >= A.Height\""],
                         condition=f"(A.Type == 'Text' || A.Type == 'Text Box') && {on_silk}",
                         severity="warning"))
        self.add(s, Rule(f"JLC silk: line width min {mm(sk['line_width_min'])}",
                         [f"assertion \"A.Line_Width >= {mm(sk['line_width_min'])} || A.Line_Width <= 0mm\""],
                         condition=f"A.Type == 'Graphic' && {on_silk}", severity="warning",
                         comment="KiCad library footprints use 0.12 mm silk lines; expect hits here."))
        self.add(s, Rule(f"JLC silk: silkscreen to pad min {mm(sk['pad_to_silk'])}",
                         [f"silk_clearance (min {mm(sk['pad_to_silk'])})"],
                         condition="A.Type == 'Pad' || B.Type == 'Pad'", severity="warning"))
        self.board_setup["Minimum text height"] = sk["text_height_min"]
        self.board_setup["Minimum text thickness"] = sk["line_width_min"]


def build_sections(profile: Profile, caps: dict | None = None):
    caps = caps or load_capabilities()
    problems = profile.validate()
    if profile.layers not in caps["layers"]["allowed"]:
        problems.append(f"JLC builds {caps['layers']['allowed']} layers, not {profile.layers}")
    if problems:
        raise ValueError("; ".join(problems))
    b = _Builder(profile, caps)
    return b.build(), b.board_setup


def render(profile: Profile, caps: dict | None = None, today: str | None = None) -> str:
    """Return the JLC block (BEGIN..END) including the (version 1) clause."""
    caps = caps or load_capabilities()
    sections, setup = build_sections(profile, caps)
    today = today or _dt.date.today().isoformat()
    p = profile
    out = [BEGIN_MARK, "(version 1)", ""]
    head = [
        "JLCPCB design rules for KiCad 10",
        f"Profile : {p.layers} layers, outer {p.outer_oz:g} oz"
        + (f", inner {p.inner_oz:g} oz" if p.multilayer else "")
        + f", {p.thickness:g} mm, finish {p.finish or 'unknown'}, {p.mask_color} mask",
        f"Tier    : {p.tier}"
        + {"tiered": " (error = beyond JLC limits, warning = paid option / recommendation)",
           "standard": " (anything needing a paid option is an error)",
           "advanced": " (absolute process limits only, no cost warnings)"}[p.tier],
        f"Accepted paid options: {', '.join(p.accept) if p.accept else 'none'}",
        f"Source  : {caps['source']} (snapshot {caps['snapshot']})",
        f"Generated {today} by jlcpcb_rules. Regenerate after changing the stackup.",
        "",
        "Set Board Setup > Design Rules > Constraints to at least these values so the",
        "router uses them:",
    ]
    head += [f"  {k:<32} {v:g} mm" for k, v in setup.items()]
    head += ["", "These rules replace KiCad's Board Setup minimums for the items they match.",
             "Netclass clearances are kept: JLC copper spacing is a separate physical_clearance",
             "check. Put stricter rules of your own BELOW the END marker; later rules win."]
    head += ["", "Not checkable with KiCad rules (see 'python3 -m jlcpcb_rules preflight'):",
             "  board size, thickness, layer count, finish/copper options, V-cut copper",
             "  clearance, same-net track spacing, impedance, gold fingers."]
    out += [f"# {line}".rstrip() for line in head]
    out.append(PROFILE_PREFIX + p.to_json())
    for sec in sections:
        if not sec.rules:
            continue
        out += ["", "#" + "-" * 78, f"# {sec.title}", "#" + "-" * 78]
        for rule in sec.rules:
            out += ["", rule.render()]
    out += ["", END_MARK, ""]
    return "\n".join(out)


def render_file(profile: Profile, user_rules: str = "", **kw) -> str:
    return render(profile, **kw) + "\n" + USER_HEADER + (user_rules.strip() + "\n" if user_rules.strip() else "")


def split_existing(text: str) -> tuple[bool, str]:
    """Return (is_generated, user_part) for an existing .kicad_dru file.

    Generated files keep everything after END_MARK. A foreign file is returned
    whole with its (version ...) clause removed so it can be appended.
    """
    if BEGIN_MARK in text and END_MARK in text:
        user = text.split(END_MARK, 1)[1]
        user = user.replace(USER_HEADER, "", 1)
        return True, user
    import re
    return False, re.sub(r"^\s*\(version\s+\d+\)\s*$", "", text, count=1, flags=re.M)


def read_profile_line(text: str) -> Profile | None:
    for line in text.splitlines():
        if line.startswith(PROFILE_PREFIX):
            return Profile.from_json(line[len(PROFILE_PREFIX):])
    return None
