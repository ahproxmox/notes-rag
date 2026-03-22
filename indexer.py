import os
import sys
import yaml
from pathlib import Path
from langchain_community.document_loaders import TextLoader
from langchain_text_splitters import RecursiveCharacterTextSplitter
from langchain_huggingface import HuggingFaceEmbeddings
from langchain_chroma import Chroma

CONFIG_PATH = os.environ.get('RAG_CONFIG_PATH', os.path.join(os.path.dirname(__file__), 'indexer.yaml'))

def load_config():
    with open(CONFIG_PATH) as f:
        return yaml.safe_load(f)

def get_embeddings(cfg):
    return HuggingFaceEmbeddings(model_name=cfg['embedding_model'])

def get_db(cfg, embeddings):
    return Chroma(persist_directory=cfg['chroma_path'], embedding_function=embeddings)

def get_md_files(workspace, exclude):
    files = []
    for path in Path(workspace).rglob('*.md'):
        parts = path.relative_to(workspace).parts
        if any(part in exclude for part in parts):
            continue
        files.append(path)
    return files

def chunk_file(path, workspace, cfg):
    # Prefer splitting at markdown headings to keep sections together
    splitter = RecursiveCharacterTextSplitter(
        chunk_size=cfg['chunk_size'],
        chunk_overlap=cfg['chunk_overlap'],
        separators=["\n## ", "\n### ", "\n#### ", "\n\n", "\n", " ", ""],
    )
    loader = TextLoader(str(path), encoding='utf-8', autodetect_encoding=True)
    raw = loader.load()
    chunks = splitter.split_documents(raw)
    rel = path.relative_to(workspace)
    folder = rel.parts[0] if len(rel.parts) > 1 else 'root'
    for chunk in chunks:
        chunk.metadata['source'] = str(path)
        chunk.metadata['folder'] = folder
        chunk.metadata['filename'] = path.name
    return chunks

def delete_file_chunks(db, path):
    results = db._collection.get(where={'source': str(path)})
    ids = results.get('ids', [])
    if ids:
        db._collection.delete(ids=ids)
    return len(ids)

def index_file(path, cfg=None, embeddings=None, db=None):
    if cfg is None:
        cfg = load_config()
    if embeddings is None:
        embeddings = get_embeddings(cfg)
    if db is None:
        db = get_db(cfg, embeddings)
    workspace = cfg['workspace']
    deleted = delete_file_chunks(db, path)
    try:
        chunks = chunk_file(Path(path), Path(workspace), cfg)
        if chunks:
            db.add_documents(chunks)
        print(f'[indexer] {path} -> {len(chunks)} chunks (replaced {deleted})', flush=True)
    except Exception as e:
        print(f'[indexer] error {path}: {e}', flush=True)

def build_index():
    cfg = load_config()
    workspace = cfg['workspace']
    exclude = set(cfg.get('exclude', []))
    print(f'[indexer] scanning {workspace}...', flush=True)
    md_files = get_md_files(workspace, exclude)
    print(f'[indexer] found {len(md_files)} .md files', flush=True)
    docs = []
    for path in md_files:
        try:
            docs.extend(chunk_file(path, Path(workspace), cfg))
        except Exception as e:
            print(f'[indexer] skipping {path}: {e}', flush=True)
    print(f'[indexer] {len(docs)} chunks, embedding...', flush=True)
    embeddings = get_embeddings(cfg)
    db = Chroma.from_documents(docs, embeddings, persist_directory=cfg['chroma_path'])
    print(f'[indexer] done. {db._collection.count()} chunks in Chroma.', flush=True)
    return db

if __name__ == '__main__':
    build_index()
