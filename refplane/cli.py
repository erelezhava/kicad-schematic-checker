"""Command line: python3 -m refplane {info,check}."""

from __future__ import annotations

import argparse
import datetime as _dt
import json
import math
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from . import report
from .check import (REF_ROLES, ROLES, TRACE_ROLES, Config, format_roles, guess_ref_nets, guess_roles,
                    parse_roles, run)
from .pcb import Board, read_board
from .project import load_netclasses, wildcard_match
from .settings import (board_config_path, load_settings, parse_class_overrides, parse_list, power_nets,
                       stitch_rules)


# --------------------------------------------------------------------------- helpers

def resolve_board(path: Path) -> Path:
    if path.is_dir():
        boards = sorted(path.glob("*.kicad_pcb"))
        if len(boards) != 1:
            raise SystemExit(f"error: expected one .kicad_pcb in {path}, found {len(boards)}")
        return boards[0]
    if path.suffix == ".kicad_pro":
        path = path.with_suffix(".kicad_pcb")
    if not path.exists():
        raise SystemExit(f"error: {path} not found")
    return path


def find_kicad_cli(explicit: str | None) -> str | None:
    if explicit:
        return explicit
    found = shutil.which("kicad-cli")
    if found:
        return found
    for cand in ("/usr/bin/kicad-cli", "/usr/local/bin/kicad-cli",
                 "/Applications/KiCad/KiCad.app/Contents/MacOS/kicad-cli",
                 r"C:\Program Files\KiCad\10.0\bin\kicad-cli.exe",
                 r"C:\Program Files\KiCad\9.0\bin\kicad-cli.exe"):
        if Path(cand).exists():
            return cand
    return None


def refill_to_copy(board_path: Path, kicad_cli: str, tmp: Path) -> Path:
    """Refill zones on a temporary copy. The user's board is never written."""
    for suffix in (".kicad_pcb", ".kicad_pro", ".kicad_dru", ".kicad_prl"):
        src = board_path.with_suffix(suffix)
        if src.exists():
            shutil.copy2(src, tmp / src.name)
    copy = tmp / board_path.name
    before = copy.stat().st_mtime_ns
    cmd = [kicad_cli, "pcb", "drc", "--refill-zones", "--save-board", "--format", "json",
           "-o", str(tmp / "drc.json"), str(copy)]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=900)
    if proc.returncode not in (0, 5) or copy.stat().st_mtime_ns == before:
        tail = (proc.stderr or proc.stdout or "").strip().splitlines()[-3:]
        raise RuntimeError(f"kicad-cli refill failed (exit {proc.returncode}): {' | '.join(tail)}")
    return copy


def track_lengths(board: Board) -> dict[tuple[str, str], float]:
    out: dict[tuple[str, str], float] = {}
    for t in board.tracks:
        L = sum(math.dist(a, b) for a, b in zip(t.points, t.points[1:]))
        out[(t.net, t.layer)] = out.get((t.net, t.layer), 0.0) + L
    return out


def match_nets(globs: str, nets: list[str]) -> set[str]:
    pats = [g.strip() for g in globs.split(",") if g.strip()]
    return {n for n in nets if any(wildcard_match(p, n) or wildcard_match(p, n.lstrip("/")) for p in pats)}


def class_table(signal_nets: set[str], class_of: dict[str, list[str]], lengths, classes: list[str]):
    table = {c: {"nets": [], "length": 0.0} for c in classes}
    for n in sorted(signal_nets):
        for c in class_of[n]:
            row = table.setdefault(c, {"nets": [], "length": 0.0})
            row["nets"].append(n)
            row["length"] += sum(L for (net, _), L in lengths.items() if net == n)
    return table


def ask(prompt: str, default: str) -> str:
    try:
        ans = input(f"{prompt} [{default}]: ").strip()
    except EOFError:
        ans = ""
    return ans or default


# --------------------------------------------------------------------------- setup

