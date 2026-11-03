"""Regression coverage for Glue-compatible location handling.

These checks deliberately require no Spark runtime: they protect the boundary
that keeps object-store locations as URIs when a caller injects a Glue session.
"""

from texas_energy.spark.gold import _weather_lineage_output
from texas_energy.spark.silver import delta_location


def test_delta_location_preserves_object_store_uri() -> None:
    uri = "s3://texas-energy-test/silver/demand"

    assert delta_location(uri) == uri


def test_weather_lineage_output_is_an_s3_sibling() -> None:
    assert _weather_lineage_output("s3://texas-energy-test/gold") == (
        "s3://texas-energy-test/gold-weather-lineage"
    )
