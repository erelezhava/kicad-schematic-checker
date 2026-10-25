"""Command line: python3 -m jlcpcb_rules {generate,install,preflight,presets}."""

from __future__ import annotations

import argparse
import datetime as _dt
import json
import shutil
import sys
from pathlib import Path

from . import preflight as _preflight
from .board import read_board
from .generator import render_file, split_existing
from .profile import ACCEPT_KEYS, FINISHES, MASK_COLORS, TIERS, Profile, profile_from_board

PRESETS = [
    dict(layers=2, outer_oz=1, thickness=1.6, finish="HASL_LF"),
    dict(layers=2, outer_oz=1, thickness=1.6, finish="ENIG"),
    dict(layers=2, outer_oz=2, thickness=1.6, finish="HASL_LF"),
    dict(layers=4, outer_oz=1, inner_oz=0.5, thickness=1.6, finish="ENIG"),
    dict(layers=4, outer_oz=1, inner_oz=0.5, thickness=0.8, finish="ENIG"),
    dict(layers=6, outer_oz=1, inner_oz=0.5, thickness=1.6, finish="ENIG"),
    dict(layers=8, outer_oz=1, inner_oz=0.5, thickness=1.6, finish="ENIG"),
]


def _add_profile_args(ap: argparse.ArgumentParser) -> None:
    g = ap.add_argument_group("profile (overrides what is read from the board)")
    g.add_argument("--layers", type=int)
    g.add_argument("--outer-oz", type=float)
    g.add_argument("--inner-oz", type=float)
    g.add_argument("--thickness", type=float, help="board thickness, mm")
    g.add_argument("--finish", choices=FINISHES)
    g.add_argument("--mask", dest="mask_color", choices=MASK_COLORS)
    g.add_argument("--tier", choices=TIERS, default=None,
                   help="tiered (default): error beyond JLC limits, warning for paid options")
    g.add_argument("--accept", default="",
                   help=f"comma list of paid options you accept (no warnings): {','.join(ACCEPT_KEYS)}")


def _profile(args, board_path: Path | None) -> Profile:
    over = dict(layers=args.layers, outer_oz=args.outer_oz, inner_oz=args.inner_oz,
                thickness=args.thickness, finish=args.finish, mask_color=args.mask_color,
                tier=args.tier)
    accept = [a.strip() for a in args.accept.split(",") if a.strip()]
    if board_path:
        p = profile_from_board(read_board(board_path), **over)
    else:
        p = Profile(**{k: v for k, v in over.items() if v is not None})
        p.notes = ["profile from command line"]
    p.accept = accept
    problems = p.validate()
    if problems:
        raise SystemExit("error: " + "; ".join(problems))
    return p


def _resolve_board(path: Path) -> Path:
    if path.is_dir():
        boards = sorted(path.glob("*.kicad_pcb"))
        if len(boards) != 1:
            raise SystemExit(f"error: expected one .kicad_pcb in {path}, found {len(boards)}")
        return boards[0]
    if path.suffix == ".kicad_pro":
        return path.with_suffix(".kicad_pcb")
    return path


def cmd_generate(args) -> int:
    board = _resolve_board(Path(args.board)) if args.board else None
    p = _profile(args, board)
    text = render_file(p)
    if args.output:
        out = Path(args.output)
        if out.exists() and not args.force:
            raise SystemExit(f"error: {out} exists (use --force, or 'install' to keep your own rules)")
        out.write_text(text, encoding="utf-8")
        print(f"wrote {out}")
    else:
        sys.stdout.write(text)
    for n in p.notes:
        print(f"  {n}", file=sys.stderr)
    return 0


