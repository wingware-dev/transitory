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


def test_small_secondary_is_only_weak():
    # Hot-Jupiter-like emission: 1.5% of the transit depth.
    hj = InjectedPlanet(2.2, 0.5, 0.007, 4.0, secondary_depth=0.0001)
    v = vet([hj])
    assert v.confidence == "weak"
    assert has_reason(v, "planetary emission")


def test_single_transit_rejected():
    v = vet([PLANET], n_transits=1)
    assert v.confidence == "none"
    assert has_reason(v, "only 1 transit")


def test_two_transits_weak():
    assert vet([PLANET], n_transits=2).confidence == "weak"


@pytest.mark.parametrize("snr,sde,confidence", [
    (5.0, 20.0, "none"), (8.0, 20.0, "weak"), (50.0, 6.0, "none"), (50.0, 8.0, "weak"),
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
