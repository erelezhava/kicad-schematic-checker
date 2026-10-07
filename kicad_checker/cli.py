import argparse
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from . import digikey
from .description import description_audit, description_markdown, owner_actions, owner_actions_markdown
from .core import digest, erc_summary, identification, identification_markdown, markdown, parse_netlist, read_json, review, write_json


def new_output(path):
    path = Path(path).resolve()
    path.mkdir(parents=True, exist_ok=False)
    return path


def run_kicad(executable, args):
    result = subprocess.run([executable, *args], capture_output=True, text=True, timeout=120)
    if result.returncode:
        raise ValueError(f"KiCad command failed ({result.returncode}): {result.stderr or result.stdout}")
    return result.stdout.strip()


def write_identification(circuit, out):
    """Default metadata audits written by extract/import: identification, description, owner actions."""
    report = identification(circuit)
    write_json(out / "identification.json", report)
    (out / "identification.md").write_text(identification_markdown(report), encoding="utf-8")
    described = description_audit(circuit)
    write_json(out / "description.json", described)
    (out / "description.md").write_text(description_markdown(described), encoding="utf-8")
    print("Component identification: " + ", ".join(f"{s}={n}" for s, n in report["coverage"].items()))
    print("Component description: " + ", ".join(f"{s}={n}" for s, n in described["coverage"].items()))
    write_owner_actions(report, described, out)


def write_owner_actions(identified, described, out, digikey_report=None):
    actions = owner_actions(identified, described, digikey_report)
    (out / "owner-actions.md").write_text(owner_actions_markdown(actions), encoding="utf-8")
    if actions:
        print(f"Owner actions: {len(actions)} components need fields filled or corrected — see {out / 'owner-actions.md'}")


