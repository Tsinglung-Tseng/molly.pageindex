#!/usr/bin/env python3
"""PageIndex Web UI - Search interface for Obsidian vault notes.

Run:
    uv run python web_ui.py [--host 127.0.0.1] [--port 7842]
"""

import os
import re
import sys
import json
import uuid as _uuid
import sqlite3
from pathlib import Path
from urllib.parse import quote

PAGEINDEX_DIR = Path(__file__).parent.resolve()
os.chdir(PAGEINDEX_DIR)
sys.path.insert(0, str(PAGEINDEX_DIR))

from settings import settings

VAULT_PATH  = settings.vault_path
VAULT_NAME  = settings.vault_name
RESULTS_DIR = settings.results_dir
MODEL       = settings.model
HISTORY_DB  = settings.history_db

from retrieval import search_notes_impl, find_notes_impl, _result_filename_to_note_name
from indexing import get_result_path

from contextlib import asynccontextmanager
from fastapi import FastAPI, Query
from fastapi.responses import HTMLResponse, JSONResponse
import uvicorn


@asynccontextmanager
async def _lifespan(app):
    print("MOLLY_READY", flush=True)
    yield


app = FastAPI(title='PageIndex', lifespan=_lifespan)


# ---------------------------------------------------------------------------
# SQLite history
# ---------------------------------------------------------------------------

def _init_history():
    with sqlite3.connect(HISTORY_DB) as conn:
        conn.execute('''
            CREATE TABLE IF NOT EXISTS searches (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                sid TEXT NOT NULL,
                mode TEXT NOT NULL,
                query TEXT NOT NULL,
                result_html TEXT,
                status_text TEXT,
                created_at TEXT DEFAULT (datetime('now','localtime'))
            )
        ''')
        # Migrate existing DB: add columns if missing
        for col_def in ('result_html TEXT', 'status_text TEXT', 'sid TEXT'):
            try:
                conn.execute(f'ALTER TABLE searches ADD COLUMN {col_def}')
            except Exception:
                pass

_init_history()


from pydantic import BaseModel

class SaveHistoryReq(BaseModel):
    mode: str
    query: str
    result_html: str = ''
    status_text: str = ''


@app.post('/api/history')
def save_history(req: SaveHistoryReq):
    sid = str(_uuid.uuid4())
    with sqlite3.connect(HISTORY_DB) as conn:
        conn.execute(
            'INSERT INTO searches (sid, mode, query, result_html, status_text) VALUES (?, ?, ?, ?, ?)',
            (sid, req.mode, req.query.strip(), req.result_html, req.status_text)
        )
    return {'ok': True, 'id': sid}


@app.get('/api/history')
def get_history(mode: str = Query(...), limit: int = 50):
    """Return distinct queries, most recent first, each with its latest UUID."""
    with sqlite3.connect(HISTORY_DB) as conn:
        rows = conn.execute(
            '''SELECT s.query, s.sid, s.created_at
               FROM searches s
               WHERE s.mode=? AND s.id = (
                   SELECT s2.id FROM searches s2
                   WHERE s2.mode=s.mode AND s2.query=s.query
                   ORDER BY s2.created_at DESC LIMIT 1
               )
               ORDER BY s.created_at DESC
               LIMIT ?''',
            (mode, limit)
        ).fetchall()
    return {'items': [{'query': r[0], 'id': r[1], 'last_used': r[2]} for r in rows]}


@app.get('/api/history/result')
def get_history_result(id: str = Query(...)):
    """Return cached result HTML by UUID."""
    with sqlite3.connect(HISTORY_DB) as conn:
        row = conn.execute(
            'SELECT result_html, status_text FROM searches WHERE sid=? LIMIT 1',
            (id,)
        ).fetchone()
    if row and row[0]:
        return {'html': row[0], 'status': row[1] or ''}
    return JSONResponse({'error': 'not found'}, status_code=404)


@app.delete('/api/history')
def clear_history(mode: str = Query(...)):
    with sqlite3.connect(HISTORY_DB) as conn:
        conn.execute('DELETE FROM searches WHERE mode=?', (mode,))
    return {'ok': True}


