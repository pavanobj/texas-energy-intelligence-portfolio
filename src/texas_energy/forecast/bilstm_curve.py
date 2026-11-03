"""Reusable weather-aware direct 24-hour Bi-LSTM architecture.

The architecture is deliberately defined outside a training script so that the
prospective runner and the SageMaker batch runtime load the exact checkpoint
that the weather-aware trainer writes. It does not create or train weights.
"""

from __future__ import annotations


CITIES = ("austin", "dallas", "el_paso", "houston", "san_antonio")
WEATHER_COLUMNS = tuple(
    f"weather_{city}_{field}"
    for city in CITIES
    for field in (
        "temperature_2m_c",
        "relative_humidity_pct",
        "apparent_temperature_c",
        "wind_speed_10m_kmh",
    )
)


def build_direct_curve(torch: object, weather_features: int, use_weather: bool = True):
    """Return the checkpoint-compatible 168-hour-context, 24-step model."""

    class DirectCurve(torch.nn.Module):
        def __init__(self) -> None:
            super().__init__()
            self.encoder1 = torch.nn.LSTM(1, 128, batch_first=True, bidirectional=True)
            self.encoder2 = torch.nn.LSTM(256, 64, batch_first=True, bidirectional=True)
            self.weather = torch.nn.Linear(weather_features, 32) if use_weather else None
            self.decoder = torch.nn.LSTM(128 + (32 if use_weather else 0), 64, batch_first=True)
            self.head = torch.nn.Linear(64, 1)

        def forward(self, demand, weather):
            encoded, _ = self.encoder1(demand)
            encoded, _ = self.encoder2(encoded)
            last = encoded[:, -1:].expand(-1, 24, -1)
            if self.weather is not None:
                last = torch.cat((last, torch.relu(self.weather(weather))), dim=-1)
            decoded, _ = self.decoder(last)
            return self.head(decoded).squeeze(-1)

    return DirectCurve()
