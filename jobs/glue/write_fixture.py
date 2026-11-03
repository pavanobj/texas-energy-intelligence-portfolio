"""Create a tiny valid Bronze schema-1.1 fixture for Glue 5 smoke testing.

The fixture contains 24 ERCOT ERCO/D hours and five city-specific 24-hour
historical-weather envelopes.  It is generated from the production Bronze
envelope constructor rather than hand-maintained JSON so the smoke test uses
the same versioned input contract as ingestion.
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from texas_energy.ingestion.bronze import BronzeSourceTimeRange, bronze_envelope
from texas_energy.ingestion.open_meteo import TexasCity
from texas_energy.ingestion.raw import RawCapture, canonical_json_bytes


START = datetime(2024, 1, 1, tzinfo=timezone.utc)


def _capture(*, source: str, dataset: str, payload: dict[str, object], parameters: dict[str, str]) -> RawCapture:
    return RawCapture(
        source=source,
        dataset=dataset,
        endpoint="https://example.invalid/glue-fixture",
        request_parameters=parameters,
        raw_payload=canonical_json_bytes(payload),
        retrieved_at=datetime(2024, 1, 2, tzinfo=timezone.utc),
        status_code=200,
        response_headers={"Content-Type": "application/json"},
    )


def _write(path: Path, name: str, envelope: dict[str, object]) -> None:
    (path / name).write_text(json.dumps(envelope, sort_keys=True), encoding="utf-8")


def write_fixture(output: Path) -> None:
    output.mkdir(parents=True, exist_ok=True)
    end = START + timedelta(hours=23)
    eia_payload = {
        "response": {
            "data": [
                {
                    "period": (START + timedelta(hours=hour)).strftime("%Y-%m-%dT%H"),
                    "respondent": "ERCO",
                    "type": "D",
                    "value": str(40_000 + hour),
                    "value-units": "megawatthours",
                }
                for hour in range(24)
            ]
        }
    }
    _write(
        output,
        "eia-erco-demand.json",
        bronze_envelope(
            _capture(
                source="eia",
                dataset="electricity/rto/region-data",
                payload=eia_payload,
                parameters={"respondent": "ERCO", "type": "D", "frequency": "hourly"},
            ),
            source_time_range=BronzeSourceTimeRange(start=START, end=end),
            units={"value": "megawatthours"},
        ),
    )
    timestamps = [int((START + timedelta(hours=hour)).timestamp()) for hour in range(24)]
    for city_index, city in enumerate(TexasCity):
        payload = {
            "latitude": city.latitude,
            "longitude": city.longitude,
            "hourly": {
                "time": timestamps,
                "temperature_2m": [10.0 + city_index + hour / 10 for hour in range(24)],
                "relative_humidity_2m": [50 + city_index for _ in range(24)],
                "wind_speed_10m": [5.0 + hour / 10 for hour in range(24)],
            },
        }
        _write(
            output,
            f"open-meteo-{city.slug}-historical.json",
            bronze_envelope(
                _capture(
                    source="open_meteo",
                    dataset="historical-weather",
                    payload=payload,
                    parameters={"latitude": str(city.latitude), "longitude": str(city.longitude), "models": "era5"},
                ),
                source_time_range=BronzeSourceTimeRange(start=START, end=end),
                units={"temperature_2m": "°C", "relative_humidity_2m": "%", "wind_speed_10m": "km/h"},
            ),
        )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    write_fixture(args.output)
    print(json.dumps({"bronze_envelopes": 6, "output": str(args.output)}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
