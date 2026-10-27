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

ROLES = ("mpn", "lcsc", "manufacturer", "quantity", "description", "value", "package", "category", "references", "fitted")
ROLE_ALIASES = {
    "mpn": {"mpn", "mfrpart", "mfrpartnumber", "mfrpartno", "mfrpn", "manufacturerpartnumber", "manufacturerpart",
            "manufacturerpartno", "manufacturerpn", "mfgpart", "mfgpartnumber", "mfgpn", "mfrpartnum"},
    "lcsc": {"lcsc", "lcscpart", "lcscpartnumber", "lcscpartno", "lcscno", "lcsccode", "jlcpcbpart", "jlcpcbpartnumber",
             "jlcpcbpartno", "jlcpart", "jlcpartnumber", "jlcpcb", "jlc", "jlccode", "componentcode"},
    "manufacturer": {"manufacturer", "mfr", "mfg", "brand", "manufacturername", "mfrname", "maker", "mfgname"},
    "description": {"description", "desc", "specification", "spec", "productdescription", "comment"},
    "value": {"value"},
    "package": {"package", "footprint", "case", "packagecase", "packagetype", "packagereference", "pcbdecal",
                "pcbfootprint", "footprintname", "casepackage"},
    "category": {"componenttype", "category", "type", "originalcategory", "parttype"},
    # Reference-design BOMs (TI, ADI, NXP, ST eval boards)
    "references": {"designator", "designators", "reference", "references", "refdes", "referencedesignator",
                   "referencedesignators", "refdesignator", "refdesignators", "ref", "refs", "partreference",
                   "partreferences", "reflist"},
    "fitted": {"fitted", "fittednotfitted", "populate", "populated", "dnp", "dnf", "donotpopulate", "mount", "mounted",
               "install", "installed", "assembly", "fit", "nopop", "stuff", "stuffed", "place", "placed"},
}
ALTERNATE_PREFIXES = ("alternative", "alternate", "secondsource", "substitute", "second", "2nd", "alt", "sub")
# Headers whose yes/no answers mean "do NOT fit" (DNP=Yes → not fitted). Positive headers
# (Fitted, Populate, Mount…) mean the opposite. Textual values keep their own meaning in both.
NEGATIVE_FITTED = {"dnp", "dnf", "donotpopulate", "nopop"}
TEXT_NOT_FITTED = {"notfitted", "dnp", "dnf", "dni", "nf", "nopop", "donotpopulate", "donotplace", "donotfit", "notplaced",
                   "notmounted", "notinstalled", "notpopulated", "npop", "omit", "nc"}
TEXT_FITTED = {"fitted", "populate", "populated", "mounted", "installed", "pop", "placed", "fit"}
ANSWER_YES = {"yes", "y", "true", "1", "x"}
ANSWER_NO = {"no", "n", "false", "0"}
# Columns that hold an MPN or a C-code depending on the export; decided by their contents.
AMBIGUOUS_PART = {"partnumber", "partno", "part", "pn", "partnum", "model"}
# Most specific first: an "available" column beats a total, which beats a bare quantity.
QUANTITY_TIERS = (
    {"availableqty", "availablequantity", "available", "usableqty", "usablequantity", "usable", "availablestock"},
    {"totalqty", "totalquantity", "totalstock"},
    {"stock", "stockqty", "instock", "inventory", "inventoryqty", "onhand", "qtyonhand", "quantityonhand", "balance",
     "currentstock", "remaining", "remainingqty"},
    {"quantity", "qty", "qtyperboard", "quantityperboard", "qtyboard", "perboard", "qtyperunit", "usage"},
)
CCODE = re.compile(r"C\d+")


def norm(text):
    return re.sub(r"[^a-z0-9]", "", str(text).casefold())


