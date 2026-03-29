#!/usr/bin/env python3
"""
PageIndex MCP Server

两个功能：
1. 后台 watchdog 监控 vault 目录，新增/修改 .md 文件时自动触发 pageindex 建索引
2. MCP tool search_notes：两阶段搜索（BM25 + LLM tree search）

运行：
    cd .pageindex
    uv run python mcp_server.py

在 Claude Code 的 settings.json 中注册：
    {
      "mcpServers": {
        "pageindex": {
          "command": "/path/to/.pageindex/.venv/bin/python",
          "args": ["/path/to/.pageindex/mcp_server.py"],
          "env": { "CHATGPT_API_KEY": "...", "OPENAI_BASE_URL": "..." }
        }
      }
    }
"""

import os
import sys
import json
import hashlib
import logging
import queue
import threading
import subprocess
import time
from pathlib import Path
from threading import Timer

# --- 统一配置 ---
PAGEINDEX_DIR = Path(__file__).parent.resolve()
os.chdir(PAGEINDEX_DIR)
sys.path.insert(0, str(PAGEINDEX_DIR))

from settings import settings

VAULT_PATH   = settings.vault_path
RESULTS_DIR  = settings.results_dir
VENV_PYTHON  = settings.venv_python
RUN_SCRIPT   = settings.run_script
LOG_PATH     = PAGEINDEX_DIR / 'mcp_server.log'
MODEL        = settings.model
DEBOUNCE_SEC = settings.debounce_sec

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    handlers=[
        logging.FileHandler(LOG_PATH, encoding='utf-8'),
        logging.StreamHandler(sys.stderr),
    ]
)
log = logging.getLogger('pageindex-mcp')


# ---------------------------------------------------------------------------
# Indexing helpers (mirrors batch_index.py logic)
# ---------------------------------------------------------------------------

def get_result_filename(md_path: Path) -> str:
    rel_path = md_path.relative_to(VAULT_PATH)
    safe_name = str(rel_path).replace(os.sep, '__').replace(' ', '_')
    return os.path.splitext(safe_name)[0] + '_structure.json'


def is_already_indexed(result_path: Path) -> bool:
    if not result_path.exists():
        return False
    try:
        with open(result_path, encoding='utf-8') as f:
            data = json.load(f)
        return 'structure' in data
    except Exception:
        return False


def md5_of_file(path: Path) -> str:
    h = hashlib.md5()
    with open(path, 'rb') as f:
        h.update(f.read())
    return h.hexdigest()


def index_file(md_path: Path) -> str:
    """Run run_pageindex.py on a single .md file, return status string."""
    result_filename = get_result_filename(md_path)
    result_path = RESULTS_DIR / result_filename
    RESULTS_DIR.mkdir(exist_ok=True)

    try:
        cmd = [
            str(VENV_PYTHON), str(RUN_SCRIPT),
            '--md_path', str(md_path),
            '--model', MODEL,
            '--if-add-node-summary', 'yes',
        ]
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
        if proc.returncode != 0:
            return f'error: {proc.stderr[:200]}'

        # run_pageindex saves to results/<basename>_structure.json; rename to our unique path
        default_name = md_path.stem + '_structure.json'
        default_path = RESULTS_DIR / default_name
        if default_path.exists() and default_path != result_path:
            if result_path.exists():
                result_path.unlink()
            default_path.rename(result_path)

        return 'ok'
    except subprocess.TimeoutExpired:
        return 'timeout'
    except Exception as e:
        return f'exception: {e}'


# ---------------------------------------------------------------------------
# Background watcher pipeline
# ---------------------------------------------------------------------------

class IndexPipeline:
    """Last-write-wins worker: 防抖 + 版本号，避免重复处理。"""

    def __init__(self):
        self._queue: queue.Queue = queue.Queue()
        self._versions: dict[str, int] = {}
        self._ver_lock = threading.Lock()
        self._hashes: dict[str, str] = {}
        t = threading.Thread(target=self._worker_loop, name='index-worker', daemon=True)
        t.start()

    def submit(self, md_path: Path):
        key = str(md_path)
        with self._ver_lock:
            v = self._versions.get(key, 0) + 1
            self._versions[key] = v
        self._queue.put((md_path, v))

    def _worker_loop(self):
        while True:
            item = self._queue.get()
            if item is None:
                break
            md_path, version = item
            self._process(md_path, version)
            self._queue.task_done()

    def _process(self, md_path: Path, version: int):
        key = str(md_path)
        with self._ver_lock:
            latest = self._versions.get(key, 0)
        if version < latest:
            log.debug(f'superseded v{version}<v{latest}, skip {md_path.name}')
            return

        try:
            current_hash = md5_of_file(md_path)
            if self._hashes.get(key) == current_hash:
                log.debug(f'unchanged, skip {md_path.name}')
                return

            log.info(f'indexing: {md_path.name}')
            status = index_file(md_path)
            if status == 'ok':
                self._hashes[key] = md5_of_file(md_path)
                log.info(f'indexed ok: {md_path.name}')
            else:
                log.warning(f'index failed [{md_path.name}]: {status}')
        except Exception as e:
            log.error(f'error [{md_path.name}]: {e}', exc_info=True)


