import os
import re
import yaml
from pathlib import Path
from langchain_community.document_loaders import TextLoader
from langchain_text_splitters import RecursiveCharacterTextSplitter, MarkdownHeaderTextSplitter
from langchain_core.documents import Document
from embeddings import ONNXEmbeddings
from store import Store
from wings import classify_document

CONFIG_PATH = os.environ.get('RAG_CONFIG_PATH', os.path.join(os.path.dirname(__file__), 'indexer.yaml'))

def load_config():
    with open(CONFIG_PATH) as f:
        return yaml.safe_load(f)

def get_embeddings(cfg):
    return ONNXEmbeddings(model_name=f"sentence-transformers/{cfg['embedding_model']}")

def get_store(cfg, embeddings) -> Store:
    db_path = os.path.join(os.path.dirname(cfg['chroma_path']), 'rag.db')
    return Store(db_path, embed_fn=embeddings)

def get_md_files(workspace, exclude):
    files = []
    for path in Path(workspace).rglob('*.md'):
        parts = path.relative_to(workspace).parts
        if any(part in exclude for part in parts):
            continue
        files.append(path)
    return files

def chunk_file(path, workspace, cfg):
    """Split a markdown file into chunks, preserving header context.

    Two-pass strategy:
    1. MarkdownHeaderTextSplitter splits on #/##/### boundaries, keeping
       each section together and recording the header hierarchy in metadata.
    2. RecursiveCharacterTextSplitter sub-splits any section that exceeds
       chunk_size, so we never send oversized chunks to the embedding model.
    """
    from lifecycle import confidence_for_folder, compute_decay_factor
    import datetime as _dt

    loader = TextLoader(str(path), encoding='utf-8', autodetect_encoding=True)
    raw = loader.load()
    text = raw[0].page_content

    # Classify document into wing/room once — all chunks inherit these tags.
    # rel_path is the workspace-relative path used by path_pattern rules.
    rel_path_str = str(path.relative_to(workspace)) if isinstance(workspace, Path) else str(path.relative_to(Path(workspace)))
    wing, room = classify_document(rel_path_str, path.name, text)

    # Extract frontmatter fields we index as metadata columns.
    project = None
    superseded_by = None
    last_updated = None
    fm_match = re.match(r'^---\s*\n(.*?)\n---', text, re.DOTALL)
    if fm_match:
        pm = re.search(r'^project:\s*([^\n]+)', fm_match.group(1), re.MULTILINE)
        if pm:
            project = pm.group(1).strip().strip('"').strip("'")
        sm = re.search(r'^superseded_by:\s*([^\n]+)', fm_match.group(1), re.MULTILINE)
        if sm:
            superseded_by = sm.group(1).strip().strip('"').strip("'")
        # Extract date for lifecycle decay (priority: updated > date > date_created)
        for date_field in ('updated', 'date', 'date_created'):
            dm = re.search(rf'^{date_field}:\s*([^\n]+)', fm_match.group(1), re.MULTILINE)
            if dm:
                candidate = dm.group(1).strip().strip('"').strip("'")
                if re.match(r'^\d{4}-\d{2}-\d{2}', candidate):
                    last_updated = candidate[:10]
                    break

    # Fall back to file mtime if no date found in frontmatter
    if not last_updated:
        last_updated = _dt.date.fromtimestamp(path.stat().st_mtime).isoformat()

    rel = path.relative_to(workspace)
    folder = rel.parts[0] if len(rel.parts) > 1 else 'root'
    confidence = confidence_for_folder(folder)
    decay_factor = compute_decay_factor(last_updated)

    # Pass 1: split on markdown headers
    md_splitter = MarkdownHeaderTextSplitter(
        headers_to_split_on=[
            ('#', 'h1'),
            ('##', 'h2'),
            ('###', 'h3'),
            ('####', 'h4'),
        ],
        strip_headers=False,
    )
    header_chunks = md_splitter.split_text(text)

    # Pass 2: sub-split oversized sections
    sub_splitter = RecursiveCharacterTextSplitter(
        chunk_size=cfg['chunk_size'],
        chunk_overlap=cfg['chunk_overlap'],
        separators=["\n\n", "\n", " ", ""],
    )

    chunks = []

    for section in header_chunks:
        # Build header breadcrumb for context (e.g. "## Setup > ### DNS")
        headers = ' > '.join(
            f"{section.metadata[k]}"
            for k in ('h1', 'h2', 'h3', 'h4')
            if k in section.metadata
        )

        base_meta = {
            'source': str(path),
            'folder': folder,
            'filename': path.name,
            'headers': headers,
            'wing': wing,
            'room': room,
            'project': project,
            'superseded_by': superseded_by,
            'last_updated': last_updated,
            'confidence': confidence,
            'decay_factor': decay_factor,
        }

        if len(section.page_content) > cfg['chunk_size']:
            sub_chunks = sub_splitter.split_text(section.page_content)
            for sc in sub_chunks:
                chunks.append(Document(page_content=sc, metadata=dict(base_meta)))
        else:
            chunks.append(Document(page_content=section.page_content, metadata=dict(base_meta)))

    # Fallback: if header splitting produced nothing (e.g. no headers in file),
    # fall back to simple recursive splitting
    if not chunks:
        fallback = RecursiveCharacterTextSplitter(
            chunk_size=cfg['chunk_size'],
            chunk_overlap=cfg['chunk_overlap'],
            separators=["\n\n", "\n", " ", ""],
        )
        chunks = fallback.split_documents(raw)
        for chunk in chunks:
            chunk.metadata.update({
                'source': str(path),
                'folder': folder,
                'filename': path.name,
                'headers': '',
                'wing': wing,
                'room': room,
                'project': project,
                'superseded_by': superseded_by,
                'last_updated': last_updated,
                'confidence': confidence,
                'decay_factor': decay_factor,
            })

    return chunks

def index_file(path, cfg=None, embeddings=None, store=None):
    if cfg is None:
        cfg = load_config()
    if embeddings is None:
        embeddings = get_embeddings(cfg)
    if store is None:
        store = get_store(cfg, embeddings)
    workspace = cfg['workspace']
    try:
        chunks = chunk_file(Path(path), Path(workspace), cfg)
        if chunks:
            store.upsert_file(str(path), chunks)
        print(f'[indexer] {path} -> {len(chunks)} chunks', flush=True)
    except Exception as e:
        print(f'[indexer] error {path}: {e}', flush=True)

def build_index():
    cfg = load_config()
    workspace = cfg['workspace']
    exclude = set(cfg.get('exclude', []))
    print(f'[indexer] scanning {workspace}...', flush=True)
    md_files = get_md_files(workspace, exclude)
    print(f'[indexer] found {len(md_files)} .md files', flush=True)

    embeddings = get_embeddings(cfg)
    store = get_store(cfg, embeddings)

    for i, path in enumerate(md_files):
        try:
            chunks = chunk_file(path, Path(workspace), cfg)
            if chunks:
                store.upsert_file(str(path), chunks)
            if (i + 1) % 50 == 0:
                print(f'[indexer] {i + 1}/{len(md_files)} files indexed...', flush=True)
        except Exception as e:
            print(f'[indexer] skipping {path}: {e}', flush=True)

    store.rebuild_fts()
    total = store.count()
    print(f'[indexer] done. {total} chunks in store.', flush=True)
    return store

if __name__ == '__main__':
    build_index()