def parse_quantity(text):
    """'1,000' / '1 000' / '250.0' / '100pcs' / '4 ea' -> int; blank or unreadable -> None."""
    text = re.sub(r"(?i)(pcs|pc|pieces|piece|ea|each|units|unit)\.?\s*$", "", str(text or "").strip())
    text = re.sub(r"[\s,'_]", "", text)
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

def _base_role(key):
    for role, aliases in ROLE_ALIASES.items():
        if key in aliases:
            return role
    return "part" if key in AMBIGUOUS_PART else None


def _role_of(header):
    key = norm(header)
    role = _base_role(key)
    if role is None and key.endswith("1") and not key[:-1][-1:].isdigit():
        role = _base_role(key[:-1])  # 'Manufacturer 1' / 'Manufacturer Part Number 1' are the primary pair
        role = role if role in ("mpn", "manufacturer") else None
    return None if role == "part" else role


def alternate_of(header):
    """('mpn' | 'manufacturer', order) for alternate-part columns: 'Alternate PartNumber',
    'Alt Manufacturer', 'Manufacturer 2', 'Manufacturer Part Number 3'. None for other columns."""
    key = norm(header)
    numbered = re.fullmatch(r"(.*?\D)(\d+)", key)
    if numbered and int(numbered.group(2)) >= 2:
        role = _base_role(numbered.group(1))
        if role in ("mpn", "part", "manufacturer"):
            return ("manufacturer" if role == "manufacturer" else "mpn", int(numbered.group(2)))
    for prefix in ALTERNATE_PREFIXES:
        if key.startswith(prefix) and len(key) > len(prefix):
            rest = re.fullmatch(r"(.*?)(\d*)", key[len(prefix):])
            role = _base_role(rest.group(1))
            if role in ("mpn", "part", "manufacturer"):
                return ("manufacturer" if role == "manufacturer" else "mpn", int(rest.group(2) or 2))
    return None


def parse_fitted(text, negative=False):
    """Fitted True/False/None. 'DNP'/'Not Fitted' text is False and 'Fitted' text is True under any
    header; yes/no/1/0 answers follow the header's polarity (negative = a DNP-style header)."""
    key = norm(text)
    if key in TEXT_NOT_FITTED:
        return False
    if key in TEXT_FITTED:
        return True
    if key in ANSWER_YES:
        return not negative
    if key in ANSWER_NO:
        return negative
    return None


MULTI_PART = re.compile(r"\s*(?:;|\n|\s/\s|\s\bor\b\s)\s*", re.I)


def detect_columns(rows, overrides=None, scan=30, require_quantity=True):
    """Return (header row index, {role: column index}, {pool header: column index}) or raise ValueError.
    Parts lists (require_quantity=False) need only an MPN or C-code column."""
    overrides = {role: norm(header) for role, header in (overrides or {}).items()}
    best = None
    for index, row in enumerate(rows[:scan]):
        headers = [norm(cell) for cell in row]
        alternates = {c for c, cell in enumerate(row) if alternate_of(cell)}
        mapping = {}
        for role, wanted in overrides.items():
            if wanted in headers:
                mapping[role] = headers.index(wanted)
        for column, key in enumerate(headers):
            role = _role_of(row[column])
            if role and role not in mapping and column not in mapping.values() and column not in alternates:
                mapping[role] = column
        if "quantity" not in mapping:
            for tier in QUANTITY_TIERS:
                hit = next((c for c, key in enumerate(headers) if key in tier), None)
                if hit is not None:
                    mapping["quantity"] = hit
                    break
        ambiguous = [c for c, key in enumerate(headers) if (key in AMBIGUOUS_PART or key[:-1] in AMBIGUOUS_PART and key.endswith("1"))
                     and c not in mapping.values() and c not in alternates]
        for column in ambiguous:
            values = [r[column].strip() for r in rows[index + 1:index + 51] if len(r) > column and r[column].strip()]
            role = "lcsc" if values and sum(bool(CCODE.fullmatch(v.upper())) for v in values) >= 0.8 * len(values) else "mpn"
            mapping.setdefault(role, column)
        if ("quantity" in mapping or not require_quantity) and ({"mpn", "lcsc"} & set(mapping)):
            score = len(mapping)
            if best is None or score > best[0]:
                best = (score, index, mapping, headers)
    if best is None:
        raise ValueError(("No header row with a quantity column and an MPN or LCSC/JLCPCB part column was found; "
                          "use --column quantity=<header> --column mpn=<header> (or lcsc=<header>)") if require_quantity else
                         "No header row with an MPN or LCSC/JLCPCB part column was found; use --column mpn=<header>")
    _, index, mapping, headers = best
    missing = [role for role in overrides if role not in mapping]
    if missing:
        raise ValueError(f"--column header(s) not found for: {', '.join(missing)}")
    used = set(mapping.values())
    pools = {rows[index][c].strip(): c for c, key in enumerate(headers)
             if c not in used and (key.endswith("qty") or key.endswith("quantity"))}
    return index, mapping, pools


