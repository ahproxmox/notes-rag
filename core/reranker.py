"""Cross-encoder reranker using FastEmbed (ONNX Runtime).

Takes candidate documents from hybrid retrieval and re-scores them
using a cross-encoder model that sees (query, document) pairs together.
This is much more accurate than bi-encoder similarity but too slow for
full-corpus search — so we use it as a second pass on top-N candidates.

Typical improvement: +28-40% NDCG@10 over retrieval-only baselines.
"""

from fastembed.rerank.cross_encoder import TextCrossEncoder
from langchain_core.documents import Document

from .embeddings import build_embed_text


class Reranker:
    """Rerank documents using a cross-encoder model via ONNX Runtime."""

    def __init__(self, model_name: str = 'Xenova/ms-marco-MiniLM-L-6-v2', contextual: bool = False):
        self._model = TextCrossEncoder(model_name=model_name)
        self._contextual = contextual

    def _text(self, doc: Document) -> str:
        if not self._contextual:
            return doc.page_content
        m = doc.metadata
        return build_embed_text(doc.page_content, m.get('filename', ''), m.get('headers', ''), m.get('project'))

    def rerank(self, query: str, docs: list[Document], top_k: int = 6) -> list[Document]:
        """Re-score and return top_k documents by cross-encoder relevance."""
        if not docs or len(docs) <= top_k:
            return docs

        texts = [self._text(doc) for doc in docs]
        scores = list(self._model.rerank(query, texts))

        # Sort by score descending, take top_k
        scored = sorted(zip(scores, docs), key=lambda x: x[0], reverse=True)
        return [doc for _, doc in scored[:top_k]]