def build_setup(args, board: Board, pro: Path, interactive: bool, saved: dict | None = None,
                settings: dict | None = None):
    saved = saved or {}
    assumptions: list[str] = []
    # 1. layer roles
    guessed, why = guess_roles(board)
    if args.layers:
        roles = {**guessed, **parse_roles(args.layers, board)}
    else:
        roles = {**guessed, **parse_roles(saved["layers"], board)} if saved.get("layers") else guessed
        if saved.get("layers"):
            why = {l: "saved" for l in board.copper_layers}
        if interactive:
            print("\nCopper layers (top -> bottom):")
            for i, l in enumerate(board.copper_layers, 1):
                print(f"  L{i} {l:8} KiCad type: {board.layer_types.get(l, '?'):7} guess: {roles[l]:6} ({why[l]})")
            print(f"Roles: {', '.join(ROLES)}. Enter a full list top->bottom (e.g. sig,gnd,pwr,sig) "
                  "or overrides (e.g. In2.Cu=hybrid).")
            ans = ask("Layer roles", format_roles(roles, board))
            roles = {**roles, **parse_roles(ans, board)}
        elif not saved.get("layers"):
            assumptions.append(f"layer roles guessed: {format_roles(roles, board)}")

    # 2. reference nets
    g_gnd, g_pwr = guess_ref_nets(board, roles)
    if args.gnd_nets is None and saved.get("gnd_nets") is not None:
        args.gnd_nets = ",".join(saved["gnd_nets"])
    if args.pwr_nets is None and saved.get("pwr_nets") is not None:
        args.pwr_nets = ",".join(saved["pwr_nets"])
    gnd = match_nets(args.gnd_nets, board.nets) if args.gnd_nets else g_gnd
    pwr = match_nets(args.pwr_nets, board.nets) if args.pwr_nets is not None else g_pwr
    pwr -= gnd
    if interactive and not (args.gnd_nets and args.pwr_nets is not None):
        print("\nReference nets: GND = full reference, PWR = reference with a warning.")
        gnd = match_nets(ask("GND nets (comma, wildcards ok)", ",".join(sorted(gnd)) or "GND"), board.nets)
        ans = ask("PWR nets (comma, wildcards ok, '-' for none)", ",".join(sorted(pwr)) or "-")
        pwr = set() if ans.strip() == "-" else match_nets(ans, board.nets) - gnd
    else:
        if not args.gnd_nets:
            assumptions.append(f"GND nets guessed by name: {', '.join(sorted(gnd)) or 'none'}")
        if args.pwr_nets is None:
            assumptions.append(f"PWR nets = other nets filled on reference layers: {', '.join(sorted(pwr)) or 'none'}")
    if not gnd and not pwr:
        raise SystemExit("error: no reference nets. Use --gnd-nets / --pwr-nets.")

    # 3. which nets to check
    trace_layers = {l for l, r in roles.items() if r in TRACE_ROLES}
    routed = {t.net for t in board.tracks if t.layer in trace_layers and t.net and t.net not in gnd | pwr}
    named = match_nets(args.nets, sorted(routed)) if args.nets else set()
    if args.nets is None and saved.get("nets") and not interactive:
        named = match_nets(saved["nets"], sorted(routed))
    skipped_power = set() if args.include_power else (power_nets(settings or {}, board) & routed) - named
    signal_nets = routed - skipped_power
    if skipped_power:
        msg = f"power nets skipped (not signals): {', '.join(sorted(skipped_power))}"
        if interactive:
            print(f"\n{msg}. Use --include-power or --nets to check them anyway.")
        else:
            assumptions.append(msg)
    nc = load_netclasses(pro if pro.exists() else None)
    class_of = {n: nc.classes_for(n) for n in board.nets}
    lengths = track_lengths(board)
    table = class_table(signal_nets, class_of, lengths, nc.classes)
    populated = [c for c in nc.classes if table.get(c, {}).get("nets")]
    default_classes = [c for c in populated if c != "Default"] or populated

    if saved.get("classes"):
        saved_classes = [c for c in saved["classes"] if c in table]
        if interactive:
            default_classes = saved_classes or default_classes
        elif not args.classes and not args.nets:
            args.classes = ",".join(saved_classes) or None
    if args.nets is None and saved.get("nets") and not interactive:
        args.nets = saved["nets"]
    if args.classes:
        chosen = populated if args.classes.strip().lower() == "all" else \
            [c.strip() for c in args.classes.split(",") if c.strip()]
        unknown = [c for c in chosen if c not in table]
        if unknown:
            raise SystemExit(f"error: unknown net class(es) {', '.join(unknown)}; have {', '.join(nc.classes)}")
    elif args.nets:
        chosen = []
    elif interactive:
        print(f"\nNet classes ({nc.source.name if nc.source else 'no .kicad_pro: everything is Default'}):")
        for i, c in enumerate(populated, 1):
            row = table[c]
            sample = ", ".join(row["nets"][:5]) + (" ..." if len(row["nets"]) > 5 else "")
            print(f"  {i:2}. {c:16} {len(row['nets']):4} net(s) {row['length']:9.1f} mm   {sample}")
        ans = ask("Check which classes? numbers or names, comma separated, or 'all'", ",".join(default_classes))
        chosen = []
        for tok in [s.strip() for s in ans.split(",") if s.strip()]:
            if tok.lower() == "all":
                chosen = list(populated)
                break
            if tok.isdigit() and 1 <= int(tok) <= len(populated):
                chosen.append(populated[int(tok) - 1])
            elif tok in table:
                chosen.append(tok)
            else:
                raise SystemExit(f"error: unknown class {tok!r}")
    else:
        chosen = default_classes
        assumptions.append(f"net classes: {', '.join(chosen)} (all non-Default classes; use --classes)")

    nets = {n for n in signal_nets if any(c in chosen for c in class_of[n])}
    if args.nets:
        nets |= match_nets(args.nets, sorted(signal_nets))
    args._skipped_power = sorted(skipped_power)
    return roles, gnd, pwr, nets, chosen, class_of, assumptions


