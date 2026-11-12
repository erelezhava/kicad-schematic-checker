"""`python3 -m kicad_checker run PROJECT`: every check in one go, results inside the project.

Creates `<project>_report_R<n>/` next to the `.kicad_pro` (R0 first, then R1, R2 ...; an
existing report is never touched) with one sub-folder per check, `run.log` (everything printed,
every kicad-cli call, versions, timings, exit codes, tracebacks; secrets masked), `summary.md`
and `run.json`.

Steps (choose with --checks):
  schematic  extract: Manufacturer/MPN/Description audit, ERC        -> 01_schematic/
  digikey    digikey-fetch (if keys and not --offline) + digikey-check -> 02_digikey/
  jlcpcb     JLC rules generated for this stackup, preflight, and DRC with those rules on a
             temporary copy (the project's own .kicad_dru is never written) -> 03_jlcpcb/
  refplane   reference-plane check (uses <board>.refplane.json when saved) -> 04_refplane/
  mcu_pins   STM32CubeMX .ioc pin configuration vs the MCU's schematic nets  -> 05_mcu_pins/
  datasheets datasheet PDF per active part: project docs, cache, DigiKey link -> 06_datasheets/

The design files are only read. Exit code: 0 nothing open, 1 a check failed, 2 open items
(needs review / owner actions), 3 a step could not run. 0 never means "design approved".
"""

from __future__ import annotations

import argparse
import datetime as _dt
import io
import json
import logging
import platform
import re
import shutil
import subprocess
import sys
import tempfile
import time
import traceback
from collections import Counter
from pathlib import Path

from . import credentials
from .core import digest, read_json, write_json
from .paths import CHECKER_ROOT, search_folders

STEPS = ("schematic", "digikey", "jlcpcb", "refplane", "mcu_pins", "datasheets")
FOLDERS = {"schematic": "01_schematic", "digikey": "02_digikey", "jlcpcb": "03_jlcpcb", "refplane": "04_refplane",
           "mcu_pins": "05_mcu_pins", "datasheets": "06_datasheets"}
EXIT_TEXT = {0: "ok", 1: "fail", 2: "open items", 3: "error", None: "not run"}


class Blocked(Exception):
    """A step that cannot start because an earlier step did not produce its input."""
log = logging.getLogger("kicad_checker")


# --------------------------------------------------------------------------- project / folders

def _is_report_dir(path: Path) -> bool:
    return re.search(r"_report_R\d+$", path.name) is not None


def find_project(path) -> Path:
    """The .kicad_pro to check: given directly, via its .kicad_sch/.kicad_pcb, or the one in a folder."""
    path = Path(path).expanduser().resolve()
    if path.is_dir():
        found = sorted(p for p in path.rglob("*.kicad_pro")
                       if not any(part == ".history" or _is_report_dir(Path(part)) for part in p.relative_to(path).parts))
        if len(found) != 1:
            listing = ", ".join(str(p.relative_to(path)) for p in found) or "none"
            raise ValueError(f"Expected one .kicad_pro under {path}, found {len(found)} ({listing}). Pass the .kicad_pro.")
        return found[0]
    if path.suffix in (".kicad_sch", ".kicad_pcb"):
        path = path.with_suffix(".kicad_pro")
    if path.suffix != ".kicad_pro" or not path.is_file():
        raise ValueError(f"Not a KiCad project: {path}")
    return path


def next_report_dir(pro: Path) -> Path:
    """<stem>_report_R<n> next to the project; n = highest existing + 1, starting at 0."""
    pattern = re.compile(rf"^{re.escape(pro.stem)}_report_R(\d+)$")
    revs = [int(m.group(1)) for p in pro.parent.iterdir() if p.is_dir() and (m := pattern.match(p.name))]
    n = max(revs) + 1 if revs else 0
    while True:
        target = pro.parent / f"{pro.stem}_report_R{n}"
        try:
            target.mkdir()
            return target
        except FileExistsError:  # created by a parallel run in the meantime
            n += 1


# --------------------------------------------------------------------------- logging

