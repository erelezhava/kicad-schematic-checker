"""Read stock inventories (CSV or XLSX) into normalized entries. Read-only, standard library.

Works with a JLCPCB parts-inventory export, a sheet derived from it, or any company stock
list with one row per part. The header row and columns are detected by name; explicit
`role=Header` overrides cover unusual exports. A row needs an MPN and/or an LCSC/JLCPCB
C-code plus a quantity column; everything else is optional.
"""

import csv
import io
import re
import zipfile
import xml.etree.ElementTree as ET
from pathlib import Path

ROLES = ("mpn", "lcsc", "manufacturer", "quantity", "description", "value", "package", "category")
ROLE_ALIASES = {
    "mpn": {"mpn", "mfrpart", "mfrpartnumber", "mfrpartno", "mfrpn", "manufacturerpartnumber", "manufacturerpart",
            "manufacturerpartno", "manufacturerpn", "mfgpart", "mfgpartnumber", "mfgpn", "mfrpartnum"},
    "lcsc": {"lcsc", "lcscpart", "lcscpartnumber", "lcscpartno", "lcscno", "lcsccode", "jlcpcbpart", "jlcpcbpartnumber",
             "jlcpcbpartno", "jlcpart", "jlcpartnumber", "jlcpcb", "jlc", "jlccode", "componentcode"},
    "manufacturer": {"manufacturer", "mfr", "mfg", "brand", "manufacturername", "mfrname", "maker"},
    "description": {"description", "desc", "specification", "spec", "productdescription", "comment"},
    "value": {"value"},
    "package": {"package", "footprint", "case", "packagecase", "packagetype"},
    "category": {"componenttype", "category", "type", "originalcategory", "parttype"},
}
# Columns that hold an MPN or a C-code depending on the export; decided by their contents.
AMBIGUOUS_PART = {"partnumber", "partno", "part", "pn", "partnum", "model"}
# Most specific first: an "available" column beats a total, which beats a bare quantity.
QUANTITY_TIERS = (
    {"availableqty", "availablequantity", "available", "usableqty", "usablequantity", "usable", "availablestock"},
    {"totalqty", "totalquantity", "totalstock"},
    {"stock", "stockqty", "instock", "inventory", "inventoryqty", "onhand", "qtyonhand", "quantityonhand", "balance",
     "currentstock", "remaining", "remainingqty"},
    {"quantity", "qty"},
)
CCODE = re.compile(r"C\d+")


def norm(text):
    return re.sub(r"[^a-z0-9]", "", str(text).casefold())


def parse_quantity(text):
    """'1,000' / '1 000' / '250.0' -> int; blank or unreadable -> None."""
    text = re.sub(r"[\s,'_]", "", str(text or ""))
    if re.fullmatch(r"\d+", text):
        return int(text)
    if re.fullmatch(r"\d+\.0*", text):
        return int(text.split(".")[0])
    return None


# --- raw tables --------------------------------------------------------------------------

def read_csv_rows(path):
    data = Path(path).read_bytes()
    for encoding in ("utf-8-sig", "cp1252", "latin-1"):
        try:
            text = data.decode(encoding)
            break
        except UnicodeDecodeError:
            continue
    try:
        dialect = csv.Sniffer().sniff(text[:8192], delimiters=",;\t")
    except csv.Error:
        dialect = csv.excel
    return [row for row in csv.reader(io.StringIO(text), dialect)]


XLSX_NS = {"m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main",
           "r": "http://schemas.openxmlformats.org/officeDocument/2006/relationships"}


def _column_index(ref):
    letters = re.match(r"[A-Z]+", ref).group(0)
    index = 0
    for letter in letters:
        index = index * 26 + ord(letter) - 64
    return index - 1


def read_xlsx_sheets(path):
    """{sheet name: rows} for every worksheet. Values only (no formulas evaluated)."""
    with zipfile.ZipFile(path) as archive:
        names = set(archive.namelist())
        shared = []
        if "xl/sharedStrings.xml" in names:
            root = ET.fromstring(archive.read("xl/sharedStrings.xml"))
            for item in root.findall("m:si", XLSX_NS):
                shared.append("".join(t.text or "" for t in item.iter(f"{{{XLSX_NS['m']}}}t")))
        workbook = ET.fromstring(archive.read("xl/workbook.xml"))
        rels = ET.fromstring(archive.read("xl/_rels/workbook.xml.rels"))
        targets = {r.get("Id"): r.get("Target") for r in rels}
        sheets = {}
        for sheet in workbook.find("m:sheets", XLSX_NS):
            target = targets[sheet.get(f"{{{XLSX_NS['r']}}}id")].lstrip("/")
            target = target if target.startswith("xl/") else "xl/" + target
            root = ET.fromstring(archive.read(target))
            rows = []
            for row in root.iter(f"{{{XLSX_NS['m']}}}row"):
                cells = {}
                for cell in row.findall("m:c", XLSX_NS):
                    kind, value = cell.get("t"), cell.find("m:v", XLSX_NS)
                    if kind == "s" and value is not None:
                        text = shared[int(value.text)]
                    elif kind == "inlineStr":
                        text = "".join(t.text or "" for t in cell.iter(f"{{{XLSX_NS['m']}}}t"))
                    else:
                        text = value.text if value is not None and value.text is not None else ""
                    cells[_column_index(cell.get("r"))] = text
                rows.append([cells.get(i, "") for i in range(max(cells) + 1)] if cells else [])
            sheets[sheet.get("name")] = rows
        return sheets


# --- header detection --------------------------------------------------------------------

