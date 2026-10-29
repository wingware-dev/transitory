# Known-planet validation

Run 2026-10-29 16:24 UTC (47 min) on **MAST** light curves, using all sectors / quarters.

Published values: NASA Exoplanet Archive, retrieved 2026-10-29 (pscomppars for planets; pscomppars, the cumulative KOI table and the TOI table to check that the null stars have no published signal).

Pipeline: BLS search from 0.3 d to half the baseline, up to 3 passes per star, detection at SDE ≥ 7 and SNR ≥ 7. A found period matches a published one within 1%, also at the 1/2× and 2× aliases.

## Summary

| | |
|---|---|
| Targets | 11 (3 null stars) |
| Published planets | 11 |
| Recovered at the published period | 11 |
| Recovered at an alias | 0 |
| Missed | 0 |
| Spurious detections on null stars | 4 |
| Spurious detections on planet hosts | 5 |
| Extra candidates rejected by vetting | 7 |
| Planets found twice (duplicates) | 1 |
| Targets with errors | 0 |

![Summary](summary.png)

## What the run shows

- 12 of 16 extra candidates cover at most 2 transits, at periods of 22–733 d: single dips from systematics or variability that the search accepts as periodic. They take search passes from real planets (on KIC 6106415, Kepler-10, Kepler-2, Kepler-22, Kepler-7, LHS 3844, WASP-18); the vetting rejects most of them afterwards.
- 9 spurious detections passed vetting as 'weak' (KIC 6106415, KIC 8006161, Kepler-22, Kepler-7, TIC 31410749, WASP-18). Their SNRs (up to 102) assume white noise; the correlated noise and instrumental dips behind them make that SNR far too optimistic.
- Found again by a later search pass: HAT-P-7 b on Kepler-2. Masking a deep transit leaves residual signal, or its secondary eclipse, at the same period.
- Found periods differ from the published ones by more than 5σ for HAT-P-7 b (63σ), pi Men c (9σ), WASP-18 b (5σ), TOI-270 b (245σ), TOI-270 c (7σ), TOI-270 d (205σ): the fit's period errors assume white noise and a strictly periodic orbit, so they are too small, and transit-timing variations shift the mean period with the time span observed (TOI-270 c and d are near a 2:1 resonance).
- Rp/Rs agrees with the published value within a median of 3.4%; beyond 10%: Kepler-22 b (+14%), TOI-270 c (+15%), TOI-270 d (+15%). The fit fixes limb darkening at Sun-like values, which biases Rp/Rs for stars unlike the Sun (the M-dwarf host of TOI-270 c, TOI-270 d), and shallow transits leave Rp/Rs degenerate with the impact parameter.

## Planets: found vs. published

| Planet | Status | P published (d) | P found (d), difference | Rp/Rs published | Rp/Rs found | Δ | T14 published (h) | T14 found (h) | Δ | SNR | SDE | Confidence |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| Kepler-10 b (b) | found | 0.83749 | 0.83749108 ± 0.00000044 (+0.00000038 d, 0.9σ) | 0.0127 | 0.0128 ± 0.0003 | +1.0% | 1.81 | 1.83 ± 0.01 | +0.8% | 189.3 | 112.7 | strong |
| Kepler-10 c (c) | found | 45.29430 | 45.29424 ± 0.00012 (-0.00006 d, 0.5σ) | 0.0204 | 0.0204 ± 0.0005 | +0.0% | 6.87 | 6.92 ± 0.06 | +0.7% | 99.1 | 99.4 | strong |
| Kepler-7 b (b) | found | 4.88549 | 4.88548878 ± 0.00000032 (-0.00000042 d, 1.3σ) | 0.0829 | 0.0807 ± 0.0001 | -2.7% | 5.13 | 5.22 ± 0.01 | +1.7% | 2902.0 | 106.9 | strong |
| HAT-P-7 b (b) | found | 2.20474 | 2.20473540 ± 0.00000007 (-0.00000460 d, 62.8σ) | 0.0754 | 0.0764 ± 0.0001 | +1.3% | 3.88 | 3.93 ± 0.00 | +1.3% | 4518.8 | 116.9 | strong |
| Kepler-22 b (b) | found | 289.86388 | 289.86398 ± 0.00120 (+0.00011 d, 0.1σ) | 0.0203 | 0.0232 ± 0.0006 | +14.4% | 7.42 | 7.34 ± 0.10 | -1.0% | 52.3 | 65.2 | strong |
| pi Men c (b) | found | 6.26784 | 6.2678222 ± 0.0000019 (-0.0000177 d, 9.1σ) | 0.0158 | 0.0170 ± 0.0003 | +7.3% | 2.95 | 3.02 ± 0.02 | +2.3% | 113.9 | 14.4 | strong |
| LHS 3844 b (b) | found | 0.46293 | 0.46292965 ± 0.00000002 (-0.00000006 d, 2.6σ) | 0.0626 | 0.0655 ± 0.0007 | +4.6% | 0.52 | 0.54 ± 0.01 | +3.1% | 79.9 | 110.6 | strong |
| WASP-18 b (b) | found | 0.94145 | 0.94145237 ± 0.00000003 (+0.00000014 d, 5.0σ) | 0.1018 | 0.0983 ± 0.0002 | -3.4% | 2.21 | 2.20 ± 0.00 | -0.3% | 690.5 | 123.2 | strong |
| TOI-270 b (d) | found | 3.35992 | 3.36016285 ± 0.00000099 (+0.00024285 d, 245.3σ) | 0.0307 | 0.0313 ± 0.0007 | +2.1% | 1.29 | 1.38 ± 0.02 | +6.8% | 27.9 | 32.0 | strong |
| TOI-270 c (b) | found | 5.66051 | 5.6604781 ± 0.0000046 (-0.0000319 d, 6.9σ) | 0.0560 | 0.0646 ± 0.0007 | +15.4% | 1.68 | 1.95 ± 0.03 | +15.7% | 76.4 | 15.5 | strong |
| TOI-270 d (c) | found | 11.38194 | 11.379703 ± 0.000011 (-0.002237 d, 204.8σ) | 0.0491 | 0.0566 ± 0.0010 | +15.3% | 2.12 | 2.35 ± 0.05 | +11.2% | 61.5 | 60.9 | strong |

