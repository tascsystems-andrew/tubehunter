# TubeHunter

A tube-shopping companion for tube-amp builders. Crawls
[thetubestore.com](https://www.thetubestore.com/), classifies every hit against
a local reference of ~1,300 tube types, and ranks each result for a specific
amp's socket / heater / dissipation / vibe constraints — all in an
iTunes-classic table with a native macOS window.

Rankings are driven by an **amp target** — a JSON description of a chassis,
heater supply, and tube slots. Ship with two: a modular Vox-inspired guitar amp
("Voxy") and a hi-fi SE monoblock. Switch targets from the toolbar and the whole
inventory re-scores; import a new target directly from a
[Filament Studio](https://github.com/tascsystems-andrew) chain export.

![screenshot placeholder](docs/screenshot.png)

## Features

- **~1,300-entry catalog** of vacuum-tube classifications with heater voltage,
  socket, µ / gm, plate dissipation, sharp-vs-remote cutoff, guitar-amp vibe,
  and a one-line description for each. Built compactly via
  [`build_catalog.py`](build_catalog.py) using an alias system so 12V-heater
  variants inherit from their 6V parent.
- **Live pricing** in CAD (or USD) via thetubestore's SuiteCommerce items JSON
  API. Rate-limited to 3 full refreshes / 24 h; a full crawl takes ~5 minutes.
- **Target-driven ranking** — every slot column, sidebar entry, chassis rule,
  heater-rail budget and build-role dropdown comes from the active target file
  (`data/targets/*.json`). One generic scoring engine, zero per-amp code.
- **Filament Studio import** — the `import…` button accepts a Filament Studio
  chain export (`schemaVersion: 1`) unchanged and converts each tube stage into
  a slot bracketing the designed operating point.
- **Envelope-based builds** ("playlists") with per-section role tagging.
  Assign one 11BM8's triode to V2t and its pentode to V2p, another 11BM8's
  triode to PI and its pentode to V2p_B, etc. Shopping list consolidates
  duplicates automatically.
- **Heater rail budget** with 3-rail warning (max 3 distinct heater voltages
  in one amp). Unassigned envelopes are treated as pure shopping-list extras
  and don't count against the budget.
- **Chassis-fit filter** — hard-disqualifies octal / UX / loctal / compactron
  from Voxy scoring since the chassis only accepts 7-pin B7G or 9-pin noval.
- **Toolbar filters**: dual-range piecewise price slider (weighted toward the
  low end), min-fit stars, in-stock, chassis-fit.
- **Store-cart bookmarklet**: clipboard-based transport, works everywhere,
  no CORS or self-signed-cert hassle. See `/bookmarklet` in the running app.
- **CSV export** of the whole crawled inventory (all 808 tubes, all 24 columns).

## Requirements

- macOS (tested on Apple Silicon)
- Python 3.7+
- `openssl` — ships with macOS
- Optional: `pip install pywebview` for the native window mode; otherwise
  falls back to the default browser.

## Quick start

```bash
git clone https://github.com/tascsystems-andrew/tubehunter.git
cd tubehunter
pip3 install --user pywebview   # optional but recommended
python3 tubehunter.py
```

Or on macOS, double-click `TubeHunter.app` — a real `.app` bundle with the
tube-shaped icon that can be dragged to the Dock.

The app opens a 1440×900 native window (via pywebview / WKWebView on macOS),
runs an HTTP server on `127.0.0.1:8765`, and displays every product
`thetubestore.com` has under
[/other-tubes/by-leading-number](https://www.thetubestore.com/other-tubes/by-leading-number).

## Repository layout

```
tubehunter.py            Single-file server + scraper + ranker + embedded HTML/CSS/JS
build_catalog.py         Compact declarative source for the tube knowledge base
data/
├── catalog.json         Generated: ~1,300 tube classifications
└── targets/             Amp target definitions (Voxy, HiFi SE monoblock, yours…)
TubeHunter.app/          macOS .app bundle with launcher + custom .icns icon
```

Runtime state (`data/snapshot.json`, TLS cert, local catalog overrides) is
gitignored — it's regenerated on first run.

## Targeting a different amp

Drop a JSON file in `data/targets/` (schema `tubehunter-target/1`) and pick it
from the toolbar. A target declares:

- `chassis.sockets` — which bases physically fit (hard filter)
- `heater_supply` — max voltage, distinct-rail budget, existing rails
- `slots` — each with `accepts` (category → base score), `requires_element`,
  socket preferences, `mu_bands`, `pd_range`, `va_min`, cutoff preference

Or design the amp in Filament Studio and use **import…** — the converter turns
each tube stage into a slot automatically. See `data/targets/*.json` for two
worked examples.

## Adding tubes to the catalog

Edit `build_catalog.py` and re-run it:

```python
# small-signal pentode preamp
C["6ZQ4"] = pp(gm=5.5, notes="Small sharp-cutoff pentode; some Japanese HiFi.")

# variant that just inherits with a different heater
C["12ZQ4"] = alias("6ZQ4", hv=12.6, ha=0.15)
```

Then:

```bash
python3 build_catalog.py
```

Reload the app; the ranker re-scores against the new catalog on server boot.

Personal edits that shouldn't be tracked can live in
`data/catalog_local.json` — that file is auto-layered on top of the built
catalog and is gitignored.

## Store-cart flow

Because SuiteCommerce Advanced doesn't accept `?additems=…` URLs on this
tenant, and browsers block cross-origin POSTs to the cart API, TubeHunter
uses a clipboard-based bookmarklet:

1. In TubeHunter, click **Push to store cart ↗** on a build. A modal opens.
2. Click **📋 Copy cart to clipboard**. TubeHunter resolves internal IDs
   server-side and writes a JSON payload to the clipboard.
3. Switch to any tab on thetubestore.com.
4. Click the **🛒 TubeHunter Cart** bookmarklet in your bookmarks bar.
   (Setup at `/bookmarklet` in the running app — drag-install once, safe
   fallback for browsers that mangle the drag.)
5. Bookmarklet reads the clipboard (Safari shows a one-time Paste
   confirmation), verifies the `_tubehunter: true` marker, and calls
   `LiveOrder.Model.getInstance().addLines(...)` — the same code path the
   store's own Add-to-Cart button uses.
6. Blue banner: *Added N tubes to your cart.*

## License

MIT — see [LICENSE](LICENSE).

## Not affiliated with

thetubestore.com. This is a fan-made tool for personal use with their public
product listings.
