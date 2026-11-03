"""Static dashboard contract checks; no browser backend or inference is needed."""
from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
PUBLIC = ROOT / "public"


def test_dashboard_is_buildless_static_and_uses_relative_published_json() -> None:
    html = (PUBLIC / "index.html").read_text(encoding="utf-8")
    script = (PUBLIC / "assets" / "dashboard.js").read_text(encoding="utf-8")
    assert "dashboard.js" in html
    assert "./data/" in script
    assert "FastAPI" not in html + script
    assert "/forecast" not in script
    assert "historical_replay" in script


def test_dashboard_has_static_accessibility_and_responsive_contracts() -> None:
    html = (PUBLIC / "index.html").read_text(encoding="utf-8")
    css = (PUBLIC / "assets" / "dashboard.css").read_text(encoding="utf-8")
    assert "Skip to dashboard content" in html
    assert 'role="img"' in html
    assert 'aria-live="polite"' in html
    assert "@media(max-width:880px)" in css
    assert "@media(max-width:500px)" in css


def test_published_replay_is_24_hourly_points_and_honestly_labelled() -> None:
    forecast = json.loads((PUBLIC / "data" / "latest-forecast.json").read_text())
    assert forecast["status"] == "historical_replay"
    assert forecast["historical_not_live"] is True
    assert len(forecast["predictions"]) == 24
    origin = datetime.fromisoformat(forecast["forecast_origin_utc"])
    for horizon, row in enumerate(forecast["predictions"], start=1):
        assert row["horizon_hours"] == horizon
        assert datetime.fromisoformat(row["target_timestamp_utc"]) == origin + timedelta(hours=horizon)


def test_weather_and_ai_empty_states_do_not_invent_values() -> None:
    weather = json.loads((PUBLIC / "data" / "weather-summary.json").read_text())
    html = (PUBLIC / "index.html").read_text(encoding="utf-8")
    assert weather["weather_values_available"] is False
    assert "metadata_only" in weather["status"]
    assert "AI briefings coming soon" in html


def test_performance_contains_real_validation_and_test_metrics() -> None:
    performance = json.loads((PUBLIC / "data" / "model-performance.json").read_text())
    splits = {row["evaluation_split"] for row in performance["leaderboard"]}
    assert {"validation", "test"}.issubset(splits)
    assert all(row["mae_mw"] > 0 for row in performance["leaderboard"])
    assert len(performance["per_horizon"]) > 0
    validation = [row for row in performance["leaderboard"] if row["evaluation_split"] == "validation"]
    winner = min(validation, key=lambda row: row["mae_mw"])["model_id"]
    assert performance["selected_local_default"] == winner == "bilstm_curve_weather_aware"