def _secrets() -> list[str]:
    """Values of every configured credential (known names plus .env.example), longest first."""
    names = set(credentials.KNOWN_KEYS)
    example = CHECKER_ROOT / ".env.example"
    if example.is_file():
        for line in example.read_text(encoding="utf-8").splitlines():
            m = re.match(r"\s*#?\s*([A-Z][A-Z0-9_]+)\s*=", line)
            if m:
                names.add(m.group(1))
    values = {v for n in names if (v := credentials.get(n)) and len(v) >= 4}
    return sorted(values, key=len, reverse=True)


class RunLog:
    """run.log writer: timestamped lines, secret values replaced by ***."""

    def __init__(self, path: Path):
        self.path = path
        self.file = path.open("w", encoding="utf-8")
        self.secrets = _secrets()
        self.step = "run"

    def mask(self, text: str) -> str:
        for s in self.secrets:
            text = text.replace(s, "***")
        return text

    def line(self, text: str, tag: str = "info") -> None:
        stamp = _dt.datetime.now().strftime("%H:%M:%S.%f")[:-3]
        for part in self.mask(str(text)).splitlines() or [""]:
            self.file.write(f"{stamp} [{self.step}] {tag:6} {part}\n")
        self.file.flush()

    def close(self) -> None:
        self.file.close()


class _Tee(io.TextIOBase):
    """Console stream that also writes every complete line (and prompts, on flush) to run.log."""

    def __init__(self, original, runlog: RunLog, tag: str):
        self.original, self.runlog, self.tag, self.buffer_ = original, runlog, tag, ""

    def write(self, s):
        self.original.write(s)
        self.buffer_ += s
        while "\n" in self.buffer_:
            line, self.buffer_ = self.buffer_.split("\n", 1)
            self.runlog.line(line, self.tag)
        return len(s)

    def flush(self):
        self.original.flush()
        if self.buffer_:  # a prompt from input(): log it now, the answer follows on the console
            self.runlog.line(self.buffer_, self.tag)
            self.buffer_ = ""

    def isatty(self):
        return self.original.isatty()

    def fileno(self):
        return self.original.fileno()


class _LogHandler(logging.Handler):
    def __init__(self, runlog: RunLog):
        super().__init__(logging.DEBUG)
        self.runlog = runlog

    def emit(self, record):
        self.runlog.line(record.getMessage(), record.levelname.lower()[:6])


# --------------------------------------------------------------------------- environment

def _kicad_cli() -> str | None:
    from refplane.cli import find_kicad_cli
    return find_kicad_cli(None)


def _version(cmd: list[str]) -> str | None:
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        return (r.stdout or r.stderr).strip() or None
    except (OSError, subprocess.TimeoutExpired):
        return None


def environment() -> dict:
    cli = _kicad_cli()
    return {
        "python": sys.version.split()[0],
        "platform": platform.platform(),
        "checker_folder": str(CHECKER_ROOT),
        "checker_commit": _version(["git", "-C", str(CHECKER_ROOT), "rev-parse", "--short", "HEAD"]),
        "checker_dirty": bool(_version(["git", "-C", str(CHECKER_ROOT), "status", "--porcelain"])),
        "kicad_cli": cli,
        "kicad_version": _version([cli, "version"]) if cli else None,
        "digikey_keys": "present" if all(credentials.get(n) for n in ("DIGIKEY_CLIENT_ID", "DIGIKEY_CLIENT_SECRET")) else "missing",
    }


# --------------------------------------------------------------------------- steps

def step_schematic(ctx) -> tuple[int, list[str]]:
    from . import cli
    out = ctx["root"] / FOLDERS["schematic"]
    ns = argparse.Namespace(project=str(ctx["pro"].with_suffix(".kicad_sch")), output=str(out))
    print(f"$ python3 -m kicad_checker extract {ns.project} --output {out}")
    cli.extract(ns, allow_inside_project=True)
    ident = read_json(out / "identification.json")["coverage"]
    desc = read_json(out / "description.json")["coverage"]
    erc = read_json(out / "erc-summary.json")
    erc_err = sum(n for k, n in erc["counts"].items() if k.startswith("error:"))
    erc_warn = sum(n for k, n in erc["counts"].items() if k.startswith("warning:"))
    notes = [f"identification: {_fmt(ident)}", f"description: {_fmt(desc)}",
             f"ERC: {erc_err} errors, {erc_warn} warnings", "start with owner-actions.md"]
    code = 2 if (ident.get("needs_review") or desc.get("needs_review") or desc.get("fail") or erc_err or erc_warn) else 0
    return (1 if desc.get("fail") else code), notes


