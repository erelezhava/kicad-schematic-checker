"""Compare an STM32CubeMX .ioc pin configuration with the MCU's connections in the schematic.

    python3 -m kicad_checker mcu-pins CIRCUIT --ioc board.ioc --output DIR [--ref U1]

For every pin CubeMX assigns (signal and optional GPIO label) the schematic net on that pin is
compared, and every port pin the schematic connects is checked against CubeMX:

  fail          a configured pin sits on a power or ground net (a driven output there is a short)
  needs_review  configured in CubeMX but unconnected in the schematic
                net name does not resemble the CubeMX signal or label (a lead, not a verdict:
                e.g. UART TX/RX or SPI MISO/MOSI swapped, or just a different naming style)
                connected in the schematic to a signal net but not configured in CubeMX
                pin assigned in CubeMX that the schematic symbol does not have (package mismatch)
  pass          configured pin connected to a net whose name matches the signal or label
  info          unused port pin tied to a power or ground net (common for unused pins)

Only port pins (PA0..PK15) are compared; power, NRST, BOOT0 and VCAP are left to datasheet rules.
"""

from __future__ import annotations

import re
from pathlib import Path

from .core import read_json, write_json

PORT_PIN = re.compile(r"^P([A-K])(\d{1,2})")
POWER_NET = re.compile(r"(^|/)(GND\w*|VSS\w*|AGND|DGND|PGND|VDD\w*|VCC\w*|VBAT\w*|V?\+\d[\w.]*|\+\w+|\d+V\d*)$", re.I)
# Name variants treated as equal when comparing a net with a CubeMX signal or label.
SYNONYMS = [(r"QUADSPI", "QSPI"), (r"OCTOSPI", "OSPI"), (r"BANK", "BK"), (r"D\+|DP\b", "DP"),
            (r"D-|DM\b", "DM"), (r"JTMS-SWDIO|JTMS_SWDIO", "SWDIO"), (r"JTCK-SWCLK|JTCK_SWCLK", "SWCLK"),
            (r"NCS\b", "CS"), (r"NSS\b", "CS"), (r"SCK\b", "CLK"), (r"USART", "UART"), (r"LPUART", "UART")]


# --------------------------------------------------------------------------- .ioc

def _unescape(s: str) -> str:
    return re.sub(r"\\(.)", r"\1", s)


def read_ioc(path) -> dict:
    """{"mcu": {...}, "pins": {"PA11": {"signal", "mode", "label", "ioc_name"}}}."""
    values = {}
    for line in Path(path).read_text(encoding="utf-8", errors="replace").splitlines():
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        values[_unescape(k.strip())] = _unescape(v.strip())
    pins: dict[str, dict] = {}
    for k, v in values.items():
        name, _, attr = k.rpartition(".")
        m = PORT_PIN.match(name)
        if not m or attr not in ("Signal", "Mode", "GPIO_Label"):
            continue
        pin = f"P{m.group(1)}{int(m.group(2))}"
        entry = pins.setdefault(pin, {"ioc_name": name})
        entry[{"Signal": "signal", "Mode": "mode", "GPIO_Label": "label"}[attr]] = v
    pins = {p: e for p, e in pins.items() if e.get("signal")}
    mcu = {k: values.get(f"Mcu.{k}") for k in ("CPN", "Name", "UserName", "Package", "Family")}
    return {"mcu": mcu, "pins": pins, "path": str(path)}


def find_ioc(project_file) -> list[Path]:
    """.ioc files in the KiCad project folder and up to the repository root (max 3 levels)."""
    start = Path(project_file).resolve().parent
    found, folder = [], start
    for _ in range(4):
        found += [p for p in folder.rglob("*.ioc")
                  if not any(part.startswith(".") or re.search(r"_report_R\d+$", part)
                             for part in p.relative_to(folder).parts)]
        if (folder / ".git").exists() or folder.parent == folder:
            break
        folder = folder.parent
    return sorted(set(found))


# --------------------------------------------------------------------------- schematic side

def mcu_pattern(mcu: dict) -> re.Pattern | None:
    """STM32H743V(G-I)Hx -> STM32H743V[G-I]H.  (x = any character)."""
    name = mcu.get("Name") or mcu.get("UserName")
    if not name:
        return None
    p = re.sub(r"\(([A-Z0-9])-([A-Z0-9])\)", r"[\1-\2]", re.escape(name).replace(r"\(", "(").replace(r"\)", ")")
               .replace(r"\-", "-"))
    p = p.replace("x", ".")
    return re.compile(p, re.I)


