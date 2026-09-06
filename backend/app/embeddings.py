"""Text -> vectors, for the Actian VectorAI DB layer.

Embeddings come from Gemini, deliberately: the app already carries a Gemini key
(supplied per-request by the Flutter app, exactly like extraction and answering),
so the semantic layer adds no second credential to set up and no local model to
download.

Two things here are load-bearing rather than cosmetic:

  * task_type. Gemini embeds a stored memory and a user's question into
    *different* spaces unless you say which is which. RETRIEVAL_DOCUMENT at
    index time and RETRIEVAL_QUERY at search time is what makes "how is his
    mother doing?" actually land on "Mom is having surgery on Monday".
  * normalisation. gemini-embedding-001 is a Matryoshka model: at its full 3072
    dimensions the output is unit-length, but any truncated size (we use 768)
    comes back un-normalised - measured ~0.59. Cosine distance divides the norm
    out anyway, so this does not change ranking; it keeps the raw scores
    comparable across rows so score_threshold means one fixed thing.
"""
import logging
import math
from typing import Optional, Sequence

from .config import get_settings

log = logging.getLogger("memorybridge.embeddings")

# Gemini accepts up to 100 texts per embed_content call (verified against the
# live API); chunking here keeps a large backlog from tripping that ceiling.
_MAX_BATCH = 100

DOCUMENT = "RETRIEVAL_DOCUMENT"
QUERY = "RETRIEVAL_QUERY"


class EmbeddingError(RuntimeError):
    pass


def _normalize(values: Sequence[float]) -> list[float]:
    norm = math.sqrt(sum(v * v for v in values))
    if norm == 0.0:
        return list(values)
    return [v / norm for v in values]


def embed_texts(texts: Sequence[str], api_key: Optional[str],
                task_type: str = DOCUMENT) -> list[list[float]]:
    """Embed a batch of texts. Blocking - callers run it in a thread.

    Returns one unit-length vector per input, in the same order.
    """
    from google import genai
    from google.genai import types

    from .llm import LLMError, resolve_api_key

    items = [t if t and t.strip() else " " for t in texts]
    if not items:
        return []

    settings = get_settings()
    try:
        key = resolve_api_key(api_key)
    except LLMError as exc:
        raise EmbeddingError(str(exc)) from exc

    client = genai.Client(api_key=key)
    config = types.EmbedContentConfig(
        task_type=task_type,
        output_dimensionality=settings.embedding_dim,
    )

    out: list[list[float]] = []
    for start in range(0, len(items), _MAX_BATCH):
        chunk = items[start:start + _MAX_BATCH]
        try:
            response = client.models.embed_content(
                model=settings.embedding_model, contents=chunk, config=config,
            )
        except Exception as exc:
            raise EmbeddingError(
                f"Gemini embedding failed ({type(exc).__name__}): {exc}"
            ) from exc
        embeddings = response.embeddings or []
        if len(embeddings) != len(chunk):
            raise EmbeddingError(
                f"Gemini returned {len(embeddings)} embeddings for {len(chunk)} texts."
            )
        for embedding in embeddings:
            values = embedding.values or []
            if len(values) != settings.embedding_dim:
                raise EmbeddingError(
                    f"Expected {settings.embedding_dim}-dim embedding, got {len(values)}. "
                    "EMBEDDING_DIM and the collection's dimension must agree."
                )
            out.append(_normalize(values))
    return out


def embed_query(text: str, api_key: Optional[str]) -> list[float]:
    """Embed one search query. Blocking - callers run it in a thread."""
    vectors = embed_texts([text], api_key, task_type=QUERY)
    if not vectors:
        raise EmbeddingError("Embedding returned nothing for the query.")
    return vectors[0]
