#!/usr/bin/env python3
"""
增量 Vault 索引 + Telegram 报告

每日定时任务：检测 Vault 变更，仅对新增/修改文件重新索引，发送 TG 日报。
首次运行：仅建状态快照（后续追踪变更）。
"""

import os
import sys
import json
import logging
from pathlib import Path
from datetime import datetime

# --- 统一配置 ---
PAGEINDEX_DIR = Path(__file__).parent.resolve()
os.chdir(PAGEINDEX_DIR)
sys.path.insert(0, str(PAGEINDEX_DIR))

from settings import settings
from indexing import get_result_path, run_index_file, VAULT_PATH, RESULTS_DIR

STATE_FILE       = PAGEINDEX_DIR / f'.vault_state_{settings.vault_name}.json'
EXCLUDE_SYMLINKS = settings.exclude_symlinks
MAX_WORKERS      = settings.max_workers

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    stream=sys.stdout,
)
log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Telegram
# ---------------------------------------------------------------------------

def tg_send(text: str):
    if not settings.tg_enabled:
        log.info('[TG] disabled, skipping send')
        return
    import urllib.request
    url = f'https://api.telegram.org/bot{settings.tg_token}/sendMessage'
    data = json.dumps({'chat_id': settings.tg_chat_id, 'text': text, 'parse_mode': 'HTML'}).encode()
    req = urllib.request.Request(url, data=data, headers={'Content-Type': 'application/json'})
    try:
        urllib.request.urlopen(req, timeout=10)
        log.info('[TG] message sent')
    except Exception as e:
        log.error(f'[TG ERROR] {e}')


# ---------------------------------------------------------------------------
# State management
# ---------------------------------------------------------------------------

def load_state() -> dict:
    """返回 {rel_path: mtime} 映射"""
    if STATE_FILE.exists():
        try:
            with open(STATE_FILE, encoding='utf-8') as f:
                return json.load(f)
        except Exception:
            pass
    return {}


def save_state(state: dict):
    with open(STATE_FILE, 'w', encoding='utf-8') as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


def scan_vault() -> dict:
    """扫描 vault（含软连接，排除 EXCLUDE_SYMLINKS），返回 {rel_path: mtime}"""
    current = {}
    for root, dirs, files in os.walk(VAULT_PATH, followlinks=True):
        root_path = Path(root)
        dirs[:] = [
            d for d in dirs
            if not d.startswith('.') and d not in EXCLUDE_SYMLINKS
        ]
        for fname in files:
            if not fname.endswith('.md'):
                continue
            full = root_path / fname
            rel = str(full.relative_to(VAULT_PATH))
            current[rel] = full.stat().st_mtime
    return current


# ---------------------------------------------------------------------------
# Indexing
# ---------------------------------------------------------------------------

def index_file(md_rel: str) -> tuple[str, str]:
    log.info(f'[index] {md_rel}')
    status = run_index_file(VAULT_PATH / md_rel)
    if status == 'ok':
        log.info(f'[index] OK: {md_rel}')
        return ('ok', md_rel)
    log.warning(f'[index] FAIL: {md_rel} → {status}')
    return ('err', f'{md_rel}: {status}')