# ---------------------------------------------------------------------------
# Fast single-LLM search (replaces the top_k+1 calls in search_notes_impl)
# ---------------------------------------------------------------------------

def _fast_search_impl(query: str, top_k: int = 5, model: str = None) -> str:
    """BM25 retrieval → extract node text directly → one LLM call for the answer."""
    if model is None:
        model = MODEL

    if not RESULTS_DIR.exists():
        return 'Results directory does not exist. Please run batch indexing first.'

    from retrieval import _load_docs, _BM25, _tokenize
    from pageindex.utils import ChatGPT_API

    docs = _load_docs(RESULTS_DIR)
    if not docs:
        return 'No indexed documents found.'

    q_tokens = _tokenize(query)
    bm25 = _BM25([d['tokens'] for d in docs])
    scores = bm25.get_scores(q_tokens)
    ranked = sorted(zip(docs, scores), key=lambda x: x[1], reverse=True)
    top_docs = [(d, s) for d, s in ranked if s > 0][:top_k]

    if not top_docs:
        return 'No documents matched the query.'

    def _extract_text(node, parts):
        if isinstance(node, dict):
            for key in ('summary', 'text', 'prefix_summary'):
                v = node.get(key)
                if v:
                    parts.append(str(v))
            for child in node.get('nodes', []):
                _extract_text(child, parts)
            for child in node.get('structure', []):
                _extract_text(child, parts)
        elif isinstance(node, list):
            for item in node:
                _extract_text(item, parts)

    context_parts = []
    for doc, _ in top_docs:
        parts = []
        _extract_text(doc['tree'], parts)
        if parts:
            name = _result_filename_to_note_name(doc['filename'])
            context_parts.append(f'--- {name} ---\n' + '\n'.join(parts))

    if not context_parts:
        return 'Found candidate documents but could not extract content.'

    context = '\n\n'.join(context_parts)
    if len(context) > 14000:
        context = context[:14000] + '\n...(truncated)'

    prompt = (
        f'You are a helpful assistant. Use the following excerpted information from '
        f'my personal notes to answer the question. 请用中文回答。\n'
        f'If the information provided is not sufficient, state that clearly.\n\n'
        f'Question: {query}\n\n'
        f'Reference Material:\n{context}\n\nAnswer:'
    )
    return ChatGPT_API(model=model, prompt=prompt)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_uri(rel: Path, heading: str = None) -> str:
    uri = (
        f'obsidian://open?vault={quote(VAULT_NAME)}'
        f'&file={quote(rel.with_suffix("").as_posix())}'
    )
    if heading:
        uri += f'&heading={quote(heading)}'
    return uri


def _find_md_file(name: str):
    """Locate .md file by stem, tolerating underscore/space differences."""
    matches = list(VAULT_PATH.rglob(f'{name}.md'))
    if matches:
        return matches[0]
    name_spaced = name.replace('_', ' ')
    if name_spaced != name:
        matches = list(VAULT_PATH.rglob(f'{name_spaced}.md'))
        if matches:
            return matches[0]
    return None


def _load_structure(md_path: Path) -> list:
    try:
        result_path = get_result_path(md_path)
        if not result_path.exists():
            return []
        with open(result_path, encoding='utf-8') as f:
            data = json.load(f)
        return data.get('structure', [])
    except Exception:
        return []


def _extract_headings(structure: list) -> list:
    """Flatten structure tree into a list of {title, line_num, depth} dicts."""
    result = []

    def _walk_node(node, depth):
        if not isinstance(node, dict):
            return
        title    = node.get('title', '')
        line_num = node.get('line_num')
        node_id  = node.get('node_id', '')
        if title and line_num is not None and node_id != '0000':
            result.append({'title': title, 'line_num': line_num, 'depth': depth})
        for child in node.get('nodes', []):
            _walk_node(child, depth + 1)

    for root in (structure if isinstance(structure, list) else []):
        _walk_node(root, 0)

    return result


# ---------------------------------------------------------------------------
# Grep logic
# ---------------------------------------------------------------------------