pipeline = IndexPipeline()


class _DebounceHandler:
    def __init__(self):
        self._timers: dict[str, Timer] = {}

    def on_change(self, path: str):
        p = Path(path)
        if p.suffix not in ('.md', '.markdown'):
            return
        # only index files inside vault
        try:
            p.relative_to(VAULT_PATH)
        except ValueError:
            return
        log.info(f'[change] {p.name}')
        self._debounce(path)

    def _debounce(self, path: str):
        if path in self._timers:
            self._timers[path].cancel()
        t = Timer(DEBOUNCE_SEC, self._run, args=[path])
        t.daemon = True
        t.start()
        self._timers[path] = t

    def _run(self, path: str):
        self._timers.pop(path, None)
        pipeline.submit(Path(path))


def _start_watcher():
    try:
        from watchdog.observers import Observer
        from watchdog.events import FileSystemEventHandler, FileSystemEvent
    except ImportError:
        log.warning('watchdog not installed, file watching disabled')
        return

    handler_logic = _DebounceHandler()

    class Bridge(FileSystemEventHandler):
        def on_created(self, event: FileSystemEvent):
            if not event.is_directory:
                handler_logic.on_change(event.src_path)

        def on_modified(self, event: FileSystemEvent):
            if not event.is_directory:
                handler_logic.on_change(event.src_path)

    observer = Observer()
    observer.schedule(Bridge(), str(VAULT_PATH), recursive=True)
    observer.daemon = True
    observer.start()
    log.info(f'watching vault: {VAULT_PATH}')


# ---------------------------------------------------------------------------
# Search logic (from search_local.py)
# ---------------------------------------------------------------------------

import math
import re
from collections import Counter


class _BM25:
    def __init__(self, corpus):
        self.corpus_size = len(corpus)
        self.avgdl = sum(len(d) for d in corpus) / self.corpus_size if self.corpus_size else 1
        self.doc_freqs = []
        self.idf = {}
        self.doc_len = []
        for doc in corpus:
            self.doc_len.append(len(doc))
            freq = Counter(doc)
            self.doc_freqs.append(freq)
            for w in freq:
                self.idf[w] = self.idf.get(w, 0) + 1
        for w, f in self.idf.items():
            self.idf[w] = math.log(1 + (self.corpus_size - f + 0.5) / (f + 0.5))

    def get_scores(self, query):
        scores = [0.0] * self.corpus_size
        for q in query:
            q_idf = self.idf.get(q, 0)
            if not q_idf:
                continue
            for i, freq in enumerate(self.doc_freqs):
                f = freq.get(q, 0)
                if f:
                    scores[i] += q_idf * (f * 2.5) / (
                        f + 1.5 * (1 - 0.75 + 0.75 * self.doc_len[i] / self.avgdl)
                    )
        return scores


def _tokenize(text: str) -> list:
    import string
    text = text.lower()
    for p in string.punctuation + '，。！？；：、\'\'""（）【】《》\n\r\t':
        text = text.replace(p, ' ')
    tokens = []
    for token in text.split():
        if re.search(r'[\u4e00-\u9fff]', token):
            tokens.extend(list(token))
        else:
            tokens.append(token)
    return tokens


def _load_docs(results_dir: Path) -> list:
    docs = []
    for fpath in results_dir.glob('*.json'):
        try:
            with open(fpath, encoding='utf-8') as f:
                tree = json.load(f)
            texts = []

            def _extract(node):
                if isinstance(node, dict):
                    for key in ('title', 'summary', 'prefix_summary', 'text'):
                        if node.get(key):
                            texts.append(str(node[key]))
                    if 'nodes' in node:
                        _extract(node['nodes'])
                    if 'structure' in node:
                        _extract(node['structure'])
                elif isinstance(node, list):
                    for item in node:
                        _extract(item)

            _extract(tree)
            tokens = _tokenize(' '.join(texts))
            if tokens:
                docs.append({'filename': fpath.name, 'filepath': str(fpath), 'tree': tree, 'tokens': tokens})
        except Exception as e:
            log.debug(f'skip {fpath.name}: {e}')
    return docs


