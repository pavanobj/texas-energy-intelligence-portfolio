"""Small, local, evidence-first RAG retrieval without a managed vector store.

The module deliberately separates retrieval from LLM generation.  It stores
only reviewed document text, embeddings, and source metadata in a local index;
the deterministic answer is extractive and explicitly not an LLM response.
"""
from __future__ import annotations

import hashlib
import html.parser
import json
import math
import re
import shutil
import tempfile
import urllib.parse
import urllib.request
import uuid
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol, Sequence

import numpy as np


INDEX_SCHEMA_VERSION = "1.0"
DEFAULT_EMBEDDING_MODEL = "sentence-transformers/all-MiniLM-L6-v2"
MAX_DOCUMENT_BYTES = 2_000_000
MAX_QUERY_CHARACTERS = 1_000
OFFICIAL_SOURCE_HOSTS = {"api.eia.gov", "www.eia.gov", "www.ercot.com", "open-meteo.com"}
_UNSAFE_PATH_PARTS = {".env", ".git", "__pycache__", ".aws", ".ssh"}
_SECRET_PATTERNS = (
    re.compile(r"AKIA[0-9A-Z]{16}"),
)
_SENSITIVE_VALUE_PATTERN = re.compile(
    r"(?i)(?P<prefix>(?:api[_-]?key|secret|token|password)\s*(?:[:=]|%3[dD])\s*)(?P<value>[^\s&#'\"`]+)"
)
_KEYWORD_STOPWORDS = {"a", "an", "and", "are", "at", "do", "for", "from", "how", "in", "is", "of", "on", "or", "the", "to", "what", "which", "why", "with"}


class RetrievalError(ValueError):
    """A local knowledge source, index, or retrieval request is invalid."""


@dataclass(frozen=True)
class SourceSpec:
    source_id: str
    title: str
    kind: str
    source_url: str | None = None
    path: str | None = None
    version: str | None = None


@dataclass(frozen=True)
class Document:
    source_id: str
    title: str
    source_uri: str
    version: str | None
    retrieved_at_utc: str
    text: str
    content_sha256: str


@dataclass(frozen=True)
class KnowledgeChunk:
    chunk_id: str
    source_id: str
    title: str
    section: str
    source_uri: str
    version: str | None
    retrieved_at_utc: str
    text: str
    content_sha256: str


@dataclass(frozen=True)
class RetrievalHit:
    chunk_id: str
    title: str
    section: str
    source_uri: str
    version: str | None
    retrieved_at_utc: str
    score: float
    text: str

    def citation(self) -> str:
        return f"{self.title} — {self.section} ({self.source_uri})"


@dataclass(frozen=True)
class RetrievalAnswer:
    question: str
    answer: str
    grounded: bool
    hits: tuple[RetrievalHit, ...]
    limitation: str

    def as_dict(self) -> dict[str, Any]:
        return {**asdict(self), "hits": [asdict(hit) | {"citation": hit.citation()} for hit in self.hits]}


class EmbeddingBackend(Protocol):
    @property
    def model_id(self) -> str: ...

    def embed(self, texts: Sequence[str]) -> np.ndarray: ...


