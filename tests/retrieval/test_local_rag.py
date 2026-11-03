from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

from texas_energy.retrieval.local import (
    KnowledgeIndex,
    RetrievalError,
    SourceSpec,
    answer_from_hits,
    load_documents,
    search_energy_knowledge,
)


class TinySemanticEmbedder:
    """Deterministic test backend that models three topics plus unknowns."""

    model_id = "test/tiny-semantic"

    def embed(self, texts: list[str]) -> np.ndarray:
        vectors = []
        for text in texts:
            lowered = text.lower()
            vectors.append(
                [
                    float(any(word in lowered for word in ("forecast", "demand", "ercot", "load"))),
                    float(any(word in lowered for word in ("weather", "issued", "open-meteo"))),
                    float(any(word in lowered for word in ("metric", "mae", "rmse", "error"))),
                    float(any(word in lowered for word in ("astronomy", "planet", "mars"))),
                ]
            )
        return np.asarray(vectors, dtype=np.float32)


@pytest.fixture()
def source_repo(tmp_path: Path) -> Path:
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "forecast.md").write_text(
        "# Forecast contract\nERCOT demand forecasting produces the next 24 hourly demand values.\n\n"
        "## Weather availability\nIssued weather forecasts are used because observed future weather would leak future information.\n",
        encoding="utf-8",
    )
    return tmp_path


def test_persistent_semantic_index_cites_source_without_absolute_path(source_repo: Path, tmp_path: Path) -> None:
    docs = load_documents([SourceSpec("forecast", "Forecast document", "local_markdown", path="docs/forecast.md")], repo_root=source_repo)
    KnowledgeIndex.build(tmp_path / "index", docs, TinySemanticEmbedder())
    reopened = KnowledgeIndex.open(tmp_path / "index")
    hits = reopened.search("Why use issued weather in the forecast?", TinySemanticEmbedder(), top_k=2)
    answer = answer_from_hits("Why use issued weather in the forecast?", hits)
    assert answer.grounded is True
    assert "repository://docs/forecast.md" in answer.answer
    assert "observed future weather" in answer.answer
    persisted = "\n".join((tmp_path / "index" / name).read_text(encoding="utf-8") for name in ("manifest.json", "chunks.jsonl"))
    assert str(source_repo) not in persisted


def test_out_of_scope_question_abstains(source_repo: Path, tmp_path: Path) -> None:
    docs = load_documents([SourceSpec("forecast", "Forecast document", "local_markdown", path="docs/forecast.md")], repo_root=source_repo)
    index = KnowledgeIndex.build(tmp_path / "index", docs, TinySemanticEmbedder())
    answer = answer_from_hits("Which planet has the tallest mountain?", index.search("Which planet has the tallest mountain?", TinySemanticEmbedder()))
    assert answer.grounded is False
    assert "do not have" in answer.answer


def test_rejects_secret_bearing_and_outside_sources(source_repo: Path) -> None:
    (source_repo / ".env").write_text("EIA_API_KEY=not-a-real-but-long-secret-value\n", encoding="utf-8")
    with pytest.raises(RetrievalError, match="Credential"):
        load_documents([SourceSpec("secret", "Secret", "text", path=".env")], repo_root=source_repo)
    with pytest.raises(RetrievalError, match="inside the repository"):
        load_documents([SourceSpec("outside", "Outside", "text", path="../outside.txt")], repo_root=source_repo)


def test_redacts_documented_api_parameter_values(source_repo: Path) -> None:
    source = source_repo / "docs" / "api.md"
    source.write_text("# API\nUse api_key=placeholder-value-with-many-characters for examples.\n", encoding="utf-8")
    document = load_documents([SourceSpec("api", "API", "local_markdown", path="docs/api.md")], repo_root=source_repo)[0]
    assert "placeholder-value" not in document.text
    assert "api_key=[REDACTED]" in document.text


def test_invalid_query_and_atomic_rebuild(source_repo: Path, tmp_path: Path) -> None:
    docs = load_documents([SourceSpec("forecast", "Forecast document", "local_markdown", path="docs/forecast.md")], repo_root=source_repo)
    backend = TinySemanticEmbedder()
    index_root = tmp_path / "index"
    KnowledgeIndex.build(index_root, docs, backend)
    rebuilt = KnowledgeIndex.build(index_root, docs, backend)
    assert rebuilt.manifest["chunk_count"] == KnowledgeIndex.open(index_root).manifest["chunk_count"]
    with pytest.raises(RetrievalError, match="non-empty"):
        rebuilt.search("", backend)


def test_tool_adapter_accepts_a_string_index_path(source_repo: Path, tmp_path: Path) -> None:
    docs = load_documents([SourceSpec("forecast", "Forecast document", "local_markdown", path="docs/forecast.md")], repo_root=source_repo)
    root = tmp_path / "index"
    KnowledgeIndex.build(root, docs, TinySemanticEmbedder())
    result = search_energy_knowledge("ERCOT demand forecast", index_root=str(root), embedder=TinySemanticEmbedder())
    assert result["tool"] == "search_energy_knowledge"
    assert result["status"] == "ok"


def test_extractive_answer_selects_the_query_relevant_sentence(source_repo: Path, tmp_path: Path) -> None:
    (source_repo / "docs" / "forecast.md").write_text(
        "# Contract\nThe platform has demand data. Issued weather forecasts prevent future observed weather leakage.\n",
        encoding="utf-8",
    )
    docs = load_documents([SourceSpec("forecast", "Forecast document", "local_markdown", path="docs/forecast.md")], repo_root=source_repo)
    index = KnowledgeIndex.build(tmp_path / "index", docs, TinySemanticEmbedder())
    answer = answer_from_hits("Why are issued weather forecasts used?", index.search("Why are issued weather forecasts used?", TinySemanticEmbedder()))
    assert answer.grounded is True
    assert answer.answer.startswith("Issued weather forecasts")


def test_extractive_answer_bounds_a_long_unstructured_passage(source_repo: Path, tmp_path: Path) -> None:
    (source_repo / "docs" / "forecast.md").write_text("# Metrics\nMAE " + ("metric-value " * 100), encoding="utf-8")
    docs = load_documents([SourceSpec("forecast", "Forecast document", "local_markdown", path="docs/forecast.md")], repo_root=source_repo)
    index = KnowledgeIndex.build(tmp_path / "index", docs, TinySemanticEmbedder())
    answer = answer_from_hits("MAE metric", index.search("MAE metric", TinySemanticEmbedder()))
    assert "…" in answer.answer
    assert len(answer.answer) < 650
