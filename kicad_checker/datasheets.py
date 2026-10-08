"""Find a datasheet PDF for every MPN: project docs first, shared cache second, download last.

Search order per MPN (the first hit wins, nothing is downloaded twice):

1. Project documents: PDFs under docs/, Docs/, datasheets/ ... next to the root schematic
   (or folders given with --docs). A PDF matches when its file name contains the MPN,
   its file name equals the DigiKey datasheet file name, or its text lists the MPN
   (text search needs `pdftotext` from poppler-utils).
2. Shared cache (cache/datasheets in the checker folder): an earlier download for this MPN.
3. Download: the DigiKey DatasheetUrl from the DigiKey cache (run digikey-fetch first).
   Only a real PDF is accepted; HTML pages and errors are reported for manual download.

Downloads go to the shared cache, never into the design project. PDFs are treated as
untrusted data: they are hashed and passed to pdftotext only, never executed or opened.
"""

import hashlib
import json
import re
import shutil
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from .core import component_exemption, identity_candidates
from .description import component_class
from . import digikey
from .paths import CACHE_ROOT

DEFAULT_CACHE = CACHE_ROOT / "datasheets"
DOC_FOLDER_NAMES = {"docs", "doc", "documents", "documentation", "datasheets", "datasheet"}
MAX_BYTES = 60 * 1024 * 1024
READ_TIMEOUT = 20   # seconds without any data
TOTAL_SECONDS = 60  # hard limit per file
USER_AGENT = "Mozilla/5.0 (X11; Linux x86_64) kicad-checker datasheet fetch"


_SHA_MEMO = {}


def sha256_file(path):
    stat = Path(path).stat()
    memo_key = (str(path), stat.st_mtime_ns, stat.st_size)
    if memo_key in _SHA_MEMO:
        return _SHA_MEMO[memo_key]
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    _SHA_MEMO[memo_key] = digest.hexdigest()
    return _SHA_MEMO[memo_key]


def compact(text):
    return re.sub(r"[\s\-#/._]", "", str(text)).upper()


def is_pdf(path):
    with open(path, "rb") as handle:
        return handle.read(5) == b"%PDF-"


# --- PDF text (cached by content hash) ----------------------------------------------

def pdftotext_available():
    return shutil.which("pdftotext") is not None


def pdf_text(path, cache_dir, sha=None):
    """Full text via pdftotext -layout, pages separated by form feeds. None if unavailable."""
    if not pdftotext_available():
        return None
    sha = sha or sha256_file(path)
    cached = Path(cache_dir) / "text" / f"{sha}.txt"
    if cached.is_file():
        return cached.read_text(encoding="utf-8", errors="replace")
    result = subprocess.run(["pdftotext", "-layout", str(path), "-"], capture_output=True, timeout=120)
    if result.returncode:
        return None
    text = result.stdout.decode("utf-8", errors="replace")
    cached.parent.mkdir(parents=True, exist_ok=True)
    cached.write_text(text, encoding="utf-8")
    return text


# --- project documents ------------------------------------------------------------

def project_doc_folders(project_dir):
    project_dir = Path(project_dir)
    if not project_dir.is_dir():
        return []
    return sorted(p for p in project_dir.iterdir() if p.is_dir() and p.name.casefold() in DOC_FOLDER_NAMES)


def local_pdfs(folders):
    found = []
    for folder in folders:
        for path in sorted(Path(folder).rglob("*")):
            if path.is_file() and path.suffix.casefold() == ".pdf" and not any(
                    part.startswith(".") for part in path.relative_to(folder).parts):
                found.append(path.resolve())
    return list(dict.fromkeys(found))


def url_filename(url):
    name = Path(urllib.parse.urlparse(url or "").path).name
    return name.casefold() if name.casefold().endswith(".pdf") else None


