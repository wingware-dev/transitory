# Transitory

A desktop dashboard for finding transiting planets in public TESS and Kepler
light curves.  Pick stars on a zoomable sky map or by name / catalog number,
and Transitory fetches each star's light curves from MAST, cleans them,
searches them for periodic transit dips (Box Least Squares), fits the best
candidates with a limb-darkened transit model, vets them, and shows the
results as a gallery of plots per star and a summary table.

![The dashboard after analysing the demo stars](docs/shots/08-final.png)

It runs on macOS, Linux and Windows (Python 3.11 or later).

## Install

```bash
git clone <this repository> transitory
cd transitory
python3 -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -e ".[dev]"            # or: pip install -r requirements.txt
```

Install it editable (`-e`) or run it from the checkout: the cache and report
directories default to the checkout itself (see [Cache](#cache)).

## Run

```bash
python run_dashboard.py                                        # real TESS / Kepler data from MAST
python run_dashboard.py --demo                                 # made-up data, no downloads
python run_dashboard.py --targets "Kepler-10, TOI-270" --max-sectors 10
```

After `pip install -e .` the same is available as the `transitory` command.

| Option | Meaning |
|---|---|
| `--demo` | Use made-up light curves with planets put in at known periods and depths; nothing is downloaded. |
| `--targets NAMES` | Comma-separated stars to put in the selection list at start-up, by name or catalog number (`"Kepler-10, TIC 261136679, KIC 11904151"`). |
| `--max-sectors N` | TESS sectors / Kepler quarters to download and analyse per star; default 20, `0` for all. |

In the window:

1. **Choose stars.**  Drag to pan and use the wheel to zoom the sky map,
   which shows the known planet hosts and, zoomed in to a few degrees, the
   TESS Input Catalog stars in view; click a star to add it.  Or type names in
   the selection panel and press Enter: planet names, TIC / KIC / EPIC
   numbers, KOI / TOI numbers or SIMBAD names, one or comma-separated.
2. **Submit.**  The stars are analysed in background workers, with the
   mission preference chosen in the panel; the progress panel shows each
   star's step.
3. **Read the results.**  The *Gallery* tab has, per star, the raw and
   cleaned light curve, the BLS periodogram, and each candidate's transits
   folded on its period with the fitted model.  The *Table* tab lists every
   candidate with its period, Rp/Rs, T14, depth, SNR, SDE, confidence and the
   vetting notes.
4. **Keep them.**  *File ▸ Save all plots as PNG…* and *File ▸ Export table
   as CSV…*.  Every plot carries the data credits of its star.

### Sectors / quarters per star

A TESS star in the continuous viewing zone can have 40+ sectors.  By default
each star uses at most **20** sectors or quarters, which keeps all 17 Kepler
quarters (long periods need the whole four-year baseline) and bounds TESS
downloads.  Change it at start-up with `--max-sectors N` (`0` = all), or while
running from **Settings ▸ Sectors / quarters per star** (5, 10, 20, 40, All);
the change applies to stars submitted afterwards.  In demo mode the menu is
disabled, since there is nothing to download.

### Demo mode

`--demo` replaces MAST with a synthetic source: six made-up stars in and near
the Kepler field (a hot Jupiter, a super-Earth, a two-planet system, an
eclipsing binary, a quiet star and a long-period planet), with light curves
generated on the fly.  The whole dashboard works the same, offline, which
makes it the quickest way to see what it does.  Their credit line says the
data are synthetic.

## How detections are graded

- **Search.** BLS runs over periods from 0.3 d to half the baseline, up to
  three passes per star, masking each found signal before the next pass.  A
  peak becomes a candidate only with **SDE ≥ 9 and SNR ≥ 10**, backed by
  three or more transits, or by two agreeing ones at SDE ≥ 10.
- **Red noise.** SNRs and the vetting's depth errors are scaled by the
  red-noise *beta factor* (Pont, Zucker & Queloz 2006; Winn et al. 2008), so
  correlated stellar and instrumental noise doesn't make a dip look more
  significant than it is.  Fit uncertainties likewise allow for red noise and
  transit-timing scatter.
- **Confidence**, in the table:
  - **strong**: passes every check;
  - **weak**: a detection that is marginal (SNR or SDE near the threshold,
    a large Rp/Rs, a failed fit);
  - **duo-transit**: only two transits, passing every check, with a fitted
    T14 that agrees with the dip's width.  It may be a long-period planet,
    but two dips can't show periodicity, so it is shown as its own class and
    **not counted as a planet candidate**;
  - **none**: rejected — odd/even depths that disagree, a secondary eclipse
    too deep for a planet, a size too big for a planet, too few transits, or
    a repeat or alias of a stronger signal on the star.

## Validation

To show it can be trusted, Transitory re-discovers known planets: a set of
stars with confirmed planets of different sizes and periods, plus null stars
with no published planet or candidate, compared with the NASA Exoplanet
Archive's published values.

The latest live run ([full report](docs/validation/validation.md), 11 targets,
all sectors / quarters, archive values retrieved 2026-10-29):

| | |
|---|---|
| Known planets recovered at the published period | **11 / 11** (none missed, none at an alias) |
| Spurious detections on the 3 null stars | 1 (KIC 8006161, *weak*, 3 transits) |
| Spurious detections on planet hosts | 0 |
| Duo-transit signals on planet hosts | 4 (Kepler-7, Kepler-22; not counted as candidates) |
| Median \|ΔRp/Rs\| against published | 3.4% |

Run it yourself from **Validation ▸ Run known-planet validation…** (and
*Show validation report*), or from the command line:

```bash
python -m transitory.validation                 # live: downloads all sectors, ~30+ min
python -m transitory.validation --demo          # synthetic stars, offline
python -m transitory.validation --targets "Kepler-10, pi Men c" --max-sectors 10
```

Reports go to `reports/validation/`; `--publish` also copies one to
`docs/validation/`.  `--rebuild-credits` rewrites only the last report's data
credits, from its `results.json` (and the light-curve cache for an older
report), without searching again.

## Cache

Downloaded data are kept so nothing is fetched twice:

- light-curve FITS files (via lightkurve) under `cache/mastDownload/`, with
  `cache/index.json` recording which files answer which query;
- MAST and Exoplanet Archive query results under `cache/catalog/` (archive
  results are refreshed after 30 days).

`cache/` is in the checkout by default and is not committed (`.gitignore`).
Set `TRANSITORY_CACHE_DIR` to put it elsewhere, and `TRANSITORY_REPORTS_DIR`
for the reports directory (default `reports/`).  Deleting the cache is safe;
it is rebuilt on demand.  Requests to MAST are made one at a time, with a
descriptive user agent, timeouts and a single retry.

## Tests

```bash
pytest                 # offline: synthetic light curves and recorded archive responses
pytest -n auto         # in parallel (pytest-xdist)
```

The GUI tests run headless (pytest-qt).  The screenshots in `docs/shots/` are
made with `python tools/take_shots.py`.

## Platform notes

- **macOS**: use a python.org or Homebrew Python 3.11+; the system Python is
  too old.  The menus appear in the macOS menu bar.
- **Linux**: Qt 6 needs the XCB cursor library on X11 (Debian/Ubuntu:
  `sudo apt install libxcb-cursor0`).  On Wayland, `QT_QPA_PLATFORM=xcb` is a
  fallback if the window misbehaves.  For a headless machine (CI),
  `QT_QPA_PLATFORM=offscreen` runs the GUI tests without a display.
- **Windows**: use `py -m venv .venv` and `.venv\Scripts\activate`, then the
  same `pip` and `python` commands.  Long cache paths can exceed Windows'
  260-character limit; enable long paths or set `TRANSITORY_CACHE_DIR` to a
  short directory such as `C:\transitory-cache`.

## Data credits

TESS and Kepler data come from MAST at STScI, and published planet values
from the NASA Exoplanet Archive; both ask to be acknowledged in any work that
uses them.  **Help ▸ Data Credits…** lists the acknowledgements and data DOIs
for the stars you analysed (with *Copy as text*), and every plot carries its
star's credit lines.  The full wording and the software citations are in
[docs/CREDITS.md](docs/CREDITS.md).

## License

MIT.
