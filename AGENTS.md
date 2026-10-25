# Working on this repository

This is a Python 3.10+ KiCad review and component-sourcing foundation. Keep it
readable by any AI assistant and runnable with the Python standard library.

- Read `README.md`, then `SOURCING.md` and `SOURCING_HANDOFF.md` for sourcing work.
  User-facing docs: `USER_GUIDE.md` (task index) and one guide per part in `docs/`.
  Update the matching `docs/NN-*.md` guide when commands, flags or outputs change.
  `AGENT_GUIDE.md` describes the separate schematic-review workflow and source policy.
- Verify changes with `python3 -m unittest discover -s tests`. Tests are offline;
  mock HTTP and credentials. Never make supplier calls as part of a unit test.
- The sourcing CLI is offline by default. `--online` explicitly requests fresh,
  read-only supplier requests. Document which integrations were actually tested live.
- Never display `.env`, secrets, authorization headers, credential exports, or
  raw supplier error bodies. Use `.env.example` only for blank configuration names.
- Preserve unknown, stale, conflicting, rejected and excluded results. Supplier
  failure must not erase useful results from another supplier. No fabricated matches.
- Manufacturer + exact MPN identifies a part. Packaging variants are not distinct
  alternatives. Do not sum inventory across packaging options or suppliers.
- DigiKey catalog screening supports any category. `kind` is a label outside the
  built-in RLC/diode profiles; explicit category and parameter criteria drive generic
  matching. Catalog target counts never establish engineering equivalence.
  Manufacturer documentation is needed for circuit-specific properties and active parts. LCSC/JLCPCB data is procurement
  data; it cannot approve engineering equivalence.
- No automatic schematic/BOM edits or ordering. Reports go into new directories.
- Update `SOURCING_HANDOFF.md` when capability, schema, validation or next steps change.
- Leave unrelated user files and the supplied older projects untouched.