def find_mcu(circuit: dict, ioc: dict, ref: str | None = None) -> str:
    comps = circuit["components"]
    if ref:
        if ref not in comps:
            raise ValueError(f"{ref} is not in the schematic")
        return ref
    cpn = (ioc["mcu"].get("CPN") or "").casefold()
    pat = mcu_pattern(ioc["mcu"])
    hits = [r for r, c in comps.items()
            if any((v or "").casefold() == cpn for v in (c.get("part_number"), c.get("value"))) and cpn]
    if not hits and pat:
        hits = [r for r, c in comps.items()
                if any(pat.match(v or "") for v in (c.get("part_number"), c.get("value"), c.get("symbol_part")))]
    if len(hits) != 1:
        raise ValueError(f"Cannot tell which part is the {ioc['mcu'].get('CPN') or ioc['mcu'].get('Name')} "
                         f"(found {hits or 'none'}); pass --ref")
    return hits[0]


def mcu_pins(circuit: dict, ref: str) -> dict:
    """{"PA11": {"ball": "C10", "symbol_name": "PA11", "net": "...", "connected": bool, "others": n}}."""
    comp = circuit["components"][ref]
    members = {n["name"]: len(n.get("nodes", [])) for n in circuit.get("nets", [])}
    out = {}
    for ball, info in (comp.get("symbol_pins") or {}).items():
        m = PORT_PIN.match(info.get("name") or "")
        if not m:
            continue
        pin = f"P{m.group(1)}{int(m.group(2))}"
        net = (comp.get("pins") or {}).get(ball, {}).get("net")
        connected = bool(net) and not net.startswith("unconnected-") and members.get(net, 1) > 1
        out[pin] = {"ball": ball, "symbol_name": info.get("name"), "net": net, "connected": connected,
                    "others": max(members.get(net, 1) - 1, 0) if net else 0}
    return out


# --------------------------------------------------------------------------- comparison

def normalize(text: str) -> str:
    t = (text or "").upper()
    t = t.rsplit("/", 1)[-1]
    for pat, rep in SYNONYMS:
        t = re.sub(pat, rep, t)
    return re.sub(r"[^A-Z0-9]+", "_", t).strip("_")


def tokens(text: str) -> set[str]:
    return {t for t in normalize(text).split("_") if t}


def name_match(net: str, signal: str, label: str | None) -> tuple[bool, str]:
    n = normalize(net)
    for what, ref in (("label", label), ("signal", signal)):
        if not ref:
            continue
        r = normalize(ref)
        nc, rc = n.replace("_", ""), r.replace("_", "")
        if (n == r or n.endswith("_" + r) or r.endswith("_" + n) or (len(r) > 3 and r in n)
                or nc == rc or (len(nc) > 3 and rc.endswith(nc)) or (len(rc) > 3 and rc in nc)):
            return True, f"net matches CubeMX {what}"
    # role tokens (TX/RX, SCL/SDA, MISO/MOSI, IO0..3, DM/DP ...) must agree when both sides name one
    roles = {"TX", "RX", "SCL", "SDA", "MISO", "MOSI", "CLK", "CS", "DM", "DP", "CTS", "RTS", "IO0", "IO1",
             "IO2", "IO3", "SWDIO", "SWCLK", "IN", "OUT"}
    nt = tokens(net) & roles
    st = (tokens(signal) | tokens(label or "")) & roles
    if nt and st and nt & st and (tokens(net) - roles) & (tokens(label or "") | tokens(signal)):
        return True, "net shares role and a name part with CubeMX"
    if nt and st and not nt & st:
        return False, f"role differs: net says {'/'.join(sorted(nt))}, CubeMX {'/'.join(sorted(st))}"
    return False, "net name does not resemble the CubeMX signal or label"