## Missed planets

None.

## Other detections

Candidates that match no published planet. *Spurious* ones passed vetting (weak or strong) and count as false positives; *rejected* ones were flagged by the vetting itself; *duplicates* are a recovered planet found again by a later search pass.

| Star | Candidate | P (d) | Depth (ppm) | SNR | SDE | Transits | Confidence | Kind | Explanation |
|---|---|---|---|---|---|---|---|---|---|
| Kepler-10 | d | 385.1092 | 256 | 14.7 | 17.0 | 2 | none | rejected | near 1× the Kepler orbit (372.5 d); only 2 events covered: more likely individual systematics or stellar variability than a periodic signal; vetting: odd/even depths differ by 5.8 sigma (281 vs 14 ppm): likely eclipsing binary; only 2 transits observed. |
| Kepler-7 | c | 719.9761 | 664 | 30.7 | 35.9 | 2 | weak | spurious | near 2× the Kepler orbit (372.5 d); only 2 events covered: more likely individual systematics or stellar variability than a periodic signal; vetting: only 2 transits observed. |
| Kepler-7 | d | 434.9402 | 242 | 16.9 | 25.9 | 2 | weak | spurious | only 2 events covered: more likely individual systematics or stellar variability than a periodic signal; vetting: only 2 transits observed. |
| Kepler-2 | c | 360.0051 | 309 | 18.4 | 17.1 | 2 | none | rejected | near 1× the Kepler orbit (372.5 d); only 2 events covered: more likely individual systematics or stellar variability than a periodic signal; vetting: odd/even depths differ by 8.2 sigma (425 vs -45 ppm): likely eclipsing binary; only 2 transits observed. |
| Kepler-2 | d | 2.2047 | 13 | 12.3 | 24.6 | 614 | none | duplicate | HAT-P-7 b found again in a later search pass (at 1× its period): what the masking after its detection left behind (residual transit signal, or the planet's secondary eclipse); vetting: repeat of the P=2.2047 d signal, not a separate planet. |
| Kepler-22 | c | 476.9100 | 182 | 19.2 | 20.9 | 2 | weak | spurious | only 2 events covered: more likely individual systematics or stellar variability than a periodic signal; vetting: only 2 transits observed. |
| Kepler-22 | d | 732.5321 | 233 | 17.9 | 20.0 | 2 | weak | spurious | near 2× the Kepler orbit (372.5 d); only 2 events covered: more likely individual systematics or stellar variability than a periodic signal; vetting: only 2 transits observed. |
| LHS 3844 | c | 114.6287 | 9561 | 35.7 | 17.9 | 2 | none | rejected | only 2 events covered: more likely individual systematics or stellar variability than a periodic signal; vetting: odd/even depths differ by 3.9 sigma (8973 vs 12343 ppm): likely eclipsing binary; only 2 transits observed. |
| LHS 3844 | d | 64.3335 | 3557 | 21.4 | 10.1 | 2 | none | rejected | only 2 events covered: more likely individual systematics or stellar variability than a periodic signal; vetting: odd/even depths differ by 4.3 sigma (2541 vs 4964 ppm): likely eclipsing binary; only 2 transits observed. |
| WASP-18 | c | 0.3138 | 109 | 15.1 | 14.6 | 352 | none | rejected | a 0.333333× alias of WASP-18 b (P = 0.9415 d); a 0.333333× alias of WASP-18 b (P = 0.9415 d); vetting: third-period alias of the P=0.9414 d signal, not a separate planet. |
| WASP-18 | d | 21.9292 | 992 | 15.9 | 11.9 | 2 | weak | spurious | only 2 events covered: more likely individual systematics or stellar variability than a periodic signal; vetting: only 2 transits observed; fitted T14 13.9 h is much longer than the 3.6 h detected dip: not transit-shaped. |
| KIC 8006161 | b | 442.3361 | 1092 | 102.2 | 33.4 | 3 | weak | spurious | only 3 events covered: more likely individual systematics or stellar variability than a periodic signal; vetting: rp/rs 0.31 is large for a planet. |
| KIC 6106415 | b | 554.7528 | 1217 | 128.0 | 29.4 | 2 | none | rejected | only 2 events covered: more likely individual systematics or stellar variability than a periodic signal; vetting: odd/even depths differ by 113.2 sigma (6731 vs 687 ppm): likely eclipsing binary; only 2 transits observed. |
| KIC 6106415 | c | 486.0685 | 976 | 86.5 | 25.6 | 2 | weak | spurious | only 2 events covered: more likely individual systematics or stellar variability than a periodic signal; vetting: only 2 transits observed; rp/rs 0.27 is large for a planet. |
| KIC 6106415 | d | 700.9178 | 700 | 80.9 | 19.4 | 2 | none | rejected | only 2 events covered: more likely individual systematics or stellar variability than a periodic signal; vetting: odd/even depths differ by 23.8 sigma (624 vs 1469 ppm): likely eclipsing binary; only 2 transits observed. |
| TIC 31410749 | b | 57.6323 | 145 | 9.4 | 10.5 | 4 | weak | spurious | not an alias or a known systematic period; correlated (red) noise, which the white-noise SNR does not account for, is the likely cause; vetting: marginal SNR 9.4. |
| TIC 31410749 | c | 96.6340 | 142 | 8.3 | 8.8 | 3 | weak | spurious | only 3 events covered: more likely individual systematics or stellar variability than a periodic signal; vetting: marginal SNR 8.3; marginal SDE 8.8. |

## Null stars

| Star | Why it is in the set | Published signals (archive check) | Data | Detections |
|---|---|---|---|---|
| KIC 8006161 | Bright (Tmag 6.8), quiet dwarf (HD 173701), Kepler asteroseismology target, 18 quarters. | none | Kepler 18 quarters, 65,262 points, 1,470 d baseline | b: spurious |
| KIC 6106415 | Bright (Tmag 6.7) dwarf, Kepler asteroseismology target, 14 long-cadence quarters. | none | Kepler 14 quarters, 50,642 points, 1,437 d baseline | b: rejected, c: spurious, d: rejected |
| TIC 31410749 | Tmag 8.3 dwarf in the southern TESS continuous viewing zone, 13 sectors (SPOC 2-min and TESS-SPOC full-frame-image light curves). | none | TESS SPOC+TESS-SPOC 13 sectors, 143,305 points, 1,852 d baseline | b: spurious, c: spurious |

## Targets

| Star | Role | Data | Time (s) | Errors |
|---|---|---|---|---|
| Kepler-10 | planet-host | Kepler 15 quarters, 51,973 points, 1,470 d baseline | 118 |  |
| Kepler-7 | planet-host | Kepler 18 quarters, 65,263 points, 1,470 d baseline | 130 |  |
| Kepler-2 | planet-host | Kepler 18 quarters, 65,283 points, 1,470 d baseline | 167 |  |
| Kepler-22 | planet-host | Kepler 18 quarters, 65,262 points, 1,470 d baseline | 183 |  |
| pi Men | planet-host | TESS SPOC 24 sectors, 393,701 points, 2,582 d baseline | 890 |  |
| LHS 3844 | planet-host | TESS SPOC 11 sectors, 168,801 points, 2,879 d baseline | 186 |  |
| WASP-18 | planet-host | TESS SPOC 10 sectors, 151,222 points, 2,906 d baseline | 209 |  |
| TOI-270 | planet-host | TESS SPOC 7 sectors, 137,828 points, 2,660 d baseline | 216 |  |
| KIC 8006161 | null | Kepler 18 quarters, 65,262 points, 1,470 d baseline | 174 |  |
| KIC 6106415 | null | Kepler 14 quarters, 50,642 points, 1,437 d baseline | 131 |  |
| TIC 31410749 | null | TESS SPOC+TESS-SPOC 13 sectors, 143,305 points, 1,852 d baseline | 420 |  |

## Folded light curves

![Kepler-10 b](figures/kepler-10-b.png)
![Kepler-10 c](figures/kepler-10-c.png)
![Kepler-10 d](figures/kepler-10-d.png)
![Kepler-7 b](figures/kepler-7-b.png)
![Kepler-7 c](figures/kepler-7-c.png)
![Kepler-7 d](figures/kepler-7-d.png)
![Kepler-2 b](figures/kepler-2-b.png)
![Kepler-2 c](figures/kepler-2-c.png)
![Kepler-2 d](figures/kepler-2-d.png)
![Kepler-22 b](figures/kepler-22-b.png)
![Kepler-22 c](figures/kepler-22-c.png)
![Kepler-22 d](figures/kepler-22-d.png)
![pi Men b](figures/pi-men-b.png)
![LHS 3844 b](figures/lhs-3844-b.png)
![LHS 3844 c](figures/lhs-3844-c.png)
![LHS 3844 d](figures/lhs-3844-d.png)
![WASP-18 b](figures/wasp-18-b.png)
![WASP-18 c](figures/wasp-18-c.png)
![WASP-18 d](figures/wasp-18-d.png)
![TOI-270 b](figures/toi-270-b.png)
![TOI-270 c](figures/toi-270-c.png)
![TOI-270 d](figures/toi-270-d.png)
![KIC 8006161 b](figures/kic-8006161-b.png)
![KIC 6106415 b](figures/kic-6106415-b.png)
![KIC 6106415 c](figures/kic-6106415-c.png)
![KIC 6106415 d](figures/kic-6106415-d.png)
![TIC 31410749 b](figures/tic-31410749-b.png)
![TIC 31410749 c](figures/tic-31410749-c.png)

## References for published values

- Bonomo et al. 2023 (https://ui.adsabs.harvard.edu/abs/2023A&A...677A..33B/abstract); NASA Exoplanet Archive pscomppars, retrieved 2026-10-29
- Bonomo et al. 2025 (https://ui.adsabs.harvard.edu/abs/2025A&A...696A.233B/abstract); NASA Exoplanet Archive pscomppars, retrieved 2026-10-29
- Cortés-Zuleta et al. 2020 (https://ui.adsabs.harvard.edu/abs/2020A&A...636A..98C/abstract); NASA Exoplanet Archive pscomppars, retrieved 2026-10-29
- Esteves et al. 2015 (https://ui.adsabs.harvard.edu/abs/2015ApJ...804..150E/abstract); NASA Exoplanet Archive pscomppars, retrieved 2026-10-29
- Kaye et al. 2022 (https://ui.adsabs.harvard.edu/abs/2022MNRAS.510.5464K/abstract); NASA Exoplanet Archive pscomppars, retrieved 2026-10-29
- Kunovac Hodžić et al. 2021 (https://ui.adsabs.harvard.edu/abs/2021MNRAS.502.2893K/abstract); NASA Exoplanet Archive pscomppars, retrieved 2026-10-29
- Nagel et al. 2026 (https://ui.adsabs.harvard.edu/abs/2026A&A...710A.311N/abstract); NASA Exoplanet Archive pscomppars, retrieved 2026-10-29
- Stassun et al. 2017 (https://ui.adsabs.harvard.edu/abs/2017AJ....153..136S/abstract); NASA Exoplanet Archive pscomppars, retrieved 2026-10-29

## Data credits

- This work includes data collected by the Kepler mission, obtained from the MAST data archive at the Space Telescope Science Institute (STScI). Funding for the Kepler mission is provided by the NASA Science Mission Directorate. STScI is operated by the Association of Universities for Research in Astronomy, Inc., under NASA contract NAS 5-26555.
- Kepler long-cadence light curves, Q0-Q17: doi:10.17909/T9488N
- Light curves retrieved with Lightkurve (Lightkurve Collaboration 2018, ascl:1812.013), which uses Astropy and Astroquery.
- This work includes data collected by the TESS mission, obtained from the MAST data archive at the Space Telescope Science Institute (STScI). Funding for the TESS mission is provided by the NASA Science Mission Directorate. STScI is operated by the Association of Universities for Research in Astronomy, Inc., under NASA contract NAS 5-26555.
- TESS SPOC light curves: doi:10.17909/t9-nmc8-f686
- TESS-SPOC full-frame-image light curves: doi:10.17909/t9-wpz1-8s54
- This research has made use of the NASA Exoplanet Archive, which is operated by the California Institute of Technology, under contract with the National Aeronautics and Space Administration under the Exoplanet Exploration Program.