def extract(args):
    source = Path(args.project).resolve()
    if source.suffix == ".kicad_pro":
        source = source.with_suffix(".kicad_sch")
    if source.suffix != ".kicad_sch" or not source.is_file():
        raise ValueError("Provide a root .kicad_sch or matching .kicad_pro file")
    output = Path(args.output).resolve()
    if output == source.parent or source.parent in output.parents:
        raise ValueError("Choose an output directory outside the source project")
    executable = shutil.which("kicad-cli")
    if not executable:
        raise ValueError("Install KiCad and put kicad-cli on PATH")
    # Record project inputs to detect edits during extraction and stale reviews.
    inputs = sorted(set(source.parent.rglob("*.kicad_sch")) | set(source.parent.glob("*.kicad_pro")))
    hashes = {str(p): digest(p) for p in inputs}
    with tempfile.TemporaryDirectory(prefix="kicad-review-") as tmp:
        tmp = Path(tmp)
        version = run_kicad(executable, ["version"])
        run_kicad(executable, ["sch", "export", "netlist", "--format", "kicadxml", "--output", str(tmp / "netlist.xml"), str(source)])
        run_kicad(executable, ["sch", "erc", "--format", "json", "--severity-all", "--output", str(tmp / "erc.json"), str(source)])
        if any(digest(p) != sha for p, sha in hashes.items()):
            raise ValueError("Project changed during extraction; save it and retry")
        circuit = parse_netlist(tmp / "netlist.xml")
        circuit["project_inputs"] = hashes
        circuit["extraction_version"] = version
        circuit["scope"] = "Saved default assembly only. Nets are not collapsed across components. ERC uses project settings and installed libraries."
        summary = erc_summary(read_json(tmp / "erc.json"))
        out = new_output(output)
        shutil.copy2(tmp / "netlist.xml", out / "netlist.xml")
        shutil.copy2(tmp / "erc.json", out / "erc.json")
        write_json(out / "circuit.json", circuit)
        write_json(out / "erc-summary.json", summary)
        write_identification(circuit, out)
        print(f"Extracted {len(circuit['components'])} components and {len(circuit['nets'])} nets with KiCad {version}.")
        print(f"ERC: {len(summary['findings'])} findings; {len(summary['ignored_checks'])} disabled check categories.")
        print(f"Saved to {out}")
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description="KiCad schematic checker v0.1 — portable evidence-based review")
    sub = parser.add_subparsers(dest="command", required=True)
    command = sub.add_parser("extract", help="Export saved project connectivity and ERC without editing the design")
    command.add_argument("project")
    command.add_argument("--output", required=True, help="New directory outside the source project")
    command = sub.add_parser("import-netlist", help="Import an existing KiCad XML netlist (offline snapshot)")
    command.add_argument("netlist")
    command.add_argument("--output", required=True)
    command = sub.add_parser("review", help="Evaluate all supplied rules and produce JSON and Markdown")
    command.add_argument("circuit")
    command.add_argument("--rules", required=True)
    command.add_argument("--evidence", required=True)
    command.add_argument("--output", required=True)
    command.add_argument("--snapshot", action="store_true", help="Explicitly review a saved snapshot without verifying live project hashes")
    command.add_argument("--docs-root", help="Resolve relative source.path values against this folder instead of the rules/evidence file folders")
    command.add_argument("--digikey-cache", help="Also cross-check against cached DigiKey data in this folder (run digikey-fetch first)")
    command = sub.add_parser("digikey-fetch", help="Look up every MPN of a circuit on DigiKey and cache the results (needs credentials)")
    command.add_argument("circuit")
    command.add_argument("--cache", default=str(digikey.DEFAULT_CACHE), help=f"Cache folder (default {digikey.DEFAULT_CACHE})")
    command.add_argument("--refresh", action="store_true", help="Query again even when a cached entry exists")
    command = sub.add_parser("digikey-check", help="Compare cached DigiKey data with the schematic (offline)")
    command.add_argument("circuit")
    command.add_argument("--cache", default=str(digikey.DEFAULT_CACHE))
    command.add_argument("--output", required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "extract":
            return extract(args)
        if args.command == "import-netlist":
            circuit = parse_netlist(args.netlist)
            circuit["scope"] = "Imported offline netlist; no live project freshness guarantee or ERC report."
            out = new_output(args.output)
            write_json(out / "circuit.json", circuit)
            write_identification(circuit, out)
            print(f"Imported {len(circuit['components'])} components to {out}")
            return 0
        if args.command == "digikey-fetch":
            circuit = read_json(args.circuit)
            client = digikey.DigiKeyClient(*digikey.load_credentials())
            summary = digikey.fetch(circuit, Path(args.cache), client, refresh=args.refresh)
            print(f"DigiKey: fetched={summary['fetched']}, already cached={summary['cached']}, errors={summary['errors']}; cache {Path(args.cache).resolve()}")
            return 3 if summary["errors"] else 0
        if args.command == "digikey-check":
            circuit = read_json(args.circuit)
            report = digikey.digikey_audit(circuit, Path(args.cache))
            out = new_output(args.output)
            write_json(out / "digikey.json", report)
            (out / "digikey.md").write_text(digikey.digikey_markdown(report), encoding="utf-8")
            write_owner_actions(identification(circuit), description_audit(circuit), out, report)
            print("DigiKey: " + ", ".join(f"{s}={n}" for s, n in report["coverage"].items()))
            print(f"Report: {out / 'digikey.md'}")
            return 1 if report["coverage"]["fail"] else 2 if report["coverage"]["needs_review"] else 0
        circuit = read_json(args.circuit)
        hashes = circuit.get("project_inputs", {})
        if not args.snapshot:
            if not hashes:
                raise ValueError("No live source hashes. Use --snapshot to explicitly review an offline snapshot")
            for path, sha in hashes.items():
                if not Path(path).is_file() or digest(path) != sha:
                    raise ValueError(f"Project input changed or is unavailable: {path}. Extract again or explicitly use --snapshot")
        # Relative source.path values resolve against the folder of the file that
        # contains them, unless --docs-root names one shared document folder.
        docs_root = Path(args.docs_root).resolve() if args.docs_root else None
        if docs_root is not None and not docs_root.is_dir():
            raise ValueError(f"--docs-root is not a directory: {docs_root}")
        rules_base = docs_root or Path(args.rules).resolve().parent
        evidence_base = docs_root or Path(args.evidence).resolve().parent
        report = review(circuit, read_json(args.rules), read_json(args.evidence), rules_base, evidence_base)
        report["source_base"] = {"rules": str(rules_base), "evidence": str(evidence_base)}
        report["snapshot_review"] = args.snapshot
        report["input_hashes"] = {str(Path(p).resolve()): digest(p) for p in (args.circuit, args.rules, args.evidence)}
        if args.snapshot:
            report["scope"] += " Offline snapshot review; current project was not verified."
        if args.digikey_cache:
            report["digikey"] = digikey.digikey_audit(circuit, Path(args.digikey_cache))
        out = new_output(args.output)
        write_json(out / "report.json", report)
        text = markdown(report)
        if report.get("digikey"):
            text += "\n" + digikey.digikey_markdown(report["digikey"], heading=2)
        (out / "report.md").write_text(text, encoding="utf-8")
        print("Coverage: " + ", ".join(f"{s}={n}" for s, n in report["coverage"].items()))
        print("Component identification: " + ", ".join(f"{s}={n}" for s, n in report["identification"]["coverage"].items()))
        print("Component description: " + ", ".join(f"{s}={n}" for s, n in report["description"]["coverage"].items()))
        if report.get("digikey"):
            print("DigiKey: " + ", ".join(f"{s}={n}" for s, n in report["digikey"]["coverage"].items()))
        write_owner_actions(report["identification"], report["description"], out, report.get("digikey"))
        approvals = report["approval_summary"]
        print(f"Passes approved by a human: {approvals['pass_human_approved']}; agent review only: {approvals['pass_agent_only']}")
        print(f"Report: {out / 'report.md'}")
        dk = (report.get("digikey") or {}).get("coverage", {})
        if report["coverage"]["fail"] or report["description"]["coverage"]["fail"] or dk.get("fail"):
            return 1
        if (not report["results"] or report["coverage"]["needs_review"] or report["coverage"]["not_checked"]
                or report["identification"]["coverage"]["needs_review"] or report["description"]["coverage"]["needs_review"]
                or dk.get("needs_review")):
            return 2
        return 0
    except (OSError, ValueError, KeyError, TypeError, subprocess.TimeoutExpired) as error:
        print(f"Error: {error}", file=sys.stderr)
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
