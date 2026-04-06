#!/usr/bin/env python3
"""
Wiki layer synthesis — Todo 123, task 1.

For each topic in wiki.yaml:
  1. Retrieve relevant chunks from the RAG store (no LLM, pure retrieval)
  2. Synthesise a structured reference page via LLM
  3. Write to /mnt/Claude/wiki/<slug>.md

The watcher picks up new/updated wiki pages automatically and indexes them.
Run daily via cron (5am), or manually: python wiki.py [--topic <slug>]
"""

import os
import re
import sys
import yaml
from datetime import date
from pathlib import Path
from langchain_openai import ChatOpenAI
from langchain_core.messages import HumanMessage

CONFIG_PATH = os.path.join(os.path.dirname(__file__), 'indexer.yaml')
WIKI_CONFIG_PATH = os.path.join(os.path.dirname(__file__), 'wiki.yaml')

WIKI_DIR = Path('/mnt/Claude/wiki')
TODAY = date.today().isoformat()

WIKI_PROMPT = """\
You are a knowledge base curator. Your job is to synthesise a clear, structured \
reference wiki page on the topic: "{title}".

You have been given excerpts from personal workspace notes, todos, session logs, \
and context files. Use them to write a comprehensive reference page that captures \
the current state of knowledge on this topic.

Guidelines:
- Write in a factual, reference style — not Q&A
- Use markdown headers (##, ###) to organise sections
- Include concrete details: IPs, file paths, commands, config values where present
- Bullet points for lists of items; prose for explanations
- If excerpts contradict each other, note the discrepancy
- Do NOT invent information not present in the excerpts
- Omit any passwords, API keys, or secrets — even if they appear in the excerpts
- Do NOT include a top-level # heading — that is added automatically

Excerpts:
{context}

Write the wiki page body now (no frontmatter, no top-level heading):"""


def _load_wiki_config():
    with open(WIKI_CONFIG_PATH) as f:
        return yaml.safe_load(f)


def _get_llm():
    return ChatOpenAI(
        base_url=os.environ.get('LLM_BASE_URL', 'https://openrouter.ai/api/v1'),
        api_key=os.environ['OPENROUTER_API_KEY'],
        model=os.environ.get('LLM_MODEL', 'google/gemini-2.5-flash-lite'),
    )


def _retrieve_for_topic(topic: dict, k_per_query: int = 15) -> tuple[list, list[str]]:
    """Retrieve chunks for all seed queries, deduplicate by content, return top chunks + sources."""
    from search import _retrieve

    seen_keys = set()
    all_docs = []

    for query in topic['queries']:
        docs = _retrieve(query, k=k_per_query)
        for doc in docs:
            key = f"{doc.metadata.get('source', '')}:{doc.page_content[:80]}"
            if key not in seen_keys:
                seen_keys.add(key)
                all_docs.append(doc)

    # Sort by RRF score descending, cap at 20 unique chunks
    all_docs.sort(key=lambda d: d.metadata.get('rrf_score', 0.0), reverse=True)
    top_docs = all_docs[:20]

    sources = list(dict.fromkeys(
        d.metadata.get('filename', 'unknown') for d in top_docs
    ))
    return top_docs, sources


def _synthesise_page(topic: dict, docs: list) -> str:
    """Call LLM to synthesise wiki page body from retrieved chunks."""
    context = "\n\n".join(
        f"[{d.metadata.get('filename', 'unknown')}]\n{d.page_content}"
        for d in docs
    )
    prompt = WIKI_PROMPT.format(title=topic['title'], context=context)
    llm = _get_llm()
    result = llm.invoke([HumanMessage(content=prompt)])
    body = result.content.strip()

    # Strip leading h1 if LLM included one despite instructions
    body = re.sub(r'^#\s+.+\n+', '', body).strip()

    return body


def _write_wiki_page(topic: dict, body: str, sources: list[str]) -> Path:
    """Write the wiki page with frontmatter to /mnt/Claude/wiki/<slug>.md"""
    WIKI_DIR.mkdir(parents=True, exist_ok=True)
    path = WIKI_DIR / f"{topic['slug']}.md"

    sources_yaml = '\n'.join(f'  - {s}' for s in sources)
    content = f"""---
title: "{topic['title']}"
type: wiki
topic: {topic['slug']}
generated: {TODAY}
sources:
{sources_yaml}
---

# {topic['title']}

{body}
"""
    path.write_text(content, encoding='utf-8')
    return path


def build_topic(topic: dict) -> Path:
    slug = topic['slug']
    print(f"[wiki] Building: {slug}...", flush=True)

    docs, sources = _retrieve_for_topic(topic)
    if not docs:
        print(f"[wiki] No chunks found for {slug} — skipping", flush=True)
        return None

    print(f"[wiki]   {len(docs)} chunks from {len(sources)} sources", flush=True)
    body = _synthesise_page(topic, docs)
    path = _write_wiki_page(topic, body, sources)
    print(f"[wiki]   Written: {path}", flush=True)
    return path


def main():
    # Load env from .env file if present (for standalone runs outside systemd)
    env_path = os.path.join(os.path.dirname(__file__), '.env')
    if os.path.exists(env_path):
        for line in open(env_path):
            line = line.strip()
            if line and not line.startswith('#') and '=' in line:
                k, _, v = line.partition('=')
                os.environ.setdefault(k.strip(), v.strip())

    wiki_cfg = _load_wiki_config()
    topics = wiki_cfg.get('topics', [])

    # Filter to specific topic if passed as arg
    if '--topic' in sys.argv:
        idx = sys.argv.index('--topic')
        if idx + 1 < len(sys.argv):
            target = sys.argv[idx + 1]
            topics = [t for t in topics if t['slug'] == target]
            if not topics:
                print(f"[wiki] Unknown topic: {target}", flush=True)
                sys.exit(1)

    print(f"[wiki] Building {len(topics)} topic(s)...", flush=True)
    built = 0
    for topic in topics:
        try:
            path = build_topic(topic)
            if path:
                built += 1
        except Exception as e:
            print(f"[wiki] ERROR on {topic['slug']}: {e}", flush=True)

    print(f"[wiki] Done. {built}/{len(topics)} pages built.", flush=True)


if __name__ == '__main__':
    main()
