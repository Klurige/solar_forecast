"""Tests for the Solar Forecast Refinement integration."""

from custom_components.solar_forecast.const import (
    CORRECTION_HALF_LIFE_DAYS,
    DOMAIN,
    MAX_HISTORY_YEARS,
    MAX_RATIO,
    MIN_CORRECTION_SAMPLES,
    MIN_RATIO,
    NIGHT_THRESHOLD_W,
    SLOTS_PER_DAY,
)


def test_domain():
    """Domain constant must match the manifest domain."""
    assert DOMAIN == "solar_forecast"


def test_slots_per_day():
    """96 slots = 24 hours × 4 quarters."""
    assert SLOTS_PER_DAY == 96


def test_algorithm_constants_are_positive():
    """All tuning constants must be strictly positive."""
    assert CORRECTION_HALF_LIFE_DAYS > 0
    assert MIN_CORRECTION_SAMPLES > 0
    assert NIGHT_THRESHOLD_W > 0
    assert MAX_HISTORY_YEARS > 0


def test_ratio_bounds():
    """Correction factor bounds must be ordered correctly."""
    assert 0 < MIN_RATIO < 1 < MAX_RATIO