_HEADING_RE = re.compile(r'^(#{1,6})\s+(.+)')


def _grep_impl(pattern: str, case_sensitive: bool = False, max_notes: int = 20, max_lines_per_note: int = 5):
    flags = 0 if case_sensitive else re.IGNORECASE
    try:
        compiled = re.compile(pattern, flags)
    except re.error as e:
        return None, f'Invalid regex: {e}'

    results = []
    for md_file in sorted(VAULT_PATH.rglob('*.md')):
        if len(results) >= max_notes:
            break
        try:
            lines = md_file.read_text(encoding='utf-8', errors='ignore').splitlines()

            # Build sorted heading index: [(lineno, heading_text), ...]
            heading_index = []
            for i, line in enumerate(lines):
                m = _HEADING_RE.match(line.strip())
                if m:
                    heading_index.append((i + 1, m.group(2).strip()))

            def _nearest_heading(lineno: int):
                result = None
                for h_lineno, h_text in heading_index:
                    if h_lineno <= lineno:
                        result = h_text
                    else:
                        break
                return result

            hits = []
            for i, line in enumerate(lines):
                if compiled.search(line):
                    lineno  = i + 1
                    heading = _nearest_heading(lineno)
                    hits.append({'lineno': lineno, 'text': line.strip(), 'heading': heading})

            if not hits:
                continue

            rel  = md_file.relative_to(VAULT_PATH)
            uri  = _make_uri(rel, hits[0].get('heading'))

            match_list = [
                {
                    'lineno':  h['lineno'],
                    'text':    h['text'],
                    'heading': h.get('heading'),
                    'uri':     _make_uri(rel, h.get('heading')),
                }
                for h in hits[:max_lines_per_note]
            ]

            results.append({
                'name':         md_file.stem,
                'path':         str(rel),
                'uri':          uri,
                'matches':      match_list,
                'total_matches': len(hits),
            })
        except Exception:
            continue

    return results, None


# ---------------------------------------------------------------------------
# HTML
# ---------------------------------------------------------------------------