# --------------------------------------------------------------------------- commands

def _load(args, board_path: Path, tmp: Path, notes: list[str]) -> tuple[Board, str]:
    mode = args.refill
    if mode == "no":
        return read_board(board_path), "as saved in the board file"
    cli = find_kicad_cli(args.kicad_cli)
    if cli is None:
        if mode == "yes":
            raise SystemExit("error: --refill yes but kicad-cli was not found (use --kicad-cli PATH)")
        notes.append("kicad-cli not found: using the fills saved in the board. Press B in KiCad and save first.")
        return read_board(board_path), "as saved (kicad-cli not found)"
    try:
        copy = refill_to_copy(board_path, cli, tmp)
    except (RuntimeError, OSError, subprocess.TimeoutExpired) as exc:
        if mode == "yes":
            raise SystemExit(f"error: {exc}")
        notes.append(f"{exc}; using the fills saved in the board")
        return read_board(board_path), "as saved (refill failed)"
    b = read_board(copy)
    b.path = board_path
    return b, "refilled by kicad-cli on a temporary copy (board file untouched)"


def cmd_check(args) -> int:
    board_path = resolve_board(Path(args.board))
    pro = board_path.with_suffix(".kicad_pro")
    interactive = sys.stdin.isatty() and not args.yes
    load_notes: list[str] = []
    with tempfile.TemporaryDirectory(prefix="refplane-") as tmp:
        board, fills = _load(args, board_path, Path(tmp), load_notes)
    settings, sources = load_settings(board_path, Path(args.config) if args.config else None)
    saved = _saved_setup(board_path, args.config)
    if saved and interactive and not args.ask:
        print(f"using saved answers from {board_config_path(board_path).name} (--ask to answer again)")
        interactive = False
    roles, gnd, pwr, nets, chosen, class_of, assumptions = build_setup(args, board, pro, interactive, saved,
                                                                       settings)
    if not nets:
        raise SystemExit("error: no routed signal nets selected (check --classes / --nets / layer roles)")
    overrides = parse_class_overrides(args.stitch_class)
    if args.stitch_mm is not None:
        prof = settings["stitch"].get("default_profile", "high_speed")
        settings["stitch"]["profiles"][prof]["distance_mm"] = args.stitch_mm
    settings["stitch"].setdefault("classes", {}).update(overrides)
    high = None                                   # None = unchanged; [] = back to name patterns
    if args.high_speed is not None:
        high = [] if args.high_speed.strip() in ("", "-") else parse_list(args.high_speed)
        settings["stitch"]["high_speed"] = high
    rules = stitch_rules(settings)
    if interactive and not args.stitch_class:
        rules, asked, asked_high = _ask_stitch(rules, settings, chosen, nets, class_of, sources,
                                               ask_high=args.high_speed is None)
        overrides.update(asked)
        if asked_high is not None:
            high = asked_high
    margin_h = args.margin_h if args.margin_h is not None else float(settings.get("margin_h", 3.0))
    min_length = args.min_length if args.min_length is not None else float(settings.get("min_length_mm", 0.02))
    if args.save_config or (interactive and ask(f"Save these answers to {board_config_path(board_path).name} "
                                                "so the next run asks nothing? (y/n)", "n").lower().startswith("y")):
        path = _save_setup(board_path, roles, board, gnd, pwr, chosen, args.nets, overrides, args.margin_h, high)
        print(f"saved: {path}")
    ncl = load_netclasses(pro if pro.exists() else None)
    net_clearance = {n: c for n in board.nets if (c := ncl.clearance_for(n)) is not None}
    cfg = Config(roles=roles, gnd_nets=gnd, pwr_nets=pwr, nets=nets, netclass_of=class_of,
                 margin_h=margin_h, min_length=min_length, stitch=rules, net_clearance=net_clearance,
                 islands={**(settings.get("islands") or {}), **({"enabled": False} if args.no_islands else {})})
    res = run(board, cfg)
    res.notes = load_notes + res.notes
    setup = {"roles": format_roles(roles, board), "gnd_nets": sorted(gnd), "pwr_nets": sorted(pwr),
             "classes": chosen, "net_count": len(nets), "nets": sorted(nets), "margin_h": margin_h,
             "stitch_profiles": {n: r.__dict__ for n, r in rules.profiles.items()},
             "stitch_default_profile": rules.default_profile,
             "stitch_class": {c: v for c, v in rules.classes.items()},
             "stitch_mode": "explicit high-speed list" if rules.explicit else "name patterns",
             "high_speed": list(rules.high_speed),
             "settings_files": sources,
             "skipped_power": getattr(args, "_skipped_power", []),
             "min_length_mm": min_length, "fills": fills, "assumptions": assumptions,
             "rerun": _rerun_line(board_path, roles, board, gnd, pwr, chosen, args, overrides, high)}
    for a in assumptions:
        print(f"assumed: {a}", file=sys.stderr)

    if args.json and not args.output:
        print(report.to_json(res, board, setup))
    else:
        print(report.to_text(res))
    if not args.no_files:
        out = Path(args.output) if args.output else \
            Path("runs") / f"refplane-{board_path.stem}-{_dt.datetime.now().strftime('%Y%m%d-%H%M%S')}"
        written = report.write_outputs(out, res, board, setup, cfg, svg=not args.no_svg)
        print(f"\nreport: {out}/ ({', '.join(p.name for p in written)})")
        print(f"re-run: {setup['rerun']}")
    return 1 if any(f.severity == "error" for f in res.findings) else 0