class TransformerEmbeddingBackend:
    """Mean-pooled, normalized transformer sentence embeddings on local CPU/GPU."""

    _shared_models: dict[tuple[str, str], tuple[Any, Any, Any]] = {}

    def __init__(self, model_id: str = DEFAULT_EMBEDDING_MODEL, *, local_model_path: Path | str | None = None, device: str = "cpu", batch_size: int = 16) -> None:
        self._model_id = model_id
        self.local_model_path = Path(local_model_path).expanduser().resolve() if local_model_path is not None else None
        self.device = device
        self.batch_size = batch_size
        self._tokenizer: Any | None = None
        self._model: Any | None = None
        self._torch: Any | None = None

    @property
    def model_id(self) -> str:
        return self._model_id

    def _load(self) -> None:
        if self._model is not None:
            return
        model_source = str(self.local_model_path) if self.local_model_path is not None else self.model_id
        shared = self._shared_models.get((model_source, self.device))
        if shared is not None:
            self._tokenizer, self._model, self._torch = shared
            return
        try:
            import torch
            from transformers import AutoModel, AutoTokenizer
        except ImportError as exc:  # pragma: no cover - environment-specific
            raise RetrievalError("Local embeddings require the optional retrieval dependencies. Install with: pip install '.[retrieval]'") from exc
        if self.device == "cuda" and not torch.cuda.is_available():
            raise RetrievalError("CUDA embeddings were requested but CUDA is unavailable.")
        self._torch = torch
        try:
            # Retrieval serves a persisted local index.  Never let an interactive
            # query contact Hugging Face or refresh model metadata.
            self._tokenizer = AutoTokenizer.from_pretrained(model_source, trust_remote_code=False, local_files_only=True)
            self._model = AutoModel.from_pretrained(model_source, trust_remote_code=False, local_files_only=True).to(self.device)
        except OSError as exc:  # no raw remote details in the user-facing error
            raise RetrievalError(f"The local embedding model {self.model_id!r} is unavailable. Download it once from a trusted Hugging Face source, then retry.") from exc
        self._model.eval()
        self._shared_models[(model_source, self.device)] = (self._tokenizer, self._model, self._torch)

    def embed(self, texts: Sequence[str]) -> np.ndarray:
        if not texts:
            return np.empty((0, 0), dtype=np.float32)
        self._load()
        assert self._tokenizer is not None and self._model is not None and self._torch is not None
        vectors: list[np.ndarray] = []
        with self._torch.inference_mode():
            for offset in range(0, len(texts), self.batch_size):
                batch = list(texts[offset : offset + self.batch_size])
                encoded = self._tokenizer(batch, padding=True, truncation=True, max_length=256, return_tensors="pt")
                encoded = {name: value.to(self.device) for name, value in encoded.items()}
                output = self._model(**encoded).last_hidden_state
                mask = encoded["attention_mask"].unsqueeze(-1).expand(output.size()).float()
                pooled = (output * mask).sum(dim=1) / mask.sum(dim=1).clamp(min=1e-9)
                pooled = self._torch.nn.functional.normalize(pooled, p=2, dim=1)
                vectors.append(pooled.detach().cpu().numpy().astype(np.float32, copy=False))
        return np.vstack(vectors)


class _TextExtractor(html.parser.HTMLParser):
    """Conservative HTML-to-text parser for explicitly allowlisted official pages."""

    def __init__(self) -> None:
        super().__init__()
        self._parts: list[str] = []
        self._ignored_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in {"script", "style", "noscript", "svg"}:
            self._ignored_depth += 1
        elif tag in {"p", "br", "li", "h1", "h2", "h3", "h4", "tr"}:
            self._parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in {"script", "style", "noscript", "svg"} and self._ignored_depth:
            self._ignored_depth -= 1

    def handle_data(self, data: str) -> None:
        if not self._ignored_depth:
            self._parts.append(data)

    def text(self) -> str:
        return _normalize_text(" ".join(self._parts))


def _now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _normalize_text(value: str) -> str:
    return "\n".join(" ".join(line.split()) for line in value.replace("\r\n", "\n").splitlines() if line.strip())


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _tokens(value: str) -> list[str]:
    return [token for token in re.findall(r"[a-z0-9]+", value.lower()) if token not in _KEYWORD_STOPWORDS]


def _sanitize_text(value: str, *, source_id: str) -> str:
    """Remove documented/example credential values before persistence.

    Official API documentation commonly contains placeholder query parameters;
    parameter *names* remain useful retrieval evidence, but their values never
    belong in a local index.
    """
    for pattern in _SECRET_PATTERNS:
        if pattern.search(value):
            raise RetrievalError(f"Source {source_id!r} appears to contain a credential and was not indexed.")
    return _SENSITIVE_VALUE_PATTERN.sub(r"\g<prefix>[REDACTED]", value)


def _safe_repo_path(repo_root: Path, relative_path: str) -> tuple[Path, str]:
    candidate = (repo_root / relative_path).resolve()
    try:
        relative = candidate.relative_to(repo_root.resolve())
    except ValueError as exc:
        raise RetrievalError("Local knowledge sources must stay inside the repository root.") from exc
    if any(part.lower() in _UNSAFE_PATH_PARTS for part in relative.parts) or any("secret" in part.lower() or "credential" in part.lower() for part in relative.parts):
        raise RetrievalError("Credential, environment, cache, and repository-control files cannot be indexed.")
    if candidate.suffix.lower() not in {".md", ".markdown", ".txt", ".pdf"}:
        raise RetrievalError("Only Markdown, text, and PDF knowledge sources are supported.")
    return candidate, relative.as_posix()


def _read_pdf(path: Path) -> str:
    try:
        from pypdf import PdfReader
    except ImportError as exc:  # pragma: no cover - dependency dependent
        raise RetrievalError("PDF ingestion requires pypdf. Install with: pip install '.[retrieval]'") from exc
    try:
        reader = PdfReader(str(path))
        text = "\n".join(page.extract_text() or "" for page in reader.pages)
    except Exception as exc:  # pypdf's exceptions can expose filesystem details
        raise RetrievalError("The PDF could not be read as text.") from exc
    return _normalize_text(text)


