"""Thin ONNX-based embedding adapter for ChromaDB + LangChain.

Replaces HuggingFaceEmbeddings (which pulls in PyTorch ~800MB) with
FastEmbed (ONNX Runtime ~50MB). Same model, same vectors, ~700MB less RAM.

Implements LangChain's Embeddings interface so it works as a drop-in
for both langchain_chroma.Chroma and any retriever that calls
embed_query / embed_documents.
"""

from fastembed import TextEmbedding


class ONNXEmbeddings:
    """LangChain-compatible embedding function using FastEmbed (ONNX Runtime)."""

    def __init__(self, model_name: str = 'sentence-transformers/all-MiniLM-L6-v2'):
        self._model = TextEmbedding(model_name=model_name)

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return [vec.tolist() for vec in self._model.embed(texts)]

    def embed_query(self, text: str) -> list[float]:
        return list(self._model.embed([text]))[0].tolist()