def cmd_install(args) -> int:
    board = _resolve_board(Path(args.project))
    if not board.exists():
        raise SystemExit(f"error: {board} not found")
    p = _profile(args, board)
    target = board.with_suffix(".kicad_dru")
    user = ""
    if target.exists():
        existing = target.read_text(encoding="utf-8")
        generated, user = split_existing(existing)
        if not generated and not (args.merge or args.force):
            raise SystemExit(
                f"error: {target.name} already has rules that were not made by this tool.\n"
                "  --merge  keep them after the JLC block (they then override JLC rules)\n"
                "  --force  replace the file (a backup is kept)")
        if args.force and not generated:
            user = ""
    text = render_file(p, user_rules=user)
    print("Profile: " + "; ".join(p.notes))
    if args.dry_run:
        print(f"dry run: would write {target} ({len(text.splitlines())} lines"
              + (", keeping your rules below the JLC block" if user.strip() else "") + ")")
        return 0
    if target.exists():
        stamp = _dt.datetime.now().strftime("%Y%m%d-%H%M%S")
        backup = target.with_name(f"{target.name}.bak-{stamp}")
        n = 1
        while backup.exists():
            n += 1
            backup = target.with_name(f"{target.name}.bak-{stamp}-{n}")
        shutil.copy2(target, backup)
        print(f"backup: {backup.name}")
    target.write_text(text, encoding="utf-8")
    print(f"wrote {target}")
    print("In KiCad: reopen Board Setup (or the board) so the new rules load, then run DRC.")
    return 0


def cmd_preflight(args) -> int:
    board = _resolve_board(Path(args.board))
    info = read_board(board)
    p = _profile(args, board)
    findings = _preflight.run(info, p, rules_path=board.with_suffix(".kicad_dru"))
    if args.json:
        print(json.dumps({"board": str(board), "profile": json.loads(p.to_json()),
                          "detected": p.notes, "findings": _preflight.to_dicts(findings)}, indent=2))
    else:
        print(_preflight.format_text(findings, p))
    return 1 if any(f.severity == "error" for f in findings) else 0


def cmd_presets(args) -> int:
    out = Path(args.output)
    out.mkdir(parents=True, exist_ok=True)
    for spec in PRESETS:
        p = Profile(**spec)
        path = out / f"{p.key()}.kicad_dru"
        path.write_text(render_file(p), encoding="utf-8")
        print(f"wrote {path}")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="python3 -m jlcpcb_rules",
                                 description="JLCPCB manufacturing rules for KiCad 10")
    sub = ap.add_subparsers(dest="cmd", required=True)

    g = sub.add_parser("generate", help="print or write a .kicad_dru for a profile or board")
    g.add_argument("--board", help=".kicad_pcb / .kicad_pro / project folder to read the stackup from")
    g.add_argument("-o", "--output", help="output file (default: stdout)")
    g.add_argument("--force", action="store_true")
    _add_profile_args(g)
    g.set_defaults(func=cmd_generate)

    i = sub.add_parser("install", help="write <project>.kicad_dru next to the board")
    i.add_argument("project", help=".kicad_pro / .kicad_pcb / project folder")
    i.add_argument("--merge", action="store_true", help="keep existing non-JLC rules after the JLC block")
    i.add_argument("--force", action="store_true", help="replace existing non-JLC rules (backup kept)")
    i.add_argument("--dry-run", action="store_true")
    _add_profile_args(i)
    i.set_defaults(func=cmd_install)

    f = sub.add_parser("preflight", help="board-level JLC checks DRC cannot do")
    f.add_argument("board", help=".kicad_pcb / .kicad_pro / project folder")
    f.add_argument("--json", action="store_true")
    _add_profile_args(f)
    f.set_defaults(func=cmd_preflight)

    pr = sub.add_parser("presets", help="regenerate the ready-made preset files")
    pr.add_argument("-o", "--output", default=str(Path(__file__).with_name("presets")))
    pr.set_defaults(func=cmd_presets)

    args = ap.parse_args(argv)
    try:
        return args.func(args)
    except ValueError as exc:
        raise SystemExit(f"error: {exc}")