def match_local(mpn, url, pdfs, cache_dir):
    """Return (path, reason) for the best local PDF, ([paths], 'ambiguous') or (None, None)."""
    key = compact(mpn)
    by_name = [p for p in pdfs if key in compact(p.stem)]
    if len(by_name) == 1:
        return by_name[0], "file name contains the MPN"
    wanted = url_filename(url)
    by_url = [p for p in pdfs if wanted and p.name.casefold() == wanted]
    if len(by_url) == 1 and not by_name:
        return by_url[0], f"same file name as the DigiKey datasheet ({wanted})"
    candidates = by_name or by_url or pdfs
    by_text = []
    for path in candidates:
        text = pdf_text(path, cache_dir)
        if text and key in compact(text):
            by_text.append(path)
    if len(by_text) == 1:
        return by_text[0], "document text lists the MPN"
    if len(by_text) > 1 or len(by_name) > 1:
        return by_text or by_name, "ambiguous"
    if len(by_url) == 1:
        return by_url[0], f"same file name as the DigiKey datasheet ({wanted})"
    return None, None


# --- shared cache and download --------------------------------------------------------

def manifest_path(cache_dir, mpn):
    digest = hashlib.sha256(digikey.mpn_key(mpn).encode()).hexdigest()[:16]
    safe = re.sub(r"[^A-Za-z0-9._-]", "_", digikey.mpn_key(mpn).upper())[:60]
    return Path(cache_dir) / "by-mpn" / f"{safe}.{digest}.json"


def cached(cache_dir, mpn):
    path = manifest_path(cache_dir, mpn)
    if not path.is_file():
        return None
    entry = json.loads(path.read_text(encoding="utf-8"))
    pdf = Path(cache_dir) / entry.get("file", "")
    if entry.get("file") and pdf.is_file() and sha256_file(pdf) == entry.get("sha256"):
        return entry
    return None