HTML = """<!DOCTYPE html>
<html lang="zh">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>PageIndex</title>
  <script src="https://cdn.tailwindcss.com"></script>
  <script src="https://cdn.jsdelivr.net/npm/marked/marked.min.js"></script>
  <link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/katex@0.16.11/dist/katex.min.css">
  <script defer src="https://cdn.jsdelivr.net/npm/katex@0.16.11/dist/katex.min.js"></script>
  <script defer src="https://cdn.jsdelivr.net/npm/katex@0.16.11/dist/contrib/auto-render.min.js"
    onload="window._katexReady=true"></script>
  <style>
    :root { --accent: #6366f1; }
    body { background: #f5f6fa; color: #1e293b; font-family: system-ui,-apple-system,sans-serif; }

    .card { background:#fff; border:1px solid #e2e8f0; border-radius:12px; transition:border-color .2s,box-shadow .2s; }
    .card:hover { border-color:var(--accent); box-shadow:0 0 0 2px rgba(99,102,241,.1); }

    .prose h1,.prose h2,.prose h3 { font-weight:600; margin:1em 0 .4em; color:#0f172a; }
    .prose h1 { font-size:1.25em; }
    .prose h2 { font-size:1.1em; }
    .prose p { margin:.6em 0; line-height:1.85; color:#334155; }
    .prose code { background:#f1f5f9; padding:2px 7px; border-radius:5px; font-size:.85em; color:#4f46e5; }
    .prose pre { background:#f8fafc; border:1px solid #e2e8f0; padding:1em; border-radius:10px; overflow-x:auto; margin:.8em 0; }
    .prose pre code { background:none; padding:0; color:#334155; }
    .prose ul,.prose ol { padding-left:1.5em; margin:.6em 0; }
    .prose li { margin:.3em 0; color:#334155; }
    .prose blockquote { border-left:3px solid var(--accent); padding-left:1em; color:#64748b; margin:.8em 0; }
    .prose strong { color:#0f172a; }

    .tab { cursor:pointer; padding:.35em 1.1em; border-radius:9999px; font-size:.8rem; font-weight:500; transition:all .15s; }
    .tab.active { background:var(--accent); color:#fff; }
    .tab:not(.active) { color:#94a3b8; }
    .tab:not(.active):hover { color:#475569; }

    .spinner { border:2px solid #e2e8f0; border-top-color:var(--accent); border-radius:50%; width:1.1em; height:1.1em; animation:spin .7s linear infinite; display:inline-block; }
    @keyframes spin { to { transform:rotate(360deg); } }

    input[type=text]:focus { outline:none; border-color:var(--accent) !important; }
    ::-webkit-scrollbar { width:5px; height:5px; }
    ::-webkit-scrollbar-track { background:#f5f6fa; }
    ::-webkit-scrollbar-thumb { background:#cbd5e1; border-radius:3px; }

    .segment-link {
      display:block; font-size:.72rem; color:#6366f1; padding:.2em .5em; border-radius:5px;
      white-space:nowrap; overflow:hidden; text-overflow:ellipsis; text-decoration:none;
      transition:background .1s;
    }
    .segment-link:hover { background:#eef2ff; }

    .match-row {
      display:flex; align-items:flex-start; gap:.5em; padding:.28em .4em; border-radius:5px;
      text-decoration:none; transition:background .1s; color:inherit;
    }
    .match-row:hover { background:#f8fafc; }

    #sidebar { background:#fff; border-right:1px solid #e8ecf0; }
    .hist-btn {
      display:block; width:100%; text-align:left; font-size:.72rem; color:#475569;
      padding:.35em .6em; border-radius:6px; white-space:nowrap; overflow:hidden;
      text-overflow:ellipsis; transition:background .1s,color .1s; border:none;
      background:none; cursor:pointer;
    }
    .hist-btn:hover { background:#eef2ff; color:#4f46e5; }
  </style>
</head>
<body class="h-screen flex overflow-hidden">

  <!-- Sidebar -->
  <aside id="sidebar" class="w-52 flex-shrink-0 flex flex-col h-full overflow-hidden">
    <div class="px-3 py-2.5 border-b border-slate-100 flex items-center justify-between flex-shrink-0">
      <div class="flex items-center gap-1.5">
        <svg class="w-3.5 h-3.5 text-slate-400" fill="none" stroke="currentColor" viewBox="0 0 24 24">
          <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2"
            d="M12 8v4l3 3m6-3a9 9 0 11-18 0 9 9 0 0118 0z"/>
        </svg>
        <span class="text-xs font-semibold text-slate-600">历史</span>
        <span id="history-mode-badge" class="text-xs text-slate-400">· AI</span>
      </div>
      <button onclick="clearHistory()"
        class="text-xs text-slate-300 hover:text-red-400 transition-colors">清空</button>
    </div>
    <div id="history-list" class="flex-1 overflow-y-auto p-1.5">
      <div class="text-xs text-slate-400 text-center py-6">暂无历史</div>
    </div>
  </aside>

  <!-- Main content -->
  <main class="flex-1 overflow-y-auto min-w-0 bg-slate-50">
    <div class="max-w-3xl mx-auto px-5 py-8">

      <!-- Header -->
      <div class="mb-7">
        <h1 class="text-xl font-bold text-slate-800 tracking-tight">
          Page<span style="color:var(--accent)">Index</span>
        </h1>
        <p class="text-xs text-slate-500 mt-0.5">Obsidian vault search</p>
      </div>

      <!-- Search bar -->
      <div class="flex gap-2 mb-4">
        <input id="query" type="text" placeholder="Search notes..."
          class="flex-1 bg-white border border-slate-200 rounded-xl px-4 py-3 text-slate-800 placeholder-slate-400 text-sm transition-colors"
          onkeydown="if(event.key==='Enter' && !event.isComposing) doSearch()">
        <button onclick="doSearch()"
          class="px-5 rounded-xl font-semibold text-sm transition-opacity hover:opacity-90 active:opacity-75"
          style="background:var(--accent);color:#fff">
          Search
        </button>
      </div>

      <!-- Mode + options -->
      <div class="flex items-center gap-4 mb-7 flex-wrap">
        <div class="flex gap-0.5 bg-white rounded-full p-1 border border-slate-200">
          <div class="tab active" id="tab-ai"   onclick="setMode('ai')">AI Answer</div>
          <div class="tab"        id="tab-find" onclick="setMode('find')">Find Notes</div>
          <div class="tab"        id="tab-grep" onclick="setMode('grep')">Grep Files</div>
        </div>
        <label id="opt-topk" class="flex items-center gap-1.5 text-xs text-slate-500 select-none">
          Top&#8209;K
          <select id="top-k" class="border border-slate-200 rounded-lg px-2 py-1 text-xs text-slate-700 bg-white cursor-pointer focus:outline-none focus:border-indigo-400">
            <option value="3">3</option>
            <option value="5">5</option>
            <option value="10">10</option>
            <option value="20" selected>20</option>
            <option value="50">50</option>
          </select>
        </label>
        <label id="opt-case" style="display:none"
          class="flex items-center gap-1.5 text-xs text-slate-500 cursor-pointer select-none">
          <input type="checkbox" id="case-sensitive"> Case sensitive
        </label>
        <div id="status" class="ml-auto text-xs text-slate-500"></div>
      </div>

      <!-- Results -->
      <div id="results"></div>

    </div>
  </main>

  <script>
    // -----------------------------------------------------------------------
    // State
    // -----------------------------------------------------------------------
    let currentMode = 'ai';
    const tabState = {
      ai:   { query: '', html: '', status: '' },
      find: { query: '', html: '', status: '' },
      grep: { query: '', html: '', status: '' },
    };
    const MODE_LABELS = { ai: 'AI', find: 'Find', grep: 'Grep' };

    // Cache: "mode:query" -> {html, status} — restored when clicking history
    const resultsCache = new Map();

    function $(id) { return document.getElementById(id); }

    function esc(s) {
      return String(s)
        .replace(/&/g,'&amp;').replace(/</g,'&lt;')
        .replace(/>/g,'&gt;').replace(/"/g,'&quot;');
    }

    // -----------------------------------------------------------------------
    // Tab switching — preserves each tab's content
    // -----------------------------------------------------------------------
    function setMode(m) {
      if (m === currentMode) return;

      // Save current tab state
      tabState[currentMode].query  = $('query').value;
      tabState[currentMode].html   = $('results').innerHTML;
      tabState[currentMode].status = $('status').textContent;

      currentMode = m;

      // Restore new tab state
      $('query').value        = tabState[m].query;
      $('results').innerHTML  = tabState[m].html;
      $('status').textContent = tabState[m].status;

      // Update tab styles
      ['ai','find','grep'].forEach(t => {
        $('tab-' + t).className = 'tab' + (t === m ? ' active' : '');
      });
      $('opt-topk').style.display = m === 'grep' ? 'none' : '';
      $('opt-case').style.display = m === 'grep' ? ''     : 'none';

      // Sidebar follows tab
      $('history-mode-badge').textContent = '· ' + MODE_LABELS[m];
      loadHistory(m);

      $('query').focus();
    }

    // -----------------------------------------------------------------------
    // History sidebar
    // -----------------------------------------------------------------------
    async function loadHistory(mode) {
      try {
        const res  = await fetch('/api/history?mode=' + mode + '&limit=50');
        const data = await res.json();
        renderHistory(data.items || []);
      } catch(e) {}
    }

    function renderHistory(items) {
      const el = $('history-list');
      if (!items.length) {
        el.innerHTML = '<div class="text-xs text-slate-400 text-center py-6">暂无历史</div>';
        return;
      }
      el.innerHTML = items.map(item =>
        '<button class="hist-btn"' +
        ' data-q="' + esc(item.query) + '"' +
        ' data-sid="' + esc(item.id || '') + '"' +
        ' title="' + esc(item.query) + '">' +
        esc(item.query) + '</button>'
      ).join('');
      el.querySelectorAll('.hist-btn').forEach(btn => {
        btn.addEventListener('click', async () => {
          const q   = btn.dataset.q;
          const sid = btn.dataset.sid;
          // 1. Check memory cache by UUID
          if (sid && resultsCache.has(sid)) {
            restoreResults(q, resultsCache.get(sid).html, resultsCache.get(sid).status);
            return;
          }
          // 2. Fetch from DB by UUID
          if (sid) {
            try {
              const res = await fetch('/api/history/result?id=' + encodeURIComponent(sid));
              if (res.ok) {
                const data = await res.json();
                resultsCache.set(sid, { html: data.html, status: data.status });
                restoreResults(q, data.html, data.status);
                return;
              }
            } catch(e) {}
          }
          // 3. Fallback: re-run search
          $('query').value = q;
          doSearch();
        });
      });
    }

    function restoreResults(q, html, status) {
      $('query').value        = q;
      $('results').innerHTML  = html;
      $('status').textContent = status;
      tabState[currentMode].query  = q;
      tabState[currentMode].html   = html;
      tabState[currentMode].status = status;
    }

    async function clearHistory() {
      await fetch('/api/history?mode=' + currentMode, { method: 'DELETE' });
      loadHistory(currentMode);
    }

    // -----------------------------------------------------------------------
    // Search
    // -----------------------------------------------------------------------
    function setLoading(on) {
      if (on) $('results').innerHTML =
        '<div class="flex items-center gap-2 text-slate-400 text-sm py-10">' +
        '<span class="spinner"></span> Searching...</div>';
    }

    function showError(msg) {
      $('results').innerHTML =
        '<div class="text-red-400 text-sm p-4 rounded-xl border border-red-200 bg-red-50">' +
        esc(msg) + '</div>';
    }

    async function doSearch() {
      const q = $('query').value.trim();
      if (!q) return;
      const mode = currentMode;  // snapshot — currentMode may change while awaiting
      setLoading(true);
      $('status').textContent = '';
      try {
        if      (mode === 'ai')   await doAI(q);
        else if (mode === 'find') await doFind(q);
        else                      await doGrep(q);

        // Capture results written to DOM by do* functions
        const _html   = $('results').innerHTML;
        const _status = $('status').textContent;

        // If the user switched tabs while the search was running,
        // stash results into the originating tab and restore the current tab
        if (mode !== currentMode) {
          tabState[mode].query  = q;
          tabState[mode].html   = _html;
          tabState[mode].status = _status;
          $('results').innerHTML  = tabState[currentMode].html;
          $('status').textContent = tabState[currentMode].status;
          $('query').value        = tabState[currentMode].query;
        }

        resultsCache.set(mode + ':' + q, { html: _html, status: _status });

        fetch('/api/history', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ mode: mode, query: q, result_html: _html, status_text: _status }),
        }).then(r => r.json()).then(data => {
          if (data.id) resultsCache.set(data.id, { html: _html, status: _status });
          loadHistory(mode);
        });
      } catch(e) {
        showError(String(e));
      }
    }

    // -----------------------------------------------------------------------
    // AI Answer
    // -----------------------------------------------------------------------
    async function doAI(q) {
      const topk = $('top-k').value;
      const t0   = Date.now();
      const res  = await fetch('/api/search?q=' + encodeURIComponent(q) + '&top_k=' + topk);
      const data = await res.json();
      const elapsed = ((Date.now() - t0) / 1000).toFixed(1);

      if (data.error) { showError(data.error); return; }

      const sources = data.sources || [];
      const sourceCards = sources.length ? (
        '<div class="mt-5 pt-4 border-t border-slate-100">' +
        '<div class="text-xs font-medium text-slate-400 mb-2">参考笔记</div>' +
        '<div class="flex flex-col gap-1.5">' +
        sources.map((r, i) =>
          '<a href="' + (r.uri || '#') + '" class="card px-3 py-2 flex items-center gap-2.5 no-underline">' +
          '<span class="text-xs font-mono text-slate-300 w-4 text-right flex-shrink-0">' + (i+1) + '</span>' +
          '<div class="min-w-0">' +
            '<div class="text-xs font-medium text-slate-700 truncate">' + esc(r.name) + '</div>' +
            (r.path ? '<div class="text-xs text-slate-400 truncate">' + esc(r.path) + '</div>' : '') +
          '</div></a>'
        ).join('') +
        '</div></div>'
      ) : '';

      const answerEl = document.createElement('div');
      answerEl.className = 'prose bg-white rounded-xl p-6 border border-slate-200 shadow-sm';
      answerEl.innerHTML = marked.parse(data.answer || '') + sourceCards;
      $('results').innerHTML = '';
      $('results').appendChild(answerEl);

      function doRenderMath() {
        if (window._katexReady && window.renderMathInElement) {
          renderMathInElement(answerEl, {
            delimiters: [
              {left:'$$', right:'$$', display:true},
              {left:'$',  right:'$',  display:false},
              {left:'\\(', right:'\\)', display:false},
              {left:'\\[', right:'\\]', display:true},
            ],
            throwOnError: false,
          });
        } else { setTimeout(doRenderMath, 50); }
      }
      doRenderMath();
      $('status').textContent = elapsed + 's';
    }

    // -----------------------------------------------------------------------
    // Find Notes — shows heading segments with deep links
    // -----------------------------------------------------------------------
    async function doFind(q) {
      const topk = $('top-k').value;
      const t0   = Date.now();
      const res  = await fetch('/api/find?q=' + encodeURIComponent(q) + '&top_k=' + topk);
      const data = await res.json();
      const elapsed = ((Date.now() - t0) / 1000).toFixed(1);

      if (data.error) { showError(data.error); return; }
      if (!data.results.length) {
        $('results').innerHTML = '<div class="text-slate-500 text-sm py-10 text-center">No notes matched.</div>';
        $('status').textContent = elapsed + 's';
        return;
      }

      $('status').textContent = data.count + ' note' + (data.count === 1 ? '' : 's') + ' · ' + elapsed + 's';

      const cards = data.results.map((r, i) => {
        const segs = r.segments || [];
        const segHtml = segs.length
          ? '<div class="mt-2 pt-2 border-t border-slate-100 flex flex-col gap-0.5">' +
              segs.map(s =>
                '<a href="' + s.uri + '" class="segment-link">§ ' + esc(s.title) + '</a>'
              ).join('') +
            '</div>'
          : '';

        return (
          '<div class="card p-4">' +
          '<div class="flex items-start gap-3">' +
            '<span class="text-xs font-mono text-slate-400 w-5 text-right flex-shrink-0 mt-0.5">' + (i+1) + '</span>' +
            '<div class="min-w-0 flex-1">' +
              '<a href="' + (r.uri || '#') + '" class="font-semibold text-slate-800 text-sm block truncate hover:text-indigo-600 no-underline">' +
                esc(r.name) +
              '</a>' +
              (r.path ? '<div class="text-xs text-slate-400 mt-0.5 truncate">' + esc(r.path) + '</div>' : '') +
            '</div>' +
          '</div>' +
          segHtml +
          '</div>'
        );
      }).join('');

      $('results').innerHTML = '<div class="flex flex-col gap-3">' + cards + '</div>';
    }

    // -----------------------------------------------------------------------
    // Grep Files — each match links to its nearest heading
    // -----------------------------------------------------------------------
    async function doGrep(q) {
      const cs  = $('case-sensitive').checked;
      const t0  = Date.now();
      const res = await fetch('/api/grep?pattern=' + encodeURIComponent(q) + '&case_sensitive=' + cs);
      const data = await res.json();
      const elapsed = ((Date.now() - t0) / 1000).toFixed(1);

      if (data.error) { showError(data.error); return; }
      if (!data.results.length) {
        $('results').innerHTML = '<div class="text-slate-500 text-sm py-10 text-center">No notes matched.</div>';
        $('status').textContent = elapsed + 's';
        return;
      }

      $('status').textContent = data.count + ' note' + (data.count === 1 ? '' : 's') + ' · ' + elapsed + 's';

      const cards = data.results.map(r => {
        const matchHtml = r.matches.map(m => {
          const headingPart = m.heading
            ? '<span class="text-xs text-indigo-300 flex-shrink-0 max-w-[7rem] truncate whitespace-nowrap">§ ' + esc(m.heading) + '</span>'
            : '';
          return (
            '<a href="' + m.uri + '" class="match-row">' +
              '<span class="font-mono text-xs text-slate-400 flex-shrink-0">L' + m.lineno + '</span>' +
              headingPart +
              '<span class="font-mono text-xs text-slate-600 min-w-0 truncate">' + esc(m.text) + '</span>' +
            '</a>'
          );
        }).join('');

        const more = r.total_matches > r.matches.length
          ? '<div class="text-xs text-slate-400 mt-1 pl-1">+' + (r.total_matches - r.matches.length) + ' more</div>'
          : '';

        return (
          '<div class="card p-4">' +
            '<div class="mb-2">' +
              '<a href="' + r.uri + '" class="font-semibold text-slate-800 text-sm block truncate hover:text-indigo-600 no-underline">' +
                esc(r.name) +
              '</a>' +
              '<div class="text-xs text-slate-400 mt-0.5 truncate">' + esc(r.path) + '</div>' +
            '</div>' +
            '<div class="border-t border-slate-100 pt-2 flex flex-col">' + matchHtml + more + '</div>' +
          '</div>'
        );
      }).join('');

      $('results').innerHTML = '<div class="flex flex-col gap-3">' + cards + '</div>';
    }

    // -----------------------------------------------------------------------
    // Init
    // -----------------------------------------------------------------------
    $('history-mode-badge').textContent = '· ' + MODE_LABELS[currentMode];
    loadHistory(currentMode);
    $('query').focus();
  </script>
</body>
</html>"""