def step_digikey(ctx) -> tuple[int, list[str]]:
    from . import cli
    circuit = ctx["root"] / FOLDERS["schematic"] / "circuit.json"
    if not circuit.is_file():
        raise Blocked("not run: needs 01_schematic/circuit.json from the schematic step")
    notes = []
    if ctx["offline"]:
        notes.append("--offline: used the DigiKey cache only")
    elif environment_keys():
        print(f"$ python3 -m kicad_checker digikey-fetch {circuit}")
        code = cli.main(["digikey-fetch", str(circuit)])
        if code:
            notes.append(f"digikey-fetch exit {code}: some lookups failed, see run.log")
    else:
        notes.append("DigiKey keys missing (.env): fetch skipped, cache only; uncached parts stay not_checked")
    out = ctx["root"] / FOLDERS["digikey"]
    print(f"$ python3 -m kicad_checker digikey-check {circuit} --output {out}")
    code = cli.main(["digikey-check", str(circuit), "--output", str(out)])
    if (out / "digikey.json").is_file():
        notes.insert(0, "DigiKey: " + _fmt(read_json(out / "digikey.json")["coverage"]))
        notes += _fill_hint(ctx, out / "digikey.json")
    return code, notes


def _fill_hint(ctx, report_file: Path) -> list[str]:
    """How many empty Manufacturer fields DigiKey can fill, and the command (nothing is written)."""
    from . import fill
    try:
        _plans, lines, total = fill.plan(ctx["pro"], read_json(report_file))
    except (OSError, ValueError) as exc:
        log.info("fill-manufacturer preview failed: %s", exc)
        return []
    skipped = sum(1 for l in lines if "choose one with --prefer" in l)
    print(f"fill-manufacturer: {total} empty Manufacturer fields can be filled from DigiKey"
          + (f", {skipped} need a choice" if skipped else ""))
    if not total:
        return [f"{skipped} Manufacturer fields need a choice (fill-manufacturer --prefer)"] if skipped else []
    return [f"{total} empty Manufacturer fields can be filled from DigiKey"
            + (f" ({skipped} need --prefer)" if skipped else "")
            + f": `python3 -m kicad_checker fill-manufacturer \"{ctx['pro']}\"` (preview), then `--apply` with KiCad closed"]


def environment_keys() -> bool:
    return all(credentials.get(n) for n in ("DIGIKEY_CLIENT_ID", "DIGIKEY_CLIENT_SECRET"))


def step_jlcpcb(ctx) -> tuple[int, list[str]]:
    from jlcpcb_rules import cli as jcli, preflight
    from jlcpcb_rules.board import read_board
    pcb = ctx["pro"].with_suffix(".kicad_pcb")
    if not pcb.is_file():
        raise ValueError(f"no board file {pcb.name}")
    out = ctx["root"] / FOLDERS["jlcpcb"]
    out.mkdir()
    rules = out / f"{pcb.stem}.kicad_dru"
    jlc_args = ["--tier", ctx["tier"]] + (["--accept", ctx["accept"]] if ctx["accept"] else [])
    print(f"$ python3 -m jlcpcb_rules generate --board {pcb} -o {rules} {' '.join(jlc_args)}")
    jcli.main(["generate", "--board", str(pcb), "-o", str(rules), *jlc_args])
    # preflight: board-level checks; staleness is judged against the rules installed in the project
    p = jcli._profile(jcli.argparse.Namespace(layers=None, outer_oz=None, inner_oz=None, thickness=None, finish=None,
                                              mask_color=None, tier=ctx["tier"], accept=ctx["accept"]), pcb)
    findings = preflight.run(read_board(pcb), p, rules_path=pcb.with_suffix(".kicad_dru"))
    text = preflight.format_text(findings, p)
    print(text)
    (out / "preflight.txt").write_text(text + "\n", encoding="utf-8")
    write_json(out / "preflight.json", {"board": str(pcb), "profile": json.loads(p.to_json()),
                                        "detected": p.notes, "findings": preflight.to_dicts(findings)})
    sev = Counter(f.severity for f in findings)
    notes = [f"preflight: {sev.get('error', 0)} errors, {sev.get('warning', 0)} warnings",
             f"rules for this stackup: {rules.name} (install with `python3 -m jlcpcb_rules install` when you want them in KiCad)"]
    code = 1 if sev.get("error") else 0
    drc_code, drc_notes = _drc(pcb, rules, out)
    return max(code, drc_code), notes + drc_notes


