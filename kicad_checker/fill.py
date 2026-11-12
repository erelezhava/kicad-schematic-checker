"""Fill EMPTY Manufacturer fields in the schematic from DigiKey (opt-in; the checks never edit a design).

    python3 -m kicad_checker fill-manufacturer PROJECT            # preview
    python3 -m kicad_checker fill-manufacturer PROJECT --apply    # write (close KiCad first)

PROJECT is the project folder, its .kicad_pro or root .kicad_sch. The DigiKey results come from
the newest `<project>_report_R<n>/02_digikey/digikey.json` (or --report).

Safety rules:
  * preview by default; nothing is written without --apply
  * only fills missing or empty fields (placeholders like TBD count as empty); never overwrites
  * the symbol's MPN must still equal the MPN DigiKey was asked about, so a renumbered or changed
    part is skipped, not mislabelled
  * several DigiKey names are resolved only by manufacturer_aliases.json (same company, or a
    stated family rule) or --prefer; otherwise skipped for the owner to choose
  * edits only the active design (root sheet + referenced sub-sheets), never .history copies
  * refuses to write while KiCad has a schematic open (lock file)
  * every changed file is checked to still parse, then replaced in one step (temporary file +
    rename), so an interrupted run cannot leave a half-written schematic; line endings are kept
  * --backup keeps *.kicad_sch.bak-<time>; with the design in git, `git diff` shows every change
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sys
from datetime import datetime
from pathlib import Path

from .schematic import design_files, key, parse, quote, root_schematic

MANUFACTURER_KEYS = {"manufacturer", "manufacturername", "mfr", "mfrname", "mfg"}
MPN_KEYS = {"mpn", "manufacturerpartnumber", "manufacturerpart", "mfrpartnumber", "mfrpart", "mfrpn", "prtnum"}
PLACEHOLDERS = {"", "?", "-", "unknown", "tbd", "tbc", "todo", "n/a", "na", "none", "null"}
ALIASES_PATH = Path(__file__).with_name("manufacturer_aliases.json")


def mpn_key(value: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (value or "").casefold())


def load_aliases(path=ALIASES_PATH) -> dict:
    data = json.loads(Path(path).read_text(encoding="utf-8")) if Path(path).is_file() else {}
    return {"same_company": {k.casefold(): v for k, v in (data.get("same_company") or {}).items()},
            "family": data.get("family_preference") or []}


def _ref_prefix(ref: str) -> str:
    return re.match(r"[A-Za-z]*", ref).group(0).upper()


def resolve(ref: str, names: list[str], prefer: list[str], aliases: dict) -> tuple[str | None, str]:
    """One manufacturer name for this reference, and how it was chosen ('' = only one listed)."""
    if len(names) == 1:
        return names[0], ""
    canon = list(dict.fromkeys(aliases["same_company"].get(n.casefold(), n) for n in names))
    if len(canon) == 1:
        return canon[0], f"same company: {' / '.join(names)}"
    chosen = [n for n in canon if n.casefold() in {p.casefold() for p in prefer}]
    if len(chosen) == 1:
        return chosen[0], "--prefer"
    for rule in aliases["family"]:
        if _ref_prefix(ref) in rule.get("refs", []) and {n.casefold() for n in canon} <= {
                n.casefold() for n in rule.get("names", [])}:
            return rule["choose"], f"family rule: {rule.get('why', '')}".rstrip(": ")
    return None, ""


def suggestions(report: dict, prefer: list[str], aliases: dict | None = None):
    """{ref: (name, mpn, how)} plus skip notes."""
    aliases = aliases or load_aliases()
    wanted, skipped = {}, []
    for item in report.get("results", []):
        names = (item.get("suggested_fields") or {}).get("Manufacturer")
        if not names:
            continue
        name, how = resolve(item["reference"], names, prefer, aliases)
        if name:
            wanted[item["reference"]] = (name, item.get("mpn"), how)
        else:
            skipped.append(f"  skip {item['reference']}: several names {names}; choose one with --prefer")
    return wanted, skipped


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


def read_exact(path: Path) -> str:
    """File text with its line endings untouched (no newline translation on any platform)."""
    with open(path, encoding="utf-8", newline="") as handle:
        return handle.read()


def write_atomic(path: Path, text: str) -> None:
    """Replace `path` in one step: a crash or full disk leaves the old file, never a partial one."""
    tmp = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    try:
        with open(tmp, "w", encoding="utf-8", newline="") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        shutil.copymode(path, tmp)
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def plan_file(path: Path, wanted: dict, field: str = "Manufacturer"):
    """Return (text, edits, notes). edits = [(start, end, replacement)]."""
    text = read_exact(path)
    newline = "\r\n" if "\r\n" in text else "\n"
    root = parse(text)
    edits, notes = [], []
    for symbol in root.children("symbol"):  # placed symbols only; lib_symbols is a different node
        refs = symbol_refs(symbol) & set(wanted)
        if not refs:
            continue
        ref = sorted(refs)[0]
        value, mpn, how = wanted[ref]
        props = [(p, p.strings()) for p in symbol.children("property")]
        props = [(p, s) for p, s in props if len(s) >= 2]
        if mpn:
            mine = [s[1][1] for p, s in props if key(s[0][1]) in MPN_KEYS and s[1][1].strip()]
            if not any(mpn_key(m) == mpn_key(mpn) for m in mine):
                notes.append(f"  skip {ref}: MPN is now {mine[0] if mine else 'empty'!r}, DigiKey result was for "
                             f"{mpn!r}; run the checks again")
                continue
        aliases = [(p, s) for p, s in props if key(s[0][1]) in MANUFACTURER_KEYS]
        filled = [s[1][1] for p, s in aliases if s[1][1].strip().casefold() not in PLACEHOLDERS]
        why = f" [{how}]" if how else ""
        if filled:
            notes.append(f"  skip {ref}: already has {field} = {filled[0]!r}")
            continue
        if aliases:  # an empty alias field exists (e.g. Manufacturer_Name ""): fill it in place
            _prop, s = aliases[0]
            edits.append((s[1][2], s[1][3], quote(value)))
            notes.append(f"  set  {ref}: {s[0][1]} = {value!r} (existing empty field){why}")
            continue
        last = symbol.children("property")[-1]
        at = next((c for c in symbol.children("at")), None)
        position = text[at.start:at.end] if at else "(at 0 0 0)"
        line_start = text.rfind("\n", 0, last.start) + 1
        indent = text[line_start:last.start]
        new = (f"\n{indent}(property {quote(field)} {quote(value)}\n{indent}\t{position}"
               f"\n{indent}\t(effects\n{indent}\t\t(font\n{indent}\t\t\t(size 1.27 1.27)\n{indent}\t\t)"
               f"\n{indent}\t\t(hide yes)\n{indent}\t)\n{indent})")
        edits.append((last.end, last.end, new.replace("\n", newline)))
        notes.append(f"  add  {ref}: {field} = {value!r} (new hidden field){why}")
    return text, edits, notes


def plan(project, report: dict, prefer=(), aliases=None):
    """Everything fill would do, without writing: (plans, printed lines, change count)."""
    wanted, skipped = suggestions(report, list(prefer), aliases)
    files = design_files(root_schematic(project))
    plans, lines, found = [], [], set()
    for path in files:
        text, edits, notes = plan_file(path, wanted)
        if notes:
            lines += [str(path), *notes]
        found |= {n.split()[1].rstrip(":") for n in notes}
        if edits:
            plans.append((path, text, edits))
    lines += skipped
    missing = sorted(set(wanted) - found)
    if missing:
        lines.append(f"  not found in schematic files: {', '.join(missing)}")
    return plans, lines, sum(len(e) for _, _, e in plans)


def latest_report(project) -> Path:
    """digikey.json of the newest <stem>_report_R<n> next to the project."""
    root = root_schematic(project)
    pattern = re.compile(rf"^{re.escape(root.stem)}_report_R(\d+)$")
    found = sorted(((int(m.group(1)), p) for p in root.parent.iterdir()
                    if p.is_dir() and (m := pattern.match(p.name)) and (p / "02_digikey" / "digikey.json").is_file()),
                   reverse=True)
    if not found:
        raise ValueError(f"No {root.stem}_report_R<n>/02_digikey/digikey.json next to {root.name}: "
                         "run `python3 -m kicad_checker run` first, or pass --report")
    return found[0][1] / "02_digikey" / "digikey.json"


def report_path(arg) -> Path:
    p = Path(arg)
    if p.is_dir():
        for cand in (p / "02_digikey" / "digikey.json", p / "digikey.json"):
            if cand.is_file():
                return cand
        raise ValueError(f"No digikey.json in {p}")
    return p


def check_locks(plans) -> None:
    locked = [p for p, _, _ in plans if (p.parent / f"~{p.name}.lck").exists()]
    if locked:
        raise ValueError(f"KiCad has these open (lock file present); close them first: {', '.join(map(str, locked))}")


def run(args) -> int:
    report_file = report_path(args.report) if args.report else latest_report(args.project)
    print(f"DigiKey results: {report_file}")
    report = json.loads(report_file.read_text(encoding="utf-8"))
    plans, lines, total = plan(args.project, report, args.prefer)
    print("\n".join(lines))
    if not args.apply:
        print(f"\nPreview only: {total} change(s) in {len(plans)} file(s). Re-run with --apply to write.")
        return 0
    check_locks(plans)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    changed = []
    for path, text, edits in plans:
        new = text
        for start, end, replacement in sorted(edits, reverse=True):
            new = new[:start] + replacement + new[end:]
        parse(new)  # refuse to leave a broken file behind
        changed.append((path, text, new))
    for path, text, new in changed:  # nothing is written until every file has been checked
        if args.backup:
            with open(path.with_name(f"{path.name}.bak-{stamp}"), "w", encoding="utf-8", newline="") as handle:
                handle.write(text)
        write_atomic(path, new)
    kept = f"; backups *.bak-{stamp}" if args.backup else ""
    print(f"\nWrote {total} change(s) in {len(plans)} file(s){kept}. Review with `git diff`, run Update PCB from "
          "Schematic (F8) in KiCad, then run the checks again.")
    return 0


def add_arguments(c) -> None:
    c.add_argument("project", help="project folder, .kicad_pro, or root .kicad_sch (sub-sheets are followed)")
    c.add_argument("--report", help="digikey.json, or a report folder (default: newest <project>_report_R<n>)")
    c.add_argument("--prefer", action="append", default=[], help="name to choose when several are listed; repeatable")
    c.add_argument("--apply", action="store_true", help="write the changes (default: preview only)")
    c.add_argument("--backup", action="store_true", help="also keep *.kicad_sch.bak-<time> copies of changed files")


def add_command(sub) -> None:
    c = sub.add_parser("fill-manufacturer", help="Fill EMPTY Manufacturer fields in the schematic from DigiKey "
                                                 "(preview; --apply writes)")
    add_arguments(c)


def legacy_main(argv=None) -> int:
    """tools/fill_fields.py REPORT PROJECT [...] (kept for existing scripts)."""
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("report", help="digikey.json written by digikey-check")
    p.add_argument("project")
    p.add_argument("--field", default="Manufacturer")
    p.add_argument("--prefer", action="append", default=[])
    p.add_argument("--apply", action="store_true")
    p.add_argument("--backup", action="store_true")
    args = p.parse_args(argv)
    if args.field != "Manufacturer":
        sys.exit("Only --field Manufacturer is supported so far")
    try:
        return run(args)
    except ValueError as error:
        sys.exit(str(error))
