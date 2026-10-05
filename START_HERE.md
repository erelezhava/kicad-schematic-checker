# Start a review with your preferred AI assistant

Give the assistant access to this folder, your KiCad project, and the relevant documents. An assistant with local file and command access can use the tools directly. In a chat-only environment, run the commands yourself and supply their outputs along with documents.

Place the project documents in `docs/` beside the root KiCad schematic, or tell the assistant the actual document-folder path. Keep each source PDF and its Markdown conversion together with matching filename stems. A reference schematic PDF can remain PDF-only.

Use DigiKey specifications for RLC passives and manufacturer datasheets/official product pages for active parts, following the component source policy in AGENT_GUIDE.md. LCSC/JLCPCB codes are procurement identifiers, not engineering evidence.

Use this opening instruction:

> Read AGENT_GUIDE.md and README.md in this checker folder. Use the provided tools to review my KiCad project. First discover its docs/ folder (or the document directory I specify), inventory the PDFs and Markdown conversions, and establish the operating conditions and authoritative document revisions. Use Markdown for searching and verify critical evidence against the original PDF pages. Extract the saved design into a new run directory. Build a complete list of applicable requirements from the supplied documents, with source locations, and keep uncertain rules as drafts. Bind rules to actual components and gather evidence for the exact part numbers. Run all listed rules and report failed, unresolved, draft, and manually reviewed requirements. Do not assume missing evidence is a pass, do not edit my design, and do not claim a complete electrical approval. Ask focused questions when design intent or evidence is missing.

Extraction automatically creates `identification.md`, `description.md` and `owner-actions.md` (plus JSON). Give `owner-actions.md` to the design owner first: it lists every component whose Manufacturer, MPN or Description must be filled or corrected. Start by checking that every exported populated purchasable component has a manufacturer and exact MPN. LCSC/JLCPCB codes are optional sourcing fields. Missing identification stays unresolved, and independent circuit checks continue. See README.md for aliases and documented exemptions.

## Suggested next iteration

Use `SCHEMATIC_SHEET_INPUT.md` to describe the next project's circuit intent and review scope. Select a functional block, establish its operating conditions, and gather the relevant requirements and exact-part evidence. Preserve unresolved checks until their evidence is established.
