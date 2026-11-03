"""Private, read-only MCP transport for existing Energy Intelligence tools.

This module deliberately owns no forecasting or retrieval business logic.  A
caller injects the current artifact roots at process startup; future S3-backed
adapters can preserve the same seven MCP tool contracts.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from mcp.server import MCPServer

from texas_energy.retrieval import search_energy_knowledge

from .tools import AnalyticsArtifactStore, TOOL_REGISTRY


ModelId = Literal[
    "xgboost_curve_weather_aware",
    "bilstm_curve_weather_aware",
    "amazon/chronos-2",
]
EvaluationSplit = Literal["validation", "test"]
MetricName = Literal["mae_mw", "rmse_mw", "mape_pct", "smape_pct"]


class EnergyMcpError(RuntimeError):
    """A safe public-facing MCP error."""


@dataclass(frozen=True)
class EnergyMcpDependencies:
    """Injected local artifact gateways, intentionally independent of transport."""

    store: AnalyticsArtifactStore
    rag_index_root: Path

    @classmethod
    def from_paths(cls, *, closeout_root: Path, rag_index_root: Path) -> "EnergyMcpDependencies":
        index = rag_index_root.expanduser().resolve()
        if not index.is_dir():
            raise EnergyMcpError("Private knowledge index is unavailable.")
        return cls(store=AnalyticsArtifactStore(closeout_root), rag_index_root=index)


def _safe_analytics(name: str, dependencies: EnergyMcpDependencies, **kwargs: Any) -> dict[str, Any]:
    try:
        return TOOL_REGISTRY[name](dependencies.store, **kwargs)
    except Exception:
        # Artifact readers can include local paths in OS-level error messages.
        # The MCP boundary returns a stable error and intentionally has no
        # exception chain for a client or standard-error log to disclose.
        raise EnergyMcpError("Trusted analytics evidence could not satisfy this request.") from None


def _safe_rag(dependencies: EnergyMcpDependencies, *, question: str, top_k: int = 4) -> dict[str, Any]:
    if not isinstance(question, str) or not question.strip() or len(question) > 200:
        raise EnergyMcpError("question must be a non-empty string no longer than 200 characters.")
    if isinstance(top_k, bool) or not isinstance(top_k, int) or not 1 <= top_k <= 4:
        raise EnergyMcpError("top_k must be an integer from 1 through 4.")
    try:
        return search_energy_knowledge(question.strip(), top_k, index_root=dependencies.rag_index_root)
    except Exception:
        raise EnergyMcpError("Private knowledge retrieval could not complete safely.") from None


def create_energy_mcp_server(dependencies: EnergyMcpDependencies) -> MCPServer:
    """Create the seven-tool server; transport selection is left to the launcher."""

    server = MCPServer(
        "Texas Energy Intelligence",
        description="Private, read-only historical forecast analytics and energy knowledge retrieval.",
        instructions="All responses are historical evidence or documentation retrieval, never live inference.",
    )

    @server.tool()
    def get_forecast_summary(model_id: ModelId = "bilstm_curve_weather_aware") -> dict[str, Any]:
        """Return one bounded 24-hour historical replay, including forecast peak evidence."""
        return _safe_analytics("get_forecast_summary", dependencies, model_id=model_id)

    @server.tool()
    def compare_model_forecasts(left_model_id: ModelId, right_model_id: ModelId) -> dict[str, Any]:
        """Compare two models on exactly the same 24 historical replay targets."""
        return _safe_analytics("compare_model_forecasts", dependencies, left_model_id=left_model_id, right_model_id=right_model_id)

    @server.tool()
    def analyze_forecast_errors(model_id: ModelId) -> dict[str, Any]:
        """Calculate post-outcome error metrics for one historical replay."""
        return _safe_analytics("analyze_forecast_errors", dependencies, model_id=model_id)

    @server.tool()
    def get_model_performance(split: EvaluationSplit, metric: MetricName = "mae_mw", horizon_hours: int | None = None) -> dict[str, Any]:
        """Rank saved validation or test performance, optionally for one horizon."""
        if horizon_hours is not None and (isinstance(horizon_hours, bool) or not 1 <= horizon_hours <= 24):
            raise EnergyMcpError("horizon_hours must be an integer from 1 through 24.")
        return _safe_analytics("get_model_performance", dependencies, split=split, metric=metric, horizon_hours=horizon_hours)

    @server.tool()
    def analyze_demand_patterns() -> dict[str, Any]:
        """Describe only the 24 actual demand values in the saved historical replay."""
        return _safe_analytics("analyze_demand_patterns", dependencies)

    @server.tool()
    def get_weather_context() -> dict[str, Any]:
        """Return saved weather provenance metadata; unavailable values are never invented."""
        return _safe_analytics("get_weather_context", dependencies)

    @server.tool()
    def search_energy_knowledge(question: str, top_k: int = 4) -> dict[str, Any]:
        """Search the private energy knowledge index and return bounded citations and excerpts."""
        return _safe_rag(dependencies, question=question, top_k=top_k)

    return server
