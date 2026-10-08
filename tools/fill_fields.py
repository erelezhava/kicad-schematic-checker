#!/usr/bin/env python3
"""Fill EMPTY schematic fields from checker suggestions (e.g. Manufacturer from DigiKey).

This is a separate, opt-in helper: the checker itself never edits a design.

Safety rules:
  * dry run by default; nothing is written without --apply
  * only fills fields that are missing or empty; never overwrites a value
  * several suggested names (e.g. two DigiKey listings) are skipped unless you
    choose one with --prefer
  * edits only the active design: the root schematic (from the .kicad_pro) and
    the sub-sheets it references; history copies (.history/) and other files are ignored
  * refuses to write while KiCad has the schematic open (lock file present)
  * --backup keeps a copy of each changed file (*.kicad_sch.bak-<time>); with the
    design in git, `git diff` already shows and can undo every change

Usage:
    python3 tools/fill_fields.py runs/board-dk/digikey.json /path/to/project            # preview
    (project = folder with one .kicad_pro, or the .kicad_pro / root .kicad_sch itself)
    python3 tools/fill_fields.py runs/board-dk/digikey.json /path/to/project \\
        --prefer "Murata Electronics" --apply                                          # write
Then re-extract and re-run the checks.
"""

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from kicad_checker.schematic import design_files, key, parse, quote, root_schematic  # noqa: E402

# Existing fields that count as the manufacturer (same aliases as the checker).
MANUFACTURER_KEYS = {"manufacturer", "manufacturername", "mfr", "mfrname", "mfg"}
PLACEHOLDERS = {"", "?", "-", "unknown", "tbd", "tbc", "todo", "n/a", "na", "none", "null"}


def symbol_refs(symbol):
    """Reference property plus every instance reference (hierarchical reuse)."""
    refs = set()
    for prop in symbol.children("property"):
        s = prop.strings()
        if len(s) >= 2 and s[0][1] == "Reference":
            refs.add(s[1][1])
    def walk(node):
        for child in node.children():
            if child.head == "reference" and child.strings():
                refs.add(child.strings()[0][1])
            walk(child)
    for inst in symbol.children("instances"):
        walk(inst)
    return refs


def plan_file(path, wanted, field):
    """Return (edits, report lines). edits = [(start, end, replacement)]."""
    text = path.read_text(encoding="utf-8")
    root = parse(text)
    edits, notes = [], []
    for symbol in root.children("symbol"):  # placed symbols only; lib_symbols is a different node
        refs = symbol_refs(symbol) & set(wanted)
        if not refs:
            continue
        ref = sorted(refs)[0]
        value = wanted[ref]
        props = symbol.children("property")
        aliases = [(p, p.strings()) for p in props if len(p.strings()) >= 2 and key(p.strings()[0][1]) in MANUFACTURER_KEYS]
        filled = [s[1][1] for p, s in aliases if s[1][1].strip().casefold() not in PLACEHOLDERS]
        if filled:
            notes.append(f"  skip {ref}: already has {field} = {filled[0]!r}")
            continue
        if aliases:  # an empty alias field exists (e.g. Manufacturer_Name ""): fill it in place
            prop, s = aliases[0]
            edits.append((s[1][2], s[1][3], quote(value)))
            notes.append(f"  set  {ref}: {s[0][1]} = {value!r} (existing empty field)")
            continue
        last = props[-1]
        at = next((c for c in symbol.children("at")), None)
        position = text[at.start:at.end] if at else "(at 0 0 0)"
        line_start = text.rfind("\n", 0, last.start) + 1
        indent = text[line_start:last.start]
        new = (f"\n{indent}(property {quote(field)} {quote(value)}\n{indent}\t{position}"
               f"\n{indent}\t(effects\n{indent}\t\t(font\n{indent}\t\t\t(size 1.27 1.27)\n{indent}\t\t)"
               f"\n{indent}\t\t(hide yes)\n{indent}\t)\n{indent})")
        edits.append((last.end, last.end, new))
        notes.append(f"  add  {ref}: {field} = {value!r} (new hidden field)")
    return text, edits, notes


def suggestions(report, field, prefer):
    wanted, skipped = {}, []
    preferred = [p.casefold() for p in prefer]
    for item in report.get("results", []):
        names = (item.get("suggested_fields") or {}).get(field)
        if not names:
            continue
        if len(names) == 1:
            wanted[item["reference"]] = names[0]
            continue
        chosen = [n for n in names if n.casefold() in preferred]
        if len(chosen) == 1:
            wanted[item["reference"]] = chosen[0]
        else:
            skipped.append(f"  skip {item['reference']}: several names {names}; choose one with --prefer")
    return wanted, skipped


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("report", help="digikey.json written by digikey-check")
    parser.add_argument("project", help="KiCad project folder, .kicad_pro, or root .kicad_sch (sub-sheets are followed)")
    parser.add_argument("--field", default="Manufacturer", help="Field to fill (default Manufacturer)")
    parser.add_argument("--prefer", action="append", default=[], help="Name to choose when several are suggested; repeatable")
    parser.add_argument("--apply", action="store_true", help="Write the changes (default: preview only)")
    parser.add_argument("--backup", action="store_true", help="Also keep *.kicad_sch.bak-<time> copies of changed files")
    args = parser.parse_args()
    if args.field != "Manufacturer":
        sys.exit("Only --field Manufacturer is supported so far")

    report = json.loads(Path(args.report).read_text(encoding="utf-8"))
    wanted, skipped = suggestions(report, args.field, args.prefer)
    try:
        files = design_files(root_schematic(args.project))
    except ValueError as error:
        sys.exit(str(error))

    plans, found = [], set()
    for path in files:
        text, edits, notes = plan_file(path, wanted, args.field)
        if notes:
            print(path)
            print("\n".join(notes))
        found |= {n.split()[1].rstrip(":") for n in notes}
        if edits:
            plans.append((path, text, edits))
    for line in skipped:
        print(line)
    missing = sorted(set(wanted) - found)
    if missing:
        print(f"  not found in schematic files: {', '.join(missing)}")
    total = sum(len(e) for _, _, e in plans)
    if not args.apply:
        print(f"\nPreview only: {total} change(s) in {len(plans)} file(s). Re-run with --apply to write.")
        return 0
    locked = [p for p, _, _ in plans if (p.parent / f"~{p.name}.lck").exists()]
    if locked:
        sys.exit(f"KiCad has these open (lock file present); close them first: {', '.join(map(str, locked))}")
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    for path, text, edits in plans:
        if args.backup:
            path.with_name(f"{path.name}.bak-{stamp}").write_text(text, encoding="utf-8")
        for start, end, replacement in sorted(edits, reverse=True):
            text = text[:start] + replacement + text[end:]
        parse(text)  # refuse to leave a broken file behind
        path.write_text(text, encoding="utf-8")
    kept = f"; backups *.bak-{stamp}" if args.backup else ""
    print(f"\nWrote {total} change(s) in {len(plans)} file(s){kept}. Review with `git diff`, then re-extract and re-run the checks.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