def _tree_summary(tree) -> str:
    def _s(node):
        if isinstance(node, dict):
            r = {}
            if 'node_id' in node: r['id'] = node['node_id']
            if 'title'   in node: r['title'] = node['title']
            if 'summary' in node: r['summary'] = node['summary']
            if node.get('nodes'): r['nodes'] = _s(node['nodes'])
            if node.get('structure'): r['structure'] = _s(node['structure'])
            return r
        elif isinstance(node, list):
            return [_s(i) for i in node]
        return node
    return json.dumps(_s(tree), ensure_ascii=False)


def search_notes_impl(query: str, top_k: int = 5, model: str = None, lang: str = 'en') -> str:
    if model is None:
        model = MODEL

    if not RESULTS_DIR.exists():
        return 'Results directory does not exist. Please run batch indexing first.'

    docs = _load_docs(RESULTS_DIR)
    if not docs:
        return 'No indexed documents found.'

    # Stage 1: BM25
    q_tokens = _tokenize(query)
    bm25 = _BM25([d['tokens'] for d in docs])
    scores = bm25.get_scores(q_tokens)
    ranked = sorted(zip(docs, scores), key=lambda x: x[1], reverse=True)
    top_docs = [(d, s) for d, s in ranked if s > 0][:top_k]

    if not top_docs:
        return 'No documents matched the query.'

    # Stage 2: LLM tree search
    from pageindex.utils import ChatGPT_API, extract_json

    def get_node_map(tree):
        mapping = {}
        def _m(node):
            if isinstance(node, dict):
                if 'node_id' in node:
                    mapping[node['node_id']] = node
                if 'nodes' in node: _m(node['nodes'])
                if 'structure' in node: _m(node['structure'])
            elif isinstance(node, list):
                for item in node: _m(item)
        _m(tree)
        return mapping

    def _search_doc(doc_score):
        doc, score = doc_score
        summary = _tree_summary(doc['tree'])
        prompt = (
            f'You are a smart search assistant. Given the following document tree structure '
            f'(with node IDs, titles, and summaries), determine which nodes are most relevant '
            f'to the user\'s query.\n\n'
            f'User Query: "{query}"\n\n'
            f'Document Tree:\n{summary}\n\n'
            f'Respond with a JSON array containing the node IDs that contain information '
            f'relevant to answering the query. For example: ["0001", "0005"]. '
            f'If none are relevant, return []. ONLY output valid JSON.'
        )
        resp = ChatGPT_API(model=model, prompt=prompt)
        try:
            node_ids = extract_json(resp)
            if not isinstance(node_ids, list):
                node_ids = []
        except Exception:
            node_ids = []

        if not node_ids:
            return None
        node_map = get_node_map(doc['tree'])
        ctx = f'--- From: {doc["filename"]} ---\n'
        for nid in node_ids:
            if nid in node_map:
                n = node_map[nid]
                title = n.get('title', 'Untitled')
                content = n.get('text') or n.get('summary', '')
                ctx += f'## {title}\n{content}\n\n'
        return ctx

    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=min(len(top_docs), 5)) as ex:
        results = list(ex.map(_search_doc, top_docs))
    contexts = [r for r in results if r]

    if not contexts:
        return 'Found candidate documents but no relevant nodes matched the query.'

    context = '\n'.join(contexts)
    lang_instruction = '请用中文回答。' if lang == 'zh' else 'Please answer in English.'
    final_prompt = (
        f'You are a helpful assistant. Use the following excerpted information from '
        f'my personal notes to answer the question. {lang_instruction}\n'
        f'If the information provided is not sufficient, state that clearly.\n\n'
        f'Question: {query}\n\n'
        f'Reference Material:\n{context}\n\nAnswer:'
    )
    return ChatGPT_API(model=model, prompt=final_prompt)


# ---------------------------------------------------------------------------
# MCP server
# ---------------------------------------------------------------------------

from mcp.server.fastmcp import FastMCP

mcp = FastMCP('pageindex')


def _result_filename_to_note_name(filename: str) -> str:
    """Convert result filename back to note stem.

    e.g. 'folder__sub__My_Note_structure.json' -> 'My_Note'
    """
    stem = filename.removesuffix('_structure.json')
    return stem.split('__')[-1]


