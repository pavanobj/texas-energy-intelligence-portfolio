"""Private, bounded Bedrock Converse tool-calling agent for analytics artifacts.

The model never receives a filesystem root or a shell.  It can select only the
six deterministic functions exposed by :mod:`texas_energy.analytics.tools`.
"""
from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Mapping, Protocol

from texas_energy.retrieval import RetrievalError, TransformerEmbeddingBackend, search_energy_knowledge

from .dashboard_tools import DASHBOARD_TOOL_REGISTRY, DashboardDataStore, DashboardToolError
from .tools import (
    AnalyticsArtifactStore,
    AnalyticsToolError,
    MODEL_COLUMNS,
    METRICS,
    TOOL_REGISTRY,
)


class EnergyAgentError(RuntimeError):
    """The private agent could not safely complete a request."""


class EnergyAgentLimitError(EnergyAgentError):
    """A bounded execution limit was reached."""


class ConverseClient(Protocol):
    """Small structural interface that keeps the loop testable offline."""

    def converse(self, **kwargs: Any) -> Mapping[str, Any]: ...


MODEL_IDS = tuple(MODEL_COLUMNS)
RAG_TOOL_NAME = "search_energy_knowledge"

TOOL_SPECS: tuple[dict[str, Any], ...] = (
    {
        "toolSpec": {
            "name": "get_forecast_summary",
            "description": "Get one saved 24-hour historical replay forecast, including peak timing. This is never a live forecast.",
            "inputSchema": {"json": {"type": "object", "properties": {"model_id": {"type": "string", "enum": list(MODEL_IDS)}}, "required": ["model_id"], "additionalProperties": False}},
        }
    },
    {
        "toolSpec": {
            "name": "compare_model_forecasts",
            "description": "Compare two saved models on exactly the same 24 historical replay targets. Disagreement is not error.",
            "inputSchema": {"json": {"type": "object", "properties": {"left_model_id": {"type": "string", "enum": list(MODEL_IDS)}, "right_model_id": {"type": "string", "enum": list(MODEL_IDS)}}, "required": ["left_model_id", "right_model_id"], "additionalProperties": False}},
        }
    },
    {
        "toolSpec": {
            "name": "analyze_forecast_errors",
            "description": "Calculate post-outcome error metrics for a saved historical replay. Actuals were unavailable at issuance.",
            "inputSchema": {"json": {"type": "object", "properties": {"model_id": {"type": "string", "enum": list(MODEL_IDS)}}, "required": ["model_id"], "additionalProperties": False}},
        }
    },
    {
        "toolSpec": {
            "name": "get_model_performance",
            "description": "Rank saved historical validation or test performance, optionally at exactly one forecast horizon.",
            "inputSchema": {"json": {"type": "object", "properties": {"split": {"type": "string", "enum": ["validation", "test"]}, "metric": {"type": "string", "enum": sorted(METRICS)}, "horizon_hours": {"type": "integer", "minimum": 1, "maximum": 24}}, "required": ["split"], "additionalProperties": False}},
        }
    },
    {
        "toolSpec": {
            "name": "analyze_demand_patterns",
            "description": "Describe demand patterns in the saved 24-hour historical replay only.",
            "inputSchema": {"json": {"type": "object", "properties": {}, "additionalProperties": False}},
        }
    },
    {
        "toolSpec": {
            "name": "get_weather_context",
            "description": "Get saved weather-provenance metadata. It must not invent numerical weather values absent from the replay.",
            "inputSchema": {"json": {"type": "object", "properties": {}, "additionalProperties": False}},
        }
    },
    {
        "toolSpec": {
            "name": RAG_TOOL_NAME,
            "description": "Search the private, read-only energy knowledge index for documentation evidence and citations. This does not access arbitrary files or the network.",
            "inputSchema": {
                "json": {
                    "type": "object",
                    "properties": {
                        "question": {"type": "string", "minLength": 1, "maxLength": 200},
                        "top_k": {"type": "integer", "minimum": 1, "maximum": 4},
                    },
                    "required": ["question"],
                    "additionalProperties": False,
                }
            },
        }
    },
)