def compare(ioc: dict, pins: dict) -> list[dict]:
    rows = []
    for pin in sorted(set(ioc["pins"]) | set(pins), key=_pin_order):
        cfg, sch = ioc["pins"].get(pin), pins.get(pin)
        row = {"pin": pin, "ball": sch["ball"] if sch else None, "net": sch["net"] if sch else None,
               "signal": cfg.get("signal") if cfg else None, "label": cfg.get("label") if cfg else None,
               "mode": cfg.get("mode") if cfg else None}
        if cfg and not sch:
            row.update(status="needs_review", reason="CubeMX assigns this pin but the schematic symbol has no such "
                                                     "pin (different package or symbol?)")
        elif cfg and not sch["connected"]:
            row.update(status="needs_review", reason="configured in CubeMX, unconnected in the schematic")
        elif cfg and POWER_NET.search(sch["net"] or ""):
            row.update(status="fail", reason=f"configured as {cfg['signal']} but tied to {sch['net']}")
        elif cfg:
            ok, why = name_match(sch["net"], cfg["signal"], cfg.get("label"))
            row.update(status="pass" if ok else "needs_review", reason=why)
        elif sch["connected"] and POWER_NET.search(sch["net"] or ""):
            row.update(status="info", reason=f"not configured in CubeMX; tied to {sch['net']}")
        elif sch["connected"]:
            row.update(status="needs_review", reason="connected in the schematic, not configured in CubeMX")
        else:
            continue  # unused on both sides
        rows.append(row)
    return rows


def _pin_order(pin: str):
    m = PORT_PIN.match(pin)
    return (m.group(1), int(m.group(2))) if m else (pin, 0)


def check(circuit: dict, ioc_path, ref: str | None = None) -> dict:
    ioc = read_ioc(ioc_path)
    mcu = find_mcu(circuit, ioc, ref)
    rows = compare(ioc, mcu_pins(circuit, mcu))
    statuses = ("pass", "fail", "needs_review", "info")
    return {"schema_version": 1, "ioc": str(ioc_path), "mcu": ioc["mcu"], "reference": mcu,
            "scope": "Port pins only. Name comparison is a lead, not a verdict; power, reset, boot and VCAP pins "
                     "need datasheet rules.",
            "coverage": {s: sum(r["status"] == s for r in rows) for s in statuses}, "results": rows}


def markdown(report: dict) -> str:
    mcu = report["mcu"]
    lines = [f"# MCU pins: {report['reference']} vs CubeMX", "",
             f"- CubeMX file: `{report['ioc']}` ({mcu.get('CPN') or mcu.get('Name')}, {mcu.get('Package')})",
             f"- {report['scope']}", "",
             "| " + " | ".join(f"{k}: {v}" for k, v in report["coverage"].items()) + " |",
             "|" + " --- |" * len(report["coverage"]), ""]
    order = {"fail": 0, "needs_review": 1, "info": 2, "pass": 3}
    lines += ["| Status | Pin | Ball | CubeMX signal | CubeMX label | Schematic net | Note |",
              "| --- | --- | --- | --- | --- | --- | --- |"]
    for r in sorted(report["results"], key=lambda r: (order[r["status"]], _pin_order(r["pin"]))):
        cells = [r["status"], r["pin"], r["ball"] or "", r["signal"] or "", r["label"] or "",
                 f"`{r['net']}`" if r["net"] and r["net"].strip() else "", r["reason"]]
        lines.append("| " + " | ".join(str(c).replace("|", "/") for c in cells) + " |")
    return "\n".join(lines) + "\n"


def write(report: dict, out: Path) -> int:
    out.mkdir(parents=True, exist_ok=False)
    write_json(out / "mcu-pins.json", report)
    (out / "mcu-pins.md").write_text(markdown(report), encoding="utf-8")
    cov = report["coverage"]
    print("MCU pins: " + ", ".join(f"{k}={v}" for k, v in cov.items() if v))
    print(f"Report: {out / 'mcu-pins.md'}")
    return 1 if cov["fail"] else 2 if cov["needs_review"] else 0


def add_command(sub) -> None:
    c = sub.add_parser("mcu-pins", help="Compare the STM32CubeMX .ioc pin configuration with the MCU's schematic nets")
    c.add_argument("circuit", help="circuit.json from extract")
    c.add_argument("--ioc", required=True, help="CubeMX .ioc file")
    c.add_argument("--ref", help="MCU reference (default: the part matching the .ioc MCU)")
    c.add_argument("--output", required=True)


def run(args) -> int:
    return write(check(read_json(args.circuit), args.ioc, args.ref), Path(args.output).resolve())