def find_notes_impl(query: str, top_k: int = 5) -> list[str]:
    """BM25-only retrieval; returns a list of note name stems."""
    if not RESULTS_DIR.exists():
        return []

    docs = _load_docs(RESULTS_DIR)
    if not docs:
        return []

    q_tokens = _tokenize(query)
    bm25 = _BM25([d['tokens'] for d in docs])
    scores = bm25.get_scores(q_tokens)
    ranked = sorted(zip(docs, scores), key=lambda x: x[1], reverse=True)
    top_docs = [(d, s) for d, s in ranked if s > 0][:top_k]

    return [_result_filename_to_note_name(d['filename']) for d, _ in top_docs]


@mcp.tool()
def find_notes(query: str, top_k: int = 5) -> str:
    """Find Obsidian notes relevant to a query using BM25 retrieval (no LLM).

    Args:
        query: Search query (Chinese or English).
        top_k: Maximum number of note names to return (default 5).

    Returns:
        Newline-separated list of note name stems ranked by relevance.
    """
    log.info(f'find_notes called: query={query!r}, top_k={top_k}')
    try:
        names = find_notes_impl(query, top_k=top_k)
        if not names:
            return 'No notes matched.'
        return '\n'.join(names)
    except Exception as e:
        log.error(f'find_notes error: {e}', exc_info=True)
        return f'Error: {e}'


@mcp.tool()
def search_notes(query: str, top_k: int = 5, model: str = None) -> str:
    """Search personal Obsidian notes using two-stage retrieval (BM25 + LLM tree search).

    Args:
        query: The search query in natural language (supports Chinese and English).
        top_k: Number of candidate documents to retrieve in Stage 1 (default 5).
        model: LLM model name for Stage 2 tree search (defaults to config model).

    Returns:
        A synthesized answer based on relevant excerpts from indexed notes.
    """
    log.info(f'search_notes called: query={query!r}, top_k={top_k}, model={model}')
    try:
        return search_notes_impl(query, top_k=top_k, model=model)
    except Exception as e:
        log.error(f'search_notes error: {e}', exc_info=True)
        return f'Error during search: {e}'


@mcp.tool()
def grep_notes(
    pattern: str,
    case_sensitive: bool = False,
    max_notes: int = 20,
    max_lines_per_note: int = 5,
) -> str:
    """Search Obsidian note content for a pattern and return matching lines.

    Args:
        pattern: Text or regex pattern to search for.
        case_sensitive: Whether the match is case-sensitive (default False).
        max_notes: Maximum number of notes to include in results (default 20).
        max_lines_per_note: Maximum matching lines to show per note (default 5).

    Returns:
        Matched lines grouped by note name, format:
            笔记名
              L42: matched line content
              L87: another matched line
    """
    flags = 0 if case_sensitive else re.IGNORECASE
    try:
        compiled = re.compile(pattern, flags)
    except re.error as e:
        return f'Invalid regex pattern: {e}'

    output = []
    note_count = 0
    for md_file in sorted(VAULT_PATH.rglob('*.md')):
        if note_count >= max_notes:
            break
        try:
            lines = md_file.read_text(encoding='utf-8', errors='ignore').splitlines()
            hits = [
                (i + 1, line)
                for i, line in enumerate(lines)
                if compiled.search(line)
            ]
            if not hits:
                continue
            note_count += 1
            block = [md_file.stem]
            for lineno, text in hits[:max_lines_per_note]:
                block.append(f'  L{lineno}: {text.strip()}')
            if len(hits) > max_lines_per_note:
                block.append(f'  ... ({len(hits) - max_lines_per_note} more lines)')
            output.append('\n'.join(block))
        except Exception:
            continue

    if not output:
        return 'No notes matched.'
    return '\n\n'.join(output)


@mcp.tool()
def index_note(md_path: str) -> str:
    """Manually trigger pageindex indexing for a specific Markdown file.

    Args:
        md_path: Absolute path to the .md file to index.

    Returns:
        Status message ('ok', 'error: ...', etc.)
    """
    p = Path(md_path)
    if not p.exists():
        return f'File not found: {md_path}'
    if p.suffix not in ('.md', '.markdown'):
        return 'Only .md / .markdown files are supported.'
    log.info(f'manual index_note: {p.name}')
    status = index_file(p)
    return f'Indexing {p.name}: {status}'


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == '__main__':
    _start_watcher()
    log.info('PageIndex MCP server starting (stdio transport)...')
    mcp.run(transport='stdio')