def _read_url(url: str, *, timeout_seconds: int = 15) -> str:
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme != "https" or parsed.query or parsed.fragment or parsed.hostname not in OFFICIAL_SOURCE_HOSTS:
        raise RetrievalError("External knowledge sources must use an allowlisted official HTTPS URL without a query or fragment.")
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": "Mozilla/5.0 (compatible; TexasEnergyKnowledgeIndexer/1.0)",
            "Accept": "text/html,application/xhtml+xml",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
            if response.status != 200:
                raise RetrievalError(f"Official source returned HTTP {response.status}.")
            raw = response.read(MAX_DOCUMENT_BYTES + 1)
    except RetrievalError:
        raise
    except Exception as exc:  # no raw URLs, headers, or provider body are surfaced
        raise RetrievalError("Official source could not be retrieved.") from exc
    if len(raw) > MAX_DOCUMENT_BYTES:
        raise RetrievalError("Official source exceeds the document size limit.")
    parser = _TextExtractor()
    parser.feed(raw.decode("utf-8", errors="replace"))
    return parser.text()


def load_documents(specs: Sequence[SourceSpec], *, repo_root: Path) -> list[Document]:
    documents: list[Document] = []
    for spec in specs:
        if bool(spec.path) == bool(spec.source_url):
            raise RetrievalError(f"Source {spec.source_id!r} must define exactly one of path or source_url.")
        if spec.path:
            path, portable_path = _safe_repo_path(repo_root, spec.path)
            if not path.is_file():
                raise RetrievalError(f"Configured local knowledge source is missing: {portable_path}")
            raw = _read_pdf(path) if path.suffix.lower() == ".pdf" else _normalize_text(path.read_text(encoding="utf-8"))
            source_uri = f"repository://{portable_path}"
            version = spec.version or datetime.fromtimestamp(path.stat().st_mtime, tz=UTC).date().isoformat()
        else:
            assert spec.source_url is not None
            try:
                raw = _read_url(spec.source_url)
            except RetrievalError as exc:
                raise RetrievalError(f"Official source {spec.source_id!r} could not be indexed safely.") from exc
            source_uri = spec.source_url
            version = spec.version
        if not raw:
            raise RetrievalError(f"Source {spec.source_id!r} contains no extractable text.")
        raw = _sanitize_text(raw, source_id=spec.source_id)
        documents.append(Document(spec.source_id, spec.title, source_uri, version, _now(), raw, _sha256_text(raw)))
    return documents


def _sections(document: Document) -> list[tuple[str, str]]:
    current = "Overview"
    buffers: list[str] = []
    sections: list[tuple[str, str]] = []
    for line in document.text.splitlines():
        match = re.match(r"^#{1,6}\s+(.+)$", line)
        if match:
            if buffers:
                sections.append((current, " ".join(buffers)))
                buffers = []
            current = match.group(1).strip()
        else:
            buffers.append(line)
    if buffers:
        sections.append((current, " ".join(buffers)))
    return [(section, text) for section, text in sections if text.strip()]


def chunk_document(document: Document, *, chunk_words: int = 180, overlap_words: int = 40) -> list[KnowledgeChunk]:
    if chunk_words <= 0 or not 0 <= overlap_words < chunk_words:
        raise RetrievalError("chunk_words must be positive and overlap_words must be smaller than chunk_words.")
    chunks: list[KnowledgeChunk] = []
    for section_number, (section, text) in enumerate(_sections(document), start=1):
        words = text.split()
        for start in range(0, len(words), chunk_words - overlap_words):
            part = words[start : start + chunk_words]
            if not part:
                break
            chunks.append(KnowledgeChunk(f"{document.source_id}:{section_number}:{start}", document.source_id, document.title, section, document.source_uri, document.version, document.retrieved_at_utc, " ".join(part), document.content_sha256))
            if start + chunk_words >= len(words):
                break
    return chunks


