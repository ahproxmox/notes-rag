import os
import re
import threading
import glob as globmod
import yaml
from langchain_huggingface import HuggingFaceEmbeddings
from langchain_chroma import Chroma
from langchain_openai import ChatOpenAI
from langchain_classic.chains import RetrievalQA
from langchain_core.prompts import PromptTemplate
from langchain_core.documents import Document
from langchain_community.retrievers import BM25Retriever
from langchain_classic.retrievers import EnsembleRetriever

CONFIG_PATH = os.environ.get('RAG_CONFIG_PATH', os.path.join(os.path.dirname(__file__), 'indexer.yaml'))

# Set when the watcher indexes or deletes a file — signals api.py to rebuild chain
_chain_dirty = threading.Event()

def invalidate_chain():
    _chain_dirty.set()

PROMPT = PromptTemplate(
    input_variables=['context', 'question'],
    template='''You are a helpful assistant with access to the user's personal workspace notes, todos, memory, and context files.

Use the following retrieved excerpts to answer the question. Cite the source filenames where relevant.
If the answer is not in the excerpts, say so honestly.

Excerpts:
{context}

Question: {question}

Answer:'''
)

def _try_todo_lookup(query):
    """Short-circuit for todo ID lookups — no LLM call needed.
    Matches: 'todo 099', 'todo 99', 'todo #99', '#099', 'todo99'
    Returns (content, [filename]) or None.
    """
    m = re.match(r'^\s*(?:todo\s*#?\s*|#)(\d{1,4})\s*$', query.strip(), re.IGNORECASE)
    if not m:
        return None
    cfg = yaml.safe_load(open(CONFIG_PATH))
    todos_dir = os.path.join(cfg['workspace'], 'todos')
    todo_id = m.group(1).zfill(3)
    pattern = os.path.join(todos_dir, f'{todo_id}-*.md')
    matches = globmod.glob(pattern)
    if not matches:
        return None
    filepath = matches[0]
    filename = os.path.basename(filepath)
    with open(filepath, 'r') as f:
        content = f.read(2000)
    return f'[{filename}]\n{content}', [filename]

def _load_db():
    cfg = yaml.safe_load(open(CONFIG_PATH))
    embeddings = HuggingFaceEmbeddings(model_name=cfg['embedding_model'])
    db = Chroma(persist_directory=cfg['chroma_path'], embedding_function=embeddings)
    return db, cfg

def get_chain():
    db, cfg = _load_db()

    # Load all indexed docs for BM25 (keyword search complement)
    all_data = db._collection.get(include=['documents', 'metadatas'])
    all_docs = [
        Document(page_content=text, metadata=meta)
        for text, meta in zip(all_data['documents'], all_data['metadatas'])
    ]

    bm25_retriever = BM25Retriever.from_documents(all_docs)
    bm25_retriever.k = 8

    semantic_retriever = db.as_retriever(search_kwargs={'k': 8})

    # Ensemble: BM25 handles exact keyword/IP matches, semantic handles meaning
    retriever = EnsembleRetriever(
        retrievers=[bm25_retriever, semantic_retriever],
        weights=[0.4, 0.6],
    )

    llm = ChatOpenAI(
        base_url=os.environ.get('LLM_BASE_URL', 'https://openrouter.ai/api/v1'),
        api_key=os.environ['OPENROUTER_API_KEY'],
        model=os.environ.get('LLM_MODEL', 'google/gemini-2.5-flash-lite'),
    )

    chain = RetrievalQA.from_chain_type(
        llm=llm,
        chain_type='stuff',
        retriever=retriever,
        return_source_documents=True,
        chain_type_kwargs={'prompt': PROMPT},
    )
    return chain

def similar(query, k=5):
    """Return top-k similar documents by vector similarity. No LLM call."""
    db, _ = _load_db()
    results = db.similarity_search_with_score(query, k=k)
    return [
        {
            'content': doc.page_content,
            'source': doc.metadata.get('filename', 'unknown'),
            'score': float(score),
        }
        for doc, score in results
    ]

def search(query, chain=None):
    # Pre-check: direct todo lookup bypasses retriever + LLM entirely
    lookup = _try_todo_lookup(query)
    if lookup:
        return lookup

    if chain is None:
        chain = get_chain()
    result = chain.invoke({'query': query})
    answer = result['result']
    sources = list({doc.metadata.get('filename', 'unknown') for doc in result['source_documents']})
    return answer, sources

def search_filtered(query, exclude_sources, chain_obj):
    """Retrieve docs, filter out excluded source files, then synthesise with LLM."""
    # Pre-check: direct todo lookup bypasses retriever + LLM entirely
    lookup = _try_todo_lookup(query)
    if lookup:
        return lookup

    from langchain_core.messages import HumanMessage
    docs = chain_obj.retriever.invoke(query)
    if exclude_sources:
        docs = [d for d in docs if d.metadata.get('filename', '') not in exclude_sources]
    if not docs:
        return "No relevant context found in the workspace.", []
    context = "\n\n".join(
        f"[{d.metadata.get('filename', 'unknown')}]\n{d.page_content}" for d in docs
    )
    prompt_text = PROMPT.format(context=context, question=query)
    llm = chain_obj.combine_documents_chain.llm_chain.llm
    result = llm.invoke([HumanMessage(content=prompt_text)])
    sources = list({d.metadata.get('filename', 'unknown') for d in docs})
    return result.content, sources

if __name__ == '__main__':
    import sys
    query = ' '.join(sys.argv[1:]) or 'What SSH setup do I have?'
    print(f'Query: {query}\n')
    answer, sources = search(query)
    print(f'Answer:\n{answer}\n')
    print(f'Sources: {sources}')
