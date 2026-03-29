import os
import sys
import subprocess
import json
import hashlib
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from tqdm import tqdm

# --- 统一配置 ---
sys.path.insert(0, str(Path(__file__).parent.resolve()))
from settings import settings

MODEL = settings.model
VENV_PYTHON = str(settings.venv_python)
RUN_SCRIPT = str(settings.run_script)
NOTES_DIR = str(settings.vault_path)
RESULTS_DIR = str(settings.results_dir)
MAX_WORKERS = settings.max_workers

def get_result_filename(md_path):
    # 使用相对路径生成唯一的文件名
    rel_path = os.path.relpath(md_path, NOTES_DIR)
    # 将路径分隔符替换为横线，方便在文件系统中存储
    safe_name = rel_path.replace(os.sep, "__").replace(" ", "_")
    return os.path.splitext(safe_name)[0] + "_structure.json"

def is_already_done(result_path):
    if not os.path.exists(result_path):
        return False
    # 尝试加载 JSON 以验证其完整性
    try:
        with open(result_path, 'r', encoding='utf-8') as f:
            data = json.load(f)
            # 简单校验：是否存在 structure 字段
            return "structure" in data
    except Exception:
        return False

def process_file(md_path):
    result_filename = get_result_filename(md_path)
    result_path = os.path.join(RESULTS_DIR, result_filename)
    
    if is_already_done(result_path):
        return ("Skipped", md_path)

    try:
        # 调用 PageIndex 脚本
        # 注意：run_pageindex.py 默认将结果保存在 ./results/<basename>_structure.json
        # 我们需要先让它跑完，然后重命名结果文件以支持断点续传的路径唯一性
        cmd = [
            VENV_PYTHON, RUN_SCRIPT,
            "--md_path", md_path,
            "--model", MODEL,
            "--if-add-node-summary", "yes"
        ]
        
        result = subprocess.run(cmd, capture_output=True, text=True)
        
        if result.returncode == 0:
            # 寻找默认生成的结果文件并重命名为我们的唯一路径名
            default_output_name = os.path.splitext(os.path.basename(md_path))[0] + "_structure.json"
            default_output_path = os.path.join(RESULTS_DIR, default_output_name)
            
            if os.path.exists(default_output_path):
                # 如果目标文件已存在（之前的冲突文件），先删除
                if os.path.exists(result_path) and result_path != default_output_path:
                    os.remove(result_path)
                os.rename(default_output_path, result_path)
                
            return ("Success", md_path)
        else:
            return ("Error", f"{md_path}\n{result.stderr}")
    except Exception as e:
        return ("Exception", f"{md_path} - {str(e)}")

def main():
    if not os.path.exists(RESULTS_DIR):
        os.makedirs(RESULTS_DIR)

    md_files = []
    for root, dirs, files in os.walk(NOTES_DIR):
        # 排除隐藏目录
        dirs[:] = [d for d in dirs if not d.startswith('.')]
        for file in files:
            if file.endswith(".md"):
                md_files.append(os.path.join(root, file))

    print(f"Found {len(md_files)} Markdown files. Resuming from last progress...")
    
    # 过滤出需要处理的文件（用于显示真实的进度条）
    to_process = []
    skipped_count = 0
    for f in md_files:
        if is_already_done(os.path.join(RESULTS_DIR, get_result_filename(f))):
            skipped_count += 1
        else:
            to_process.append(f)

    print(f"Already Indexed: {skipped_count}")
    print(f"To Process: {len(to_process)}")

    if not to_process:
        print("All files are already indexed.")
        return

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        results = list(tqdm(executor.map(process_file, to_process), total=len(to_process)))

    # 统计
    success_count = sum(1 for status, _ in results if status == "Success")
    error_results = [msg for status, msg in results if status in ["Error", "Exception"]]
    
    print("\nBatch Processing Summary:")
    print(f"New Successes: {success_count}")
    print(f"New Errors: {len(error_results)}")
    
    if error_results:
        print("\nDetail of errors:")
        for err in error_results[:10]: # 只打印前10个错误
            print(f"- {err}")
        if len(error_results) > 10:
            print(f"... and {len(error_results) - 10} more errors.")

if __name__ == "__main__":
    main()
