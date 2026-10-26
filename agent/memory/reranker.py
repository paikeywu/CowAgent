"""
Pluggable reranker for memory retrieval.

A reranker scores each candidate's text against the query directly, which is
usually more accurate than the fused cosine / BM25 score, and is used to
reorder the hybrid-search candidates. It is off by default and selected by
``rerank_provider`` in config.json.

Providers are registered by name, so a local model and a remote rerank API are
interchangeable behind the same ``Reranker`` interface. This module imports
nothing beyond the standard library: a provider's own dependencies (and any
model download) are only touched once a configured reranker scores its first
query.
"""

import math
import threading
from abc import ABC, abstractmethod
from typing import Callable, Dict, List, Optional, Tuple

from common.log import logger

# bge-reranker-base has a 512-token input limit, which matches the 500-char
# ``snippet`` already carried by SearchResult.
DEFAULT_RERANK_MODEL = "BAAI/bge-reranker-base"

# Where the containerised reranker listens by default (see docker/reranker/).
# The main process only speaks HTTP here, so it never loads torch itself.
DEFAULT_RERANK_URL = "http://127.0.0.1:9898"


class Reranker(ABC):
    """Scores candidate documents against a query."""

    @abstractmethod
    def rerank(self, query: str, documents: List[str]) -> List[float]:
        """Score each document's relevance to the query.

        Returned scores are aligned 1:1 with ``documents``; higher is better.
        Raise on failure: the caller keeps the un-reranked order.

        The PROVIDERS return scores on the retrieval scale — the top
        candidate is 1.0, the rest decay below it — so a reranked score can
        be compared with ``min_score`` like any other retrieval score. That
        matters because the raw cross-encoder value cannot: a
        bge-reranker-v2-m3 logit for a clearly relevant pair is often -4, and
        a threshold of 0.1 applied to it would discard every candidate. The
        ORDER is computed on the raw value, which keeps the model's full
        resolution; the SCORE is reported on the common scale.
        """


def _rank_normalize_scores(scores: List[float]) -> List[float]:
    """Map scores onto the retrieval scale: the top hit becomes 1.0.

    The same shape ``MemoryManager._rank_normalize`` gives the fusion
    channels, so a reranked result and a fused result carry comparable
    numbers and a threshold means the same thing in both places.
    """
    n = len(scores)
    if n == 0:
        return []
    order = sorted(range(n), key=lambda i: scores[i], reverse=True)
    normalized = [0.0] * n
    for rank, index in enumerate(order):
        normalized[index] = (n - rank) / n
    return normalized



class SentenceTransformerReranker(Reranker):
    """Local cross-encoder backed by ``sentence_transformers.CrossEncoder``.

    Construction is cheap: the dependency is imported and the model loaded
    (downloaded on first use) when the first query is scored. A failed load is
    remembered so later searches fail fast instead of retrying the download.
    """

    def __init__(self, model_name: str = DEFAULT_RERANK_MODEL):
        self.model_name = model_name
        self._model = None
        self._load_error: Optional[Exception] = None
        # CrossEncoder.predict is not documented as thread-safe, and sessions
        # share one instance, so loading and scoring are serialized.
        self._lock = threading.Lock()

    def rerank(self, query: str, documents: List[str]) -> List[float]:
        if not documents:
            return []
        with self._lock:
            model = self._load()
            # CrossEncoder applies a Sigmoid to the model's raw logit by
            # default (sentence-transformers >= 2.3), so the value it returns
            # is already in [0, 1]. Applying _sigmoid() to that would be a
            # second sigmoid: every score collapses toward 0.5 (0.53 / 0.50 /
            # 0.50 for strong / weak / unrelated), which flattens the ranking
            # and pushes everything above the min_score threshold. Request the
            # raw logit explicitly and normalize once, ourselves.
            pairs = [(query, doc) for doc in documents]
            try:
                logits = model.predict(pairs, activation_fct=_identity_activation())
            except TypeError:
                # Older sentence-transformers (or a test double) do not accept
                # ``activation_fct``. Their output is raw logits already when
                # num_labels != 1, but a Sigmoid when it is 1 — indistinguishable
                # from here, so fall back to the documented default behaviour
                # and treat the result as a probability.
                logits = model.predict(pairs)
                return _rank_normalize_scores(
                    [_clamp_probability(float(score)) for score in logits]
                )
        # The logits are the cross-encoder's own scale, and the ranking is
        # computed on them; the score handed back is on the common retrieval
        # scale, so it can be compared with min_score like any other score.
        return _rank_normalize_scores([float(score) for score in logits])

    def _load(self):
        if self._model is not None:
            return self._model
        if self._load_error is not None:
            raise RuntimeError(
                f"rerank model '{self.model_name}' is unavailable"
            ) from self._load_error
        try:
            from sentence_transformers import CrossEncoder

            logger.info(f"[Reranker] Loading local rerank model '{self.model_name}'")
            self._model = CrossEncoder(self.model_name)
        except ImportError as e:
            self._load_error = e
            logger.warning(
                "[Reranker] sentence-transformers is not installed; rerank is "
                "skipped. Install it with: pip install sentence-transformers"
            )
            raise
        except Exception as e:
            self._load_error = e
            logger.error(f"[Reranker] Failed to load rerank model '{self.model_name}': {e}")
            raise
        return self._model


