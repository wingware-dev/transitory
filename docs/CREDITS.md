# Data and software credits

Transitory uses public data from MAST and the NASA Exoplanet Archive, and
open-source packages.  Work that uses its results should carry the
acknowledgements below.  **Help ▸ Data Credits…** in the dashboard shows the
same text, plus the lines for the stars analysed in the session (*Copy as
text* puts them on the clipboard), and every plot carries its star's credit
lines.  The dashboard takes its text from the constants named in each section,
so this file and the dialog stay in step.

Wording checked against each source's own page on 2026-10-29.

## TESS and Kepler data (MAST)

Source: MAST, [Mission acknowledgements](https://archive.stsci.edu/publishing/mission-acknowledgements)
and [Citing data with DOIs](https://archive.stsci.edu/publishing/doi).
In code: `transitory.pipeline.fetch.MAST_CREDITS`, `DOIS`, `DOI_TITLES`.

TESS:

> This paper includes data collected with the TESS mission, obtained from the
> MAST data archive at the Space Telescope Science Institute (STScI). Funding
> for US Institutions for the TESS mission is provided by the NASA Explorer
> Program. STScI is operated by the Association of Universities for Research
> in Astronomy, Inc., under NASA contract NAS5–26555.

Kepler:

> This paper includes data collected by the Kepler mission and obtained from
> the MAST data archive at the Space Telescope Science Institute (STScI).
> Funding to US Institutions for the Kepler mission was provided by the NASA
> Science Mission Directorate. STScI is operated by the Association of
> Universities for Research in Astronomy, Inc., under NASA contract
> NAS5–26555.

MAST asks that the data DOIs be cited, as `https://doi.org/` URLs, and for
TESS with the sector range used.  The products Transitory downloads:

| Pipeline | Product | DOI |
|---|---|---|
| TESS SPOC (2-min and 20-s cadence) | TESS Light Curves - All Sectors | [10.17909/t9-nmc8-f686](https://doi.org/10.17909/t9-nmc8-f686) |
| TESS-SPOC (full-frame images) | TESS Light Curves From Full Frame Images ("TESS-SPOC") | [10.17909/t9-wpz1-8s54](https://doi.org/10.17909/t9-wpz1-8s54) |
| Kepler (long cadence) | Kepler LC, Q0-Q17 | [10.17909/T9488N](https://doi.org/10.17909/T9488N) |

A star's credits list only the DOIs of the products actually used for it,
each followed by the sectors or quarters used, e.g.
`TESS Light Curves - All Sectors: https://doi.org/10.17909/t9-nmc8-f686 (sectors 1-13, 27-39)`.
For a paper covering several stars, combine their sector ranges, as the
validation report's credits do (each star's ranges listed under the combined
line).

## Where the light-curve files come from

In code: `transitory.pipeline.fetch.RETRIEVED_FROM`, `RETRIEVAL_NOTE`, `AWS_DATASETS`.

Light curves are retrieved from STScI's archive files
(<https://archive.stsci.edu/missions/>), else from AWS Open Data
(`s3://stpubdata`), else found with the MAST search
(<https://mast.stsci.edu>).  They are the same MAST products from whichever
copy, so the acknowledgements and DOIs above apply unchanged.  Each star's
credits add a line naming the copy used and the date, e.g.
`Light curves retrieved on 2026-10-30 from STScI archive files (https://archive.stsci.edu/missions/).`

Files from AWS also carry the citation the
[Registry of Open Data on AWS](https://registry.opendata.aws/mast-tess/) asks
for in each dataset's "How to Cite" (checked 2026-10-30), with the date
retrieved:

- Transiting Exoplanet Survey Satellite (TESS) was accessed on DATE from
  https://registry.opendata.aws/mast-tess.
- Kepler Mission Data was accessed on DATE from https://registry.opendata.aws/mast-kepler.
- K2 Mission Data was accessed on DATE from https://registry.opendata.aws/mast-k2.

## TESS Input Catalog (MAST)

Star names are resolved, and stellar Teff, log g, mass and radius taken,
through the MAST API's name lookup and TESS Input Catalog services.
In code: `transitory.catalog.tic.TIC_CREDIT`.

- Stassun, K. G., et al. 2019, *The Revised TESS Input Catalog and Candidate
  Target List*, AJ 158, 138.

## NASA Exoplanet Archive

Source: [Acknowledging the NASA Exoplanet Archive](https://exoplanetarchive.ipac.caltech.edu/docs/acknowledge.html)
and [DOIs](https://exoplanetarchive.ipac.caltech.edu/docs/doi.html).
In code: `transitory.catalog.archive.ARCHIVE_CREDIT`, `ARCHIVE_REFERENCES`.

> This research has made use of the NASA Exoplanet Archive, which is operated
> by the California Institute of Technology, under contract with the National
> Aeronautics and Space Administration under the Exoplanet Exploration
> Program.

The archive asks that its paper be cited, and the tables used:

- Christiansen, J. L., et al. 2025, *The NASA Exoplanet Archive*, PSJ,
  [doi:10.3847/PSJ/ade3c2](https://doi.org/10.3847/PSJ/ade3c2) (replaces
  Akeson et al. 2013).
- Planetary Systems Composite Parameters Table,
  [doi:10.26133/NEA13](https://doi.org/10.26133/NEA13): published planets for
  the sky map, the planet markers on the plots and the validation.
- Kepler Objects of Interest (KOI) Cumulative Table,
  [doi:10.26133/NEA4](https://doi.org/10.26133/NEA4): KOIs, to check that the
  validation's null stars have no published signal.
- TESS Objects of Interest (TOI) table, used for the same check, has no DOI.

The archive also asks that the literature reference of any individual planet
parameter used be cited directly; the validation report names, for each
planet, the archive table and retrieval date of the values it compares with.
The archive is queried over its TAP service with plain HTTP.

<!-- The sky view shows no survey images while CDS hips2fits doesn't answer
(transitory.gui.sky_background.SKY_IMAGES is off), so their credits are left
out.  Restore this section when the switch is turned back on.

## Sky images (CDS hips2fits, NASA SkyView; DSS2, 2MASS, Pan-STARRS1)

Zoomed in, the sky view draws a survey image behind the stars, cut out by
the CDS hips2fits service from the chosen HiPS survey, or by NASA SkyView
(DSS2 and 2MASS only) when hips2fits doesn't answer.  Wording checked on
2026-10-30.
In code: `transitory.catalog.hips.HIPS2FITS_CREDIT`, `SKYVIEW_CREDIT`,
`SURVEY_CREDITS`.

hips2fits, as the [astroquery hips2fits docs](https://docs.astropy.org/projects/astroquery/en/latest/hips2fits/hips2fits.html)
ask (CDS's own site could not be reached to confirm it):

> This research has made use of the hips2fits, a tool developed at CDS,
> Strasbourg, France aiming at extracting FITS images from HiPS sky maps with
> respect to a WCS.

SkyView, from the [SkyView FAQ](https://skyview.gsfc.nasa.gov/current/help/faq.html)
("How can I acknowledge SkyView in my publications?"; SkyView asks for it
but doesn't require it):

> We acknowledge the use of NASA's SkyView facility
> (http://skyview.gsfc.nasa.gov) located at NASA Goddard Space Flight Center.

DSS2, from [Acknowledging the DSS](https://archive.stsci.edu/dss/acknowledging.html):

> The Digitized Sky Surveys were produced at the Space Telescope Science
> Institute under U.S. Government grant NAG W-2166. The images of these
> surveys are based on photographic data obtained using the Oschin Schmidt
> Telescope on Palomar Mountain and the UK Schmidt Telescope. The plates were
> processed into the present compressed digital form with the permission of
> these institutions.

2MASS, from [2MASS publications](https://irsa.ipac.caltech.edu/data/2MASS/docs/publications/):

> This publication makes use of data products from the Two Micron All Sky
> Survey, which is a joint project of the University of Massachusetts and the
> Infrared Processing and Analysis Center/California Institute of Technology,
> funded by the National Aeronautics and Space Administration and the
> National Science Foundation.

Pan-STARRS1, from [Credit where it is due](https://outerspace.stsci.edu/display/PANSTARRS/):

> The Pan-STARRS1 Surveys (PS1) and the PS1 public science archive have been
> made possible through contributions by the Institute for Astronomy, the
> University of Hawaii, the Pan-STARRS Project Office, the Max-Planck Society
> and its participating institutes, the Max Planck Institute for Astronomy,
> Heidelberg and the Max Planck Institute for Extraterrestrial Physics,
> Garching, The Johns Hopkins University, Durham University, the University of
> Edinburgh, the Queen's University Belfast, the Harvard-Smithsonian Center for
> Astrophysics, the Las Cumbres Observatory Global Telescope Network
> Incorporated, the National Central University of Taiwan, the Space Telescope
> Science Institute, the National Aeronautics and Space Administration under
> Grant No. NNX08AR22G issued through the Planetary Science Division of the
> NASA Science Mission Directorate, the National Science Foundation Grant No.
> AST-1238877, the University of Maryland, Eotvos Lorand University (ELTE), the
> Los Alamos National Laboratory, and the Gordon and Betty Moore Foundation.
-->

## Software

In code: `transitory.pipeline.fetch.LIGHTKURVE_CREDIT`,
`transitory.gui.credits_dialog.SOFTWARE_CREDITS`.

- **Lightkurve** (light-curve search, download and reading):
  "This research made use of Lightkurve, a Python package for Kepler and TESS
  data analysis (Lightkurve Collaboration, 2018)."
  Lightkurve Collaboration et al. 2018, *Lightkurve: Kepler and TESS time
  series analysis in Python*, Astrophysics Source Code Library, ascl:1812.013.
- **Astropy** (BLS periodogram, units, FITS, time):
  "This work made use of Astropy (https://www.astropy.org), a
  community-developed core Python package and an ecosystem of tools and
  resources for astronomy."
  Astropy Collaboration 2013, A&A 558, A33; 2018, AJ 156, 123; 2022, ApJ
  935, 167.
- **Astroquery** (MAST queries, through Lightkurve and directly):
  Ginsburg, A., Sipőcz, B. M., Brasseur, C. E., et al. 2019, *astroquery: An
  Astronomical Web-querying Package in Python*, AJ 157, 98.

Also used, without a requested citation in the dashboard: NumPy, SciPy,
Matplotlib, PySide6 (Qt for Python), pyqtgraph and Requests.

## Methods

Not data credits, but the methods the pipeline follows, for anyone writing
up its results:

- Box Least Squares: Kovács, Zucker & Mazeh 2002, A&A 391, 369.
- Limb-darkening coefficients: Claret 2017, A&A 600, A30 (TESS); Claret &
  Bloemen 2011, A&A 529, A75 (Kepler).
- Red-noise beta factor: Pont, Zucker & Queloz 2006, MNRAS 373, 231; Winn et
  al. 2008, ApJ 683, 1076.