def _saved_setup(board_path: Path, extra: str | None) -> dict:
    """Board-specific answers (layers, nets, classes) from <board>.refplane.json / --config."""
    out: dict = {}
    for p in (board_config_path(board_path), Path(extra) if extra else None):
        if p and p.exists():
            data = json.loads(p.read_text(encoding="utf-8"))
            out.update({k: data[k] for k in ("layers", "gnd_nets", "pwr_nets", "classes", "nets") if k in data})
    return out


def _save_setup(board_path, roles, board, gnd, pwr, chosen, nets_glob, overrides, margin_h, high=None) -> Path:
    path = board_config_path(board_path)
    data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    data["_about"] = "refplane settings for this board; any key of refplane/defaults.json may be added"
    data.update({"layers": format_roles(roles, board), "gnd_nets": sorted(gnd), "pwr_nets": sorted(pwr),
                 "classes": list(chosen)})
    if nets_glob:
        data["nets"] = nets_glob
    if margin_h is not None:
        data["margin_h"] = margin_h
    if overrides:
        data.setdefault("stitch", {}).setdefault("classes", {}).update(overrides)
    if high is not None:
        data.setdefault("stitch", {})["high_speed"] = list(high)
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    return path


def _ask_stitch(rules, settings, chosen, nets, class_of, sources, ask_high=True):
    """Show the return-via rule each checked class/net gets; let the user change it.

    First: which nets are high speed (then everything else is low speed), or Enter to keep
    the current mode. Then: per-class overrides.
    """
    print(f"\nReturn-via rules (from {', '.join(Path(s).name for s in sources)}):")
    for r in rules.profiles.values():
        print(f"  profile {r.describe()}")
    asked_high = None
    if ask_high:
        classes = sorted({c for n in nets for c in class_of[n]})
        print(f"  Mode now: {'high-speed list ' + ', '.join(rules.high_speed) if rules.explicit else 'name patterns (*I2C*, *SPI*, *UART* ... = low_speed, rest high_speed)'}")
        print(f"  Name your high-speed nets and everything else becomes {rules.low_speed_profile}. "
              f"Classes here: {', '.join(classes)}")
        ans = ask("High-speed classes or nets, comma separated, wildcards ok "
                  "('-' = name patterns, Enter keeps the mode)", "")
        if ans.strip():
            asked_high = [] if ans.strip() == "-" else parse_list(ans)
            settings["stitch"]["high_speed"] = asked_high
            rules = stitch_rules(settings)
    rows: dict[str, dict[str, list[str]]] = {}
    for n in sorted(nets):
        cls = next((c for c in class_of[n] if c in chosen), class_of[n][0])
        prof = rules.rule_for(n, class_of[n]).name
        src = rules.source_for(n, class_of[n])
        rows.setdefault(cls, {}).setdefault(f"{prof} ({src})", []).append(n)
    for cls, groups in rows.items():
        for label, members in groups.items():
            sample = ", ".join(members[:4]) + (" ..." if len(members) > 4 else "")
            print(f"  {cls:16} {len(members):3} net(s) -> {label:32} {sample}")
    if not rules.explicit:
        print("  (pattern) = matched a name pattern such as *I2C*, *SPI*, *UART* in the settings file")
    ans = ask("Change per class? CLASS=MM or CLASS=PROFILE, comma separated (Enter keeps these)", "")
    asked = parse_class_overrides(ans)
    if asked:
        settings["stitch"].setdefault("classes", {}).update(asked)
        rules = stitch_rules(settings)
    return rules, asked, asked_high


