"""Text, Markdown, JSON and SVG output for the reference-plane check."""

from __future__ import annotations

import html
import json
from pathlib import Path

from .check import Result
from .pcb import Board

COLORS = {"no_reference": "#d62728", "reference_edge": "#c2185b", "pwr_reference": "#ff7f0e",
          "reference_margin": "#e6b800"}


def counts(res: Result) -> dict[str, int]:
    c = {"error": 0, "warning": 0, "info": 0}
    for f in res.findings:
        c[f.severity] = c.get(f.severity, 0) + 1
    return c


def to_json(res: Result, board: Board, setup: dict) -> str:
    return json.dumps({
        "tool": "refplane",
        "board": str(board.path),
        "setup": setup,
        "summary": counts(res),
        "layers": [{"layer": p.layer, "role": p.role,
                    "references": [{"layer": r, "h_mm": h, "margin_mm": round(m, 4)} for r, h, m in p.refs]}
                   for p in res.plans],
        "nets": res.net_stats,
        "notes": res.notes,
        "islands": res.islands,
        "findings": [f.to_dict() for f in res.findings],
    }, indent=2)


def to_text(res: Result, limit: int = 25) -> str:
    c = counts(res)
    lines = [f"Reference-plane check: {c['error']} error(s), {c['warning']} warning(s), {c['info']} info"]
    for p in res.plans:
        refs = ", ".join(f"{r} (H {h:.3f}, margin {m:.2f})" for r, h, m in p.refs) or "NONE"
        lines.append(f"  {p.layer:8} [{p.role}] references: {refs}")
    for n in res.notes:
        lines.append(f"  note: {n}")
    for f in res.findings[:limit]:
        lines.append(f"{f.severity.upper():7} {f.kind}: {f.message}")
    if len(res.findings) > limit:
        lines.append(f"... {len(res.findings) - limit} more in the report")
    return "\n".join(lines)


def to_markdown(res: Result, board: Board, setup: dict) -> str:
    c = counts(res)
    out = [f"# Reference-plane check: {board.path.name}", "",
           f"**{c['error']} error(s), {c['warning']} warning(s), {c['info']} info.** "
           "Coordinates are KiCad board coordinates in mm (Y down).", "",
           "## Setup", "",
           f"- Layer roles: `{setup['roles']}`",
           f"- GND nets: {', '.join(setup['gnd_nets']) or '-'}",
           f"- PWR nets (warning-level reference): {', '.join(setup['pwr_nets']) or '-'}",
           f"- Net classes checked: {', '.join(setup['classes']) or '-'} ({setup['net_count']} nets)",
           f"- Margin: {setup['margin_h']:g} x H; zone fills: {setup['fills']}",
           "- Return-via profiles: " + "; ".join(
               f"{n} {r['distance_mm']:g} mm, max {r['max_share']} per return via, missing = {r['severity']}"
               for n, r in setup.get("stitch_profiles", {}).items())
           + f" (default {setup.get('stitch_default_profile', '-')})",
           f"- Return-via mode: {setup.get('stitch_mode', '-')}"
           + (f" ({', '.join(setup['high_speed'])}; every other net low speed)" if setup.get("high_speed") else ""),
           "- Class rules: " + (", ".join(f"{c} = {v}" for c, v in setup.get("stitch_class", {}).items()) or "none"),
           f"- Power rails skipped as signals: {', '.join(setup.get('skipped_power', [])) or 'none'}",
           f"- Settings: {', '.join(setup.get('settings_files', []))}", ""]
    for a in setup.get("assumptions", []):
        out.append(f"- Assumed: {a}")
    out += ["", "## Layers", "", "| Layer | Role | Reference layer(s) | H (mm) | Margin (mm) |",
            "| --- | --- | --- | --- | --- |"]
    for p in res.plans:
        if p.refs:
            out.append(f"| {p.layer} | {p.role} | {', '.join(r for r, _, _ in p.refs)} | "
                       f"{' / '.join(f'{h:.3f}' for _, h, _ in p.refs)} | "
                       f"{' / '.join(f'{m:.2f}' for _, _, m in p.refs)} |")
        else:
            out.append(f"| {p.layer} | {p.role} | **none** | - | - |")
    for n in res.notes:
        out.append(f"\nNote: {n}")
    if res.islands:
        out += ["", "## Reference copper islands", "",
                "Each reference net's fills are split into regions at necks narrower than the island `neck_mm`. "
                "Regions are joined by vias and through pads. The largest region of each layer in the main group "
                "is that layer's plane; every other region is an island.", "",
                "| Net | Regions | Islands | Main group area (mm²) | Plane per layer (mm²) |",
                "| --- | --- | --- | --- | --- |"]
        for net, st in res.islands.items():
            planes = ", ".join(f"{l} {a:g}" for l, a in st.get("planes", {}).items())
            out.append(f"| {net} | {st['regions']} | {st['islands']} | {st['main_area_mm2']:g} | {planes} |")
    out += ["", "## Nets", "", "| Net | Class | Layers | Length (mm) | No reference | Edge over void | PWR only | Margin | Layer changes OK | Return-via profile | References (mm) |",
            "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |"]
    for net, st in sorted(res.net_stats.items(), key=lambda kv: (-kv[1]["unreferenced"], kv[0])):
        out.append(f"| {net} | {st['netclass']} | {', '.join(st['layers'])} | {st['length']:.2f} | "
                   f"{st['unreferenced']:.2f} | {st.get('edge_only', 0):.2f} | {st['pwr_only']:.2f} | {st['margin']:.2f} | "
                   f"{st.get('transitions_ok', 0)}/{st.get('transitions', 0)} | {st.get('stitch_profile', '-')} | "
                   f"{', '.join(f'{k} {v:.1f}' for k, v in st.get('references', {}).items()) or '-'} |")
    for sev in ("error", "warning", "info"):
        items = [f for f in res.findings if f.severity == sev]
        if not items:
            continue
        out += ["", f"## {sev.capitalize()}s ({len(items)})", ""]
        for f in items:
            out.append(f"- `{f.kind}` {f.message}")
    return "\n".join(out) + "\n"