def _role_of(header):
    key = norm(header)
    for role, aliases in ROLE_ALIASES.items():
        if key in aliases:
            return role
    return None


def detect_columns(rows, overrides=None, scan=30):
    """Return (header row index, {role: column index}, {pool header: column index}) or raise ValueError."""
    overrides = {role: norm(header) for role, header in (overrides or {}).items()}
    best = None
    for index, row in enumerate(rows[:scan]):
        headers = [norm(cell) for cell in row]
        mapping = {}
        for role, wanted in overrides.items():
            if wanted in headers:
                mapping[role] = headers.index(wanted)
        for column, key in enumerate(headers):
            role = _role_of(row[column])
            if role and role not in mapping and column not in mapping.values():
                mapping[role] = column
        if "quantity" not in mapping:
            for tier in QUANTITY_TIERS:
                hit = next((c for c, key in enumerate(headers) if key in tier), None)
                if hit is not None:
                    mapping["quantity"] = hit
                    break
        ambiguous = [c for c, key in enumerate(headers) if key in AMBIGUOUS_PART and c not in mapping.values()]
        for column in ambiguous:
            values = [r[column].strip() for r in rows[index + 1:index + 51] if len(r) > column and r[column].strip()]
            role = "lcsc" if values and sum(bool(CCODE.fullmatch(v.upper())) for v in values) >= 0.8 * len(values) else "mpn"
            mapping.setdefault(role, column)
        if "quantity" in mapping and ({"mpn", "lcsc"} & set(mapping)):
            score = len(mapping)
            if best is None or score > best[0]:
                best = (score, index, mapping, headers)
    if best is None:
        raise ValueError("No header row with a quantity column and an MPN or LCSC/JLCPCB part column was found; "
                         "use --column quantity=<header> --column mpn=<header> (or lcsc=<header>)")
    _, index, mapping, headers = best
    missing = [role for role in overrides if role not in mapping]
    if missing:
        raise ValueError(f"--column header(s) not found for: {', '.join(missing)}")
    used = set(mapping.values())
    pools = {rows[index][c].strip(): c for c, key in enumerate(headers)
             if c not in used and (key.endswith("qty") or key.endswith("quantity"))}
    return index, mapping, pools


# --- loading -----------------------------------------------------------------------------

def load_stock(path, overrides=None, label=None, sheet=None):
    """Normalized stock entries for one file. Duplicate rows stay separate (the matcher sums
    rows of the same identity within one source; different sources are never summed)."""
    path = Path(path)
    warnings = []
    if path.suffix.casefold() in (".xlsx", ".xlsm"):
        sheets = read_xlsx_sheets(path)
        if sheet:
            if sheet not in sheets:
                raise ValueError(f"Sheet {sheet!r} not in {path.name}; sheets: {', '.join(sheets)}")
            candidates = {sheet: sheets[sheet]}
        else:
            candidates = sheets
        found = []
        for name, rows in candidates.items():
            try:
                found.append((len(detect_columns(rows, overrides)[1]), name, rows))
            except ValueError:
                continue
        if not found:
            raise ValueError(f"{path.name}: no sheet has a recognizable stock header; pass --sheet and --column")
        found.sort(key=lambda item: -item[0])
        _, chosen, rows = found[0]
        if len(found) > 1 and not sheet:
            warnings.append(f"{path.name}: used sheet {chosen!r} (also usable: {', '.join(f[1] for f in found[1:])}); "
                            "pass --sheet to choose")
    elif path.suffix.casefold() in (".csv", ".tsv", ".txt"):
        rows, chosen = read_csv_rows(path), None
    else:
        raise ValueError(f"Unsupported stock file type {path.suffix!r}; use .csv or .xlsx")
    header, mapping, pools = detect_columns(rows, overrides)
    names = {role: rows[header][column].strip() for role, column in mapping.items()}
    entries, skipped, bad_codes = [], 0, 0

    def cell(row, role):
        column = mapping.get(role)
        return row[column].strip() if column is not None and column < len(row) else ""

    for number, row in enumerate(rows[header + 1:], start=header + 2):
        if not any(c.strip() for c in row):
            continue
        mpn, code = cell(row, "mpn"), cell(row, "lcsc").upper()
        if code and not CCODE.fullmatch(code):
            bad_codes += 1
            code = ""
        if not mpn and not code:
            skipped += 1
            continue
        raw_qty = cell(row, "quantity")
        entries.append({
            "source": label or path.stem, "row": number, "mpn": mpn or None, "lcsc": code or None,
            "manufacturer": cell(row, "manufacturer") or None,
            "quantity": parse_quantity(raw_qty), "quantity_text": raw_qty,
            "pools": {name: parse_quantity(row[c]) if c < len(row) else None for name, c in pools.items()},
            "description": cell(row, "description"), "value": cell(row, "value"),
            "package": cell(row, "package"), "category": cell(row, "category"),
        })
    if skipped:
        warnings.append(f"{path.name}: {skipped} row(s) without MPN or C-code were ignored")
    if bad_codes:
        warnings.append(f"{path.name}: {bad_codes} C-code cell(s) were not C+digits and were ignored")
    unknown = sum(e["quantity"] is None for e in entries)
    if unknown:
        warnings.append(f"{path.name}: {unknown} row(s) have an unreadable quantity (treated as unknown, never as stock)")
    if "manufacturer" not in mapping:
        warnings.append(f"{path.name}: no manufacturer column; matching uses MPN and C-code only")
    return {"source": label or path.stem, "path": str(path), "sheet": chosen, "header_row": header + 1,
            "columns": names, "quantity_pools": list(pools), "entries": entries, "warnings": warnings}
