# 0. Setup

Get the tool running on your machine, with your own keys. Ten minutes, once.

[← All guides](../USER_GUIDE.md)

## You need

| Need | Required? | Why |
| --- | --- | --- |
| Python 3.10+ | yes | Runs everything. Standard library only: no `pip install` |
| KiCad 10 with `kicad-cli` on PATH | for `.kicad_pro` input | Reads the saved project. Not needed if you pass an exported netlist or `circuit.json` |
| `pdftotext` | optional | Datasheet text search: `sudo apt install poppler-utils` |
| DigiKey API keys | optional | Catalog checks, stock, alternatives. Without them those results stay `unknown` |
| LCSC / JLCPCB API keys | optional | LCSC and JLCPCB stock columns (not yet tested live) |

## Install

```sh
git clone git@github.com:erelezhava/kicad-schematic-checker.git
cd kicad-schematic-checker
python3 -m unittest discover -s tests      # must end with OK; offline, no keys needed
cp .env.example .env                       # then fill in YOUR keys
mkdir -p stock                             # stock exports go here (git-ignored)
```

Check KiCad: `kicad-cli version` should print 10.x.

## Keys

All keys go in `.env` (git-ignored). Alternatives: `~/.config/kicad_checker/credentials.env`
for every checkout on the machine, or environment variables (they win; use them in CI).

**DigiKey:** developer.digikey.com → create an organization → *Production App* → enable
*Product Information V4* → copy Client ID and Secret into `DIGIKEY_CLIENT_ID` /
`DIGIKEY_CLIENT_SECRET`.

**LCSC:** apply at lcsc.com/agent. Request only the Product modules (the tool is read-only)
and whitelist the public IP you run from. A VPN changes that IP.

**JLCPCB:** api.jlcpcb.com → `JLCPCB_APP_ID`, `JLCPCB_ACCESS_KEY`, `JLCPCB_SECRET_KEY`.

## Where things live

| Folder | Content | In git? |
| --- | --- | --- |
| `kicad_checker/`, `jlcpcb_rules/`, `tools/` | the code | yes |
| `docs/` | these guides | yes |
| `examples/`, `rules/` | demo inputs, reusable rule packs | yes |
| `.env` | your keys | **never** |
| `stock/` | company stock exports | **never** |
| `runs/` | every report; one new folder per run | no |
| `cache/` | DigiKey answers, datasheet PDFs, sourcing snapshots | no |

## Sharing with a colleague

They clone the repo and add **their own** `.env` (the DigiKey quota is per app, and keys are
personal). Don't send them `.env`, `stock/` or `runs/`: stock is company data, and LCSC's API
agreement forbids passing on its data, so reports stay internal.

## Try it without a board (2 minutes, offline)

```sh
python3 -m kicad_checker import-netlist examples/demo/netlist.xml --output runs/demo-001
python3 -m kicad_checker review runs/demo-001/circuit.json \
  --rules examples/demo/rules.json --evidence examples/demo/evidence.json \
  --snapshot --output runs/demo-001-review
```

Open `runs/demo-001/owner-actions.md` and `runs/demo-001-review/report.md`. The demo
contains a deliberate violation, so the second command exits with `1`.

**Next:** [1. Check the part data in a schematic](01-schematic-check.md)