def _sigmoid(value: float) -> float:
    """Squash a cross-encoder logit into [0, 1], overflow-safe."""
    if value >= 0:
        return 1.0 / (1.0 + math.exp(-value))
    exp = math.exp(value)
    return exp / (1.0 + exp)


def _clamp_probability(value: float) -> float:
    """Clamp a value that is already a probability into [0, 1].

    Used on the legacy path where the model's activation is unknown: a
    Sigmoid output is already a probability, and clamping keeps it in the
    documented range instead of letting an out-of-range value through.
    """
    if value < 0.0:
        return 0.0
    if value > 1.0:
        return 1.0
    return value


def _identity_activation():
    """Return a no-op activation, so ``CrossEncoder.predict`` yields raw logits.

    A plain lambda instead of ``torch.nn.Identity()``: the main CowAgent
    process may not have torch installed at all (the reranker can run in a
    separate container instead), and this module must stay importable — and
    usable — without any heavy dependency. CrossEncoder only calls the
    activation on the logits tensor, so a plain callable is enough.
    """
    return lambda logits: logits


class HttpReranker(Reranker):
    """Remote reranker that calls a cross-encoder served over HTTP.

    Used when the model runs in a separate container (see ``docker/reranker/``)
    instead of inside this process. The container owns the model weights, so
    the main process never loads torch — which matters because every Agent
    session shares one Python process and the model is ~2.3GB.

    The endpoint contract matches the container's ``POST /rerank``:
    ``{"query": str, "documents": [str, ...]}`` -> ``{"scores": [float, ...]}``.
    Scores come back aligned 1:1 with ``documents``, exactly like the local
    provider, so the caller cannot tell the two apart.

    The container also returns the raw ``logits``. They are used when
    present: the Sigmoid probability saturates (25 candidate pairs measured,
    only 5 above 0.1 — the differences between 0.00002 and 0.001 are all the
    resolution left), while the logits keep the cross-encoder's full
    ordering. The ranking is what decides the result, so it is computed from
    the logits and the score is reported on the common retrieval scale.
    """

    def __init__(self, model_name: str = "", base_url: Optional[str] = None,
                 timeout: int = 30):
        self.model_name = model_name
        self.base_url = (base_url or DEFAULT_RERANK_URL).rstrip("/")
        self.timeout = timeout
        self._warned = False

    def rerank(self, query: str, documents: List[str]) -> List[float]:
        if not documents:
            return []
        import requests

        payload = {"query": query, "documents": documents}
        if self.model_name:
            payload["model"] = self.model_name
        try:
            response = requests.post(
                f"{self.base_url}/rerank", json=payload, timeout=self.timeout
            )
            response.raise_for_status()
            body = response.json()
        except Exception as e:
            # The caller keeps the un-reranked order when this raises, so a
            # dead container degrades to plain hybrid search instead of
            # breaking memory search outright.
            logger.warning(
                f"[Reranker] HTTP rerank failed ({self.base_url}): "
                f"{type(e).__name__}: {e}"
            )
            raise

        scores = body.get("scores")
        if not isinstance(scores, list) or len(scores) != len(documents):
            raise ValueError(
                f"[Reranker] HTTP rerank returned {len(scores) if isinstance(scores, list) else 'no'} "
                f"scores for {len(documents)} documents"
            )
        values = [float(s) for s in scores]

        # Prefer the raw logits when the service provides them: they carry
        # the cross-encoder's full resolution, which the Sigmoid saturates
        # away. A service that predates the field still works — the
        # probability is then the only signal, and the order it induces is
        # the same order.
        logits = body.get("logits")
        if isinstance(logits, list) and len(logits) == len(documents):
            try:
                values = [float(v) for v in logits]
            except (TypeError, ValueError):
                pass

        return _rank_normalize_scores(values)