def _fetch(url, partial, opener, deadline, total_seconds):
    """Stream one URL into `partial`; a stalling or slow site is cut off at the deadline."""
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/pdf,*/*"})
    timeout = max(1.0, min(READ_TIMEOUT, deadline - time.monotonic()))
    with opener(request, timeout=timeout) as response, open(partial, "wb") as handle:
        total = 0
        for block in iter(lambda: response.read(1 << 16), b""):
            total += len(block)
            if total > MAX_BYTES:
                raise ValueError(f"larger than {MAX_BYTES // (1024 * 1024)} MB")
            if time.monotonic() > deadline:
                raise ValueError(f"took longer than {total_seconds:g} s")
            handle.write(block)
        return getattr(response, "url", url)


def candidate_urls(url):
    """https first for an http:// link (many sites stall or block plain http), then the original."""
    if url.startswith("//"):
        return ["https:" + url]
    if url.startswith("http://"):
        return ["https://" + url[len("http://"):], url]
    return [url]


def download(url, cache_dir, mpn, opener=urllib.request.urlopen, total_seconds=TOTAL_SECONDS):
    """Download a PDF into the cache. Returns (entry, None) or (None, reason)."""
    if not url:
        return None, "DigiKey lists no datasheet URL"
    cache_dir = Path(cache_dir)
    cache_dir.mkdir(parents=True, exist_ok=True)
    partial = cache_dir / f".download-{hashlib.sha256(url.encode()).hexdigest()[:12]}.part"
    errors, deadline = [], time.monotonic() + total_seconds  # one budget for all attempts of this file
    for attempt in candidate_urls(url):
        if errors and time.monotonic() > deadline:
            errors.append(f"{attempt.split(':', 1)[0]}: skipped, time limit reached")
            continue
        try:
            final_url = _fetch(attempt, partial, opener, deadline, total_seconds)
        except (urllib.error.URLError, OSError, ValueError) as error:
            partial.unlink(missing_ok=True)
            errors.append(f"{attempt.split(':', 1)[0]}: {error}")
            continue
        if not is_pdf(partial):
            partial.unlink(missing_ok=True)
            errors.append(f"{attempt.split(':', 1)[0]}: the URL returned a web page, not a PDF (download it manually)")
            continue
        sha = sha256_file(partial)
        target = cache_dir / f"{sha}.pdf"
        partial.replace(target)
        entry = {"mpn": mpn, "file": target.name, "sha256": sha, "url": attempt, "digikey_url": url,
                 "final_url": final_url, "bytes": target.stat().st_size,
                 "fetched_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
        path = manifest_path(cache_dir, mpn)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(entry, indent=1) + "\n", encoding="utf-8")
        return entry, None
    if len(errors) == 1:
        message = errors[0].split(": ", 1)[1]
        return None, message if "web page" in message else f"download failed: {message}"
    return None, "download failed: " + "; ".join(errors)


# --- per-circuit run ----------------------------------------------------------------

def wanted_parts(circuit, include_passives=False):
    """{mpn: [refs]} for populated parts with one declared MPN; R/C/L skipped unless asked."""
    parts = {}
    for ref, component in sorted(circuit["components"].items()):
        if component_exemption(ref, component) or (component_class(ref) and not include_passives):
            continue
        mpns = list(dict.fromkeys(identity_candidates(component, "part_number").values()))
        if len(mpns) == 1:
            parts.setdefault(mpns[0], []).append(ref)
    return parts


def find_datasheets(circuit, cache_dir=DEFAULT_CACHE, digikey_cache=digikey.DEFAULT_CACHE, doc_folders=None,
                    include_passives=False, offline=False, opener=urllib.request.urlopen, log=print):
    cache_dir = Path(cache_dir)
    if doc_folders is None:
        source = Path(circuit.get("source") or "")
        doc_folders = project_doc_folders(source.parent) if source.suffix == ".kicad_sch" else []
    pdfs = local_pdfs(doc_folders)
    results = []
    for mpn, refs in wanted_parts(circuit, include_passives).items():
        entry = digikey.load(digikey_cache, mpn)
        products = [p for p in (entry or {}).get("exact_matches", [])
                    if digikey.mpn_key(p.get("ManufacturerProductNumber", "")) == digikey.mpn_key(mpn)]
        url = next((p.get("DatasheetUrl") for p in products if p.get("DatasheetUrl")), None)
        item = {"mpn": mpn, "references": refs, "url": url}
        found, reason = match_local(mpn, url, pdfs, cache_dir)
        if reason == "ambiguous":
            item.update(status="needs_review", reason="Several project PDFs match: " + ", ".join(str(p) for p in found)
                        + ". Keep one or rename the right one to contain the MPN")
        elif found:
            item.update(status="found", source="project_docs", path=str(found), sha256=sha256_file(found), reason=reason)
        elif (hit := cached(cache_dir, mpn)):
            item.update(status="found", source="cache", path=str(cache_dir / hit["file"]), sha256=hit["sha256"],
                        reason=f"downloaded earlier from {hit['url']}")
        elif offline:
            item.update(status="missing", reason="Not in project docs or cache (offline run)")
        else:
            if url:
                log(f"{', '.join(refs)} {mpn}: fetching {url} ...")
            hit, error = download(url, cache_dir, mpn, opener=opener)
            if hit:
                item.update(status="found", source="downloaded", path=str(cache_dir / hit["file"]), sha256=hit["sha256"],
                            reason=f"downloaded from {hit['url']}")
            else:
                item.update(status="missing", reason=f"{error}. Put the PDF into the project docs folder"
                            + (f" (from {url})" if url else ""))
        log(f"{mpn}: {item['status']} ({item.get('source', '-')}) {item.get('reason', '')}")
        results.append(item)
    return {
        "schema_version": 1,
        "scope": "Datasheet located per MPN of populated non-passive parts (passives only with --all). "
                 "Finding a document does not verify its revision or that it covers the exact ordering code.",
        "doc_folders": [str(p) for p in doc_folders],
        "cache_dir": str(cache_dir),
        "pdftotext": pdftotext_available(),
        "coverage": {s: sum(r["status"] == s for r in results) for s in ("found", "needs_review", "missing")},
        "results": results,
    }


def datasheets_markdown(report):
    def cell(value):
        return str(value or "—").replace("|", "\\|")

    lines = ["# Datasheets", "", report["scope"], "",
             f"Project document folders: {', '.join(report['doc_folders']) or 'none found'}",
             f"Shared cache: {report['cache_dir']}", ""]
    if not report["pdftotext"]:
        lines.extend(["`pdftotext` is not installed, so PDFs were matched by file name only "
                      "(install poppler-utils for text matching).", ""])
    lines.extend(f"- {status}: {count}" for status, count in report["coverage"].items())
    lines.extend(["", "| MPN | Parts | Status | Source | File | Why |", "| --- | --- | --- | --- | --- | --- |"])
    for item in report["results"]:
        lines.append("| " + " | ".join(cell(v) for v in (item["mpn"], ", ".join(item["references"]), item["status"],
                                                         item.get("source"), item.get("path"), item.get("reason"))) + " |")
    return "\n".join(lines) + "\n"