# ---------------------------------------------------------------------------
# API routes
# ---------------------------------------------------------------------------

@app.get('/', response_class=HTMLResponse)
def index():
    return HTML


@app.get('/api/search')
def api_search(q: str = Query(...), top_k: int = 5, model: str = None):
    if model is None:
        model = MODEL
    try:
        answer = _fast_search_impl(q, top_k=top_k, model=model)
        names  = find_notes_impl(q, top_k=top_k)
        sources = []
        for name in names:
            md = _find_md_file(name)
            if md:
                rel = md.relative_to(VAULT_PATH)
                sources.append({'name': name, 'path': str(rel), 'uri': _make_uri(rel)})
            else:
                sources.append({'name': name, 'path': '', 'uri': ''})
        return {'answer': answer, 'sources': sources}
    except Exception as e:
        return JSONResponse({'error': str(e)}, status_code=500)


@app.get('/api/find')
def api_find(q: str = Query(...), top_k: int = 5):
    """BM25 note retrieval with heading segments for deep linking."""
    try:
        names = find_notes_impl(q, top_k=top_k)
        results = []
        for name in names:
            md = _find_md_file(name)
            if md:
                rel       = md.relative_to(VAULT_PATH)
                structure = _load_structure(md)
                headings  = _extract_headings(structure)
                segments  = [
                    {'title': h['title'], 'line_num': h['line_num'], 'uri': _make_uri(rel, h['title'])}
                    for h in headings[:8]
                ]
                results.append({
                    'name':     name,
                    'path':     str(rel),
                    'uri':      _make_uri(rel),
                    'segments': segments,
                })
            else:
                results.append({'name': name, 'path': '', 'uri': '', 'segments': []})
        return {'results': results, 'count': len(results)}
    except Exception as e:
        return JSONResponse({'error': str(e)}, status_code=500)


@app.get('/api/grep')
def api_grep(pattern: str = Query(...), case_sensitive: bool = False):
    results, error = _grep_impl(pattern, case_sensitive)
    if error:
        return JSONResponse({'error': error}, status_code=400)
    return {'results': results, 'count': len(results)}


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == '__main__':
    import argparse
    p = argparse.ArgumentParser(description='PageIndex Web UI')
    p.add_argument('--host', default=settings.web_host)
    p.add_argument('--port', type=int, default=settings.web_port)
    args = p.parse_args()
    print(f'\n  PageIndex Web UI  →  http://{args.host}:{args.port}\n')
    uvicorn.run(app, host=args.host, port=args.port)
