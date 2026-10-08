"""Thin ONNX-based embedding adapter for LangChain.

Replaces HuggingFaceEmbeddings (which pulls in PyTorch ~800MB) with
FastEmbed (ONNX Runtime ~50MB). Same model, same vectors, ~700MB less RAM.

Implements LangChain's Embeddings interface (embed_query / embed_documents).
"""

from fastembed import TextEmbedding

# Bump when the text fed to the embedder changes shape (see build_embed_text).
# Recorded in the store's meta table so stale vectors are detectable.
EMBED_PREFIX_VERSION = '1'

# Texts per ONNX pass. Memory grows with batch x seq_len^2: 150 x 512-token chunks
# in one pass (fastembed's default batch of 256) took +2.2 GB and OOM-killed the
# 2026-10-08 re-embed; batches of 16 need ~0.5 GB.
EMBED_BATCH_SIZE = 16


def build_embed_text(content: str, filename: str, headers: str, project: str | None) -> str:
    """Contextual text embedded (and reranked) in place of the bare chunk content.

    A chunk like "## Next Steps - check on 11-02" says nothing about which file or
    project it belongs to; the prefix carries that. `chunks.content` stays
    unchanged so LLM context isn't polluted.
    """
    return f"{filename} | {headers or ''} | project: {project or ''}\n{content}"


class ONNXEmbeddings:
    """LangChain-compatible embedding function using FastEmbed (ONNX Runtime).

    Documents go through passage_embed() and queries through query_embed(); for
    models without asymmetric handling (MiniLM) both are plain embed(). Some
    models (bge) expect an instruction on queries, which fastembed does not add,
    so `query_prefix` is prepended explicitly.
    """

    def __init__(self, model_name: str = 'sentence-transformers/all-MiniLM-L6-v2',
                 query_prefix: str = ''):
        self._model = TextEmbedding(model_name=model_name)
        self._query_prefix = query_prefix

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return [vec.tolist() for vec in self._model.passage_embed(texts, batch_size=EMBED_BATCH_SIZE)]

    def embed_query(self, text: str) -> list[float]:
        return list(self._model.query_embed([self._query_prefix + text]))[0].tolist()