LIVE_TOOL_SPECS: tuple[dict[str, Any], ...] = (
    {
        "toolSpec": {
            "name": "get_latest_prospective_forecast",
            "description": "Get all three published 24-hour prospective forecast curves, their forecast issue, targets, actual availability, provenance, and peak values.",
            "inputSchema": {"json": {"type": "object", "properties": {}, "additionalProperties": False}},
        }
    },
    {
        "toolSpec": {
            "name": "get_rolling_prospective_metrics",
            "description": "Get 7-day or 14-day metrics from genuine observed ERCOT demand matched to prospective forecast issues. Missing actuals remain pending.",
            "inputSchema": {"json": {"type": "object", "properties": {"window_days": {"type": "integer", "enum": [7, 14]}}, "required": ["window_days"], "additionalProperties": False}},
        }
    },
    {
        "toolSpec": {
            "name": "get_historical_backtest_metrics",
            "description": "Get 7-day or 14-day historical replay backtest metrics. These are retrospective backtests, never prospective live performance.",
            "inputSchema": {"json": {"type": "object", "properties": {"window_days": {"type": "integer", "enum": [7, 14]}}, "required": ["window_days"], "additionalProperties": False}},
        }
    },
    {
        "toolSpec": {
            "name": "get_weather_comparison",
            "description": "Get issued forecast weather and available observations for exactly one approved Texas city, for latest prospective data or a historical backtest.",
            "inputSchema": {"json": {"type": "object", "properties": {"city": {"type": "string", "enum": ["austin", "dallas", "el_paso", "houston", "san_antonio"]}, "scope": {"type": "string", "enum": ["latest_prospective", "historical_backtest"]}}, "required": ["city", "scope"], "additionalProperties": False}},
        }
    },
)

SYSTEM_PROMPT = """You are the private Texas Energy Intelligence Agent.
Use only the supplied read-only tools. Every quantitative claim must come from
a tool result and name its evidence artifact. Use the knowledge-search tool
for documentation claims and retain its citations. The published-dashboard tools
are the source for prospective forecasts and prospective evaluation; say their
issue time, target window, freshness, and missing-observation state. Historical
backtest tools are retrospective and must never be described as live performance.
For prospective forecasts, forecast_issue_utc is when the forecast was issued,
dashboard_published_at_utc is when its dashboard projection was published, and
target_timestamp_utc identifies a predicted hour. Never substitute one timestamp
for another. End every answer with a complete sentence; if detail will not fit,
give a concise complete summary rather than starting an incomplete citation list.
Never invent weather values, actual demand, model performance, causality, or
evidence. Do not request tools outside this allowlist. Keep answers concise and
distinguish model disagreement from forecasting error.
"""


@dataclass(frozen=True)
class AgentLimits:
    max_steps: int = 4
    max_tool_calls: int = 6
    timeout_seconds: float = 45.0
    max_output_tokens: int = 512

    def __post_init__(self) -> None:
        if self.max_steps < 1 or self.max_tool_calls < 1 or self.timeout_seconds <= 0 or self.max_output_tokens < 1:
            raise ValueError("Agent limits must be positive.")


@dataclass(frozen=True)
class ToolObservation:
    name: str
    input: Mapping[str, Any]
    evidence: tuple[Mapping[str, str], ...]


@dataclass(frozen=True)
class AgentAnswer:
    answer: str
    model_id: str
    tool_observations: tuple[ToolObservation, ...]
    usage: Mapping[str, int]
    latency_seconds: float
    model_turns: int
    estimated_cost_usd: None = None
    cost_note: str = "Bedrock responses expose token usage, not an authoritative price; no static price is inferred."

    def as_dict(self) -> dict[str, Any]:
        result = asdict(self)
        result["tool_observations"] = [asdict(item) for item in self.tool_observations]
        return result


def _validate_text(value: object, *, field: str, allowed: set[str] | None = None) -> str:
    if not isinstance(value, str) or not value or len(value) > 200:
        raise EnergyAgentError(f"{field} must be a non-empty short string.")
    if allowed is not None and value not in allowed:
        raise EnergyAgentError(f"{field} is not an allowed value.")
    return value