class KnowledgeIndex:
    """Persistent cosine-similarity index backed by JSONL metadata and NumPy arrays."""

    def __init__(self, root: Path, chunks: Sequence[KnowledgeChunk], embeddings: np.ndarray, manifest: dict[str, Any]) -> None:
        self.root = root.expanduser().resolve()
        self.chunks = tuple(chunks)
        self.embeddings = np.asarray(embeddings, dtype=np.float32)
        self.manifest = dict(manifest)
        if self.embeddings.ndim != 2 or self.embeddings.shape[0] != len(self.chunks):
            raise RetrievalError("Knowledge index embeddings and chunk metadata are inconsistent.")
        if not np.isfinite(self.embeddings).all():
            raise RetrievalError("Knowledge index embeddings must be finite.")

    @classmethod
    def build(cls, root: Path, documents: Sequence[Document], embedder: EmbeddingBackend) -> "KnowledgeIndex":
        chunks = [chunk for document in documents for chunk in chunk_document(document)]
        if not chunks:
            raise RetrievalError("No usable chunks were extracted from the configured knowledge sources.")
        embeddings = np.asarray(embedder.embed([chunk.text for chunk in chunks]), dtype=np.float32)
        if embeddings.ndim != 2 or embeddings.shape[0] != len(chunks) or not np.isfinite(embeddings).all():
            raise RetrievalError("Embedding backend returned invalid vectors.")
        norms = np.linalg.norm(embeddings, axis=1)
        if np.any(norms <= 0):
            raise RetrievalError("Embedding backend returned a zero vector.")
        embeddings = embeddings / norms[:, None]
        manifest = {"schema_version": INDEX_SCHEMA_VERSION, "created_at_utc": _now(), "embedding_model_id": embedder.model_id, "retrieval_algorithm": "hybrid_cosine_keyword_v1", "chunk_count": len(chunks), "embedding_dimensions": int(embeddings.shape[1]), "sources": [{"source_id": document.source_id, "title": document.title, "source_uri": document.source_uri, "version": document.version, "retrieved_at_utc": document.retrieved_at_utc, "content_sha256": document.content_sha256} for document in documents]}
        index = cls(root, chunks, embeddings, manifest)
        index._write_atomically()
        return index

    @classmethod
    def open(cls, root: Path) -> "KnowledgeIndex":
        root = root.expanduser().resolve()
        try:
            manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
            chunks = [KnowledgeChunk(**json.loads(line)) for line in (root / "chunks.jsonl").read_text(encoding="utf-8").splitlines() if line]
            embeddings = np.load(root / "embeddings.npy", allow_pickle=False)
        except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
            raise RetrievalError("Knowledge index is missing or corrupt; rebuild it from trusted sources.") from exc
        if manifest.get("schema_version") != INDEX_SCHEMA_VERSION:
            raise RetrievalError("Knowledge index schema version is unsupported; rebuild it.")
        return cls(root, chunks, embeddings, manifest)

    def _write_atomically(self) -> None:
        parent = self.root.parent
        parent.mkdir(parents=True, exist_ok=True)
        temporary = Path(tempfile.mkdtemp(prefix=f".{self.root.name}.tmp-", dir=parent))
        try:
            (temporary / "manifest.json").write_text(json.dumps(self.manifest, indent=2, sort_keys=True, allow_nan=False) + "\n", encoding="utf-8")
            (temporary / "chunks.jsonl").write_text("".join(json.dumps(asdict(chunk), sort_keys=True, allow_nan=False) + "\n" for chunk in self.chunks), encoding="utf-8")
            with (temporary / "embeddings.npy").open("wb") as handle:
                np.save(handle, self.embeddings, allow_pickle=False)
            backup = self.root.with_name(f".{self.root.name}.previous-{uuid.uuid4().hex}")
            if self.root.exists():
                self.root.rename(backup)
            try:
                temporary.rename(self.root)
            except Exception:
                if backup.exists() and not self.root.exists():
                    backup.rename(self.root)
                raise
            if backup.exists():
                shutil.rmtree(backup)
        except Exception:
            shutil.rmtree(temporary, ignore_errors=True)
            raise

    def search(self, question: str, embedder: EmbeddingBackend, *, top_k: int = 4, minimum_score: float = 0.20) -> tuple[RetrievalHit, ...]:
        if not isinstance(question, str) or not question.strip() or len(question) > MAX_QUERY_CHARACTERS:
            raise RetrievalError(f"question must be a non-empty string no longer than {MAX_QUERY_CHARACTERS} characters.")
        if not 1 <= top_k <= 10:
            raise RetrievalError("top_k must be between 1 and 10.")
        query = np.asarray(embedder.embed([question.strip()]), dtype=np.float32)
        if query.shape != (1, self.embeddings.shape[1]) or not np.isfinite(query).all():
            raise RetrievalError("Embedding backend returned an incompatible query vector.")
        norm = float(np.linalg.norm(query[0]))
        if norm <= 0:
            raise RetrievalError("Embedding backend returned a zero query vector.")
        cosine_scores = self.embeddings @ (query[0] / norm)
        query_tokens = _tokens(question)
        query_unique = set(query_tokens)
        document_frequency = {token: sum(token in set(_tokens(chunk.text)) for chunk in self.chunks) for token in query_unique}
        keyword_scores = []
        for chunk in self.chunks:
            chunk_tokens = set(_tokens(chunk.text))
            keyword_scores.append(sum(math.log((len(self.chunks) + 1) / (document_frequency[token] + 1)) + 1 for token in query_unique if token in chunk_tokens))
        keyword_max = max(keyword_scores, default=1.0) or 1.0
        scores = 0.65 * cosine_scores + 0.35 * (np.asarray(keyword_scores, dtype=np.float32) / keyword_max)
        order = sorted(range(len(scores)), key=lambda index: (-float(scores[index]), self.chunks[index].chunk_id))
        hits = []
        seen_sections: set[tuple[str, str]] = set()
        for index in order:
            score = float(scores[index])
            if score < minimum_score:
                break
            chunk = self.chunks[index]
            section_key = (chunk.source_id, chunk.section)
            if section_key in seen_sections:
                continue
            seen_sections.add(section_key)
            hits.append(RetrievalHit(chunk.chunk_id, chunk.title, chunk.section, chunk.source_uri, chunk.version, chunk.retrieved_at_utc, score, chunk.text))
            if len(hits) == top_k:
                break
        return tuple(hits)


