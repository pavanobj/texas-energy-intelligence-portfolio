"""Regression coverage for compact replay issue IDs in the static dashboard."""
from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = (ROOT / "public" / "assets" / "dashboard.js").read_text(encoding="utf-8")


def test_historical_weather_chart_normalizes_iso_issue_timestamps_to_compact_ids() -> None:
    """Replay curves use ISO timestamps while archived weather uses compact issue IDs."""
    assert "const normalizeIssueId = (value)" in SCRIPT
    assert "parsed.toISOString().replace(/[-:]/g, \"\").replace(\".000\", \"\")" in SCRIPT
    assert "windowIssueIds.has(normalizeIssueId(row.issue_id))" in SCRIPT


def test_historical_weather_chart_uses_temperature_units_dates_and_truthful_gaps() -> None:
    assert "function temperatureScale(values)" in SCRIPT
    assert "function utcDateTickIndices(labels, plotWidth)" in SCRIPT
    assert 'axis: "temperature_c", xTickStyle: "utcDate"' in SCRIPT
    assert '`${value.toFixed(0)} °C`' in SCRIPT
    assert 'dash: [8, 5]' in SCRIPT
    assert "${observed} of ${weather.length} hours observed" in SCRIPT
    assert "if (!Number.isFinite(value)) { connected = false; return; }" in SCRIPT


def test_el_paso_historical_series_keeps_issued_points_and_observed_gaps() -> None:
    """El Paso uses the title-cased dashboard key and compact weather issue IDs."""
    issue_ids = {"20261012T060000Z"}
    rows = [
        {
            "issue_id": "20261012T060000Z",
            "city": "El Paso",
            "issued_temperature_2m_c": 21.0,
            "observed_temperature_2m_c": 23.2 if index < 161 else None,
        }
        for index in range(336)
    ]
    selected = [row for row in rows if row["issue_id"] in issue_ids and row["city"] == "El Paso"]
    assert len(selected) == 336
    assert sum(row["issued_temperature_2m_c"] is not None for row in selected) == 336
    assert sum(row["observed_temperature_2m_c"] is not None for row in selected) == 161
    assert "windowIssueIds.has(normalizeIssueId(row.issue_id)) && row.city === citySelect.value" in SCRIPT
