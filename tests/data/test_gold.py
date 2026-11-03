"""Unit tests for the dependency-free Gold V1 configuration contract."""

import pytest

from texas_energy.data.gold import GoldConfigurationError, GoldFeatureConfig


def test_gold_feature_config_defaults_preserve_the_reviewed_lag_contract() -> None:
    config = GoldFeatureConfig()

    assert config.lag_hours == (1, 24, 168)
    assert config.trailing_window_hours == (24, 168)


@pytest.mark.parametrize(
    ("lag_hours", "trailing_window_hours"),
    (
        ((), (24,)),
        ((1, 1), (24,)),
        ((1, 0), (24,)),
        ((1,), (24, 24)),
        ((1,), (0,)),
    ),
)
def test_gold_feature_config_rejects_ambiguous_or_noncausal_hour_windows(
    lag_hours: tuple[int, ...], trailing_window_hours: tuple[int, ...]
) -> None:
    with pytest.raises(GoldConfigurationError):
        GoldFeatureConfig(lag_hours=lag_hours, trailing_window_hours=trailing_window_hours)
