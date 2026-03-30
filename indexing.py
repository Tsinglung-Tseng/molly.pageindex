"""Shared indexing utilities — result path mapping, indexed-check, subprocess runner."""

import os
import json
import hashlib
import subprocess
from pathlib import Path

from settings import settings

VAULT_PATH  = settings.vault_path
RESULTS_DIR = settings.results_dir
VENV_PYTHON = settings.venv_python
RUN_SCRIPT  = settings.run_script
MODEL       = settings.model


def get_result_path(md_path: Path) -> Path:
    """Return the canonical result JSON path for a given .md file (absolute path in)."""
    rel  = md_path.relative_to(VAULT_PATH)
    safe = str(rel).replace(os.sep, '__').replace(' ', '_')
    return RESULTS_DIR / (os.path.splitext(safe)[0] + '_structure.json')


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


def run_index_file(md_path: Path, model: str = None) -> str:
    """Run run_pageindex.py on a .md file.

    Returns 'ok', 'timeout', 'error: <stderr>', or 'exception: <msg>'.

    Note: run_pageindex.py always writes output to ./results/<basename>_structure.json
    (relative to the project directory).  This function renames the output to the
    canonical unique path returned by get_result_path().
    """
    if model is None:
        model = MODEL
    result_path = get_result_path(md_path)
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    try:
        cmd = [
            str(VENV_PYTHON), str(RUN_SCRIPT),
            '--md_path', str(md_path),
            '--model', model,
            '--output_dir', str(RESULTS_DIR),
            '--if-add-node-summary', 'yes',
        ]
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
        if proc.returncode != 0:
            return f'error: {proc.stderr.strip()}'

        return 'ok'
    except subprocess.TimeoutExpired:
        return 'timeout'
    except Exception as e:
        return f'exception: {e}'
