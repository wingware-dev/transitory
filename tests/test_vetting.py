"""Candidate vetting on synthetic light curves.

Candidates are built from the injected parameters (see ``injected_candidate``),
so each check is tested apart from the search.
"""

import pytest

from pipeline_synth import flat_cleaned, injected_candidate
from transitory.pipeline.fit import fit_transit
from transitory.pipeline.vetting import vet_candidate
from transitory.synthetic import InjectedPlanet

PLANET = InjectedPlanet(3.21, 1.3, 0.010, 2.9)


def vet(planets, cand=None, with_fit=True, others=(), **cand_kw):
    cleaned = flat_cleaned(planets)
    cand = cand or injected_candidate(planets[0], **cand_kw)
    fit = fit_transit(cleaned, cand) if with_fit else None
    return vet_candidate(cleaned, cand, fit, others=others)


def has_reason(vetting, text):
    return any(text in r for r in vetting.reasons)


def test_clean_planet_is_strong():
    v = vet([PLANET])
    assert v.confidence == "strong"
    assert v.odd_even_sigma < 3
    assert abs(v.secondary_depth) < 3 * 300e-6
    assert has_reason(v, "odd/even consistent")


def test_odd_even_eclipsing_binary_flagged():
    eb = InjectedPlanet(2.7, 0.5, 0.01, 3.1, odd_even_ratio=0.7)
    v = vet([eb])
    assert v.confidence == "none"
    assert v.odd_even_sigma > 10
    assert has_reason(v, "odd/even depths differ")


def test_deep_secondary_eclipse_flagged():
    eb = InjectedPlanet(2.7, 0.5, 0.01, 3.1, secondary_depth=0.003)
    v = vet([eb])
    assert v.confidence == "none"
    assert v.secondary_depth == pytest.approx(0.003, rel=0.2)
    assert has_reason(v, "likely eclipsing binary")


def test_planetary_secondary_stays_strong():
    # Hot-Jupiter-like emission: 1.5% of the transit depth, a clear detection
    # that is listed but doesn't lower the confidence.
    hj = InjectedPlanet(2.2, 0.5, 0.007, 4.0, secondary_depth=0.0001)
    v = vet([hj], snr=500.0)
    assert v.confidence == "strong"
    assert v.secondary_depth == pytest.approx(0.0001, abs=50e-6)
    assert has_reason(v, "planetary emission")
    assert has_reason(v, "odd/even consistent")


def test_planetary_secondary_does_not_hide_other_weakness():
    hj = InjectedPlanet(2.2, 0.5, 0.007, 4.0, secondary_depth=0.0001)
    v = vet([hj], snr=12.0)
    assert v.confidence == "weak"
    assert v.reasons[0] == "marginal SNR 12.0"
    assert has_reason(v, "planetary emission")


def test_deep_secondary_rejected_despite_strong_detection():
    # Same strong, many-transit signal as above, but a secondary 30% of the primary.
    eb = InjectedPlanet(2.2, 0.5, 0.007, 4.0, secondary_depth=0.0015)
    v = vet([eb], snr=500.0)
    assert v.confidence == "none"
    assert has_reason(v, "likely eclipsing binary")
    assert not has_reason(v, "planetary emission")


def test_single_transit_rejected():
    v = vet([PLANET], n_transits=1)
    assert v.confidence == "none"
    assert has_reason(v, "only 1 transit")


def duo(box_hours=None, **cand_kw):
    """Vet a clean 5 h, 4000 ppm transit at P=20 d seen twice in 40 d (at 10 and 30 d)."""
    planet = InjectedPlanet(20.0, 10.0, 0.004, 5.0)
    cand = injected_candidate(planet, n_transits=cand_kw.pop("n_transits", 2), **cand_kw)
    if box_hours is not None:
        cand.duration = box_hours / 24.0
    cleaned = flat_cleaned([planet], baseline_days=40.0)
    return vet_candidate(cleaned, cand, fit_transit(cleaned, cand))


def test_two_clean_transits_are_a_duo_transit_not_a_candidate():
    v = duo()
    assert v.confidence == "duo-transit"
    assert has_reason(v, "only 2 transits observed")
    assert has_reason(v, "not a planet candidate")


def test_two_transits_with_any_doubt_rejected():
    # A marginal SNR leaves a 3-transit signal 'weak'; with only 2 transits it rejects.
    v = duo(snr=12.0)
    assert v.confidence == "none"
    assert v.reasons[0].startswith("only 2 transits observed")
    assert has_reason(v, "marginal SNR 12.0")
    assert vet([PLANET], n_transits=2, snr=12.0).confidence == "none"
    assert vet([PLANET], n_transits=2, with_fit=False).confidence == "none"


@pytest.mark.parametrize("box_hours", [2.0, 12.0])
def test_two_transit_t14_mismatch_rejected(box_hours):
    # The fit finds the real 5 h transit: a 2 h or 12 h detected dip doesn't match its shape.
    v = duo(box_hours)
    assert v.confidence == "none"
    assert has_reason(v, f"disagrees with the {box_hours:.1f} h detected dip")


