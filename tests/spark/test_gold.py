"""Temporary-path Spark/Delta integration tests for causal Gold V1 features."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
import importlib.util
from pathlib import Path

import pytest

from texas_energy.data.gold import CANONICAL_WEATHER_CITIES, GoldFeatureConfig
from texas_energy.spark import gold
from texas_energy.spark import silver


_CITY_COORDINATES = {
    "dallas": (32.7767, -96.7970, 32.794376, -96.8215),
    "houston": (29.7604, -95.3698, 29.771528, -95.33099),
    "austin": (30.2672, -97.7431, 30.263618, -97.69321),
    "san_antonio": (29.4241, -98.4936, 29.420034, -98.52194),
    "el_paso": (31.7619, -106.4850, 31.739893, -106.42258),
}


def _demand_rows(*, periods: int = 24, value_offset: int = 0) -> list[dict[str, object]]:
    start = datetime(2024, 1, 1, tzinfo=timezone.utc)
    return [
        {
            "source": "eia",
            "dataset": "electricity/rto/region-data",
            "period_utc": start + timedelta(hours=index),
            "respondent": "ERCO",
            "demand_type": "D",
            "demand_value": Decimal(40_000 + value_offset + index),
            "demand_unit": "megawatthours",
            "source_request_fingerprint": "a" * 64,
            "source_raw_payload_sha256": "b" * 64,
            "sanitized_payload_sha256": "c" * 64,
            "ingested_at_utc": datetime(2024, 1, 2, tzinfo=timezone.utc),
            "bronze_uri": "file:///bronze/eia.json",
            "quality_status": "valid",
            "quality_flags": [],
        }
        for index in range(periods)
    ]


def _weather_rows(*, periods: int = 24, omit: tuple[str, int] | None = None) -> list[dict[str, object]]:
    start = datetime(2024, 1, 1, tzinfo=timezone.utc)
    rows: list[dict[str, object]] = []
    for city_index, city in enumerate(CANONICAL_WEATHER_CITIES):
        requested_latitude, requested_longitude, provider_latitude, provider_longitude = _CITY_COORDINATES[city]
        for hour in range(periods):
            if omit == (city, hour):
                continue
            rows.append(
                {
                    "source": "open_meteo",
                    "dataset": "historical-weather",
                    "city": city,
                    "requested_latitude": requested_latitude,
                    "requested_longitude": requested_longitude,
                    "provider_latitude": provider_latitude,
                    "provider_longitude": provider_longitude,
                    "provider_coordinate_distance_km": 1.0 + city_index,
                    "observed_at_utc": start + timedelta(hours=hour),
                    "temperature_2m": 10.0 + city_index + hour,
                    "relative_humidity_2m": 50.0 + city_index,
                    "wind_speed_10m": 5.0 + hour,
                    "temperature_unit": "°C",
                    "relative_humidity_unit": "%",
                    "wind_speed_unit": "km/h",
                    "source_request_fingerprint": (str(city_index) * 64),
                    "source_raw_payload_sha256": "d" * 64,
                    "sanitized_payload_sha256": "e" * 64,
                    "ingested_at_utc": datetime(2024, 1, 2, tzinfo=timezone.utc),
                    "bronze_uri": f"file:///bronze/{city}.json",
                    "quality_status": "valid",
                    "quality_flags": [],
                }
            )
    return rows


def _write_silver(
    *,
    silver_input: Path,
    demand_rows: list[dict[str, object]],
    weather_rows: list[dict[str, object]],
    mode: str = "errorifexists",
) -> None:
    spark, _, schemas = silver._spark_runtime()
    try:
        (
            spark.createDataFrame(demand_rows, schema=schemas["demand"])
            .write.format("delta")
            .mode(mode)
            .save((silver_input / "demand").resolve().as_uri())
        )
        (
            spark.createDataFrame(weather_rows, schema=schemas["weather"])
            .write.format("delta")
            .mode(mode)
            .save((silver_input / "weather_historical").resolve().as_uri())
        )
    finally:
        spark.stop()


@pytest.mark.skipif(
    not (importlib.util.find_spec("pyspark") and importlib.util.find_spec("delta")),
    reason="PySpark and delta-spark are optional local dependencies.",
)
def test_gold_v1_pivots_weather_preserves_units_and_is_idempotent(tmp_path: Path) -> None:
    silver_input = tmp_path / "silver-v2"
    gold_output = tmp_path / "gold-v1"
    _write_silver(silver_input=silver_input, demand_rows=_demand_rows(), weather_rows=_weather_rows())

    assert gold.run_local_gold_job(silver_input=silver_input, gold_output=gold_output) == 24
    assert gold.run_local_gold_job(silver_input=silver_input, gold_output=gold_output) == 24

    spark, delta_table, _ = silver._spark_runtime()
    try:
        result = spark.read.format("delta").load(gold_output.resolve().as_uri())
        weather_lineage = spark.read.format("delta").load(_lineage_output(gold_output).resolve().as_uri())
        assert result.count() == 24
        assert result.select("period_utc").distinct().count() == 24
        assert weather_lineage.count() == 120
        assert weather_lineage.select("period_utc", "city").distinct().count() == 120
        assert weather_lineage.groupBy("period_utc").count().filter("count != 5").count() == 0
        assert {row.city for row in weather_lineage.select("city").distinct().collect()} == set(CANONICAL_WEATHER_CITIES)
        assert set(weather_lineage.columns) == {
            "period_utc",
            "city",
            "weather_source",
            "weather_dataset",
            "weather_source_request_fingerprint",
            "weather_source_raw_payload_sha256",
            "weather_sanitized_payload_sha256",
            "weather_ingested_at_utc",
            "weather_bronze_uri",
            "weather_lineage_reference",
        }
        for city in CANONICAL_WEATHER_CITIES:
            actual_city_rows = weather_lineage.filter(f"city = '{city}'").orderBy("period_utc").collect()
            expected_city_rows = [row for row in _weather_rows() if row["city"] == city]
            assert len(actual_city_rows) == len(expected_city_rows) == 24
            for lineage_row, weather_row in zip(actual_city_rows, expected_city_rows, strict=True):
                assert lineage_row.weather_source == weather_row["source"]
                assert lineage_row.weather_dataset == weather_row["dataset"]
                assert lineage_row.weather_source_request_fingerprint == weather_row["source_request_fingerprint"]
                assert lineage_row.weather_source_raw_payload_sha256 == weather_row["source_raw_payload_sha256"]
                assert lineage_row.weather_sanitized_payload_sha256 == weather_row["sanitized_payload_sha256"]
                assert lineage_row.weather_bronze_uri == weather_row["bronze_uri"]
                assert len(lineage_row.weather_lineage_reference) == 64
        assert result.filter("period_utc IS NULL OR observed_demand_value_mwh IS NULL").count() == 0
        assert {row.demand_unit for row in result.select("demand_unit").distinct().collect()} == {"megawatthours"}
        assert {row.weather_availability for row in result.select("weather_availability").distinct().collect()} == {
            "historical_observed"
        }
        assert not {column for column in result.columns if column.startswith("target_")}
        expected_weather = {
            f"{city}_observed_{feature}"
            for city in CANONICAL_WEATHER_CITIES
            for feature in ("temperature_2m_c", "relative_humidity_pct", "wind_speed_10m_kmh")
        }
        assert expected_weather.issubset(result.columns)
        ordered = result.orderBy("period_utc")
        first = ordered.first()
        sixth = ordered.collect()[5]
        last = ordered.collect()[-1]
        assert first.demand_lag_1h_mwh is None
        assert first.demand_trailing_mean_24h_mwh is None
        assert last.demand_lag_24h_mwh is None
        assert last.demand_lag_168h_mwh is None
        assert sixth.demand_lag_1h_mwh == Decimal("40004.000000000000")
        # At t=05 there are only five preceding observations. A partial
        # 24-hour window is explicit warm-up, never a mislabeled 24-hour mean.
        assert sixth.demand_trailing_mean_24h_mwh is None
        assert sixth.dallas_observed_temperature_2m_c == 15.0
        assert sixth.observed_average_demand_mw == sixth.observed_demand_value_mwh
        history = delta_table.forPath(spark, gold_output.resolve().as_uri()).history().orderBy("version", ascending=False)
        latest = history.first()
        if latest.operation == "MERGE":
            assert latest.operationMetrics.get("numTargetRowsUpdated", "0") == "0"
        lineage_history = delta_table.forPath(spark, _lineage_output(gold_output).resolve().as_uri()).history()
        lineage_latest = lineage_history.orderBy("version", ascending=False).first()
        if lineage_latest.operation == "MERGE":
            assert lineage_latest.operationMetrics.get("numTargetRowsUpdated", "0") == "0"
    finally:
        spark.stop()

    revised_weather = _weather_rows()
    revised_weather[0] = {
        **revised_weather[0],
        "temperature_2m": 99.0,
        "source_request_fingerprint": "f" * 64,
        "source_raw_payload_sha256": "g" * 64,
        "sanitized_payload_sha256": "h" * 64,
        "bronze_uri": "file:///bronze/dallas-revision.json",
    }
    _write_silver(
        silver_input=silver_input,
        demand_rows=_demand_rows(value_offset=1_000),
        weather_rows=revised_weather,
        mode="overwrite",
    )
    assert gold.run_local_gold_job(silver_input=silver_input, gold_output=gold_output) == 24
    spark, _, _ = silver._spark_runtime()
    try:
        revised = spark.read.format("delta").load(gold_output.resolve().as_uri()).orderBy("period_utc").first()
        revised_lineage = (
            spark.read.format("delta")
            .load(_lineage_output(gold_output).resolve().as_uri())
            .filter("city = 'dallas'")
            .orderBy("period_utc")
            .first()
        )
        assert revised.observed_demand_value_mwh == Decimal("41000.000000000000")
        assert revised.dallas_observed_temperature_2m_c == 99.0
        assert revised_lineage.weather_source_request_fingerprint == "f" * 64
        assert revised_lineage.weather_source_raw_payload_sha256 == "g" * 64
        assert revised_lineage.weather_sanitized_payload_sha256 == "h" * 64
        assert revised_lineage.weather_bronze_uri == "file:///bronze/dallas-revision.json"
    finally:
        spark.stop()


@pytest.mark.skipif(
    not (importlib.util.find_spec("pyspark") and importlib.util.find_spec("delta")),
    reason="PySpark and delta-spark are optional local dependencies.",
)
def test_gold_rejects_incomplete_weather_or_duplicate_demand_before_writing(tmp_path: Path) -> None:
    missing_silver = tmp_path / "missing-silver"
    missing_gold = tmp_path / "missing-gold"
    _write_silver(
        silver_input=missing_silver,
        demand_rows=_demand_rows(),
        weather_rows=_weather_rows(omit=("dallas", 4)),
    )
    with pytest.raises(gold.GoldValidationError, match="missing at least one configured city-hour"):
        gold.run_local_gold_job(silver_input=missing_silver, gold_output=missing_gold)
    assert not missing_gold.exists()

    duplicate_silver = tmp_path / "duplicate-silver"
    duplicate_gold = tmp_path / "duplicate-gold"
    duplicated = _demand_rows()
    duplicated.append(duplicated[0].copy())
    _write_silver(silver_input=duplicate_silver, demand_rows=duplicated, weather_rows=_weather_rows())
    with pytest.raises(gold.GoldValidationError, match="duplicate business keys"):
        gold.run_local_gold_job(silver_input=duplicate_silver, gold_output=duplicate_gold)
    assert not duplicate_gold.exists()

    misaligned_silver = tmp_path / "misaligned-silver"
    misaligned_gold = tmp_path / "misaligned-gold"
    misaligned_weather = _weather_rows()
    misaligned_weather[0]["observed_at_utc"] = datetime(2024, 1, 1, 0, 30, tzinfo=timezone.utc)
    _write_silver(
        silver_input=misaligned_silver,
        demand_rows=_demand_rows(),
        weather_rows=misaligned_weather,
    )
    with pytest.raises(gold.GoldValidationError, match="timestamps must align exactly to UTC hours"):
        gold.run_local_gold_job(silver_input=misaligned_silver, gold_output=misaligned_gold)
    assert not misaligned_gold.exists()

    gap_silver = tmp_path / "gap-silver"
    gap_gold = tmp_path / "gap-gold"
    demand_with_gap = _demand_rows()
    del demand_with_gap[5]
    _write_silver(silver_input=gap_silver, demand_rows=demand_with_gap, weather_rows=_weather_rows())
    assert gold.run_local_gold_job(silver_input=gap_silver, gold_output=gap_gold) == 23
    spark, _, _ = silver._spark_runtime()
    try:
        result = spark.read.format("delta").load(gap_gold.resolve().as_uri()).orderBy("period_utc").collect()
        first_after_gap = result[5]
        assert first_after_gap.demand_lag_1h_mwh is None
        assert first_after_gap.demand_trailing_mean_24h_mwh is None
    finally:
        spark.stop()


@pytest.mark.skipif(
    not (importlib.util.find_spec("pyspark") and importlib.util.find_spec("delta")),
    reason="PySpark and delta-spark are optional local dependencies.",
)
def test_core_demand_gold_keeps_strict_weather_enrichment_separate(tmp_path: Path) -> None:
    silver_input = tmp_path / "core-demand-silver"
    core_output = tmp_path / "core-demand-gold"
    _write_silver(
        silver_input=silver_input,
        demand_rows=_demand_rows(),
        weather_rows=_weather_rows(omit=("dallas", 4)),
    )
    assert gold.run_core_demand_gold_job(silver_input=silver_input, gold_output=core_output) == 24
    spark, _, _ = silver._spark_runtime()
    try:
        result = spark.read.format("delta").load(core_output.resolve().as_uri())
        assert result.count() == 24
        assert {row.weather_availability for row in result.select("weather_availability").distinct().collect()} == {
            "not_required_for_core_demand"
        }
        assert not [column for column in result.columns if "_observed_temperature_" in column]
        assert result.select("period_utc").distinct().count() == 24
    finally:
        spark.stop()


@pytest.mark.skipif(
    not (importlib.util.find_spec("pyspark") and importlib.util.find_spec("delta")),
    reason="PySpark and delta-spark are optional local dependencies.",
)
def test_core_demand_gold_writes_partial_observed_weather_lineage_without_fabrication(tmp_path: Path) -> None:
    silver_input = tmp_path / "core-demand-lineage-silver"
    core_output = tmp_path / "core-demand-lineage-gold"
    lineage_output = tmp_path / "core-demand-observed-weather-lineage"
    _write_silver(
        silver_input=silver_input,
        demand_rows=_demand_rows(),
        weather_rows=_weather_rows(omit=("dallas", 4)),
    )

    assert gold.run_core_demand_gold_job(
        silver_input=silver_input,
        gold_output=core_output,
        observed_weather_lineage_output=lineage_output,
    ) == 24
    spark, _, _ = silver._spark_runtime()
    try:
        lineage = spark.read.format("delta").load(lineage_output.resolve().as_uri())
        assert lineage.count() == 119
        assert lineage.select("period_utc", "city").distinct().count() == 119
        assert lineage.filter("city = 'dallas'").count() == 23
        assert not lineage.filter("period_utc = timestamp '2024-01-01 04:00:00' AND city = 'dallas'").count()
    finally:
        spark.stop()


@pytest.mark.skipif(
    not (importlib.util.find_spec("pyspark") and importlib.util.find_spec("delta")),
    reason="PySpark and delta-spark are optional local dependencies.",
)
def test_core_demand_gold_refuses_partial_paired_v2_contract(tmp_path: Path) -> None:
    silver_input = tmp_path / "core-demand-partial-silver"
    core_output = tmp_path / "core-demand-partial-gold"
    lineage_output = tmp_path / "core-demand-partial-lineage"
    _write_silver(silver_input=silver_input, demand_rows=_demand_rows(), weather_rows=_weather_rows())
    assert gold.run_core_demand_gold_job(silver_input=silver_input, gold_output=core_output) == 24

    with pytest.raises(gold.GoldSchemaMigrationRequired, match="only one required Delta product"):
        gold.run_core_demand_gold_job(
            silver_input=silver_input,
            gold_output=core_output,
            observed_weather_lineage_output=lineage_output,
        )


@pytest.mark.skipif(
    not (importlib.util.find_spec("pyspark") and importlib.util.find_spec("delta")),
    reason="PySpark and delta-spark are optional local dependencies.",
)
def test_gold_refuses_to_migrate_an_existing_feature_only_output_in_place(tmp_path: Path) -> None:
    silver_input = tmp_path / "silver-v2"
    existing_gold = tmp_path / "existing-gold-v1"
    _write_silver(silver_input=silver_input, demand_rows=_demand_rows(), weather_rows=_weather_rows())

    spark, _, _ = silver._spark_runtime()
    try:
        demand = spark.read.format("delta").load((silver_input / "demand").resolve().as_uri())
        weather = spark.read.format("delta").load((silver_input / "weather_historical").resolve().as_uri())
        features = gold.build_gold_features(
            spark=spark,
            demand=demand,
            historical_weather=weather,
            config=GoldFeatureConfig(),
        )
        features.write.format("delta").mode("errorifexists").save(existing_gold.resolve().as_uri())
    finally:
        spark.stop()

    with pytest.raises(gold.GoldSchemaMigrationRequired, match="Do not modify it in place"):
        gold.run_local_gold_job(silver_input=silver_input, gold_output=existing_gold)
    assert not _lineage_output(existing_gold).exists()


def _lineage_output(gold_output: Path) -> Path:
    return gold_output.with_name(f"{gold_output.name}-weather-lineage")
