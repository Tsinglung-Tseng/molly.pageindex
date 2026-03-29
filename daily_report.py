#!/usr/bin/env python3
"""
每日 Vault 变更检测 + 重新索引 + Telegram 报告
"""

import os
import sys
import json
from pathlib import Path
from datetime import datetime

# --- 统一配置 ---
PAGEINDEX_DIR = Path(__file__).parent.resolve()
os.chdir(PAGEINDEX_DIR)
sys.path.insert(0, str(PAGEINDEX_DIR))

from settings import settings
from indexing import get_result_path, run_index_file, VAULT_PATH, RESULTS_DIR

STATE_FILE       = PAGEINDEX_DIR / '.vault_state.json'
EXCLUDE_SYMLINKS = settings.exclude_symlinks
MAX_WORKERS      = settings.max_workers


# ---------------------------------------------------------------------------
# Telegram
# ---------------------------------------------------------------------------

def tg_send(text: str):
    if not settings.tg_enabled:
        print('[TG] disabled, skipping send')
        return
    import urllib.request
    url = f'https://api.telegram.org/bot{settings.tg_token}/sendMessage'
    data = json.dumps({'chat_id': settings.tg_chat_id, 'text': text, 'parse_mode': 'HTML'}).encode()
    req = urllib.request.Request(url, data=data, headers={'Content-Type': 'application/json'})
    try:
        urllib.request.urlopen(req, timeout=10)
    except Exception as e:
        print(f'[TG ERROR] {e}')


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
    status = run_index_file(VAULT_PATH / md_rel)
    if status == 'ok':
        return ('ok', md_rel)
    return ('err', f'{md_rel}: {status}')


def remove_index(md_rel: str):
    p = get_result_path(VAULT_PATH / md_rel)
    if p.exists():
        p.unlink()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)

    prev  = load_state()
    curr  = scan_vault()

    # 首次运行：只建状态快照，不触发索引
    if not prev:
        save_state(curr)
        tg_send(f'📚 <b>Vault 日报初始化</b>\n已记录 {len(curr)} 个文件的状态快照。\n明天起开始追踪变更。')
        print(f'[init] 记录 {len(curr)} 个文件，下次运行开始追踪变更。')
        return

    prev_set = set(prev)
    curr_set = set(curr)

    added    = sorted(curr_set - prev_set)
    deleted  = sorted(prev_set - curr_set)
    modified = sorted(
        f for f in curr_set & prev_set
        if curr[f] != prev[f]
    )

    to_index = added + modified

    # --- 重新索引 ---
    ok_list, err_list = [], []
    if to_index:
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
            for status, msg in ex.map(index_file, to_index):
                (ok_list if status == 'ok' else err_list).append(msg)

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
    print(report)
    tg_send(report)


if __name__ == '__main__':
    main()