def _validated_tool_input(name: str, payload: object) -> dict[str, Any]:
    if name not in {*TOOL_REGISTRY, *DASHBOARD_TOOL_REGISTRY, RAG_TOOL_NAME} or not isinstance(payload, Mapping):
        raise EnergyAgentError("Tool request is not allowlisted or has invalid input.")
    values = dict(payload)
    allowed: dict[str, set[str]] = {
        "get_forecast_summary": {"model_id"},
        "compare_model_forecasts": {"left_model_id", "right_model_id"},
        "analyze_forecast_errors": {"model_id"},
        "get_model_performance": {"split", "metric", "horizon_hours"},
        "analyze_demand_patterns": set(),
        "get_weather_context": set(),
        "get_latest_prospective_forecast": set(),
        "get_rolling_prospective_metrics": {"window_days"},
        "get_historical_backtest_metrics": {"window_days"},
        "get_weather_comparison": {"city", "scope"},
        RAG_TOOL_NAME: {"question", "top_k"},
    }
    if set(values).difference(allowed[name]):
        raise EnergyAgentError("Tool request contains unsupported fields.")
    if name in {"get_forecast_summary", "analyze_forecast_errors"}:
        return {"model_id": _validate_text(values.get("model_id"), field="model_id", allowed=set(MODEL_IDS))}
    if name == "compare_model_forecasts":
        return {
            "left_model_id": _validate_text(values.get("left_model_id"), field="left_model_id", allowed=set(MODEL_IDS)),
            "right_model_id": _validate_text(values.get("right_model_id"), field="right_model_id", allowed=set(MODEL_IDS)),
        }
    if name == "get_model_performance":
        result: dict[str, Any] = {"split": _validate_text(values.get("split"), field="split", allowed={"validation", "test"})}
        if "metric" in values:
            result["metric"] = _validate_text(values["metric"], field="metric", allowed=set(METRICS))
        if "horizon_hours" in values:
            horizon = values["horizon_hours"]
            if isinstance(horizon, bool) or not isinstance(horizon, int) or not 1 <= horizon <= 24:
                raise EnergyAgentError("horizon_hours must be an integer from 1 through 24.")
            result["horizon_hours"] = horizon
        return result
    if name == RAG_TOOL_NAME:
        result = {"question": _validate_text(values.get("question"), field="question")}
        if "top_k" in values:
            top_k = values["top_k"]
            if isinstance(top_k, bool) or not isinstance(top_k, int) or not 1 <= top_k <= 4:
                raise EnergyAgentError("top_k must be an integer from 1 through 4.")
            result["top_k"] = top_k
        return result
    if name in {"get_rolling_prospective_metrics", "get_historical_backtest_metrics"}:
        window_days = values.get("window_days")
        if isinstance(window_days, bool) or not isinstance(window_days, int) or window_days not in {7, 14}:
            raise EnergyAgentError("window_days must be 7 or 14.")
        return {"window_days": window_days}
    if name == "get_weather_comparison":
        return {
            "city": _validate_text(values.get("city"), field="city", allowed={"austin", "dallas", "el_paso", "houston", "san_antonio"}),
            "scope": _validate_text(values.get("scope"), field="scope", allowed={"latest_prospective", "historical_backtest"}),
        }
    return {}


