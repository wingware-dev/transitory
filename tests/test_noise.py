"""Red-noise beta factor on white and correlated synthetic noise."""

import numpy as np
import pytest

from transitory.pipeline.noise import beta_factor
from transitory.synthetic import red_noise

CADENCE = 10.0 / 1440.0
TIME = np.arange(0.0, 54.0, CADENCE)


def white(seed=1, sigma=300e-6, time=TIME):
    return 1.0 + np.random.default_rng(seed).normal(0.0, sigma, time.shape)


@pytest.mark.parametrize("hours", [1.0, 3.0, 8.0])
def test_white_noise_gives_one(hours):
    assert beta_factor(TIME, white(), hours / 24.0) == pytest.approx(1.0, abs=0.1)


def test_never_below_one():
    # Anti-correlated noise bins down faster than white noise.
    flux = 1.0 + 300e-6 * (-1.0) ** np.arange(len(TIME))
    assert beta_factor(TIME, flux, 3.0 / 24.0) == 1.0


def test_red_noise_grows_with_timescale():
    rng = np.random.default_rng(2)
    flux = white() + red_noise(TIME, 300e-6, 8.0 / 24.0, rng)
    betas = [beta_factor(TIME, flux, h / 24.0) for h in (1.0, 3.0, 8.0)]
    assert 1.5 < betas[0] < betas[1] < betas[2]


def test_red_noise_beta_matches_binned_scatter():
    # For bins of N points, beta = binned rms / (point rms / sqrt(N)).
    rng = np.random.default_rng(3)
    flux = white(3) + red_noise(TIME, 400e-6, 6.0 / 24.0, rng)
    width = 3.0 / 24.0
    n = int(round(width / CADENCE))
    m = len(TIME) // n
    binned = flux[:m * n].reshape(m, n).mean(axis=1)
    expected = np.std(binned) / (np.std(flux) / np.sqrt(n))
    assert beta_factor(TIME, flux, width) == pytest.approx(expected, rel=0.25)


def test_gaps_and_unsorted_input():
    keep = (TIME < 20.0) | (TIME > 26.0)
    time, flux = TIME[keep], white()[keep]
    order = np.random.default_rng(4).permutation(len(time))
    assert beta_factor(time[order], flux[order], 3.0 / 24.0) == pytest.approx(1.0, abs=0.1)


@pytest.mark.parametrize("time,flux,timescale", [
    (TIME[:5], white()[:5], 0.1),  # too few points
    (TIME, np.ones_like(TIME), 0.1),  # no scatter
    (TIME, white(), 0.0),  # no timescale
    (TIME, white(), 40.0),  # too few bins
])
def test_degenerate_input_gives_one(time, flux, timescale):
    assert beta_factor(time, flux, timescale) == 1.0