def _rerun_line(board_path, roles, board, gnd, pwr, chosen, args, overrides=None, high=None) -> str:
    parts = ["python3 -m refplane check", f'"{board_path}"', f"--layers {format_roles(roles, board)}",
             f"--gnd-nets '{','.join(sorted(gnd))}'", f"--pwr-nets '{','.join(sorted(pwr))}'"]
    if chosen:
        parts.append(f"--classes '{','.join(chosen)}'")
    if args.nets:
        parts.append(f"--nets '{args.nets}'")
    if args.stitch_mm is not None:
        parts.append(f"--stitch-mm {args.stitch_mm:g}")
    if overrides:
        parts.append("--stitch-class '" + ",".join(f"{c}={v:g}" if isinstance(v, float) else f"{c}={v}"
                                                  for c, v in overrides.items()) + "'")
    if high is not None:
        parts.append(f"--high-speed '{','.join(high) or '-'}'")
    if args.config:
        parts.append(f"--config '{args.config}'")
    if args.margin_h is not None:
        parts.append(f"--margin-h {args.margin_h:g}")
    return " ".join(parts)


def cmd_info(args) -> int:
    board_path = resolve_board(Path(args.board))
    board = read_board(board_path)
    pro = board_path.with_suffix(".kicad_pro")
    roles, why = guess_roles(board)
    gnd, pwr = guess_ref_nets(board, roles)
    nc = load_netclasses(pro if pro.exists() else None)
    lengths = track_lengths(board)
    settings, _ = load_settings(board_path)
    skipped = power_nets(settings, board, exclude=gnd)
    signal = {t.net for t in board.tracks if t.net and t.net not in gnd | pwr | skipped}
    class_of = {n: nc.classes_for(n) for n in board.nets}
    table = class_table(signal, class_of, lengths, nc.classes)
    layers = []
    for i, l in enumerate(board.copper_layers):
        nxt = board.copper_layers[i + 1] if i + 1 < len(board.copper_layers) else None
        fills = sorted({z.net for z in board.zones if l in z.fills and z.net})
        layers.append({"layer": l, "kicad_type": board.layer_types.get(l), "guess": roles[l], "why": why[l],
                       "fills": fills, "tracks": sum(1 for t in board.tracks if t.layer == l),
                       "h_to_next_mm": board.h_between(l, nxt) if nxt else None})
    data = {"board": str(board_path), "stackup": board.has_stackup, "layers": layers,
            "gnd_nets_guess": sorted(gnd), "pwr_nets_guess": sorted(pwr),
            "power_nets_skipped": sorted(skipped & {t.net for t in board.tracks}),
            "netclasses": {c: {"nets": len(r["nets"]), "length_mm": round(r["length"], 2),
                               "sample": r["nets"][:8]} for c, r in table.items() if r["nets"]},
            "suggested": f'python3 -m refplane check "{board_path}" --layers {format_roles(roles, board)}'}
    if args.json:
        print(json.dumps(data, indent=2))
        return 0
    print(f"{board_path.name}: {len(board.copper_layers)} copper layers, stackup {'yes' if board.has_stackup else 'NO'}")
    for i, d in enumerate(layers, 1):
        h = f"  H to next {d['h_to_next_mm']:.3f} mm" if d["h_to_next_mm"] else ""
        print(f"  L{i} {d['layer']:8} {d['kicad_type'] or '?':7} guess {d['guess']:6} ({d['why']}); "
              f"{d['tracks']} tracks; fills: {', '.join(d['fills']) or '-'}{h}")
    print(f"GND nets (guess): {', '.join(data['gnd_nets_guess']) or '-'}")
    print(f"PWR nets (guess): {', '.join(data['pwr_nets_guess']) or '-'}")
    print(f"Power rails skipped as signals: {', '.join(data['power_nets_skipped']) or '-'}")
    print("Net classes with routed signal nets:")
    for c, r in data["netclasses"].items():
        print(f"  {c:16} {r['nets']:4} nets {r['length_mm']:9.1f} mm  {', '.join(r['sample'][:5])}")
    print(f"\nnext: {data['suggested']}")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="python3 -m refplane",
                                 description="Check that routed nets run over an unbroken reference plane")
    sub = ap.add_subparsers(dest="cmd", required=True)

    i = sub.add_parser("info", help="show layers, guessed roles, reference nets and net classes")
    i.add_argument("board", help=".kicad_pcb / .kicad_pro / project folder")
    i.add_argument("--json", action="store_true")
    i.set_defaults(func=cmd_info)

    c = sub.add_parser("check", help="run the reference-plane check")
    c.add_argument("board", help=".kicad_pcb / .kicad_pro / project folder")
    c.add_argument("--layers", help="roles top->bottom 'sig,gnd,pwr,sig' or 'In2.Cu=hybrid,...' "
                                    f"({', '.join(ROLES)})")
    c.add_argument("--gnd-nets", help="GND reference nets, comma list, wildcards ok (default: by name)")
    c.add_argument("--pwr-nets", help="PWR reference nets, comma list ('' for none; default: other nets "
                                      "filled on reference layers)")
    c.add_argument("--classes", help="net classes to check, comma list or 'all' (default: ask, or all non-Default)")
    c.add_argument("--nets", help="extra nets to check, comma list, wildcards ok")
    c.add_argument("--stitch-mm", type=float, default=None,
                   help="distance of the default return-via profile, mm (settings file: 1.0)")
    c.add_argument("--stitch-class", help="per net class: CLASS=MM or CLASS=PROFILE, e.g. 'USB=0.6,Default=low_speed'")
    c.add_argument("--no-islands", action="store_true", help="skip the reference-copper island check")
    c.add_argument("--include-power", action="store_true",
                   help="also check routed power-rail nets (+3V3, VCC..., nets with a zone fill)")
    c.add_argument("--high-speed", help="explicit mode: these classes/nets (comma, wildcards) are high speed, "
                                        "every other net low speed; '-' = back to name patterns")
    c.add_argument("--config", help="extra settings JSON (same keys as refplane/defaults.json)")
    c.add_argument("--ask", action="store_true", help="ask again even when <board>.refplane.json has answers")
    c.add_argument("--save-config", action="store_true",
                   help="write the answers to <board>.refplane.json next to the board")
    c.add_argument("--margin-h", type=float, default=None, help="margin band each side, in multiples of H (default 3)")
    c.add_argument("--min-length", type=float, default=None, help="ignore runs shorter than this, mm (default 0.02)")
    c.add_argument("--refill", choices=("auto", "yes", "no"), default="auto",
                   help="refill zones with kicad-cli on a temporary copy first (default auto)")
    c.add_argument("--kicad-cli", help="path to kicad-cli")
    c.add_argument("-y", "--yes", action="store_true", help="no questions: use flags and guesses")
    c.add_argument("-o", "--output", help="report directory (must not exist; default runs/refplane-<board>-<time>)")
    c.add_argument("--json", action="store_true", help="print JSON to stdout")
    c.add_argument("--no-svg", action="store_true")
    c.add_argument("--no-files", action="store_true", help="print only, write no report directory")
    c.set_defaults(func=cmd_check)

    args = ap.parse_args(argv)
    try:
        return args.func(args)
    except ValueError as exc:
        raise SystemExit(f"error: {exc}")