class EnergyIntelligenceAgent:
    """A small client-side Bedrock Converse loop over trusted local tools."""

    def __init__(self, *, client: ConverseClient, model_id: str, store: AnalyticsArtifactStore, dashboard_store: DashboardDataStore | None = None, rag_index_root: Path | None = None, rag_embedding_model_path: Path | None = None, limits: AgentLimits = AgentLimits()) -> None:
        self._client, self._model_id, self._store, self._limits = client, _validate_text(model_id, field="model_id"), store, limits
        self._dashboard_store = dashboard_store
        self._rag_index_root = rag_index_root.expanduser().resolve() if rag_index_root is not None else None
        self._rag_embedding_model_path = rag_embedding_model_path.expanduser().resolve() if rag_embedding_model_path is not None else None

    def _execute_tool(self, name: str, payload: object) -> tuple[dict[str, Any], ToolObservation]:
        values = _validated_tool_input(name, payload)
        if name == RAG_TOOL_NAME:
            if self._rag_index_root is None:
                raise EnergyAgentError("Private knowledge retrieval is not configured for this agent.")
            try:
                if self._rag_embedding_model_path is None:
                    observation = search_energy_knowledge(index_root=self._rag_index_root, **values)
                else:
                    observation = search_energy_knowledge(
                        index_root=self._rag_index_root,
                        embedder=TransformerEmbeddingBackend(local_model_path=self._rag_embedding_model_path),
                        **values,
                    )
            except RetrievalError as exc:
                raise EnergyAgentError("Private knowledge retrieval could not complete safely.") from exc
            citations = observation.get("citations", ())
            evidence = tuple({"citation": citation} for citation in citations if isinstance(citation, str))
            return observation, ToolObservation(name=name, input=values, evidence=evidence)
        if name in DASHBOARD_TOOL_REGISTRY:
            if self._dashboard_store is None:
                raise EnergyAgentError("Published dashboard evidence is not configured for this agent.")
            try:
                observation = DASHBOARD_TOOL_REGISTRY[name](self._dashboard_store, **values)
            except DashboardToolError as exc:
                raise EnergyAgentError("Published dashboard data rejected the request.") from exc
            evidence = tuple(observation.get("evidence", ()))
            return observation, ToolObservation(name=name, input=values, evidence=evidence)
        try:
            observation = TOOL_REGISTRY[name](self._store, **values)
        except AnalyticsToolError as exc:
            raise EnergyAgentError("Trusted analytics tool rejected the request.") from exc
        evidence = tuple(observation.get("evidence", ()))
        return observation, ToolObservation(name=name, input=values, evidence=evidence)

    def answer(self, question: str) -> AgentAnswer:
        question = _validate_text(question, field="question")
        started = time.monotonic()
        messages: list[dict[str, Any]] = [{"role": "user", "content": [{"text": question}]}]
        observations: list[ToolObservation] = []
        usage = {"inputTokens": 0, "outputTokens": 0, "totalTokens": 0}
        model_turns = 0
        compact_answer_requested = False
        for _ in range(self._limits.max_steps):
            if time.monotonic() - started > self._limits.timeout_seconds:
                raise EnergyAgentLimitError("Agent execution exceeded its time limit.")
            response = self._client.converse(
                modelId=self._model_id,
                system=[{"text": SYSTEM_PROMPT}],
                messages=messages,
                inferenceConfig={"maxTokens": self._limits.max_output_tokens, "temperature": 0.0},
                toolConfig={"tools": list(TOOL_SPECS) + list(LIVE_TOOL_SPECS)},
            )
            model_turns += 1
            response_usage = response.get("usage", {})
            for key in usage:
                value = response_usage.get(key, 0)
                if isinstance(value, int) and value >= 0:
                    usage[key] += value
            message = response.get("output", {}).get("message")
            if not isinstance(message, Mapping) or not isinstance(message.get("content"), list):
                raise EnergyAgentError("Bedrock response lacks a valid assistant message.")
            tool_uses = [item["toolUse"] for item in message["content"] if isinstance(item, Mapping) and "toolUse" in item]
            if tool_uses:
                messages.append(dict(message))
                results: list[dict[str, Any]] = []
                for tool_use in tool_uses:
                    if len(observations) >= self._limits.max_tool_calls:
                        raise EnergyAgentLimitError("Agent reached its maximum tool-call limit.")
                    if not isinstance(tool_use, Mapping):
                        raise EnergyAgentError("Bedrock tool request is malformed.")
                    name = tool_use.get("name")
                    tool_use_id = tool_use.get("toolUseId")
                    if not isinstance(name, str) or not isinstance(tool_use_id, str):
                        raise EnergyAgentError("Bedrock tool request lacks a name or ID.")
                    try:
                        observation, recorded = self._execute_tool(name, tool_use.get("input", {}))
                        observations.append(recorded)
                        content = [{"text": json.dumps(observation, sort_keys=True, allow_nan=False)}]
                        status = "success"
                    except EnergyAgentError as error:
                        content = [{"text": json.dumps({"error": str(error)}, sort_keys=True)}]
                        status = "error"
                    results.append({"toolResult": {"toolUseId": tool_use_id, "content": content, "status": status}})
                messages.append({"role": "user", "content": results})
                continue
            text = "".join(item.get("text", "") for item in message["content"] if isinstance(item, Mapping) and isinstance(item.get("text"), str)).strip()
            if not text:
                raise EnergyAgentError("Bedrock ended without a textual answer.")
            if not observations:
                raise EnergyAgentError("Agent ended without trusted tool evidence.")
            if response.get("stopReason") == "max_tokens":
                if compact_answer_requested:
                    raise EnergyAgentLimitError("Agent response reached its output limit before a complete answer.")
                compact_answer_requested = True
                messages.append(dict(message))
                messages.append({"role": "user", "content": [{"text": "Your prior answer reached its output limit. Return one complete concise answer in at most 120 words using the trusted tool evidence already retrieved. Preserve forecast_issue_utc, dashboard_published_at_utc, and target_timestamp_utc exactly; do not begin a citation list you cannot finish."}]})
                continue
            return AgentAnswer(answer=text, model_id=self._model_id, tool_observations=tuple(observations), usage=usage, latency_seconds=time.monotonic() - started, model_turns=model_turns)
        raise EnergyAgentLimitError("Agent reached its maximum model-step limit without a final answer.")


def create_bedrock_agent(*, model_id: str, store: AnalyticsArtifactStore, dashboard_store: DashboardDataStore | None = None, rag_index_root: Path | None = None, rag_embedding_model_path: Path | None = None, region_name: str = "us-east-1", profile_name: str | None = None, limits: AgentLimits = AgentLimits()) -> EnergyIntelligenceAgent:
    """Create the private agent with SDK default credentials or an explicit local profile."""

    import boto3
    from botocore.config import Config

    session = boto3.Session(profile_name=profile_name, region_name=region_name)
    client = session.client(
        "bedrock-runtime",
        config=Config(retries={"total_max_attempts": 2, "mode": "adaptive"}, connect_timeout=5, read_timeout=min(30, max(5, int(limits.timeout_seconds))), max_pool_connections=1),
    )
    return EnergyIntelligenceAgent(client=client, model_id=model_id, store=store, dashboard_store=dashboard_store, rag_index_root=rag_index_root, rag_embedding_model_path=rag_embedding_model_path, limits=limits)
