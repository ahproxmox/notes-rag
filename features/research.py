import os
import json
import re
import datetime
import requests
from pathlib import Path
from bs4 import BeautifulSoup
from langchain_openai import ChatOpenAI
from langchain_core.prompts import PromptTemplate

BRAVE_SEARCH_URL = 'https://api.search.brave.com/res/v1/web/search'
_MODELS_CONFIG  = Path('/mnt/Claude/config/models.json')
_HEALTH_CONFIG  = Path('/mnt/Claude/config/model-health.json')
_DEFAULT_MODEL  = 'stepfun/step-3.5-flash'

SUMMARISE_PROMPT = PromptTemplate(
    input_variables=['query', 'content'],
    template='''Summarise the following web content in response to the query. Be concise and factual. Use markdown.

Query: {query}

Content:
{content}

Summary:'''
)


def _get_model():
    try:
        return json.loads(_MODELS_CONFIG.read_text()).get('notes-rag') or _DEFAULT_MODEL
    except Exception:
        return _DEFAULT_MODEL


def _write_health(status, error=None):
    try:
        _HEALTH_CONFIG.parent.mkdir(parents=True, exist_ok=True)
        health = json.loads(_HEALTH_CONFIG.read_text()) if _HEALTH_CONFIG.exists() else {}
        entry = {'status': status, 'last_checked': datetime.datetime.utcnow().isoformat() + 'Z'}
        if error:
            entry['error'] = str(error)[:200]
        health['notes-rag'] = entry
        _HEALTH_CONFIG.write_text(json.dumps(health, indent=2))
    except Exception:
        pass


def brave_search(query, count=5):
    headers = {
        'Accept': 'application/json',
        'Accept-Encoding': 'gzip',
        'X-Subscription-Token': os.environ['BRAVE_API_KEY'],
    }
    resp = requests.get(BRAVE_SEARCH_URL, headers=headers, params={'q': query, 'count': count}, timeout=10)
    resp.raise_for_status()
    return resp.json().get('web', {}).get('results', [])


def scrape(url, max_chars=3000):
    try:
        resp = requests.get(url, timeout=8, headers={'User-Agent': 'Mozilla/5.0'})
        soup = BeautifulSoup(resp.text, 'html.parser')
        for tag in soup(['script', 'style', 'nav', 'footer', 'header']):
            tag.decompose()
        text = ' '.join(soup.get_text(separator=' ').split())
        return text[:max_chars]
    except Exception as e:
        return f'[scrape failed: {e}]'


def summarise(query, content):
    model = _get_model()
    print(f'[research] using model: {model}', flush=True)
    try:
        llm = ChatOpenAI(
            base_url=os.environ.get('LLM_BASE_URL', 'https://openrouter.ai/api/v1'),
            api_key=os.environ['OPENROUTER_API_KEY'],
            model=model,
        )
        chain = SUMMARISE_PROMPT | llm
        result = chain.invoke({'query': query, 'content': content}).content
        _write_health('ok')
        return result
    except Exception as e:
        _write_health('error', error=e)
        raise RuntimeError(f'Model {model} failed: {e}')


def research(query):
    workspace = os.environ.get('RAG_WORKSPACE', '/mnt/Claude')
    print(f'[research] searching: {query}', flush=True)
    results = brave_search(query)
    if not results:
        return 'No results found.', None

    combined = ''
    for r in results[:3]:
        title   = r.get('title', '')
        url     = r.get('url', '')
        snippet = r.get('description', '')
        body    = scrape(url)
        combined += f'\n\n## {title}\nURL: {url}\n{snippet}\n\n{body}'

    summary = summarise(query, combined[:8000])

    date     = datetime.datetime.now().strftime('%Y-%m-%d-%H%M')
    slug     = re.sub(r'[^a-z0-9]+', '-', query.lower())[:40].strip('-')
    filename = f'{date}-research-{slug}.md'
    filepath = os.path.join(workspace, 'inbox', filename)

    os.makedirs(os.path.dirname(filepath), exist_ok=True)
    with open(filepath, 'w') as f:
        f.write(f'# Research: {query}\n\n')
        f.write(f'*Generated: {datetime.datetime.now().isoformat()}*\n\n')
        f.write(summary)
        f.write('\n\n---\n*Sources:*\n')
        for r in results[:3]:
            f.write(f'- [{r.get("title", "")}]({r.get("url", "")})\n')

    print(f'[research] written to {filepath}', flush=True)
    return summary, filepath


if __name__ == '__main__':
    import sys
    query = ' '.join(sys.argv[1:]) or 'LangChain RAG best practices 2025'
    summary, path = research(query)
    print(f'\nSummary:\n{summary}\n\nSaved to: {path}')
