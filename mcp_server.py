#!/usr/bin/env python3
"""
PageIndex MCP Server

两个功能：
1. 后台 watchdog 监控 vault 目录，新增/修改 .md 文件时自动触发 pageindex 建索引
2. MCP tool search_notes：两阶段搜索（BM25 + LLM tree search）

运行：
    uv run python mcp_server.py

在 Claude Code 的 settings.json 中注册：
    {
      "mcpServers": {
        "pageindex": {
          "command": "/path/to/molly.pageindex/.venv/bin/python",
          "args": ["/path/to/molly.pageindex/mcp_server.py"],
          "env": { "LLM_API_KEY": "...", "LLM_SERVICE_BASE_URL": "..." }
        }
      }
    }
"""

import os
import re
import sys
import select
import time
import logging
import queue
import threading
from pathlib import Path
from threading import Timer


# ---------------------------------------------------------------------------
# Protect stdio transport: redirect print() to stderr so that stray print()
# from imported libraries cannot corrupt the JSON-RPC stream.
# FastMCP uses sys.stdout.buffer directly, so we cannot redirect sys.stdout
# itself — instead we override builtins.print.
# ---------------------------------------------------------------------------
import builtins
_builtin_print = builtins.print

def _safe_print(*args, **kwargs):
    kwargs.setdefault('file', sys.stderr)
    return _builtin_print(*args, **kwargs)

builtins.print = _safe_print


# ---------------------------------------------------------------------------
# Parent-death detection: exit when ANY ancestor dies
# ---------------------------------------------------------------------------


def _get_ppid_of(pid: int) -> int:
    import subprocess
    try:
        return int(subprocess.check_output(
            ['ps', '-o', 'ppid=', '-p', str(pid)],
            text=True, stderr=subprocess.DEVNULL,
        ).strip())
    except Exception:
        return 0


def _collect_ancestors() -> list[int]:
    chain = []
    pid = os.getppid()
    seen = set()
    while pid > 1 and pid not in seen:
        chain.append(pid)
        seen.add(pid)
        pid = _get_ppid_of(pid)
    return chain


def _watch_parent():
    ancestors = _collect_ancestors()
    if not ancestors:
        os._exit(0)
    if sys.platform == 'darwin':
        try:
            kq = select.kqueue()
            events = [
                select.kevent(
                    pid,
                    filter=select.KQ_FILTER_PROC,
                    flags=select.KQ_EV_ADD,
                    fflags=select.KQ_NOTE_EXIT,
                )
                for pid in ancestors
            ]
            kq.control(events, 0)
            kq.control(None, 1)
            os._exit(0)
        except OSError:
            pass
    while True:
        time.sleep(1)
        for pid in ancestors:
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                os._exit(0)
            except PermissionError:
                pass

threading.Thread(target=_watch_parent, daemon=True).start()

# --- 统一配置 ---
PAGEINDEX_DIR = Path(__file__).parent.resolve()
os.chdir(PAGEINDEX_DIR)
sys.path.insert(0, str(PAGEINDEX_DIR))

from settings import settings
from indexing import get_result_path, md5_of_file, run_index_file, RESULTS_DIR, VAULT_PATH
from retrieval import (
    _BM25, _tokenize, _load_docs,
    _result_filename_to_note_name, search_notes_impl, find_notes_impl,
)

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
            status = run_index_file(md_path)
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
# MCP server
# ---------------------------------------------------------------------------

from mcp.server.fastmcp import FastMCP

mcp = FastMCP('pageindex')


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
    status = run_index_file(p)
    return f'Indexing {p.name}: {status}'


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == '__main__':
    _start_watcher()
    log.info('PageIndex MCP server starting (stdio transport)...')
    mcp.run(transport='stdio')
