"""Read-only stock audit for the checker's circuit.json. No design writes."""

import argparse
import re
from pathlib import Path

from ..core import component_exemption, digest, identity_candidates, read_json, write_json
from .providers import identity, utc_now
from .requirements import positive_int, stock_policy


def add_command(sub):
    command = sub.add_parser("parts-bom", help="Check stock for the unique exact parts in an extracted circuit (offline by default)")
    command.add_argument("circuit")
    command.add_argument("--boards", type=int, default=1)
    command.add_argument("--min-stock", type=int, default=1000)
    command.add_argument("--max-age-hours", type=float, default=24)
    command.add_argument("--max-parts", type=int, default=10, help="Total unique-part budget; remaining parts stay unknown")
    command.add_argument("--output", required=True)
    command.add_argument("--cache", default=None)
    command.add_argument("--online", action="store_true")
    command.add_argument("--stock-import", action="append", default=[])


def run(args):
    # Deferred import keeps command wiring and report orchestration separate.
    from .cli import run as run_part
    from ..paths import CACHE_ROOT

    positive_int(args.boards, "boards")
    positive_int(args.max_parts, "max-parts")
    stock_policy({"min_stock": args.min_stock, "max_age_hours": args.max_age_hours})
    circuit = read_json(args.circuit)
    if not isinstance(circuit.get("components"), dict):
        raise ValueError("Expected checker circuit.json with a components object")
    output = Path(args.output).resolve()
    if output.exists():
        raise ValueError("Choose a new BOM report directory")
    groups, rows = {}, []
    for ref, component in sorted(circuit["components"].items()):
        if component_exemption(ref, component):
            rows.append({"references": [ref], "status": "not_applicable", "reason": "DNP or documented exemption"})
            continue
        mfr = list(dict.fromkeys(identity_candidates(component, "manufacturer").values()))
        mpn = list(dict.fromkeys(identity_candidates(component, "part_number").values()))
        if len(mfr) != 1 or len(mpn) != 1 or re.fullmatch(r"C\d+", mpn[0], re.I):
            rows.append({"references": [ref], "status": "unknown", "reason": "Missing or conflicting manufacturer/MPN"})
            continue
        key = identity(mfr[0], mpn[0])
        entry = groups.setdefault(key, {"manufacturer": mfr[0], "mpn": mpn[0], "references": [], "codes": set()})
        entry["references"].append(ref)
        entry["codes"].update(identity_candidates(component, "jlcpcb").values())
        entry["codes"].update(identity_candidates(component, "lcsc").values())
    output.mkdir(parents=True, exist_ok=False)
    for index, entry in enumerate(groups.values()):
        codes = entry.pop("codes")
        quantity = len(entry["references"]) * args.boards
        if index >= args.max_parts:
            rows.append({**entry, "quantity": quantity, "status": "unknown", "reason": "Unique-part request budget reached"})
            continue
        relative = f"part-{index + 1:04d}"
        part_args = argparse.Namespace(command="parts-stock", mpn=entry["mpn"], manufacturer=entry["manufacturer"],
                                      quantity=quantity, min_stock=args.min_stock, max_age_hours=args.max_age_hours,
                                      jlc_code=next(iter(codes)) if len(codes) == 1 else None,
                                      output=str(output / relative), cache=args.cache or str(CACHE_ROOT / "sourcing"),
                                      online=args.online, dry_run=False, stock_import=args.stock_import,
                                      digikey_snapshot=None, max_stock_lookups=1)
        try:
            run_part(part_args)
            report = read_json(output / relative / "sourcing.json")
            rows.append({**entry, "quantity": quantity, "status": report["status"], "suppliers": report["suppliers"],
                         "report": relative + "/sourcing.md", "warnings": report["warnings"]})
        except (OSError, ValueError, KeyError, TypeError):
            rows.append({**entry, "quantity": quantity, "status": "unknown", "reason": "Part lookup/report failed; other BOM rows continued"})
    report = {"schema_version": 1, "mode": "bom_stock", "generated_at": utc_now(), "circuit_sha256": digest(args.circuit),
              "boards": args.boards, "results": rows,
              "scope": "Saved extraction only; no live schematic freshness guarantee. Quantities = populated references × boards; assembly wastage is not included. DigiKey availability determines row status; each supplier retains its own stock."}
    write_json(output / "bom-stock.json", report)
    lines = ["# BOM stock", "", report["scope"], "", "| References | Manufacturer | MPN | Quantity | Status | Details |",
             "| --- | --- | --- | --- | --- | --- |"]
    for row in rows:
        values = [", ".join(row["references"]), row.get("manufacturer", ""), row.get("mpn", ""), row.get("quantity", ""),
                  row["status"], f"[report]({row['report']})" if row.get("report") else row.get("reason", "")]
        lines.append("| " + " | ".join(str(v).replace("|", "\\|").replace("\n", " ") for v in values) + " |")
    (output / "bom-stock.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"BOM stock report: {output / 'bom-stock.md'}")
    return 0 if all(row["status"] in ("available", "not_applicable") for row in rows) and rows else 2