LEGEND = [("#d62728", "line", "no reference under the trace centre (error)"),
          ("#c2185b", "line", "trace edge over a void (warning)"),
          ("#ff7f0e", "line", "PWR reference only"),
          ("#e6b800", "line", "void or plane edge within margin"),
          ("#4a90d9", "box", "GND fill on the reference layer"),
          ("#e8a33d", "box", "PWR fill on the reference layer"),
          ("#d62728", "ring", "missing return via (ring = allowed distance) / floating island"),
          ("#ff7f0e", "ring", "net change / low-speed miss / shared via / weak island"),
          ("#555555", "via", "via (filled: checked net, open: other)"),
          ("#2e7d32", "gndvia", "GND via (green outline)")]


def to_svg(res: Result, board: Board, layer: str, ref_layers: list[str], nets: set[str],
           gnd_nets: set[str], pwr_nets: set[str]) -> str:
    if board.outline_bbox:
        x0, y0, x1, y1 = board.outline_bbox
    else:
        xs = [p[0] for t in board.tracks for p in t.points] or [0, 1]
        ys = [p[1] for t in board.tracks for p in t.points] or [0, 1]
        x0, y0, x1, y1 = min(xs), min(ys), max(xs), max(ys)
    bw, bh = x1 - x0, y1 - y0
    pad = max(1.0, min(bw, bh) * 0.04)
    fs = max(0.5, min(1.2, max(bw, bh) / 60))           # legend font, mm
    line_h = fs * 1.45
    longest = max(len(t) for _, _, t in LEGEND)
    legend_w = fs * 1.8 + longest * fs * 0.55
    title = f"{layer} over {', '.join(ref_layers) or 'nothing'}"
    top = pad + fs * 1.6 + len(LEGEND) * line_h + pad   # title + legend band above the board
    w = max(bw + 2 * pad, legend_w + 2 * pad)
    h = bh + pad + top
    vx = x0 - pad - (w - bw - 2 * pad) / 2               # centre the board under a wide legend
    scale = 1600 / max(w, h)
    parts = [f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="{vx:.3f} {y0 - top:.3f} {w:.3f} {h:.3f}" '
             f'width="{w * scale:.0f}" height="{h * scale:.0f}">',
             f'<rect x="{vx:.3f}" y="{y0 - top:.3f}" width="{w:.3f}" height="{h:.3f}" fill="#fafafa"/>',
             f'<title>{html.escape(title)}</title>']
    if board.outline_bbox:
        parts.append(f'<rect x="{x0}" y="{y0}" width="{bw}" height="{bh}" fill="none" '
                     f'stroke="#888" stroke-width="0.05"/>')
    tints = {"gnd": "#4a90d9", "pwr": "#e8a33d"}
    for rl in ref_layers:
        for z in board.zones:
            if z.net not in gnd_nets | pwr_nets:
                continue
            color = tints["gnd" if z.net in gnd_nets else "pwr"]
            for poly in z.fills.get(rl, []):
                d = "M" + " L".join(f"{x:.4f},{y:.4f}" for x, y in poly) + " Z"
                parts.append(f'<path d="{d}" fill="{color}" fill-opacity="0.18" fill-rule="evenodd" '
                             f'stroke="none"/>')
    # pads on this layer (light), tracks, vias
    for p in board.pads:
        if not p.on_layer(layer) or p.size == (0.0, 0.0):
            continue
        pw, ph = p.size
        color = "#9e9e9e" if p.net in nets else "#d0d0d0"
        tip = html.escape(f"{p.ref}.{p.number} {p.net}")
        if p.shape == "circle":
            parts.append(f'<circle cx="{p.at[0]:.4f}" cy="{p.at[1]:.4f}" r="{pw / 2:.4f}" fill="{color}" '
                         f'fill-opacity="0.7"><title>{tip}</title></circle>')
        else:
            rx = min(pw, ph) / 2 if p.shape == "oval" else min(pw, ph) * 0.15
            parts.append(f'<rect x="{-pw / 2:.4f}" y="{-ph / 2:.4f}" width="{pw:.4f}" height="{ph:.4f}" rx="{rx:.4f}" '
                         f'fill="{color}" fill-opacity="0.7" transform="translate({p.at[0]:.4f} {p.at[1]:.4f}) '
                         f'rotate({-p.angle:g})"><title>{tip}</title></rect>')
    for t in board.tracks:
        if t.layer != layer:
            continue
        color = "#333" if t.net in nets else "#bbb"
        pts = " ".join(f"{x:.4f},{y:.4f}" for x, y in t.points)
        parts.append(f'<polyline points="{pts}" fill="none" stroke="{color}" stroke-width="{max(t.width, 0.05)}" '
                     f'stroke-linecap="round" stroke-linejoin="round"><title>{html.escape(t.net)}</title></polyline>')
    for v in board.vias:
        if not board.via_spans(v, layer):
            continue
        checked = v.net in nets
        fill = "#555" if checked else "none"
        stroke = "#2e7d32" if v.net in gnd_nets else "#555"
        parts.append(f'<circle cx="{v.at[0]:.4f}" cy="{v.at[1]:.4f}" r="{v.size / 2:.4f}" fill="{fill}" '
                     f'fill-opacity="0.6" stroke="{stroke}" stroke-width="0.04"><title>via {html.escape(v.net)}'
                     f'</title></circle>')
    for f in res.findings:
        if f.via_layers:
            if layer in f.via_layers and f.start:
                color = "#d62728" if f.severity == "error" else "#ff7f0e"
                r = f.radius or 1.0
                parts.append(f'<circle cx="{f.start[0]:.4f}" cy="{f.start[1]:.4f}" r="{r:.3f}" fill="none" '
                             f'stroke="{color}" stroke-width="0.08" stroke-dasharray="0.25 0.15">'
                             f'<title>{html.escape(f.message)}</title></circle>'
                             f'<circle cx="{f.start[0]:.4f}" cy="{f.start[1]:.4f}" r="0.18" fill="{color}"/>')
            continue
        if f.layer != layer or not f.pieces:
            continue
        color = COLORS.get(f.kind, "#d62728")
        for pc in f.pieces:
            p, q = pc[0], pc[1]
            parts.append(f'<line x1="{p[0]:.4f}" y1="{p[1]:.4f}" x2="{q[0]:.4f}" y2="{q[1]:.4f}" '
                         f'stroke="{color}" stroke-width="0.35" stroke-opacity="0.85" stroke-linecap="round">'
                         f'<title>{html.escape(f.message)}</title></line>')
        if f.severity == "error":
            parts.append(f'<circle cx="{f.start[0]:.4f}" cy="{f.start[1]:.4f}" r="0.6" fill="none" '
                         f'stroke="{color}" stroke-width="0.12"/>')
    # title and legend, in their own band above the board
    tx, ty = vx + pad, y0 - top + pad + fs * 1.2
    parts.append(f'<text x="{tx:.3f}" y="{ty:.3f}" font-size="{fs * 1.25:.3f}" font-weight="bold" '
                 f'font-family="sans-serif">{html.escape(title)}</text>')
    for i, (c, kind, label) in enumerate(LEGEND):
        yy = ty + fs * 0.6 + (i + 1) * line_h
        cy = yy - fs * 0.35
        if kind == "line":
            sym = (f'<line x1="{tx:.3f}" y1="{cy:.3f}" x2="{tx + fs * 1.2:.3f}" y2="{cy:.3f}" stroke="{c}" '
                   f'stroke-width="{fs * 0.35:.3f}" stroke-linecap="round"/>')
        elif kind == "box":
            sym = (f'<rect x="{tx:.3f}" y="{cy - fs * 0.45:.3f}" width="{fs * 1.2:.3f}" height="{fs * 0.9:.3f}" '
                   f'fill="{c}" fill-opacity="0.35"/>')
        elif kind == "ring":
            sym = (f'<circle cx="{tx + fs * 0.6:.3f}" cy="{cy:.3f}" r="{fs * 0.45:.3f}" fill="none" stroke="{c}" '
                   f'stroke-width="{fs * 0.1:.3f}" stroke-dasharray="{fs * 0.2:.3f} {fs * 0.12:.3f}"/>')
        elif kind == "gndvia":
            sym = (f'<circle cx="{tx + fs * 0.6:.3f}" cy="{cy:.3f}" r="{fs * 0.35:.3f}" fill="none" stroke="{c}" '
                   f'stroke-width="{fs * 0.08:.3f}"/>')
        else:
            sym = (f'<circle cx="{tx + fs * 0.35:.3f}" cy="{cy:.3f}" r="{fs * 0.3:.3f}" fill="{c}" fill-opacity="0.6"/>'
                   f'<circle cx="{tx + fs * 1.05:.3f}" cy="{cy:.3f}" r="{fs * 0.3:.3f}" fill="none" stroke="{c}" '
                   f'stroke-width="{fs * 0.06:.3f}"/>')
        parts.append(sym + f'<text x="{tx + fs * 1.8:.3f}" y="{yy:.3f}" font-size="{fs:.3f}" '
                     f'font-family="sans-serif">{html.escape(label)}</text>')
    parts.append("</svg>")
    return "\n".join(parts) + "\n"


def write_outputs(out_dir: Path, res: Result, board: Board, setup: dict, cfg, svg: bool = True) -> list[Path]:
    out_dir.mkdir(parents=True, exist_ok=False)
    written = []
    p = out_dir / "report.json"
    p.write_text(to_json(res, board, setup), encoding="utf-8")
    written.append(p)
    p = out_dir / "report.md"
    p.write_text(to_markdown(res, board, setup), encoding="utf-8")
    written.append(p)
    if svg:
        for plan in res.plans:
            if not any(t.layer == plan.layer and t.net in cfg.nets for t in board.tracks):
                continue
            p = out_dir / f"{plan.layer.replace('.', '_')}.svg"
            p.write_text(to_svg(res, board, plan.layer, [r for r, _, _ in plan.refs], cfg.nets,
                                cfg.gnd_nets, cfg.pwr_nets), encoding="utf-8")
            written.append(p)
    return written