PROJECT_TABLES = ("fp-lib-table", "sym-lib-table")


def copy_for_drc(pcb: Path, rules: Path, tmp: Path) -> Path:
    """Copy what DRC needs into `tmp` and put the JLC rules next to the copy.

    The project's library tables come along with ${KIPRJMOD} replaced by the real project
    folder, so project-local footprint libraries still resolve from the temporary copy and
    KiCad's library checks report the board, not the missing folder.
    """
    for suffix in (".kicad_pcb", ".kicad_pro", ".kicad_prl"):
        if pcb.with_suffix(suffix).is_file():
            shutil.copy2(pcb.with_suffix(suffix), tmp / (pcb.stem + suffix))
    project_dir = pcb.parent.resolve().as_posix()
    for name in PROJECT_TABLES:
        table = pcb.parent / name
        if table.is_file():
            text = table.read_text(encoding="utf-8", errors="replace")
            text = text.replace("${KIPRJMOD}", project_dir).replace("$(KIPRJMOD)", project_dir)
            (tmp / name).write_text(text, encoding="utf-8")
    shutil.copy2(rules, tmp / (pcb.stem + ".kicad_dru"))
    return tmp / pcb.name


def summarize_drc(data: dict) -> dict:
    """Group kicad-cli DRC violations into one row per rule. Violations the designer excluded in
    KiCad are counted apart and never as errors or warnings."""
    rows, worst, excluded = Counter(), {}, 0
    for v in data.get("violations", []):
        if v.get("excluded") or v.get("severity") == "exclusion":
            excluded += 1
            continue
        desc = v.get("description", "")
        m = re.search(r";?\s*actual (-?[0-9.]+) mm", desc)
        desc = re.sub(r";?\s*actual -?[0-9.]+ mm", "", desc)  # one row per rule, not per measured value
        k = (v.get("severity", "?"), v.get("type", "?"), desc)
        rows[k] += 1
        if m:
            worst[k] = min(worst.get(k, float("inf")), float(m.group(1)))
    unconnected = [u for u in data.get("unconnected_items", []) if not u.get("excluded")]
    excluded += len(data.get("unconnected_items", [])) - len(unconnected)
    sev = Counter()
    for (s, _, _), n in rows.items():
        sev[s] += n
    return {"rows": rows, "worst": worst, "severity": sev, "unconnected": len(unconnected), "excluded": excluded}