# --- loading -----------------------------------------------------------------------------

def load_stock(path, overrides=None, label=None, sheet=None, require_quantity=True):
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
                found.append((len(detect_columns(rows, overrides, require_quantity=require_quantity)[1]), name, rows))
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
    header, mapping, pools = detect_columns(rows, overrides, require_quantity=require_quantity)
    names = {role: rows[header][column].strip() for role, column in mapping.items()}
    negative_fitted = "fitted" in mapping and norm(rows[header][mapping["fitted"]]) in NEGATIVE_FITTED
    pairs = {}
    for column, cell_text in enumerate(rows[header]):
        found = alternate_of(cell_text) if column not in mapping.values() else None
        if found:
            pairs.setdefault(found[1], {})[found[0]] = column
    alternate_columns = [pairs[k] for k in sorted(pairs) if "mpn" in pairs[k]]
    if alternate_columns:
        names["alternates"] = [" + ".join(rows[header][c].strip() for c in (pair.get("manufacturer"), pair["mpn"])
                                          if c is not None) for pair in alternate_columns]
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
        alternates = []
        if mpn and MULTI_PART.search(mpn):  # 'GRM155R71C104KA88D / CL05B104KO5NNNC' in one cell
            options = [m for m in MULTI_PART.split(mpn) if m]
            makers = [m for m in MULTI_PART.split(cell(row, "manufacturer")) if m]
            mpn = options[0]
            alternates += [{"mpn": m, "manufacturer": makers[i] if len(makers) == len(options) else None}
                           for i, m in enumerate(options[1:], 1)]
            if len(makers) == len(options):
                row = list(row)
                row[mapping["manufacturer"]] = makers[0]
        for pair in alternate_columns:
            alt = row[pair["mpn"]].strip() if pair["mpn"] < len(row) else ""
            maker = row[pair["manufacturer"]].strip() if "manufacturer" in pair and pair["manufacturer"] < len(row) else ""
            if alt and alt.casefold() not in ("-", "n/a", "na", "none") and alt != mpn:
                alternates.append({"mpn": alt, "manufacturer": maker or None})
        entries.append({
            "references": cell(row, "references"),
            "fitted": parse_fitted(cell(row, "fitted"), negative_fitted) if "fitted" in mapping else None,
            "alternates": alternates,
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
    if unknown and "quantity" in mapping:
        warnings.append(f"{path.name}: {unknown} row(s) have an unreadable quantity (treated as unknown, never as stock)")
    if "manufacturer" not in mapping:
        warnings.append(f"{path.name}: no manufacturer column; matching uses MPN and C-code only")
    return {"source": label or path.stem, "path": str(path), "sheet": chosen, "header_row": header + 1,
            "headers": [h.strip() for h in rows[header]],
            "columns": names, "quantity_pools": list(pools), "entries": entries, "warnings": warnings}