def answer_from_hits(question: str, hits: Sequence[RetrievalHit]) -> RetrievalAnswer:
    if not hits:
        return RetrievalAnswer(question, "I do not have sufficiently relevant trusted documentation to answer that question.", False, (), "This is local semantic retrieval, not an LLM answer. Add a reviewed source or rephrase the question.")
    excerpts = []
    question_tokens = set(_tokens(question))
    for hit in hits[:3]:
        sentences = [sentence for sentence in re.split(r"(?<=[.!?])\s+", hit.text.strip()) if sentence]
        sentence = max(sentences, key=lambda item: (len(question_tokens.intersection(_tokens(item))), -sentences.index(item)))
        if len(sentence) > 480:
            sentence = sentence[:477].rsplit(" ", 1)[0] + "…"
        excerpts.append(f"{sentence} [{hit.citation()}]")
    return RetrievalAnswer(question, " ".join(excerpts), True, tuple(hits), "Extractive local retrieval only; it does not perform LLM reasoning or infer facts beyond the cited passages.")


def _load_specs(manifest_path: Path, *, include_official: bool) -> list[SourceSpec]:
    try:
        entries = json.loads(manifest_path.read_text(encoding="utf-8"))["sources"]
    except (OSError, KeyError, TypeError, json.JSONDecodeError) as exc:
        raise RetrievalError("Knowledge source manifest is missing or invalid.") from exc
    return [SourceSpec(source_id=entry["source_id"], title=entry["title"], kind=entry["kind"], path=entry.get("path"), source_url=entry.get("source_url"), version=entry.get("version")) for entry in entries if include_official or not entry.get("official")]


def build_default_index(repo_root: Path, index_root: Path, *, include_official: bool = False, embedder: EmbeddingBackend | None = None) -> KnowledgeIndex:
    """Build a persistent index from reviewed project docs and optional official pages."""
    specs = _load_specs(repo_root / "config" / "energy_knowledge_sources.json", include_official=include_official)
    documents = load_documents(specs, repo_root=repo_root)
    return KnowledgeIndex.build(index_root, documents, embedder or TransformerEmbeddingBackend())


def search_energy_knowledge(question: str, top_k: int = 4, *, index_root: Path | str, embedder: EmbeddingBackend | None = None) -> dict[str, Any]:
    """Read-only retrieval tool suitable for a future bounded agent adapter."""
    index = KnowledgeIndex.open(Path(index_root))
    backend = embedder or TransformerEmbeddingBackend(str(index.manifest["embedding_model_id"]))
    answer = answer_from_hits(question, index.search(question, backend, top_k=top_k))
    return {"tool": "search_energy_knowledge", "status": "ok" if answer.grounded else "out_of_scope", "question": question, "answer": answer.answer, "historical_or_operational_limitation": answer.limitation, "citations": [hit.citation() for hit in answer.hits], "results": [asdict(hit) for hit in answer.hits], "embedding_model_id": index.manifest["embedding_model_id"], "index_schema_version": index.manifest["schema_version"]}