def _drc(pcb: Path, rules: Path, out: Path) -> tuple[int, list[str]]:
    cli = _kicad_cli()
    if not cli:
        return 2, ["DRC not run: kicad-cli not found (put it on PATH or set KICAD_CLI)"]
    with tempfile.TemporaryDirectory(prefix="kicad-drc-") as tmp:
        board = copy_for_drc(pcb, rules, Path(tmp))
        cmd = [cli, "pcb", "drc", "--severity-all", "--format", "json", "--output", str(out / "drc.json"), str(board)]
        _logged_run(cmd, timeout=600)
    if not (out / "drc.json").is_file():
        return 3, ["DRC failed: kicad-cli wrote no report, see run.log"]
    summary = summarize_drc(read_json(out / "drc.json"))
    rows, worst, sev, unconnected = summary["rows"], summary["worst"], summary["severity"], summary["unconnected"]
    lines = ["# DRC with JLCPCB rules", "",
             f"Board: `{pcb.name}`, rules: `{rules.name}` (applied to a temporary copy; the project was not changed).", "",
             f"- errors: {sev.get('error', 0)}", f"- warnings: {sev.get('warning', 0)}", f"- unconnected items: {unconnected}"]
    if summary["excluded"]:
        lines.append(f"- excluded in KiCad by the designer (not counted above): {summary['excluded']}")
    lines += ["", "| Severity | Type | Description | Count | Worst actual |", "| --- | --- | --- | --- | --- |"]
    order = {"error": 0, "warning": 1}
    for (s, t, d), n in sorted(rows.items(), key=lambda kv: (order.get(kv[0][0], 2), -kv[1])):
        w = f"{worst[(s, t, d)]:g} mm" if (s, t, d) in worst else ""
        lines.append(f"| {s} | {t} | {d.replace('|', '/')} | {n} | {w} |")
    (out / "drc.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"DRC: {sev.get('error', 0)} errors, {sev.get('warning', 0)} warnings, {unconnected} unconnected")
    code = 1 if sev.get("error") else 2 if (sev.get("warning") or unconnected) else 0
    return code, [f"DRC (JLC rules): {sev.get('error', 0)} errors, {sev.get('warning', 0)} warnings, {unconnected} unconnected"
                  + (f", {summary['excluded']} excluded" if summary["excluded"] else "")]


def step_refplane(ctx) -> tuple[int, list[str]]:
    from refplane import cli as rcli
    from refplane.settings import board_config_path
    pcb = ctx["pro"].with_suffix(".kicad_pcb")
    if not pcb.is_file():
        raise ValueError(f"no board file {pcb.name}")
    out = ctx["root"] / FOLDERS["refplane"]
    argv = ["check", str(pcb), "-o", str(out)] + (["-y"] if ctx["yes"] else []) + ctx["refplane_args"]
    saved = board_config_path(pcb)
    if saved.is_file():
        print(f"using saved answers {saved.name}")
    elif ctx["yes"] or not sys.stdin.isatty():
        print("no saved refplane answers and no terminal: using guessed layer roles and nets (see report.md 'assumed')")
    print("$ python3 -m refplane " + " ".join(argv))
    code = rcli.main(argv) or 0
    notes = []
    if (out / "report.json").is_file():
        data = read_json(out / "report.json")
        sev = Counter(f.get("severity") for f in data.get("findings", []))
        notes.append(f"refplane: {sev.get('error', 0)} errors, {sev.get('warning', 0)} warnings")
        notes += [f"assumed: {a}" for a in data.get("setup", {}).get("assumptions", [])]
        if code == 0 and sev.get("warning"):
            code = 2
    return (code if code in (0, 1, 2) else 3), notes


def _logged_run(cmd: list[str], timeout: int) -> subprocess.CompletedProcess:
    t0 = time.monotonic()
    log.info("exec: %s", " ".join(cmd))
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    log.info("exit %s after %.1f s", r.returncode, time.monotonic() - t0)
    for name, text in (("stdout", r.stdout), ("stderr", r.stderr)):
        if text.strip():
            log.debug("%s:\n%s", name, text.rstrip())
    return r


def _fmt(coverage: dict) -> str:
    return ", ".join(f"{k}={v}" for k, v in coverage.items() if v)  or "nothing to check"


def _circuit(ctx) -> Path:
    circuit = ctx["root"] / FOLDERS["schematic"] / "circuit.json"
    if not circuit.is_file():
        raise Blocked("not run: needs 01_schematic/circuit.json from the schematic step")
    return circuit


def step_mcu_pins(ctx) -> tuple[int, list[str]]:
    from . import cubemx
    circuit = _circuit(ctx)
    if ctx.get("ioc"):
        ioc = Path(ctx["ioc"]).expanduser().resolve()
    else:
        found = cubemx.find_ioc(ctx["pro"])
        if not found:
            raise Blocked("not run: no CubeMX .ioc found in the project or repository (pass --ioc)")
        if len(found) > 1:
            raise Blocked("not run: several .ioc files (" + ", ".join(p.name for p in found) + "); pass --ioc")
        ioc = found[0]
    out = ctx["root"] / FOLDERS["mcu_pins"]
    print(f"$ python3 -m kicad_checker mcu-pins {circuit} --ioc {ioc} --output {out}")
    code = cubemx.write(cubemx.check(read_json(circuit), ioc), out)
    report = read_json(out / "mcu-pins.json")
    notes = [f"{report['reference']} vs {ioc.name}: {_fmt(report['coverage'])}"]
    notes += [f"{r['pin']}: {r['reason']}" for r in report["results"] if r["status"] == "fail"][:5]
    return code, notes


def step_datasheets(ctx) -> tuple[int, list[str]]:
    from . import datasheets
    circuit = _circuit(ctx)
    folders = []
    for folder in search_folders(ctx["pro"].parent):  # docs/ next to the project and up to the repository root
        folders += datasheets.project_doc_folders(folder)
    out = ctx["root"] / FOLDERS["datasheets"]
    print(f"$ python3 -m kicad_checker datasheet-fetch {circuit} --output {out}"
          + "".join(f" --docs {f}" for f in folders) + (" --offline" if ctx["offline"] else ""))
    report = datasheets.find_datasheets(read_json(circuit), doc_folders=folders, offline=ctx["offline"])
    out.mkdir()
    write_json(out / "datasheets.json", report)
    (out / "datasheets.md").write_text(datasheets.datasheets_markdown(report), encoding="utf-8")
    notes = ["datasheets: " + _fmt(report["coverage"]),
             "docs searched: " + (", ".join(Path(f).name + "/" for f in folders) or "none (cache and DigiKey only)")]
    missing = [r["mpn"] for r in report["results"] if r["status"] != "found"]
    if missing:
        notes.append("missing: " + ", ".join(missing[:8]) + (" ..." if len(missing) > 8 else ""))
    weak = [r["mpn"] for r in report["results"] if r.get("verify")]
    if weak:
        notes.append(f"{len(weak)} matched by document text or family name only, confirm it is the right PDF: "
                     + ", ".join(weak[:8]) + (" ..." if len(weak) > 8 else ""))
    return (0 if not missing else 2), notes


RUNNERS = {"schematic": step_schematic, "digikey": step_digikey, "jlcpcb": step_jlcpcb, "refplane": step_refplane,
           "mcu_pins": step_mcu_pins, "datasheets": step_datasheets}


# --------------------------------------------------------------------------- orchestration

def run(args) -> int:
    pro = find_project(args.project)
    checks = [c.strip() for c in args.checks.split(",") if c.strip()]
    unknown = [c for c in checks if c not in STEPS]
    if unknown:
        raise ValueError(f"unknown checks {unknown}; choose from {','.join(STEPS)}")
    inputs = [p for p in (pro, pro.with_suffix(".kicad_sch"), pro.with_suffix(".kicad_pcb")) if p.is_file()]
    if pro.with_suffix(".kicad_sch").is_file():
        from .schematic import design_files
        inputs = sorted(set(inputs) | set(design_files(pro.with_suffix(".kicad_sch"))))
    root = next_report_dir(pro)
    runlog = RunLog(root / "run.log")
    handler = _LogHandler(runlog)
    log.addHandler(handler)
    log.setLevel(logging.DEBUG)
    started = _dt.datetime.now()
    env = environment()
    record = {"project": str(pro), "report": str(root), "started": started.isoformat(timespec="seconds"),
              "command": " ".join(["python3", "-m", "kicad_checker", *sys.argv[1:]]) if sys.argv else "",
              "checks": checks, "environment": env,
              "inputs": {str(p): digest(p) for p in inputs}, "steps": []}
    ctx = {"pro": pro, "root": root, "offline": args.offline, "yes": args.yes, "tier": args.tier,
           "accept": args.accept, "refplane_args": args.refplane_args or [], "ioc": args.ioc}
    old_out, old_err = sys.stdout, sys.stderr
    sys.stdout, sys.stderr = _Tee(old_out, runlog, "out"), _Tee(old_err, runlog, "err")
    try:
        print(f"Project: {pro}")
        print(f"Report:  {root}")
        runlog.line("command: " + record["command"])
        for k, v in env.items():
            runlog.line(f"{k}: {v}")
        for p, sha in record["inputs"].items():
            runlog.line(f"input {Path(p).name} sha256={sha}")
        for name in STEPS:
            if name not in checks:
                continue
            runlog.step = name
            print(f"\n=== {name} ===")
            t0 = time.monotonic()
            code, notes, error = None, [], None
            try:
                code, notes = RUNNERS[name](ctx)
            except Blocked as exc:
                code, notes = None, [str(exc)]
                print(exc)
            except SystemExit as exc:  # the sub-tools report usage/errors this way
                code = exc.code if isinstance(exc.code, int) else 3
                error = None if isinstance(exc.code, int) else str(exc.code)
            except Exception as exc:  # noqa: BLE001 - one broken step must not stop the others
                code, error = 3, f"{type(exc).__name__}: {exc}"
                runlog.line(traceback.format_exc(), "trace")
            if error:
                print(f"Error: {error}", file=sys.stderr)
                notes = [error, *notes]
            elapsed = time.monotonic() - t0
            runlog.line(f"step finished: exit {code} ({EXIT_TEXT.get(code, code)}) in {elapsed:.1f} s")
            record["steps"].append({"step": name, "folder": FOLDERS[name], "exit": code,
                                    "status": EXIT_TEXT.get(code, str(code)), "seconds": round(elapsed, 1),
                                    "notes": notes})
        runlog.step = "run"
        changed = [p for p, sha in record["inputs"].items() if not Path(p).is_file() or digest(p) != sha]
        if changed:
            print("Warning: design files changed while the checks ran: " + ", ".join(Path(p).name for p in changed)
                  + ". Save and run again.", file=sys.stderr)
        record["changed_during_run"] = changed
        record["finished"] = _dt.datetime.now().isoformat(timespec="seconds")
        codes = [s["exit"] for s in record["steps"]]
        overall = 3 if 3 in codes else 1 if 1 in codes else 2 if (2 in codes or changed) else 0
        record["exit"] = overall
        write_json(root / "run.json", record)
        (root / "summary.md").write_text(summary_markdown(record), encoding="utf-8")
        print(f"\nOverall: exit {overall} ({EXIT_TEXT[overall]}). Summary: {root / 'summary.md'}")
        runlog.line(f"run finished: exit {overall}")
        return overall
    finally:
        sys.stdout, sys.stderr = old_out, old_err
        log.removeHandler(handler)
        runlog.close()


def summary_markdown(record: dict) -> str:
    pro = Path(record["project"])
    env = record["environment"]
    lines = [f"# {pro.stem}: {Path(record['report']).name}", "",
             f"- Project: `{pro}`",
             f"- Started {record['started']}, finished {record['finished']}",
             f"- Checker {env.get('checker_commit') or 'unknown'}{' (uncommitted changes)' if env.get('checker_dirty') else ''}, "
             f"KiCad {env.get('kicad_version') or 'not found'}, Python {env['python']}",
             f"- Overall exit **{record['exit']} ({EXIT_TEXT[record['exit']]})**. Exit 0 means nothing open in what was "
             "checked, never that the design is approved.", "",
             "| Check | Result | Folder | Notes |", "| --- | --- | --- | --- |"]
    for s in record["steps"]:
        notes = "<br>".join(n.replace("|", "/") for n in s["notes"])
        folder = f"[{s['folder']}/]({s['folder']}/)" if (Path(record["report"]) / s["folder"]).is_dir() else "—"
        result = s["status"] if s["exit"] is None else f"{s['status']} (exit {s['exit']})"
        lines.append(f"| {s['step']} | {result} | {folder} | {notes} |")
    if record.get("changed_during_run"):
        lines += ["", "**Design files changed during the run; results may be inconsistent. Save and run again.**"]
    lines += ["", "Full log: [run.log](run.log). Machine-readable: [run.json](run.json).", "",
              "Where to look first: `01_schematic/owner-actions.md`, `02_digikey/digikey.md`, `03_jlcpcb/drc.md` "
              "and `preflight.txt`, `04_refplane/report.md` (+ `.svg` per layer), `05_mcu_pins/mcu-pins.md`, "
              "`06_datasheets/datasheets.md`."]
    return "\n".join(lines) + "\n"


def add_command(sub) -> None:
    c = sub.add_parser("run", help="Run all checks; results go to <project>_report_R<n>/ next to the .kicad_pro")
    c.add_argument("project", help=".kicad_pro / .kicad_sch / .kicad_pcb, or a folder containing one project")
    c.add_argument("--checks", default=",".join(STEPS), help=f"comma list (default all): {','.join(STEPS)}")
    c.add_argument("--offline", action="store_true", help="no DigiKey requests; use the cache only")
    c.add_argument("-y", "--yes", action="store_true", help="no questions (refplane uses saved answers or guesses)")
    c.add_argument("--tier", default="tiered", choices=("tiered", "standard", "advanced"), help="JLC rule strictness")
    c.add_argument("--accept", default="", help="JLC paid options you accept: small_via,hdi,via_in_pad,backdrill")
    c.add_argument("--ioc", help="STM32CubeMX .ioc for the mcu_pins check (default: the one .ioc in the project/repo)")
    c.add_argument("--refplane-args", nargs=argparse.REMAINDER,
                   help="extra refplane check flags, must come last, e.g. --refplane-args --layers sig,gnd,gnd,sig")