def test_t14_agreement_is_only_required_of_two_transits():
    # The same 2 h box backed by 3 transits passes: its other events vouch for it.
    assert duo(2.0, n_transits=3).confidence == "strong"


def test_wasp_18_d_like_wide_fit_rejected():
    # A broad 14 h dip detected with a 3.6 h box, seen twice: the fit chases the
    # broad dip (fitted T14 ~4x the box), as for WASP-18 d in the live validation.
    broad = InjectedPlanet(21.9, 8.0, 0.001, 14.0)
    cand = injected_candidate(broad, n_transits=2)
    cand.duration = 3.6 / 24.0
    cleaned = flat_cleaned([broad], baseline_days=40.0)
    v = vet_candidate(cleaned, cand, fit_transit(cleaned, cand))
    assert v.confidence == "none"
    assert has_reason(v, "not transit-shaped")


@pytest.mark.parametrize("snr,sde,confidence", [
    (5.0, 20.0, "none"), (12.0, 20.0, "weak"), (50.0, 8.0, "none"), (50.0, 10.0, "weak"),
    (50.0, 14.4, "strong"),
    # TIC 31410749's 57.63 d noise peak in the live validation.
    (9.4, 10.5, "none"),
])
def test_detection_thresholds(snr, sde, confidence):
    v = vet([PLANET], snr=snr, sde=sde)
    assert v.confidence == confidence


def test_failed_fit_makes_weak():
    v = vet([PLANET], with_fit=False)
    assert v.confidence == "weak"
    assert has_reason(v, "fit failed")


def test_stellar_sized_companion_rejected():
    big = InjectedPlanet(2.7, 0.5, 0.25, 3.1)  # rp/rs ~ 0.5
    v = vet([big])
    assert v.confidence == "none"
    assert has_reason(v, "too large for a planet")


def test_other_planet_not_mistaken_for_secondary():
    # c's transits land near phase 0.5 of b at times; masking c keeps b clean.
    b = InjectedPlanet(2.43, 0.8, 0.003, 2.0)
    c = InjectedPlanet(9.87, 4.4, 0.005, 3.8)
    cand_b, cand_c = injected_candidate(b), injected_candidate(c)
    v = vet([b, c], cand=cand_b, others=[cand_c, cand_b])
    assert v.confidence == "strong"


def test_half_period_alias_of_stronger_signal_rejected():
    eb = InjectedPlanet(1.87, 0.5, 0.02, 3.1, odd_even_ratio=0.6)
    primary = injected_candidate(eb, snr=500.0)
    alias = injected_candidate(InjectedPlanet(0.935, 0.5, 0.012, 3.1), snr=100.0)
    v = vet([eb], cand=alias, others=[primary, alias])
    assert v.confidence == "none"
    assert v.reasons == ["half-period alias of the P=1.8700 d signal, not a separate planet"]


def test_secondary_eclipse_found_as_own_candidate_rejected():
    eb = InjectedPlanet(2.7, 0.5, 0.01, 3.1, secondary_depth=0.003)
    primary = injected_candidate(eb, snr=500.0)
    secondary = injected_candidate(InjectedPlanet(2.7, 0.5 + 1.35, 0.003, 3.1), snr=80.0)
    v = vet([eb], cand=secondary, others=[primary, secondary])
    assert v.confidence == "none"
    assert has_reason(v, "secondary eclipse of the P=2.7000 d signal")


def test_resonant_pair_not_called_alias():
    # Near 2:1 but 1% off, as real resonant pairs are.
    inner = InjectedPlanet(5.66, 1.0, 0.002, 2.5)
    outer = InjectedPlanet(11.43, 3.0, 0.003, 3.2)
    cand_in, cand_out = injected_candidate(inner, snr=60.0), injected_candidate(outer, snr=90.0)
    v = vet([inner, outer], cand=cand_in, others=[cand_out, cand_in])
    assert v.confidence == "strong"


@pytest.mark.parametrize("seed", [1, 2, 3, 16])
def test_red_noise_does_not_look_like_an_eclipsing_binary(seed):
    # 600 ppm noise correlated over 8 h: with white-noise errors these seeds
    # showed odd/even differences of 4.5-5 sigma or a 3+ sigma secondary.
    planet = InjectedPlanet(6.3, 2.0, 0.004, 6.0)
    cleaned = flat_cleaned([planet], seed=seed, red_noise_ppm=600, red_noise_hours=8)
    v = vet_candidate(cleaned, injected_candidate(planet), None)
    assert not has_reason(v, "eclipsing binary")
    assert not has_reason(v, "secondary eclipse")
    assert v.odd_even_sigma < 3


def test_eclipsing_binary_still_flagged_in_red_noise():
    eb = InjectedPlanet(2.7, 0.5, 0.01, 3.1, odd_even_ratio=0.7)
    cleaned = flat_cleaned([eb], red_noise_ppm=600, red_noise_hours=8)
    v = vet_candidate(cleaned, injected_candidate(eb), None)
    assert v.confidence == "none"
    assert has_reason(v, "odd/even depths differ")
