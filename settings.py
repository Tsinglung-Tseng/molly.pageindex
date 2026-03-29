"""
Unified configuration loader for PageIndex.

All modules should import from here:
    from settings import settings

Config resolution order (higher wins):
    code defaults < config.yaml < environment variables
"""

import os
import sys
import yaml
from pathlib import Path
from dataclasses import dataclass, field
from dotenv import load_dotenv


# ---------------------------------------------------------------------------
# Locate project root & load .env
# ---------------------------------------------------------------------------

PROJECT_DIR = Path(__file__).resolve().parent
load_dotenv(PROJECT_DIR / '.env')


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------

class ConfigError(Exception):
    """Raised when a required config value is missing."""


def _require(value, name: str, source: str = 'config.yaml'):
    """Ensure a required value is present; raise ConfigError if not."""
    if value is None or (isinstance(value, str) and not value.strip()):
        raise ConfigError(
            f"Required config '{name}' is not set.\n"
            f"Please define it in {source} or the corresponding environment variable.\n"
            f"See config.example.yaml for reference."
        )
    return value


# ---------------------------------------------------------------------------
# Settings dataclass
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Settings:
    # -- project paths (derived, not user-configured) --
    project_dir: Path

    # -- vault (REQUIRED, no default) --
    vault_path: Path
    vault_name: str

    # -- model (REQUIRED, no default) --
    model: str

    # -- paths --
    results_dir: Path
    venv_python: Path
    run_script: Path

    # -- concurrency --
    max_workers: int

    # -- vault scanning --
    exclude_symlinks: frozenset

    # -- web ui --
    web_host: str
    web_port: int

    # -- mcp --
    debounce_sec: float

    # -- telegram daily report --
    tg_enabled: bool
    tg_token: str
    tg_chat_id: str

    # -- history --
    history_db: Path


# ---------------------------------------------------------------------------
# Loader
# ---------------------------------------------------------------------------

_CONFIG_YAML = PROJECT_DIR / 'config.yaml'


def _load_yaml() -> dict:
    if not _CONFIG_YAML.exists():
        return {}
    with open(_CONFIG_YAML, encoding='utf-8') as f:
        data = yaml.safe_load(f)
    return data if isinstance(data, dict) else {}


def load_settings() -> Settings:
    """Build a Settings instance from config.yaml + env vars.

    Required fields with no default: vault_path, vault_name, model.
    If any of these are missing, ConfigError is raised immediately.
    """
    cfg = _load_yaml()

    # -- vault (REQUIRED) --
    vault_path_raw = os.getenv('VAULT_PATH') or cfg.get('vault_path')
    _require(vault_path_raw, 'vault_path')
    vault_path = Path(vault_path_raw).expanduser().resolve()

    vault_name = os.getenv('VAULT_NAME') or cfg.get('vault_name')
    _require(vault_name, 'vault_name')

    # -- model (REQUIRED) --
    model = os.getenv('PAGEINDEX_MODEL') or cfg.get('model')
    _require(model, 'model')

    # -- paths (sensible defaults relative to project) --
    results_dir = Path(
        os.getenv('RESULTS_DIR') or cfg.get('results_dir', str(PROJECT_DIR / 'results'))
    )
    if not results_dir.is_absolute():
        results_dir = PROJECT_DIR / results_dir

    venv_python = Path(
        os.getenv('VENV_PYTHON') or cfg.get('venv_python', str(PROJECT_DIR / '.venv/bin/python3'))
    )
    if not venv_python.is_absolute():
        venv_python = PROJECT_DIR / venv_python

    run_script = Path(
        cfg.get('run_script', str(PROJECT_DIR / 'run_pageindex.py'))
    )
    if not run_script.is_absolute():
        run_script = PROJECT_DIR / run_script

    # -- concurrency --
    max_workers = int(os.getenv('MAX_WORKERS') or cfg.get('max_workers', 3))

    # -- vault scanning --
    exclude_raw = cfg.get('exclude_symlinks', [])
    exclude_symlinks = frozenset(exclude_raw) if isinstance(exclude_raw, list) else frozenset()

    # -- web ui --
    web_host = os.getenv('WEB_HOST') or cfg.get('web_host', '127.0.0.1')
    web_port = int(os.getenv('WEB_PORT') or cfg.get('web_port', 7842))

    # -- mcp --
    debounce_sec = float(cfg.get('debounce_sec', 3.0))

    # -- telegram --
    tg_section = cfg.get('telegram', {}) or {}
    tg_enabled = tg_section.get('enabled', False)
    tg_token = os.getenv('TG_TOKEN') or tg_section.get('token', '')
    tg_chat_id = os.getenv('TG_CHAT_ID') or tg_section.get('chat_id', '')

    if tg_enabled:
        _require(tg_token, 'telegram.token', 'config.yaml or TG_TOKEN env var')
        _require(tg_chat_id, 'telegram.chat_id', 'config.yaml or TG_CHAT_ID env var')

    # -- history --
    history_db = Path(cfg.get('history_db', str(PROJECT_DIR / 'history.db')))
    if not history_db.is_absolute():
        history_db = PROJECT_DIR / history_db

    return Settings(
        project_dir=PROJECT_DIR,
        vault_path=vault_path,
        vault_name=vault_name,
        model=model,
        results_dir=results_dir,
        venv_python=venv_python,
        run_script=run_script,
        max_workers=max_workers,
        exclude_symlinks=exclude_symlinks,
        web_host=web_host,
        web_port=web_port,
        debounce_sec=debounce_sec,
        tg_enabled=tg_enabled,
        tg_token=tg_token,
        tg_chat_id=tg_chat_id,
        history_db=history_db,
    )


# ---------------------------------------------------------------------------
# Module-level singleton
# ---------------------------------------------------------------------------

settings = load_settings()