def _make_http_reranker(model: str) -> Reranker:
    """Build the HTTP reranker, taking its endpoint from config.

    Reading config here (not at import time) keeps the factory cheap and
    lets a config reload take effect on the next instance.
    """
    try:
        from config import conf

        url = conf().get("rerank_url", "") or ""
        timeout = int(conf().get("rerank_timeout", 30) or 30)
    except Exception:
        url, timeout = "", 30
    return HttpReranker(model, base_url=url or None, timeout=timeout)


# Provider name -> factory taking the configured model name ("" = provider
# default). Factories must stay cheap: no heavy imports, no network, no model
# loading. A remote provider reads its own endpoint / key settings from config.
_PROVIDERS: Dict[str, Callable[[str], Reranker]] = {
    "local": lambda model: SentenceTransformerReranker(model or DEFAULT_RERANK_MODEL),
    "http": _make_http_reranker,
}

# One instance per (provider, model) for the whole process: every session's
# MemoryManager shares it, so a local model is loaded at most once.
_instances: Dict[Tuple[str, str], Reranker] = {}
_instances_lock = threading.Lock()


def register_reranker_provider(name: str, factory: Callable[[str], Reranker]) -> None:
    """Register (or replace) a reranker provider under ``name``."""
    key = name.strip().lower()
    with _instances_lock:
        _PROVIDERS[key] = factory
        for cached in [k for k in _instances if k[0] == key]:
            del _instances[cached]


def create_reranker(provider: Optional[str], model: Optional[str] = None) -> Optional[Reranker]:
    """Return the shared reranker for ``provider``, or None when disabled.

    An empty provider means rerank is off. An unknown provider is logged and
    treated as off, so a typo never breaks memory search. So is a non-string value.
    """
    name = provider.strip().lower() if isinstance(provider, str) else ""
    if not name:
        return None
    factory = _PROVIDERS.get(name)
    if factory is None:
        logger.warning(
            f"[Reranker] Unknown rerank_provider '{name}', rerank is disabled. "
            f"Available: {', '.join(sorted(_PROVIDERS))}"
        )
        return None
    key = (name, model.strip() if isinstance(model, str) else "")
    with _instances_lock:
        reranker = _instances.get(key)
        if reranker is None:
            reranker = factory(key[1])
            _instances[key] = reranker
    return reranker


def create_default_reranker() -> Optional[Reranker]:
    """Build the reranker selected by ``rerank_provider`` / ``rerank_model``."""
    from config import conf

    return create_reranker(conf().get("rerank_provider", ""), conf().get("rerank_model", ""))


def clear_reranker_cache() -> None:
    """Drop the shared instances (for tests and config reloads)."""
    with _instances_lock:
        _instances.clear()