def remove_index(md_rel: str):
    p = get_result_path(VAULT_PATH / md_rel)
    if p.exists():
        p.unlink()
        log.info(f'[index] removed index for deleted file: {md_rel}')


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    log.info('=== batch_index start ===')
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)

    prev  = load_state()
    curr  = scan_vault()

    log.info(f'vault scan: {len(curr)} files found')

    # 首次运行：只对根目录下的 md 文件建立初始索引
    if not prev:
        root_files = sorted(rel for rel in curr if os.sep not in rel and '/' not in rel)
        log.info(f'[init] 首次运行，对根目录 {len(root_files)}/{len(curr)} 个文件建立索引…')
        ok_list, err_list = [], []
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
            for status, msg in ex.map(index_file, root_files):
                (ok_list if status == 'ok' else err_list).append(msg)
        log.info(f'[init] 索引完成：{len(ok_list)} ok, {len(err_list)} failed')
        # 索引失败的文件不写入状态，下次自动重试
        for failed_rel in err_list:
            rel = failed_rel.split(':')[0].strip()
            curr.pop(rel, None)
        save_state(curr)
        now = datetime.now().strftime('%Y-%m-%d %H:%M')
        msg = (
            f'📚 <b>Vault 初始化完成</b> {now}\n'
            f'共索引 {len(curr)} 个文件\n'
            f'✅ {len(ok_list)} 成功 / ❌ {len(err_list)} 失败'
        )
        tg_send(msg)
        return

    prev_set = set(prev)
    curr_set = set(curr)

    added    = sorted(curr_set - prev_set)
    deleted  = sorted(prev_set - curr_set)
    modified = sorted(
        f for f in curr_set & prev_set
        if curr[f] != prev[f]
    )

    log.info(f'changes: +{len(added)} added, ~{len(modified)} modified, -{len(deleted)} deleted')

    to_index = added + modified

    # --- 重新索引 ---
    ok_list, err_list = [], []
    if to_index:
        log.info(f'indexing {len(to_index)} files with {MAX_WORKERS} workers')
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
            for status, msg in ex.map(index_file, to_index):
                (ok_list if status == 'ok' else err_list).append(msg)
        log.info(f'index done: {len(ok_list)} ok, {len(err_list)} failed')

    # 删除已删文件的索引
    for f in deleted:
        remove_index(f)

    # 清理孤儿索引（results/ 中存在但 vault 里已无对应文件的）
    valid_index_names = {get_result_path(VAULT_PATH / rel).name for rel in curr}
    orphan_removed = 0
    for idx_file in RESULTS_DIR.glob('*_structure.json'):
        if idx_file.name not in valid_index_names:
            idx_file.unlink()
            orphan_removed += 1
            log.info(f'[cleanup] removed orphan index: {idx_file.name}')

    # --- 更新状态（索引失败的文件保留旧 mtime，下次重试）---
    for failed_rel in err_list:
        # err_list 格式：'rel_path: reason'
        rel = failed_rel.split(':')[0].strip()
        if rel in prev:
            curr[rel] = prev[rel]   # 恢复旧 mtime，触发下次重试
        else:
            curr.pop(rel, None)     # 新增但索引失败，下次当新增重试
    save_state(curr)

    # --- 构建报告 ---
    now = datetime.now().strftime('%Y-%m-%d %H:%M')
    lines = [f'📚 <b>Vault 日报</b> {now}', '']

    if not added and not deleted and not modified:
        lines.append('✅ 今日无变更')
    else:
        if added:
            lines.append(f'🆕 新增 ({len(added)})')
            for f in added[:10]:
                lines.append(f'  • {f}')
            if len(added) > 10:
                lines.append(f'  ...共 {len(added)} 个')

        if modified:
            lines.append(f'✏️ 修改 ({len(modified)})')
            for f in modified[:10]:
                lines.append(f'  • {f}')
            if len(modified) > 10:
                lines.append(f'  ...共 {len(modified)} 个')

        if deleted:
            lines.append(f'🗑️ 删除 ({len(deleted)})')
            for f in deleted[:10]:
                lines.append(f'  • {f}')
            if len(deleted) > 10:
                lines.append(f'  ...共 {len(deleted)} 个')

    lines.append('')
    if to_index:
        lines.append(f'⚙️ 索引结果：✅ {len(ok_list)} 成功 / ❌ {len(err_list)} 失败')
        for e in err_list[:5]:
            lines.append(f'  ⚠️ {e}')
    if orphan_removed:
        lines.append(f'🧹 清理孤儿索引：{orphan_removed} 个')

    report = '\n'.join(lines)
    log.info(f'report:\n{report}')
    tg_send(report)
    log.info('=== batch_index done ===')


if __name__ == '__main__':
    main()
