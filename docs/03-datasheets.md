# 3. Collect datasheets

Finds one datasheet PDF per part: from your project folder first, then the shared cache, then a
download. Useful before a design review, and required input for writing datasheet rules.

[← All guides](../USER_GUIDE.md) · Command: `datasheet-fetch`

## Run it

```sh
python3 -m kicad_checker datasheet-fetch runs/board-001/circuit.json --output runs/board-001-ds
```

Open `runs/board-001-ds/datasheets.md`: which PDF was used for each MPN and where it came from.

By default only active parts are included. R/C/L datasheets rarely add anything DigiKey
doesn't state; add `--all` when you need them.

## Search order

1. **Project docs:** `docs/`, `Docs/`, `datasheets/` next to the root schematic, subfolders
   included, hidden folders such as `.history` skipped. A PDF matches when its name contains
   the MPN, its name equals DigiKey's datasheet file name, or its text lists the MPN.
2. **Shared cache:** `cache/datasheets/` (PDFs stored by hash, with extracted text).
3. **Download** from DigiKey's datasheet link (run `digikey-fetch` first): https first, 60 s
   limit, real PDFs only.

## Options

| Option | Use |
| --- | --- |
| `--docs DIR` | Search this folder instead of the defaults (repeatable) |
| `--offline` | Never download; project docs and cache only |
| `--all` | Include R/C/L parts |
| `--digikey-cache DIR` | DigiKey cache with datasheet URLs (default `cache/digikey`) |
| `--cache DIR` | Shared PDF cache (default `cache/datasheets`) |

## Tips

- Keep your project's PDFs in `docs/` beside the `.kicad_pro`. If you convert them to
  Markdown for an AI assistant, keep the same file stem (`lp5912-ep.pdf` + `lp5912-ep.md`).
- Two local files matching the same MPN → `needs_review`. Remove or rename the stale one.
- A failed download prints the URL. Fetch it by hand into `docs/` and run again.
- Without `pdftotext`, matching by text inside the PDF is skipped (file names still work).

**Next:** [4. Check datasheet requirements](04-rules-review.md)
